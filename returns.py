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

Review hardening (2026-07-21, three passes — Codex 083/092/096):
  * split_orphan_sets — defense-in-depth against ledger rows whose holding no
    longer exists (or never did, i.e. legacy holding_id=NULL rows): such a set
    has no terminal value to close it, so including it would book a permanent
    phantom loss into household XIRR (review measured -58%/yr on a toy case).
    A set is kept only if its holding is alive OR it is self-closed per
    _group_is_closed — units-based (outflow units - SELL/MATURITY units ~= 0),
    or value-coverage-based when units are unknown, or a deterministic
    auto-SELL-on-delete provenance row (Codex 096 P4: "has ANY SELL" is NOT
    sufficient — a partial SELL must never masquerade as a full exit).
    Everything else is excluded and COUNTED so the UI can say so out loud
    instead of silently skewing the number.
  * Benchmark lockstep (Codex 096 P1+P5) — household_benchmark_panel runs ONE
    shared eligibility pass (build_comparison_set): a row is comparison-
    eligible iff its date parses and it carries (or backfills, one lazy
    attempt) bench_units. BOTH the actual-side and benchmark-side XIRR of the
    "vs NIFTYBEES" panel are computed from that EXACT SAME comparison_txns
    list — never all-flow actual XIRR against a partial-flow benchmark. The
    all-flow household_xirr remains its own standalone headline metric, never
    the direct benchmark counterpart when coverage is partial. The 30-day
    MIN_DAYS_FOR_XIRR gate runs on the comparison set's own date span, not
    the all-transaction span — and if the comparison set is empty/too young,
    BOTH sides of the panel render "— (needs time)" together.
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


_AUTO_SELL_PROVENANCE_PREFIX = "auto-SELL on holding delete"
_CLOSURE_UNIT_TOL = 1e-4          # ~zero remaining units
_CLOSURE_VALUE_COVERAGE = 0.95    # realized inflows must cover >=95% of outflow basis


def _group_is_closed(txns):
    """Is this orphaned group (holding deleted/never existed) fully wound
    down, per Codex 096 P2? "Has a SELL" is NOT sufficient — a partial SELL
    followed by the holding vanishing must NOT be treated as closed, or the
    unsold remainder's cost basis books a phantom loss (review repro:
    OPENING_BASELINE -100000, SELL +10000, holding gone -> was wrongly kept).

    Closure tests, in order:
      (a) units available: sum(outflow units) - sum(SELL/MATURITY units) ~= 0
      (b) units unavailable: realized inflow amount >= ~95% of outflow amount
      (c) contains a deterministic auto-SELL-on-delete provenance row (the
          delete_holding() path always books the full remaining quantity, so
          its presence alone is a reliable full-exit signal even if per-row
          units bookkeeping above is ambiguous).
    Anything else -> not closed -> excluded from XIRR, counted in the caveat.
    """
    if any(str(t.get("provenance") or "").startswith(_AUTO_SELL_PROVENANCE_PREFIX)
           for t in txns):
        return True

    outflow_units = 0.0
    close_units = 0.0
    units_known = True
    outflow_amt = 0.0
    close_amt = 0.0
    for t in txns:
        kind = t.get("kind")
        amt = float(t.get("amount") or 0.0)
        u = t.get("units")
        if kind in db.TRANSACTION_OUTFLOW_KINDS:
            outflow_amt += amt
            if u in (None, ""):
                units_known = False
            else:
                outflow_units += float(u)
        elif kind in ("SELL", "MATURITY"):
            close_amt += amt
            if u in (None, ""):
                units_known = False
            else:
                close_units += float(u)

    if units_known and outflow_units > 0:
        return abs(outflow_units - close_units) <= _CLOSURE_UNIT_TOL

    if outflow_amt > 0:
        return close_amt >= _CLOSURE_VALUE_COVERAGE * outflow_amt

    return False


