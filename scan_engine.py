"""
In-app market scanner — server-side port of the Mac's stock_scanner.py.

Ported from scripts/stock_scanner.py (canonical pre-registered spec, SCAN-01)
— any gate change must land in BOTH files or neither.

Runs inside the Streamlit app's own process (outbound HTTP only via
`requests` — no new credentials, no new ports, no daemons). Two universes:

  CORE      Nifty 500 — quality/growth candidates. Hard filters: uptrend
            (close > SMA200) + profitable quality (ROE >=15%, revenue
            growth >=8%, D/E <100%, positive margins) + size floor
            (mcap >= Rs 1000 Cr).
  SMALLCAP  Nifty Smallcap 250 — "future midcap" satellite experiment.
            LOW PRIOR, screening heuristic only. Hard filters: uptrend,
            revenue growth >=15%, earnings growth >=15%, ROE >=15%,
            D/E <80%, 20d avg traded value >= Rs 3 Cr/day, mcap < Rs 20,000 Cr.

Gates + score weights (25/20/20/20/15, same caps) are FROZEN — pre-registered
2026-07-20 (CODEX-081-QUANT v1.0), see scripts/stock_scanner.py's docstring
for the full spec. Do not tune after seeing output.

Data sources (same as the Mac scanner):
  1. Universe CSVs from archives.nseindia.com (Nifty 500 / Nifty Smallcap 250
     index constituent lists). Cached in app_state (7-day TTL), falls back to
     the cached copy on fetch failure.
  2. Prices from Yahoo Finance chart endpoint (query1.finance.yahoo.com/v8),
     symbol = NSE symbol + ".NS". Always fetched fresh.
  3. Fundamentals from Yahoo quoteSummary (query1.finance.yahoo.com/v10),
     cookie+crumb flow from fc.yahoo.com + /v1/test/getcrumb, refreshed once
     on "Invalid Crumb". Cached in app_state (7-day TTL, saved every 50 syms).
  4. ^NSEI (Nifty 50 index) 6-month return as the relative-strength benchmark.

Rate-limit resilience: this app runs from Streamlit Cloud's shared egress IP,
which is far more likely to get throttled by Yahoo than the Mac's residential
IP. We count consecutive HTTP 429/999/crumb failures; at 15 consecutive the
scan ABORTS and returns status "rate_limited" with whatever partial results
exist — it never hangs, never silently pretends to have scanned the universe.

Output shape matches the Mac scanner's scanner_output.json:
  {generated, spec, core[15], smallcap[10], stats, source, status}
"""

import csv
import io
import json
import time
import datetime as dt

import requests

import db

UNIVERSE_URLS = {
    "core": "https://archives.nseindia.com/content/indices/ind_nifty500list.csv",
    "small": "https://archives.nseindia.com/content/indices/ind_niftysmallcap250list.csv",
}
UNIVERSE_STATE_KEYS = {
    "core": "scanner_universe_core",
    "small": "scanner_universe_small",
}
FUND_CACHE_KEY = "scanner_fund_cache"

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=1y&interval=1d"
QSUMMARY_URL = ("https://query1.finance.yahoo.com/v10/finance/quoteSummary/{sym}"
                "?modules=financialData,defaultKeyStatistics,price,summaryDetail&crumb={crumb}")
UA = "Mozilla/5.0"
REQUEST_TIMEOUT = 25

CACHE_MAX_AGE_S = 7 * 86400
SLEEP_S = 0.15
RATE_LIMIT_ABORT_N = 15

CORE_TOP_N = 15
SMALL_TOP_N = 10

SPEC = "CODEX-081-QUANT v1.1 (gates/scoring unchanged from v1.0; +valuation/extension/diff context)"


# ---------------------------------------------------------------------------
# Rate-limit tracking — shared mutable state threaded through one scan run
# ---------------------------------------------------------------------------
class RateLimitAbort(Exception):
    """Raised internally when consecutive-failure count hits the abort line."""


class RunState:
    def __init__(self):
        self.consecutive_failures = 0
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": UA})

    def note_failure(self):
        self.consecutive_failures += 1
        if self.consecutive_failures >= RATE_LIMIT_ABORT_N:
            raise RateLimitAbort(
                f"{self.consecutive_failures} consecutive HTTP 429/999/crumb "
                "failures — aborting scan"
            )

    def note_success(self):
        self.consecutive_failures = 0


