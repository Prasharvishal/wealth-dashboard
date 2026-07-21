"""
Household Wealth Dashboard — a FIRE wealth tracker for a two-person household.

Streamlit single-instance app, shared by two users behind a shared-secret gate.
Live prices auto-fetch (AMFI / yfinance / CoinGecko) with manual override and
stale-fallback; everything the users type persists in SQLite (wealth.db).

Run locally:   streamlit run app.py
Deploy:        Streamlit Community Cloud (see README.md)

Module layout:
  db.py        — SQLite persistence (holdings, cashflow, goals, settings, allocation)
  prices.py    — live price fetchers + graceful stale fallback
  fi_engine.py — deterministic FI projection (3 scenarios, real + nominal)
  app.py       — this file: auth gate, theme, 9 tabs (Start Here, Net Worth &
                 Allocation, Targets, Deploy This Month, Scanner, Cash Flow &
                 Savings, FI Projection, Holdings, Goals & Rebalance), charts
"""

import re
from datetime import datetime, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import db
import prices
import returns
from fi_engine import FIInputs, project, run_scenarios

# ---------------------------------------------------------------------------
# Page config + DB bootstrap
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="MAVI Vault — FIRE Dashboard",
    page_icon="💰",
    layout="wide",
    initial_sidebar_state="collapsed",
)

db.init_db()  # idempotent: creates tables + seeds locked allocation on first run
db.pull()     # Turso mode: refresh local replica from cloud (no-op on local SQLite)

# Accent palette (kept in sync with .streamlit/config.toml).
GOLD = "#d4af37"
TEAL = "#2dd4bf"
BLUE = "#4f9cf9"
RED = "#f85149"
GREEN = "#3fb950"
MUTED = "#8b949e"
SLEEVE_COLORS = {
    "Nifty 50 (core)": "#4f9cf9",
    "Nifty Next 50": "#2dd4bf",
    "Direct Stocks": "#f0883e",   # orange — was gold, too close to the Gold sleeve
    "Global / US": "#a371f7",
    "Gold": "#e3b341",
    "Tactical / thematic": "#56d364",
}

# Plain-English "what do I actually buy for this sleeve" hints, shown in the
# Deploy tab so a non-expert knows where the money goes.
# Crypto entry retired 2026-07-21 (targets sitting) — crypto exposure now
# lives only via the Sentinel probation charter, never the Vault sleeves.
INSTRUMENT_HINTS = {
    "Nifty 50 (core)": "Nifty 50 index fund / ETF (e.g. UTI or Nippon Nifty 50 Index Fund)",
    "Nifty Next 50": "Nifty Next 50 index fund (e.g. ICICI/UTI Next 50 Index Fund)",
    "Direct Stocks": "Your own researched stocks (the 20% you pick yourself)",
    "Global / US": "S&P 500 / Nasdaq-100 fund (e.g. Motilal Oswal S&P 500 Index Fund)",
    "Gold": "Gold ETF, Sovereign Gold Bond, or digital gold",
    "Tactical / thematic": "Your thematic / sector bets (e.g. PSU, energy, momentum funds)",
}

# ONE-HOUSEHOLD layer doctrine (user decision 2026-07-20): no separate "family"
# grouping. Ownership stays visible in each holding's NAME only — these two
# non-target sleeve labels fold into the household's two real layers:
#   "Family (Parents)" (the 6 NSCs held by parents) -> locked/safety layer,
#     same bucket as "Debt / Safety" (EPF, post office) — guaranteed but
#     pledge-able, not spendable, NOT an emergency fund.
#   "Family (Father)" (his HDFC Mid-Cap MF)          -> growth/market layer,
#     alongside the 7 target sleeves — it is a real market asset that should
#     count toward net worth growth and (if ever tagged) sleeve gap math.
SAFETY_LAYER_SLEEVES = {"Debt / Safety", "Family (Parents)"}
GROWTH_EXTRA_SLEEVES = {"Family (Father)"}  # counts as growth but has no target %

# Emergency fund as a first-class sleeve (Feature A, 2026-07-20). Its own
# fourth layer bucket — deliberately NOT in growth (no target %, not the
# rebalance/gap pool), NOT in the locked safety floor (this money must stay
# genuinely liquid, unlike EPF/NSCs), NOT transit (it has no landing date,
# it just sits ready). Until a holding exists in this sleeve the Deploy tab
# keeps today's manual-input behaviour — this is additive, never a forced
# migration of existing data.
EMERGENCY_SLEEVE = "🚨 Emergency (liquid)"

