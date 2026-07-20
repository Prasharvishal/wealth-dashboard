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
  app.py       — this file: auth gate, theme, 5 tabs, charts
"""

from datetime import datetime, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import db
import prices
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
    "Crypto": "#f778ba",
    "Tactical / thematic": "#56d364",
}

# Plain-English "what do I actually buy for this sleeve" hints, shown in the
# Deploy tab so a non-expert knows where the money goes.
INSTRUMENT_HINTS = {
    "Nifty 50 (core)": "Nifty 50 index fund / ETF (e.g. UTI or Nippon Nifty 50 Index Fund)",
    "Nifty Next 50": "Nifty Next 50 index fund (e.g. ICICI/UTI Next 50 Index Fund)",
    "Direct Stocks": "Your own researched stocks (the 20% you pick yourself)",
    "Global / US": "S&P 500 / Nasdaq-100 fund (e.g. Motilal Oswal S&P 500 Index Fund)",
    "Gold": "Gold ETF, Sovereign Gold Bond, or digital gold",
    "Crypto": "BTC / SOL via your exchange (Delta, CoinDCX, etc.)",
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
    """Price every holding, compute value / cost / P&L / CAGR. Returns DataFrame."""
    rows = []
    any_stale = False
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
            "Stale": stale,
            "Source": source,
            "Age": prices.last_price_age(h),
            "Maturity": h.get("maturity_date"),
            "Prov": h.get("maturity_provenance"),
        })
    df = pd.DataFrame(rows)
    return df, any_stale


def market_context():
    """Live 'cheap or expensive vs its own 200-day average' per sleeve.

    Rows are pushed into Turso by MAVI Sentinel twice daily (06:05 / 17:15 IST)
    from the same verified price feeds the watchers use. Informational ONLY:
    it never reorders targets or overrides the allocation rules — it tells you
    what kind of price you're paying when you deploy.
    """
    try:
        return {r["sleeve"]: r for r in db.query("SELECT * FROM market_context")}
    except Exception:
        return {}


def context_badge(mc, sleeve):
    """One-line price-context sentence for a sleeve, or '' if no benchmark.

    Wording is deliberately NEUTRAL (Codex 082 ruling): state position vs the
    200-day average and trend state only — never imply cheap = attractive.
    Sleeve gaps decide priority; this line is context, not a signal.
    """
    r = mc.get(sleeve)
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
    if target_pct is None:
        return "HOLD", "no target set for this sleeve yet"
    if sleeve_pct < target_pct * 0.8:
        return "INCREASE", f"sleeve at {sleeve_pct:.0f}% vs {target_pct:.0f}% target — underweight"
    if sleeve_pct > target_pct * 1.5:
        return "PAUSE", f"sleeve at {sleeve_pct:.0f}% vs {target_pct:.0f}% target — overweight"
    return "HOLD", f"sleeve near target ({sleeve_pct:.0f}% vs {target_pct:.0f}%)"


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
                       "days": d, "kind": kind, "prov": prov})
    nm = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
    ev.append({"name": "Monthly Direct-Stocks review (10-min rulebook pass)",
               "value": 0, "date": nm.isoformat(), "days": (nm - today).days,
               "kind": "ritual", "prov": "confirmed"})
    ev.sort(key=lambda e: e["days"])
    return ev


def sleeve_breakdown(df):
    """Current value per sleeve + target % comparison. Returns DataFrame."""
    targets = dict(db.get_target_allocation())
    # Growth/investable total = target sleeves + one-household growth extras
    # (e.g. "Family (Father)" MF — a real market asset with no target % of its
    # own yet). Debt/Safety and Family (Parents) sit outside, in the locked layer.
    _growth_sleeves = set(targets) | GROWTH_EXTRA_SLEEVES
    total = (df.loc[df["Sleeve"].isin(_growth_sleeves), "Value"].sum() if not df.empty else 0.0)
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
sleeve_df, net_worth_growth = sleeve_breakdown(holdings_df)

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
# lands in exactly ONE of three buckets — growth (net_worth_growth, computed
# in sleeve_breakdown above from target sleeves + GROWTH_EXTRA_SLEEVES),
# transit (dated, awaiting redeployment), or the locked safety floor
# (Debt/Safety + Family (Parents) NSCs). Explicit membership sets avoid the
# old "everything not a target sleeve" catch-all, which orphaned/double-counted
# non-target growth sleeves like Family (Father).
_growth_sleeves = set(_tgt_sleeves) | GROWTH_EXTRA_SLEEVES
transit_value = (holdings_df.loc[holdings_df["Sleeve"] == "Transit (redeploying)", "Value"].sum()
                 if not holdings_df.empty else 0.0)
safety_value = (holdings_df.loc[holdings_df["Sleeve"].isin(SAFETY_LAYER_SLEEVES), "Value"].sum()
                if not holdings_df.empty else 0.0)
_unmapped_sleeves = (sorted(set(holdings_df["Sleeve"].unique())
                            - _growth_sleeves - SAFETY_LAYER_SLEEVES
                            - {"Transit (redeploying)"})
                     if not holdings_df.empty else [])
# Household total: EPF already sits inside safety_value (holdings rows), so add
# only the EXPLICIT pf/nps settings here — never the holdings-derived feed —
# or EPF would be counted twice.
# Codex 082 P1 guard: if BOTH sources exist (EPF holdings rows AND a pf_current
# setting), net worth uses the holdings rows only and a blocking warning renders.
_pf_setting = s_float("pf_current", 0)
_pf_conflict = _epf_from_holdings > 0 and _pf_setting > 0
total_net_worth = (net_worth_growth + safety_value + transit_value
                   + (0.0 if _pf_conflict else _pf_setting)
                   + s_float("nps_current", 0))

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

tab_start, tab1, tab_deploy, tab2, tab3, tab4, tab5 = st.tabs([
    "🚀 Start Here",
    "📊 Net Worth & Allocation",
    "💸 Deploy This Month",
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
        plan = deploy_plan(amt, sleeve_df, net_worth_growth, "rebalance")
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
            watype = w3.selectbox("Type", ["mf", "stock", "global", "crypto", "manual"],
                                  help="mf=AMFI code · stock=.NS ticker · global=US ticker "
                                       "· crypto=BTC/SOL · manual=type price")
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
        st.write("Your starting picture:")
        m1, m2, m3 = st.columns(3)
        m1.metric("Net Worth", fmt_inr(total_net_worth))
        m2.metric("FI Progress", f"{base_result.fi_progress_pct:.0f}%")
        m3.metric("Est. FI Date",
                  f"age {base_result.fi_age}" if base_result.fi_age
                  else f">{fi_inp.retirement_age}")
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
    _em_goal = float(_manifest.get("emergency_goal") or EMERGENCY_GOAL_DEFAULT)

    dc1, dc2, dc3 = st.columns([1, 1, 1.3])
    with dc1:
        amt = st.number_input("Amount to invest this month (₹)", min_value=0.0,
                              value=float(investable or 100000), step=10000.0)
    with dc2:
        em_bal = st.number_input(
            "Liquid emergency money you already hold (₹)", min_value=0.0,
            value=float(st.session_state.get("deploy_em_balance", 0.0)), step=10000.0,
            help="Bank / liquid-fund / sweep-FD balance — the TRUE liquid buffer. "
                 "EPF and NSCs don't count; they're locked.",
            key="deploy_em_balance",
        )
    with dc3:
        mode_label = st.radio(
            "How to split the sleeve portion",
            ["Smart — rebalance-aware (recommended)", "Simple — pure target %"],
            help="Smart steers new money into whichever sleeves are below target, "
                 "so you rebalance without selling. With no holdings yet, both "
                 "give the same answer.",
        )
    mode = "rebalance" if mode_label.startswith("Smart") else "simple"

    em_amount, sleeve_plan = emergency_first_plan(
        amt, em_bal, sleeve_df, net_worth_growth, _em_floor, mode)
    tgt_map = dict(db.get_target_allocation())

    # Each entry: (label, why, rupee amount, color-tag). color-tag drives the
    # bar chart below without re-parsing formatted strings.
    _plan = []
    if em_amount > 0:
        _plan.append((
            "🚨 Emergency fund — liquid MF / sweep-FD",
            (f"you hold {fmt_inr(em_bal)} of the {fmt_inr(_em_floor)} floor "
             f"(Vault goal {fmt_inr(_em_goal)}) — safety before markets"),
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
    elif mode == "rebalance" and net_worth_growth > 0:
        st.info("ℹ️ **Smart mode** is funneling more into your under-weight "
                "sleeves first, so your portfolio drifts back toward target "
                "without you selling anything.")
    elif net_worth_growth <= 0:
        st.info("ℹ️ No holdings yet, so this is a clean target-weight split. "
                "Once you own things, **Smart mode** will tilt new money toward "
                "whatever is lagging.")

    st.divider()
    st.markdown("##### 🔎 Stock scanner — Codex-081 quantitative screen")
    st.caption("Nifty 500 quality/growth screen + Smallcap-250 satellite experiment · "
               "**advisory only, two-stage**: this screen shortlists → ask Claude for "
               "the judgment pass (news · governance · thesis) before any buy.")
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
    c2.metric("Growth Sleeve", fmt_inr(net_worth_growth))
    c3.metric("🛡 Safety Floor", fmt_inr(safety_value),
              help="EPF (both of you) + post office + all 7 NSCs (incl. parents') — "
                   "guaranteed but LOCKED: pledge-able, not spendable, NOT an emergency "
                   "fund. EPF also feeds the FI projection's PF engine automatically.")

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

    st.markdown("##### Allocation — current vs target (growth sleeve)")
    st.caption(f"🛡 Locked-in base (EPF, post office, all NSCs): {fmt_inr(safety_value)}  ·  "
               f"⏳ Transit, redeploying Jul–Aug 26 (chits, small FDs): {fmt_inr(transit_value)}  ·  "
               f"Household total: {fmt_inr(total_net_worth)}")
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
                annotations=[dict(text=fmt_inr(net_worth_growth), x=0.5, y=0.5,
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
        cur_corpus = b1.number_input("Current growth corpus (₹)", 0.0,
                                     value=s_float("current_corpus", 0) or net_worth_growth,
                                     step=50000.0,
                                     help="Defaults to your priced growth sleeve")
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
        pf_cur = d1.number_input("PF current (₹)", 0.0, value=s_float("pf_current", 0), step=50000.0,
                                 help="Leave 0 if EPF is already a holdings row (Debt / Safety) — "
                                      "it auto-feeds from there; setting both double-counts.")
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
                                          "Family (Parents)", "Family (Father)"],
            help="Transit = money with a landing date you'll redeploy (chits, short FDs). "
                 "Debt / Safety / Family (Parents) = locked safety floor (EPF, post "
                 "office, NSCs) outside growth targets. Family (Father) = his MF, "
                 "counted as growth (one-household doctrine).",
        )
        atype = h3.selectbox(
            "Type", ["mf", "stock", "global", "crypto", "manual"],
            help="mf=AMFI NAV · stock=yfinance .NS · global=yfinance USD→INR · "
                 "crypto=CoinGecko · manual=enter price yourself",
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
               "Transit needs redeploying, target sleeves compare current vs target % "
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
            view["Stale"] = view["Stale"].map(lambda v: "⚠" if v else "")
            view["Suggestion"] = _chip
            cols = ["Asset", "Qty", "Buy", "Price", "Value", "Cost",
                    "P/L", "P/L %", "CAGR %", "Suggestion", "Source", "Stale", "Age"]
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
    st.markdown("##### 📋 Suggestions — rule-based, priority-ordered, from live data")
    if net_worth_growth > 0:
        _mc = market_context()
        _tips = []
        # Underweights first, ranked by rupee gap (biggest hole = Priority 1).
        # The gap maths is itself price-responsive: when a sleeve's market falls,
        # its current % shrinks and its gap (priority + ₹) grows automatically.
        _unders, _overs = [], []
        for _, _r in sleeve_df.iterrows():
            _gap = _r["Target %"] - _r["Current %"]
            _amt = _gap / 100.0 * net_worth_growth
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
            _ft = net_worth_growth + transit_value
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
                "Crypto": "**BTC only, spot only.** One coin: Bitcoin is the index of "
                    "this asset class; everything else is higher-beta on top of it. INR "
                    "exchange (CoinDCX spot) or Delta spot — **never futures/leverage "
                    "for the sleeve**. BTC is currently below its 200-day average (trend "
                    "not repaired) — split into 2–3 buys over a few weeks; the 5% cap "
                    "applies regardless of price. Tax truth: 30% flat on gains + 1% TDS, "
                    "**no loss offset** — enter only what you'd hold for years. Log every "
                    "buy here in Holdings (type `crypto`, ticker `BTC`).",
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
    # Emergency fund is funded from cash; milestones tracked against net worth.
    for g in goals:
        if g["kind"] == "emergency":
            current = s_float("emergency_current", 0)
        elif g["kind"] == "travel":
            current = s_float("travel_current", 0)   # earmarked only — not net worth
        else:
            current = total_net_worth
        pct = min(current / g["target_amount"] * 100.0, 100.0) if g["target_amount"] else 0.0
        st.markdown(f"**{g['name']}** — {fmt_inr(current)} / {fmt_inr(g['target_amount'])} "
                    f"({pct:.0f}%)")
        st.progress(min(pct / 100.0, 1.0))

    with st.expander("Edit goals / emergency fund balance"):
        ef = st.number_input("Emergency fund current balance (₹)", min_value=0.0,
                             value=s_float("emergency_current", 0), step=10000.0)
        if st.button("Save emergency balance"):
            db.set_setting("emergency_current", ef)
            st.rerun()
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
    if net_worth_growth <= 0:
        st.info("Add priced holdings to generate rebalance signals.")
    else:
        signals = []
        for _, r in sleeve_df.iterrows():
            dev = r["Deviation"]
            target_val = r["Target %"] / 100.0 * net_worth_growth
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
