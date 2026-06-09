"""
FInancial-Independence projection engine.

This is the analytical heart of the dashboard. It projects three wealth
streams year-by-year to retirement age:

  1. Growth sleeve  — equities/crypto/gold etc. Compounds at the chosen
     scenario return (8 / 10 / 12 %). Monthly contributions step up annually.
  2. PF (provident fund) — debt layer. Steps up 5 %/yr, compounds at 7.5 %.
  3. NPS — debt layer. Steps up 10 %/yr, compounds at 9 %.

The FI target is NOT a constant. Annual expenses are PROJECTED forward: a base
expense that grows with inflation, plus an optional lifestyle ramp to a higher
target by a chosen year (e.g. a child arriving, a bigger home). The FI number
in any year = that year's projected annual expense ÷ safe-withdrawal-rate.

Outputs are returned in BOTH nominal and inflation-adjusted (real) rupees. The
UI surfaces the *real* number prominently because the nominal one flatters the
result. "FI reached at age X" is the first year total nominal corpus >= the
nominal FI number for that year.

No Monte Carlo — deterministic compounding only, by spec.
"""

from dataclasses import dataclass, field


@dataclass
class FIInputs:
    # Ages
    current_age: int = 30
    retirement_age: int = 50

    # Growth sleeve
    current_corpus: float = 0.0
    monthly_contribution: float = 100_000.0
    contribution_stepup_pct: float = 8.0
    return_pct: float = 10.0  # scenario return for the growth sleeve

    # PF (provident fund) — debt layer
    pf_current: float = 0.0
    pf_monthly: float = 37_500.0
    pf_stepup_pct: float = 5.0
    pf_return_pct: float = 7.5

    # NPS — debt layer
    nps_current: float = 0.0
    nps_monthly: float = 10_000.0
    nps_stepup_pct: float = 10.0
    nps_return_pct: float = 9.0

    # Expense projection (the projectable variable)
    expense_now_monthly: float = 60_000.0
    expense_target_monthly: float = 90_000.0
    expense_target_year: int = 8     # year by which the lifestyle ramp completes
    inflation_pct: float = 6.0

    # Withdrawal
    swr_pct: float = 3.5


@dataclass
class YearRow:
    year: int
    age: int
    growth_nominal: float
    pf_nominal: float
    nps_nominal: float
    total_nominal: float
    total_real: float
    annual_expense_nominal: float
    fi_number_nominal: float
    fi_number_real: float
    fi_reached: bool


@dataclass
class FIResult:
    rows: list = field(default_factory=list)
    fi_age: int = None              # age FI first reached (nominal), or None
    final_total_nominal: float = 0.0
    final_total_real: float = 0.0
    final_fi_number_nominal: float = 0.0
    final_fi_number_real: float = 0.0
    fi_progress_pct: float = 0.0    # current total / current-year FI number


def _projected_monthly_expense(inp: FIInputs, year: int) -> float:
    """Project the monthly expense for a given year-from-now.

    Two forces combine:
      * Inflation drift on the *base* expense (always present).
      * An optional lifestyle ramp that linearly lifts today's expense toward
        `expense_target_monthly` (in today's rupees) by `expense_target_year`,
        after which it holds at the target level — itself then inflating.

    We compute the lifestyle level in today's-rupees first, then inflate that
    real level to nominal for the given year. This keeps "expenses rise to ₹X
    by year N" intuitive: ₹X is read as today's money.
    """
    infl = inp.inflation_pct / 100.0
    t_year = max(inp.expense_target_year, 1)

    if year <= 0:
        real_level = inp.expense_now_monthly
    elif year >= t_year:
        real_level = inp.expense_target_monthly
    else:
        # Linear interpolation in today's rupees between now and target year.
        frac = year / t_year
        real_level = (
            inp.expense_now_monthly
            + (inp.expense_target_monthly - inp.expense_now_monthly) * frac
        )

    # Inflate the real lifestyle level to nominal rupees for that year.
    return real_level * ((1 + infl) ** year)


def _compound_stream(current, monthly, stepup_pct, return_pct, year_index):
    """Advance one stream by a single year.

    Contributions are made monthly and assumed to compound at the annual rate
    applied monthly (rate/12). The monthly contribution for the given year is
    stepped up annually by `stepup_pct`. Returns the end-of-year balance.
    """
    r_month = (return_pct / 100.0) / 12.0
    monthly_this_year = monthly * ((1 + stepup_pct / 100.0) ** year_index)
    bal = current
    for _ in range(12):
        bal = bal * (1 + r_month) + monthly_this_year
    return bal


def project(inp: FIInputs) -> FIResult:
    """Run the full deterministic projection to retirement age."""
    result = FIResult()
    infl = inp.inflation_pct / 100.0
    swr = max(inp.swr_pct, 0.1) / 100.0
    n_years = max(inp.retirement_age - inp.current_age, 0)

    growth = inp.current_corpus
    pf = inp.pf_current
    nps = inp.nps_current

    fi_reached_flag = False

    for year in range(0, n_years + 1):
        if year > 0:
            growth = _compound_stream(
                growth, inp.monthly_contribution,
                inp.contribution_stepup_pct, inp.return_pct, year - 1,
            )
            pf = _compound_stream(
                pf, inp.pf_monthly, inp.pf_stepup_pct, inp.pf_return_pct, year - 1,
            )
            nps = _compound_stream(
                nps, inp.nps_monthly, inp.nps_stepup_pct, inp.nps_return_pct, year - 1,
            )

        total_nominal = growth + pf + nps
        deflator = (1 + infl) ** year
        total_real = total_nominal / deflator

        annual_expense_nominal = _projected_monthly_expense(inp, year) * 12.0
        fi_number_nominal = annual_expense_nominal / swr
        fi_number_real = fi_number_nominal / deflator

        reached_this_year = total_nominal >= fi_number_nominal
        if reached_this_year and not fi_reached_flag:
            fi_reached_flag = True
            result.fi_age = inp.current_age + year

        result.rows.append(
            YearRow(
                year=year,
                age=inp.current_age + year,
                growth_nominal=growth,
                pf_nominal=pf,
                nps_nominal=nps,
                total_nominal=total_nominal,
                total_real=total_real,
                annual_expense_nominal=annual_expense_nominal,
                fi_number_nominal=fi_number_nominal,
                fi_number_real=fi_number_real,
                fi_reached=reached_this_year,
            )
        )

    if result.rows:
        first = result.rows[0]
        last = result.rows[-1]
        result.final_total_nominal = last.total_nominal
        result.final_total_real = last.total_real
        result.final_fi_number_nominal = last.fi_number_nominal
        result.final_fi_number_real = last.fi_number_real
        # Progress today = current total vs THIS year's FI number.
        result.fi_progress_pct = (
            100.0 * first.total_nominal / first.fi_number_nominal
            if first.fi_number_nominal > 0 else 0.0
        )

    return result


def run_scenarios(inp: FIInputs, scenarios=(8.0, 10.0, 12.0)):
    """Run the projection across the three return scenarios.

    Returns {return_pct: FIResult}. Only the growth-sleeve return varies;
    PF/NPS returns are fixed per spec.
    """
    out = {}
    for r in scenarios:
        scen = FIInputs(**{**inp.__dict__, "return_pct": r})
        out[r] = project(scen)
    return out
