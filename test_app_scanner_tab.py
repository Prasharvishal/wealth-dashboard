"""
AppTest coverage for the Scanner tab's in-app "Scan the market now" button.

Verifies the tab renders with zero exceptions in three states:
  (a) no scan in app_state
  (b) a seeded scan dict (both Mac-sourced and app-sourced shapes)
  (c) the button gated when last_scan_at is recent (<6h)

Uses a THROWAWAY local SQLite file only — TURSO_DATABASE_URL/TURSO_AUTH_TOKEN
are unset before import so db.py resolves to the local-sqlite backend and
never touches live Turso.

Run: .venv/bin/python test_app_scanner_tab.py
"""
import os
import sys

# Must happen BEFORE importing db/app — _resolve_backend() reads env once.
os.environ.pop("TURSO_DATABASE_URL", None)
os.environ.pop("TURSO_AUTH_TOKEN", None)

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(TEST_DIR, "_apptest_scanner.db")
if os.path.exists(DB_FILE):
    os.remove(DB_FILE)

sys.path.insert(0, TEST_DIR)
import db  # noqa: E402
db.DB_PATH = DB_FILE  # redirect the local-sqlite path to a throwaway file

from streamlit.testing.v1 import AppTest  # noqa: E402


def _new_apptest():
    at = AppTest.from_file(os.path.join(TEST_DIR, "app.py"), default_timeout=60)
    at.session_state["authed"] = True
    at.session_state["user"] = "Partner A (30)"
    return at


def test_a_no_scan_in_app_state():
    db.delete_app_state("scanner")
    db.delete_app_state("scanner_last_scan_at")
    at = _new_apptest()
    at.run()
    assert not at.exception, f"exceptions on empty-scan render: {at.exception}"
    print("PASS: (a) no scan in app_state -> 0 exceptions")


def test_b_seeded_scan_dict_mac_sourced():
    db.set_app_state("scanner", {
        "generated": "15 Jul 2026 · 09:00 IST",
        "spec": "CODEX-081-QUANT v1.0 (pre-registered 2026-07-20)",
        "core": [{
            "sym": "TCS", "name": "Tata Consultancy Services", "sector": "IT",
            "price": 3800.5, "mcap_cr": 1400000.0, "roe_pct": 45.0,
            "rev_g_pct": 12.0, "earn_g_pct": 10.0, "de": 8.0,
            "rs6m_pct": 5.0, "score": 72.3,
        }],
        "smallcap": [],
        "stats": {"core_universe": 500, "core_passed": 1, "core_skipped_datagaps": 3,
                   "small_universe": 250, "small_passed": 0, "small_skipped_datagaps": 250},
    })
    db.delete_app_state("scanner_last_scan_at")
    at = _new_apptest()
    at.run()
    assert not at.exception, f"exceptions on Mac-sourced seeded scan: {at.exception}"
    print("PASS: (b1) Mac-sourced seeded scan -> 0 exceptions")


def test_b_seeded_scan_dict_app_sourced():
    db.set_app_state("scanner", {
        "generated": "15 Jul 2026 · 09:00 IST",
        "spec": "CODEX-081-QUANT v1.0 (pre-registered 2026-07-20)",
        "core": [],
        "smallcap": [{
            "sym": "XYZ", "name": "XYZ Ltd", "sector": "Chemicals",
            "price": 450.0, "mcap_cr": 5000.0, "roe_pct": 20.0,
            "rev_g_pct": 18.0, "earn_g_pct": 22.0, "de": 30.0,
            "rs6m_pct": 8.0, "score": 65.0,
        }],
        "stats": {"core_universe": 500, "core_passed": 0, "core_skipped_datagaps": 500,
                   "small_universe": 250, "small_passed": 1, "small_skipped_datagaps": 249},
        "source": "app",
    })
    db.delete_app_state("scanner_last_scan_at")
    at = _new_apptest()
    at.run()
    assert not at.exception, f"exceptions on app-sourced seeded scan: {at.exception}"
    print("PASS: (b2) app-sourced seeded scan -> 0 exceptions")


def test_c_button_gated_when_recent():
    from datetime import datetime
    db.set_app_state("scanner_last_scan_at", {"ts": datetime.now().isoformat()})
    at = _new_apptest()
    at.run()
    assert not at.exception, f"exceptions with recent last_scan_at: {at.exception}"
    btn = next((b for b in at.button if b.key == "scan_now_btn"), None)
    assert btn is not None, "scan_now_btn not found in rendered tab"
    assert btn.disabled, "button should be DISABLED when last scan is <6h old"
    print("PASS: (c) button correctly gated (disabled) when last_scan_at is recent")


def test_c_button_not_gated_when_stale():
    db.set_app_state("scanner_last_scan_at", {"ts": "2020-01-01T00:00:00"})
    at = _new_apptest()
    at.run()
    assert not at.exception, f"exceptions with stale last_scan_at: {at.exception}"
    btn = next((b for b in at.button if b.key == "scan_now_btn"), None)
    assert btn is not None, "scan_now_btn not found in rendered tab"
    assert not btn.disabled, "button should be ENABLED when last scan is >6h old"
    print("PASS: (c) button correctly ungated (enabled) when last_scan_at is stale")


ALL_TESTS = [
    test_a_no_scan_in_app_state,
    test_b_seeded_scan_dict_mac_sourced,
    test_b_seeded_scan_dict_app_sourced,
    test_c_button_gated_when_recent,
    test_c_button_not_gated_when_stale,
]

if __name__ == "__main__":
    for fn in ALL_TESTS:
        fn()
    print(f"\n{len(ALL_TESTS)} AppTest scenarios passed.")
    if os.path.exists(DB_FILE):
        os.remove(DB_FILE)
