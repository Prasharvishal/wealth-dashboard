"""
Storage layer for the Household Wealth Dashboard — durable across restarts.

Two interchangeable backends, chosen automatically at runtime:

  * **Turso (libSQL)** over HTTP — used when TURSO_DATABASE_URL (+ token) is
    present in the environment or st.secrets. This is a hosted SQLite database,
    so data survives Streamlit Community Cloud's ephemeral filesystem. We use
    the pure-HTTP `libsql_client` (NOT an embedded replica), which is the right
    fit for a serverless host: every query is a stateless request to the cloud
    primary, so there's no local replica file and no blocking background sync.

  * **Local SQLite** — the zero-config default when no Turso credentials are
    set (i.e. running on your own Mac). Identical SQL, a plain `wealth.db` file.

Robustness: the Turso connection is attempted ONCE behind a hard timeout in a
worker thread. If it can't establish within the timeout (or errors), we fall
back to local SQLite and record the reason in `turso_error()` — the app shows a
warning but NEVER hangs at startup. (An earlier embedded-replica approach hung
on Streamlit Cloud during its startup sync; this design makes that impossible.)

Row access is backend-neutral: reads return plain dicts (built from column
names), so the public API (list[dict] / dict / scalars) is identical no matter
which backend is live.
"""

import os
import sqlite3
import threading
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wealth.db")

# The locked target allocation (growth sleeve). Seeded on first launch.
TARGET_ALLOCATION = [
    ("Nifty 50 (core)", 25.0),
    ("Nifty Next 50", 10.0),
    ("Direct Stocks", 20.0),
    ("Global / US", 20.0),
    ("Gold", 5.0),
    ("Crypto", 5.0),
    ("Tactical / thematic", 15.0),
]
SLEEVES = [s for s, _ in TARGET_ALLOCATION]

DEFAULT_GOALS = [
    ("Emergency Fund", 1_200_000.0, "emergency"),
    ("₹50 Lakh", 5_000_000.0, "milestone"),
    ("₹1 Crore", 10_000_000.0, "milestone"),
    ("₹5 Crore", 50_000_000.0, "milestone"),
    ("₹10 Crore", 100_000_000.0, "milestone"),
    ("Travel Fund", 500_000.0, "travel"),
]

DEFAULT_SETTINGS = {
    "age_self": "30", "age_spouse": "28", "retirement_age": "50",
    "inflation_pct": "6.0", "swr_pct": "3.5",
    "monthly_contribution": "100000", "contribution_stepup_pct": "8.0",
    "pf_monthly": "37500", "pf_stepup_pct": "5.0", "pf_return_pct": "7.5",
    "nps_monthly": "10000", "nps_stepup_pct": "10.0", "nps_return_pct": "9.0",
    "current_corpus": "0", "pf_current": "0", "nps_current": "0",
    "expense_now_monthly": "60000", "expense_target_monthly": "90000",
    "expense_target_year": "8",
}

# ---------------------------------------------------------------------------
# Backend selection + connection management
# ---------------------------------------------------------------------------
_LOCK = threading.Lock()            # serialise access (2-user app)
_BACKEND = None                     # 'turso' | 'sqlite' — resolved once
_TURSO_CLIENT = None                # cached libsql_client HTTP client
_TURSO_ERROR = None                 # human-readable reason Turso fell back
_TURSO_CONNECT_TIMEOUT = 10         # seconds — hard cap so startup can't hang


def _turso_creds():
    """Read Turso URL + token from env first, then st.secrets (Cloud)."""
    url = os.environ.get("TURSO_DATABASE_URL")
    token = os.environ.get("TURSO_AUTH_TOKEN")
    if not url:
        try:
            import streamlit as st  # only available inside the app
            url = url or st.secrets.get("TURSO_DATABASE_URL")
            token = token or st.secrets.get("TURSO_AUTH_TOKEN")
        except Exception:
            pass
    return url, token


def _http_url(url):
    """libsql_client wants an http(s)/ws(s) URL; map the libsql:// scheme."""
    if url.startswith("libsql://"):
        return "https://" + url[len("libsql://"):]
    return url


def _try_connect_turso():
    """Create the HTTP client and force one real round-trip. Raises on failure."""
    global _TURSO_CLIENT
    import libsql_client
    url, token = _turso_creds()
    client = libsql_client.create_client_sync(url=_http_url(url), auth_token=token)
    client.execute("SELECT 1")  # prove the connection actually works
    _TURSO_CLIENT = client
    return client