def _is_rate_limit_status(status_code):
    return status_code in (429, 999)


# ---------------------------------------------------------------------------
# universe loading (cached in app_state, 7-day TTL, fallback to stale cache)
# ---------------------------------------------------------------------------
def _fetch_universe_csv(kind, state):
    url = UNIVERSE_URLS[kind]
    try:
        resp = state.session.get(url, timeout=REQUEST_TIMEOUT)
        if _is_rate_limit_status(resp.status_code):
            state.note_failure()
            return None
        resp.raise_for_status()
        state.note_success()
        return resp.text
    except RateLimitAbort:
        raise
    except Exception:
        state.note_failure()
        return None


def load_universe(kind, state):
    """Return list of (symbol, name, sector), app_state-cached 7 days.

    Falls back to the cached copy (any age) on fetch failure, matching the
    Mac scanner's "stale cache beats no data" philosophy.
    """
    state_key = UNIVERSE_STATE_KEYS[kind]
    cached = db.get_app_state(state_key)
    rows_text = None
    now = dt.datetime.now(dt.timezone.utc)

    need_fetch = True
    if cached and cached.get("fetched_iso") and cached.get("rows"):
        try:
            fetched = dt.datetime.fromisoformat(cached["fetched_iso"])
            if (now - fetched).total_seconds() < CACHE_MAX_AGE_S:
                need_fetch = False
        except Exception:
            pass

    if need_fetch:
        txt = _fetch_universe_csv(kind, state)
        if txt and "Symbol" in txt:
            parsed = list(csv.DictReader(io.StringIO(txt)))
            rows_out = []
            for row in parsed:
                sym = (row.get("Symbol") or "").strip()
                name = (row.get("Company Name") or "").strip()
                sector = (row.get("Industry") or "").strip()
                if sym:
                    rows_out.append([sym, name, sector])
            if rows_out:
                db.set_app_state(state_key, {
                    "fetched_iso": now.isoformat(),
                    "rows": rows_out,
                })
                rows_text = rows_out
        if rows_text is None:
            # fetch failed or looked bad — fall back to whatever cache exists
            if cached and cached.get("rows"):
                rows_text = cached["rows"]
            else:
                raise ValueError(
                    f"universe download failed for {kind} and no cache exists"
                )
    else:
        rows_text = cached["rows"]

    return [(r[0], r[1], r[2]) for r in rows_text]


# ---------------------------------------------------------------------------
# fundamentals (cookie+crumb), cached in app_state
# ---------------------------------------------------------------------------
def get_cookie_and_crumb(state):
    try:
        state.session.get("https://fc.yahoo.com", timeout=REQUEST_TIMEOUT)
        r = state.session.get(
            "https://query1.finance.yahoo.com/v1/test/getcrumb",
            timeout=REQUEST_TIMEOUT,
        )
        if _is_rate_limit_status(r.status_code):
            state.note_failure()
            return ""
        state.note_success()
        return (r.text or "").strip()
    except RateLimitAbort:
        raise
    except Exception:
        state.note_failure()
        return ""


def load_fund_cache():
    cached = db.get_app_state(FUND_CACHE_KEY)
    if isinstance(cached, dict):
        # strip the get_app_state-injected "_updated" bookkeeping key
        return {k: v for k, v in cached.items() if k != "_updated"}
    return {}


def save_fund_cache(cache):
    db.set_app_state(FUND_CACHE_KEY, cache)


def _raw(d, *path):
    """Walk a dict path, unwrap Yahoo's {"raw": x} leaves."""
    cur = d
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur[k]
    if isinstance(cur, dict) and "raw" in cur:
        return cur["raw"]
    return cur


