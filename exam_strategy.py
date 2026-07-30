"""PSC exam-strategy section — bolted onto the MAVI Vault Streamlit app.

v2 (2026-07-30): cloud/mobile persistence + study-power features.

Data-file resolution is two-tier so the SAME code runs locally and on Streamlit
Community Cloud:
  1. "local mode" — the absolute "New project" paths (BASE below), when that
     tree exists on disk (i.e. this Mac).
  2. "cloud mode" — falls back to ./exam_data/ (bundled COPIES committed into
     this repo) when the local tree is absent.
A caption in the UI always shows which source is active.

Persistence (study log / mock scores / error log) now lives in the SAME DB
layer as the Vault (db.py — Turso over HTTP with local-SQLite fallback), NOT
JSON. This is what makes "log from the phone" and "log from localhost" write
to the same place. Tables are exam_study_log / exam_mock_scores /
exam_error_log, created lazily (CREATE TABLE IF NOT EXISTS, guarded) the first
time any of them is touched — this module still shares nothing else with the
Vault (no wealth.db table, no allocation logic). The legacy local JSON files
(strategy/study_log.json, strategy/mock_scores.json) are imported ONCE into
the DB on first run (idempotent — skipped if the DB already has rows) so nothing
logged before v2 is lost. If Turso is unreachable, writes fall back to the JSON
files with a visible warning — a log is never silently dropped.

Every number surfaced in the UI carries an evidence-tier caption:
  CALCULATED — computed here from the on-disk paper corpus.
  OFFICIAL   — read from a commission notification (target_engine.py EXAMS) or
               a verified official URL.
  ESTIMATED  — modelled/assumed, editable.
  WEAK       — single-source or small-sample; informational only.
  CONVERGENT — multiple independent sources agree (toppers_evidence.md).

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

import db  # shared Turso/SQLite persistence layer — reused verbatim, not duplicated

# ---------------------------------------------------------------------------
# Paths — two-tier resolution: local "New project" tree first, else the
# bundled ./exam_data/ copies committed into this repo (cloud mode).
# ---------------------------------------------------------------------------
BASE = Path("/Users/vishal/Documents/New project")
PROC = BASE / "data" / "processed"
CONFIG = BASE / "config"
STRATEGY_DIR = BASE / "strategy"
REPORTS = BASE / "reports"
PDF_DIR = BASE / "data" / "raw" / "pdfs"
MANIFEST_FILE = PROC / "uppsc_harvest_manifest.csv"

CLOUD_DATA = Path(__file__).parent / "exam_data"

LOCAL_MODE = BASE.exists()

if LOCAL_MODE:
    BACKTEST_FILE = PROC / "backtest_g2_results.json"
    QBANK_REPAIRED = PROC / "question_bank_repaired.csv"
    QBANK_RECOVERED = PROC / "question_bank_recovered.csv"
    TAXONOMY_FILE = CONFIG / "topic_taxonomy_v2.json"
else:
    BACKTEST_FILE = CLOUD_DATA / "backtest_g2_results.json"
    QBANK_REPAIRED = CLOUD_DATA / "question_bank_repaired.csv"
    QBANK_RECOVERED = CLOUD_DATA / "question_bank_recovered.csv"
    TAXONOMY_FILE = CLOUD_DATA / "topic_taxonomy_v2.json"

# These have no cloud copies by design (§Finish note / task spec): the toppers
# report, charter, and PDF corpus stay local-only. Cloud mode degrades them
# gracefully (existing _warn_missing / dedicated messages), never crashes.
TOPPERS_FILE = REPORTS / "toppers_evidence.md"
CHARTER_FILE = BASE / "STRATEGY_CHARTER.md"

# Legacy JSON persistence — read-only now except for the one-time migration
# into the DB tables below. Never written to going forward except as the
# fallback path when Turso/SQLite itself is unreachable. In cloud mode
# STRATEGY_DIR doesn't exist on disk at all (it's part of the local-only
# "New project" tree) — the fallback write path uses a local dir next to
# this file instead, so a Turso outage on Cloud still has somewhere durable
# (for that container's lifetime) to land instead of raising on a missing dir.
if LOCAL_MODE:
    STUDY_LOG_FILE = STRATEGY_DIR / "study_log.json"
    MOCK_SCORES_FILE = STRATEGY_DIR / "mock_scores.json"
    ERROR_LOG_FILE = STRATEGY_DIR / "error_log.json"  # fallback only; no legacy v1 file existed
else:
    _FALLBACK_DIR = Path(__file__).parent / "exam_data" / "_fallback"
    STUDY_LOG_FILE = _FALLBACK_DIR / "study_log.json"
    MOCK_SCORES_FILE = _FALLBACK_DIR / "mock_scores.json"
    ERROR_LOG_FILE = _FALLBACK_DIR / "error_log.json"


def data_source_caption() -> None:
    """Small caption showing which data-source tier is active (task Part A.1)."""
    if LOCAL_MODE:
        st.caption(f"📁 Data source: **local mode** — reading from `{BASE}`")
    else:
        st.caption(f"☁️ Data source: **cloud mode** — reading bundled copies from `{CLOUD_DATA}`")

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


# ---------------------------------------------------------------------------
# Junk-OCR / quality filter (added after live user testing of v2's Question
# Bank tab surfaced garbled Devanagari-as-Latin OCR rows, e.g. "qTma ap
# ffi-ffitr3 q5q dr alis q5nga qffiq€f*@ flfaRI" — these pass any ASCII-only
# check since OCR garbage is still Latin characters, just not English words.
# ---------------------------------------------------------------------------
_ENGLISH_SIGNAL_WORDS = {
    "the", "of", "in", "is", "which", "what", "who", "following", "correct",
    "india", "answer", "given", "above", "below", "not", "are", "was", "with", "from",
}
_WORD_RE = re.compile(r"[A-Za-z]+")
_MIN_ENGLISH_SIGNAL_WORDS = 3

# Paper names that are inherently Hindi-content (their OCR is real Hindi text
# mis-rendered as Latin garbage, not fixable by better English-word matching —
# task item #3: exclude by paper-name pattern, with an opt-in checkbox).
_HINDI_PAPER_RE = re.compile(r"hindi|language|literature|essay", re.IGNORECASE)


def is_english_like(text: str) -> bool:
    """True if `text` contains >= _MIN_ENGLISH_SIGNAL_WORDS distinct common
    English function/exam words (case-insensitive, word-boundary). Garbled
    OCR (Devanagari rendered as Latin noise) is still ASCII but essentially
    never contains real English function words, so this catches it without
    needing language-detection libraries."""
    if not text:
        return False
    words = {w.lower() for w in _WORD_RE.findall(text)}
    return len(words & _ENGLISH_SIGNAL_WORDS) >= _MIN_ENGLISH_SIGNAL_WORDS


def is_hindi_paper(paper: str) -> bool:
    return bool(paper) and bool(_HINDI_PAPER_RE.search(str(paper)))


def _clean_option(val) -> str:
    """Empty/NaN/whitespace-only option fields collapse to '' — pandas
    concat can introduce real float NaN for a column absent from one of the
    two source CSVs (repaired.csv has no option_* columns at all), which
    later stringifies to the literal text 'nan' if not guarded here."""
    if val is None:
        return ""
    try:
        if pd.isna(val):
            return ""
    except (TypeError, ValueError):
        pass
    return str(val).strip()


# ---------------------------------------------------------------------------
# Display-layer OCR-garbage cleanup (Question Bank tab only — never touches
# the source CSVs). Bilingual PSC papers OCR as: [real English question] +
# [garbled Hindi-half tail rendered as Latin-looking noise], sometimes with a
# second question's block merged in. Three passes, in order:
#   1. _first_option_block  — cut off a second "(a)...(d)" block (Hindi
#      repeat, or a merged next question) before anything else runs.
#   2. _split_stem_options   — regex-parse the first "(a)...(d)" block into
#      (stem, [options]) so they can be cleaned + rendered independently.
#   3. _strip_garbage        — token-level filter, applied to the stem and
#      each option separately (never to the whole blob at once, so garbage
#      trailing one option can't contaminate the next).
# ---------------------------------------------------------------------------

_GARBAGE_CHARS_RE = re.compile(r"[€¥®{}\[\]|@*`]")
_LIGATURE_JUNK_RE = re.compile(r"ff[il]{1,2}", re.IGNORECASE)
_VOWEL_RE = re.compile(r"[aeiouAEIOU]")
_PUNCT_RUN_RE = re.compile(r"^[:\-_.]{3,}$")
_LONE_OPTION_MARKER_RE = re.compile(r"^[\(\[]\s*[a-dA-D1-4]\s*[\)\].:]?$")

# Legit digit+alpha tokens to always preserve: plain years ("1947"), ordinals
# ("91st", "42nd"), and hyphenated alphanum like "COVID-19" / "Article-356".
_ORDINAL_RE = re.compile(r"^\d+(st|nd|rd|th)$", re.IGNORECASE)
_YEAR_RE = re.compile(r"^\d{3,4}$")
_ALPHANUM_WORD_RE = re.compile(r"^[A-Za-z]+-?\d+$")


def _is_garbage_token(tok: str) -> bool:
    """Conservative per-token junk test — when unsure, keep the token (task
    spec). Only fires on strong, specific signals of OCR-noise, never on
    plain unusual-looking real words."""
    if not tok:
        return False
    alpha_only = "".join(c for c in tok if c.isalpha())
    has_vowel = bool(_VOWEL_RE.search(alpha_only))

    if _GARBAGE_CHARS_RE.search(tok):
        return True
    if _PUNCT_RUN_RE.match(tok):
        return True

    colon_count = tok.count(":")
    # Token dominated by colon punctuation with little/no real alpha content,
    # e.g. "A::::::::" or "{:i" or "{:}" (OCR box/table-line noise).
    if colon_count >= 3 and colon_count >= len(alpha_only):
        return True
    # Colons gluing together alpha chunks where any chunk itself has zero
    # vowels, e.g. "::tnhd::eenndd:eenntt" -> chunks "tnhd"/"eenndd"/"eenntt";
    # "tnhd" has no vowel at all — real words glued by stray colons don't
    # happen; this is OCR table-line noise bleeding into the token stream.
    if colon_count >= 2:
        chunks = [p for p in tok.split(":") if p]
        if len(chunks) >= 2 and any(
            len(c) >= 3 and c.isalpha() and not _VOWEL_RE.search(c) for c in chunks
        ):
            return True

    # ffi/ffl ligature runs embedded in otherwise non-word junk (no vowel
    # elsewhere in the token) — legitimate words containing "ffi"/"ffl"
    # ("office", "affluent") always carry vowels around them, so this only
    # fires on the vowel-less garbage case.
    if _LIGATURE_JUNK_RE.search(tok) and not has_vowel:
        return True
    # Length>=5 alpha token with zero vowels — no real English word does this.
    if len(alpha_only) >= 5 and alpha_only.isalpha() and not has_vowel:
        return True

    # Digit+alpha mixed junk, e.g. "3FTTapqtr", "42iFT+rfu", "52FT.dwh" — but
    # preserve legit patterns first: pure years, ordinals, "COVID-19"-style,
    # and plain all-alpha or all-digit tokens.
    if _YEAR_RE.match(tok) or _ORDINAL_RE.match(tok):
        return False
    if tok.isalpha() or tok.isdigit():
        return False
    if _ALPHANUM_WORD_RE.match(tok):
        return False
    has_digit = any(c.isdigit() for c in tok)
    has_alpha = any(c.isalpha() for c in tok)
    if has_digit and has_alpha:
        stripped = tok.strip(".,;:")
        if _YEAR_RE.match(stripped) or _ORDINAL_RE.match(stripped) or _ALPHANUM_WORD_RE.match(stripped):
            return False
        return True
    return False


def _strip_garbage(text: str) -> str:
    """Token-level cleaner — drop OCR-garbage tokens, collapse whitespace.
    Conservative: only drops tokens _is_garbage_token flags; everything else
    (including odd-looking but plausible real words) is kept."""
    if not text:
        return text
    kept = []
    for tok in text.split():
        if _is_garbage_token(tok):
            continue
        if _LONE_OPTION_MARKER_RE.match(tok):
            continue
        kept.append(tok)
    return re.sub(r"\s+", " ", " ".join(kept)).strip()


_FIRST_OPTION_A_RE = re.compile(r"\(\s*[aA1i]\s*\)")


def _first_option_block(text: str) -> str:
    """If the text contains two or more '(a)...' sequences, truncate
    everything from the SECOND '(a)' onward — the Hindi repeat of the same
    question, or a merged-in next question. Must run BEFORE garbage
    stripping (stripping first could delete a marker and hide the merge)."""
    if not text:
        return text
    matches = list(_FIRST_OPTION_A_RE.finditer(text))
    if len(matches) >= 2:
        return text[:matches[1].start()].strip()
    return text


_STEM_OPTIONS_RE = re.compile(
    r"^(?P<stem>.*?)"
    r"[\(\[]\s*(?:a|A|1|i)\s*[\)\].:]\s*(?P<a>.*?)"
    r"[\(\[]\s*(?:b|B|2|ii)\s*[\)\].:]\s*(?P<b>.*?)"
    r"(?:[\(\[]\s*(?:c|C|3|iii)\s*[\)\].:]\s*(?P<c>.*?))?"
    r"(?:[\(\[]\s*(?:d|D|4|iv)\s*[\)\].:]\s*(?P<d>.*?))?"
    r"$",
    re.IGNORECASE | re.DOTALL,
)
_WORDLIKE_RE = re.compile(r"[A-Za-z]{2,}")


def _split_stem_options(text: str) -> tuple[str, list[str]]:
    """Regex-parse the first '(a) X (b) Y (c) Z (d) W' block into
    (stem, [options]); tolerates (A)/(1)/(i) variants. Returns ([], "") — i.e.
    (text, []) — unchanged if parsing doesn't yield >=2 usable options."""
    if not text:
        return text, []
    m = _STEM_OPTIONS_RE.match(text)
    if not m:
        return text, []
    stem = (m.group("stem") or "").strip()
    opts = [m.group(k).strip() for k in ("a", "b", "c", "d") if m.group(k) and m.group(k).strip()]
    good = [o for o in opts if _WORDLIKE_RE.search(o)]
    if len(good) >= 2 and stem:
        return stem, opts
    return text, []


