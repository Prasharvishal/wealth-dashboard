"""
Storage layer for the Household Wealth Dashboard — durable across restarts.

Two interchangeable backends, chosen automatically at runtime:

  * **Turso (libSQL)** — used when TURSO_DATABASE_URL (+ TURSO_AUTH_TOKEN) is
    present in the environment or st.secrets. This is a hosted SQLite database,
    so the data survives Streamlit Community Cloud's ephemeral filesystem. We
    connect as an *embedded replica*: a small local file is kept in sync with
    the remote primary, giving fast local reads while every write is durably
    forwarded to the cloud.

  * **Local SQLite** — the zero-config default when no Turso credentials are
    set (i.e. running on your own Mac). Identical SQL, a plain `wealth.db` file.

Because libSQL speaks the SQLite dialect, the schema and every query below are
shared verbatim between the two backends. The only backend-specific bits are
how a connection is opened (see `_get_conn`) and an optional `pull()` that
refreshes the local replica from the cloud.

Row access is backend-neutral: we never rely on `sqlite3.Row`; instead every
read goes through `_rows()` which zips `cursor.description` with the tuples to
produce plain dicts. That keeps the public API (list[dict] / dict / scalars)
identical no matter which backend is live.
"""

import os
import sqlite3
import threading
from datetime import datetime

# Quiet the libSQL Rust layer's connection-retry logging — failures are already
# surfaced cleanly in-app via turso_error(). Set before any libsql import.
os.environ.setdefault("RUST_LOG", "off")

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wealth.db")
# Local replica file used only in Turso mode (synced with the cloud primary).
REPLICA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wealth_replica.db")

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
_LOCK = threading.Lock()        # serialise access (2-user app; cheap insurance)
_BACKEND = None                 # 'turso' | 'sqlite' — resolved once, lazily
_TURSO_CONN = None              # cached embedded-replica connection (Turso only)
_TURSO_ERROR = None             # human-readable reason Turso fell back, if any


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


def _resolve_backend():
    """Decide once whether we're on Turso or local SQLite."""
    global _BACKEND, _TURSO_ERROR
    if _BACKEND is not None:
        return _BACKEND
    url, _ = _turso_creds()
    if not url:
        _BACKEND = "sqlite"
        return _BACKEND
    # Turso creds present — try to stand up an embedded-replica connection.
    try:
        _open_turso()
        _BACKEND = "turso"
    except Exception as e:
        # Never crash: degrade to local SQLite and remember why.
        _TURSO_ERROR = f"{type(e).__name__}: {e}"
        _BACKEND = "sqlite"
    return _BACKEND


def _open_turso():
    """Open (and cache) the embedded-replica libSQL connection.

    Embedded replica = a local file (REPLICA_PATH) kept in sync with the remote
    Turso primary. Reads are served locally; writes are forwarded to the cloud
    so they persist across restarts. We sync() once on open to pull the latest.
    """
    global _TURSO_CONN
    if _TURSO_CONN is not None:
        return _TURSO_CONN
    url, token = _turso_creds()
    import libsql_experimental as libsql  # Rust-backed; wheels on Linux+macOS
    conn = libsql.connect(REPLICA_PATH, sync_url=url, auth_token=token)
    conn.sync()  # pull latest from the cloud primary into the local replica
    _TURSO_CONN = conn
    return conn


def _get_conn():
    """Return a live connection for the active backend.

    Local SQLite: a fresh short-lived connection per call (cheap; closed by the
    caller helpers). Turso: the cached embedded-replica connection.
    """
    if _resolve_backend() == "turso":
        return _open_turso()
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    return conn


def backend_name():
    """'turso' or 'sqlite' — for display in the app footer."""
    return _resolve_backend()


def turso_error():
    """If Turso creds were set but we fell back to SQLite, the reason; else None."""
    _resolve_backend()
    return _TURSO_ERROR


def pull():
    """Refresh the local replica from the cloud (no-op on local SQLite).

    Call once at the top of each Streamlit run so reads reflect writes made by
    the other user on a different device.
    """
    if _resolve_backend() == "turso":
        with _LOCK:
            try:
                _open_turso().sync()
            except Exception:
                pass  # transient network hiccup — serve last-synced data


# ---------------------------------------------------------------------------
# Backend-neutral query helpers (manual dict rows — no sqlite3.Row dependency)
# ---------------------------------------------------------------------------
def _rows(cur):
    cols = [d[0] for d in cur.description] if cur.description else []
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def query(sql, params=()):
    """Run a SELECT, return list[dict]."""
    with _LOCK:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute(sql, params)
        out = _rows(cur)
        if _BACKEND == "sqlite":
            conn.close()
        return out


def query_one(sql, params=()):
    rows = query(sql, params)
    return rows[0] if rows else None


def query_scalar(sql, params=()):
    """Run a SELECT returning a single value (e.g. COUNT(*))."""
    with _LOCK:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute(sql, params)
        row = cur.fetchone()
        if _BACKEND == "sqlite":
            conn.close()
        return row[0] if row else None


def execute(sql, params=()):
    """Run a single write (INSERT/UPDATE/DELETE/DDL) and commit."""
    with _LOCK:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute(sql, params)
        conn.commit()
        if _BACKEND == "sqlite":
            conn.close()


def executemany(sql, seq):
    """Run a write across many parameter tuples. Implemented as a loop so it is
    portable across both backends regardless of cursor.executemany support."""
    with _LOCK:
        conn = _get_conn()
        cur = conn.cursor()
        for params in seq:
            cur.execute(sql, params)
        conn.commit()
        if _BACKEND == "sqlite":
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
