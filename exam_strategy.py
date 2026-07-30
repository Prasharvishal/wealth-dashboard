"""PSC exam-strategy section — bolted onto the MAVI Vault Streamlit app.

Fully independent of the Vault's data: never touches wealth.db / Turso. All exam
persistence (study log, mock scores) lives as JSON under
`/Users/vishal/Documents/New project/strategy/`. Read-only sources (backtest
results, question bank, taxonomy, evidence docs) live under the sibling
`/Users/vishal/Documents/New project/` tree and are loaded fresh on every call
to render() (no caching across reruns needed — files are small and local).

Every number surfaced in the UI carries an evidence-tier caption:
  CALCULATED — computed here from the on-disk paper corpus.
  OFFICIAL   — read from a commission notification (target_engine.py EXAMS).
  ESTIMATED  — modelled/assumed, editable.
  WEAK       — single-source or small-sample; informational only.

Entry point: exam_strategy.render() — called from app.py when the sidebar
"📚 Exam" mode is selected, before any Vault data loads.
"""

from __future__ import annotations

import csv
import json
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

# ---------------------------------------------------------------------------
# Paths — all under the exam project root, never inside wealth_dashboard/.
# ---------------------------------------------------------------------------
BASE = Path("/Users/vishal/Documents/New project")
PROC = BASE / "data" / "processed"
CONFIG = BASE / "config"
STRATEGY_DIR = BASE / "strategy"
REPORTS = BASE / "reports"

BACKTEST_FILE = PROC / "backtest_g2_results.json"
QBANK_REPAIRED = PROC / "question_bank_repaired.csv"
QBANK_RECOVERED = PROC / "question_bank_recovered.csv"
TAXONOMY_FILE = CONFIG / "topic_taxonomy_v2.json"
TOPPERS_FILE = REPORTS / "toppers_evidence.md"
CHARTER_FILE = BASE / "STRATEGY_CHARTER.md"

STUDY_LOG_FILE = STRATEGY_DIR / "study_log.json"
MOCK_SCORES_FILE = STRATEGY_DIR / "mock_scores.json"

# Import the real target_engine module (ported logic, not duplicated) —
# strategy/ is a plain directory (no __init__.py), so add it to sys.path.
_target_engine = None
_target_engine_error = None
try:
    if str(STRATEGY_DIR) not in sys.path:
        sys.path.insert(0, str(STRATEGY_DIR))
    import target_engine as _target_engine  # noqa: E402
except Exception as exc:  # pragma: no cover - degrade gracefully
    _target_engine_error = str(exc)

MONTH_HOUR_TARGET = 180  # STRATEGY_CHARTER.md §7 — 180-200 h/month planned band
UPPSC_PRELIMS_DATE = date(2026, 12, 6)
K1_GATE_DATE = date(2027, 1, 31)

TIER_CALCULATED = "CALCULATED"
TIER_OFFICIAL = "OFFICIAL"
TIER_ESTIMATED = "ESTIMATED"
TIER_WEAK = "WEAK"
TIER_UNKNOWN = "UNKNOWN"


def _tier_caption(tier: str, note: str = "") -> None:
    st.caption(f"Evidence tier: **{tier}**" + (f" — {note}" if note else ""))


# ---------------------------------------------------------------------------
# Safe loaders — every one degrades to None/[] + a warning, never a crash.
# ---------------------------------------------------------------------------
def _warn_missing(path: Path, what: str) -> None:
    st.warning(f"⚠ {what} not found at `{path}` — this section is degraded. "
               f"Nothing else in the app is affected.")