def clean_question_display(raw_text: str) -> tuple[str, list[str]]:
    """Full pipeline for one question_text blob -> (clean_stem, clean_options).
    clean_options is [] when structured parsing wasn't possible/reliable —
    callers should render clean_stem as the whole cleaned text in that case.
    Order (task spec): _first_option_block -> _split_stem_options ->
    _strip_garbage (stem and each option separately) -> re-check options
    survived cleaning with real wordlike content (garbage-only options, e.g.
    unrecoverable Hindi-OCR answer choices, must fall back to plain text
    rather than render as empty/junk option rows)."""
    if not raw_text:
        return "", []
    truncated = _first_option_block(raw_text)
    stem, opts = _split_stem_options(truncated)
    if opts:
        clean_stem = _strip_garbage(stem)
        clean_opts = [_strip_garbage(o) for o in opts]
        good = [o for o in clean_opts if _WORDLIKE_RE.search(o)]
        if len(good) >= 2:
            return clean_stem, clean_opts
    return _strip_garbage(truncated), []


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
        # Guard against pd.concat-introduced NaN (see _clean_option docstring)
        # AND genuine empty/whitespace cells — both collapse to "".
        df[col] = df[col].apply(_clean_option)

    text_col = df["question_text"] if "question_text" in df.columns else pd.Series([""] * len(df))
    df["_low_quality"] = ~text_col.fillna("").apply(is_english_like)
    df["_hindi_paper"] = df["paper"].fillna("").apply(is_hindi_paper) if "paper" in df.columns else False
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
# Study log / mock scores / error log persistence — v2: db.py (Turso over HTTP
# with local-SQLite fallback), REUSING db.py's own query/execute/query_scalar
# helpers verbatim (same pattern app.py uses for every Vault table). This is
# what makes phone-logged and localhost-logged entries land in the SAME place.
#
# Legacy JSON files (study_log.json / mock_scores.json) are migrated into the
# DB tables exactly once, idempotently: the migration checks the DB's row
# count first and only imports if the DB side is still empty AND the JSON
# file has entries — so re-running render() a thousand times never duplicates
# rows, and a DB that already has real data is never clobbered by a stale
# JSON file.
#
# If the DB is unreachable (Turso down AND local sqlite3 write fails for some
# reason), every writer below falls back to the JSON file and raises a
# visible st.warning — a log is never silently dropped (task Part A.3).
# ---------------------------------------------------------------------------
_EXAM_TABLES_READY = False


