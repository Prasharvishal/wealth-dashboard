# PSC Strategy System — Charter

**Pre-registered 2026-07-29, BEFORE any analysis result exists.**
Owner: Vishal (DOB 11-09-1996) · Built by: Claude · Status: ACTIVE

This document exists so that the strategy cannot be quietly rewritten after results arrive.
Gates and kill lines below are fixed. If a gate fails, the answer is "this approach failed",
not "let us adjust the gate".

---

## 1. The candidate — verified facts

| Fact | Value | Status |
|---|---|---|
| Date of birth | 11 September 1996 | user-confirmed |
| Degree | B.Tech, BITS Pilani | **eligibility CLEARED** — BITS Pilani is a UGC §3 deemed university |
| Domicile | Jharkhand | user-confirmed |
| Category | General / Unreserved — no relaxation in any state | user-confirmed |
| Medium | English only; Hindi conversational, weak formal writing | user-confirmed |
| Height | 171 cm | **clears DSP bar** (UPPSC & JPSC require 165 cm) |
| Chest | reported adequate | needs exact measurement: UPPSC DSP needs 84 cm unexpanded / 89 expanded |
| Employment | Voyage adviser, full-time, work-from-home | continues throughout — no resignation |
| Household | Wife earning; EMIs ₹96k/mo to ~Jul 2029, then ₹31.8k to ~2031 | joining year ~2029 aligns with EMI step-down |

## 2. Targets

- **Posts wanted:** SDM / Deputy Collector, DSP, CO.
- **Anchor exams:** BPSC (best odds, most regular) and JPSC (50% home-state prelims paper,
  top-heavy post list). UPPSC = longest runway (eligible to ~2036) and the only live 2026 door.
- **First real attempt:** UPPSC PCS Prelims, **6 December 2026**. User applying by 3 Aug 2026.

## 3. Eligibility runway (from official notifications — see reference memory)

| Exam | Upper age | Cut-off basis | Eligible until | Realistic cycles left |
|---|---|---|---|---|
| UPPSC | 40 | 1 July of exam year | ~2036 | ~11 (annual) |
| BPSC | 37 | 1 Aug of exam year | ~2033 | ~7-8 (annual) |
| JPSC | 35 | re-declared per cycle, **has been backdated** | ~2031, possibly later | ~2-3 (irregular) |

No attempt limits in any of the three.

## 4. What the system will and will not claim

**Will:** state every recommendation with its evidence, sample size and confidence; distinguish
CALCULATED from ESTIMATED from UNKNOWN; be backtested on held-out years before being trusted;
adapt to actual study logs rather than assuming the plan was followed.

**Will not:** claim to predict specific questions; present coaching-site figures as fact;
hide a failed validation; recommend a "skip" that the backtest does not support.

## 5. PRE-REGISTERED GATES — fixed now, never moved

**G1 — Data sufficiency (before any leave-list is published).**
A topic weightage claim may only be published if it rests on ≥ 3 distinct exam years and
≥ 30 questions for that topic. Below that: report as INFORMATIONAL, never as a study directive.

**G2 — Backtest validity (the load-bearing gate).**
Train the weightage model on papers up to year T; test on years > T (held out, never seen).
The strategy PASSES only if the topics it would have prioritised account for **≥ 70% of the
marks actually asked** in the held-out years, across **≥ 2 independent held-out years**.
If it fails, the leave-list is NOT published and the honest finding is: "past weightage does
not predict future papers well enough to justify skipping anything."

**G3 — Skip safety.**
No topic is placed on the LEAVE list unless, in the backtest, dropping it still leaves
cumulative expected marks **≥ cutoff + 15% safety buffer**.

**G4 — Source honesty.**
Every book/resource recommendation must name how many independent toppers cited it.
Single-source recommendations are labelled WEAK EVIDENCE and never presented as required reading.

## 6. KILL LINES

- **K1 — Calibration.** By **31 January 2027**, on ≥150 study-hours/month logged, a cold timed
  full-length past prelims paper must be scored against that paper's actual General cutoff.
  More than 15% below cutoff → stop, reassess honestly, do not reflexively buy another year.
