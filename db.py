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
# Re-locked at user targets sitting 2026-07-21 (audit: app_state
# targets_history); crypto exposure only via the Sentinel probation charter,
# never the Vault.
TARGET_ALLOCATION = [
    ("Nifty 50 (core)", 30.0),
    ("Direct Stocks", 20.0),
    ("Global / US", 20.0),
    ("Tactical / thematic", 15.0),
    ("Nifty Next 50", 10.0),
    ("Gold", 5.0),
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
#
# SQLite paths close their connection in try/finally (fix 2026-07-20): the
# old shape ran `conn.close()` after the statement, so a FAILING statement
# (e.g. a NOT NULL violation on liabilities.principal) raised past the close
# and leaked the connection with an open transaction — every later local
# write in the same process then died "database is locked". Turso paths are
# untouched (stateless HTTP, no connection to leak).
# ---------------------------------------------------------------------------
def query(sql, params=()):
    """Run a SELECT, return list[dict]."""
    with _LOCK:
        if _resolve_backend() == "turso":
            rs = _TURSO_CLIENT.execute(sql, list(params))
            cols = list(rs.columns)
            return [{cols[i]: row[i] for i in range(len(cols))} for row in rs.rows]
        conn = _get_sqlite()
        try:
            cur = conn.cursor()
            cur.execute(sql, params)
            cols = [d[0] for d in cur.description] if cur.description else []
            return [dict(zip(cols, r)) for r in cur.fetchall()]
        finally:
            conn.close()


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
        try:
            cur = conn.cursor()
            cur.execute(sql, params)
            row = cur.fetchone()
            return row[0] if row else None
        finally:
            conn.close()


def execute(sql, params=()):
    """Run a single write (INSERT/UPDATE/DELETE/DDL) and commit."""
    with _LOCK:
        if _resolve_backend() == "turso":
            _TURSO_CLIENT.execute(sql, list(params))  # HTTP autocommits
            return
        conn = _get_sqlite()
        try:
            cur = conn.cursor()
            cur.execute(sql, params)
            conn.commit()
        finally:
            conn.close()


def executemany(sql, seq):
    """Run a write across many parameter tuples (looped for backend portability)."""
    with _LOCK:
        if _resolve_backend() == "turso":
            for params in seq:
                _TURSO_CLIENT.execute(sql, list(params))
            return
        conn = _get_sqlite()
        try:
            cur = conn.cursor()
            for params in seq:
                cur.execute(sql, params)
            conn.commit()
        finally:
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
        buy_date TEXT, manual_price REAL, last_price REAL, last_price_time TEXT,
        maturity_date TEXT, maturity_provenance TEXT)""")
    for _mig in ("ALTER TABLE holdings ADD COLUMN maturity_date TEXT",
                 "ALTER TABLE holdings ADD COLUMN maturity_provenance TEXT"):
        try:  # migrations for DBs created before these columns existed
            execute(_mig)
        except Exception:
            pass  # column already present
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


def set_target_allocation(sleeve, target_pct):
    """Upsert one sleeve's locked target %. Charter: targets move only by
    explicit user decision (Targets tab CONFIRM gate) — this helper has no
    guard of its own, the caller owns that discipline."""
    execute(
        "INSERT INTO target_allocation (sleeve, target_pct) VALUES (?, ?) "
        "ON CONFLICT(sleeve) DO UPDATE SET target_pct = excluded.target_pct",
        (sleeve, float(target_pct)),
    )


# ---------------------------------------------------------------------------
# Holdings
# ---------------------------------------------------------------------------
def get_holdings():
    return query("SELECT * FROM holdings ORDER BY sleeve, asset_name")


def add_holding(asset_name, sleeve, asset_type, ticker, quantity, buy_price,
                buy_date, manual_price=None, maturity_date=None):
    execute("""INSERT INTO holdings
        (asset_name, sleeve, asset_type, ticker, quantity, buy_price, buy_date,
         manual_price, maturity_date)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (asset_name, sleeve, asset_type, ticker, quantity, buy_price,
             buy_date, manual_price, maturity_date))


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