def _ensure_exam_tables() -> bool:
    """CREATE TABLE IF NOT EXISTS for all three exam tables, guarded.

    Returns True if the DB layer looks usable, False if even the guarded
    create failed (caller should fall back to JSON). Cheap to call repeatedly
    — cached per-process via _EXAM_TABLES_READY once it succeeds once.
    """
    global _EXAM_TABLES_READY
    if _EXAM_TABLES_READY:
        return True
    try:
        db.execute("""CREATE TABLE IF NOT EXISTS exam_study_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT, exam TEXT, topic TEXT, hours REAL, type TEXT,
            note TEXT, created_at TEXT)""")
        db.execute("""CREATE TABLE IF NOT EXISTS exam_mock_scores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT, exam TEXT, score REAL, max_score REAL,
            note TEXT, created_at TEXT)""")
        db.execute("""CREATE TABLE IF NOT EXISTS exam_error_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT, exam TEXT, topic TEXT, count INTEGER,
            note TEXT, created_at TEXT)""")
        _EXAM_TABLES_READY = True
        return True
    except Exception:
        return False


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


def _migrate_json_to_db_once() -> None:
    """One-time, idempotent import of legacy JSON logs into the DB tables.

    Idempotency guard: only imports into a table that is still EMPTY (row
    count checked first) AND only if the legacy JSON file actually has
    entries. A DB that already has rows (from this migration or from normal
    use) is never touched again — safe to call on every render().
    """
    if not _ensure_exam_tables():
        return
    try:
        study_count = db.query_scalar("SELECT COUNT(*) FROM exam_study_log") or 0
        if study_count == 0:
            legacy = _read_json_list(STUDY_LOG_FILE)
            if legacy:
                now = datetime.now().isoformat(timespec="seconds")
                db.executemany(
                    "INSERT INTO exam_study_log (date, exam, topic, hours, type, note, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [(r.get("date"), r.get("exam"), r.get("topic"), r.get("hours"),
                      r.get("type"), r.get("note"), now) for r in legacy],
                )
    except Exception:
        pass  # migration is best-effort; never blocks normal render()

    try:
        mock_count = db.query_scalar("SELECT COUNT(*) FROM exam_mock_scores") or 0
        if mock_count == 0:
            legacy = _read_json_list(MOCK_SCORES_FILE)
            if legacy:
                now = datetime.now().isoformat(timespec="seconds")
                db.executemany(
                    "INSERT INTO exam_mock_scores (date, exam, score, max_score, note, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    [(r.get("date"), r.get("exam"), r.get("score"),
                      r.get("max") if r.get("max") is not None else r.get("max_score"),
                      r.get("note"), now) for r in legacy],
                )
    except Exception:
        pass


def load_study_log() -> list[dict]:
    if _ensure_exam_tables():
        try:
            rows = db.query("SELECT * FROM exam_study_log ORDER BY date")
            return rows
        except Exception:
            pass
    return _read_json_list(STUDY_LOG_FILE)


def append_study_log(entry: dict) -> bool:
    if _ensure_exam_tables():
        try:
            db.execute(
                "INSERT INTO exam_study_log (date, exam, topic, hours, type, note, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (entry.get("date"), entry.get("exam"), entry.get("topic"),
                 entry.get("hours"), entry.get("type"), entry.get("note"),
                 datetime.now().isoformat(timespec="seconds")),
            )
            return True
        except Exception as exc:
            st.warning(f"⚠ Could not write to the shared DB ({exc}) — falling back to local "
                       "JSON so this entry isn't lost. It will NOT sync to your phone until "
                       "the DB is reachable again.")
    rows = _read_json_list(STUDY_LOG_FILE)
    rows.append(entry)
    return _write_json_list(STUDY_LOG_FILE, rows)


def load_mock_scores() -> list[dict]:
    if _ensure_exam_tables():
        try:
            rows = db.query("SELECT * FROM exam_mock_scores ORDER BY date")
            # normalise 'max_score' -> 'max' key used by the rest of this module, and
            # (re)compute 'pct' rather than trusting a stored value — the DB schema
            # (task spec) has no pct column, only score/max_score.
            for r in rows:
                r.setdefault("max", r.get("max_score"))
                try:
                    r["pct"] = round(100 * float(r["score"]) / float(r["max"]), 1) if r.get("max") else None
                except (TypeError, ValueError, ZeroDivisionError):
                    r["pct"] = None
            return rows
        except Exception:
            pass
    rows = _read_json_list(MOCK_SCORES_FILE)
    for r in rows:
        if r.get("pct") is None and r.get("max"):
            try:
                r["pct"] = round(100 * float(r["score"]) / float(r["max"]), 1)
            except (TypeError, ValueError, ZeroDivisionError):
                pass
    return rows


def append_mock_score(entry: dict) -> bool:
    if _ensure_exam_tables():
        try:
            db.execute(
                "INSERT INTO exam_mock_scores (date, exam, score, max_score, note, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (entry.get("date"), entry.get("exam"), entry.get("score"),
                 entry.get("max"), entry.get("note"),
                 datetime.now().isoformat(timespec="seconds")),
            )
            return True
        except Exception as exc:
            st.warning(f"⚠ Could not write to the shared DB ({exc}) — falling back to local "
                       "JSON so this score isn't lost. It will NOT sync to your phone until "
                       "the DB is reachable again.")
    rows = _read_json_list(MOCK_SCORES_FILE)
    rows.append(entry)
    return _write_json_list(MOCK_SCORES_FILE, rows)


