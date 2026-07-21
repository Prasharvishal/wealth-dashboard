"""
Forward-measured returns (XIRR) — built on the `transactions` ledger.

Epoch-split doctrine: XIRR here is FORWARD-ONLY from each cashflow's own
recorded date (baseline rows dated 2026-07-21 onward). Pre-baseline gains are
never graded — that's *why* OPENING_BASELINE exists as its own transaction
kind instead of backdating a BUY to the holding's real purchase date. (The
floor is also ENFORCED at write time: db.add_transaction rejects any
non-baseline row dated before the household's earliest baseline.)

Convention (matches xirr.py): OPENING_BASELINE/BUY/SIP are money leaving the
household -> negative. SELL/DIVIDEND/MATURITY are money returned -> positive.
The terminal leg (today's live value) is always positive and always the LAST
cashflow appended, dated today.

Review hardening (2026-07-21, both passes):
  * split_orphan_sets — defense-in-depth against ledger rows whose holding no
    longer exists (or never did, i.e. legacy holding_id=NULL rows): such a set
    has no terminal value to close it, so including it would book a permanent
    phantom loss into household XIRR (review measured -58%/yr on a toy case).
    A set is kept only if its holding is alive OR it is self-closed by a
    SELL/MATURITY flow; everything else is excluded and COUNTED so the UI can
    say so out loud instead of silently skewing the number.
  * Benchmark lockstep — the NIFTYBEES shadow ledger includes ONLY flows that
    carry bench_units, and reports how many were excluded so the UI renders
    "benchmark compares M of N flows" instead of a silently apples-to-oranges
    comparison. A NULL bench_units row gets ONE lazy backfill attempt (fetch
    NIFTYBEES' close for that row's date, persist on success) before being
    excluded. The headline ACTUAL XIRR still uses every row.
  * Malformed dates never crash the page — bad rows are skipped per-row and
    counted (count_bad_dates) for a visible caption.
"""

from datetime import date, datetime

import db
import prices
import xirr as xirr_mod
from xirr import EXTREME

MIN_DAYS_FOR_XIRR = 30  # avoid absurd annualized numbers on day-one data
BENCH_TICKER = "NIFTYBEES.NS"


def _to_date(d):
    if isinstance(d, date) and not isinstance(d, datetime):
        return d
    return datetime.fromisoformat(str(d)).date()


def _txn_date(t):
    """One transaction's date as a date object, or None if malformed.
    Never raises (review fix: a single bad row must not crash the page)."""
    try:
        return _to_date(t["date"])
    except Exception:
        return None


def count_bad_dates(transactions):
    """How many rows have unparseable dates — surfaced as a UI caption."""
    return sum(1 for t in transactions if _txn_date(t) is None)


def _span_days(transactions):
    """Days between the earliest (valid-dated) transaction and today — the
    basis for the "needs time" gate (MIN_DAYS_FOR_XIRR)."""
    ds = [d for d in (_txn_date(t) for t in transactions) if d is not None]
    if not ds:
        return 0
    return (date.today() - min(ds)).days


def split_orphan_sets(transactions, live_holdings):
    """Partition the ledger into (kept_transactions, excluded_set_count).

    Grouping: rows with a holding_id group by that id; legacy rows without one
    (holding_id NULL — the removed free-text path) group by asset_name. A
    group is KEPT if its holding still exists (today's value provides its
    terminal leg) or if it is self-closed by a SELL/MATURITY flow (the money
    already round-tripped; holdings contribute nothing more for it).
    Everything else is an orphan: excluded from XIRR and counted for the UI
    caveat ("record their SELLs") — never silently folded into the number.
    """
    live_ids = {h["id"] for h in live_holdings}
    groups = {}
    for t in transactions:
        hid = t.get("holding_id")
        key = ("id", hid) if hid is not None else ("name", t.get("asset_name"))
        groups.setdefault(key, []).append(t)

    kept, excluded = [], 0
    for key, txns in groups.items():
        closed = any(t.get("kind") in ("SELL", "MATURITY") for t in txns)
        alive = key[0] == "id" and key[1] in live_ids
        if alive or closed:
            kept.extend(txns)
        else:
            excluded += 1
    kept.sort(key=lambda t: (str(t.get("date")), t.get("id") or 0))
    return kept, excluded


def _portfolio_cashflows(transactions, terminal_value):
    flows = []
    for t in transactions:
        d = _txn_date(t)
        if d is None:
            continue  # malformed date — skipped, counted by count_bad_dates
        amt = float(t["amount"] or 0.0)
        signed = -amt if t["kind"] in db.TRANSACTION_OUTFLOW_KINDS else amt
        flows.append((d, signed))
    flows.append((date.today(), float(terminal_value)))
    return flows


