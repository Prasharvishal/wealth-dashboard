# 💰 Household Wealth Dashboard — FIRE tracker

A dark, terminal-styled Streamlit dashboard for a two-person household pursuing
FIRE. Live prices auto-fetch (with manual override and graceful stale-fallback);
all inputs persist in a local SQLite file. Two users share one instance behind a
shared passcode.

## What it does

| Tab | Shows |
|---|---|
| **Start Here** | 4-step guided walkthrough for the first month (amount → split → record buys → summary) |
| **Net Worth & Allocation** | Total net worth, current-vs-target donut + bar, per-sleeve deviation |
| **Targets** | Guided sitting on the locked target allocation — current standing, propose a new split, save only via typed CONFIRM, change history |
| **Deploy This Month** | The full waterfall: emergency buffer → sleeve gaps → scanner stock + optional smallcap satellite |
| **Scanner** | Nifty 500 quality/growth screen + Smallcap-250 satellite shortlist (advisory, two-stage) |
| **Cash Flow & Savings** | Log monthly investable + expenses, savings-rate %, history chart |
| **FI Projection** | 3 return scenarios (8/10/12%), **real + nominal** corpus, projectable expenses, "FI reached at age X", PF/NPS projected separately |
| **Holdings** | Auto-priced table (value, cost, P/L %, CAGR), grouped by sleeve |
| **Goals & Rebalance** | Progress bars (emergency fund [live from holdings], ₹50L/1Cr/5Cr/10Cr, travel) + buy/hold/trim rebalance signal |

### Data sources (all free, no keys)
- **Indian mutual funds** → [AMFI NAVAll.txt](https://www.amfiindia.com/spages/NAVAll.txt), parsed by scheme code.
- **Stocks / indices / global ETFs** → `yfinance` (Indian tickers need `.NS`/`.BO`; global tickers auto-converted USD→INR).
- **Crypto** → CoinGecko free `simple/price` API. Dormant since the 2026-07-21
  targets sitting retired the Vault's Crypto sleeve — the fetcher stays in
  `prices.py` (harmless, unreachable from the UI) but no add-form offers it.

All fetches are cached 1 hour (`@st.cache_data(ttl=3600)`). If a fetch fails, the
last-stored price is shown with a **⚠ stale** flag — the app never crashes.

### Storage
Everything you type (holdings, monthly cash flow, goals, FI settings, the locked
allocation) lives in `wealth.db` (SQLite), created and seeded on first launch.

---

## Run locally

```bash
cd wealth_dashboard
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

Open http://localhost:8501. Default passcode: **`fire2026`** (override on deploy — see below).

### Asset-type cheat-sheet (Holdings tab)
| Type | Ticker field | Example |
|---|---|---|
| `mf` | AMFI scheme code | `122639` |
| `stock` | yfinance ticker (.NS/.BO) | `RELIANCE.NS` |
| `global` | yfinance ticker (USD) | `VOO`, `QQQM` |
| `manual` | — (set Manual price) | gold bars, ESOPs, FDs |

`crypto` (CoinGecko) is not offered in the Holdings add-form — the Vault's
Crypto sleeve was retired at the 2026-07-21 targets sitting. Crypto exposure
now lives only via the separate Sentinel probation charter, never this app.

---

## Deploy to Streamlit Community Cloud (free)

1. **Push to GitHub.** Create a public or private repo containing `app.py`,
   `db.py`, `prices.py`, `fi_engine.py`, `requirements.txt`, and
   `.streamlit/config.toml`. (Do **not** commit `wealth.db` if you want a clean
   start; add it to `.gitignore`.)

   ```bash
   git init && git add app.py db.py prices.py fi_engine.py requirements.txt .streamlit/config.toml README.md
   echo "wealth.db" > .gitignore && git add .gitignore
   git commit -m "Household wealth dashboard"
   git branch -M main
   git remote add origin https://github.com/<you>/<repo>.git
   git push -u origin main
   ```

2. **Go to [share.streamlit.io](https://share.streamlit.io)** → *New app* → pick
   your repo, branch `main`, main file `app.py` → **Deploy**.

3. **Set the shared passcode.** In the app's *Settings → Secrets*, add:

   ```toml
   app_password = "your-household-passcode"
   ```

   Without this, the app uses the local default `fire2026`. Both users log in
   with this one passcode and reach the same instance.

4. **Share the URL** (`https://<your-app>.streamlit.app`) with your partner. It's
   mobile-friendly — both of you can check it from your phones.

### 💾 Storage: durable cloud persistence with Turso (recommended for Cloud)

The app uses a **dual-backend** storage layer (`db.py`), chosen automatically:

| When | Backend | Persistence |
|---|---|---|
| No Turso credentials set | **local SQLite** (`wealth.db`) | persists on your own machine only |
| `TURSO_DATABASE_URL` present (env or `st.secrets`) | **Turso (libSQL cloud)** | **permanent** — survives Cloud restarts/redeploys |

Streamlit Community Cloud's filesystem is **ephemeral**, so for a deployed app you
want Turso. It's free (generous tier), SQLite-compatible (zero SQL changes), and
takes ~3 minutes to set up:

**1. Create a free Turso database** (one-time):
```bash
# install the Turso CLI (macOS)
brew install tursodatabase/tap/turso
turso auth signup                       # opens browser; free account
turso db create wealth-dashboard        # create the database
turso db show wealth-dashboard --url    # -> copy the libsql://... URL
turso db tokens create wealth-dashboard # -> copy the auth token
```
*(No Homebrew? Use `curl -sSfL https://get.tur.so/install.sh | bash`.)*

**2. Give the app the credentials.**
- **On Streamlit Cloud** → app **Settings → Secrets**, add (alongside `app_password`):
  ```toml
  app_password = "your-household-passcode"
  TURSO_DATABASE_URL = "libsql://wealth-dashboard-<you>.turso.io"
  TURSO_AUTH_TOKEN = "<the-token-from-step-1>"
  ```
- **To test Turso locally**, either put the same two keys in
  `.streamlit/secrets.toml` (git-ignored) **or** export them as env vars before
  `streamlit run app.py`:
  ```bash
  export TURSO_DATABASE_URL="libsql://...turso.io"
  export TURSO_AUTH_TOKEN="..."
  streamlit run app.py
  ```

**3. Confirm it's live.** The footer at the bottom of the app shows the active
store: **`☁️ Turso (cloud — permanent)`** when connected, or
**`💾 local SQLite`** otherwise. If credentials are set but unreachable, the app
shows a yellow warning and safely falls back to local SQLite (it never crashes).

Both of you now read/write the **same cloud database** from your phones — changes
by one show up for the other on next load. (Tables auto-create + seed on first
connect, exactly like the local file.)

> No Turso? The app still works on Community Cloud — just know `wealth.db` resets
> when the container restarts. Turso removes that limitation.

---

## File map
```
app.py            # UI: auth gate, theme, 9 tabs, KPI row, charts
db.py             # SQLite layer; seeds locked allocation + defaults on first run
prices.py         # AMFI / yfinance / CoinGecko fetchers + stale fallback
fi_engine.py      # deterministic FI projection (real + nominal, 3 scenarios)
requirements.txt
.streamlit/config.toml   # dark gold/teal/blue terminal theme
wealth.db         # auto-created SQLite store (git-ignored)
```
