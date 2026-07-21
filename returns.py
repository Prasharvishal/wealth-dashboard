"""
Forward-measured returns (XIRR) — built on the `transactions` ledger.

Epoch-split doctrine: XIRR here is FORWARD-ONLY from each cashflow's own
recorded date (baseline rows dated 2026-07-21 onward). Pre-baseline gains are
never graded — that's *why* OPENING_BASELINE exists as its own transaction
kind instead of backdating a BUY to the holding's real purchase date.

Convention (matches xirr.py): OPENING_BASELINE/BUY/SIP are money leaving the
household -> negative. SELL/DIVIDEND/MATURITY are money returned -> positive.
The terminal leg (today's live value) is always positive and always the LAST
cashflow appended, dated today.

Two parallel ledgers per cashflow set:
  - portfolio cashflows: `amount` (₹), terminal = current live Value
  - benchmark ("if this had bought NIFTYBEES instead") cashflows: same sign/
    dates, but built from `bench_units` and the terminal is
    (sum of bench_units so far) * live NIFTYBEES price
"""

from datetime import date, datetime

import db
import xirr as xirr_mod

MIN_DAYS_FOR_XIRR = 30  # avoid absurd annualized numbers on day-one data


def _to_date(d):
    if isinstance(d, date):
        return d
    return datetime.fromisoformat(str(d)).date()


def _span_days(transactions):
    """Days between the earliest transaction and today — the basis for the
    "needs time" gate (MIN_DAYS_FOR_XIRR)."""
    if not transactions:
        return 0
    earliest = min(_to_date(t["date"]) for t in transactions)
    return (date.today() - earliest).days


def _portfolio_cashflows(transactions, terminal_value):
    flows = []
    for t in transactions:
        amt = float(t["amount"] or 0.0)
        signed = -amt if t["kind"] in db.TRANSACTION_OUTFLOW_KINDS else amt
        flows.append((_to_date(t["date"]), signed))
    flows.append((date.today(), float(terminal_value)))
    return flows


def compute_xirr_for_transactions(transactions, terminal_value):
    """XIRR for one set of transactions + today's terminal value.

    Returns None if there are fewer than MIN_DAYS_FOR_XIRR days of history,
    or if xirr.xirr() itself can't find a defined rate (see xirr.py's None
    cases). Never fabricates a number — "—" is the correct display for both
    reasons, and callers don't need to distinguish them.
    """
    if not transactions:
        return None
    if _span_days(transactions) < MIN_DAYS_FOR_XIRR:
        return None
    flows = _portfolio_cashflows(transactions, terminal_value)
    return xirr_mod.xirr(flows)


def compute_benchmark_xirr(transactions, bench_price):
    """XIRR of the NIFTYBEES shadow benchmark over the same cashflow dates.

    total bench_units accumulated (net of any SELL/DIVIDEND/MATURITY units,
    same sign convention as the ₹ ledger) * live NIFTYBEES price = terminal
    benchmark value. None if there's no live bench_price, no bench_units on
    any row, or the underlying xirr() is undefined (see xirr.py).
    """
    if not transactions or not bench_price:
        return None
    if _span_days(transactions) < MIN_DAYS_FOR_XIRR:
        return None

    flows = []
    net_units = 0.0
    for t in transactions:
        bu = t.get("bench_units")
        if bu in (None, ""):
            continue
        bu = float(bu)
        outflow = t["kind"] in db.TRANSACTION_OUTFLOW_KINDS
        amt = float(t["amount"] or 0.0)
        flows.append((_to_date(t["date"]), -amt if outflow else amt))
        net_units += -bu if outflow else bu

    if not flows:
        return None
    terminal_bench_value = net_units * float(bench_price)
    flows.append((date.today(), terminal_bench_value))
    return xirr_mod.xirr(flows)


def holding_xirr(holding_id, terminal_value):
    """XIRR for a single holding, keyed by holding_id."""
    txns = db.get_transactions(holding_id=holding_id)
    return compute_xirr_for_transactions(txns, terminal_value)


def sleeve_xirr(all_transactions, sleeve, terminal_value_by_sleeve):
    """XIRR for one sleeve, aggregating every transaction tagged to it."""
    txns = [t for t in all_transactions if t.get("sleeve") == sleeve]
    terminal = terminal_value_by_sleeve.get(sleeve, 0.0)
    return compute_xirr_for_transactions(txns, terminal)


def household_xirr(all_transactions, total_terminal_value):
    """XIRR across every recorded transaction — the headline household number."""
    return compute_xirr_for_transactions(all_transactions, total_terminal_value)


def household_benchmark_xirr(all_transactions, bench_price):
    """The 'vs just-buying-NIFTYBEES' headline number."""
    return compute_benchmark_xirr(all_transactions, bench_price)


def fmt_xirr(rate):
    """'—' for None/NaN (degenerate/insufficient history), else a signed %
    string. NaN check matters because a None stored in a pandas column next
    to real floats (e.g. the Holdings table's "XIRR (fwd)" column) comes back
    out as float('nan'), not None — `rate is None` alone would miss it."""
    if rate is None:
        return "—"
    try:
        if rate != rate:  # NaN != NaN is the cheapest, dependency-free NaN test
            return "—"
    except TypeError:
        return "—"
    return f"{rate * 100:+.1f}%/yr"