@st.cache_data(show_spinner=False)
def load_backtest_results() -> list[dict]:
    if not BACKTEST_FILE.exists():
        return []
    try:
        return json.loads(BACKTEST_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


@st.cache_data(show_spinner=False)
def load_taxonomy() -> dict:
    if not TAXONOMY_FILE.exists():
        return {}
    try:
        return json.loads(TAXONOMY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def taxonomy_topics() -> list[str]:
    tax = load_taxonomy()
    topics = []
    for subj in tax.get("subjects", []):
        for t in subj.get("topics", []):
            topics.append(t["topic"])
    return topics


def _stem_pattern(keyword: str):
    """Ported verbatim from strategy/weightage_backtest.py stem_pattern()."""
    kw = keyword.strip().lower()
    body = r"\s+".join(re.escape(p) for p in kw.split())
    lead = r"\b" if kw[:1].isalnum() else ""
    return re.compile(lead + body, re.I)


@st.cache_resource(show_spinner=False)
def build_classifier_rules():
    """[(subject, topic, [compiled patterns])] — ported from weightage_backtest.py."""
    tax = load_taxonomy()
    rules = []
    for subj in tax.get("subjects", []):
        for t in subj.get("topics", []):
            pats = [_stem_pattern(k) for k in t.get("keywords", []) if k.strip()]
            if pats:
                rules.append((subj["subject"], t["topic"], pats))
    return rules


def classify_text(text: str, rules) -> str:
    """Topic with the most keyword hits; ties broken by rule order. Ported logic."""
    if not text:
        return "UNCLASSIFIED"
    best_topic, best_hits = "UNCLASSIFIED", 0
    for _subject, topic, pats in rules:
        hits = sum(1 for p in pats if p.search(text))
        if hits > best_hits:
            best_topic, best_hits = topic, hits
    return best_topic


@st.cache_data(show_spinner=False)
def load_question_bank() -> pd.DataFrame:
    """Union of repaired (has topic) + recovered (has options, needs classify)."""
    frames = []
    if QBANK_REPAIRED.exists():
        try:
            df1 = pd.read_csv(QBANK_REPAIRED, dtype=str, keep_default_na=False)
            df1["_source_file"] = "repaired"
            frames.append(df1)
        except Exception:
            pass
    if QBANK_RECOVERED.exists():
        try:
            df2 = pd.read_csv(QBANK_RECOVERED, dtype=str, keep_default_na=False)
            df2["_source_file"] = "recovered"
            frames.append(df2)
        except Exception:
            pass
    if not frames:
        return pd.DataFrame()

    df = pd.concat(frames, ignore_index=True, sort=False)

    if "topic" not in df.columns:
        df["topic"] = ""
    # Classify rows missing a topic (all of `recovered`, plus any blank in `repaired`).
    needs_classify = df["topic"].fillna("").str.strip().eq("")
    if needs_classify.any():
        rules = build_classifier_rules()
        text_col = df["question_text"] if "question_text" in df.columns else pd.Series([""] * len(df))
        df.loc[needs_classify, "topic"] = text_col[needs_classify].apply(
            lambda t: classify_text(t, rules))
        df.loc[needs_classify, "_topic_source"] = "auto-classified (taxonomy v2)"
    df.loc[~needs_classify, "_topic_source"] = "bank-labelled"

    for col in ("exam", "year", "stage", "paper", "subject"):
        if col not in df.columns:
            df[col] = ""
    for col in ("option_a", "option_b", "option_c", "option_d"):
        if col not in df.columns:
            df[col] = ""
    return df


def load_toppers_md() -> str | None:
    if not TOPPERS_FILE.exists():
        return None
    try:
        return TOPPERS_FILE.read_text(encoding="utf-8")
    except Exception:
        return None


def load_charter_md() -> str | None:
    if not CHARTER_FILE.exists():
        return None
    try:
        return CHARTER_FILE.read_text(encoding="utf-8")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Study log + mock scores persistence (JSON, local, exam-only — never wealth.db)
# ---------------------------------------------------------------------------
def _read_json_list(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _write_json_list(path: Path, rows: list[dict]) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rows, indent=2, default=str), encoding="utf-8")
        return True
    except Exception as exc:
        st.error(f"Could not save to {path}: {exc}")
        return False


def load_study_log() -> list[dict]:
    return _read_json_list(STUDY_LOG_FILE)


def append_study_log(entry: dict) -> bool:
    rows = load_study_log()
    rows.append(entry)
    return _write_json_list(STUDY_LOG_FILE, rows)


def load_mock_scores() -> list[dict]:
    return _read_json_list(MOCK_SCORES_FILE)


def append_mock_score(entry: dict) -> bool:
    rows = load_mock_scores()
    rows.append(entry)
    return _write_json_list(MOCK_SCORES_FILE, rows)


# ---------------------------------------------------------------------------
# Adaptive priority engine (deterministic — spec from the build task verbatim)
# ---------------------------------------------------------------------------
def emphasis_tier_map(exam: str) -> dict[str, str]:
    """topic -> DEEP / STANDARD / ONE-PASS, from the G2 keep-list for this exam."""
    results = load_backtest_results()
    rec = next((r for r in results if r.get("exam", "").upper() == exam.upper()), None)
    tiers = {t: "ONE-PASS" for t in taxonomy_topics()}
    if not rec or "keep_list" not in rec:
        return tiers
    keep = rec["keep_list"]
    half = max(1, (len(keep) + 1) // 2)
    for i, topic in enumerate(keep):
        tiers[topic] = "DEEP" if i < half else "STANDARD"
    return tiers


TIER_WEIGHT = {"DEEP": 3, "STANDARD": 2, "ONE-PASS": 1}


def _last_touch_dates(study_log: list[dict]) -> dict[str, date]:
    last = {}
    for row in study_log:
        topic = row.get("topic")
        d = _parse_date(row.get("date"))
        if topic and d:
            if topic not in last or d > last[topic]:
                last[topic] = d
    return last


def _parse_date(s) -> date | None:
    if not s:
        return None
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def revision_schedule_for_topic(topic: str, study_log: list[dict]) -> dict:
    """+1/+3/+7/+21/+45-day schedule from the topic's 'new study' entries.

    A later revision log entry for the same topic advances the schedule (its
    date becomes the new anchor for the *next* offset still owed).
    """
    offsets = [1, 3, 7, 21, 45]
    new_study_dates = sorted(
        d for d in (_parse_date(r.get("date")) for r in study_log
                    if r.get("topic") == topic and r.get("type") == "new study")
        if d)
    if not new_study_dates:
        return {}
    anchor = new_study_dates[0]  # first time this topic was studied
    revision_dates = sorted(
        d for d in (_parse_date(r.get("date")) for r in study_log
                    if r.get("topic") == topic and r.get("type") == "revision")
        if d)

    schedule = []
    last_point = anchor
    rev_pool = list(revision_dates)
    for off in offsets:
        due = anchor + timedelta(days=off)
        # if a revision was logged on/after this due date (and after the
        # previous stage), treat this stage as done and advance the anchor.
        done_on = next((d for d in rev_pool if d >= due), None)
        if done_on is not None:
            rev_pool.remove(done_on)
            schedule.append({"offset": off, "due": due, "done": True, "done_on": done_on})
            last_point = done_on
        else:
            schedule.append({"offset": off, "due": due, "done": False, "done_on": None})
    return {"anchor": anchor, "stages": schedule}


def next_due_stage(topic: str, study_log: list[dict], today: date) -> dict | None:
    sched = revision_schedule_for_topic(topic, study_log)
    for stage in sched.get("stages", []):
        if not stage["done"]:
            return stage
    return None


def compute_priority_queue(exam: str, study_log: list[dict], today: date, top_n: int = 5) -> list[dict]:
    tiers = emphasis_tier_map(exam)
    last_touch = _last_touch_dates(study_log)
    rows = []
    for topic, tier in tiers.items():
        touched = last_touch.get(topic)
        staleness_days = min((today - touched).days, 30) if touched else 30
        staleness_score = staleness_days / 30.0
        stage = next_due_stage(topic, study_log, today)
        overdue_bonus = 0
        if stage and stage["due"] <= today:
            overdue_bonus = 2
        score = TIER_WEIGHT.get(tier, 1) * staleness_score + overdue_bonus
        rows.append({
            "topic": topic,
            "tier": tier,
            "staleness_days": staleness_days,
            "last_touched": touched.isoformat() if touched else "never",
            "revision_overdue": overdue_bonus > 0,
            "score": round(score, 3),
        })
    rows.sort(key=lambda r: r["score"], reverse=True)
    return rows[:top_n]


# ---------------------------------------------------------------------------
# Streak + monthly hours
# ---------------------------------------------------------------------------
def hours_this_month(study_log: list[dict], today: date) -> float:
    total = 0.0
    for row in study_log:
        d = _parse_date(row.get("date"))
        if d and d.year == today.year and d.month == today.month:
            try:
                total += float(row.get("hours") or 0)
            except (TypeError, ValueError):
                pass
    return total


def current_streak(study_log: list[dict], today: date) -> int:
    days_with_log = {d for d in (_parse_date(r.get("date")) for r in study_log) if d}
    streak = 0
    cursor = today
    while cursor in days_with_log:
        streak += 1
        cursor -= timedelta(days=1)
    return streak


# ---------------------------------------------------------------------------
# render() — the single entry point called from app.py
# ---------------------------------------------------------------------------
def render() -> None:
    st.markdown("## 📚 PSC Exam Strategy")
    st.caption(
        "Independent of the Vault — persistence in `strategy/*.json` under the "
        "exam project folder. Never touches wealth.db."
    )

    if _target_engine is None:
        st.warning(
            "⚠ `strategy/target_engine.py` could not be imported "
            f"({_target_engine_error}). The Attack Calculator tab will be degraded."
        )

    today = date.today()
    study_log = load_study_log()
    if not STUDY_LOG_FILE.exists():
        st.info(
            "No study log yet at `strategy/study_log.json` — Today/Revision tabs "
            "will show empty state until you log your first session."
        )

    (tab_today, tab_priorities, tab_qbank, tab_calc,
     tab_revision, tab_resources, tab_charter) = st.tabs([
        "📌 Today", "🎯 Priorities", "📝 Question Bank", "🧮 Attack Calculator",
        "🔁 Revision", "📚 Resources", "🧭 Charter",
    ])

    with tab_today:
        _render_today(study_log, today)
    with tab_priorities:
        _render_priorities()
    with tab_qbank:
        _render_question_bank()
    with tab_calc:
        _render_attack_calculator()
    with tab_revision:
        _render_revision(study_log, today)
    with tab_resources:
        _render_resources()
    with tab_charter:
        _render_charter()


# ---------------------------------------------------------------------------
# Tab 1 — Today
# ---------------------------------------------------------------------------
def _render_today(study_log: list[dict], today: date) -> None:
    days_to_prelims = (UPPSC_PRELIMS_DATE - today).days
    days_to_k1 = (K1_GATE_DATE - today).days

    c1, c2 = st.columns(2)
    c1.metric("UPPSC Prelims countdown", f"{days_to_prelims} days",
              help=f"Target date {UPPSC_PRELIMS_DATE.isoformat()}")
    c2.metric("K1 calibration gate", f"{days_to_k1} days",
              help=f"STRATEGY_CHARTER.md K1 — {K1_GATE_DATE.isoformat()}")
    _tier_caption(TIER_OFFICIAL, "dates fixed in STRATEGY_CHARTER.md / user's exam calendar")

    st.markdown("#### Study queue — top 5 right now")
    queue = compute_priority_queue("UPPSC", study_log, today, top_n=5)
    if queue:
        qdf = pd.DataFrame(queue)[["topic", "tier", "staleness_days", "last_touched", "revision_overdue"]]
        qdf.columns = ["Topic", "Emphasis", "Days stale (cap 30)", "Last touched", "Revision overdue"]
        st.dataframe(qdf, use_container_width=True, hide_index=True)
        st.caption(
            "why these: score = tier weight (DEEP=3 / STANDARD=2 / ONE-PASS=1) × "
            "staleness/30, +2 if a revision is overdue. Highest score first. "
            "Primary exam = UPPSC (until 2026-12-06 per STRATEGY_CHARTER.md §2)."
        )
    else:
        st.info("Priority engine has no taxonomy topics to rank — check config/topic_taxonomy_v2.json.")
    _tier_caption(TIER_CALCULATED, "deterministic score from backtest keep-list + study_log.json")

    st.markdown("#### This month")
    hrs = hours_this_month(study_log, today)
    st.progress(min(hrs / MONTH_HOUR_TARGET, 1.0),
                text=f"{hrs:.1f}h / {MONTH_HOUR_TARGET}h target this month")
    streak = current_streak(study_log, today)
    st.metric("Current streak", f"{streak} day{'s' if streak != 1 else ''}")
    _tier_caption(TIER_CALCULATED, "summed from strategy/study_log.json entries")

    st.markdown("#### Quick-log a session")
    topics = taxonomy_topics()
    with st.form("quick_log_form", clear_on_submit=True):
        fc1, fc2 = st.columns(2)
        log_date = fc1.date_input("Date", value=today)
        exam = fc2.selectbox("Exam", ["UPPSC", "BPSC", "JPSC"])
        fc3, fc4 = st.columns(2)
        topic = fc3.selectbox("Topic", topics if topics else ["(taxonomy unavailable)"])
        hours = fc4.number_input("Hours", min_value=0.0, max_value=16.0, step=0.5, value=1.0)
        study_type = st.selectbox("Type", ["new study", "revision", "PYQ practice", "mock"])
        note = st.text_input("Note (optional)")
        submitted = st.form_submit_button("Log session", use_container_width=True)
        if submitted:
            entry = {
                "date": log_date.isoformat(),
                "exam": exam,
                "topic": topic,
                "hours": hours,
                "type": study_type,
                "note": note,
            }
            if append_study_log(entry):
                st.success(f"Logged {hours}h — {topic} ({study_type}) on {log_date.isoformat()}")
                st.rerun()


# ---------------------------------------------------------------------------
# Tab 2 — Priorities
# ---------------------------------------------------------------------------
def _render_priorities() -> None:
    results = load_backtest_results()
    if not results:
        _warn_missing(BACKTEST_FILE, "Backtest results")
        return

    exam = st.selectbox("Exam", ["UPPSC", "BPSC", "JPSC"], key="priorities_exam")
    rec = next((r for r in results if r.get("exam", "").upper() == exam.upper()), None)
    if not rec:
        st.warning(f"No G2 backtest record found for {exam}.")
        return

    verdict = rec.get("verdict", "UNKNOWN")
    if exam.upper() in ("UPPSC", "BPSC"):
        st.info(
            f"**G2 verdict {exam}: {verdict}** — skipping NOT supported — unequal "
            "emphasis IS (lift 1.6–1.9×). No topic is ever labelled 'skip'."
        )
    elif exam.upper() == "JPSC":
        st.success(
            "**JPSC: G2 PASS** but LEAVE-list blocked pending G3 cutoff data. "
            "Emphasis tiers below reflect the backtest keep-list; nothing is dropped."
        )

    tiers = emphasis_tier_map(exam)
    keep_list = rec.get("keep_list", [])
    rows = []
    for topic, tier in tiers.items():
        rank = keep_list.index(topic) + 1 if topic in keep_list else None
        rows.append({"Topic": topic, "Emphasis": tier, "Keep-list rank": rank or "—"})
    # DEEP first, then STANDARD, then ONE-PASS; within tier, by keep-list rank.
    tier_order = {"DEEP": 0, "STANDARD": 1, "ONE-PASS": 2}
    rows.sort(key=lambda r: (tier_order.get(r["Emphasis"], 3),
                              r["Keep-list rank"] if isinstance(r["Keep-list rank"], int) else 999))
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    st.caption(
        f"DEEP = top half of the {exam} keep-list ({rec.get('topics_kept', '?')}/"
        f"{rec.get('topics_total', '?')} topics kept at 80% train-coverage cut). "
        "STANDARD = bottom half of keep-list. ONE-PASS = not in keep-list — still "
        "covered once, never dropped."
    )
    _tier_caption(TIER_CALCULATED, f"data/processed/backtest_g2_results.json — {exam} record")

    st.markdown("#### Held-out year coverage")
    years = rec.get("years", {})
    if years:
        yrows = []
        for year, y in years.items():
            yrows.append({
                "Held-out year": year,
                "N questions": y.get("n"),
                "Strict coverage %": y.get("strict_coverage_pct"),
                "Classified-only coverage %": y.get("classified_coverage_pct"),
                "Unclassified %": y.get("unclassified_pct"),
                "Topic-share kept %": y.get("topic_share_kept_pct"),
                "Lift": y.get("lift"),
            })
        st.dataframe(pd.DataFrame(yrows), use_container_width=True, hide_index=True)
        st.caption(
            "Strict coverage = keep-list questions / ALL held-out questions (gate "
            "metric, unclassified counted against it). Lift = classified coverage "
            "÷ topic-share-kept (null baseline of random topic selection)."
        )
    _tier_caption(TIER_CALCULATED, "data/processed/backtest_g2_results.json — per-year gate metrics (STRATEGY_CHARTER.md G2)")


# ---------------------------------------------------------------------------
# Tab 3 — Question Bank
# ---------------------------------------------------------------------------
def _render_question_bank() -> None:
    df = load_question_bank()
    if df.empty:
        _warn_missing(QBANK_REPAIRED, "Question bank")
        return

    st.caption(f"{len(df):,} questions loaded (repaired + recovered, deduplication not applied).")
    _tier_caption(TIER_CALCULATED, "question_bank_repaired.csv + question_bank_recovered.csv; "
                                    "recovered-file topics auto-classified on the fly with taxonomy v2")

    fc1, fc2, fc3, fc4 = st.columns(4)
    exam_opts = ["(all)"] + sorted(x for x in df["exam"].unique() if x)
    year_opts = ["(all)"] + sorted((x for x in df["year"].unique() if x), reverse=True)
    stage_opts = ["(all)"] + sorted(x for x in df["stage"].unique() if x)
    topic_opts = ["(all)"] + sorted(x for x in df["topic"].unique() if x)

    exam_f = fc1.selectbox("Exam", exam_opts, key="qb_exam")
    year_f = fc2.selectbox("Year", year_opts, key="qb_year")
    stage_f = fc3.selectbox("Stage", stage_opts, key="qb_stage")
    topic_f = fc4.selectbox("Topic", topic_opts, key="qb_topic")
    search = st.text_input("Text search", key="qb_search")

    filtered = df
    if exam_f != "(all)":
        filtered = filtered[filtered["exam"] == exam_f]
    if year_f != "(all)":
        filtered = filtered[filtered["year"] == year_f]
    if stage_f != "(all)":
        filtered = filtered[filtered["stage"] == stage_f]
    if topic_f != "(all)":
        filtered = filtered[filtered["topic"] == topic_f]
    if search.strip():
        filtered = filtered[filtered["question_text"].str.contains(
            re.escape(search.strip()), case=False, na=False)]

    st.write(f"**{len(filtered):,}** questions match.")

    page_size = 20
    n_pages = max(1, (len(filtered) + page_size - 1) // page_size)
    page = st.number_input("Page", min_value=1, max_value=n_pages, value=1, step=1)
    start = (page - 1) * page_size
    page_df = filtered.iloc[start:start + page_size]

    has_options_col = "option_a" in page_df.columns

    for _, row in page_df.iterrows():
        header = f"{row.get('exam','?')} {row.get('year','?')} · {row.get('stage','?')} · {row.get('topic','UNCLASSIFIED')}"
        with st.container(border=True):
            st.markdown(f"**{header}**")
            st.write(row.get("question_text", ""))
            opts = [row.get(c, "") for c in ("option_a", "option_b", "option_c", "option_d")]
            if has_options_col and any(str(o).strip() for o in opts):
                with st.expander("Practice mode — reveal options"):
                    labels = ["a", "b", "c", "d"]
                    for lbl, opt in zip(labels, opts):
                        if str(opt).strip():
                            st.write(f"**{lbl})** {opt}")
    st.caption(f"Page {page} of {n_pages} · {page_size}/page")


# ---------------------------------------------------------------------------
# Tab 4 — Attack Calculator
# ---------------------------------------------------------------------------
def _render_attack_calculator() -> None:
    if _target_engine is None:
        st.warning("target_engine.py unavailable — Attack Calculator is degraded.")
        st.caption("Check strategy/target_engine.py exists and imports cleanly.")
        return

    exams = _target_engine.EXAMS
    exam_key = st.selectbox(
        "Exam", list(exams.keys()),
        format_func=lambda k: exams[k].name, key="calc_exam")
    exam = exams[exam_key]

    if not exam.verified:
        st.warning(f"⚠ UNVERIFIED structure — {exam.notes} (source: {exam.source})")
    else:
        st.caption(f"Source: {exam.source}")
    if exam.notes and exam.verified:
        st.caption(exam.notes)

    st.write(
        f"**{exam.questions} questions · {exam.total_marks:g} marks · "
        f"{exam.marks_per_question:.3f} marks/question**"
    )
    if exam.negative_fraction > 0:
        st.write(f"Negative marking: −1/{1/exam.negative_fraction:.0f} per wrong answer "
                 f"(−{exam.penalty:.3f} marks)")
        st.write(f"**Break-even accuracy: {exam.breakeven_accuracy:.1%}**")
    else:
        st.write("**No negative marking** — attempt everything, no penalty.")
    _tier_caption(TIER_OFFICIAL, exam.source if exam.verified else "UNVERIFIED — do not treat as fact")

    st.markdown("#### Your attack plan")
    remaining = exam.questions
    c1, c2, c3, c4 = st.columns(4)
    n_solid = c1.slider("# solid (know it)", 0, exam.questions, min(remaining, 60))
    remaining -= n_solid
    n_elim2 = c2.slider("# eliminate-2", 0, max(remaining, 0), min(remaining, 40))
    remaining -= n_elim2
    n_elim1 = c3.slider("# eliminate-1", 0, max(remaining, 0), min(remaining, 30))
    remaining -= n_elim1
    n_blind = c4.slider("# blind (no idea)", 0, max(remaining, 0), max(remaining, 0))

    total_entered = n_solid + n_elim2 + n_elim1 + n_blind
    if total_entered > exam.questions:
        st.error(f"Bands sum to {total_entered}, more than {exam.questions} questions — reduce a slider.")
        return
    elif total_entered < exam.questions:
        st.caption(f"{exam.questions - total_entered} question(s) unallocated (left blank, not counted below).")

    bands = [
        _target_engine.Band("solid", n_solid, exam.options - 1),
        _target_engine.Band("eliminate 2", n_elim2, 2),
        _target_engine.Band("eliminate 1", n_elim1, 1),
        _target_engine.Band("blind", n_blind, 0),
    ]
    attempt_blind = exam.negative_fraction == 0 or True  # attempt if EV >= 0, per evaluate()
    outcome = _target_engine.evaluate(exam, bands, attempt_blind=attempt_blind)
    lo, hi = outcome.interval(1.0)

    m1, m2, m3 = st.columns(3)
    m1.metric("Expected marks", f"{outcome.expected_marks:.1f} / {exam.total_marks:g}",
               help=f"{outcome.expected_marks/exam.total_marks:.1%} of total")
    m2.metric("1-sigma range", f"{lo:.1f} – {hi:.1f}")
    m3.metric("Attempted / blank", f"{outcome.attempted} / {outcome.left_blank}")
    _tier_caption(TIER_CALCULATED, "strategy/target_engine.py evaluate() — EV & variance per band")

    # Cost of timidity: leaving eliminate-1 (and blind) blank instead of attempting.
    timid_bands = [b for b in bands if b.eliminated >= 2]
    timid_outcome = _target_engine.evaluate(exam, timid_bands, attempt_blind=False)
    forfeited = outcome.expected_marks - timid_outcome.expected_marks
    if exam.negative_fraction > 0:
        st.info(
            f"Leaving every uncertain (eliminate-1 / blind) question blank instead "
            f"forfeits **{forfeited:.1f} marks** of expected value.\n\n"
            "**Rule: never leave a question blank you can eliminate one option on** "
            "(this exam has negative marking)."
        )
    else:
        st.info("**JPSC rule: attempt everything, no penalty.** There is no EV reason to leave anything blank.")
    _tier_caption(TIER_CALCULATED, "strategy/target_engine.py — forfeited EV from timid attempt vs full plan")

    with st.expander("Band-by-band detail"):
        detail_rows = [{"Band": lbl, "Count": cnt, "Accuracy": f"{acc:.1%}", "EV/question": f"{ev:+.3f}"}
                        for lbl, cnt, acc, ev in outcome.band_detail]
        st.dataframe(pd.DataFrame(detail_rows), use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------------
# Tab 5 — Revision
# ---------------------------------------------------------------------------
def _render_revision(study_log: list[dict], today: date) -> None:
    studied_topics = sorted({r["topic"] for r in study_log
                              if r.get("type") == "new study" and r.get("topic")})
    if not studied_topics:
        st.info("No 'new study' entries logged yet — log one in the Today tab to start a revision schedule.")
        return

    overdue, due_today, upcoming = [], [], []
    for topic in studied_topics:
        stage = next_due_stage(topic, study_log, today)
        if stage is None:
            continue  # fully revised through +45 days
        row = {"Topic": topic, "Offset": f"+{stage['offset']}d", "Due": stage["due"].isoformat()}
        if stage["due"] < today:
            overdue.append(row)
        elif stage["due"] == today:
            due_today.append(row)
        else:
            upcoming.append(row)

    st.markdown(f"#### 🔴 Overdue ({len(overdue)})")
    if overdue:
        st.dataframe(pd.DataFrame(overdue), use_container_width=True, hide_index=True)
    else:
        st.caption("None.")

    st.markdown(f"#### 🟡 Due today ({len(due_today)})")
    if due_today:
        st.dataframe(pd.DataFrame(due_today), use_container_width=True, hide_index=True)
    else:
        st.caption("None.")

    st.markdown(f"#### ⚪ Upcoming ({len(upcoming)})")
    if upcoming:
        upcoming_sorted = sorted(upcoming, key=lambda r: r["Due"])
        st.dataframe(pd.DataFrame(upcoming_sorted), use_container_width=True, hide_index=True)
    else:
        st.caption("None.")

    st.caption(
        "Schedule: +1 / +3 / +7 / +21 / +45 days from first 'new study' log for a "
        "topic. Logging a 'revision' entry for that topic on/after a due date "
        "advances the schedule to the next stage."
    )
    _tier_caption(TIER_CALCULATED, "derived from strategy/study_log.json — no separate state file")


# ---------------------------------------------------------------------------
# Tab 6 — Resources (static from toppers_evidence.md)
# ---------------------------------------------------------------------------
def _render_resources() -> None:
    md = load_toppers_md()
    if md is None:
        _warn_missing(TOPPERS_FILE, "Toppers evidence report")
        return

    st.markdown("#### Convergent core-GS books (by independent topper count)")
    book_rows = [
        {"Book": "M. Laxmikanth", "Named by": 9, "Subject": "Polity"},
        {"Book": "NCERT VI–XII", "Named by": 8, "Subject": "Foundation"},
        {"Book": "Spectrum / Rajiv Ahir", "Named by": 6, "Subject": "Modern History"},
        {"Book": "G.C. Leong", "Named by": 2, "Subject": "Physical Geography"},
        {"Book": "Shankar IAS", "Named by": 2, "Subject": "Environment"},
        {"Book": "Ramesh Singh", "Named by": 1, "Subject": "Economy (WEAK — single source)"},
        {"Book": "Lucent (Gen Science)", "Named by": 2, "Subject": "General Science"},
        {"Book": "RS Sharma / Satish Chandra (old NCERT)", "Named by": 3, "Subject": "History"},
    ]
    st.dataframe(pd.DataFrame(book_rows), use_container_width=True, hide_index=True)
    _tier_caption(TIER_WEAK, "convergent-count survivorship evidence — reports/toppers_evidence.md Part 2; "
                              "single-named rows are WEAK, never required reading (Charter G4)")

    st.markdown("#### State-specific English picks")
    st.markdown(
        "- **Jharkhand: \"Know Your State Jharkhand\" (Arihant)** — CONFIRMED ENGLISH, "
        "ISBN **9789378162992**, 2026-27 edition, 392pp, ~₹292. Strongest English-medium "
        "state recommendation found.\n"
        "- **Bihar:** Bihar Economic Survey + Bihar Budget (bilingual, official) + Bihar Samagra, "
        "Ghatna Chakra.\n"
        "- **UP:** \"Know Your State Uttar Pradesh\" + UP Budget + PRS Analysis of UP Budget.\n"
        "- State CURRENT AFFAIRS remains the real English gap for all three (funnelled to "
        "Hindi dailies — Prabhat Khabar / Dainik Bhaskar)."
    )
    _tier_caption(TIER_OFFICIAL, "reports/toppers_evidence.md Part 2 — ISBNs/publisher-confirmed")

    st.markdown("#### The Siddharth Gupta quote (UPPCS 2023 Rank 1)")
    st.info(
        '*"Dont ignore Hindi paper (especially for English medium candidates). '
        'It a low hanging fruit. Practice hindi daily."*\n\n'
        "UPPSC General Hindi is 150 merit marks and IS counted (not qualifying-only, "
        "per the official Advt A-1/E-1 Appendix-2)."
    )
    _tier_caption(TIER_OFFICIAL, "reports/toppers_evidence.md Part 1 (structure) + Part 5 (quote)")

    st.markdown("#### Mock-test prescriptions")
    st.markdown(
        "- **Weekly full-length test (FLT)** early in prep.\n"
        "- **2–3 FLTs/week in the final 2 months** (Siddharth Gupta, UPPCS 2023 R1).\n"
        "- **10+ essays** written before the exam.\n"
        "- **4–5 answers written daily** (mains practice).\n"
        "- Revision counts vary (10–12 rounds to 3–4 rounds) — no consensus; "
        "**no JPSC topper anywhere states a revision count.**"
    )
    _tier_caption(TIER_WEAK, "reports/toppers_evidence.md Part 6 — small named-sample prescriptive numbers")

    with st.expander("Full toppers_evidence.md"):
        st.markdown(md)


# ---------------------------------------------------------------------------
# Tab 7 — Charter (static + mock-score tracker)
# ---------------------------------------------------------------------------
def _render_charter() -> None:
    st.markdown("#### Gates (fixed — never moved after results, STRATEGY_CHARTER.md §5)")
    st.markdown(
        "- **G1 — Data sufficiency.** Weightage claim needs ≥3 exam years and ≥30 "
        "questions for that topic, else INFORMATIONAL only.\n"
        "- **G2 — Backtest validity (load-bearing).** Keep-list must cover ≥70% of "
        "held-out marks across ≥2 independent held-out years, or the leave-list is "
        "not published.\n"
        "- **G3 — Skip safety.** No topic on a LEAVE list unless dropping it still "
        "clears cutoff + 15% buffer in the backtest.\n"
        "- **G4 — Source honesty.** Every book recommendation states topper-count; "
        "single-source = WEAK EVIDENCE, never required reading."
    )
    st.markdown("#### Kill lines (§6)")
    st.markdown(
        "- **K1 — Calibration.** By 2027-01-31, on ≥150 h/month logged, a cold timed "
        "full-length paper scored against real cutoff. >15% below → stop and reassess.\n"
        "- **K2 — System usefulness.** If no G2-passing study/leave list by "
        "2026-09-30 → downgrade to plain tracker, revert to full-syllabus coverage.\n"
        "- **K3 — Health/sustainability.** <120 h/month for 2 consecutive months "
        "while plan assumes 180+ → re-plan to actual capacity, the plan is wrong "
        "not the person."
    )
    st.markdown("#### Time budget (§7)")
    st.markdown(
        "**Planned: 180–200 h/month**, one full rest day/week, 250+ h reserved only "
        "for the final 8-week run into an exam. User's original 269h/month proposal "
        "was rejected as unsustainable (a second full-time job, historically collapses "
        "around month 4)."
    )
    _tier_caption(TIER_OFFICIAL, "STRATEGY_CHARTER.md §5/§6/§7 — pre-registered 2026-07-29")

    st.divider()
    st.markdown("#### Mock-score tracker")
    with st.form("mock_score_form", clear_on_submit=True):
        c1, c2, c3, c4 = st.columns(4)
        m_date = c1.date_input("Date", value=date.today(), key="mock_date")
        m_exam = c2.selectbox("Exam", ["UPPSC", "BPSC", "JPSC"], key="mock_exam")
        m_score = c3.number_input("Score", min_value=0.0, step=0.5, key="mock_score")
        m_max = c4.number_input("Out of (max)", min_value=1.0, step=1.0, value=200.0, key="mock_max")
        submitted = st.form_submit_button("Log mock score", use_container_width=True)
        if submitted:
            entry = {
                "date": m_date.isoformat(), "exam": m_exam,
                "score": m_score, "max": m_max,
                "pct": round(100 * m_score / m_max, 1) if m_max else None,
            }
            if append_mock_score(entry):
                st.success(f"Logged {m_exam} {m_score}/{m_max:g} on {m_date.isoformat()}")
                st.rerun()

    scores = load_mock_scores()
    if not scores:
        st.info("No mock scores logged yet.")
        return

    df = pd.DataFrame(scores)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date")
    df["reference_55pct"] = 55.0

    import plotly.graph_objects as go
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=df["date"], y=df["pct"], mode="lines+markers", name="Mock score %"))
    fig.add_trace(go.Scatter(x=df["date"], y=df["reference_55pct"], mode="lines",
                              name="55% reference (indicative only)", line=dict(dash="dash")))
    fig.update_layout(yaxis_title="% of max", xaxis_title="Date", height=350,
                       margin=dict(l=10, r=10, t=30, b=10))
    st.plotly_chart(fig, use_container_width=True)
    st.caption("55% line is **indicative only — UPPSC publishes no advance cutoff**.")
    _tier_caption(TIER_CALCULATED, "strategy/mock_scores.json; 55% reference is ESTIMATED, not an official cutoff")