def split_orphan_sets(transactions, live_holdings):
    """Partition the ledger into (kept_transactions, excluded_set_count).

    Grouping: rows with a holding_id group by that id; legacy rows without one
    (holding_id NULL — the removed free-text path) group by asset_name. A
    group is KEPT if its holding still exists (today's value provides its
    terminal leg) or if it is self-closed per _group_is_closed (a genuine
    full exit — by units, by realized-value coverage, or a deterministic
    auto-SELL-on-delete row — not merely "contains any SELL"). Everything
    else is an orphan: excluded from XIRR and counted for the UI caveat
    ("record their SELLs") — never silently folded into the number.
    """
    live_ids = {h["id"] for h in live_holdings}
    groups = {}
    for t in transactions:
        hid = t.get("holding_id")
        key = ("id", hid) if hid is not None else ("name", t.get("asset_name"))
        groups.setdefault(key, []).append(t)

    kept, excluded = [], 0
    for key, txns in groups.items():
        alive = key[0] == "id" and key[1] in live_ids
        closed = (not alive) and _group_is_closed(txns)
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


def build_comparison_set(transactions):
    """The ONE shared eligibility pass for the 'vs NIFTYBEES' panel (Codex 096
    P1/P5 fix). A row is comparison-eligible iff its date parses AND it has
    (or can, after one lazy backfill attempt, acquire) a bench_units value.

    Returns (comparison_txns, compared, lacking):
      comparison_txns — the eligible rows, sorted by date (both the actual
                         and the benchmark side of the panel are built from
                         EXACTLY this list, so they can never be on different
                         capital bases).
      compared/lacking — counts for the "M of N flows" caption.

    Malformed dates are skipped and counted separately via count_bad_dates —
    they never enter `lacking` (that count is specifically about missing
    bench_units on an otherwise-valid row).
    """
    comparison_txns = []
    lacking = 0
    for t in transactions:
        d = _txn_date(t)
        if d is None:
            continue  # malformed date — counted by count_bad_dates instead
        bu = t.get("bench_units")
        if bu in (None, ""):
            bu = _try_backfill_bench_units(t)
        if bu in (None, ""):
            lacking += 1
            continue
        comparison_txns.append(t)
    comparison_txns.sort(key=lambda t: (str(t.get("date")), t.get("id") or 0))
    return comparison_txns, len(comparison_txns), lacking


def _outflow_basis(transactions):
    """Sum of outflow (money-leaving-household) amounts across `transactions`
    — the capital basis used to scale a partial-coverage terminal value."""
    return sum(float(t.get("amount") or 0.0) for t in transactions
               if t.get("kind") in db.TRANSACTION_OUTFLOW_KINDS)


def comparison_actual_xirr(comparison_txns, all_kept_txns, total_net_worth):
    """Actual-side XIRR for the 'vs NIFTYBEES' panel, computed ONLY from
    comparison_txns (Codex 096 P1 fix) — never the all-flow household XIRR.

    Terminal value is scaled to the capital comparison_txns represents, using
    the simplest correct approximation the review accepted: pro-rate
    total_net_worth by (comparison outflow basis / all-flow outflow basis).
    This assumes the household's overall return profile is representative of
    the compared subset's return profile — an approximation, not an exact
    subset valuation (a true subset terminal value would require per-holding
    attribution, which the ledger doesn't carry). Documented here AND in the
    UI caption per the review's requirement.

    Returns (rate, roots_found) — (None, 0) if comparison_txns is empty/too
    young (MIN_DAYS_FOR_XIRR gated on the COMPARISON span, not all-flow span)
    or if the outflow-basis ratio can't be computed (all-flow basis is 0).
    """
    if not comparison_txns:
        return None, 0
    if _span_days(comparison_txns) < MIN_DAYS_FOR_XIRR:
        return None, 0
    all_basis = _outflow_basis(all_kept_txns)
    comp_basis = _outflow_basis(comparison_txns)
    if all_basis <= 0:
        return None, 0
    terminal_comparison = float(total_net_worth) * (comp_basis / all_basis)
    flows = _portfolio_cashflows(comparison_txns, terminal_comparison)
    if len(flows) < 2:
        return None, 0
    return xirr_mod.xirr_detailed(flows)