def load_error_log() -> list[dict]:
    if _ensure_exam_tables():
        try:
            return db.query("SELECT * FROM exam_error_log ORDER BY date")
        except Exception:
            pass
    return _read_json_list(ERROR_LOG_FILE)


def append_error_log(entry: dict) -> bool:
    if _ensure_exam_tables():
        try:
            db.execute(
                "INSERT INTO exam_error_log (date, exam, topic, count, note, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (entry.get("date"), entry.get("exam"), entry.get("topic"),
                 entry.get("count"), entry.get("note"),
                 datetime.now().isoformat(timespec="seconds")),
            )
            return True
        except Exception as exc:
            st.warning(f"⚠ Could not write to the shared DB ({exc}) — falling back to local "
                       "JSON so this entry isn't lost. It will NOT sync to your phone until "
                       "the DB is reachable again.")
    rows = _read_json_list(ERROR_LOG_FILE)
    rows.append(entry)
    return _write_json_list(ERROR_LOG_FILE, rows)


def error_log_hits_last_n_days(error_log: list[dict], topic: str, today: date, n: int = 60) -> int:
    """Count of exam_error_log 'count' values logged for `topic` in the last n days."""
    total = 0
    for row in error_log:
        if row.get("topic") != topic:
            continue
        d = _parse_date(row.get("date"))
        if d and (today - d).days <= n and (today - d).days >= 0:
            try:
                total += int(row.get("count") or 0)
            except (TypeError, ValueError):
                pass
    return total


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


ERROR_LOG_WEIGHT = 1.5
ERROR_LOG_CAP_DAYS = 60
ERROR_LOG_CAP_HITS = 3