# ---------------------------------------------------------------------------
# Liabilities (loans/EMIs — cash-flow only, never netted against investments)
# ---------------------------------------------------------------------------
# Schema history: this table was created directly against the live Turso DB
# (dt_system OPEN_ITEMS C10, 2026-07-20) — there is no CREATE TABLE for it in
# this repo's history. A first pass here inferred a 6-column shape from
# dt_system/wealth_pull.py's SELECT projection; that inference was WEAK — a
# SELECT naming 6 columns proves those 6 EXIST, not that others don't.
# Corrected same day from the live DB's sqlite_master DDL (queried live by
# the session coordinator; this repo's env has no Turso credentials to
# re-run it — verbatim DDL below). Note `principal REAL NOT NULL`: an INSERT
# that omits principal does not degrade gracefully, it fails the whole
# statement — which is why add_liability REQUIRES it.
#
#   CREATE TABLE liabilities (id INTEGER PRIMARY KEY AUTOINCREMENT,
#     name TEXT NOT NULL, lender TEXT, principal REAL NOT NULL,
#     outstanding REAL NOT NULL, rate_pct REAL, emi REAL,
#     tenure_months INTEGER, start_date TEXT, provenance TEXT)
LIABILITY_COLS = ("name", "lender", "principal", "outstanding", "rate_pct",
                  "emi", "tenure_months", "start_date", "provenance")


def _ensure_liabilities_table():
    # Mirrors the live DDL above (IF NOT EXISTS -> true no-op against Turso;
    # only a fresh local-SQLite install actually creates anything here).
    execute("""CREATE TABLE IF NOT EXISTS liabilities (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL, lender TEXT, principal REAL NOT NULL,
        outstanding REAL NOT NULL, rate_pct REAL, emi REAL,
        tenure_months INTEGER, start_date TEXT, provenance TEXT)""")
    # Local-only migrations: a wealth.db created by the earlier 6-column
    # version of this DDL needs the 3 newer columns added. (SQLite can't ADD
    # COLUMN with NOT NULL and no default, so principal migrates as plain
    # REAL locally — the live Turso table already carries the real
    # constraint.) Against live Turso every ALTER fails "duplicate column"
    # and is swallowed — same idiom init_db uses for the holdings migrations.
    for _mig in ("ALTER TABLE liabilities ADD COLUMN lender TEXT",
                 "ALTER TABLE liabilities ADD COLUMN principal REAL",
                 "ALTER TABLE liabilities ADD COLUMN start_date TEXT"):
        try:
            execute(_mig)
        except Exception:
            pass  # column already present


def get_liabilities():
    """All loan rows, or [] if the table doesn't exist yet on an older DB."""
    try:
        return query("SELECT * FROM liabilities ORDER BY outstanding DESC")
    except Exception:
        return []


def add_liability(name, outstanding, principal, lender=None, start_date=None,
                  emi=None, rate_pct=None, tenure_months=None,
                  provenance="user-entered"):
    """Insert one loan row — full live column set (see the DDL note above).

    `principal` is REQUIRED: the live column is NOT NULL, so an INSERT that
    omits it doesn't degrade to a partial row, it fails outright. Raise here
    with a clear message rather than letting the constraint error surface."""
    if principal is None:
        raise ValueError("add_liability: principal is required "
                         "(NOT NULL column on the live liabilities table)")
    _ensure_liabilities_table()
    execute(
        "INSERT INTO liabilities (name, lender, principal, outstanding, rate_pct, "
        "emi, tenure_months, start_date, provenance) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (name, lender, float(principal), float(outstanding or 0), rate_pct,
         emi, tenure_months, start_date, provenance),
    )


def update_liability(liability_id, **fields):
    """Update selected columns on one loan row. Accepts any of the 9 live
    columns (LIABILITY_COLS — lender/principal/start_date included, though
    the Vault UI currently only edits outstanding/emi/rate_pct/provenance).
    Drops any key NOT in LIABILITY_COLS so a stray field can never crash the
    write — but raises if that leaves NOTHING to write, rather than silently
    no-op'ing while the caller's UI still reports success. Returns the number
    of rows affected (0 or 1) so the caller can distinguish "updated" from
    "no row with that id"."""
    fields = {k: v for k, v in fields.items() if k in LIABILITY_COLS}
    if not fields:
        raise ValueError(
            "update_liability: no valid fields to write (all keys were "
            f"outside LIABILITY_COLS={LIABILITY_COLS}) — refusing a silent no-op."
        )
    cols = ", ".join(f"{k} = ?" for k in fields)
    execute(f"UPDATE liabilities SET {cols} WHERE id = ?",
            list(fields.values()) + [liability_id])
    row = query_one("SELECT id FROM liabilities WHERE id = ?", (liability_id,))
    return 1 if row else 0


def delete_liability(liability_id):
    execute("DELETE FROM liabilities WHERE id = ?", (liability_id,))


