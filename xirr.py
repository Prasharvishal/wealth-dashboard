"""
XIRR — money-weighted annualized return via grid-scan + bisection. Pure stdlib.

Convention (matches finance-industry XIRR): investments are NEGATIVE cashflows
(money leaving your pocket), the terminal/current value and any money returned
to you (SELL, DIVIDEND, MATURITY) are POSITIVE. The solver finds annual rates
r such that:

    sum( cf / (1 + r) ** (days_from_first / 365.25) ) == 0

(day-count /365.25 to match the Holdings tab's adjacent CAGR column — review
fix 2026-07-21; /365.0 drifted cosmetically against it).

Solver (review fix 2026-07-21, second pass): a pure endpoint-sign bisection
misses real roots whenever an EVEN number of sign-crossings sit inside the
bracket (reproduced by review with a BUY/SELL/BUY-shaped ledger whose NPV is
negative at both ends but positive between two roots). So instead:

  1. Grid-scan NPV at ~200 points across the primary range [-0.9999, 10.0]
     (-99.99% .. +1000%/yr — the band real portfolios live in).
  2. Bisect EVERY sign-change subinterval found — collecting ALL roots.
  3. If no roots in the primary range, rescan the wide range
     [-0.9999, 10000.0]. (10000, not 1000: a "1000x in a calendar year" flow
     implies r ≈ +1004 with /365.25 day-count, so the wide ceiling needs
     headroom above 1000 for the review's own must-resolve case.)
  4. One root -> return it. Multiple roots -> return the one closest to the
     simple annualized estimate (total_in/total_out)**(1/years) - 1, and
     report the root count so callers can mark the answer approximate ("~").

Return contract of xirr_detailed(): (rate, roots_found)
  - (float, n>=1)   — a defined rate; n>1 means multi-root, treat as approx.
  - (None, 0)       — structurally undefined: <2 cashflows, all same day, or
    no sign change across the cashflows themselves.
  - (EXTREME, 0)    — cashflows DO change sign but NPV stays one-signed even
    across the wide range: the implied rate is beyond [-99.99%, +1000000%]/yr.
    Callers must render this distinctly from None ("— (extreme)" vs
    "— (needs time)") — conflating them hides real blowups behind the same
    dash as day-one data.

xirr() is the single-value wrapper (rate | None | EXTREME) for callers that
don't need the root count.
"""

from datetime import date

_DAYS_PER_YEAR = 365.25   # matches the CAGR column's day-count
_SCAN_LO = -0.9999
_PRIMARY_HI = 10.0
_WIDE_HI = 10000.0
_GRID_POINTS = 200
_TOL = 1e-6
_MAX_ITER = 200

# Sentinel: rate exists in principle but sits outside even the wide range.
# A string (not object()) so it survives a trip through a pandas object column
# unchanged and compares by value after any serialization.
EXTREME = "extreme"


def _npv(rate, cashflows, t0):
    """Net present value of `cashflows` at `rate`, anchored to t0 (day 0)."""
    total = 0.0
    for d, amt in cashflows:
        years = (d - t0).days / _DAYS_PER_YEAR
        total += amt / ((1.0 + rate) ** years)
    return total


def _bisect(flows, t0, lo, hi, npv_lo):
    """Standard bisection on a KNOWN sign-change subinterval [lo, hi]."""
    for _ in range(_MAX_ITER):
        mid = (lo + hi) / 2.0
        npv_mid = _npv(mid, flows, t0)
        if abs(npv_mid) < _TOL:
            return mid
        if (npv_mid > 0) == (npv_lo > 0):
            lo, npv_lo = mid, npv_mid
        else:
            hi = mid
        if (hi - lo) < _TOL:
            break
    return (lo + hi) / 2.0


def _find_roots(flows, t0, lo, hi, points=_GRID_POINTS):
    """All NPV roots in [lo, hi]: grid-scan for sign changes, bisect each."""
    xs = [lo + (hi - lo) * i / points for i in range(points + 1)]
    vals = [_npv(x, flows, t0) for x in xs]
    roots = []
    for i in range(points):
        fa, fb = vals[i], vals[i + 1]
        if fa == 0.0:
            roots.append(xs[i])
        elif (fa > 0) != (fb > 0):
            roots.append(_bisect(flows, t0, xs[i], xs[i + 1], fa))
    if vals[-1] == 0.0:
        roots.append(xs[-1])
    # Dedupe near-identical roots (a root landing exactly on a grid point can
    # register from both neighbouring cells).
    deduped = []
    for r in sorted(roots):
        if not deduped or abs(r - deduped[-1]) > 1e-5:
            deduped.append(r)
    return deduped


def _simple_estimate(flows):
    """Naive annualized estimate used ONLY to pick among multiple roots:
    (total money back / total money in) ** (1/years) - 1."""
    total_out = sum(-a for _, a in flows if a < 0)
    total_in = sum(a for _, a in flows if a > 0)
    days = (max(d for d, _ in flows) - min(d for d, _ in flows)).days
    years = days / _DAYS_PER_YEAR
    if total_out <= 0 or years <= 0:
        return 0.0
    try:
        return (total_in / total_out) ** (1.0 / years) - 1.0
    except (OverflowError, ZeroDivisionError, ValueError):
        return 0.0