def fetch_fundamentals(sym_ns, crumb_state, state):
    """crumb_state is a mutable dict {'crumb': str} so we can refresh once."""
    url = QSUMMARY_URL.format(sym=sym_ns, crumb=crumb_state["crumb"])
    try:
        r = state.session.get(url, timeout=REQUEST_TIMEOUT)
    except RateLimitAbort:
        raise
    except Exception:
        state.note_failure()
        return None
    if _is_rate_limit_status(r.status_code):
        state.note_failure()
        return None
    txt = r.text or ""
    if "Invalid Crumb" in txt:
        crumb_state["crumb"] = get_cookie_and_crumb(state)
        url = QSUMMARY_URL.format(sym=sym_ns, crumb=crumb_state["crumb"])
        try:
            r = state.session.get(url, timeout=REQUEST_TIMEOUT)
        except RateLimitAbort:
            raise
        except Exception:
            state.note_failure()
            return None
        if _is_rate_limit_status(r.status_code):
            state.note_failure()
            return None
        txt = r.text or ""
    try:
        j = json.loads(txt)
    except Exception:
        state.note_failure()
        return None
    result = (j.get("quoteSummary") or {}).get("result")
    if not result:
        state.note_failure()
        return None
    state.note_success()
    r0 = result[0]
    return dict(
        marketCap=_raw(r0, "price", "marketCap"),
        returnOnEquity=_raw(r0, "financialData", "returnOnEquity"),
        revenueGrowth=_raw(r0, "financialData", "revenueGrowth"),
        earningsGrowth=_raw(r0, "financialData", "earningsGrowth"),
        debtToEquity=_raw(r0, "financialData", "debtToEquity"),
        profitMargins=_raw(r0, "financialData", "profitMargins"),
        # v1.1 DISPLAY-ONLY additions (not gates, not score inputs) — tolerate
        # absence, old cache entries fetched pre-v1.1 won't have summaryDetail.
        trailingPE=_raw(r0, "summaryDetail", "trailingPE"),
        priceToBook=_raw(r0, "defaultKeyStatistics", "priceToBook"),
    )


def get_fundamentals(sym_ns, cache, crumb_state, state):
    entry = cache.get(sym_ns)
    if entry:
        try:
            fetched = dt.datetime.fromisoformat(entry["fetched"])
            if (dt.datetime.now(dt.timezone.utc) - fetched).total_seconds() < CACHE_MAX_AGE_S:
                return entry["data"]
        except Exception:
            pass
    data = fetch_fundamentals(sym_ns, crumb_state, state)
    time.sleep(SLEEP_S)
    cache[sym_ns] = dict(
        fetched=dt.datetime.now(dt.timezone.utc).isoformat(), data=data
    )
    return data


# ---------------------------------------------------------------------------
# price series
# ---------------------------------------------------------------------------
def fetch_price_series(sym_ns, state):
    """Returns dict(close=last, sma200, ret6m, avgval20) or None."""
    url = CHART_URL.format(sym=sym_ns)
    try:
        r = state.session.get(url, timeout=REQUEST_TIMEOUT)
    except RateLimitAbort:
        raise
    except Exception:
        state.note_failure()
        return None
    if _is_rate_limit_status(r.status_code):
        state.note_failure()
        return None
    try:
        j = r.json()
    except Exception:
        state.note_failure()
        return None
    try:
        result = j["chart"]["result"][0]
        closes_raw = result["indicators"]["quote"][0]["close"]
        vols_raw = result["indicators"]["quote"][0]["volume"]
    except Exception:
        state.note_failure()
        return None
    state.note_success()
    pairs = [(c, v) for c, v in zip(closes_raw, vols_raw) if c is not None]
    if len(pairs) < 200:
        return None
    closes = [c for c, v in pairs]
    vols = [v if v is not None else 0 for c, v in pairs]
    last = closes[-1]
    sma200 = sum(closes[-200:]) / 200
    idx6m = -126 if len(closes) >= 126 else 0
    ret6m = (last / closes[idx6m]) - 1 if closes[idx6m] else None
    last20 = list(zip(closes[-20:], vols[-20:]))
    avgval20 = sum(c * v for c, v in last20) / len(last20) if last20 else None
    return dict(close=last, sma200=sma200, ret6m=ret6m, avgval20=avgval20)


def fetch_index_ret6m(state, sym="^NSEI"):
    ser = fetch_price_series(sym, state)
    return ser["ret6m"] if ser else None


# ---------------------------------------------------------------------------
# scoring — EXACT port of scripts/stock_scanner.py, gates FROZEN
# ---------------------------------------------------------------------------
def clamp01(x):
    return max(0.0, min(1.0, x))


def score_row(revenueGrowth, earningsGrowth, roe, rs6m, de):
    eg = earningsGrowth or 0.0
    s = (25 * clamp01(revenueGrowth / 0.30)
         + 20 * clamp01(eg / 0.30)
         + 20 * clamp01(roe / 0.30)
         + 20 * clamp01(max(rs6m, 0) / 0.30)
         + 15 * max(0.0, 1 - de / 100.0))
    return round(s, 1)