def compute_priority_queue(exam: str, study_log: list[dict], today: date, top_n: int = 5,
                            error_log: list[dict] | None = None) -> list[dict]:
    tiers = emphasis_tier_map(exam)
    last_touch = _last_touch_dates(study_log)
    error_log = error_log or []
    rows = []
    for topic, tier in tiers.items():
        touched = last_touch.get(topic)
        staleness_days = min((today - touched).days, 30) if touched else 30
        staleness_score = staleness_days / 30.0
        stage = next_due_stage(topic, study_log, today)
        overdue_bonus = 0
        if stage and stage["due"] <= today:
            overdue_bonus = 2
        error_hits = min(error_log_hits_last_n_days(error_log, topic, today, ERROR_LOG_CAP_DAYS),
                          ERROR_LOG_CAP_HITS)
        error_bonus = ERROR_LOG_WEIGHT * error_hits
        score = TIER_WEIGHT.get(tier, 1) * staleness_score + overdue_bonus + error_bonus
        rows.append({
            "topic": topic,
            "tier": tier,
            "staleness_days": staleness_days,
            "last_touched": touched.isoformat() if touched else "never",
            "revision_overdue": overdue_bonus > 0,
            "error_hits_60d": error_hits,
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
        "v2 — persistence in the shared Vault DB (Turso/SQLite via db.py), same "
        "backend as the Vault tables but its own tables — never touches Vault "
        "sleeves/holdings/goals. Logging from your phone (cloud) and localhost "
        "now write to the SAME database."
    )
    data_source_caption()
    st.caption(f"DB backend: **{db.backend_name()}**"
               + (f" (Turso unreachable: {db.turso_error()})" if db.turso_error() else ""))

    _migrate_json_to_db_once()

    if _target_engine is None:
        st.warning(
            "⚠ `strategy/target_engine.py` could not be imported "
            f"({_target_engine_error}). The Attack Calculator tab will be degraded."
        )

    today = date.today()
    study_log = load_study_log()
    error_log = load_error_log()
    if not study_log:
        st.info(
            "No study log yet — Today/Revision tabs will show empty state until "
            "you log your first session."
        )

    # PYQ-first handoff (task #5): a "PYQs →" button on the Today queue sets
    # this session-state key; the Question Bank tab reads + clears it so the
    # pre-filter applies exactly once per click, not on every rerun after.
    _qbank_prefilter = st.session_state.pop("_qbank_prefilter_topic", None)

    (tab_today, tab_priorities, tab_qbank, tab_calc,
     tab_revision, tab_papers, tab_resources, tab_charter) = st.tabs([
        "📌 Today", "🎯 Priorities", "📝 Question Bank", "🧮 Attack Calculator",
        "🔁 Revision", "📄 Paper Library", "📚 Resources", "🧭 Charter",
    ])

    with tab_today:
        _render_today(study_log, today, error_log)
    with tab_priorities:
        _render_priorities()
    with tab_qbank:
        _render_question_bank(prefilter_topic=_qbank_prefilter)
    with tab_calc:
        _render_attack_calculator()
    with tab_revision:
        _render_revision(study_log, today)
    with tab_papers:
        _render_paper_library()
    with tab_resources:
        _render_resources(study_log, today)
    with tab_charter:
        _render_charter()


# ---------------------------------------------------------------------------
# Tab 1 — Today
# ---------------------------------------------------------------------------
def _render_today(study_log: list[dict], today: date, error_log: list[dict] | None = None) -> None:
    error_log = error_log or []
    days_to_prelims = (UPPSC_PRELIMS_DATE - today).days
    days_to_k1 = (K1_GATE_DATE - today).days

    c1, c2 = st.columns(2)
    c1.metric("UPPSC Prelims countdown", f"{days_to_prelims} days",
              help=f"Target date {UPPSC_PRELIMS_DATE.isoformat()}")
    c2.metric("K1 calibration gate", f"{days_to_k1} days",
              help=f"STRATEGY_CHARTER.md K1 — {K1_GATE_DATE.isoformat()}")
    _tier_caption(TIER_OFFICIAL, "dates fixed in STRATEGY_CHARTER.md / user's exam calendar")

    st.markdown("#### Study queue — top 5 right now")
    queue = compute_priority_queue("UPPSC", study_log, today, top_n=5, error_log=error_log)
    if queue:
        qdf = pd.DataFrame(queue)[["topic", "tier", "staleness_days", "last_touched",
                                    "revision_overdue", "error_hits_60d"]]
        qdf.columns = ["Topic", "Emphasis", "Days stale (cap 30)", "Last touched",
                       "Revision overdue", "Error-log hits (60d, cap 3)"]
        st.dataframe(qdf, use_container_width=True, hide_index=True)
        st.caption(
            "why these: score = tier weight (DEEP=3 / STANDARD=2 / ONE-PASS=1) × "
            "staleness/30, +2 if a revision is overdue, +1.5 × error-log hits for "
            "that topic in the last 60 days (capped at 3 hits). Highest score first. "
            "Primary exam = UPPSC (until 2026-12-06 per STRATEGY_CHARTER.md §2)."
        )
        st.markdown("**Jump to PYQs for a topic:**")
        pyq_cols = st.columns(min(len(queue), 5))
        for i, row in enumerate(queue):
            if pyq_cols[i % len(pyq_cols)].button(f"PYQs → {row['topic'][:22]}", key=f"pyq_btn_{i}",
                                                    use_container_width=True):
                st.session_state["_qbank_prefilter_topic"] = row["topic"]
                st.session_state["_switch_to_qbank_hint"] = True
                st.rerun()
        if st.session_state.pop("_switch_to_qbank_hint", False):
            st.info("Filter set — open the **📝 Question Bank** tab above to see it applied.")
    else:
        st.info("Priority engine has no taxonomy topics to rank — check config/topic_taxonomy_v2.json.")
    _tier_caption(TIER_CALCULATED, "deterministic score from backtest keep-list + study log + error log")

    st.markdown("#### This month")
    hrs = hours_this_month(study_log, today)
    st.progress(min(hrs / MONTH_HOUR_TARGET, 1.0),
                text=f"{hrs:.1f}h / {MONTH_HOUR_TARGET}h target this month")
    streak = current_streak(study_log, today)
    st.metric("Current streak", f"{streak} day{'s' if streak != 1 else ''}")
    _tier_caption(TIER_CALCULATED, "summed from the exam_study_log DB table")

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

    _render_hindi_drill_tracker(study_log, today)


# ---------------------------------------------------------------------------
# Hindi drill tracker (task #6) — UPPSC General Hindi = 150 MERIT marks,
# confirmed in toppers_evidence.md Part 1 (not qualifying-only, contra the
# widespread coaching claim). His weakest high-stakes area per that report.
# ---------------------------------------------------------------------------
HINDI_SYLLABUS_SECTIONS = [
    "Comprehension of a given passage",
    "संक्षेपण (precis writing)",
    "Official / semi-official letters, telegram, office order, notification, circular",
    "Word knowledge — prefix/suffix, antonyms, one-word-for-phrase, spelling & sentence correction",
    "Idioms and proverbs",
]
HINDI_DRILL_TARGET_MIN = 30


def _render_hindi_drill_tracker(study_log: list[dict], today: date) -> None:
    st.divider()
    st.markdown("#### 🇮🇳 Hindi drill tracker")
    st.caption(
        "UPPSC General Hindi is **150 MERIT marks** — counted, not qualifying-only "
        "(the widespread coaching claim to the contrary is wrong; see Resources tab). "
        "This is his weakest high-stakes area."
    )

    hindi_rows = [r for r in study_log if r.get("type") == "hindi_drill"]
    hindi_days = {d for d in (_parse_date(r.get("date")) for r in hindi_rows) if d}
    streak = 0
    cursor = today
    while cursor in hindi_days:
        streak += 1
        cursor -= timedelta(days=1)
    today_minutes = sum(
        float(r.get("hours") or 0) * 60
        for r in hindi_rows if _parse_date(r.get("date")) == today
    )

    hc1, hc2, hc3 = st.columns(3)
    hc1.metric("Hindi streak", f"{streak} day{'s' if streak != 1 else ''}")
    hc2.metric("Today", f"{today_minutes:.0f} min", help=f"Target {HINDI_DRILL_TARGET_MIN} min/day")
    hc3.metric("Total sessions logged", len(hindi_rows))
    _tier_caption(TIER_CALCULATED, "type='hindi_drill' rows in exam_study_log")

    with st.expander("Official 5-section syllabus (reference checklist)"):
        for i, section in enumerate(HINDI_SYLLABUS_SECTIONS, 1):
            st.checkbox(f"{i}. {section}", key=f"hindi_syllabus_ref_{i}",
                        help="Reference only — ticking just tracks your own read-through, "
                             "not persisted separately from this session.")
        _tier_caption(TIER_OFFICIAL, "reports/toppers_evidence.md Part 1 — official Advt A-1/E-1 "
                                      "Appendix-2; NO translation/अनुवाद section exists (verified "
                                      "absent from syllabus + actual 2018/2021 papers)")

    st.info(
        '**Siddharth Gupta, UPPCS 2023 Rank 1:** *"Dont ignore Hindi paper (especially for '
        'English medium candidates). It a low hanging fruit. Practice hindi daily."* '
        "He had scored poorly in Hindi in a previous attempt and fixed it."
    )
    _tier_caption(TIER_OFFICIAL, "reports/toppers_evidence.md Part 5 — direct topper quote")

    with st.form("hindi_drill_form", clear_on_submit=True):
        hd1, hd2 = st.columns(2)
        hd_date = hd1.date_input("Date", value=today, key="hindi_date")
        hd_minutes = hd2.number_input("Minutes", min_value=0, max_value=240, step=5,
                                       value=HINDI_DRILL_TARGET_MIN, key="hindi_minutes")
        hd_note = st.text_input("What did you drill? (optional)", key="hindi_note")
        hd_submit = st.form_submit_button(f"Log Hindi drill (target {HINDI_DRILL_TARGET_MIN} min/day)",
                                           use_container_width=True)
        if hd_submit:
            entry = {
                "date": hd_date.isoformat(), "exam": "UPPSC", "topic": "General Hindi",
                "hours": round(hd_minutes / 60.0, 3), "type": "hindi_drill", "note": hd_note,
            }
            if append_study_log(entry):
                st.success(f"Logged {hd_minutes} min Hindi drill on {hd_date.isoformat()}")
                st.rerun()


# ---------------------------------------------------------------------------
# Tab 2 — Priorities
# ---------------------------------------------------------------------------
def _render_priorities() -> None:
    st.caption(
        "📖 Doctrine: complete ~80% of Mains-relevant study BEFORE prelims — for "
        "UPPSC, UP-GK study serves both prelims and the 400-mark GS-V/VI mains "
        "papers (source: Satvik Srivastava, Rank 3, 2023)."
    )
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
def _render_question_bank(prefilter_topic: str | None = None) -> None:
    df = load_question_bank()
    if df.empty:
        _warn_missing(QBANK_REPAIRED, "Question bank")
        return

    st.caption(f"{len(df):,} questions loaded (repaired + recovered, deduplication not applied).")
    _tier_caption(TIER_CALCULATED, "question_bank_repaired.csv + question_bank_recovered.csv; "
                                    "recovered-file topics auto-classified on the fly with taxonomy v2")

    n_hindi_paper = int(df["_hindi_paper"].sum())

    fc1, fc2, fc3, fc4 = st.columns(4)
    exam_opts = ["(all)"] + sorted(x for x in df["exam"].unique() if x)
    year_opts = ["(all)"] + sorted((x for x in df["year"].unique() if x), reverse=True)
    stage_opts = ["(all)"] + sorted(x for x in df["stage"].unique() if x)
    topic_opts = ["(all)"] + sorted(x for x in df["topic"].unique() if x)

    if prefilter_topic and prefilter_topic in topic_opts:
        st.session_state["qb_topic"] = prefilter_topic
        st.success(f"Pre-filtered to **{prefilter_topic}** (from the Today queue's PYQs button).")

    exam_f = fc1.selectbox("Exam", exam_opts, key="qb_exam")
    year_f = fc2.selectbox("Year", year_opts, key="qb_year")
    stage_f = fc3.selectbox("Stage", stage_opts, key="qb_stage")
    topic_f = fc4.selectbox("Topic", topic_opts, key="qb_topic")
    search = st.text_input("Text search", key="qb_search")

    qc1, qc2 = st.columns(2)
    show_hindi_papers = qc2.checkbox(
        f"include Hindi-paper rows ({n_hindi_paper:,} hidden)", value=False, key="qb_show_hindi")

    filtered = df
    if not show_hindi_papers:
        filtered = filtered[~filtered["_hindi_paper"]]
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

    # Clean each remaining row's display text BEFORE the low-quality hide/show
    # decision — a row whose raw OCR looked garbled but whose cleaned stem is
    # now legible English should show even with "show low-quality" unchecked
    # (task requirement: re-apply is_english_like AFTER cleaning).
    cleaned_rows = []
    for _, row in filtered.iterrows():
        raw_text = row.get("question_text", "") or ""
        clean_stem, parsed_opts = clean_question_display(raw_text)
        csv_opts = [_strip_garbage(_clean_option(row.get(c, "")))
                    for c in ("option_a", "option_b", "option_c", "option_d")]
        non_empty_csv_opts = [o for o in csv_opts if o]
        # Prefer real CSV option columns when they survive cleaning; else
        # fall back to the options parsed out of the stem text itself.
        if len(non_empty_csv_opts) >= 2:
            display_opts = csv_opts
        elif len(parsed_opts) >= 2:
            display_opts = parsed_opts
        else:
            display_opts = []
        is_clean_english = is_english_like(clean_stem)
        cleaned_rows.append({
            "row": row,
            "clean_stem": clean_stem,
            "display_opts": display_opts,
            "is_low_quality": not is_clean_english,
        })

    n_low_quality_after_clean = sum(1 for r in cleaned_rows if r["is_low_quality"])
    show_low_quality = qc1.checkbox(
        f"show low-quality OCR rows ({n_low_quality_after_clean:,} hidden)",
        value=False, key="qb_show_low_quality")
    st.caption(
        "Default view hides rows whose CLEANED stem still contains fewer than 3 distinct common "
        "English words (garbled Devanagari-as-Latin OCR noise, e.g. \"qTma ap ffi-ffitr3 q5q dr alis\", "
        "survives even after garbage-token stripping) and rows from papers matching "
        "hindi/language/literature/essay (inherently Hindi-content, so their OCR is real Hindi text "
        "mis-rendered, not fixable by the English-word check)."
    )
    if not show_low_quality:
        cleaned_rows = [r for r in cleaned_rows if not r["is_low_quality"]]

    st.write(f"**{len(cleaned_rows):,}** questions match.")

    page_size = 20
    n_pages = max(1, (len(cleaned_rows) + page_size - 1) // page_size)
    page = st.number_input("Page", min_value=1, max_value=n_pages, value=1, step=1)
    start = (page - 1) * page_size
    page_rows = cleaned_rows[start:start + page_size]

    for item in page_rows:
        row = item["row"]
        header = f"{row.get('exam','?')} {row.get('year','?')} · {row.get('stage','?')} · {row.get('topic','UNCLASSIFIED')}"
        with st.container(border=True):
            st.markdown(f"**{header}**")
            st.write(item["clean_stem"])
            display_opts = item["display_opts"]
            # Practice mode only when there's something real to reveal — a
            # row with <2 usable options gets no expander rather than an
            # empty/near-empty one (task fix #2, preserved).
            if len(display_opts) >= 2:
                with st.expander("Practice mode — reveal options"):
                    labels = ["a", "b", "c", "d"]
                    for lbl, opt in zip(labels, display_opts):
                        if opt:
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
    _tier_caption(TIER_CALCULATED, "derived from the exam_study_log DB table — no separate state file")


# ---------------------------------------------------------------------------
# Tab (new) — Paper Library (task #7): the harvested official papers. This is
# his mock-exam source — real past papers, not third-party recompilations.
# ---------------------------------------------------------------------------
_PAPER_FILENAME_RE = re.compile(
    r"^(?P<exam>uppsc|bpsc|jpsc)_src_[0-9a-f]+_(?P<rest>.+)\.pdf$", re.IGNORECASE)
_PAPER_YEAR_RE = re.compile(r"(20\d{2})")
_PAPER_STAGE_RE = re.compile(r"\b(pre|prelim|mains|main)\b", re.IGNORECASE)


def _parse_paper_filename(fname: str) -> dict:
    """Best-effort exam/year/paper parse from a harvested PDF's filename.

    Never invents data — any field that can't be found from the filename
    text renders as '?' rather than a guess.
    """
    m = _PAPER_FILENAME_RE.match(fname)
    exam = m.group("exam").upper() if m else "?"
    rest = m.group("rest") if m else fname
    rest_words = rest.replace("-", " ")
    ym = _PAPER_YEAR_RE.search(rest_words)
    year = ym.group(1) if ym else "?"
    sm = _PAPER_STAGE_RE.search(rest_words)
    stage = "Prelims" if sm and sm.group(1).lower().startswith("pre") else (
        "Mains" if sm else "?")
    # paper label = whatever's left after stripping exam/year/stage boilerplate words
    label = rest_words
    return {"exam": exam, "year": year, "stage": stage, "paper_label": label, "filename": fname}


@st.cache_data(show_spinner=False)
def load_paper_manifest() -> pd.DataFrame:
    if not MANIFEST_FILE.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(MANIFEST_FILE, dtype=str, keep_default_na=False)
    except Exception:
        return pd.DataFrame()


def _render_paper_library() -> None:
    st.markdown("#### 📄 Paper Library — harvested official papers")
    st.caption(
        "His mock-exam source: real past papers, not third-party recompilations. "
        "Filtered to UPPSC-harvested files (uppsc_* filenames) plus any file with a "
        "manifest row — see reports/uppsc_harvest_report.md for provenance."
    )

    if not LOCAL_MODE:
        st.info(
            "📄 Papers available in **local mode only** — the PDF corpus "
            f"({PDF_DIR}) isn't bundled into this cloud deployment (too large / "
            "copyright-sensitive to commit). Open this app on the Mac to download."
        )
        return

    if not PDF_DIR.exists():
        _warn_missing(PDF_DIR, "PDF corpus directory")
        return

    manifest = load_paper_manifest()
    manifest_files = set()
    if not manifest.empty and "local_file" in manifest.columns:
        manifest_files = {Path(p).name for p in manifest["local_file"] if p}

    all_files = sorted(p.name for p in PDF_DIR.glob("*.pdf"))
    uppsc_files = [f for f in all_files if f.lower().startswith("uppsc_")]
    # union with any manifest-listed file not already caught by the uppsc_ prefix
    extra_manifest_files = sorted(manifest_files - set(uppsc_files))
    shown_files = uppsc_files + [f for f in extra_manifest_files if (PDF_DIR / f).exists()]

    if not shown_files:
        st.info("No UPPSC papers found in the local PDF corpus yet.")
        return

    rows = [_parse_paper_filename(f) for f in shown_files]
    rows.sort(key=lambda r: (r["year"], r["stage"], r["paper_label"]), reverse=True)

    st.write(f"**{len(rows)}** papers available.")
    fc1, fc2 = st.columns(2)
    year_opts = ["(all)"] + sorted({r["year"] for r in rows}, reverse=True)
    stage_opts = ["(all)"] + sorted({r["stage"] for r in rows})
    year_f = fc1.selectbox("Year", year_opts, key="paper_year")
    stage_f = fc2.selectbox("Stage", stage_opts, key="paper_stage")

    filtered_rows = rows
    if year_f != "(all)":
        filtered_rows = [r for r in filtered_rows if r["year"] == year_f]
    if stage_f != "(all)":
        filtered_rows = [r for r in filtered_rows if r["stage"] == stage_f]

    for r in filtered_rows:
        path = PDF_DIR / r["filename"]
        with st.container(border=True):
            cc1, cc2 = st.columns([4, 1])
            cc1.markdown(f"**{r['exam']} {r['year']} · {r['stage']}** — {r['paper_label']}")
            try:
                data = path.read_bytes()
                cc2.download_button("Download", data=data, file_name=r["filename"],
                                     mime="application/pdf", key=f"dl_{r['filename']}",
                                     use_container_width=True)
            except Exception as exc:
                cc2.caption(f"unreadable: {exc}")
    _tier_caption(TIER_OFFICIAL, "data/raw/pdfs/ harvested from official commission sites — "
                                  "see reports/uppsc_harvest_report.md for the fetch mechanism/provenance")


# ---------------------------------------------------------------------------
# Tab 6 — Resources (static from toppers_evidence.md)
# ---------------------------------------------------------------------------
def _render_resources(study_log: list[dict] | None = None, today: date | None = None) -> None:
    study_log = study_log or []
    today = today or date.today()
    md = load_toppers_md()
    if md is None:
        _warn_missing(TOPPERS_FILE, "Toppers evidence report")
        return

    st.markdown("#### Convergent core-GS books (by independent topper count)")
    book_rows = [
        {"Book": "M. Laxmikanth", "Named by": 9, "Subject": "Polity", "English?": "Yes"},
        {"Book": "NCERT VI–XII", "Named by": 8, "Subject": "Foundation", "English?": "Yes"},
        {"Book": "Spectrum / Rajiv Ahir", "Named by": 6, "Subject": "Modern History", "English?": "Yes"},
        {"Book": "RS Sharma / Satish Chandra (old NCERT)", "Named by": 3, "Subject": "History", "English?": "Yes"},
        {"Book": "G.C. Leong", "Named by": 2, "Subject": "Physical Geography", "English?": "Yes"},
        {"Book": "Shankar IAS", "Named by": 2, "Subject": "Environment", "English?": "Yes"},
        {"Book": "Lucent (Gen Science)", "Named by": 2, "Subject": "General Science", "English?": "Yes"},
        {"Book": "Ramesh Singh", "Named by": 1, "Subject": "Economy (WEAK — single source)", "English?": "Yes"},
        {"Book": "Know Your State Jharkhand (Arihant, ISBN 9789378162992)", "Named by": 1,
         "Subject": "Jharkhand state GK (WEAK — single confirmation, but strongest English "
                    "state pick found)", "English?": "Yes — CONFIRMED"},
        {"Book": "Samanya Hindi (Aaditya Publication)", "Named by": 1,
         "Subject": "General Hindi paper (WEAK — the ONLY topper-cited Hindi-paper book found)",
         "English?": "No — Hindi paper"},
    ]
    st.dataframe(pd.DataFrame(book_rows), use_container_width=True, hide_index=True)
    _tier_caption(TIER_WEAK, "convergent-count survivorship evidence — reports/toppers_evidence.md Part 2; "
                              "single-named rows are WEAK, never required reading (Charter G4). Citation "
                              "counts = distinct named toppers only, not a popularity poll.")

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

    st.divider()
    _render_current_affairs_checklist(study_log, today)

    st.divider()
    _render_resource_links()

    with st.expander("Full toppers_evidence.md"):
        st.markdown(md)


# ---------------------------------------------------------------------------
# Current-affairs 12-month checklist (task #9)
# ---------------------------------------------------------------------------
CA_MONTHS = ["Dec-2025", "Jan-2026", "Feb-2026", "Mar-2026", "Apr-2026", "May-2026",
             "Jun-2026", "Jul-2026", "Aug-2026", "Sep-2026", "Oct-2026", "Nov-2026"]


def _render_current_affairs_checklist(study_log: list[dict], today: date) -> None:
    st.markdown("#### 🗞️ Current-affairs coverage — 12-month grid (Dec-2025 → Nov-2026)")
    st.caption(
        "Prelims current-affairs share measured **13–30%** (CALCULATED from the paper corpus) "
        "— a one-year window is the minimum; this grid runs Dec-2025 through Nov-2026 to cover "
        "the full run-up to the 2026-12-06 UPPSC Prelims."
    )
    done_months = {r.get("topic") for r in study_log if r.get("type") == "current_affairs"}

    cols = st.columns(4)
    for i, month in enumerate(CA_MONTHS):
        checked = month in done_months
        new_val = cols[i % 4].checkbox(month, value=checked, key=f"ca_month_{month}")
        if new_val and not checked:
            entry = {"date": today.isoformat(), "exam": "UPPSC", "topic": month,
                      "hours": 0, "type": "current_affairs", "note": "monthly CA coverage marked done"}
            if append_study_log(entry):
                st.rerun()
        elif checked and not new_val:
            st.caption(f"(un-checking {month} isn't persisted — logs are append-only; "
                       "log a fresh entry if you genuinely need to redo this month)")
    n_done = len(done_months & set(CA_MONTHS))
    st.progress(n_done / len(CA_MONTHS), text=f"{n_done}/{len(CA_MONTHS)} months covered")
    _tier_caption(TIER_CALCULATED, "13-30% range measured from data/processed backtest corpus "
                                    "(topic_taxonomy_v2.json current-affairs tag share); persisted "
                                    "as type='current_affairs' rows in exam_study_log, topic=month string")


# ---------------------------------------------------------------------------
# Resource links (task #10) — real, honest links only. No fabricated channel
# URLs: topper-cited YouTube channels render as search-query links, never a
# guessed /channel/ or /@handle URL, since none could be verified.
# ---------------------------------------------------------------------------
def _render_resource_links() -> None:
    st.markdown("#### 🔗 Resource links")

    st.markdown("**Official (verified URLs):**")
    official_links = [
        ("UPPSC advertisement / notifications", "https://uppsc.up.nic.in/"),
        ("UPPSC previous question papers page",
         "https://uppsc.up.nic.in/OuterPages/PreQuesPapers.aspx?ID=PrevQues"),
        ("JPSC official site", "https://www.jpsc.gov.in/"),
        ("BPSC official site", "https://bpsc.bihar.gov.in/"),
        ("PRS Legislative Research — budget analysis", "https://prsindia.org/budgets"),
    ]
    for label, url in official_links:
        st.markdown(f"- **OFFICIAL** [{label}]({url}) — `{url}`")
    _tier_caption(TIER_OFFICIAL, "URLs verified directly against the commission/PRS domains")

    st.markdown("**Topper-cited YouTube channels** (search links — exact channel URLs not "
                "independently verified, so these deliberately point at a YouTube search, "
                "never a guessed channel handle):")
    yt_channels = [
        ("Study for Civil Services", "Gyan Prakash Mishra — UP current affairs/budget",
         "cited by Amit Gupta (UPPCS 2019+2020 selected)", "WEAK — single citation"),
        ("UPAAM Abhyuday Cell UPPCS", "free e-content authored by serving SDMs",
         "cited in toppers_evidence.md Part 2 (UP state-specific section)", "WEAK — single mention"),
        ("ONLY IAS UPPCS", "general UPPCS prep channel", "referenced alongside UPAAM/Dhyeya in "
         "the same evidence pass", "WEAK — not independently multi-sourced"),
        ("Dhyeya IAS UPPCS", "general UPPCS prep channel", "referenced alongside UPAAM/ONLY IAS",
         "WEAK — not independently multi-sourced"),
        ("KR Studies", "BPSC geography/economy", "referenced in the BPSC resource pass",
         "WEAK — single mention"),
        ("SK Seth data interpretation", "BPSC data-interpretation/CSAT-style content",
         "referenced in the BPSC resource pass", "WEAK — single mention"),
    ]
    for name, desc, who, tier in yt_channels:
        q = name.replace(" ", "+")
        st.markdown(
            f"- [{name}](https://www.youtube.com/results?search_query={q}) — {desc}. "
            f"**Who cited it:** {who}. **Evidence tier: {tier}**."
        )
    _tier_caption(TIER_WEAK, "reports/toppers_evidence.md — no channel URL is fabricated; every "
                              "entry above is a live YouTube search link, not a claimed exact channel")

    st.markdown("**Free PDFs (search links, not direct downloads — publishers change URLs often):**")
    free_pdfs = [
        ("Vision IAS Monthly Current Affairs (PDF)", "Vision+IAS+monthly+current+affairs+pdf"),
        ("Bihar Economic Survey (Finance Dept, Govt of Bihar)", "Bihar+Economic+Survey+finance+department+pdf"),
        ("Jharkhand Economic Survey", "Jharkhand+Economic+Survey+pdf"),
    ]
    for label, q in free_pdfs:
        st.markdown(f"- [{label}](https://www.google.com/search?q={q}+filetype:pdf)")
    _tier_caption(TIER_ESTIMATED, "search-query links, not verified direct PDF URLs — "
                                   "publisher/government hosting paths change; verify freshness before relying on one")


# ---------------------------------------------------------------------------
# Mock schedule generator (task #8) — one full-length mock/week until the
# final 5 weeks (2026-10-31), then 2-3/week; matched against logged mock
# scores so each dated slot shows done/missed/upcoming.
# ---------------------------------------------------------------------------
MOCK_SCHEDULE_END = UPPSC_PRELIMS_DATE  # 2026-12-06
MOCK_SCHEDULE_RAMP_DATE = date(2026, 10, 31)


def generate_mock_schedule(start: date, end: date = MOCK_SCHEDULE_END,
                            ramp_date: date = MOCK_SCHEDULE_RAMP_DATE) -> list[date]:
    """One mock/week from `start` to `ramp_date`, then 2-3/week (alternating
    2 and 3 to average ~2.5) from `ramp_date` to `end`. Siddharth Gupta
    (UPPCS 2023 Rank 1) prescription — see toppers_evidence.md Part 6."""
    dates = []
    cursor = start
    while cursor <= min(ramp_date, end):
        dates.append(cursor)
        cursor += timedelta(days=7)
    # Final stretch: alternate 3-days-apart / 4-days-apart spacing so a week
    # gets ~2-3 mocks (7 days / 2.5 ≈ every 2.8 days) without a rigid single
    # cadence claiming false precision.
    toggle = True
    while cursor <= end:
        dates.append(cursor)
        cursor += timedelta(days=3 if toggle else 4)
        toggle = not toggle
    return dates


def _render_mock_schedule_generator() -> None:
    st.markdown("#### 🗓️ Mock schedule generator")
    st.caption(
        "One full-length mock/week from today to 2026-10-31, then 2-3/week into "
        "the 2026-12-06 UPPSC Prelims. **Source: Siddharth Gupta (UPPCS 2023 "
        "Rank 1) prescription** — toppers_evidence.md Part 6."
    )
    today = date.today()
    scores = load_mock_scores()
    logged_dates = {_parse_date(s.get("date")) for s in scores if _parse_date(s.get("date"))}

    if not scores:
        st.warning(
            "🚨 **Sit UPPSC 2023 prelims COLD this week** — baseline before study "
            "distorts it. The paper is in the **📄 Paper Library** tab."
        )

    schedule = generate_mock_schedule(today)
    rows = []
    for d in schedule:
        # "done" if any mock is logged within ±3 days of the slot (a real slot
        # isn't going to land on the exact calendar day every time).
        hit = any(abs((d - ld).days) <= 3 for ld in logged_dates)
        if hit:
            status = "✅ done"
        elif d < today:
            status = "❌ missed"
        else:
            status = "⚪ upcoming"
        rows.append({"Date": d.isoformat(), "Status": status})

    sdf = pd.DataFrame(rows)
    st.dataframe(sdf, use_container_width=True, hide_index=True, height=300)
    n_done = sum(1 for r in rows if r["Status"] == "✅ done")
    n_missed = sum(1 for r in rows if r["Status"] == "❌ missed")
    st.caption(f"{n_done} done · {n_missed} missed · {len(rows) - n_done - n_missed} upcoming "
               f"— matched against exam_mock_scores within a ±3 day window per slot.")
    _tier_caption(TIER_WEAK, "cadence is Siddharth Gupta's single-topper prescription "
                              "(toppers_evidence.md Part 6), not a cross-topper consensus")


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
    _render_mock_schedule_generator()

    st.divider()
    st.markdown("#### Mock-score tracker")
    st.caption(
        "Add topics you got wrong below — this feeds the Today tab's priority queue "
        "(+1.5 per topic hit, last 60 days, capped at 3) so weak-spot topics surface "
        "faster than plain staleness alone would put them."
    )
    topics = taxonomy_topics()
    with st.form("mock_score_form", clear_on_submit=True):
        c1, c2, c3, c4 = st.columns(4)
        m_date = c1.date_input("Date", value=date.today(), key="mock_date")
        m_exam = c2.selectbox("Exam", ["UPPSC", "BPSC", "JPSC"], key="mock_exam")
        m_score = c3.number_input("Score", min_value=0.0, step=0.5, key="mock_score")
        m_max = c4.number_input("Out of (max)", min_value=1.0, step=1.0, value=200.0, key="mock_max")
        st.markdown("**Questions I got wrong, by topic:**")
        wrong_topics = st.multiselect("Topics missed", topics if topics else [], key="mock_wrong_topics")
        wrong_counts = {}
        if wrong_topics:
            wc_cols = st.columns(min(len(wrong_topics), 4))
            for i, t in enumerate(wrong_topics):
                wrong_counts[t] = wc_cols[i % len(wc_cols)].number_input(
                    f"# wrong — {t[:20]}", min_value=1, max_value=50, value=1, step=1,
                    key=f"mock_wrong_count_{t}")
        submitted = st.form_submit_button("Log mock score", use_container_width=True)
        if submitted:
            entry = {
                "date": m_date.isoformat(), "exam": m_exam,
                "score": m_score, "max": m_max,
            }
            ok = append_mock_score(entry)
            error_ok = True
            for t, cnt in wrong_counts.items():
                error_ok = append_error_log({
                    "date": m_date.isoformat(), "exam": m_exam, "topic": t,
                    "count": cnt, "note": "from mock-score form",
                }) and error_ok
            if ok:
                msg = f"Logged {m_exam} {m_score}/{m_max:g} on {m_date.isoformat()}"
                if wrong_counts:
                    msg += f" + {len(wrong_counts)} error-log topic(s)"
                st.success(msg)
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
    _tier_caption(TIER_CALCULATED, "exam_mock_scores DB table; 55% reference is ESTIMATED, not an official cutoff")