def _resolve_backend():
    """Decide once whether we're on Turso or local SQLite.

    The Turso attempt runs in a worker thread with a hard timeout: if it can't
    connect in time it's abandoned and we fall back to local SQLite, so the
    startup path can never block on a slow/hung network call.
    """
    global _BACKEND, _TURSO_ERROR
    if _BACKEND is not None:
        return _BACKEND
    url, _ = _turso_creds()
    if not url:
        _BACKEND = "sqlite"
        return _BACKEND

    # Attempt the connect in a DAEMON thread bounded by a join timeout. A daemon
    # thread can never delay process shutdown, and join() returning early means
    # a slow/hung connect simply yields a local-SQLite fallback instead of a hang.
    result = {}

    def _worker():
        try:
            _try_connect_turso()
            result["ok"] = True
        except Exception as e:
            result["err"] = f"{type(e).__name__}: {e}"[:300]

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(_TURSO_CONNECT_TIMEOUT)

    if result.get("ok"):
        _BACKEND = "turso"
    else:
        _TURSO_ERROR = result.get("err") or f"connect timed out after {_TURSO_CONNECT_TIMEOUT}s"
        _BACKEND = "sqlite"
    return _BACKEND


def _get_sqlite():
    """A fresh local SQLite connection (caller closes it)."""
    return sqlite3.connect(DB_PATH, check_same_thread=False)


def backend_name():
    """'turso' or 'sqlite' — for display in the app footer."""
    return _resolve_backend()


def turso_error():
    """If Turso creds were set but we fell back to SQLite, the reason; else None."""
    _resolve_backend()
    return _TURSO_ERROR


def pull():
    """No-op. Kept for API compatibility — the HTTP client is always fresh
    (every read hits the cloud primary directly, so there's nothing to sync)."""
    return None


# ---------------------------------------------------------------------------
# Backend-neutral query helpers (return plain dict rows from either backend)
# ---------------------------------------------------------------------------
def query(sql, params=()):
    """Run a SELECT, return list[dict]."""
    with _LOCK:
        if _resolve_backend() == "turso":
            rs = _TURSO_CLIENT.execute(sql, list(params))
            cols = list(rs.columns)
            return [{cols[i]: row[i] for i in range(len(cols))} for row in rs.rows]
        conn = _get_sqlite()
        cur = conn.cursor()
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description] if cur.description else []
        out = [dict(zip(cols, r)) for r in cur.fetchall()]
        conn.close()
        return out


def query_one(sql, params=()):
    rows = query(sql, params)
    return rows[0] if rows else None


def query_scalar(sql, params=()):
    """Run a SELECT returning a single value (e.g. COUNT(*))."""
    with _LOCK:
        if _resolve_backend() == "turso":
            rs = _TURSO_CLIENT.execute(sql, list(params))
            return rs.rows[0][0] if rs.rows else None
        conn = _get_sqlite()
        cur = conn.cursor()
        cur.execute(sql, params)
        row = cur.fetchone()
        conn.close()
        return row[0] if row else None


def execute(sql, params=()):
    """Run a single write (INSERT/UPDATE/DELETE/DDL) and commit."""
    with _LOCK:
        if _resolve_backend() == "turso":
            _TURSO_CLIENT.execute(sql, list(params))  # HTTP autocommits
            return
        conn = _get_sqlite()
        cur = conn.cursor()
        cur.execute(sql, params)
        conn.commit()
        conn.close()


def executemany(sql, seq):
    """Run a write across many parameter tuples (looped for backend portability)."""
    with _LOCK:
        if _resolve_backend() == "turso":
            for params in seq:
                _TURSO_CLIENT.execute(sql, list(params))
            return
        conn = _get_sqlite()
        cur = conn.cursor()
        for params in seq:
            cur.execute(sql, params)
        conn.commit()
        conn.close()


