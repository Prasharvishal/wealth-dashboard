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

from datetime import datetime

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
    "Direct Stocks": "#d4af37",
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

# Soft, modern CSS polish — rounded cards, breathing room, mobile friendly.
st.markdown(
    """
    <style>
      .block-container {padding-top: 2.2rem; padding-bottom: 3rem; max-width: 1320px;}
      h1, h2, h3, h4, h5 {letter-spacing: -0.01em;}
      /* KPI / metric cards: soft panels with a little lift */
      div[data-testid="stMetric"] {
        background: linear-gradient(180deg,#1c232e 0%, #171d26 100%);
        border: 1px solid #232c38; border-radius: 14px;
        padding: 16px 18px; box-shadow: 0 1px 2px rgba(0,0,0,0.25);
      }
      [data-testid="stMetricValue"] {font-size: 1.7rem; font-weight: 600;}
      [data-testid="stMetricLabel"] {color: #9aa7b4; font-weight: 500;}
      /* Tabs: pill-style, easier to scan */
      .stTabs [data-baseweb="tab-list"] {gap: 6px; flex-wrap: wrap; border-bottom: none;}
      .stTabs [data-baseweb="tab"] {
        background:#1a2029; border-radius: 10px; padding: 8px 14px;
        font-weight: 500;
      }
      .stTabs [aria-selected="true"] {background:#2a3340 !important; color:#e0b84c !important;}
      /* Buttons: rounder, friendlier */
      .stButton button, .stForm button {border-radius: 10px; font-weight: 600;}
      /* Inputs: softer corners */
      input, .stNumberInput, .stTextInput, .stSelectbox {border-radius: 10px;}
      .stale-flag {color:#f0a868; font-size:0.78rem;}
      .step-pill {display:inline-block; background:#2a3340; color:#e0b84c;
        border-radius:999px; padding:2px 12px; font-size:0.8rem; font-weight:600;
        margin-bottom:6px;}
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
        })
    df = pd.DataFrame(rows)
    return df, any_stale


def sleeve_breakdown(df):
    """Current value per sleeve + target % comparison. Returns DataFrame."""
    targets = dict(db.get_target_allocation())
    # Growth/investable total = target sleeves only; Debt/Safety etc. sit outside.
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

# Build FI inputs from persisted settings; current corpus = priced growth sleeve
# unless the user has set an explicit current_corpus override (>0).
fi_inp = FIInputs(
    current_age=s_int("age_self", 30),
    retirement_age=s_int("retirement_age", 50),
    current_corpus=(s_float("current_corpus", 0) or net_worth_growth),
    monthly_contribution=s_float("monthly_contribution", 100000),
    contribution_stepup_pct=s_float("contribution_stepup_pct", 8.0),
    pf_current=s_float("pf_current", 0),
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
safety_value = (holdings_df.loc[~holdings_df["Sleeve"].isin(_tgt_sleeves), "Value"].sum()
                if not holdings_df.empty else 0.0)
total_net_worth = net_worth_growth + safety_value + fi_inp.pf_current + fi_inp.nps_current

st.markdown(f"#### 💰 MAVI Vault  ·  *{st.session_state.get('user','')}*")
if any_stale:
    st.markdown(
        "<span class='stale-flag'>⚠ Some prices are stale (last-fetch fallback in use). "
        "Pull-to-refresh / reload to retry.</span>",
        unsafe_allow_html=True,
    )

k1, k2, k3, k4 = st.columns(4)
k1.metric("Net Worth", fmt_inr(total_net_worth),
          help="Priced growth sleeve + PF + NPS")
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
    st.caption("Type what you have to invest this month — get the exact rupee "
               "split per sleeve. This is your monthly action list.")

    dc1, dc2 = st.columns([1, 1.4])
    with dc1:
        amt = st.number_input("Amount to invest this month (₹)", min_value=0.0,
                              value=float(investable or 100000), step=10000.0)
    with dc2:
        mode_label = st.radio(
            "How to split it",
            ["Smart — rebalance-aware (recommended)", "Simple — pure target %"],
            help="Smart steers new money into whichever sleeves are below target, "
                 "so you rebalance without selling. With no holdings yet, both "
                 "give the same answer.",
        )
    mode = "rebalance" if mode_label.startswith("Smart") else "simple"

    plan = deploy_plan(amt, sleeve_df, net_worth_growth, mode)
    tgt_map = dict(db.get_target_allocation())

    plan_rows = [{
        "Sleeve": s,
        "Target %": f"{tgt_map.get(s, 0):.0f}%",
        "Invest now": fmt_full_inr(a),
        "What to buy": INSTRUMENT_HINTS.get(s, ""),
    } for s, a in plan]
    st.dataframe(pd.DataFrame(plan_rows), use_container_width=True, hide_index=True)

    total_alloc = sum(a for _, a in plan)
    st.caption(f"Total deployed: **{fmt_full_inr(total_alloc)}**  ·  "
               f"PF ₹{s_float('pf_monthly', 37500):,.0f} + "
               f"NPS ₹{s_float('nps_monthly', 10000):,.0f} go in separately.")

    # Visual: horizontal bar of the rupee split.
    if amt > 0:
        pf_fig = go.Figure(go.Bar(
            x=[a for _, a in plan],
            y=[s for s, _ in plan],
            orientation="h",
            marker_color=[SLEEVE_COLORS.get(s, MUTED) for s, _ in plan],
            text=[fmt_full_inr(a) for _, a in plan],
            textposition="auto",
        ))
        pf_fig.update_layout(
            template="plotly_dark", height=320,
            margin=dict(t=10, b=10, l=10, r=10),
            xaxis_title="₹ this month", yaxis=dict(autorange="reversed"),
        )
        st.plotly_chart(pf_fig, use_container_width=True)

    if mode == "rebalance" and net_worth_growth > 0:
        st.info("ℹ️ **Smart mode** is funneling more into your under-weight "
                "sleeves first, so your portfolio drifts back toward target "
                "without you selling anything.")
    elif net_worth_growth <= 0:
        st.info("ℹ️ No holdings yet, so this is a clean target-weight split. "
                "Once you own things, **Smart mode** will tilt new money toward "
                "whatever is lagging.")


# ===========================================================================
# TAB 1 — NET WORTH & ALLOCATION
# ===========================================================================
with tab1:
    c1, c2, c3 = st.columns(3)
    c1.metric("Total Net Worth", fmt_inr(total_net_worth))
    c2.metric("Growth Sleeve", fmt_inr(net_worth_growth))
    c3.metric("Debt Layer (PF+NPS)", fmt_inr(fi_inp.pf_current + fi_inp.nps_current))

    st.markdown("##### Allocation — current vs target (growth sleeve)")
    st.caption(f"🛡 Safety floor outside these targets (FDs, EPF, chits, post office): "
               f"{fmt_inr(safety_value)}  ·  Household total: {fmt_inr(total_net_worth)}")
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
        pf_cur = d1.number_input("PF current (₹)", 0.0, value=s_float("pf_current", 0), step=50000.0)
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
        sleeve = h2.selectbox("Sleeve", db.SLEEVES)
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
        h7, h8 = st.columns(2)
        buy_date = h7.date_input("Buy date", value=datetime.now())
        manual_price = h8.number_input("Manual price override (₹, 0 = auto)",
                                       min_value=0.0, step=1.0)
        if st.form_submit_button("Add holding", use_container_width=True):
            if name and qty > 0:
                db.add_holding(
                    name, sleeve, atype, ticker.strip(), qty, buy_price,
                    str(buy_date), manual_price if manual_price > 0 else None,
                )
                st.success(f"Added {name}.")
                st.rerun()
            else:
                st.error("Need at least an asset name and quantity > 0.")

    st.markdown("##### Holdings — auto-priced, grouped by sleeve")
    if holdings_df.empty:
        st.info("No holdings yet.")
    else:
        for sleeve in db.SLEEVES:
            sdf = holdings_df[holdings_df["Sleeve"] == sleeve]
            if sdf.empty:
                continue
            sval = sdf["Value"].sum()
            st.markdown(f"**{sleeve}** — {fmt_inr(sval)}")
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
            st.dataframe(
                view[["Asset", "Qty", "Buy", "Price", "Value", "Cost",
                      "P/L", "P/L %", "CAGR %", "Source", "Stale", "Age"]],
                use_container_width=True, hide_index=True,
            )

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
    st.markdown("##### 📋 Suggestions — rule-based, from live data")
    if net_worth_growth > 0:
        _tips = []
        for _, _r in sleeve_df.iterrows():
            _gap = _r["Target %"] - _r["Current %"]
            _amt = _gap / 100.0 * net_worth_growth
            if _gap >= 5:
                _tips.append(f"**{_r['Sleeve']}** underweight ({_r['Current %']:.0f}% vs "
                             f"{_r['Target %']:.0f}% target) → route the next "
                             f"{fmt_inr(_amt)} of fresh money here first.")
            elif _gap <= -5:
                _x = (" Follow the monthly Direct-Stocks review — no ad-hoc sells."
                      if _r["Sleeve"] == "Direct Stocks" else
                      " No new money; let growth elsewhere dilute it.")
                _tips.append(f"**{_r['Sleeve']}** overweight ({_r['Current %']:.0f}% vs "
                             f"{_r['Target %']:.0f}%).{_x}")
        if safety_value > 0:
            _tips.append(f"**Safety floor** {fmt_inr(safety_value)} sits outside growth "
                         f"targets (by design). Any upcoming liquidity (chit exits, FD "
                         f"maturities) should get a written destination *before* it lands.")
        _tips.append("_Rules: cash-flow rebalancing (new money → underweight sleeves; "
                     "selling = last resort) · 5% deviation band · Direct Stocks governed "
                     "by the monthly rulebook review._")
        for _t in _tips:
            st.markdown(f"- {_t}")
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