def compute_xirr_for_transactions(transactions, terminal_value):
    """XIRR for one set of transactions + today's terminal value.

    Returns (rate, roots_found) per xirr_detailed's contract, gated to
    (None, 0) when there are fewer than MIN_DAYS_FOR_XIRR days of history.
    Never fabricates a number — fmt_xirr renders "— (needs time)" for None
    and "— (extreme)" for the EXTREME sentinel.
    """
    if not transactions:
        return None, 0
    if _span_days(transactions) < MIN_DAYS_FOR_XIRR:
        return None, 0
    flows = _portfolio_cashflows(transactions, terminal_value)
    if len(flows) < 2:
        return None, 0
    return xirr_mod.xirr_detailed(flows)


def _try_backfill_bench_units(t):
    """One lazy backfill attempt for a NULL bench_units row: fetch NIFTYBEES'
    close on the row's own date and persist the derived units. Returns the
    units on success, None on continued failure (the row is then excluded
    from the benchmark and counted in the lockstep caveat)."""
    try:
        px = prices.fetch_stock_close_on(BENCH_TICKER, str(t["date"])[:10])
        if not px:
            return None
        bu = float(t["amount"] or 0.0) / float(px)
        db.update_transaction_bench_units(t["id"], bu)
        return bu
    except Exception:
        return None


def compute_benchmark_xirr(transactions, bench_price):
    """XIRR of the NIFTYBEES shadow benchmark over the same cashflow dates.

    Lockstep rule (review fix): ONLY flows carrying bench_units enter the
    benchmark ledger — a fetch-miss row is excluded (after one lazy backfill
    attempt) and counted, never silently mixed. Terminal = net accumulated
    bench_units * live NIFTYBEES price.

    Returns (rate, roots_found, compared, lacking):
      compared — how many ledger flows made it into the benchmark
      lacking  — how many were excluded for missing bench_units
    """
    if not transactions:
        return None, 0, 0, 0

    flows = []
    net_units = 0.0
    lacking = 0
    for t in transactions:
        d = _txn_date(t)
        if d is None:
            continue  # counted separately by count_bad_dates
        bu = t.get("bench_units")
        if bu in (None, ""):
            bu = _try_backfill_bench_units(t)
        if bu in (None, ""):
            lacking += 1
            continue
        bu = float(bu)
        amt = float(t["amount"] or 0.0)
        outflow = t["kind"] in db.TRANSACTION_OUTFLOW_KINDS
        flows.append((d, -amt if outflow else amt))
        # Units run OPPOSITE to the ₹ sign: an outflow (money in) BUYS bench
        # units (+bu); an inflow (SELL/DIVIDEND/MATURITY) sheds them (-bu).
        # (Caught by test U5d 2026-07-21 — the original inverted sign made
        # net units negative and the benchmark silently degenerate.)
        net_units += bu if outflow else -bu

    compared = len(flows)
    if not flows or not bench_price:
        return None, 0, compared, lacking
    if _span_days(transactions) < MIN_DAYS_FOR_XIRR:
        return None, 0, compared, lacking

    flows.append((date.today(), net_units * float(bench_price)))
    rate, roots = xirr_mod.xirr_detailed(flows)
    return rate, roots, compared, lacking


def holding_xirr(holding_id, terminal_value):
    """(rate, roots_found) for a single holding, keyed by holding_id."""
    txns = db.get_transactions(holding_id=holding_id)
    return compute_xirr_for_transactions(txns, terminal_value)


def sleeve_xirr(all_transactions, sleeve, terminal_value_by_sleeve):
    """(rate, roots_found) for one sleeve's tagged transactions."""
    txns = [t for t in all_transactions if t.get("sleeve") == sleeve]
    terminal = terminal_value_by_sleeve.get(sleeve, 0.0)
    return compute_xirr_for_transactions(txns, terminal)


def household_xirr(all_transactions, total_terminal_value):
    """(rate, roots_found) across every KEPT transaction — the headline
    number. Callers should pass transactions already filtered through
    split_orphan_sets so orphaned sets can't book phantom losses."""
    return compute_xirr_for_transactions(all_transactions, total_terminal_value)


def household_benchmark_xirr(all_transactions, bench_price):
    """The 'vs just-buying-NIFTYBEES' headline number:
    (rate, roots_found, compared, lacking)."""
    return compute_benchmark_xirr(all_transactions, bench_price)


def fmt_xirr(rate, roots_found=1):
    """Render one XIRR value:
      None / NaN        -> "— (needs time)"  (insufficient history / undefined)
      EXTREME sentinel  -> "— (extreme)"     (beyond the solver's wide range)
      float             -> "+12.3%/yr", "~"-prefixed when multiple NPV roots
                           existed and the nearest-to-estimate one was chosen.
    The two dash cases are deliberately distinct text (review fix): an extreme
    blowup must never wear the same dash as day-one data. NaN check matters
    because a None stored next to real floats in a pandas column comes back
    as float('nan'), not None."""
    if rate is None:
        return "— (needs time)"
    if isinstance(rate, str):
        return "— (extreme)" if rate == EXTREME else "— (needs time)"
    try:
        if rate != rate:  # NaN != NaN — cheapest dependency-free NaN test
            return "— (needs time)"
    except TypeError:
        return "— (needs time)"
    prefix = "~" if (roots_found or 1) > 1 else ""
    return f"{prefix}{rate * 100:+.1f}%/yr"
