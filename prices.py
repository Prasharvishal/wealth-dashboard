"""
Live price fetchers for the Household Wealth Dashboard.

Three free, no-key data sources, one per asset class:
  - Indian mutual funds  -> AMFI daily NAV text dump
  - Stocks / indices / global ETFs -> yfinance
  - Crypto (BTC, SOL, ...) -> CoinGecko free API

Every fetcher follows the same contract and the same failure philosophy:
  * Network/parse calls are wrapped in @st.cache_data(ttl=3600) so we hit each
    upstream at most once an hour (the spec's anti-hammer requirement).
  * On ANY failure (timeout, HTTP error, missing symbol, parse error) the
    fetcher returns None instead of raising. The caller — get_price_for_holding
    — then falls back to the last-stored DB price and flags the row "stale".
    The app therefore never crashes on a bad fetch.

All prices are returned in INR. yfinance tickers for Indian instruments must
carry the exchange suffix (e.g. RELIANCE.NS); global tickers (e.g. VOO) are
fetched in their native currency and converted to INR via the USD/INR rate.
"""

from datetime import datetime

import requests
import streamlit as st

try:
    import yfinance as yf
except Exception:  # pragma: no cover - yfinance optional at import time
    yf = None

import db

AMFI_URL = "https://www.amfiindia.com/spages/NAVAll.txt"
COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"
REQUEST_TIMEOUT = 15

# CoinGecko uses internal coin ids. Map common tickers the user might type.
COINGECKO_IDS = {
    "BTC": "bitcoin",
    "BTCUSD": "bitcoin",
    "BITCOIN": "bitcoin",
    "SOL": "solana",
    "SOLUSD": "solana",
    "SOLANA": "solana",
    "ETH": "ethereum",
    "USDT": "tether",
    "USDC": "usd-coin",
}


# ---------------------------------------------------------------------------
# USD -> INR (needed to normalise global/US holdings into INR)
# ---------------------------------------------------------------------------
@st.cache_data(ttl=3600, show_spinner=False)
def get_usd_inr():
    """USD/INR spot via yfinance (USDINR=X).

    Source: yfinance FX pair. Failure behaviour: returns a conservative
    fallback of 83.0 so global-asset valuation degrades gracefully rather than
    crashing — the number is clearly labelled as approximate in the UI.
    """
    if yf is None:
        return 83.0
    try:
        fx = yf.Ticker("USDINR=X")
        px = _yf_last_price(fx)
        return float(px) if px else 83.0
    except Exception:
        return 83.0