# Soft, modern CSS polish — rounded cards, breathing room, mobile friendly.
st.markdown(
    """
    <style>
      .block-container {padding-top: 1.6rem; padding-bottom: 3rem; max-width: 1320px;}
      h1, h2, h3, h4, h5 {letter-spacing: -0.01em;}
      /* KPI / metric cards: glass panels, gold hairline, gentle lift on hover */
      div[data-testid="stMetric"] {
        background: linear-gradient(165deg,#1d2531 0%, #161c25 100%);
        border: 1px solid #243040; border-top: 2px solid rgba(212,175,55,.45);
        border-radius: 15px; padding: 16px 18px;
        box-shadow: 0 2px 10px rgba(0,0,0,0.3);
        transition: transform .18s ease, border-color .18s ease;
      }
      div[data-testid="stMetric"]:hover {transform: translateY(-2px); border-color:#3a4656;}
      [data-testid="stMetricValue"] {font-size: 1.7rem; font-weight: 650;}
      [data-testid="stMetricLabel"] {color: #9aa7b4; font-weight: 500; letter-spacing:.02em;}
      /* Tabs: pill-style, gold active state */
      .stTabs [data-baseweb="tab-list"] {gap: 6px; flex-wrap: wrap; border-bottom: none;}
      .stTabs [data-baseweb="tab"] {
        background:#1a2029; border-radius: 10px; padding: 8px 14px;
        font-weight: 500; border:1px solid transparent;
      }
      .stTabs [aria-selected="true"] {
        background:linear-gradient(160deg,#2c3543,#252e3a) !important;
        color:#e0b84c !important; border:1px solid rgba(212,175,55,.35) !important;
        box-shadow: 0 0 14px rgba(212,175,55,.08);
      }
      /* Buttons: rounder, friendlier */
      .stButton button, .stForm button {border-radius: 10px; font-weight: 600;}
      /* Inputs: softer corners */
      input, .stNumberInput, .stTextInput, .stSelectbox {border-radius: 10px;}
      .stale-flag {color:#f0a868; font-size:0.78rem;}
      .step-pill {display:inline-block; background:#2a3340; color:#e0b84c;
        border-radius:999px; padding:2px 12px; font-size:0.8rem; font-weight:600;
        margin-bottom:6px;}
      /* Progress bars: gold gradient */
      .stProgress > div > div > div > div {
        background: linear-gradient(90deg,#d4af37,#3fb950); border-radius: 6px;
      }
      /* Hero wordmark */
      .vault-hero {display:flex; align-items:baseline; gap:14px; flex-wrap:wrap;
        padding: 2px 0 10px; border-bottom:1px solid rgba(255,255,255,.06); margin-bottom:14px;}
      .vault-hero .wm {font-size:1.55rem; font-weight:750; letter-spacing:.14em;}
      .vault-hero .wm b {color:#d4af37;}
      .vault-hero .sub {color:#8b949e; font-size:.78rem; letter-spacing:.22em;
        text-transform:uppercase;}
      .vault-hero .who {margin-left:auto; color:#8b949e; font-size:.82rem;
        background:#1a2029; border:1px solid #243040; border-radius:999px; padding:3px 14px;}
      /* Money-timeline event chips */
      .ev-row {display:flex; gap:10px; overflow-x:auto; padding:6px 0 10px;}
      .ev-chip {min-width:185px; background:linear-gradient(165deg,#1d2531,#161c25);
        border:1px solid #243040; border-radius:13px; padding:11px 14px; flex-shrink:0;}
      .ev-chip .d {font-size:1.25rem; font-weight:700; font-variant-numeric:tabular-nums;}
      .ev-chip .d small {font-size:.68rem; color:#8b949e; font-weight:400; margin-left:5px;}
      .ev-chip .n {font-size:.78rem; line-height:1.35; margin-top:3px; color:#c9d1d9;}
      .ev-chip .h {font-size:.7rem; color:#8b949e; margin-top:2px;}
      .ev-u7  {border-color:rgba(248,81,73,.55); box-shadow:0 0 14px rgba(248,81,73,.12);}
      .ev-u7  .d {color:#f85149;}
      .ev-u30 {border-color:rgba(227,179,65,.5);} .ev-u30 .d {color:#e3b341;}
      .ev-uX  .d {color:#4f9cf9;}
      @media (max-width: 640px) {.block-container {padding-left:0.6rem; padding-right:0.6rem;}}
    </style>
    """,
    unsafe_allow_html=True,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def fmt_inr(x):
    """Format a rupee amount in the Indian lakh/crore convention."""
    try:
        x = float(x)
    except (TypeError, ValueError):
        return "₹0"
    sign = "-" if x < 0 else ""
    x = abs(x)
    if x >= 1e7:
        return f"{sign}₹{x/1e7:.2f} Cr"
    if x >= 1e5:
        return f"{sign}₹{x/1e5:.2f} L"
    if x >= 1e3:
        return f"{sign}₹{x/1e3:.1f} K"
    return f"{sign}₹{x:,.0f}"


def fmt_full_inr(x):
    try:
        return f"₹{float(x):,.0f}"
    except (TypeError, ValueError):
        return "₹0"


def s_float(key, default=0.0):
    """Read a setting as float with a safe default."""
    try:
        return float(db.get_setting(key, default))
    except (TypeError, ValueError):
        return float(default)


def s_int(key, default=0):
    try:
        return int(float(db.get_setting(key, default)))
    except (TypeError, ValueError):
        return int(default)


def compute_holdings_table():
    """Price every holding, compute value / cost / P&L / CAGR / XIRR. Returns DataFrame."""
    rows = []
    any_stale = False
    # Fetched once outside the loop (single API call, not one per holding) —
    # same pattern as prices.get_usd_inr's shared-fetch approach.
    _all_txns = db.get_transactions()
    _txns_by_holding = {}
    for _t in _all_txns:
        _txns_by_holding.setdefault(_t.get("holding_id"), []).append(_t)

    for h in db.get_holdings():
        price, stale, source = prices.get_price_for_holding(h)
        any_stale = any_stale or stale
        qty = h.get("quantity") or 0.0
        buy = h.get("buy_price") or 0.0
        value = qty * price
        cost = qty * buy
        pl = value - cost
        pl_pct = (pl / cost * 100.0) if cost else 0.0

        # CAGR from buy_date to today (annualised), guarding short/zero holds.
        cagr = None
        bd = h.get("buy_date")
        if bd and buy > 0 and price > 0:
            try:
                d0 = datetime.fromisoformat(str(bd))
                years = (datetime.now() - d0).days / 365.25
                if years > 0.08:  # ~1 month minimum to make CAGR meaningful
                    cagr = ((price / buy) ** (1 / years) - 1) * 100.0
            except Exception:
                cagr = None

        # Forward-measured XIRR from the transactions ledger (epoch-split
        # doctrine). "—" renders automatically via returns.fmt_xirr when
        # there's no ledger history yet or < 30 days of it (returns.py
        # MIN_DAYS_FOR_XIRR) — never an absurd day-one annualized number.
        _h_txns = _txns_by_holding.get(h["id"], [])
        holding_xirr_val = returns.compute_xirr_for_transactions(_h_txns, value)

        rows.append({
            "id": h["id"],
            "Asset": h["asset_name"],
            "Sleeve": h["sleeve"],
            "Type": h["asset_type"],
            "Ticker": h.get("ticker") or "",
            "Qty": qty,
            "Buy": buy,
            "Price": price,
            "Value": value,
            "Cost": cost,
            "P/L": pl,
            "P/L %": pl_pct,
            "CAGR %": cagr,
            "XIRR (fwd)": holding_xirr_val,
            "Stale": stale,
            "Source": source,
            "Age": prices.last_price_age(h),
            "Maturity": h.get("maturity_date"),
            "Prov": h.get("maturity_provenance"),
        })
    df = pd.DataFrame(rows)
    return df, any_stale


def growth_chart_figure(history_rows):
    """Line chart of networth_history: assets + growth-layer lines, loans as
    a declining negative/secondary line, emergency layer once non-zero.

    Uses the app's existing plotly.graph_objects approach (same pattern as
    the donut/bar charts elsewhere in this file) rather than introducing a
    second charting library. Returns None if there's no history yet — the
    caller renders the "building your history" caption in that case.
    """
    if not history_rows:
        return None
    hdf = pd.DataFrame(history_rows).sort_values("date")
    fig = go.Figure()
    fig.add_scatter(x=hdf["date"], y=hdf["assets"], name="Total assets",
                    mode="lines+markers", line=dict(color=TEAL, width=2))
    fig.add_scatter(x=hdf["date"], y=hdf["growth"], name="Growth sleeve",
                    mode="lines+markers", line=dict(color=GOLD, width=2))
    if "emergency" in hdf.columns and (hdf["emergency"].fillna(0) != 0).any():
        fig.add_scatter(x=hdf["date"], y=hdf["emergency"], name="Emergency",
                        mode="lines+markers", line=dict(color=BLUE, width=2))
    if "loans" in hdf.columns:
        fig.add_scatter(x=hdf["date"], y=-hdf["loans"].fillna(0), name="Loans (–)",
                        mode="lines+markers", line=dict(color=RED, width=2, dash="dot"))
    fig.update_layout(
        template="plotly_dark", height=340,
        margin=dict(t=10, b=10, l=10, r=10),
        legend=dict(orientation="h", y=1.15),
        yaxis_title="₹",
    )
    return fig


def market_context():
    """Live 'cheap or expensive vs its own 200-day average' per sleeve, keyed by
    the EXACT sleeve name so each sleeve only ever sees its own row.

    Rows are pushed into Turso by MAVI Sentinel twice daily (06:05 / 17:15 IST)
    from the same verified price feeds the watchers use. Informational ONLY:
    it never reorders targets or overrides the allocation rules — it tells you
    what kind of price you're paying when you deploy.

    Fix 3 (2026-07-20): order by `updated` ASC so that if Sentinel ever leaves
    more than one row for the same sleeve (a stale row alongside a fresh one),
    the dict comprehension's last-write-wins keeps the MOST RECENT row, not
    whichever happened to be returned last by an unordered SELECT. Sleeve keys
    are also stripped so incidental whitespace can't create a silent mismatch.
    Never falls back to a different sleeve's benchmark — a sleeve with no row
    of its own gets no badge (see context_badge below).
    """
    try:
        rows = db.query("SELECT * FROM market_context ORDER BY updated ASC")
    except Exception:
        try:
            rows = db.query("SELECT * FROM market_context")  # no `updated` column
        except Exception:
            return {}
    return {str(r["sleeve"]).strip(): r for r in rows if r.get("sleeve")}


def context_badge(mc, sleeve):
    """One-line price-context sentence for a sleeve, or '' if no benchmark.

    Wording is deliberately NEUTRAL (Codex 082 ruling): state position vs the
    200-day average and trend state only — never imply cheap = attractive.
    Sleeve gaps decide priority; this line is context, not a signal.

    Strict exact-sleeve lookup only (Fix 3, 2026-07-20) — a sleeve with no
    matching market_context row (e.g. "Direct Stocks", which has no single
    tradable benchmark) renders NO badge. Never borrows another sleeve's
    benchmark (e.g. NIFTYBEES) just because one happens to be on file.
    """
    r = mc.get(str(sleeve).strip())
    if not r or r.get("dist_sma") is None:
        return ""
    d = float(r["dist_sma"])
    side = "below" if d < 0 else "above"
    base = f"{r['benchmark']} is {abs(d):.0f}% {side} its 200-day average"
    if d <= -15:
        note = "trend weak / not repaired — deploy in tranches; the allocation cap still applies"
    elif d < 0:
        note = "trend not repaired"
    elif d <= 10:
        note = "trend intact"
    else:
        note = "well above its average — no urgency to add"
    return f" · 📊 {base} ({note})"


def suggestion_chip(sleeve, sleeve_pct, target_pct):
    """Per-holding suggestion chip: (label, reason) for the Holdings table.

    Locked sleeves never get a rebalance opinion — they're outside the growth
    plan by design. Family (Father) always says continue-SIP (never break the
    best compounder in the house to prepay). Transit money is dateless once it
    lands, so it always needs a destination. Everything else compares its
    sleeve's current % against target using the same ±deviation bands as the
    Rebalance signal (Codex 082 doctrine: gaps decide priority, not opinion).
    """
    if sleeve in ("Debt / Safety", "Family (Parents)"):
        return "HOLD · locked", "guaranteed but locked — pledge-able, not spendable"
    if sleeve == "Family (Father)":
        return "CONTINUE SIP", "best compounder in the house — never break to prepay"
    if sleeve == "Transit (redeploying)":
        return "REDEPLOY", "dated money awaiting its landing-plan destination"
    if sleeve == EMERGENCY_SLEEVE:
        return ("HOLD · liquid",
                f"goal-tracked ({fmt_inr(_em_goal_amount)}), not a %-target sleeve — "
                "keep it reachable")
    if target_pct is None:
        return "HOLD", "no target set for this sleeve yet"
    if sleeve_pct < target_pct * 0.8:
        return "INCREASE", f"sleeve at {sleeve_pct:.0f}% vs {target_pct:.0f}% target — underweight"
    if sleeve_pct > target_pct * 1.5:
        return "PAUSE", f"sleeve at {sleeve_pct:.0f}% vs {target_pct:.0f}% target — overweight"
    return "HOLD", f"sleeve near target ({sleeve_pct:.0f}% vs {target_pct:.0f}%)"


MAX_MATURITY_PARSE_MULTIPLE = 20.0  # reject a name-parsed value >20x or <1/20x current Value


def parse_maturity_value_from_name(name, current_value=None):
    """Best-effort ₹ maturity value embedded in a holding's asset name.

    Looks for an Indian-lakh/crore-style amount near a ₹ sign, e.g.
    "NSC — matures ₹5.2L 2029" -> 520000.0, "Fund ₹36.2L" -> 3620000.0,
    "₹50000" -> 50000.0. Returns None if nothing parses, OR if what parsed
    fails a plausibility check against the holding's current priced value
    (this function never guesses a number that isn't textually present, and
    never hands back a wildly implausible one either — review fix: an
    unanchored name like "Fund managed by ₹500 Cr AUM firm" would otherwise
    parse as a ₹500 crore "maturity value" with nothing to catch it).

    `current_value`, if given, bounds the accepted parse to within
    MAX_MATURITY_PARSE_MULTIPLE x the holding's current Value in either
    direction — a real NSC maturity value is a modest multiple of what it's
    worth today (interest accrual), never orders of magnitude off.
    """
    if not name:
        return None
    m = re.search(r"₹\s*([\d,]+\.?\d*)\s*(cr|crore|l|lakh|lac)?", str(name), re.IGNORECASE)
    if not m:
        return None
    try:
        amt = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    unit = (m.group(2) or "").lower()
    if unit in ("cr", "crore"):
        amt *= 1e7
    elif unit in ("l", "lakh", "lac"):
        amt *= 1e5
    if current_value and current_value > 0:
        ratio = amt / current_value
        if ratio > MAX_MATURITY_PARSE_MULTIPLE or ratio < (1.0 / MAX_MATURITY_PARSE_MULTIPLE):
            return None  # implausible vs the holding's actual priced value — reject
    return amt


def upcoming_events(df):
    """Date-aware money radar: maturities from holdings + the monthly review ritual.

    Returns a list of dicts sorted by days-to-event. 'kind' drives styling:
    transit = money landing for redeployment, safety = long-horizon maturity,
    ritual = recurring process date.
    """
    ev = []
    today = datetime.now().date()
    if not df.empty and "Maturity" in df.columns:
        for _, r in df.iterrows():
            if not r.get("Maturity"):
                continue
            try:
                d = (datetime.fromisoformat(str(r["Maturity"])).date() - today).days
            except Exception:
                continue
            kind = "transit" if "Transit" in str(r["Sleeve"]) else "safety"
            prov = r.get("Prov") if pd.notna(r.get("Prov")) else "estimated"
            ev.append({"name": r["Asset"], "value": r["Value"], "date": str(r["Maturity"]),
                       "days": d, "kind": kind, "prov": prov, "sleeve": r["Sleeve"]})
    nm = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
    ev.append({"name": "Monthly Direct-Stocks review (10-min rulebook pass)",
               "value": 0, "date": nm.isoformat(), "days": (nm - today).days,
               "kind": "ritual", "prov": "confirmed", "sleeve": None})
    ev.sort(key=lambda e: e["days"])
    return ev


IDLE_TRANSIT_AGE_DAYS = 30   # "sat too long with no destination"
IDLE_TRANSIT_HORIZON_DAYS = 45  # a maturity inside this window means it's NOT idle


def detect_idle_transit(df):
    """Feature D (charter Phase B4): flag Transit-sleeve money that's been
    sitting with no near-term destination.

    Rule: a Transit (redeploying) holding is IDLE if its buy_date is more
    than IDLE_TRANSIT_AGE_DAYS old AND it has no maturity_date within the
    next IDLE_TRANSIT_HORIZON_DAYS.

    buy_date fallback: the spec also allows deriving an age from
    "maturity_date minus tenure" when buy_date is absent — but holdings have
    no tenure column in this schema (tenure only exists on liabilities;
    verified against db.py/init_db and the live liabilities query). Rather
    than invent a number, a Transit holding with NO buy_date and no way to
    compute an age is flagged SEPARATELY as "age unknown" (review fix,
    2026-07-20 — the earlier version silently skipped these entirely, which
    is exactly backwards: a holding with no recorded start date is the
    likeliest real data-gap case, not a safe case to stay silent on).

    Emergency (liquid) shortfalls are explicitly out of scope — that is the
    waterfall's job (Deploy tab), never this detector's.

    Returns a list of dicts, oldest/most-uncertain first. Each dict has
    `name`, `value`, and either `days_held` (int, a real computed age) or
    `age_unknown=True` (no buy_date to compute from) — callers should check
    for `age_unknown` before formatting `days_held`.
    """
    if df.empty:
        return []
    today = datetime.now().date()
    idle = []
    tdf = df.loc[df["Sleeve"] == "Transit (redeploying)"]
    # `df` here is the priced holdings DataFrame (Asset/Sleeve/Value/... — see
    # compute_holdings_table), which does NOT carry buy_date as a bare column
    # (only "Maturity"/"Prov" made it in). Pull buy_date from the raw holdings
    # rows (db.get_holdings()) keyed by id.
    raw_by_id = {h["id"]: h for h in db.get_holdings()}
    for _, r in tdf.iterrows():
        h = raw_by_id.get(r.get("id"))
        if not h:
            continue
        buy_date_raw = h.get("buy_date")
        maturity_raw = h.get("maturity_date")
        if not buy_date_raw:
            idle.append({"name": r["Asset"], "value": r["Value"], "age_unknown": True})
            continue
        try:
            buy_date = datetime.fromisoformat(str(buy_date_raw)).date()
        except Exception:
            idle.append({"name": r["Asset"], "value": r["Value"], "age_unknown": True})
            continue
        days_held = (today - buy_date).days
        if days_held <= IDLE_TRANSIT_AGE_DAYS:
            continue
        has_near_maturity = False
        if maturity_raw:
            try:
                mdate = datetime.fromisoformat(str(maturity_raw)).date()
                if 0 <= (mdate - today).days <= IDLE_TRANSIT_HORIZON_DAYS:
                    has_near_maturity = True
            except Exception:
                pass
        if not has_near_maturity:
            idle.append({"name": r["Asset"], "value": r["Value"], "days_held": days_held,
                        "age_unknown": False})
    idle.sort(key=lambda x: -(x["days_held"] if not x.get("age_unknown") else 10**9))
    return idle


def sleeve_breakdown(df):
    """Current value per sleeve + target % comparison. Returns (DataFrame, pool_total).

    USER DECISION 2026-07-20 (Option A + concentration guard): the pool total is
    TARGET SLEEVES ONLY. "Family (Father)" (GROWTH_EXTRA_SLEEVES) is a real market
    asset with no target % of its own — it counts toward net worth and the
    growth-layer summary (added on top of this pool at the call site), but it must
    NEVER dilute the target-sleeve denominator. Before this fix, `total` mixed in
    GROWTH_EXTRA_SLEEVES while the per-sleeve loop only ever summed target sleeves,
    so every target sleeve's Current % was silently deflated (e.g. Global/US showed
    ~55% on the donut but ~11% "underweight" in Current %/deviations — contradictory
    numbers from one bug). This pool feeds Current %, Deviation, deploy_plan's
    gap math, and the Goals & Rebalance priorities — all of it must agree.
    """
    targets = dict(db.get_target_allocation())
    total = (df.loc[df["Sleeve"].isin(targets), "Value"].sum() if not df.empty else 0.0)
    data = []
    for sleeve, tgt in db.get_target_allocation():
        cur_val = df.loc[df["Sleeve"] == sleeve, "Value"].sum() if not df.empty else 0.0
        cur_pct = (cur_val / total * 100.0) if total else 0.0
        data.append({
            "Sleeve": sleeve,
            "Value": cur_val,
            "Current %": cur_pct,
            "Target %": tgt,
            "Deviation": cur_pct - tgt,
        })
    return pd.DataFrame(data), total


# Concentration-guard fallbacks (Fix 2, 2026-07-20 "Option A + concentration
# guard" decision). app_state["sentinel_manifest"]["concentration_guard"]
# overrides these when present: {threshold_pct, selfheal_note}.
CONCENTRATION_THRESHOLD_DEFAULT = 40.0
CONCENTRATION_SELFHEAL_DEFAULT = (
    "Self-heals to ~40% by ~2031 at current flows (your ₹1L/mo outgrows it)."
)


def concentration_check(holdings_df, growth_total):
    """Single-holding concentration inside the growth layer (target sleeves +
    GROWTH_EXTRA_SLEEVES). Returns the single worst offender as a dict
    {name, value, pct, threshold_pct, selfheal_note} if any ONE holding is
    over threshold, else None.

    Deliberately per-HOLDING, not per-sleeve: a sleeve can be within its target
    band while still being dominated by one name (e.g. Family (Father)'s HDFC
    Mid-Cap fund, which has no sleeve target at all — sleeve-level deviation
    would never catch it). share = holding value / growth-layer total, where
    growth-layer total is the SAME denominator as net_worth_growth (target
    sleeves + GROWTH_EXTRA_SLEEVES), never the target-sleeve-only pool.
    """
    if holdings_df.empty or growth_total <= 0:
        return None
    targets = set(dict(db.get_target_allocation()))
    growth_sleeves = targets | GROWTH_EXTRA_SLEEVES
    gdf = holdings_df.loc[holdings_df["Sleeve"].isin(growth_sleeves)]
    if gdf.empty:
        return None

    manifest = db.get_app_state("sentinel_manifest")
    cg = manifest.get("concentration_guard") or {}
    threshold = float(cg.get("threshold_pct") or CONCENTRATION_THRESHOLD_DEFAULT)
    selfheal_note = cg.get("selfheal_note") or CONCENTRATION_SELFHEAL_DEFAULT

    worst = None
    for _, row in gdf.iterrows():
        value = float(row["Value"] or 0.0)
        pct = (value / growth_total * 100.0) if growth_total else 0.0
        if pct > threshold and (worst is None or pct > worst["pct"]):
            worst = {"name": row["Asset"], "value": value, "pct": pct,
                     "threshold_pct": threshold, "selfheal_note": selfheal_note}
    return worst


def concentration_warning_text(hit):
    """Render the standard concentration-guard banner text from a
    concentration_check() hit dict."""
    return (
        f"⚠ Single-fund concentration: {hit['name']} = {hit['pct']:.0f}% of "
        f"household market assets. {hit['selfheal_note']} Standing rule: your "
        f"new money never buys midcap until this is <{hit['threshold_pct']:.0f}%."
    )


def deploy_plan(amount, sleeve_df, growth_total, mode="rebalance"):
    """Split a fresh monthly investable amount across the sleeves.

    Two modes:
      * "simple"     — pure target-weight split (₹1L → 25% Nifty 50, etc.).
                       Easy to understand; ignores what you already hold.
      * "rebalance"  — cash-flow rebalancing. Steers NEW money toward the
                       sleeves that are currently *underweight* vs target, so
                       you drift back to your target mix WITHOUT selling
                       anything (no tax, no churn). This is the smarter default
                       for someone still accumulating.

    With an empty/near-target portfolio the two modes coincide, so a brand-new
    user with ₹0 holdings gets a clean target split either way.

    Returns a list of (sleeve, rupee_amount) preserving canonical sleeve order.
    """
    targets = {r["Sleeve"]: r["Target %"] for _, r in sleeve_df.iterrows()}
    current = {r["Sleeve"]: r["Value"] for _, r in sleeve_df.iterrows()}
    total_tgt = sum(targets.values()) or 100.0

    def tgt_w(s):
        return targets.get(s, 0.0) / total_tgt

    if mode == "simple" or growth_total <= 0:
        return [(s, amount * tgt_w(s)) for s in db.SLEEVES]

    # Rebalance-aware: compute each sleeve's gap to its desired value at the
    # post-deployment portfolio size, then fund the gaps first.
    future = growth_total + amount
    gaps = {s: max(tgt_w(s) * future - current.get(s, 0.0), 0.0) for s in db.SLEEVES}
    gap_sum = sum(gaps.values())

    if gap_sum <= 0:
        # Already at/above target everywhere — fall back to target split.
        return [(s, amount * tgt_w(s)) for s in db.SLEEVES]
    if gap_sum <= amount:
        # Enough to fill every gap; spread the leftover by target weight.
        remainder = amount - gap_sum
        return [(s, gaps[s] + remainder * tgt_w(s)) for s in db.SLEEVES]
    # Not enough to fully rebalance — distribute proportionally to the gaps.
    return [(s, amount * gaps[s] / gap_sum) for s in db.SLEEVES]


# Emergency-first waterfall constants (Sentinel doctrine, app_state["sentinel_manifest"]
# carries the live values when present; these are the fallback if that key is absent).
EMERGENCY_FLOOR_DEFAULT = 360_000.0   # 6x expenses floor — first thing every deployment fills
EMERGENCY_GOAL_DEFAULT = 1_200_000.0  # Vault goal, keeps growing after the floor
SMALLCAP_SATELLITE_AMT = 7_500.0      # one optional smallcap pick when the DS slice is big enough
SMALLCAP_MIN_DS_SLICE = 15_000.0      # Direct Stocks slice must be at least this to spare a satellite
EMERGENCY_VEHICLES_DEFAULT = (
    "Park in ONE liquid fund, direct-growth — e.g. ICICI Prudential Liquid, HDFC "
    "Liquid, SBI Liquid — or a bank sweep-in FD. Reachable in 1 business day."
)


def emergency_first_plan(amount, emergency_balance, sleeve_df, growth_total,
                         emergency_floor=EMERGENCY_FLOOR_DEFAULT, mode="rebalance"):
    """Split a fresh deployment: emergency buffer first, then sleeve gaps.

    Doctrine (2026-07-20): 50% of any deployment routes to the LIQUID emergency
    buffer until `emergency_floor` (₹3.6L) exists; the remainder follows the
    existing sleeve-gap math (deploy_plan). Once the floor is met, 100% of the
    amount goes to sleeve gaps — the emergency line simply doesn't appear.

    Returns (emergency_amount, sleeve_plan) where sleeve_plan is the same
    list[(sleeve, rupee)] shape deploy_plan returns, sized off the post-
    emergency remainder.
    """
    shortfall = max(0.0, emergency_floor - emergency_balance)
    emergency_amount = min(round(amount * 0.5), shortfall) if shortfall > 0 else 0.0
    remainder = amount - emergency_amount
    sleeve_plan = deploy_plan(remainder, sleeve_df, growth_total, mode)
    return emergency_amount, sleeve_plan


def latest_cashflow():
    """Most recent cash-flow row, or sensible defaults from settings."""
    cf = db.get_cashflow()
    if cf:
        return cf[-1]
    return {
        "month": datetime.now().strftime("%Y-%m"),
        "investable": s_float("monthly_contribution", 100000),
        "expenses": s_float("expense_now_monthly", 60000),
        "pf": s_float("pf_monthly", 37500),
        "nps": s_float("nps_monthly", 10000),
    }


# ---------------------------------------------------------------------------
# Auth — simple shared-secret gate (works on Community Cloud via st.secrets)
# ---------------------------------------------------------------------------
def check_auth():
    """Shared-secret login so both users reach the same instance.

    The password is read from st.secrets["app_password"] when deployed; locally
    it falls back to "fire2026" so the app runs out-of-the-box. Users also pick
    which of the two of them is viewing (cosmetic, stored in session only).
    """
    if st.session_state.get("authed"):
        return True

    try:
        expected = st.secrets["app_password"]
    except Exception:
        expected = "fire2026"  # local-dev default; override via secrets on deploy

    st.markdown("## 💰 MAVI Vault")
    st.caption("Shared FIRE tracker — enter the household passcode to continue.")
    with st.form("login"):
        who = st.selectbox("Who's viewing?", ["Partner A (30)", "Partner B (28)"])
        pwd = st.text_input("Passcode", type="password")
        ok = st.form_submit_button("Unlock", use_container_width=True)
    if ok:
        if pwd == expected:
            st.session_state["authed"] = True
            st.session_state["user"] = who
            st.rerun()
        else:
            st.error("Wrong passcode.")
    st.stop()


check_auth()


# ===========================================================================
# HEADER + KPI ROW (shown above all tabs)
# ===========================================================================
holdings_df, any_stale = compute_holdings_table()
# sleeve_pool = TARGET SLEEVES ONLY (the deviation/suggestion/deploy denominator —
# Fix 1, 2026-07-20). Family (Father) is added back on top for net-worth/growth-
# layer purposes below (net_worth_growth), never for sleeve %/gap math.
sleeve_df, sleeve_pool = sleeve_breakdown(holdings_df)
_family_father_value = (
    holdings_df.loc[holdings_df["Sleeve"].isin(GROWTH_EXTRA_SLEEVES), "Value"].sum()
    if not holdings_df.empty else 0.0)
net_worth_growth = sleeve_pool + _family_father_value

cf = latest_cashflow()
investable = cf.get("investable", 0.0)
expenses = cf.get("expenses", 0.0)
income = investable + expenses  # income reconstructed from investable + spend
savings_rate = (investable / income * 100.0) if income else 0.0

# EPF balances live as holdings rows (sleeve "Debt / Safety"); auto-feed the FI
# projection from them when the explicit pf_current setting is 0, so the PF
# compounding engine works without double-entering the number anywhere.
_epf_from_holdings = (
    holdings_df.loc[holdings_df["Asset"].str.contains("EPF|Provident", case=False,
                                                      na=False), "Value"].sum()
    if not holdings_df.empty else 0.0)

# Build FI inputs from persisted settings; current corpus = priced growth sleeve
# unless the user has set an explicit current_corpus override (>0).
fi_inp = FIInputs(
    current_age=s_int("age_self", 30),
    retirement_age=s_int("retirement_age", 50),
    current_corpus=(s_float("current_corpus", 0) or net_worth_growth),
    monthly_contribution=s_float("monthly_contribution", 100000),
    contribution_stepup_pct=s_float("contribution_stepup_pct", 8.0),
    pf_current=(s_float("pf_current", 0) or _epf_from_holdings),
    pf_monthly=s_float("pf_monthly", 37500),
    pf_stepup_pct=s_float("pf_stepup_pct", 5.0),
    pf_return_pct=s_float("pf_return_pct", 7.5),
    nps_current=s_float("nps_current", 0),
    nps_monthly=s_float("nps_monthly", 10000),
    nps_stepup_pct=s_float("nps_stepup_pct", 10.0),
    nps_return_pct=s_float("nps_return_pct", 9.0),
    expense_now_monthly=s_float("expense_now_monthly", 60000),
    expense_target_monthly=s_float("expense_target_monthly", 90000),
    expense_target_year=s_int("expense_target_year", 8),
    inflation_pct=s_float("inflation_pct", 6.0),
    swr_pct=s_float("swr_pct", 3.5),
)
base_result = project(fi_inp)  # 10% baseline-ish (uses stored return default)
_tgt_sleeves = dict(db.get_target_allocation())
# One-household layer split (Codex/user doctrine 2026-07-20): every holding
# lands in exactly ONE of FOUR buckets — growth (net_worth_growth = sleeve_pool
# [target sleeves] + Family (Father), computed above), transit (dated, awaiting
# redeployment), the locked safety floor (Debt/Safety + Family (Parents)
# NSCs), or emergency (liquid, Feature A 2026-07-20 — its own bucket, NOT
# growth/gap-pool, NOT locked safety, NOT transit; see EMERGENCY_SLEEVE note).
# Explicit membership sets avoid the old "everything not a target sleeve"
# catch-all, which orphaned/double-counted non-target growth sleeves like
# Family (Father).
_growth_sleeves = set(_tgt_sleeves) | GROWTH_EXTRA_SLEEVES
transit_value = (holdings_df.loc[holdings_df["Sleeve"] == "Transit (redeploying)", "Value"].sum()
                 if not holdings_df.empty else 0.0)
safety_value = (holdings_df.loc[holdings_df["Sleeve"].isin(SAFETY_LAYER_SLEEVES), "Value"].sum()
                if not holdings_df.empty else 0.0)
emergency_holdings_value = (
    holdings_df.loc[holdings_df["Sleeve"] == EMERGENCY_SLEEVE, "Value"].sum()
    if not holdings_df.empty else 0.0)
_unmapped_sleeves = (sorted(set(holdings_df["Sleeve"].unique())
                            - _growth_sleeves - SAFETY_LAYER_SLEEVES
                            - {"Transit (redeploying)", EMERGENCY_SLEEVE})
                     if not holdings_df.empty else [])
# Household total: EPF already sits inside safety_value (holdings rows), so add
# only the EXPLICIT pf/nps settings here — never the holdings-derived feed —
# or EPF would be counted twice.
# Codex 082 P1 guard: if BOTH sources exist (EPF holdings rows AND a pf_current
# setting), net worth uses the holdings rows only and a blocking warning renders.
_pf_setting = s_float("pf_current", 0)
_pf_conflict = _epf_from_holdings > 0 and _pf_setting > 0
total_net_worth = (net_worth_growth + safety_value + transit_value + emergency_holdings_value
                   + (0.0 if _pf_conflict else _pf_setting)
                   + s_float("nps_current", 0))

# Concentration guard (Fix 2): computed once here, rendered on "Start Here"
# and "Goals & Rebalance" only (not a global header banner like _pf_conflict).
_concentration_hit = concentration_check(holdings_df, net_worth_growth)

# Feature A: emergency-fund goal progress, computed once here so Start Here
# and Deploy This Month agree on the SAME number (code-review fix, 2026-07-20:
# these two tabs previously derived the goal amount from two independent
# sources — the Deploy tab's Sentinel-manifest override and this header's
# goals-table lookup — which could silently disagree if either was edited
# without the other). Single precedence chain, used everywhere from here on:
#   1. Sentinel manifest override (app_state["sentinel_manifest"]["emergency_goal"])
#   2. goals table row with kind='emergency' (seeded ₹12L in db.DEFAULT_GOALS —
#      this is the charter target a user edits via Goals & Rebalance)
#   3. hardcoded EMERGENCY_GOAL_DEFAULT, if both above are absent.
# Progress uses LIVE HOLDINGS ONLY (emergency_holdings_value) at the header
# level — the Deploy tab's per-session manual override is a "this month's
# plan" input, not a change to the tracked balance, so it uses its own em_bal
# for ITS progress bar (see tab_deploy) while sharing this same goal amount.
_em_manifest = db.get_app_state("sentinel_manifest")
_em_goal_row = next((g for g in db.get_goals() if g["kind"] == "emergency"), None)
_em_goal_amount = float(
    _em_manifest.get("emergency_goal")
    or (_em_goal_row["target_amount"] if _em_goal_row else None)
    or EMERGENCY_GOAL_DEFAULT
)
_em_goal_progress_pct = (min(emergency_holdings_value / _em_goal_amount * 100.0, 100.0)
                         if _em_goal_amount else 0.0)

st.markdown(
    f"""<div class="vault-hero"><span class="wm"><b>MAVI</b> VAULT</span>
    <span class="sub">Household Wealth Engine</span>
    <span class="who">👤 {st.session_state.get('user','')}</span></div>""",
    unsafe_allow_html=True,
)
if any_stale:
    st.markdown(
        "<span class='stale-flag'>⚠ Some prices are stale (last-fetch fallback in use). "
        "Pull-to-refresh / reload to retry.</span>",
        unsafe_allow_html=True,
    )
if _pf_conflict:
    st.error(
        f"🚫 **PF is entered twice** — as EPF holdings rows ({fmt_inr(_epf_from_holdings)}) "
        f"AND as the FI setting 'PF current' ({fmt_inr(_pf_setting)}). Net worth is using the "
        "holdings rows ONLY and ignoring the setting. Fix one: set 'PF current' to 0 in the "
        "FI tab, or delete the EPF holdings rows. PF lives in ONE place, never both."
    )
if _unmapped_sleeves:
    st.warning(
        f"⚠ Sleeve(s) not mapped to a layer, excluded from Net Worth: "
        f"{', '.join(_unmapped_sleeves)}. Add them to GROWTH_EXTRA_SLEEVES or "
        "SAFETY_LAYER_SLEEVES in app.py, or fix the sleeve name."
    )

k1, k2, k3, k4 = st.columns(4)
k1.metric("Net Worth", fmt_inr(total_net_worth),
          help="Growth sleeve + transit + safety floor (EPF, post office, all NSCs)")
k2.metric("Savings Rate", f"{savings_rate:.0f}%",
          help="Investable ÷ (investable + expenses), latest month")
k3.metric("FI Progress", f"{base_result.fi_progress_pct:.0f}%",
          help="Current corpus vs this year's FI number")
fi_date = (f"age {base_result.fi_age}" if base_result.fi_age
           else f">{fi_inp.retirement_age}")
k4.metric("Est. FI Date", fi_date,
          help="First age total corpus ≥ FI number (10% baseline)")

st.divider()

tab_start, tab1, tab_targets, tab_deploy, tab_scanner, tab2, tab3, tab4, tab5 = st.tabs([
    "🚀 Start Here",
    "📊 Net Worth & Allocation",
    "🎯 Targets",
    "💸 Deploy This Month",
    "🔎 Scanner",
    "💵 Cash Flow & Savings",
    "🔥 FI Projection",
    "📁 Holdings",
    "🎯 Goals & Rebalance",
])


# ===========================================================================
# TAB — START HERE: a 4-step guided walkthrough for the first month.
# Returning users (who already have holdings) land on the final summary step.
# ===========================================================================
with tab_start:
    if _concentration_hit:
        st.warning(concentration_warning_text(_concentration_hit))

    # Feature D: idle-cash detector (charter Phase B4) — Transit money that's
    # sat >30 days with no destination inside the next 45. One warning per
    # flagged holding, same pattern as the concentration-guard banner above.
    # age_unknown holdings (no buy_date recorded) get a distinct message —
    # review fix: these were previously silently skipped, which is backwards
    # since a missing start date is itself the data gap worth surfacing.
    for _idle in detect_idle_transit(holdings_df):
        if _idle.get("age_unknown"):
            st.warning(f"💤 {_idle['name']} {fmt_inr(_idle['value'])} is a transit "
                       f"holding with no buy_date — age unknown, add the date or "
                       f"route it via Deploy This Month.")
        else:
            st.warning(f"💤 Idle money: {_idle['name']} {fmt_inr(_idle['value'])} has sat "
                       f"in transit >30 days with no destination — route it via "
                       f"Deploy This Month.")

    # First-time users start at step 1; returning users see the summary (step 4).
    st.session_state.setdefault("wiz", 4 if not holdings_df.empty else 1)
    step = st.session_state["wiz"]

    st.markdown(f"<span class='step-pill'>Step {step} of 4</span>",
                unsafe_allow_html=True)
    st.progress(step / 4)

    # ---- Step 1: how much this month? -------------------------------------
    if step == 1:
        st.markdown("### 👋 Welcome — let's set up this month in 4 quick steps")
        st.write("Takes ~2 minutes. **First question: how much are you investing "
                 "this month?**")
        wiz_amt = st.number_input(
            "Amount to invest this month (₹)", min_value=0.0,
            value=float(st.session_state.get("wiz_amt", investable or 100000)),
            step=10000.0, key="wiz_amt_field",
        )
        st.caption("Just your growth money — PF & NPS are separate (auto-deducted "
                   "from salary, tracked elsewhere).")
        if st.button("Show me where it goes →", type="primary"):
            st.session_state["wiz_amt"] = wiz_amt
            st.session_state["wiz"] = 2
            st.rerun()

    # ---- Step 2: the split -------------------------------------------------
    elif step == 2:
        amt = st.session_state.get("wiz_amt", 100000)
        st.markdown(f"### 🧮 Here's where your {fmt_full_inr(amt)} goes this month")
        plan = deploy_plan(amt, sleeve_df, sleeve_pool, "rebalance")
        tgt_map = dict(db.get_target_allocation())
        st.dataframe(pd.DataFrame([{
            "Sleeve": s,
            "Target %": f"{tgt_map.get(s, 0):.0f}%",
            "Invest now": fmt_full_inr(a),
            "What to buy": INSTRUMENT_HINTS.get(s, ""),
        } for s, a in plan]), use_container_width=True, hide_index=True)
        st.success("👉 Go place these buys in your broker / mutual-fund app. "
                   "Come back here when you're done.")
        b1, b2 = st.columns(2)
        if b1.button("← Back"):
            st.session_state["wiz"] = 1
            st.rerun()
        if b2.button("I've bought these → next", type="primary"):
            st.session_state["wiz"] = 3
            st.rerun()

    # ---- Step 3: record what you bought -----------------------------------
    elif step == 3:
        st.markdown("### 📥 Add what you bought")
        st.write("Record each purchase so the app auto-tracks its live price. "
                 "Add a few now — you can always add more in the **Holdings** tab.")
        with st.form("wiz_add_holding"):
            w1, w2 = st.columns(2)
            wname = w1.text_input("Asset name", placeholder="e.g. Nippon Nifty 50 Index")
            wsleeve = w2.selectbox("Sleeve", db.SLEEVES)
            w3, w4 = st.columns(2)
            # "crypto" intentionally excluded (Vault doctrine, 2026-07-21): no
            # crypto tracking here — see the Sentinel probation charter.
            watype = w3.selectbox("Type", ["mf", "stock", "global", "manual"],
                                  help="mf=AMFI code · stock=.NS ticker · global=US ticker "
                                       "· manual=type price")
            wticker = w4.text_input("Ticker / scheme code / coin",
                                    placeholder="122639 / RELIANCE.NS / VOO / BTC")
            w5, w6, w7 = st.columns(3)
            wqty = w5.number_input("Units", min_value=0.0, step=1.0, format="%.4f")
            wbuy = w6.number_input("Buy price (₹)", min_value=0.0, step=1.0)
            wmanual = w7.number_input("Manual price (₹, 0=auto)", min_value=0.0, step=1.0)
            if st.form_submit_button("➕ Add holding"):
                if wname and wqty > 0:
                    db.add_holding(wname, wsleeve, watype, wticker.strip(), wqty,
                                   wbuy, datetime.now().strftime("%Y-%m-%d"),
                                   wmanual if wmanual > 0 else None)
                    st.rerun()
                else:
                    st.error("Need an asset name and units > 0.")
        existing = db.get_holdings()
        if existing:
            st.caption(f"✅ Added so far ({len(existing)}): " +
                       ", ".join(h["asset_name"] for h in existing))
        b1, b2 = st.columns(2)
        if b1.button("← Back"):
            st.session_state["wiz"] = 2
            st.rerun()
        if b2.button("Done →", type="primary"):
            st.session_state["wiz"] = 4
            st.rerun()

    # ---- Step 4: you're set up --------------------------------------------
    else:
        st.markdown("### 🎉 You're all set up!")

        # Tier-1 growth visibility: the headline "is my money growing" answer,
        # forward-measured from the transactions ledger (epoch-split doctrine
        # — pre-baseline gains aren't graded). With only baseline rows on file
        # this is degenerate by design (see returns.py MIN_DAYS_FOR_XIRR) and
        # renders "—" rather than a fake day-one number.
        _all_txns = db.get_transactions()
        _bench_price = prices.fetch_stock_price("NIFTYBEES.NS", is_global=False)
        _hh_xirr = returns.household_xirr(_all_txns, total_net_worth)
        _hh_bench_xirr = returns.household_benchmark_xirr(_all_txns, _bench_price)
        gc1, gc2 = st.columns(2)
        gc1.metric("📈 Your money's growth rate (since 21 Jul 2026)",
                   returns.fmt_xirr(_hh_xirr))
        gc2.metric("vs just-buying-NIFTYBEES", returns.fmt_xirr(_hh_bench_xirr))
        st.caption("Forward-measured from the 21 Jul 2026 baseline (epoch-split "
                   "doctrine: pre-baseline gains aren't graded). Early readings "
                   "swing wildly — trust it after ~3 months.")

        _hist = db.get_networth_history()
        _gfig = growth_chart_figure(_hist)
        if _gfig is not None:
            st.plotly_chart(_gfig, use_container_width=True)
        if len(_hist) < 7:
            st.caption("building your history — one point per day from 21 Jul 2026")

        st.write("Your starting picture:")
        m1, m2, m3 = st.columns(3)
        m1.metric("Net Worth", fmt_inr(total_net_worth))
        m2.metric("FI Progress", f"{base_result.fi_progress_pct:.0f}%")
        m3.metric("Est. FI Date",
                  f"age {base_result.fi_age}" if base_result.fi_age
                  else f">{fi_inp.retirement_age}")
        st.caption(f"🚨 Emergency fund: {fmt_inr(emergency_holdings_value)} / "
                   f"{fmt_inr(_em_goal_amount)} ({_em_goal_progress_pct:.0f}%)")
        st.progress(min(_em_goal_progress_pct / 100.0, 1.0))
        st.markdown(
            """
**Your monthly routine from now on — just 2 steps:**
1. Open **💸 Deploy This Month**, type your amount, buy the split it shows.
2. Add those buys in **📁 Holdings** so prices keep updating.

Then explore the other tabs whenever you like:
- **📊 Net Worth & Allocation** — your live mix vs target
- **💵 Cash Flow & Savings** — track your savings rate
- **🔥 FI Projection** — when you two can retire (8 / 10 / 12 % scenarios)
- **🎯 Goals & Rebalance** — milestone progress + buy/trim nudges

> Reminder: **PF & NPS are separate** from the growth-sleeve money above.
            """
        )
        if st.button("↺ Run the walkthrough again"):
            st.session_state["wiz"] = 1
            st.rerun()


# ===========================================================================
# TAB — DEPLOY THIS MONTH (the "I got ₹X, where does it go?" calculator)
# ===========================================================================
with tab_deploy:
    st.markdown("##### 💸 Deploy this month's money")
    st.caption("The complete waterfall: **① emergency buffer → ② sleeve gaps → "
               "③ scanner stock + optional smallcap satellite.** One plan, in order.")

    # Sentinel manifest (app_state) can override the doctrine constants; falls
    # back to the hardcoded defaults if the key/table isn't there yet.
    _manifest = db.get_app_state("sentinel_manifest")
    _em_floor = float(_manifest.get("emergency_floor") or EMERGENCY_FLOOR_DEFAULT)
    # _em_goal_amount (same precedence chain: manifest -> goals table -> default)
    # is computed once at header level so this tab agrees with Start Here —
    # see the header comment above _em_goal_amount for the full rationale.
    _em_goal = _em_goal_amount
    _em_vehicles = _manifest.get("emergency_vehicles") or EMERGENCY_VEHICLES_DEFAULT

    dc1, dc2, dc3 = st.columns([1, 1, 1.3])
    with dc1:
        amt = st.number_input("Amount to invest this month (₹)", min_value=0.0,
                              value=float(investable or 100000), step=10000.0)
    with dc2:
        # Feature A: once a "🚨 Emergency (liquid)" holding exists, its summed
        # Value auto-fills this input (live from holdings). The field stays
        # editable — whatever the user types is still what feeds the
        # waterfall below, so typing over the auto-fill is a genuine
        # override, not a fight with the widget. Until such a holding exists,
        # this is byte-for-byte the old manual-input behaviour.
        #
        # Review fix: a keyed number_input only honors `value=` on that key's
        # FIRST render ever — every rerun after that is driven purely by
        # session_state, so a naive `value=emergency_holdings_value` goes
        # stale the moment holdings change on a later rerun while the
        # "live from holdings" caption below keeps claiming it's live. Fix:
        # explicitly re-sync session_state to the live figure whenever the
        # live figure itself changes (tracked via a sentinel key), BEFORE
        # instantiating the widget — then the widget is created with key=
        # only, no competing value=. A manual override within the same live
        # figure still sticks; it only re-syncs when holdings actually move.
        _em_has_holdings = emergency_holdings_value > 0
        if _em_has_holdings:
            if st.session_state.get("_em_live_seen") != emergency_holdings_value:
                st.session_state["deploy_em_balance"] = emergency_holdings_value
                st.session_state["_em_live_seen"] = emergency_holdings_value
        else:
            st.session_state.setdefault("deploy_em_balance", 0.0)
        em_bal = st.number_input(
            "Liquid emergency money you already hold (₹)", min_value=0.0,
            step=10000.0,
            help="Bank / liquid-fund / sweep-FD balance — the TRUE liquid buffer. "
                 "EPF and NSCs don't count; they're locked.",
            key="deploy_em_balance",
        )
        if _em_has_holdings:
            st.caption(f"🟢 live from holdings: {fmt_inr(emergency_holdings_value)} "
                       "— edit to override this month only.")
    with dc3:
        mode_label = st.radio(
            "How to split the sleeve portion",
            ["Smart — rebalance-aware (recommended)", "Simple — pure target %"],
            help="Smart steers new money into whichever sleeves are below target, "
                 "so you rebalance without selling. With no holdings yet, both "
                 "give the same answer.",
        )
    mode = "rebalance" if mode_label.startswith("Smart") else "simple"

    # Feature A: progress vs the Vault emergency GOAL (goals table, kind=
    # 'emergency' — ₹12L, seeded in db.DEFAULT_GOALS; _em_goal_amount computed
    # once at header level). Distinct from _em_floor (the ₹3.6L waterfall
    # trigger above) — the floor is when the waterfall stops force-feeding
    # this line; the goal is the longer-run target it keeps growing toward
    # after that. Uses em_bal (this tab's live-or-override balance) rather
    # than the header's holdings-only number, since this bar is about what's
    # being planned right here.
    _em_bal_goal_pct = (min(em_bal / _em_goal_amount * 100.0, 100.0)
                        if _em_goal_amount else 0.0)
    st.caption(f"🚨 Emergency fund progress: {fmt_inr(em_bal)} / {fmt_inr(_em_goal_amount)} "
               f"({_em_bal_goal_pct:.0f}%)")
    st.progress(min(_em_bal_goal_pct / 100.0, 1.0))

    em_amount, sleeve_plan = emergency_first_plan(
        amt, em_bal, sleeve_df, sleeve_pool, _em_floor, mode)
    tgt_map = dict(db.get_target_allocation())

    # Each entry: (label, why, rupee amount, color-tag). color-tag drives the
    # bar chart below without re-parsing formatted strings.
    _plan = []
    if em_amount > 0:
        _plan.append((
            "🚨 Emergency fund — liquid MF / sweep-FD",
            (f"you hold {fmt_inr(em_bal)} of the {fmt_inr(_em_floor)} floor "
             f"(Vault goal {fmt_inr(_em_goal)}) — safety before markets. {_em_vehicles}"),
            em_amount, GOLD))
    for s, a in sleeve_plan:
        if a < 500:
            continue
        _plan.append((f"{s} — {INSTRUMENT_HINTS.get(s, '')}", f"target {tgt_map.get(s, 0):.0f}%",
                      a, SLEEVE_COLORS.get(s, MUTED)))

    # Within the Direct Stocks slice: scanner #1 pick + optional smallcap satellite.
    _ds_amount = dict(sleeve_plan).get("Direct Stocks", 0.0)
    _scanner = db.get_app_state("scanner")
    _core = _scanner.get("core") or []
    _small = _scanner.get("smallcap") or []
    if _ds_amount >= 500:
        _ds_core_amount = _ds_amount
        if _ds_amount >= SMALLCAP_MIN_DS_SLICE and _small:
            _sat = _small[0]
            _ds_core_amount = _ds_amount - SMALLCAP_SATELLITE_AMT
            _plan.append((
                f"🧪 {_sat.get('sym', '?')} — smallcap satellite (score {_sat.get('score', '?')})",
                "⚠ experiment: ₹25k total cap · kill line −30%/name · judgment pass FIRST",
                SMALLCAP_SATELLITE_AMT, RED))
        if _core:
            _pick = _core[0]
            _plan.append((
                f"{_pick.get('sym', '?')} — scanner #1 (score {_pick.get('score', '?')})",
                "Direct Stocks sleeve · max ONE new name/month · ⚠ judgment pass FIRST",
                _ds_core_amount, SLEEVE_COLORS.get("Direct Stocks", MUTED)))

    plan_rows = [{"Destination": lbl, "Why": why, "Amount": fmt_full_inr(a)}
                 for lbl, why, a, _c in _plan]
    st.dataframe(pd.DataFrame(plan_rows), use_container_width=True, hide_index=True)

    total_alloc = sum(a for _, _, a, _c in _plan)
    st.caption(f"Total deployed: **{fmt_full_inr(total_alloc)}**  ·  "
               f"PF ₹{s_float('pf_monthly', 37500):,.0f} + "
               f"NPS ₹{s_float('nps_monthly', 10000):,.0f} go in separately.")
    if _core or _small:
        st.warning("⚠ Stock lines are candidates, not recommendations — run the judgment "
                   "pass (news / governance / thesis via Claude) before buying either one.")

    # Visual: horizontal bar of the rupee split.
    if amt > 0 and _plan:
        pf_fig = go.Figure(go.Bar(
            x=[a for _, _, a, _c in _plan],
            y=[lbl[:36] for lbl, _w, _a, _c in _plan],
            orientation="h",
            marker_color=[c for _l, _w, _a, c in _plan],
            text=[fmt_full_inr(a) for _, _, a, _c in _plan],
            textposition="auto",
        ))
        pf_fig.update_layout(
            template="plotly_dark", height=max(320, 40 * len(_plan)),
            margin=dict(t=10, b=10, l=10, r=10),
            xaxis_title="₹ this month", yaxis=dict(autorange="reversed"),
        )
        st.plotly_chart(pf_fig, use_container_width=True)

    if em_amount > 0:
        st.info(f"ℹ️ **Emergency-first**: half of this deployment ({fmt_inr(em_amount)}) "
                f"tops up your liquid buffer before anything else — "
                f"{fmt_inr(max(0.0, _em_floor - em_bal - em_amount))} still short of the "
                f"{fmt_inr(_em_floor)} floor.")
    elif mode == "rebalance" and sleeve_pool > 0:
        st.info("ℹ️ **Smart mode** is funneling more into your under-weight "
                "sleeves first, so your portfolio drifts back toward target "
                "without you selling anything.")
    elif sleeve_pool <= 0:
        st.info("ℹ️ No holdings yet, so this is a clean target-weight split. "
                "Once you own things, **Smart mode** will tilt new money toward "
                "whatever is lagging.")
    st.caption("🔎 Full scanner shortlist (CORE top-15 + smallcap satellite) is in "
               "its own **Scanner** tab.")

    st.divider()
    st.markdown("##### 💹 Record an investment")
    st.caption("This ledger powers your XIRR — record EVERY buy/SIP here. "
               "Holding values still update via the Holdings tab; this records "
               "the cash flow.")
    with st.form("record_investment"):
        _existing_holdings = db.get_holdings()
        _holding_options = [0] + [h["id"] for h in _existing_holdings]
        _holding_labels = {0: "— new asset (type name below) —"}
        _holding_labels.update({h["id"]: f"{h['asset_name']} ({h['sleeve']})"
                                for h in _existing_holdings})
        ri1, ri2 = st.columns(2)
        ri_holding_id = ri1.selectbox(
            "Holding", _holding_options,
            format_func=lambda i: _holding_labels.get(i, str(i)))
        ri_new_name = ri2.text_input(
            "New asset name (only if 'new asset' selected above)")
        ri3, ri4, ri5 = st.columns(3)
        ri_kind = ri3.selectbox("Kind", ["BUY", "SIP", "SELL", "DIVIDEND", "MATURITY"])
        ri_amount = ri4.number_input("Amount (₹)", min_value=0.0, step=1000.0)
        ri_date = ri5.date_input("Date", value=datetime.now().date())
        ri6, ri7 = st.columns(2)
        ri_units = ri6.number_input("Units (optional)", min_value=0.0, step=1.0,
                                    format="%.4f")
        ri_price = ri7.number_input("Price (optional, ₹)", min_value=0.0, step=1.0)
        if st.form_submit_button("➕ Record investment", type="primary"):
            _sel_holding = next((h for h in _existing_holdings if h["id"] == ri_holding_id), None)
            _asset_name = _sel_holding["asset_name"] if _sel_holding else ri_new_name.strip()
            _sleeve = _sel_holding["sleeve"] if _sel_holding else None
            if not _asset_name:
                st.error("Need a holding selected, or a new asset name typed in.")
            elif ri_amount <= 0:
                st.error("Amount must be greater than 0.")
            else:
                db.add_transaction(
                    date=str(ri_date), asset_name=_asset_name, kind=ri_kind,
                    amount=ri_amount, holding_id=(ri_holding_id or None),
                    sleeve=_sleeve, units=(ri_units or None), price=(ri_price or None),
                    provenance="user-entered",
                )
                st.success(f"Recorded {ri_kind} {fmt_inr(ri_amount)} for {_asset_name}.")
                st.rerun()


# ===========================================================================
# TAB — SCANNER (Fix 7, 2026-07-20: promoted out of Deploy This Month into its
# own tab. The Deploy tab's waterfall keeps its own scanner-pick lines — this
# tab is the full-detail shortlist: timestamp, spec, CORE top-15, smallcap
# with its experiment banner, and the two-stage disclaimer.)
# ===========================================================================
with tab_scanner:
    st.markdown("##### 🔎 Stock scanner — Codex-081 quantitative screen")
    st.caption("Nifty 500 quality/growth screen + Smallcap-250 satellite experiment · "
               "**advisory only, two-stage**: this screen shortlists → ask Claude for "
               "the judgment pass (news · governance · thesis) before any buy.")
    _scanner = db.get_app_state("scanner")
    _core = _scanner.get("core") or []
    _small = _scanner.get("smallcap") or []
    if not _core and not _small:
        st.info("No scan on file yet — the Sentinel dashboard pushes this at each "
                "regeneration (`dt_system/dashboard_update.py`).")
    else:
        _gen = _scanner.get("generated") or _scanner.get("_updated") or "?"
        _spec = _scanner.get("spec")
        st.caption(f"Scanned **{_gen}**" + (f" · spec: {_spec}" if _spec else ""))
        _stats = _scanner.get("stats") or {}
        if _core:
            st.markdown(f"**CORE** · Nifty 500 · "
                        f"{_stats.get('core_passed', '?')}/{_stats.get('core_universe', 500)} "
                        f"passed all quality gates — top 15 shown")
            _core_rows = [{
                "Sym": r.get("sym"), "Name": r.get("name"), "Sector": r.get("sector"),
                "Price": fmt_full_inr(r.get("price", 0)), "ROE %": r.get("roe_pct"),
                "Rev gr %": r.get("rev_g_pct"), "Earn gr %": r.get("earn_g_pct"),
                "D/E": r.get("de"), "RS 6m %": r.get("rs6m_pct"), "Score": r.get("score"),
            } for r in _core[:15]]
            st.dataframe(pd.DataFrame(_core_rows), use_container_width=True, hide_index=True)
        st.markdown(f"🧪 **SMALLCAP SATELLITE** · Smallcap 250 · "
                    f"{_stats.get('small_passed', '?')}/{_stats.get('small_universe', 250)} "
                    f"passed · EXPERIMENT: low prior, ₹25k total cap, ₹5–10k/name, "
                    f"kill line −30%/name, graded at N=10")
        if _small:
            _small_rows = [{
                "Sym": r.get("sym"), "Name": r.get("name"), "Sector": r.get("sector"),
                "Price": fmt_full_inr(r.get("price", 0)), "ROE %": r.get("roe_pct"),
                "Rev gr %": r.get("rev_g_pct"), "RS 6m %": r.get("rs6m_pct"),
                "Score": r.get("score"),
            } for r in _small]
            st.dataframe(pd.DataFrame(_small_rows), use_container_width=True, hide_index=True)
        else:
            st.caption("Nothing passed the smallcap gates this scan — correct behaviour, "
                       "not a bug. The gates stay put.")
        st.warning("⚠ **Two-stage discipline**: these are quantitative candidates, NOT "
                   "recommendations. Every name needs the judgment pass — news, "
                   "governance, thesis — via Claude before any order goes in.")


# ===========================================================================
# TAB 1 — NET WORTH & ALLOCATION
# ===========================================================================
with tab1:
    c1, c2, c3 = st.columns(3)
    c1.metric("Total Net Worth", fmt_inr(total_net_worth))
    c2.metric("Growth Sleeve", fmt_inr(net_worth_growth),
              help=f"Target sleeves ({fmt_inr(sleeve_pool)}) + Family (Father)'s "
                   f"HDFC Mid-Cap fund ({fmt_inr(_family_father_value)}) — a real market "
                   "asset with no allocation target of its own. The allocation charts "
                   "below (donut/bar/deviation) use the target-sleeve pool only.")
    c3.metric("🛡 Safety Floor", fmt_inr(safety_value),
              help="EPF (both of you) + post office + all 7 NSCs (incl. parents') — "
                   "guaranteed but LOCKED: pledge-able, not spendable, NOT an emergency "
                   "fund. EPF also feeds the FI projection's PF engine automatically.")

    # Tier-1 growth visibility: same chart as Start Here, since Net Worth &
    # Allocation is where a returning user actually looks for "is it growing".
    st.markdown("##### 📈 Growth over time")
    _nw_hist = db.get_networth_history()
    _nw_gfig = growth_chart_figure(_nw_hist)
    if _nw_gfig is not None:
        st.plotly_chart(_nw_gfig, use_container_width=True)
    if len(_nw_hist) < 7:
        st.caption("building your history — one point per day from 21 Jul 2026")

    # Money timeline — the app taps your shoulder as dated events approach.
    _evs = [e for e in upcoming_events(holdings_df) if -3 <= e["days"] <= 140]
    if _evs:
        st.markdown("##### 🗓 Money timeline — what's coming up")
        _chips = ""
        for _e in _evs:
            _est = _e.get("prov") == "estimated"
            # estimated dates never get red/urgent styling (Codex 082)
            _u = ("ev-u30" if _e["days"] <= 30 else "ev-uX") if _est else (
                "ev-u7" if _e["days"] <= 7 else ("ev-u30" if _e["days"] <= 30 else "ev-uX"))
            _hint = {"transit": "lands → redeploy via landing plan",
                     "ritual": "10-min ritual · rulebook 081",
                     "safety": "matures (long horizon)"}[_e["kind"]]
            if _est:
                _hint += " · date unconfirmed"
            _when = (("est. " if _est else "")
                     + datetime.fromisoformat(_e["date"]).strftime("%d %b"))
            _val = f"{fmt_inr(_e['value'])} · " if _e["value"] else ""
            _chips += (f'<div class="ev-chip {_u}"><div class="d">{"~" if _est else ""}{_e["days"]}'
                       f'<small>days · {_when}</small></div>'
                       f'<div class="n">{_e["name"][:58]}</div>'
                       f'<div class="h">{_val}{_hint}</div></div>')
        st.markdown(f'<div class="ev-row">{_chips}</div>', unsafe_allow_html=True)
        _soon = [e for e in _evs if 0 <= e["days"] <= 7 and e.get("prov") != "estimated"]
        if _soon:
            st.warning("🔔 " + "  ·  ".join(
                f"**{e['name'][:48]}** — {e['days']} day{'s' if e['days'] != 1 else ''} away"
                for e in _soon))

        # Feature F: the "Monthly Direct-Stocks review" chip above is a visual
        # reminder only (raw HTML, not clickable) — this expander is where you
        # actually DO the 10-minute rulebook pass. Static checklist, no state
        # persistence this round (per spec).
        with st.expander("📋 Monthly Direct-Stocks review (10-min rulebook pass) — open to do it now"):
            st.markdown(
                """
1. **Per holding**: has any stop-loss / exit rule been breached?
2. **Concentration**: any single name >15% of the Direct Stocks sleeve, or any sector >25%?
3. **Basket drawdown**: check current drawdown vs the −12% / −18% / −25% ladder.
4. **Thesis check**: has the original thesis broken on any name?
5. **Scanner cross-check**: is any Scanner-tab candidate strictly better than a current holding?
                """
            )
            st.caption("Full rulebook: mavi_collab/outbox/2026-06-13-081-result.md — "
                       "Direct Stocks currently holds only Mansi's L&T + HDFC Bank "
                       "(~₹50k), so steps 1–4 are quick.")

    # Feature E: maturity horizon beyond the 140-day timeline above. Same
    # holdings-derived maturity data (upcoming_events), just the far end of
    # the list instead of the near end. Muted/caption styling deliberately —
    # this is "known and scheduled", not something needing attention now.
    # Value shown per line: parsed from the asset name where present (NSC
    # names sometimes embed their fixed maturity/face value, which differs
    # from today's marked-to-market Value), else the current priced Value.
    # Emergency (liquid) is explicitly excluded here (code review finding,
    # 2026-07-20): that sleeve is genuinely liquid/on-demand by definition —
    # a sweep-FD renewal date living inside it must never render as a distant
    # "locked maturity" line alongside NSCs.
    _beyond = [e for e in upcoming_events(holdings_df)
              if e["days"] > 140 and e["kind"] != "ritual" and e.get("sleeve") != EMERGENCY_SLEEVE]
    if _beyond:
        st.caption("**Beyond the horizon:**")
        for _e in _beyond[:3]:
            _when = datetime.fromisoformat(_e["date"]).strftime("%b %Y")
            _parsed = parse_maturity_value_from_name(_e["name"], current_value=_e["value"])
            _show_val = _parsed if _parsed is not None else _e["value"]
            st.caption(f"  · {_e['name'][:58]} — {fmt_inr(_show_val)} — {_when}")
        st.caption("_NSC ladder totals ₹36.2L across Apr 2029 – Jan 2031 — arrives "
                   "exactly as the ₹35L loans finish._")

    st.markdown("##### Allocation — current vs target (growth sleeve)")
    _em_caption_part = (f"🚨 Emergency (liquid): {fmt_inr(emergency_holdings_value)}  ·  "
                        if emergency_holdings_value > 0 else "")
    st.caption(f"🛡 Locked-in base (EPF, post office, all NSCs): {fmt_inr(safety_value)}  ·  "
               f"⏳ Transit, redeploying Jul–Aug 26 (chits, small FDs): {fmt_inr(transit_value)}  ·  "
               f"{_em_caption_part}"
               f"Household total: {fmt_inr(total_net_worth)}")

    # Layer breakup (Fix 6, 2026-07-20; Emergency bucket added Feature A): click
    # into each of the four layers to see exactly which holdings sit inside it —
    # same one-household mapping (_growth_sleeves / SAFETY_LAYER_SLEEVES /
    # "Transit (redeploying)" / EMERGENCY_SLEEVE) used to compute
    # net_worth_growth / safety_value / transit_value / emergency_holdings_value above.
    def _layer_holdings_table(sleeve_mask):
        ldf = holdings_df.loc[sleeve_mask, ["Asset", "Sleeve", "Value"]].sort_values(
            "Value", ascending=False)
        view = ldf.copy()
        view["Value"] = view["Value"].map(fmt_inr)
        view.columns = ["Name", "Sleeve", "Value"]
        st.dataframe(view, use_container_width=True, hide_index=True)

    with st.expander(f"🚀 Growth engine ({fmt_inr(net_worth_growth)})"):
        if holdings_df.empty or not holdings_df["Sleeve"].isin(_growth_sleeves).any():
            st.caption("No growth-layer holdings yet.")
        else:
            _layer_holdings_table(holdings_df["Sleeve"].isin(_growth_sleeves))
    with st.expander(f"🛡 Locked-in base ({fmt_inr(safety_value)})"):
        if holdings_df.empty or not holdings_df["Sleeve"].isin(SAFETY_LAYER_SLEEVES).any():
            st.caption("No locked-in base holdings yet.")
        else:
            _layer_holdings_table(holdings_df["Sleeve"].isin(SAFETY_LAYER_SLEEVES))
    with st.expander(f"⏳ Transit ({fmt_inr(transit_value)})"):
        if holdings_df.empty or not (holdings_df["Sleeve"] == "Transit (redeploying)").any():
            st.caption("No transit holdings yet.")
        else:
            _layer_holdings_table(holdings_df["Sleeve"] == "Transit (redeploying)")
    with st.expander(f"{EMERGENCY_SLEEVE} ({fmt_inr(emergency_holdings_value)})"):
        if holdings_df.empty or not (holdings_df["Sleeve"] == EMERGENCY_SLEEVE).any():
            st.caption("No holdings tagged Emergency (liquid) yet — the Deploy tab's "
                       "emergency line still uses manual input until one exists.")
        else:
            _layer_holdings_table(holdings_df["Sleeve"] == EMERGENCY_SLEEVE)

    left, right = st.columns([1, 1])

    with left:
        # Donut of current allocation by sleeve.
        if not sleeve_df.empty and sleeve_df["Value"].sum() > 0:
            donut = go.Figure(go.Pie(
                labels=sleeve_df["Sleeve"],
                values=sleeve_df["Value"],
                hole=0.62,
                marker=dict(colors=[SLEEVE_COLORS.get(s, MUTED) for s in sleeve_df["Sleeve"]]),
                textinfo="percent",
                hovertemplate="%{label}<br>%{percent}<br>%{customdata}<extra></extra>",
                customdata=[fmt_inr(v) for v in sleeve_df["Value"]],
            ))
            donut.update_layout(
                template="plotly_dark", height=340, showlegend=True,
                margin=dict(t=10, b=10, l=10, r=10),
                legend=dict(font=dict(size=10)),
                # Center label = sum of the visible slices (target-sleeve pool),
                # NOT net_worth_growth — that would double-count Family (Father),
                # who isn't a slice here (Fix 1: donut, deviations, and priorities
                # must all agree with each other).
                annotations=[dict(text=fmt_inr(sleeve_pool), x=0.5, y=0.5,
                                  font_size=15, showarrow=False)],
            )
            st.plotly_chart(donut, use_container_width=True)
        else:
            st.info("No priced holdings yet — add holdings in the Holdings tab.")

    with right:
        # Grouped bar: current % vs target % per sleeve.
        bar = go.Figure()
        bar.add_bar(name="Current %", x=sleeve_df["Sleeve"], y=sleeve_df["Current %"],
                    marker_color=TEAL)
        bar.add_bar(name="Target %", x=sleeve_df["Sleeve"], y=sleeve_df["Target %"],
                    marker_color=MUTED)
        bar.update_layout(
            template="plotly_dark", height=340, barmode="group",
            margin=dict(t=10, b=10, l=10, r=10),
            legend=dict(orientation="h", y=1.1),
            yaxis_title="%", xaxis_tickangle=-30,
        )
        st.plotly_chart(bar, use_container_width=True)

    st.markdown("##### Deviation per sleeve")
    disp = sleeve_df.copy()
    disp["Value"] = disp["Value"].map(fmt_inr)
    disp["Current %"] = disp["Current %"].map(lambda v: f"{v:.1f}%")
    disp["Target %"] = disp["Target %"].map(lambda v: f"{v:.0f}%")
    disp["Deviation"] = disp["Deviation"].map(lambda v: f"{v:+.1f}pp")
    st.dataframe(disp, use_container_width=True, hide_index=True)

    st.divider()
    st.markdown("##### 🏦 Loans & EMIs")
    st.caption("Cash-flow only — these financed the (off-balance) land, so outstanding "
               "principal is **never subtracted** from the investment totals above.")
    liabilities = db.get_liabilities()
    if not liabilities:
        st.info("No loans recorded.")
    else:
        loan_rows = []
        total_emi = 0.0
        for l in liabilities:
            prov = (l.get("provenance") or "")
            flag = "⚠ estimate" if "estimated" in prov.lower() else "confirmed"
            emi = l.get("emi") or 0.0
            total_emi += emi
            loan_rows.append({
                "Loan": l.get("name") or "?",
                "Lender": l.get("lender") or "—",
                "Outstanding": fmt_inr(l.get("outstanding") or 0),
                "Rate": f"{l.get('rate_pct'):.1f}%" if l.get("rate_pct") is not None else "—",
                "EMI/mo": fmt_inr(emi),
                "Provenance": flag,
            })
        st.dataframe(pd.DataFrame(loan_rows), use_container_width=True, hide_index=True)
        total_outstanding = sum(l.get("outstanding") or 0 for l in liabilities)
        lc1, lc2 = st.columns(2)
        lc1.metric("Total outstanding", fmt_inr(total_outstanding))
        lc2.metric("Combined EMI/mo", fmt_inr(total_emi))
        st.caption(
            "🏡 These loans financed the off-balance land — shown here as **cash-flow "
            "only**, never netted against your investments. Collateral / payoff path: "
            "parents' NSCs mature **₹36.2L across 2029–31**, timed to cover the loans "
            "as they wind down."
        )

    # ---- Feature C: liabilities editor (2026-07-20) -----------------------
    # Full-column persistence (schema corrected same day against the live
    # DB's sqlite_master DDL — see the schema-history note in db.py): name,
    # lender, principal, outstanding, rate_pct, emi, tenure_months,
    # start_date, provenance. `principal` is NOT NULL on the live table, so
    # the add-loan form REQUIRES it — a blank/0 principal falls back to the
    # outstanding amount (a fresh loan starts with the two equal). Existing-
    # loan edits stay limited to outstanding / EMI / rate / provenance —
    # enough for the monthly pass; the other columns are set at creation.
    with st.expander("✏️ Edit loans"):
        liabilities = db.get_liabilities()
        if liabilities:
            st.markdown("**Existing loans**")
            for l in liabilities:
                _lid = l["id"]
                st.markdown(f"— **{l.get('name') or '?'}**")
                ec1, ec2, ec3 = st.columns(3)
                _new_outstanding = ec1.number_input(
                    "Outstanding (₹)", min_value=0.0,
                    value=float(l.get("outstanding") or 0), step=10000.0,
                    key=f"liab_outstanding_{_lid}")
                _new_emi = ec2.number_input(
                    "EMI (₹/mo)", min_value=0.0, value=float(l.get("emi") or 0),
                    step=500.0, key=f"liab_emi_{_lid}")
                _new_rate = ec3.number_input(
                    "Rate (%)", min_value=0.0, max_value=50.0,
                    value=float(l.get("rate_pct") or 0), step=0.05,
                    format="%.2f", key=f"liab_rate_{_lid}")
                es1, es2 = st.columns([1, 1])
                if es1.button("💾 Save", key=f"liab_save_{_lid}"):
                    # update_liability now returns the affected row count
                    # (review fix) — only claim success on an actual write.
                    _rows = db.update_liability(
                        _lid, outstanding=_new_outstanding, emi=_new_emi,
                        rate_pct=_new_rate,
                        provenance=f"user-confirmed {datetime.now().strftime('%Y-%m-%d')}")
                    if _rows:
                        st.success(f"Saved {l.get('name')}.")
                        st.rerun()
                    else:
                        st.error(f"Save failed — no loan with id {_lid} found. "
                                 "Nothing was written.")
                _del_confirm = es2.text_input(
                    "Type CONFIRM to delete", key=f"liab_delconfirm_{_lid}")
                if _del_confirm.strip() == "CONFIRM":
                    if st.button(f"🗑 Delete {l.get('name')}", key=f"liab_del_{_lid}"):
                        db.delete_liability(_lid)
                        st.success(f"Deleted {l.get('name')}.")
                        st.rerun()
                st.divider()
        else:
            st.caption("No loans recorded yet — add one below.")

        st.markdown("**Add a loan**")
        with st.form("add_liability"):
            al1, al2, al3 = st.columns(3)
            al_name = al1.text_input("Name", placeholder="e.g. Home loan — SBI")
            al_lender = al2.text_input("Lender", placeholder="e.g. SBI")
            al_principal = al3.number_input(
                "Original principal (₹) — required", min_value=0.0, step=10000.0,
                help="NOT NULL on the live table. Leave 0 to default it to the "
                     "outstanding amount (a fresh loan starts with the two equal).")
            al4, al5, al6 = st.columns(3)
            al_outstanding = al4.number_input("Outstanding (₹)", min_value=0.0, step=10000.0)
            al_rate = al5.number_input("Rate (%)", min_value=0.0, max_value=50.0,
                                       step=0.05, format="%.2f")
            al_emi = al6.number_input("EMI (₹/mo)", min_value=0.0, step=500.0)
            al7, al8 = st.columns(2)
            al_tenure = al7.number_input("Tenure (months)", min_value=0, step=1)
            al_start = al8.date_input("Start date", value=datetime.now())
            if st.form_submit_button("➕ Add loan", use_container_width=True):
                # principal is NOT NULL on the live table: required here, with
                # outstanding as the sensible default when left at 0.
                _al_principal = al_principal if al_principal > 0 else al_outstanding
                if al_name and al_outstanding >= 0 and _al_principal > 0:
                    # Note: st.number_input ALWAYS returns a number (never
                    # blank/None) — pass raw values straight through so a
                    # legitimate 0 (0% interest-free loan, 0 months tenure
                    # remaining) is stored as 0, not coerced to NULL.
                    db.add_liability(
                        al_name, al_outstanding, principal=_al_principal,
                        lender=(al_lender.strip() or None),
                        start_date=str(al_start) if al_start else None,
                        emi=al_emi, rate_pct=al_rate,
                        tenure_months=int(al_tenure),
                        provenance="user-entered",
                    )
                    st.success(f"Added {al_name}.")
                    st.rerun()
                else:
                    st.error("Need a name, outstanding ≥ 0, and a principal > 0 "
                             "(principal defaults to the outstanding amount if left 0).")


# ===========================================================================
# TAB — TARGETS (Feature B, 2026-07-20): guided target-allocation sitting.
# The 7 target_allocation sleeves are LOCKED by charter — this tab lets you
# SEE the case for change (current %, deviation, market context) and PROPOSE
# a new split, but saving requires typing CONFIRM: targets move only by
# explicit user decision, never silently, never via a stray click.
# ===========================================================================
TARGETS_LOCKED_SINCE_DEFAULT = "locked since 2026-06-12 (Phase A)"

# Known-evidence context captions per sleeve (task spec, 2026-07-20) — static
# background facts to inform the sitting, not a recommendation of what to type.
TARGETS_CONTEXT_CAPTIONS = {
    "Global / US": "📊 Currently ~58% of the target-sleeve pool (overweight vs its "
        "20% target) — the donut/deviation numbers on Net Worth & Allocation are "
        "the live version of this.",
    "Direct Stocks": "📊 Governed by the scanner + one-new-name-per-month rule "
        "(see the Scanner tab and the Monthly review) — raising this target doesn't "
        "relax that discipline.",
}

with tab_targets:
    st.markdown("##### 🎯 Targets — guided sitting")
    st.caption("The 7 locked sleeves. See the case, propose a new split, save only "
               "by typing CONFIRM. Targets move by explicit decision only — this tab "
               "makes no changes on its own.")

    _mc_targets = market_context()
    _targets_now = dict(db.get_target_allocation())
    _cur_pct_map = {r["Sleeve"]: r["Current %"] for _, r in sleeve_df.iterrows()}
    _dev_map = {r["Sleeve"]: r["Deviation"] for _, r in sleeve_df.iterrows()}

    st.markdown("###### Current standing")
    _std_rows = [{
        "Sleeve": s,
        "Locked target %": f"{_targets_now.get(s, 0):.0f}%",
        "Current actual %": f"{_cur_pct_map.get(s, 0):.1f}%",
        "Deviation": f"{_dev_map.get(s, 0):+.1f}pp",
        "Market context": (context_badge(_mc_targets, s) or "—").lstrip(" ·").strip(),
    } for s in db.SLEEVES]
    st.dataframe(pd.DataFrame(_std_rows), use_container_width=True, hide_index=True)
    for _s, _cap in TARGETS_CONTEXT_CAPTIONS.items():
        st.caption(_cap)

    st.divider()
    st.markdown("###### Propose a new split")
    _proposed = {}
    _cols = st.columns(4)
    for _i, _s in enumerate(db.SLEEVES):
        with _cols[_i % 4]:
            _proposed[_s] = st.number_input(
                f"{_s}", min_value=0.0, max_value=100.0,
                value=float(_targets_now.get(_s, 0.0)), step=1.0,
                key=f"targets_proposed_{_s}",
                help=f"Locked at {_targets_now.get(_s, 0):.0f}% today.",
            )
    _running_sum = sum(_proposed.values())
    _sum_ok = abs(_running_sum - 100.0) <= 0.5
    (st.success if _sum_ok else st.warning)(
        f"Running sum: **{_running_sum:.1f}%** "
        f"{'✅ within 100 ±0.5%' if _sum_ok else '— must equal 100 ±0.5% to save'}"
    )

    st.markdown("###### Save (requires confirmation)")
    st.caption("Charter: targets move only by explicit user decision. Type "
               "**CONFIRM** exactly to enable saving.")
    _confirm_col, _save_col = st.columns([2, 1])
    _confirm_text = _confirm_col.text_input(
        "Type CONFIRM to enable saving", key="targets_confirm_text")
    _can_save = _sum_ok and _confirm_text.strip() == "CONFIRM"
    if _save_col.button("💾 Save new targets", disabled=not _can_save,
                        use_container_width=True):
        # Review fix (ordering + exception safety): the audit record is
        # written FIRST, and target_allocation is updated ONLY if that
        # succeeds — the old order (targets first, audit second) could leave
        # targets silently changed with no audit trail and a crashed UI if
        # the audit-append raised (e.g. a corrupted non-list targets_history
        # value). append_app_state_list(strict read) itself raises rather
        # than silently treating a read failure as "no history yet", so a
        # transient DB hiccup here also aborts cleanly instead of risking a
        # lost audit entry.
        _old = dict(_targets_now)
        _new = dict(_proposed)
        try:
            db.append_app_state_list("targets_history", {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "old": _old,
                "new": _new,
            })
        except ValueError:
            st.error("🚫 targets_history is corrupted (holds a non-list value) — "
                     "ABORTED. Nothing changed. Please report this before retrying.")
        except Exception as _e:
            st.error(f"🚫 Could not write the audit record — ABORTED, targets were "
                     f"NOT changed. Try again. ({type(_e).__name__})")
        else:
            try:
                for _s, _pct in _new.items():
                    db.set_target_allocation(_s, _pct)
            except Exception as _e:
                st.error(f"⚠ Audit record was saved, but the target_allocation update "
                         f"FAILED partway through — targets may be in a MIXED state. "
                         f"Re-check the table below and re-save if needed. ({type(_e).__name__})")
            else:
                st.success("Saved. Targets updated and logged to the audit history below.")
                st.rerun()
    elif _confirm_text and not _can_save:
        if not _sum_ok:
            st.error("Fix the running sum to 100 ±0.5% first.")
        else:
            st.error("Type CONFIRM exactly (case-sensitive) to enable saving.")

    st.divider()
    st.markdown("###### Change history")
    _th = db.get_app_state("targets_history")
    if _th and not isinstance(_th, list):
        # Visible corruption flag rather than silently treating a non-list
        # value as "empty history" (review fix) — this key should only ever
        # hold a JSON list; anything else means something wrote to it wrong.
        st.error(f"⚠ targets_history holds an unexpected {type(_th).__name__}, not a "
                 "list — the save button above will refuse to append until this is "
                 "fixed. History display below is empty pending a fix.")
        _th_list = []
    else:
        _th_list = _th if isinstance(_th, list) else []
    if not _th_list:
        st.caption(f"📌 {TARGETS_LOCKED_SINCE_DEFAULT} — no changes on file yet.")
    else:
        _last = _th_list[-1]
        st.caption(f"📌 Last changed: {_last.get('ts', '?')}")
        with st.expander(f"Full history ({len(_th_list)} change"
                         f"{'s' if len(_th_list) != 1 else ''})"):
            for _rec in reversed(_th_list):
                _old_s = ", ".join(f"{k} {v:.0f}%" for k, v in (_rec.get("old") or {}).items())
                _new_s = ", ".join(f"{k} {v:.0f}%" for k, v in (_rec.get("new") or {}).items())
                st.markdown(f"**{_rec.get('ts', '?')}**  \n"
                           f"From: {_old_s}  \n"
                           f"To: {_new_s}")


# ===========================================================================
# TAB 2 — CASH FLOW & SAVINGS RATE
# ===========================================================================
with tab2:
    st.markdown("##### Log this month's cash flow")
    st.caption("Investable amount varies month to month — log each month; the "
               "rest computes.")

    with st.form("cashflow_form"):
        cc1, cc2, cc3 = st.columns(3)
        month = cc1.text_input("Month (YYYY-MM)", value=datetime.now().strftime("%Y-%m"))
        inv = cc2.number_input("Investable this month (₹)", min_value=0.0,
                               value=float(cf.get("investable", 100000)), step=5000.0)
        exp = cc3.number_input("Expenses this month (₹)", min_value=0.0,
                               value=float(cf.get("expenses", 60000)), step=5000.0)
        cc4, cc5 = st.columns(2)
        pf_in = cc4.number_input("PF contribution (₹)", min_value=0.0,
                                 value=s_float("pf_monthly", 37500), step=2500.0)
        nps_in = cc5.number_input("NPS contribution (₹)", min_value=0.0,
                                  value=s_float("nps_monthly", 10000), step=1000.0)
        if st.form_submit_button("Save month", use_container_width=True):
            db.upsert_cashflow(month, inv, exp, pf_in, nps_in)
            st.success(f"Saved {month}.")
            st.rerun()

    income_m = inv + exp
    sr = (inv / income_m * 100.0) if income_m else 0.0
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Income (implied)", fmt_inr(income_m), help="Investable + expenses")
    m2.metric("Expenses", fmt_inr(exp))
    m3.metric("Investable", fmt_inr(inv))
    m4.metric("Savings Rate", f"{sr:.0f}%")

    st.markdown("##### History")
    cf_hist = db.get_cashflow()
    if cf_hist:
        hdf = pd.DataFrame(cf_hist)
        hdf["income"] = hdf["investable"] + hdf["expenses"]
        hdf["savings_rate"] = (hdf["investable"] / hdf["income"] * 100).round(1)

        fig = go.Figure()
        fig.add_bar(name="Investable", x=hdf["month"], y=hdf["investable"], marker_color=TEAL)
        fig.add_bar(name="Expenses", x=hdf["month"], y=hdf["expenses"], marker_color=RED)
        fig.add_scatter(name="Savings Rate %", x=hdf["month"], y=hdf["savings_rate"],
                        yaxis="y2", mode="lines+markers", line=dict(color=GOLD))
        fig.update_layout(
            template="plotly_dark", height=320, barmode="group",
            margin=dict(t=10, b=10, l=10, r=10),
            yaxis_title="₹", yaxis2=dict(title="%", overlaying="y", side="right"),
            legend=dict(orientation="h", y=1.15),
        )
        st.plotly_chart(fig, use_container_width=True)

        show = hdf[["month", "income", "expenses", "investable", "pf", "nps", "savings_rate"]].copy()
        for col in ["income", "expenses", "investable", "pf", "nps"]:
            show[col] = show[col].map(fmt_inr)
        show["savings_rate"] = show["savings_rate"].map(lambda v: f"{v:.0f}%")
        st.dataframe(show, use_container_width=True, hide_index=True)

        del_m = st.selectbox("Delete a month", [""] + [r["month"] for r in cf_hist])
        if del_m and st.button(f"Delete {del_m}"):
            db.delete_cashflow(del_m)
            st.rerun()
    else:
        st.info("No cash-flow rows yet — log your first month above.")


# ===========================================================================
# TAB 3 — FI PROJECTION
# ===========================================================================
with tab3:
    st.markdown("##### Projection inputs")
    st.caption("Edit and the three scenarios recompute. Expenses are a "
               "**projectable variable** — model a future lifestyle/child cost.")

    with st.form("fi_form"):
        a1, a2, a3 = st.columns(3)
        cur_age = a1.number_input("Current age", 18, 70, s_int("age_self", 30))
        ret_age = a2.number_input("Retirement age", int(cur_age) + 1, 80,
                                  s_int("retirement_age", 50))
        infl = a3.number_input("Inflation %", 0.0, 15.0, s_float("inflation_pct", 6.0), step=0.5)

        b1, b2, b3 = st.columns(3)
        _corpus_stored = s_float("current_corpus", 0)
        cur_corpus = b1.number_input("Current growth corpus (₹)", 0.0,
                                     value=_corpus_stored or net_worth_growth,
                                     step=50000.0,
                                     help="Defaults to your priced growth sleeve")
        b1.caption("📌 stored override" if _corpus_stored > 0 else "🟢 live from holdings")
        mc = b2.number_input("Monthly contribution (₹)", 0.0,
                             value=s_float("monthly_contribution", 100000), step=5000.0)
        mc_step = b3.number_input("Contribution step-up %/yr", 0.0, 30.0,
                                  s_float("contribution_stepup_pct", 8.0), step=1.0)

        c1, c2, c3 = st.columns(3)
        swr = c1.number_input("Safe withdrawal rate %", 2.0, 6.0,
                              s_float("swr_pct", 3.5), step=0.25,
                              help="FI number = annual expenses ÷ SWR")
        exp_now = c2.number_input("Expenses now (₹/mo)", 0.0,
                                  value=s_float("expense_now_monthly", 60000), step=5000.0)
        exp_tgt = c3.number_input("Expenses target (₹/mo, today's ₹)", 0.0,
                                  value=s_float("expense_target_monthly", 90000), step=5000.0)
        exp_year = st.slider("Years until expense target reached", 1, 30,
                             s_int("expense_target_year", 8),
                             help="e.g. child arrives / lifestyle change")

        st.markdown("**Debt layer** (projected separately from the growth sleeve)")
        d1, d2, d3 = st.columns(3)
        _pf_stored = s_float("pf_current", 0)
        pf_cur = d1.number_input("PF current (₹)", 0.0,
                                 value=_pf_stored or _epf_from_holdings, step=50000.0,
                                 help="Leave 0 if EPF is already a holdings row (Debt / Safety) — "
                                      "it auto-feeds from there; setting both double-counts.")
        d1.caption("📌 stored override" if _pf_stored > 0 else "🟢 live from holdings")
        pf_mo = d2.number_input("PF monthly (₹)", 0.0, value=s_float("pf_monthly", 37500), step=2500.0)
        nps_cur = d3.number_input("NPS current (₹)", 0.0, value=s_float("nps_current", 0), step=50000.0)
        d4, d5 = st.columns(2)
        nps_mo = d4.number_input("NPS monthly (₹)", 0.0, value=s_float("nps_monthly", 10000), step=1000.0)
        st.caption("PF: step 5%/yr @ 7.5% · NPS: step 10%/yr @ 9% (locked per spec)")

        saved = st.form_submit_button("Recompute & save", use_container_width=True)

    if saved:
        db.set_settings({
            "age_self": int(cur_age), "retirement_age": int(ret_age),
            "inflation_pct": infl, "current_corpus": cur_corpus,
            "monthly_contribution": mc, "contribution_stepup_pct": mc_step,
            "swr_pct": swr, "expense_now_monthly": exp_now,
            "expense_target_monthly": exp_tgt, "expense_target_year": int(exp_year),
            "pf_current": pf_cur, "pf_monthly": pf_mo,
            "nps_current": nps_cur, "nps_monthly": nps_mo,
        })
        st.success("Saved. Recomputed below.")

    # Build inputs from the (possibly just-edited) form values directly.
    live_inp = FIInputs(
        current_age=int(cur_age), retirement_age=int(ret_age), inflation_pct=infl,
        current_corpus=cur_corpus, monthly_contribution=mc,
        contribution_stepup_pct=mc_step, swr_pct=swr,
        expense_now_monthly=exp_now, expense_target_monthly=exp_tgt,
        expense_target_year=int(exp_year),
        pf_current=pf_cur, pf_monthly=pf_mo, pf_stepup_pct=s_float("pf_stepup_pct", 5.0),
        pf_return_pct=s_float("pf_return_pct", 7.5),
        nps_current=nps_cur, nps_monthly=nps_mo, nps_stepup_pct=s_float("nps_stepup_pct", 10.0),
        nps_return_pct=s_float("nps_return_pct", 9.0),
    )
    scenarios = run_scenarios(live_inp, scenarios=(8.0, 10.0, 12.0))

    st.markdown("##### Outcome at retirement age — **real (inflation-adjusted)** shown big")
    cols = st.columns(3)
    for col, (rate, res) in zip(cols, scenarios.items()):
        with col:
            st.markdown(f"**{rate:.0f}% return scenario**")
            st.metric("Real corpus (today's ₹)", fmt_inr(res.final_total_real),
                      help="Inflation-adjusted — the number that matters")
            st.caption(f"Nominal: {fmt_inr(res.final_total_nominal)}")
            fi_txt = f"age {res.fi_age}" if res.fi_age else "not within horizon"
            st.metric("FI reached at", fi_txt)
            st.caption(f"FI number (real): {fmt_inr(res.final_fi_number_real)}")

    # Trajectory chart — real corpus vs real FI number across scenarios.
    st.markdown("##### Trajectory — real corpus vs FI number")
    fig = go.Figure()
    scen_colors = {8.0: BLUE, 10.0: TEAL, 12.0: GOLD}
    base = scenarios[10.0]
    ages = [r.age for r in base.rows]
    for rate, res in scenarios.items():
        fig.add_scatter(x=ages, y=[r.total_real for r in res.rows],
                        name=f"{rate:.0f}% (real corpus)", mode="lines",
                        line=dict(color=scen_colors[rate], width=2))
    fig.add_scatter(x=ages, y=[r.fi_number_real for r in base.rows],
                    name="FI number (real)", mode="lines",
                    line=dict(color=RED, width=2, dash="dash"))
    fig.update_layout(
        template="plotly_dark", height=380, margin=dict(t=10, b=10, l=10, r=10),
        xaxis_title="Age", yaxis_title="₹ (today's money)",
        legend=dict(orientation="h", y=1.12),
    )
    st.plotly_chart(fig, use_container_width=True)

    with st.expander("Year-by-year table (10% scenario)"):
        rows = [{
            "Age": r.age,
            "Growth": fmt_inr(r.growth_nominal),
            "PF": fmt_inr(r.pf_nominal),
            "NPS": fmt_inr(r.nps_nominal),
            "Total (nominal)": fmt_inr(r.total_nominal),
            "Total (real)": fmt_inr(r.total_real),
            "Annual expense": fmt_inr(r.annual_expense_nominal),
            "FI number": fmt_inr(r.fi_number_nominal),
            "FI?": "✅" if r.fi_reached else "",
        } for r in base.rows]
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


# ===========================================================================
# TAB 4 — HOLDINGS
# ===========================================================================
with tab4:
    st.markdown("##### Add a holding")
    with st.form("add_holding"):
        h1, h2, h3 = st.columns(3)
        name = h1.text_input("Asset name", placeholder="e.g. Parag Parikh Flexi Cap")
        sleeve = h2.selectbox(
            "Sleeve", list(db.SLEEVES) + ["Transit (redeploying)", "Debt / Safety",
                                          "Family (Parents)", "Family (Father)",
                                          EMERGENCY_SLEEVE],
            help="Transit = money with a landing date you'll redeploy (chits, short FDs). "
                 "Debt / Safety / Family (Parents) = locked safety floor (EPF, post "
                 "office, NSCs) outside growth targets. Family (Father) = his MF, "
                 "counted as growth (one-household doctrine). "
                 f"{EMERGENCY_SLEEVE} = liquid buffer (bank/liquid-fund/sweep-FD) — its "
                 "own layer, feeds the Deploy tab's emergency line automatically.",
        )
        # "crypto" intentionally excluded (Vault doctrine, 2026-07-21): no
        # crypto tracking here — crypto exposure only via the Sentinel
        # probation charter (dt_system/RUNBOOK.md §4), never the Vault.
        atype = h3.selectbox(
            "Type", ["mf", "stock", "global", "manual"],
            help="mf=AMFI NAV · stock=yfinance .NS · global=yfinance USD→INR · "
                 "manual=enter price yourself",
        )
        h4, h5, h6 = st.columns(3)
        ticker = h4.text_input("Ticker / scheme code / coin",
                               placeholder="122639 / RELIANCE.NS / VOO / BTC")
        qty = h5.number_input("Quantity / units", min_value=0.0, step=1.0, format="%.4f")
        buy_price = h6.number_input("Buy price (₹)", min_value=0.0, step=1.0)
        h7, h8, h9 = st.columns(3)
        buy_date = h7.date_input("Buy date", value=datetime.now())
        manual_price = h8.number_input("Manual price override (₹, 0 = auto)",
                                       min_value=0.0, step=1.0)
        maturity = h9.date_input(
            "Maturity / landing date (optional)", value=None,
            help="FDs, chits, bonds: when the money lands. Drives the Money "
                 "timeline + approaching alerts.",
        )
        if st.form_submit_button("Add holding", use_container_width=True):
            if name and qty > 0:
                db.add_holding(
                    name, sleeve, atype, ticker.strip(), qty, buy_price,
                    str(buy_date), manual_price if manual_price > 0 else None,
                    str(maturity) if maturity else None,
                )
                st.success(f"Added {name}.")
                st.rerun()
            else:
                st.error("Need at least an asset name and quantity > 0.")

    st.markdown("##### Holdings — auto-priced, grouped by sleeve")
    st.caption("**Suggestion** chip: locked sleeves hold, Family (Father) keeps its SIP, "
               "Transit needs redeploying, Emergency (liquid) is goal-tracked not %-target, "
               "target sleeves compare current vs target % "
               "(±20%/±50% bands — same rule as the Rebalance signal in Goals).")
    if holdings_df.empty:
        st.info("No holdings yet.")
    else:
        _extra_sleeves = sorted(s for s in holdings_df["Sleeve"].unique()
                                if s not in db.SLEEVES)
        _sleeve_pct = {r["Sleeve"]: r["Current %"] for _, r in sleeve_df.iterrows()}
        _sleeve_tgt = {r["Sleeve"]: r["Target %"] for _, r in sleeve_df.iterrows()}
        for sleeve in list(db.SLEEVES) + _extra_sleeves:
            sdf = holdings_df[holdings_df["Sleeve"] == sleeve]
            if sdf.empty:
                continue
            sval = sdf["Value"].sum()
            _chip, _reason = suggestion_chip(
                sleeve, _sleeve_pct.get(sleeve), _sleeve_tgt.get(sleeve))
            st.markdown(f"**{sleeve}** — {fmt_inr(sval)}  ·  `{_chip}` — {_reason}")
            view = sdf.copy()
            view["Value"] = view["Value"].map(fmt_inr)
            view["Cost"] = view["Cost"].map(fmt_inr)
            view["Price"] = view["Price"].map(lambda v: f"₹{v:,.2f}")
            view["Buy"] = view["Buy"].map(lambda v: f"₹{v:,.2f}")
            view["P/L"] = view["P/L"].map(fmt_inr)
            view["P/L %"] = view["P/L %"].map(lambda v: f"{v:+.1f}%")
            view["CAGR %"] = view["CAGR %"].map(
                lambda v: f"{v:+.1f}%" if pd.notna(v) else "—")
            view["XIRR (fwd)"] = view["XIRR (fwd)"].map(returns.fmt_xirr)
            view["Stale"] = view["Stale"].map(lambda v: "⚠" if v else "")
            view["Suggestion"] = _chip
            cols = ["Asset", "Qty", "Buy", "Price", "Value", "Cost",
                    "P/L", "P/L %", "CAGR %", "XIRR (fwd)", "Suggestion",
                    "Source", "Stale", "Age"]
            if view["Maturity"].notna().any():
                view["Lands"] = view.apply(
                    lambda r: (f"⏳ {(datetime.fromisoformat(str(r['Maturity'])).date() - datetime.now().date()).days}d"
                               + (" (est.)" if r.get("Prov") == "estimated" else ""))
                    if pd.notna(r["Maturity"]) and r["Maturity"] else "—", axis=1)
                cols.insert(1, "Lands")
            st.dataframe(view[cols], use_container_width=True, hide_index=True)

        del_id = st.selectbox(
            "Delete a holding",
            [0] + holdings_df["id"].tolist(),
            format_func=lambda i: "—" if i == 0 else
            f"{holdings_df.loc[holdings_df['id']==i,'Asset'].values[0]}",
        )
        if del_id and st.button("Delete selected holding"):
            db.delete_holding(int(del_id))
            st.rerun()


# ===========================================================================
# TAB 5 — GOALS & REBALANCE
# ===========================================================================
with tab5:
    if _concentration_hit:
        st.warning(concentration_warning_text(_concentration_hit))

    st.markdown("##### 📋 Suggestions — rule-based, priority-ordered, from live data")
    if sleeve_pool > 0:
        _mc = market_context()
        _tips = []
        # Underweights first, ranked by rupee gap (biggest hole = Priority 1).
        # The gap maths is itself price-responsive: when a sleeve's market falls,
        # its current % shrinks and its gap (priority + ₹) grows automatically.
        # Uses sleeve_pool (target sleeves only, Fix 1) — same basis as Current %/
        # Deviation above, so the rupee amount here always agrees with the gap %.
        _unders, _overs = [], []
        for _, _r in sleeve_df.iterrows():
            _gap = _r["Target %"] - _r["Current %"]
            _amt = _gap / 100.0 * sleeve_pool
            if _gap >= 5:
                _unders.append((_amt, _gap, _r))
            elif _gap <= -5:
                _overs.append((_gap, _r))
        _unders.sort(key=lambda x: -x[0])
        for _i, (_amt, _gap, _r) in enumerate(_unders, 1):
            _tips.append(f"🎯 **Priority {_i} — {_r['Sleeve']}**: underweight "
                         f"({_r['Current %']:.0f}% vs {_r['Target %']:.0f}% target) → "
                         f"route the next {fmt_inr(_amt)} of fresh money here"
                         f"{context_badge(_mc, _r['Sleeve'])}")
        for _gap, _r in _overs:
            _x = (" Follow the monthly Direct-Stocks review — no ad-hoc sells."
                  if _r["Sleeve"] == "Direct Stocks" else
                  " No new money; let growth elsewhere dilute it.")
            _tips.append(f"**{_r['Sleeve']}** overweight ({_r['Current %']:.0f}% vs "
                         f"{_r['Target %']:.0f}%).{_x}{context_badge(_mc, _r['Sleeve'])}")
        if transit_value > 0:
            _ft = sleeve_pool + transit_value
            _gaps = []
            for _, _r in sleeve_df.iterrows():
                _need = max(0.0, _r["Target %"] / 100.0 * _ft - _r["Value"])
                if _need > 1000:
                    _gaps.append((_r["Sleeve"], _need))
            _gs = sum(g for _, g in _gaps) or 1.0
            _plan = " · ".join(f"{n} {fmt_inr(transit_value * g / _gs)}"
                               for n, g in sorted(_gaps, key=lambda x: -x[1])[:5])
            _tev = [e for e in upcoming_events(holdings_df)
                    if e["kind"] == "transit" and e["days"] >= -3]
            _sched = "  \n".join(
                f"  · **{e['name'][:46]}** {fmt_inr(e['value'])} — lands in "
                f"**{'~' if e.get('prov') == 'estimated' else ''}{e['days']}d** "
                f"({'est. ' if e.get('prov') == 'estimated' else ''}"
                f"{datetime.fromisoformat(e['date']).strftime('%d %b')})"
                for e in _tev)
            _tips.append(f"⏳ **{fmt_inr(transit_value)} in transit**, each piece dated:  \n"
                         f"{_sched}  \n"
                         f"  → landing plan on arrival: {_plan}. "
                         f"Lock this on paper before the money arrives.")
        if safety_value > 0:
            _tips.append(f"🛡 **Locked-in base** {fmt_inr(safety_value)} (EPF, post office, "
                         f"all 7 NSCs incl. parents') stays outside growth targets — "
                         f"guaranteed but locked, not spendable, by design untouched.")
        _tips.append("_Rules: cash-flow rebalancing (new money → underweight sleeves; "
                     "selling = last resort) · 5% deviation band · Direct Stocks governed "
                     "by the monthly rulebook review._")
        for _t in _tips:
            st.markdown(f"- {_t}")
        if _mc:
            _upd = next(iter(_mc.values())).get("updated", "")
            st.caption(f"📊 price context from MAVI Sentinel's verified feeds, {_upd} · "
                       "The rules (Codex 082): events are reminders, not decisions · sleeve "
                       "gaps decide priority, not price badges · badges are context only · "
                       "PF lives in one place, never both.")
        with st.expander("🛒 What exactly to buy, sleeve by sleeve (instruments + how)"):
            # Guidance is CONDITIONAL on live sleeve state (Codex 082): buy text is
            # actionable only where the sleeve is underweight and inside its cap.
            _guide = {
                "Nifty 50 (core)": "One index fund, direct-growth plan: *UTI Nifty 50 "
                    "Index Fund* or *Nippon Nifty 50 Index Fund* (via Groww/Coin/Dhan), "
                    "or the ETF *NIFTYBEES* for exchange delivery. Lump or split over "
                    "2–3 weeks — either is fine at this size.",
                "Nifty Next 50": "*ICICI Pru Nifty Next 50 Index Fund* or *UTI Next 50* "
                    "direct-growth (ETF alternative: *JUNIORBEES*). Same mechanics as the core.",
                "Direct Stocks": "ONLY through the monthly rulebook review (Codex 081): "
                    "HOLD/SELL list first, BUYs only inside the 20% envelope. No ad-hoc "
                    "buying because a stock \"looks good\".",
                "Global / US": "When this sleeve is open for money again: an S&P 500 "
                    "index fund/FoF (e.g. *Motilal Oswal S&P 500*) is simpler than direct "
                    "US stocks — no LRS paperwork, no 20% TCS on remittance, still dollar "
                    "exposure. Tesla stays as-is.",
                "Gold": "*GOLDBEES* ETF in the demat, or Sovereign Gold Bonds from the "
                    "secondary market when yield-to-maturity is fair (SGB adds 2.5%/yr "
                    "interest, tax-free at maturity).",
                # Crypto sleeve retired 2026-07-21 (targets sitting) — crypto
                # exposure only via the Sentinel probation charter, never the Vault.
                "Tactical / thematic": "Your own theses (PSU, momentum funds, etc.). The "
                    "one rule: a written entry reason + exit condition per position, "
                    "reviewed monthly alongside the stocks.",
            }
            for _, _r in sleeve_df.iterrows():
                _g = _guide.get(_r["Sleeve"])
                if not _g:
                    continue
                _gap = _r["Target %"] - _r["Current %"]
                if _gap >= 5:
                    _st = "✅ underweight — new money welcome here"
                elif _gap <= -5:
                    _st = "⛔ overweight — NO new money; reference only"
                else:
                    _st = "⏸ at target — maintenance only"
                st.markdown(f"**{_r['Sleeve']}** · {_st}  \n{_g}")
            st.caption("Buy guidance is actionable ONLY where the sleeve is underweight "
                       "and inside its cap. Sleeve gaps decide priority; price badges are "
                       "context, not signals. (Codex 082)")
        st.divider()
    st.markdown("##### Goal progress")
    goals = db.get_goals()
    # Emergency fund progress uses the LIVE holdings-derived balance
    # (emergency_holdings_value — the same source Start Here/Deploy/Net Worth
    # all use), never a manually-typed setting. Milestones track net worth.
    for g in goals:
        if g["kind"] == "emergency":
            current = emergency_holdings_value
        elif g["kind"] == "travel":
            current = s_float("travel_current", 0)   # earmarked only — not net worth
        else:
            current = total_net_worth
        pct = min(current / g["target_amount"] * 100.0, 100.0) if g["target_amount"] else 0.0
        st.markdown(f"**{g['name']}** — {fmt_inr(current)} / {fmt_inr(g['target_amount'])} "
                    f"({pct:.0f}%)")
        st.progress(min(pct / 100.0, 1.0))

    with st.expander("Edit goals / emergency fund balance"):
        st.caption("🚨 Emergency fund progress is now LIVE from the "
                   f"{EMERGENCY_SLEEVE} sleeve's holdings — add/edit those in the "
                   "Holdings tab. There's no manual balance to type here anymore.")
        st.divider()
        with st.form("add_goal"):
            gn = st.text_input("Goal name")
            gt = st.number_input("Target (₹)", min_value=0.0, step=100000.0)
            gk = st.selectbox("Kind", ["milestone", "travel", "emergency"])
            if st.form_submit_button("Add / update goal"):
                if gn and gt > 0:
                    db.upsert_goal(gn, gt, gk)
                    st.rerun()
        dg = st.selectbox("Delete goal", [0] + [g["id"] for g in goals],
                          format_func=lambda i: "—" if i == 0 else
                          next((g["name"] for g in goals if g["id"] == i), ""))
        if dg and st.button("Delete goal"):
            db.delete_goal(int(dg))
            st.rerun()

    st.divider()
    st.markdown("##### Rebalance signal")
    st.caption("Buy / hold / trim per sleeve vs locked target. Threshold ±3pp.")
    if sleeve_pool <= 0:
        st.info("Add priced holdings to generate rebalance signals.")
    else:
        signals = []
        for _, r in sleeve_df.iterrows():
            dev = r["Deviation"]
            target_val = r["Target %"] / 100.0 * sleeve_pool
            gap_val = target_val - r["Value"]
            if dev <= -3:
                action, color = "🟢 BUY", GREEN
            elif dev >= 3:
                action, color = "🔴 TRIM", RED
            else:
                action, color = "⚪ HOLD", MUTED
            signals.append({
                "Sleeve": r["Sleeve"],
                "Current %": f"{r['Current %']:.1f}%",
                "Target %": f"{r['Target %']:.0f}%",
                "Deviation": f"{dev:+.1f}pp",
                "₹ to target": fmt_inr(gap_val),
                "Action": action,
            })
        st.dataframe(pd.DataFrame(signals), use_container_width=True, hide_index=True)


st.divider()
_backend = db.backend_name()
_store = ("☁️ Turso (cloud — permanent)" if _backend == "turso"
          else "💾 local SQLite (wealth.db)")
st.caption(
    f"Prices: AMFI (MF NAVs) · yfinance (stocks/global) · CoinGecko (crypto) · "
    f"cached 1h · stale-fallback on failure. Storage: {_store}. "
    f"Last render {datetime.now().strftime('%Y-%m-%d %H:%M')}."
)
# If Turso creds were set but unreachable, warn loudly — data is NOT persisting.
if db.turso_error():
    st.warning(f"⚠️ Turso configured but unreachable — running on local SQLite "
               f"(data will NOT persist on Cloud). Reason: {db.turso_error()}")