def xirr_detailed(cashflows):
    """Money-weighted annualized return with root-count metadata.

    Args:
        cashflows: list[tuple[date, float]] — investments negative, returns/
            terminal value positive. Order doesn't matter; sorted internally.

    Returns:
        (rate, roots_found) per the module-docstring contract.
    """
    if not cashflows or len(cashflows) < 2:
        return None, 0

    flows = sorted(cashflows, key=lambda cf: cf[0])
    t0 = flows[0][0]

    if all(d == t0 for d, _ in flows):
        return None, 0  # no time spread — nothing to annualize

    amounts = [amt for _, amt in flows]
    has_pos = any(a > 0 for a in amounts)
    has_neg = any(a < 0 for a in amounts)
    if not (has_pos and has_neg):
        return None, 0  # no sign change — NPV can't cross zero at any rate

    roots = _find_roots(flows, t0, _SCAN_LO, _PRIMARY_HI)
    if not roots:
        roots = _find_roots(flows, t0, _SCAN_LO, _WIDE_HI)
    if not roots:
        return EXTREME, 0  # defined in principle, beyond even the wide range

    if len(roots) == 1:
        return roots[0], 1
    est = _simple_estimate(flows)
    best = min(roots, key=lambda r: abs(r - est))
    return best, len(roots)


def xirr(cashflows):
    """Single-value wrapper: float rate, None, or EXTREME (see xirr_detailed)."""
    return xirr_detailed(cashflows)[0]


if __name__ == "__main__":
    # ------------------------------------------------------------------
    # Self-tests — run with: python3 xirr.py
    # ------------------------------------------------------------------
    passed = 0
    failed = 0

    def check(name, actual, expected, tol=2e-3):
        global passed, failed
        if expected is None or actual is None:
            ok = actual is None and expected is None
        elif expected == EXTREME or actual == EXTREME:
            ok = actual == expected
        else:
            ok = abs(actual - expected) < tol
        status = "PASS" if ok else "FAIL"
        if ok:
            passed += 1
        else:
            failed += 1
        print(f"[{status}] {name}: got {actual}, expected {expected}")

    # 1. Single year double: -100 on day 0, +200 one calendar year later.
    #    With /365.25 day-count 365 days is slightly under a year, so the
    #    exact answer is ~100.1% — tolerance 2e-3 covers the day-count skew.
    cf1 = [(date(2025, 1, 1), -100.0), (date(2026, 1, 1), 200.0)]
    check("single-year double = ~100%", xirr(cf1), 1.00)

    # 2. Flat: invest 100, get exactly 100 back a year later == 0%/yr.
    cf2 = [(date(2025, 1, 1), -100.0), (date(2026, 1, 1), 100.0)]
    check("flat = 0%", xirr(cf2), 0.00)

    # 3. Monthly SIP case: invest 1000/month for 12 months, terminal 13500 —
    #    just assert a sane positive-return band.
    cf3 = [(date(2025, m, 1), -1000.0) for m in range(1, 13)]
    cf3.append((date(2026, 1, 1), 13500.0))
    r3 = xirr(cf3)
    ok3 = r3 is not None and r3 != EXTREME and 0.05 < r3 < 0.60
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

    # 7. Review case: -99.5% wipeout resolves (scan floor -0.9999; the old
    #    -0.99 floor mis-rendered this as the same "—" as needs-time data).
    cf7 = [(date(2025, 1, 1), -100.0), (date(2026, 1, 1), 0.5)]
    check("-99.5% loss resolves (not None)", xirr(cf7), -0.995)

    # 8. Review case: a 1000x calendar year resolves via the wide rescan.
    #    /365.25 day-count puts the root at ~+1003.7, above the old 1000 cap —
    #    which is exactly why _WIDE_HI is 10000.
    cf8 = [(date(2025, 1, 1), -1.0), (date(2026, 1, 1), 1000.0)]
    check("1000x year resolves", xirr(cf8), 1003.7, tol=1.0)

    # 9. Truly beyond even the wide range -> EXTREME sentinel (distinct from
    #    None): 1e9x overnight implies an annualized rate beyond astronomical.
    cf9 = [(date(2025, 1, 1), -1.0), (date(2025, 1, 2), 1e9)]
    check("beyond wide range = EXTREME", xirr(cf9), EXTREME)

    # 10. Review case (second pass): even number of interior sign-crossings —
    #     NPV negative at BOTH scan endpoints, two real roots between. The
    #     construction -100 / +230 / -132 at yearly spacing has exact roots
    #     r=10% and r=20% (from -100x^2 + 230x - 132 = 0 -> x = 1.1, 1.2).
    #     The old endpoint-sign check returned no answer here; the grid scan
    #     must find BOTH and select the one nearest the simple estimate
    #     ((230/232)^(1/2yr)-1 ~ -0.4%/yr -> nearest root is 10%).
    cf10 = [(date(2025, 1, 1), -100.0), (date(2026, 1, 1), 230.0),
            (date(2027, 1, 1), -132.0)]
    r10, n10 = xirr_detailed(cf10)
    ok10 = r10 is not None and r10 != EXTREME and abs(r10 - 0.10) < 5e-3 and n10 == 2
    print(f"[{'PASS' if ok10 else 'FAIL'}] multi-root: got rate={r10}, roots={n10}, "
          f"expected ~0.10 with roots=2")
    passed += 1 if ok10 else 0
    failed += 0 if ok10 else 1

    print(f"\n{passed} passed, {failed} failed")
    if failed:
        raise SystemExit(1)