def passes_core_gates(close, sma200, mcap, roe, revg, de, margins):
    if close is None or sma200 is None or close <= sma200:
        return False
    needed = [mcap, roe, revg, de, margins]
    if any(v is None for v in needed):
        return False
    if not (mcap >= 1000e7):  # 1000 Cr = 1e10 INR
        return False
    if not (roe >= 0.15):
        return False
    if not (revg >= 0.08):
        return False
    if not (de < 100):
        return False
    if not (margins > 0):
        return False
    return True


def passes_smallcap_gates(close, sma200, mcap, roe, revg, earng, de, avgval20):
    if close is None or sma200 is None or close <= sma200:
        return False
    needed = [revg, roe, de, mcap, avgval20]
    if any(v is None for v in needed):
        return False
    if earng is None or not (earng >= 0.15):
        return False
    if not (revg >= 0.15):
        return False
    if not (roe >= 0.15):
        return False
    if not (de < 80):
        return False
    if not (avgval20 >= 3e7):
        return False
    if not (mcap < 2e11):
        return False
    return True


# ---------------------------------------------------------------------------
# per-universe scan
# ---------------------------------------------------------------------------
def scan_universe(kind, entries, index_ret6m, cache, crumb_state, top_n, state,
                   progress_cb=None, done_offset=0, total_all=0):
    passed = []
    skipped = 0
    total = len(entries)
    for i, (sym, name, sector) in enumerate(entries, 1):
        sym_ns = sym + ".NS"
        try:
            price = fetch_price_series(sym_ns, state)
            time.sleep(SLEEP_S)
            if not price or price["sma200"] is None:
                skipped += 1
                continue
            close = price["close"]
            sma200 = price["sma200"]
            ret6m = price["ret6m"]
            avgval20 = price["avgval20"]
            if close is None or sma200 is None or close <= sma200:
                skipped += 1
                continue
            fund = get_fundamentals(sym_ns, cache, crumb_state, state)
            if not fund:
                skipped += 1
                continue
            mcap = fund.get("marketCap")
            roe = fund.get("returnOnEquity")
            revg = fund.get("revenueGrowth")
            earng = fund.get("earningsGrowth")
            de = fund.get("debtToEquity")
            margins = fund.get("profitMargins")
            pe = fund.get("trailingPE")
            pb = fund.get("priceToBook")

            if kind == "core":
                if not passes_core_gates(close, sma200, mcap, roe, revg, de, margins):
                    skipped += 1
                    continue
            else:
                if not passes_smallcap_gates(close, sma200, mcap, roe, revg, earng, de, avgval20):
                    skipped += 1
                    continue

            if ret6m is None or index_ret6m is None:
                skipped += 1
                continue
            rs6m = ret6m - index_ret6m
            sc = score_row(revg, earng, roe, rs6m, de)
            # v1.1 DISPLAY-ONLY: extension above 200-DMA. Not a gate, not a
            # score input — pure context for how stretched the chart looks.
            ext_pct = round((close / sma200 - 1) * 100, 1) if sma200 else None
            passed.append(dict(
                sym=sym, name=name, sector=sector,
                price=round(close, 2),
                mcap_cr=round(mcap / 1e7, 1),
                roe_pct=round(roe * 100, 1),
                rev_g_pct=round(revg * 100, 1),
                earn_g_pct=(round(earng * 100, 1) if earng is not None else None),
                de=round(de, 1),
                rs6m_pct=round(rs6m * 100, 1),
                score=sc,
                # v1.1 DISPLAY-ONLY context columns (gates/scoring unchanged):
                pe=(round(pe, 1) if pe is not None else None),
                pb=(round(pb, 1) if pb is not None else None),
                ext_pct=ext_pct,
            ))
        except RateLimitAbort:
            raise
        except Exception:
            skipped += 1
            continue
        if i % 10 == 0 and progress_cb:
            progress_cb(done_offset + i, total_all,
                        f"{kind}: {i}/{total} scanned, {len(passed)} passed gates")
        if i % 50 == 0:
            save_fund_cache(cache)
    passed.sort(key=lambda r: r["score"], reverse=True)
    return passed[:top_n], len(passed), skipped


# ---------------------------------------------------------------------------
# "what changed" diff — v1.1, mirrors scripts/stock_scanner.py's diff_lists()
# ---------------------------------------------------------------------------
def _symset(rows):
    return {r["sym"] for r in (rows or [])}