- **K2 — System usefulness.** If by **30 September 2026** the system has not produced a
  backtest-passing (G2) study/leave list, it is downgraded to a plain tracker and the study
  plan reverts to conventional full-syllabus coverage. No further build time is spent.
- **K3 — Health/sustainability.** Sustained study below 120 h/month for 2 consecutive months
  while the plan assumes 180+ → the plan is wrong, not the person. Re-plan to actual capacity.

## 7. Study-time budget (pushed back from the user's original proposal)

User proposed ~269 h/month (6-7h workdays + 12h on 13-14 off-days) ≈ 62 h/week.
**Rejected as unsustainable** — that is a second full-time job and historically collapses
around month 4.

**Planned: 180-200 h/month**, one full rest day per week, with 250+ reserved only for the
final 8-week run into an exam. The system tracks ACTUAL hours and re-optimises against them
(see §8), rather than assuming compliance.

## 8. Adaptivity requirement

The plan is not a fixed calendar. It must re-optimise when reality deviates:
- Missed sessions roll forward by priority, not by date — highest marks-per-hour topics
  reclaim the next available slot.
- Revision intervals driven by spaced repetition on actual recall performance, not a fixed grid.
- Weightage model refreshes whenever new papers are added to the corpus.
- Every re-plan is logged so drift is visible, and the user can always see WHY the plan changed.

## 9. System components (build order)

1. **Target engine** — published cutoffs → required attempts, accuracy, and marks. *No new data needed.*
2. **Data repair** — UPPSC historical paper harvest · OCR of 350 scanned PDFs · BPSC ordinal→year
   mapping · taxonomy word-boundary fix. *Binding constraint on everything below.*
3. **Weightage + LEAVE list** — marks-per-study-hour ranking, cutoff-anchored cut line. Gated by G1/G3.
4. **Backtest harness** — train/test on held-out years. Gated by G2. **Blocks 3 from publication.**
5. **Cross-exam recycling detector** — near-duplicate concepts across UPPSC/BPSC/JPSC and years.
6. **English source map** — topic → specific English book/chapter, with explicit gap flags.
7. **Toppers meta-analysis** — convergent falsifiable claims only, frequency-ranked. Gated by G4.
8. **Streamlit app** — the daily surface, mirroring the Vault app pattern.

## 9a. TOKEN & MODEL POLICY (user order 2026-07-30 — binding)

1. **Main loop:** the top model (Fable 5) is reserved for judgement calls only — verdicts,
   backtest design, skip-list decisions, doctrine edits. The main-loop model is set by the
   user via /model; Claude cannot switch it itself, but must SAY when a cheaper model would
   suffice so the user can downgrade, and must not burn top-model tokens on mechanical work.
2. **All mechanical work goes to cheap subagents:** parsing/extraction → Haiku or Sonnet;
   code implementation from a tight spec → Sonnet; judgement-grade review → Opus. Never
   spawn a top-model subagent for mechanical work.
3. **Broad web-research fan-outs are DONE and BANNED without explicit user approval** —
   the 2026-07-29 fan-out consumed a full session limit. All findings are preserved in
   reports/toppers_evidence.md and the memory files; never re-research what is on disk.
4. **Local compute is free — prefer it.** OCR (tesseract), parsing, and analysis run
   locally at zero token cost. Choose local scripts over agent reasoning wherever possible.
5. **Max 2 concurrent subagents** unless the user explicitly approves a wider fan-out.
6. At session start, report any running/queued background tasks and kill orphans.

## 10. Evidence tiers (used on every output surface)

- **CALCULATED** — computed from extracted paper data; sample size shown.
- **OFFICIAL** — read from a commission notification PDF; source cited.
- **ESTIMATED** — modelled or assumed; assumption stated and editable in config.
- **WEAK** — single-source or small-sample; explicitly flagged, never a directive.
- **UNKNOWN** — no data exists. Said plainly rather than filled with plausible guesses.

## 11. Standing honesty rules

1. Never quote a number without its source in the same view.
2. Never present a coaching-aggregator figure as official.
3. If a validation fails, report the failure before anything else.
4. Small samples (<30) are informational, never directives.
5. The system says "I don't know" whenever that is the true answer.