# ---------------------------------------------------------------------------
# Schema + seed
# ---------------------------------------------------------------------------
def init_db():
    """Create tables if absent and seed locked allocation / defaults once.

    Idempotent and backend-agnostic — runs the same DDL on Turso or local
    SQLite. Safe to call at the top of every Streamlit run.
    """
    execute("""CREATE TABLE IF NOT EXISTS target_allocation (
        sleeve TEXT PRIMARY KEY, target_pct REAL NOT NULL)""")
    execute("""CREATE TABLE IF NOT EXISTS holdings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        asset_name TEXT NOT NULL, sleeve TEXT NOT NULL, asset_type TEXT NOT NULL,
        ticker TEXT, quantity REAL NOT NULL, buy_price REAL NOT NULL,
        buy_date TEXT, manual_price REAL, last_price REAL, last_price_time TEXT)""")
    execute("""CREATE TABLE IF NOT EXISTS cashflow (
        id INTEGER PRIMARY KEY AUTOINCREMENT, month TEXT NOT NULL,
        investable REAL NOT NULL DEFAULT 0, expenses REAL NOT NULL DEFAULT 0,
        pf REAL NOT NULL DEFAULT 0, nps REAL NOT NULL DEFAULT 0, UNIQUE(month))""")
    execute("""CREATE TABLE IF NOT EXISTS goals (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
        target_amount REAL NOT NULL, kind TEXT NOT NULL DEFAULT 'milestone')""")
    execute("""CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY, value TEXT)""")

    if (query_scalar("SELECT COUNT(*) FROM target_allocation") or 0) == 0:
        executemany(
            "INSERT INTO target_allocation (sleeve, target_pct) VALUES (?, ?)",
            TARGET_ALLOCATION)
    if (query_scalar("SELECT COUNT(*) FROM goals") or 0) == 0:
        executemany(
            "INSERT INTO goals (name, target_amount, kind) VALUES (?, ?, ?)",
            DEFAULT_GOALS)
    for k, v in DEFAULT_SETTINGS.items():
        execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
def get_setting(key, default=None):
    row = query_one("SELECT value FROM settings WHERE key = ?", (key,))
    return row["value"] if row else default


def get_settings():
    return {r["key"]: r["value"] for r in query("SELECT key, value FROM settings")}


def set_setting(key, value):
    execute("INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)))


def set_settings(mapping):
    for key, value in mapping.items():
        set_setting(key, value)


# ---------------------------------------------------------------------------
# Target allocation
# ---------------------------------------------------------------------------
def get_target_allocation():
    rows = query("SELECT sleeve, target_pct FROM target_allocation")
    by_sleeve = {r["sleeve"]: r["target_pct"] for r in rows}
    return [(s, by_sleeve.get(s, 0.0)) for s in SLEEVES]


# ---------------------------------------------------------------------------
# Holdings
# ---------------------------------------------------------------------------
def get_holdings():
    return query("SELECT * FROM holdings ORDER BY sleeve, asset_name")


def add_holding(asset_name, sleeve, asset_type, ticker, quantity, buy_price,
                buy_date, manual_price=None):
    execute("""INSERT INTO holdings
        (asset_name, sleeve, asset_type, ticker, quantity, buy_price, buy_date, manual_price)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (asset_name, sleeve, asset_type, ticker, quantity, buy_price,
             buy_date, manual_price))


def update_holding(holding_id, **fields):
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    execute(f"UPDATE holdings SET {cols} WHERE id = ?",
            list(fields.values()) + [holding_id])


def delete_holding(holding_id):
    execute("DELETE FROM holdings WHERE id = ?", (holding_id,))


def cache_price(holding_id, price):
    """Store the last successfully used price + timestamp for stale fallback."""
    execute("UPDATE holdings SET last_price = ?, last_price_time = ? WHERE id = ?",
            (price, datetime.now().isoformat(timespec="seconds"), holding_id))


# ---------------------------------------------------------------------------
# Cash flow
# ---------------------------------------------------------------------------
def get_cashflow():
    return query("SELECT * FROM cashflow ORDER BY month")


def upsert_cashflow(month, investable, expenses, pf, nps):
    execute("""INSERT INTO cashflow (month, investable, expenses, pf, nps)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(month) DO UPDATE SET
          investable = excluded.investable, expenses = excluded.expenses,
          pf = excluded.pf, nps = excluded.nps""",
            (month, investable, expenses, pf, nps))


def delete_cashflow(month):
    execute("DELETE FROM cashflow WHERE month = ?", (month,))


# ---------------------------------------------------------------------------
# Goals
# ---------------------------------------------------------------------------
def get_goals():
    return query("SELECT * FROM goals ORDER BY target_amount")


def upsert_goal(name, target_amount, kind, goal_id=None):
    if goal_id:
        execute("UPDATE goals SET name = ?, target_amount = ?, kind = ? WHERE id = ?",
                (name, target_amount, kind, goal_id))
    else:
        execute("INSERT INTO goals (name, target_amount, kind) VALUES (?, ?, ?)",
                (name, target_amount, kind))


def delete_goal(goal_id):
    execute("DELETE FROM goals WHERE id = ?", (goal_id,))