# ---------------------------------------------------------------------------
# App state (Sentinel -> Vault sync channel; key/value JSON blobs)
# ---------------------------------------------------------------------------
def get_app_state(key, strict=False):
    """One app_state row's JSON value, decoded to a dict, or {} if absent.

    strict=False (default, all existing call sites): any failure — table
    missing, transient connection error, bad JSON — is swallowed and treated
    the same as "key absent" ({}). This is the right behaviour for read-only
    display code (a dashboard tile should degrade quietly, not crash).

    strict=True: a genuine query/connection error RAISES instead of being
    conflated with "key absent". Callers that are about to WRITE based on
    "is there existing data under this key" (e.g. append_app_state_list) must
    use strict=True — with strict=False, a transient Turso hiccup looks
    identical to "no history yet", and appending on that false-empty read
    would silently overwrite/lose real existing history.

    "Table doesn't exist yet" is explicitly NOT treated as a strict-mode
    error: it's ensured here first (idempotent CREATE TABLE IF NOT EXISTS,
    matches the live Turso shape) so a fresh local SQLite install — which
    has never had anything written to app_state — reads as a genuine, valid
    "key absent" rather than tripping strict mode's error path on its very
    first read. Only a real query/connection failure after that point raises.
    """
    _ensure_app_state_table()
    try:
        row = query_one("SELECT value, updated FROM app_state WHERE key = ?", (key,))
    except Exception:
        if strict:
            raise
        return {}
    if not row or not row.get("value"):
        return {}
    try:
        import json
        data = json.loads(row["value"])
        if isinstance(data, dict):
            data.setdefault("_updated", row.get("updated"))
        return data
    except Exception:
        if strict:
            raise
        return {}


def _ensure_app_state_table():
    # Table already exists live (Sentinel created it one-time, 2026-07-20 —
    # see dt_system/dashboard_update.py push_app_state). This mirrors that
    # exact shape so it's a no-op against Turso and only helps a fresh local
    # SQLite install.
    execute("""CREATE TABLE IF NOT EXISTS app_state (
        key TEXT PRIMARY KEY, value TEXT, updated TEXT)""")


def set_app_state(key, value):
    """Upsert one app_state row. `value` is any JSON-serialisable object
    (dict/list) — this is the Vault-side counterpart to Sentinel's own
    push_app_state upsert, same ON CONFLICT convention, same 'updated' column.
    Used for Vault-authored blobs (e.g. targets_history) as well as any
    throwaway test key — callers own cleanup for anything named *_test."""
    import json
    _ensure_app_state_table()
    execute(
        "INSERT INTO app_state (key, value, updated) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
        "updated = excluded.updated",
        (key, json.dumps(value), datetime.now().isoformat(timespec="seconds")),
    )


def delete_app_state(key):
    """Remove one app_state row entirely — used to clean up throwaway test keys."""
    try:
        execute("DELETE FROM app_state WHERE key = ?", (key,))
    except Exception:
        pass


_APPEND_LOCK = threading.RLock()  # serialises append_app_state_list's read-modify-write


def append_app_state_list(key, record):
    """Append one record to a JSON-LIST app_state value, creating it if absent.

    Used for audit trails (e.g. "targets_history"): reads the current value,
    appends `record`, writes the whole list back as one upsert. get_app_state
    returns the stored JSON verbatim for non-dict payloads (a stored list
    comes back as a list, untouched) — so the only cases here are "absent"
    ({}  ->  start a new list) and "already a list" (append to it). A key
    that holds a genuine dict blob (e.g. sentinel_manifest) is a caller bug —
    refuse to silently clobber it rather than guess.

    Uses strict=True on the read: a transient query/connection error RAISES
    here instead of being silently treated as "key absent" — the latter
    would let a Turso hiccup masquerade as an empty key and overwrite real
    existing history with a fresh single-entry list. Callers must not catch
    this and retry-as-empty; surface the failure and let the caller decide
    (the Targets tab aborts the whole save on this, see app.py).

    Concurrency: the read-modify-write sequence is wrapped in a dedicated
    `_APPEND_LOCK` (separate from the module's per-statement `_LOCK`, which
    only serialises individual query()/execute() calls, not a multi-call
    sequence). This makes two overlapping calls from the SAME PROCESS safe
    (no lost update between two Streamlit reruns in one server process).
    It is NOT a database-level transaction, so it does not protect against
    two separate processes/machines writing concurrently — acceptable for
    this 2-user household app, which runs as one Streamlit process.
    """
    with _APPEND_LOCK:
        current = get_app_state(key, strict=True)
        if isinstance(current, list):
            history = current
        elif current == {}:
            history = []
        else:
            raise ValueError(
                f"app_state key '{key}' already holds a non-list blob "
                f"({type(current).__name__}); append_app_state_list refuses to "
                "overwrite it."
            )
        history = history + [record]
        set_app_state(key, history)
        return history
