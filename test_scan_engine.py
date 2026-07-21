"""
Unit tests for scan_engine's pure gate/scoring logic — the parts that must
match scripts/stock_scanner.py EXACTLY (SCAN-01 spec, ported verbatim).

Run: .venv/bin/python -m pytest test_scan_engine.py -v
     (or plain: .venv/bin/python test_scan_engine.py)

The Mac script (scripts/stock_scanner.py) isn't importable by path from this
repo (different machine/module namespace assumptions, and it lives outside
this git repo at /Users/vishal/TRADINGVIEW/scripts/), so these are hand-
computed fixtures cross-checked against the score_row/gate formulas as they
appear in scripts/stock_scanner.py's scan_universe()/score_row() (read
2026-07-21, verbatim byte-for-byte comparison of the gate conditions).
"""

import scan_engine as se


def _check(cond, msg):
    assert cond, msg


def test_score_row_matches_hand_computation():
    # revg=0.20, earng=0.10, roe=0.18, rs6m=0.05, de=40
    # 25*clamp(0.20/0.30) + 20*clamp(0.10/0.30) + 20*clamp(0.18/0.30)
    #   + 20*clamp(0.05/0.30) + 15*(1-40/100)
    # = 25*0.6667 + 20*0.3333 + 20*0.6 + 20*0.1667 + 15*0.6
    # = 16.6667 + 6.6667 + 12.0 + 3.3333 + 9.0 = 47.6667 -> 47.7
    got = se.score_row(0.20, 0.10, 0.18, 0.05, 40)
    _check(abs(got - 47.7) < 0.05, f"expected ~47.7 got {got}")


def test_score_row_caps_at_030_ratio():
    # revenueGrowth way above 0.30 should clamp to full 25 points, not overflow.
    got = se.score_row(1.0, 1.0, 1.0, 1.0, 0)
    # 25*1 + 20*1 + 20*1 + 20*1 + 15*1 = 100
    _check(abs(got - 100.0) < 0.05, f"expected 100.0 (fully clamped) got {got}")


def test_score_row_negative_rs6m_floors_at_zero_not_negative():
    # rs6m negative should contribute 0, not a negative score component.
    got_neg = se.score_row(0.20, 0.10, 0.18, -0.50, 40)
    got_zero = se.score_row(0.20, 0.10, 0.18, 0.0, 40)
    _check(abs(got_neg - got_zero) < 1e-9,
           f"negative rs6m ({got_neg}) should score same as rs6m=0 ({got_zero})")


def test_score_row_earnings_growth_none_treated_as_zero():
    got_none = se.score_row(0.20, None, 0.18, 0.05, 40)
    got_zero = se.score_row(0.20, 0.0, 0.18, 0.05, 40)
    _check(abs(got_none - got_zero) < 1e-9,
           "earningsGrowth=None must score identically to earningsGrowth=0.0")


def test_score_row_de_over_100_floors_component_at_zero():
    # D/E of 150 -> max(0, 1-1.5) = 0 contribution from that term, not negative.
    got = se.score_row(0.30, 0.30, 0.30, 0.30, 150)
    # 25+20+20+20+15*0 = 85
    _check(abs(got - 85.0) < 0.05, f"expected 85.0 (de term floored) got {got}")


def test_core_gate_passes_on_exact_boundary_values():
    # close must be strictly > sma200 (not >=)
    _check(not se.passes_core_gates(100.0, 100.0, 1000e7, 0.15, 0.08, 99.9, 0.01),
           "close == sma200 must FAIL (strict >)")
    _check(se.passes_core_gates(100.01, 100.0, 1000e7, 0.15, 0.08, 99.9, 0.01),
           "boundary-passing core row should PASS")


def test_core_gate_fails_below_each_threshold():
    base = dict(close=110.0, sma200=100.0, mcap=1000e7, roe=0.15, revg=0.08,
                de=99.9, margins=0.01)
    _check(se.passes_core_gates(**base), "baseline core row should pass")
    _check(not se.passes_core_gates(**{**base, "mcap": 999e7}), "mcap just under 1000Cr must fail")
    _check(not se.passes_core_gates(**{**base, "roe": 0.1499}), "ROE just under 15% must fail")
    _check(not se.passes_core_gates(**{**base, "revg": 0.0799}), "revG just under 8% must fail")
    _check(not se.passes_core_gates(**{**base, "de": 100.0}), "D/E == 100 must fail (strict <)")
    _check(not se.passes_core_gates(**{**base, "margins": 0.0}), "margins == 0 must fail (strict >)")
    _check(not se.passes_core_gates(**{**base, "mcap": None}), "missing mcap must fail (None gate)")


def test_smallcap_gate_fails_below_each_threshold():
    base = dict(close=110.0, sma200=100.0, mcap=100e7, roe=0.15, revg=0.15,
                earng=0.15, de=79.9, avgval20=3e7)
    _check(se.passes_smallcap_gates(**base), "baseline smallcap row should pass")
    _check(not se.passes_smallcap_gates(**{**base, "earng": 0.1499}), "earnG just under 15% must fail")
    _check(not se.passes_smallcap_gates(**{**base, "earng": None}), "missing earnG must fail")
    _check(not se.passes_smallcap_gates(**{**base, "revg": 0.1499}), "revG just under 15% must fail")
    _check(not se.passes_smallcap_gates(**{**base, "roe": 0.1499}), "ROE just under 15% must fail")
    _check(not se.passes_smallcap_gates(**{**base, "de": 80.0}), "D/E == 80 must fail (strict <)")
    _check(not se.passes_smallcap_gates(**{**base, "avgval20": 2.99e7}), "traded value just under Rs3Cr must fail")
    _check(not se.passes_smallcap_gates(**{**base, "mcap": 2e11}), "mcap == 20000Cr must fail (strict <)")


def test_clamp01_bounds():
    _check(se.clamp01(-5) == 0.0, "clamp01 floors negative at 0")
    _check(se.clamp01(5) == 1.0, "clamp01 ceils >1 at 1")
    _check(se.clamp01(0.5) == 0.5, "clamp01 passes through mid-range values")


ALL_TESTS = [
    test_score_row_matches_hand_computation,
    test_score_row_caps_at_030_ratio,
    test_score_row_negative_rs6m_floors_at_zero_not_negative,
    test_score_row_earnings_growth_none_treated_as_zero,
    test_score_row_de_over_100_floors_component_at_zero,
    test_core_gate_passes_on_exact_boundary_values,
    test_core_gate_fails_below_each_threshold,
    test_smallcap_gate_fails_below_each_threshold,
    test_clamp01_bounds,
]

if __name__ == "__main__":
    n_asserts = 0
    for fn in ALL_TESTS:
        fn()
        print(f"PASS: {fn.__name__}")
    print(f"\n{len(ALL_TESTS)} test functions passed.")
