"""Cutoff-anchored target engine for state PSC prelims.

Answers the question "how much do I actually need?" in arithmetic rather than vibes:
how many questions to attempt, at what accuracy, for what expected score and spread.

Exam structures here are read from official commission notifications (see EXAMS below,
each carries its own `source`). Anything not confirmed from a notification PDF is marked
verified=False and must not be presented as fact.

Run:  python3 strategy/target_engine.py
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Exam:
    key: str
    name: str
    questions: int
    total_marks: float
    options: int
    negative_fraction: float  # penalty as a fraction of a question's marks
    source: str
    verified: bool = True
    notes: str = ""

    @property
    def marks_per_question(self) -> float:
        return self.total_marks / self.questions

    @property
    def penalty(self) -> float:
        return self.marks_per_question * self.negative_fraction

    @property
    def breakeven_accuracy(self) -> float:
        """Accuracy at which attempting a question becomes EV-positive."""
        return self.penalty / (self.marks_per_question + self.penalty)


EXAMS: dict[str, Exam] = {
    "uppsc": Exam(
        key="uppsc",
        name="UPPSC PCS Prelims — GS Paper I",
        questions=150,
        total_marks=200.0,
        options=4,
        negative_fraction=1 / 3,
        source="UPPSC Advt A-1/E-1/2026 (uppsc.up.nic.in, 25-06-2026)",
        notes="Paper-II (CSAT) is qualifying at 33%; merit is decided on Paper-I alone.",
    ),
    "bpsc": Exam(
        key="bpsc",
        name="BPSC CCE Prelims — General Studies",
        questions=150,
        total_marks=150.0,
        options=4,
        negative_fraction=1 / 3,
        source="BPSC 72nd CCE advertisement (05-05-2026)",
    ),
    "jpsc": Exam(
        key="jpsc",
        name="JPSC CCE Prelims — Paper I + Paper II",
        questions=200,
        total_marks=400.0,
        options=4,
        negative_fraction=0.0,
        source="JPSC Advt 01/2026 — STRUCTURE NOT YET CONFIRMED FROM PDF",
        verified=False,
        notes=(
            "Paper II is Jharkhand-specific. Question count, per-paper marks and whether "
            "negative marking applies still need reading off the official notification."
        ),
    ),
    "upsc": Exam(
        key="upsc",
        name="UPSC CSE Prelims — GS Paper I",
        questions=100,
        total_marks=200.0,
        options=4,
        negative_fraction=1 / 3,
        source="UPSC CSE notification — official pattern",
        notes="Paper-II (CSAT) is qualifying at 33%; merit is decided on Paper-I alone.",
    ),
}


@dataclass
class Band:
    """A slice of the paper the candidate can treat alike.

    eliminated: how many of the wrong options he can rule out (0..options-1).
                options-1 eliminated means he knows the answer outright.
    """

    label: str
    count: int
    eliminated: int

    def accuracy(self, exam: Exam) -> float:
        remaining = exam.options - self.eliminated
        return 1.0 / max(remaining, 1)


@dataclass
class Outcome:
    attempted: int
    left_blank: int
    expected_marks: float
    std_dev: float
    band_detail: list[tuple[str, int, float, float]] = field(default_factory=list)

    def interval(self, sigmas: float = 1.0) -> tuple[float, float]:
        return (
            self.expected_marks - sigmas * self.std_dev,
            self.expected_marks + sigmas * self.std_dev,
        )


def question_ev(exam: Exam, accuracy: float) -> float:
    """Expected marks from attempting one question at the given accuracy."""
    return accuracy * exam.marks_per_question - (1 - accuracy) * exam.penalty


def question_variance(exam: Exam, accuracy: float) -> float:
    m, p = exam.marks_per_question, exam.penalty
    mean = question_ev(exam, accuracy)
    second = accuracy * m**2 + (1 - accuracy) * p**2
    return max(second - mean**2, 0.0)


def evaluate(exam: Exam, bands: list[Band], attempt_blind: bool = True) -> Outcome:
    """Score a paper-attack plan.

    A band is attempted whenever doing so is EV-positive. Bands that are exactly
    EV-neutral (a blind guess under 1/3 negative marking) are attempted only if
    attempt_blind is True — they add expected value of zero but real variance.
    """
    expected, variance, attempted, detail = 0.0, 0.0, 0, []

    for band in bands:
        acc = band.accuracy(exam)
        ev = question_ev(exam, acc)
        worth_it = ev > 1e-9 or (attempt_blind and ev >= -1e-9)
        if worth_it and band.count:
            expected += ev * band.count
            variance += question_variance(exam, acc) * band.count
            attempted += band.count
        detail.append((band.label, band.count, acc, ev))

    total_questions = sum(b.count for b in bands)
    return Outcome(
        attempted=attempted,
        left_blank=total_questions - attempted,
        expected_marks=expected,
        std_dev=math.sqrt(variance),
        band_detail=detail,
    )


def accuracy_needed(exam: Exam, target_marks: float, attempted: int) -> float | None:
    """Raw accuracy required across `attempted` questions to reach target_marks.

    Returns None when the target is unreachable at that attempt count.
    """
    if attempted <= 0:
        return None
    m, p = exam.marks_per_question, exam.penalty
    # target = attempted * (acc*m - (1-acc)*p)  ->  solve for acc
    acc = (target_marks / attempted + p) / (m + p)
    return acc if 0.0 <= acc <= 1.0 else None


def percentile_target(vacancies: int, ratio: float, appeared: int) -> dict[str, float]:
    """How selective prelims is, from the official mains-call ratio.

    Commissions call a fixed multiple of vacancies to mains, so the prelims bar is a
    percentile of those who actually turn up — not of those who merely registered.
    """
    slots = vacancies * ratio
    share = slots / appeared
    return {
        "mains_slots": slots,
        "share_of_appeared": share,
        "top_percent": share * 100,
        "one_in": appeared / slots if slots else float("inf"),
    }


def _bar(value: float, peak: float, width: int = 28) -> str:
    filled = int(round(width * value / peak)) if peak > 0 else 0
    return "█" * filled + "·" * (width - filled)


def main() -> None:
    exam = EXAMS["uppsc"]

    print("=" * 74)
    print(f"  {exam.name}")
    print(f"  Source: {exam.source}")
    print("=" * 74)
    print(f"  {exam.questions} questions · {exam.total_marks:g} marks "
          f"· {exam.marks_per_question:.3f} marks/question")
    print(f"  Negative marking: 1/{1/exam.negative_fraction:.0f} "
          f"= -{exam.penalty:.3f} per wrong answer")
    print(f"  BREAK-EVEN ACCURACY: {exam.breakeven_accuracy:.1%}")
    print()
    print("  Marginal value of eliminating options (per question):")
    for elim in range(exam.options):
        acc = 1.0 / (exam.options - elim)
        ev = question_ev(exam, acc)
        verdict = "ATTEMPT" if ev > 1e-9 else ("neutral" if ev >= -1e-9 else "SKIP")
        print(f"    eliminate {elim} → {acc:5.1%} accuracy → "
              f"EV {ev:+.3f} marks  [{verdict}]")
    print()

    # How selective is the bar, using the official 15x mains-call ratio.
    print("-" * 74)
    print("  HOW SELECTIVE IS PRELIMS  (official ratio: mains call = 15x vacancies)")
    print("-" * 74)
    scenarios = [
        ("PCS 2026 (~500 vac, 2025 turnout)", 500, 15, 267_340),
        ("PCS 2025 actual (920 vac)", 920, 15, 267_340),
        ("PCS 2024 actual (947 vac)", 947, 15, 243_111),
    ]
    for label, vac, ratio, appeared in scenarios:
        r = percentile_target(vac, ratio, appeared)
        print(f"  {label:36s} top {r['top_percent']:5.2f}%  "
              f"(~1 in {r['one_in']:.0f} who appear)")
    print()
    print("  Note: in 2025, 6,26,387 registered but only 2,67,340 appeared —")
    print("  well over half the 'competition' never sits the paper.")
    print()

    # Three honest attack plans for a first-timer.
    print("-" * 74)
    print("  PAPER-ATTACK PLANS  (150 questions)")
    print("-" * 74)
    plans = {
        "Weak prep": [
            Band("solid", 30, 3), Band("eliminate 2", 30, 2),
            Band("eliminate 1", 40, 1), Band("no idea", 50, 0),
        ],
        "Moderate prep": [
            Band("solid", 55, 3), Band("eliminate 2", 40, 2),
            Band("eliminate 1", 35, 1), Band("no idea", 20, 0),
        ],
        "Strong prep": [
            Band("solid", 80, 3), Band("eliminate 2", 40, 2),
            Band("eliminate 1", 22, 1), Band("no idea", 8, 0),
        ],
    }

    for name, bands in plans.items():
        out = evaluate(exam, bands, attempt_blind=True)
        lo, hi = out.interval(1.0)
        print(f"\n  {name}: attempt {out.attempted}, blank {out.left_blank}")
        for label, count, acc, ev in out.band_detail:
            print(f"      {label:14s} {count:3d} Q  acc {acc:5.1%}  "
                  f"EV {ev:+.3f}  {_bar(max(ev, 0), exam.marks_per_question, 20)}")
        print(f"      → expected {out.expected_marks:6.1f} / {exam.total_marks:g} marks "
              f"({out.expected_marks/exam.total_marks:.1%})")
        print(f"      → 1-sigma range {lo:.1f} to {hi:.1f} marks")

        # Cost of the common mistake: leaving the eliminate-1 band blank.
        timid = [b for b in bands if b.eliminated >= 2]
        timid_out = evaluate(exam, timid + [Band("skipped", 0, 0)], attempt_blind=False)
        forfeited = out.expected_marks - timid_out.expected_marks
        print(f"      → leaving every uncertain question blank forfeits "
              f"{forfeited:.1f} marks")

    print()
    print("-" * 74)
    print("  ACCURACY REQUIRED FOR A GIVEN SCORE")
    print("-" * 74)
    print(f"  {'attempted':>10s} " + "".join(f"{t:>10s}" for t in
          ["90 mks", "100 mks", "110 mks", "120 mks"]))
    for attempted in (100, 110, 120, 130, 140, 150):
        row = f"  {attempted:>10d} "
        for target in (90, 100, 110, 120):
            acc = accuracy_needed(exam, target, attempted)
            row += f"{acc:>9.1%} " if acc is not None else f"{'—':>10s}"
        print(row)

    print()
    print("  CAVEAT: UPPSC does not publish a prelims marks cutoff in advance, so the")
    print("  score targets above are illustrative. The mains-call ratio (15x) IS official")
    print("  and is the reliable anchor. Real cutoffs get wired in as the paper corpus")
    print("  is rebuilt — see STRATEGY_CHARTER.md gate G1.")
    print()

    unverified = [e for e in EXAMS.values() if not e.verified]
    if unverified:
        print("  ⚠ UNVERIFIED STRUCTURES (do not quote as fact):")
        for e in unverified:
            print(f"    - {e.name}: {e.notes}")
    print()


if __name__ == "__main__":
    main()