def compute_benchmark_xirr(comparison_txns, bench_price):
    """Benchmark-side XIRR of the NIFTYBEES shadow ledger for the 'vs
    NIFTYBEES' panel.

    Lockstep rule (Codex 096 P1/P5 fix): the caller MUST pass the same
    comparison_txns used for comparison_actual_xirr — this function no
    longer does its own eligibility filtering (that's build_comparison_set's
    job now, run ONCE and shared by both sides of the panel) and no longer
    re-attempts a backfill (already attempted when the set was built). The
    30-day gate here runs on the COMPARISON set's own span, not the original
    all-transaction span (P5) — a young comparison set renders "needs time"
    even if an old ineligible row would have satisfied the old, wrong gate.

    Returns (rate, roots_found, compared, lacking) for backward-compatible
    signature; compared/lacking simply describe the input set here (the
    counting itself now lives in build_comparison_set).
    """
    if not comparison_txns:
        return None, 0, 0, 0

    flows = []
    net_units = 0.0
    for t in comparison_txns:
        d = _txn_date(t)
        if d is None:
            continue  # shouldn't happen — build_comparison_set already filtered
        bu = t.get("bench_units")
        if bu in (None, ""):
            continue  # shouldn't happen — build_comparison_set already filtered
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
        return None, 0, compared, 0
    if _span_days(comparison_txns) < MIN_DAYS_FOR_XIRR:
        return None, 0, compared, 0

    flows.append((date.today(), net_units * float(bench_price)))
    rate, roots = xirr_mod.xirr_detailed(flows)
    return rate, roots, compared, 0


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
    """(rate, roots_found) across every KEPT transaction — the headline,
    ALL-FLOW number. This is its own standalone metric (Codex 096 P1(b)):
    never place it as the direct 'vs NIFTYBEES' counterpart when benchmark
    coverage is partial — use household_benchmark_panel for that comparison.
    Callers should pass transactions already filtered through
    split_orphan_sets so orphaned sets can't book phantom losses."""
    return compute_xirr_for_transactions(all_transactions, total_terminal_value)


def household_benchmark_panel(all_kept_transactions, total_net_worth, bench_price):
    """The full 'vs NIFTYBEES' comparison panel — true lockstep (Codex 096
    P1+P5 fix). Runs the ONE shared eligibility pass (build_comparison_set),
    then computes BOTH sides of the panel from the exact same comparison_txns
    so they're never on different capital bases.

    Returns a dict:
      actual_rate, actual_roots   — comparison-actual XIRR (comparison_txns
                                     only, terminal scaled to that subset's
                                     capital — see comparison_actual_xirr)
      bench_rate, bench_roots     — benchmark XIRR, same comparison_txns
      compared, lacking           — "compares M of N flows" caption counts
      comparison_txns             — the eligible subset (callers rarely need
                                     this directly, but it's exposed for
                                     tests/debugging)

    If comparison_txns is empty or younger than MIN_DAYS_FOR_XIRR, BOTH
    actual_rate and bench_rate come back None -> fmt_xirr renders
    "— (needs time)" on both sides of the panel, per the review's spec.
    """
    comparison_txns, compared, lacking = build_comparison_set(all_kept_transactions)
    actual_rate, actual_roots = comparison_actual_xirr(
        comparison_txns, all_kept_transactions, total_net_worth)
    bench_rate, bench_roots, _, _ = compute_benchmark_xirr(comparison_txns, bench_price)
    # Lockstep enforcement: if either side is gated to None (empty/too-young
    # comparison set), the OTHER side must not render a number either — a
    # young comparison set must show "needs time" on BOTH sides (P1(e)).
    if actual_rate is None or bench_rate is None:
        actual_rate, actual_roots = None, 0
        bench_rate, bench_roots = None, 0
    return {
        "actual_rate": actual_rate, "actual_roots": actual_roots,
        "bench_rate": bench_rate, "bench_roots": bench_roots,
        "compared": compared, "lacking": lacking,
        "comparison_txns": comparison_txns,
    }


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