def diff_lists(prev_scanner, core_top, small_top):
    """Diff old vs new core/smallcap symbol lists for the 'what changed' block.

    prev_scanner is the previous app_state["scanner"] dict (or {} / None on
    first v1.1 run / absent key) — same shape as this module's own output.
    First run (no prev_generated, i.e. absent old state): empty lists, not
    "everything is new" — there's nothing meaningful to diff against yet.
    """
    prev_scanner = prev_scanner or {}
    prev_generated = prev_scanner.get("generated")
    if not prev_generated:
        return dict(core_new=[], core_dropped=[], small_new=[], small_dropped=[],
                    prev_generated=None)
    prev_core = _symset(prev_scanner.get("core"))
    prev_small = _symset(prev_scanner.get("smallcap"))
    new_core = _symset(core_top)
    new_small = _symset(small_top)
    return dict(
        core_new=sorted(new_core - prev_core),
        core_dropped=sorted(prev_core - new_core),
        small_new=sorted(new_small - prev_small),
        small_dropped=sorted(prev_small - new_small),
        prev_generated=prev_generated,
    )


# ---------------------------------------------------------------------------
# main entry point
# ---------------------------------------------------------------------------
def run_scan(progress_cb=None, prev_scanner=None):
    """Run the full CORE + SMALLCAP scan. Returns a dict in the exact shape of
    the Mac scanner's scanner_output.json plus {source, status}.

    prev_scanner: the caller's current app_state["scanner"] dict, read BEFORE
    this call, so the "what changed" diff (v1.1) can compare against it.
    Optional — omit for no diff (empty-lists changes block).

    progress_cb(done, total, phase) is called roughly every 10 symbols.
    Never raises on rate-limiting — returns status "rate_limited" with
    whatever partial results were collected instead.
    """
    state = RunState()
    cache = load_fund_cache()
    stats = {}
    core_top, small_top = [], []
    partial = {}

    try:
        crumb_state = {"crumb": get_cookie_and_crumb(state)}

        if progress_cb:
            progress_cb(0, 1, "fetching universes + benchmark")
        index_ret6m = fetch_index_ret6m(state)

        core_entries = load_universe("core", state)
        small_entries = load_universe("small", state)
        total_all = len(core_entries) + len(small_entries)

        core_top, core_passed_n, core_skipped = scan_universe(
            "core", core_entries, index_ret6m, cache, crumb_state, CORE_TOP_N,
            state, progress_cb=progress_cb, done_offset=0, total_all=total_all,
        )
        stats["core_universe"] = len(core_entries)
        stats["core_passed"] = core_passed_n
        stats["core_skipped_datagaps"] = core_skipped
        save_fund_cache(cache)

        small_top, small_passed_n, small_skipped = scan_universe(
            "small", small_entries, index_ret6m, cache, crumb_state, SMALL_TOP_N,
            state, progress_cb=progress_cb, done_offset=len(core_entries),
            total_all=total_all,
        )
        stats["small_universe"] = len(small_entries)
        stats["small_passed"] = small_passed_n
        stats["small_skipped_datagaps"] = small_skipped
        save_fund_cache(cache)

        changes = diff_lists(prev_scanner, core_top, small_top)

        return dict(
            generated=dt.datetime.now().strftime("%d %b %Y · %H:%M IST"),
            spec=SPEC,
            core=core_top,
            smallcap=small_top,
            stats=stats,
            changes=changes,
            source="app",
            status="ok",
        )
    except RateLimitAbort as e:
        save_fund_cache(cache)
        partial = dict(
            generated=dt.datetime.now().strftime("%d %b %Y · %H:%M IST"),
            spec=SPEC,
            core=core_top,
            smallcap=small_top,
            stats=stats,
        )
        scanned_n = stats.get("core_passed", 0) + stats.get("core_skipped_datagaps", 0) \
            + stats.get("small_passed", 0) + stats.get("small_skipped_datagaps", 0)
        return dict(
            status="rate_limited",
            partial=partial,
            scanned_n=scanned_n,
            total_n=stats.get("core_universe", 0) + stats.get("small_universe", 0),
            source="app",
            error=str(e),
        )
    except Exception as e:
        return dict(
            status="error",
            partial=dict(core=core_top, smallcap=small_top, stats=stats),
            scanned_n=0,
            total_n=0,
            source="app",
            error=str(e),
        )
