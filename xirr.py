"""
XIRR — money-weighted annualized return via bisection. Pure stdlib, no deps.

Convention (matches finance-industry XIRR): investments are NEGATIVE cashflows
(money leaving your pocket), the terminal/current value and any money returned
to you (SELL, DIVIDEND, MATURITY) are POSITIVE. `xirr()` finds the annual rate
r such that:

    sum( cf / (1 + r) ** (days_from_first / 365.0) ) == 0

Solved by bisection over r in [-0.99, 10.0] (i.e. -99% to +1000%/yr) rather
than Newton's method — bisection can't diverge or overshoot into r <= -1
(which would make the exponentiation undefined), so it's the robust choice
for a household finance tool that must never crash on odd cashflow shapes.

Returns None (never raises, never guesses) when the answer isn't well-defined:
  - fewer than 2 cashflows
  - all cashflows on the same day (no time value to annualize)
  - no sign change across cashflows (e.g. all negative, or all positive) —
    there's no rate that zeroes an all-one-sign NPV, bisection has nothing
    to bracket
  - bisection doesn't converge inside the search bounds within the iteration
    budget (NPV doesn't cross zero inside [-0.99, 10.0])
"""

from datetime import date

_LO, _HI = -0.99, 10.0
_TOL = 1e-6
_MAX_ITER = 200


def _npv(rate, cashflows, t0):
    """Net present value of `cashflows` at `rate`, anchored to t0 (day 0)."""
    total = 0.0
    for d, amt in cashflows:
        years = (d - t0).days / 365.0
        total += amt / ((1.0 + rate) ** years)
    return total


def xirr(cashflows):
    """Money-weighted annualized return.

    Args:
        cashflows: list[tuple[date, float]] — investments negative, returns/
            terminal value positive. Order doesn't matter; sorted internally.

    Returns:
        float annual rate (e.g. 0.15 == 15%/yr), or None if undefined.
    """
    if not cashflows or len(cashflows) < 2:
        return None

    flows = sorted(cashflows, key=lambda cf: cf[0])
    t0 = flows[0][0]

    if all(d == t0 for d, _ in flows):
        return None  # no time spread — nothing to annualize

    amounts = [amt for _, amt in flows]
    has_pos = any(a > 0 for a in amounts)
    has_neg = any(a < 0 for a in amounts)
    if not (has_pos and has_neg):
        return None  # no sign change — NPV can't cross zero

    lo, hi = _LO, _HI
    npv_lo = _npv(lo, flows, t0)
    npv_hi = _npv(hi, flows, t0)

    # Need a sign change in NPV across [lo, hi] to bracket a root.
    if npv_lo == 0.0:
        return lo
    if npv_hi == 0.0:
        return hi
    if (npv_lo > 0) == (npv_hi > 0):
        return None  # doesn't bracket within the search bounds

    for _ in range(_MAX_ITER):
        mid = (lo + hi) / 2.0
        npv_mid = _npv(mid, flows, t0)
        if abs(npv_mid) < _TOL:
            return mid
        if (npv_mid > 0) == (npv_lo > 0):
            lo, npv_lo = mid, npv_mid
        else:
            hi, npv_hi = mid, npv_mid
        if (hi - lo) < _TOL:
            return (lo + hi) / 2.0

    return None  # didn't converge within the iteration budget


if __name__ == "__main__":
    # ------------------------------------------------------------------
    # Self-tests — run with: python3 xirr.py
    # ------------------------------------------------------------------
    passed = 0
    failed = 0

    def check(name, actual, expected, tol=1e-3):
        global passed, failed
        ok = (actual is None and expected is None) or (
            actual is not None and expected is not None and abs(actual - expected) < tol
        )
        status = "PASS" if ok else "FAIL"
        if ok:
            passed += 1
        else:
            failed += 1
        print(f"[{status}] {name}: got {actual}, expected {expected}")

    # 1. Single year double: -100 on day 0, +200 one year later == 100%/yr.
    cf1 = [(date(2025, 1, 1), -100.0), (date(2026, 1, 1), 200.0)]
    check("single-year double = 100%", xirr(cf1), 1.00)

    # 2. Flat: invest 100, get exactly 100 back a year later == 0%/yr.
    cf2 = [(date(2025, 1, 1), -100.0), (date(2026, 1, 1), 100.0)]
    check("flat = 0%", xirr(cf2), 0.00)

    # 3. Monthly SIP case: invest 1000/month for 12 months, terminal value
    #    13500 at month 12 (a plausible ~15%/yr-ish outcome) — just assert it
    #    lands in a sane positive-return band (exact value depends on the
    #    solver; this checks the shape of the answer, not a hand-derived one).
    cf3 = [(date(2025, m, 1), -1000.0) for m in range(1, 13)]
    cf3.append((date(2026, 1, 1), 13500.0))
    r3 = xirr(cf3)
    ok3 = r3 is not None and 0.05 < r3 < 0.60
    print(f"[{'PASS' if ok3 else 'FAIL'}] monthly SIP lands in sane band: got {r3}")
    passed += 1 if ok3 else 0
    failed += 0 if ok3 else 1

    # 4. None case: fewer than 2 cashflows.
    check("single cashflow = None", xirr([(date(2025, 1, 1), -100.0)]), None)

    # 5. None case: all cashflows on the same day.
    cf5 = [(date(2025, 1, 1), -100.0), (date(2025, 1, 1), 100.0)]
    check("same-day flows = None", xirr(cf5), None)

    # 6. None case: no sign change (all negative — pure outflow, no return).
    cf6 = [(date(2025, 1, 1), -100.0), (date(2026, 1, 1), -50.0)]
    check("all-negative flows = None", xirr(cf6), None)

    print(f"\n{passed} passed, {failed} failed")
    if failed:
        raise SystemExit(1)