# ---------------------------------------------------------------------------
# AMFI mutual-fund NAVs
# ---------------------------------------------------------------------------
@st.cache_data(ttl=3600, show_spinner=False)
def fetch_amfi_navs():
    """Download + parse the full AMFI NAV dump into {scheme_code: nav_float}.

    Source: https://www.amfiindia.com/spages/NAVAll.txt (semicolon-delimited).
    Each data line is:
        Scheme Code;ISIN Payout;ISIN Reinvest;Scheme Name;NAV;Date
    Header rows, blank lines and AMC/category labels lack the 6-field shape and
    are skipped. Failure behaviour: returns an empty dict on any network/parse
    error, which makes every MF holding fall back to its last-stored NAV.
    """
    navs = {}
    try:
        resp = requests.get(AMFI_URL, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        for line in resp.text.splitlines():
            parts = line.split(";")
            if len(parts) < 6:
                continue
            code = parts[0].strip()
            nav_raw = parts[4].strip()
            if not code.isdigit():
                continue
            try:
                navs[code] = float(nav_raw)
            except ValueError:
                # NAVs occasionally appear as 'N.A.' for suspended schemes.
                continue
    except Exception:
        return {}
    return navs


def get_mf_nav(scheme_code):
    """Look up one scheme's NAV from the cached AMFI table. None if absent."""
    if not scheme_code:
        return None
    return fetch_amfi_navs().get(str(scheme_code).strip())


# ---------------------------------------------------------------------------
# Stocks / indices / global ETFs (yfinance)
# ---------------------------------------------------------------------------
def _yf_last_price(ticker_obj):
    """Best-effort last price from a yfinance Ticker, across API variants."""
    # fast_info is cheapest; fall back to history if it's unavailable.
    try:
        fi = ticker_obj.fast_info
        px = fi.get("last_price") if hasattr(fi, "get") else fi["last_price"]
        if px:
            return float(px)
    except Exception:
        pass
    try:
        hist = ticker_obj.history(period="5d")
        if not hist.empty:
            return float(hist["Close"].dropna().iloc[-1])
    except Exception:
        pass
    return None


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_stock_price(ticker, is_global=False):
    """Last price for a stock/index/ETF via yfinance, returned in INR.

    Source: yfinance (Yahoo Finance). Indian tickers need a .NS/.BO suffix and
    are already in INR; global tickers (is_global=True) come back in USD and
    are multiplied by the USD/INR rate. Failure behaviour: returns None on any
    error or unknown symbol so the caller can use the last-stored price.
    """
    if yf is None or not ticker:
        return None
    try:
        px = _yf_last_price(yf.Ticker(ticker))
        if px is None:
            return None
        if is_global:
            px *= get_usd_inr()
        return float(px)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Crypto (CoinGecko)
# ---------------------------------------------------------------------------
@st.cache_data(ttl=3600, show_spinner=False)
def fetch_crypto_price(coin_key):
    """Spot price in INR for a crypto asset via CoinGecko's free simple/price.

    Source: https://api.coingecko.com/api/v3/simple/price (no key required).
    `coin_key` may be a friendly ticker (BTC/SOL) mapped via COINGECKO_IDS or a
    raw CoinGecko id (e.g. 'bitcoin'). Failure behaviour: returns None on any
    error so the holding falls back to its last-stored price and is flagged
    stale.
    """
    if not coin_key:
        return None
    coin_id = COINGECKO_IDS.get(str(coin_key).upper(), str(coin_key).lower())
    try:
        resp = requests.get(
            COINGECKO_URL,
            params={"ids": coin_id, "vs_currencies": "inr"},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        return float(data[coin_id]["inr"])
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Unified resolver used by the app
# ---------------------------------------------------------------------------
def get_price_for_holding(holding):
    """Resolve the current INR price for one holding row.

    Routing:
      - manual override set        -> use it (never stale)
      - asset_type == 'mf'         -> AMFI NAV by scheme code
      - asset_type == 'stock'      -> yfinance (.NS etc., INR)
      - asset_type == 'global'     -> yfinance in USD -> INR
      - asset_type == 'crypto'     -> CoinGecko (INR)
      - asset_type == 'manual'     -> stored manual price / last price

    Returns (price, is_stale, source_label). On a live-fetch miss we return the
    last-stored DB price with is_stale=True; if there is no stored price either,
    we fall back to the holding's buy_price (still flagged stale) so the row can
    render rather than crash. On a successful live fetch we also persist the
    price back to the DB via db.cache_price for future fallback.
    """
    atype = (holding.get("asset_type") or "manual").lower()
    manual = holding.get("manual_price")

    # Explicit manual override always wins and is considered fresh.
    if manual not in (None, "") and atype == "manual":
        return float(manual), False, "manual"
    if manual not in (None, ""):
        return float(manual), False, "manual override"

    price = None
    source = atype
    if atype == "mf":
        price = get_mf_nav(holding.get("ticker"))
        source = "AMFI"
    elif atype == "stock":
        price = fetch_stock_price(holding.get("ticker"), is_global=False)
        source = "yfinance"
    elif atype == "global":
        price = fetch_stock_price(holding.get("ticker"), is_global=True)
        source = "yfinance (USD→INR)"
    elif atype == "crypto":
        price = fetch_crypto_price(holding.get("ticker"))
        source = "CoinGecko"

    if price is not None:
        # Persist for future stale-fallback, then return fresh.
        try:
            db.cache_price(holding["id"], price)
        except Exception:
            pass
        return price, False, source

    # --- Fetch failed: graceful degradation ---
    last = holding.get("last_price")
    if last not in (None, ""):
        return float(last), True, f"{source} (stale)"
    # No stored price at all — fall back to buy price so we never crash.
    return float(holding.get("buy_price") or 0.0), True, "buy price (no data)"


def last_price_age(holding):
    """Human-readable age of the stored price, for the 'stale' tooltip."""
    ts = holding.get("last_price_time")
    if not ts:
        return "never"
    try:
        dt = datetime.fromisoformat(ts)
        delta = datetime.now() - dt
        hours = delta.total_seconds() / 3600
        if hours < 1:
            return f"{int(delta.total_seconds() // 60)}m ago"
        if hours < 48:
            return f"{int(hours)}h ago"
        return f"{int(hours // 24)}d ago"
    except Exception:
        return ts
