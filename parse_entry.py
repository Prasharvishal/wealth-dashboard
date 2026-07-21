"""
parse_entry.py — deterministic natural-language quick-entry parser for the
MAVI Vault dashboard.

NO LLM. NO API key. NO network call. Pure stdlib (re + string ops only).
Everything here is regex + heuristics, on purpose — the entire point of this
module is that it must work offline, for free, forever, with zero drift.

Public surface: parse_entry(text, holdings, sleeves) -> dict. See its
docstring for the exact return shape. This module NEVER writes to the
database and NEVER raises on bad input — worst case is intent="UNKNOWN".
"""

import re

# ---------------------------------------------------------------------------
# Amount parsing
# ---------------------------------------------------------------------------

# Recognizes: 10000 | 10,000 | 5k | 1 lakh | 1L | 1.5L | 2 cr | 2 crore
# Currency/unit cue required to disambiguate "which number is the amount"
# when multiple numbers appear in the sentence.
_NUM = r"(\d+(?:,\d{2,3})*(?:\.\d+)?)"

_AMOUNT_CUE_RE = re.compile(
    r"(?:₹|rs\.?|inr|rupees?)\s*" + _NUM +
    r"|" + _NUM + r"\s*(?:₹|rs\.?|inr|rupees?)"
    r"|" + _NUM + r"\s*(k|l|lac|lacs|lakh|lakhs|cr|crore|crores)\b"
    r"|(k|l|lac|lacs|lakh|lakhs|cr|crore|crores)\s*" + _NUM,
    re.IGNORECASE,
)

_UNIT_MULT = {
    "k": 1_000,
    "l": 100_000, "lac": 100_000, "lacs": 100_000, "lakh": 100_000, "lakhs": 100_000,
    "cr": 10_000_000, "crore": 10_000_000, "crores": 10_000_000,
}

# Bare number fallback (no cue at all): plain integers/decimals, optionally
# comma-grouped. Used only when no cued amount is found.
_BARE_NUM_RE = re.compile(r"\b" + _NUM + r"\b")

# P&L phrases: these numbers are NEVER the transaction amount — they go to
# notes so the user sees them, but they don't hijack the amount field.
_PNL_RE = re.compile(
    r"\b(negative|down|loss|lost|profit|gain|up)\s+"
    r"(?:of\s+)?(₹|rs\.?|inr)?\s*" + _NUM + r"\s*(k|l|lac|lacs|lakh|lakhs|cr|crore|crores)?",
    re.IGNORECASE,
)


def _num_to_float(raw):
    return float(raw.replace(",", ""))


def _parse_amount(text):
    """Returns (amount, notes) — amount is None if nothing found. P&L
    phrases are stripped into notes and excluded from amount candidacy."""
    notes = []
    working = text

    pnl_spans = []
    for m in _PNL_RE.finditer(text):
        word = m.group(1).lower()
        num = _num_to_float(m.group(3))
        unit = (m.group(4) or "").lower()
        num *= _UNIT_MULT.get(unit, 1)
        sign = "-" if word in ("negative", "down", "loss", "lost") else "+"
        notes.append(f"P&L mention ({word} {m.group(3)}{m.group(4) or ''}) "
                     f"noted, not treated as the amount: {sign}{num:,.0f}")
        pnl_spans.append(m.span())

    # Mask out the P&L number spans so the cued-amount search can't reuse them.
    def _masked(s, spans):
        chars = list(s)
        for a, b in spans:
            for i in range(a, b):
                chars[i] = "\0"
        return "".join(chars)

    masked = _masked(working, pnl_spans)

    m = _AMOUNT_CUE_RE.search(masked)
    if m:
        groups = m.groups()
        # Currency-cue forms (groups 0/1) → plain rupees.
        if groups[0]:
            return _num_to_float(groups[0]), notes
        if groups[1]:
            return _num_to_float(groups[1]), notes
        # Unit-suffix forms: number then unit (groups 2/3), or unit then
        # number (groups 4/5) e.g. "5k", "k5" (rare but handled).
        if groups[2] is not None and groups[3]:
            return _num_to_float(groups[2]) * _UNIT_MULT.get(groups[3].lower(), 1), notes
        if groups[4] and groups[5] is not None:
            return _num_to_float(groups[5]) * _UNIT_MULT.get(groups[4].lower(), 1), notes

    # No cued amount — fall back to the first bare number not inside a P&L span.
    bm = _BARE_NUM_RE.search(masked)
    if bm:
        return _num_to_float(bm.group(1)), notes

    return None, notes


# ---------------------------------------------------------------------------
# Kind parsing
# ---------------------------------------------------------------------------

_KIND_PATTERNS = [
    (re.compile(r"\bsip\b", re.IGNORECASE), "SIP"),
    (re.compile(r"\b(sold|sell|exited|exit)\b", re.IGNORECASE), "SELL"),
    (re.compile(r"\b(dividend|payout)\b", re.IGNORECASE), "DIVIDEND"),
    (re.compile(r"\b(matured|maturity)\b", re.IGNORECASE), "MATURITY"),
    (re.compile(r"\b(invested|invest|bought|buy|purchase[d]?|put)\b", re.IGNORECASE), "BUY"),
]


def _parse_kind(text):
    for pat, kind in _KIND_PATTERNS:
        if pat.search(text):
            return kind
    return None


# ---------------------------------------------------------------------------
# Intent parsing
# ---------------------------------------------------------------------------

_DEPLOY_QUERY_RE = re.compile(
    r"\bwhere\b|\bhow should\b|\bsuggest\b|\binvest\?|\bwhere should\b|\bwhat should i (do|invest)\b"
    # sms-speak / shorthand variants ("i hv 50000 whr shld i invest")
    r"|\bwhr\b|\bshld\b|\bhw\b|\bkaha+n?\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Asset matching
# ---------------------------------------------------------------------------

# Synonym table: spoken phrase (lowercase) -> a resolver keyword or literal
# note. Order matters — longer/more specific phrases first so e.g. "junior"
# doesn't get shadowed by a generic "nifty" match.
_SYNONYMS = [
    (re.compile(r"\bjunior\b|\bnext\s*50\b|\bjuniorbees\b", re.IGNORECASE), "JUNIORBEES", None),
    (re.compile(r"\bnifty\s*50\b|\bniftybees\b|\bnifty\b(?!\s*next)", re.IGNORECASE), "NIFTYBEES", None),
    (re.compile(r"\bgold\b|\bgoldbees\b|\bsgb\b", re.IGNORECASE), "GOLDBEES", None),
    (re.compile(r"\bmid\s*cap\b|\bmidcap\b", re.IGNORECASE), "MIDCAP", None),
    (re.compile(r"\bus\b|\btesla\b|\bglobal\b|\bs&p\b|\bsp500\b|\bs&p500\b", re.IGNORECASE), "GLOBAL", None),
    (re.compile(r"\bliquid\b|\bemergency\b", re.IGNORECASE), "EMERGENCY", None),
    (re.compile(r"\bcrypto\b|\bbtc\b|\bbitcoin\b|\beth\b|\bethereum\b", re.IGNORECASE), None,
     "crypto is Sentinel-only, not tracked in Vault"),
]

_STOPWORDS = {
    "i", "invested", "invest", "bought", "buy", "purchase", "purchased", "put",
    "sip", "sold", "sell", "exited", "exit", "dividend", "payout", "matured",
    "maturity", "in", "into", "on", "of", "today", "yesterday", "the", "a",
    "an", "to", "for", "have", "has", "rs", "rupees", "inr", "negative",
    "down", "loss", "lost", "profit", "gain", "up", "where", "should",
    "how", "suggest", "invest?", "and", "with",
}


def _tokenize(text):
    toks = re.findall(r"[a-z0-9]+", text.lower())
    return [t for t in toks if t not in _STOPWORDS and not re.fullmatch(r"\d+(\.\d+)?", t)]


def _match_asset(text, holdings, notes):
    """Returns (asset_match_dict_or_None, asset_guess_name_or_None,
    confidence_str)."""
    holdings = holdings or []
    residual_tokens = _tokenize(text)
    lowered = text.lower()

    synonym_hit_note = None
    synonym_key = None
    for pat, key, note in _SYNONYMS:
        if pat.search(lowered):
            synonym_key = key
            synonym_hit_note = note
            break

    if synonym_hit_note:
        notes.append(synonym_hit_note)
        if synonym_key is None:
            return None, None, "low"

    # Build candidate search terms: the synonym resolver key (if any) plus
    # residual tokens from the raw text (asset names are user free text, so
    # substring + token overlap against asset_name is the general path).
    search_terms = []
    if synonym_key:
        search_terms.append(synonym_key)
    search_terms.extend(residual_tokens)

    def _score(holding):
        name = (holding.get("asset_name") or "").lower()
        sleeve = (holding.get("sleeve") or "").lower()
        score = 0
        # Synonym-key resolution: special-cased sleeve/keyword routing.
        if synonym_key == "MIDCAP" and ("mid-cap" in name or "midcap" in name or "mid cap" in name):
            score += 5
        if synonym_key == "GLOBAL" and ("global" in sleeve or "us" in name.split()
                                         or "tesla" in name or "s&p" in name):
            score += 5
        if synonym_key == "EMERGENCY" and ("emergency" in sleeve or "liquid" in sleeve
                                            or "liquid" in name):
            score += 5
        if synonym_key in ("NIFTYBEES", "JUNIORBEES", "GOLDBEES") and synonym_key.lower() in name.replace(" ", ""):
            score += 5
        if synonym_key == "NIFTYBEES" and "nifty 50" in sleeve:
            score += 3
        if synonym_key == "JUNIORBEES" and "next 50" in sleeve:
            score += 3
        if synonym_key == "GOLDBEES" and "gold" in sleeve:
            score += 3
        # Generic substring + token overlap against free-text asset_name.
        name_tokens = set(re.findall(r"[a-z0-9]+", name))
        for t in residual_tokens:
            if t in name:
                score += 2
            if t in name_tokens:
                score += 1
        return score

    scored = [(_score(h), h) for h in holdings]
    scored = [s for s in scored if s[0] > 0]
    scored.sort(key=lambda s: s[0], reverse=True)

    if not scored:
        guess = synonym_key or (residual_tokens[0] if residual_tokens else None)
        return None, guess, "low"

    if len(scored) >= 2 and scored[0][0] == scored[1][0]:
        # Tie — ambiguous, don't guess wrong.
        guess = scored[0][1].get("asset_name")
        return None, guess, "low"

    best_score, best = scored[0]
    confidence = "high" if best_score >= 5 else ("medium" if best_score >= 2 else "low")
    if confidence == "low":
        return None, best.get("asset_name"), "low"
    return best, best.get("asset_name"), confidence


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def parse_entry(text, holdings=None, sleeves=None):
    """Deterministically parse a free-text quick-entry line.

    Returns a dict:
      intent: "RECORD" | "DEPLOY_QUERY" | "UNKNOWN"
      kind: "BUY"|"SIP"|"SELL"|"DIVIDEND"|"MATURITY"|None
      amount: float or None
      asset_match: holding dict or None
      asset_guess_name: str or None
      confidence: "high"|"medium"|"low"
      raw: original text
      notes: list[str]
    """
    holdings = holdings or []
    raw = text or ""
    notes = []

    out = {
        "intent": "UNKNOWN",
        "kind": None,
        "amount": None,
        "asset_match": None,
        "asset_guess_name": None,
        "confidence": "low",
        "raw": raw,
        "notes": notes,
    }

    text = raw.strip()
    if not text:
        notes.append("empty input")
        return out

    amount, pnl_notes = _parse_amount(text)
    notes.extend(pnl_notes)
    kind = _parse_kind(text)
    asset_match, asset_guess, asset_conf = _match_asset(text, holdings, notes)

    out["amount"] = amount
    out["kind"] = kind
    out["asset_match"] = asset_match
    out["asset_guess_name"] = asset_guess

    is_deploy_query = bool(_DEPLOY_QUERY_RE.search(text))
    # A guess with no confident holding match at all (asset_match is None)
    # is weak signal — deploy-query phrasing ("where should I invest 2 lakh")
    # takes priority over a leftover word like "lakh" or "something" getting
    # mistaken for an asset guess.
    has_asset_signal = asset_match is not None or (asset_guess is not None and not is_deploy_query)

    if is_deploy_query and amount is not None and asset_match is None:
        out["intent"] = "DEPLOY_QUERY"
        out["confidence"] = "high"
        return out

    if amount is not None and has_asset_signal:
        out["intent"] = "RECORD"
        if kind is None:
            kind = "BUY"
            out["kind"] = kind
            notes.append("no verb found — defaulted kind to BUY")
        out["confidence"] = asset_conf if asset_match is not None else "low"
        if asset_match is None:
            notes.append("no confident holding match — pick the right one")
        return out

    if amount is not None and kind is None and asset_match is None:
        # Bare amount ("50k") — no verb, no asset: the useful answer is the
        # deploy plan, not a half-empty record card.
        out["intent"] = "DEPLOY_QUERY"
        out["confidence"] = "medium"
        notes.append("amount only — showing the deploy plan; say 'bought 50k gold' to record")
        return out

    if amount is not None and is_deploy_query:
        # amount + deploy-query phrasing but asset signal leaked in anyway —
        # still a deploy query, the asset words were noise.
        out["intent"] = "DEPLOY_QUERY"
        out["confidence"] = "medium"
        return out

    if amount is None and is_deploy_query:
        notes.append("looked like a deploy question but no amount found")
        return out

    if amount is not None and not has_asset_signal and kind is not None:
        notes.append("amount and a kind verb found, but no matching asset — "
                      "say which fund/holding")
        return out

    notes.append("couldn't parse — try like 'bought 5000 gold' or 'invested 20k niftybees'")
    return out


# ---------------------------------------------------------------------------
# Self-tests
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    _HOLDINGS = [
        {"id": 1, "asset_name": "NIFTYBEES", "sleeve": "Nifty 50 (core)"},
        {"id": 2, "asset_name": "JUNIORBEES", "sleeve": "Nifty Next 50"},
        {"id": 3, "asset_name": "GOLDBEES", "sleeve": "Gold"},
        {"id": 4, "asset_name": "HDFC Mid-Cap Fund", "sleeve": "Tactical / thematic"},
        {"id": 5, "asset_name": "Motilal Oswal S&P 500 FoF", "sleeve": "Global / US"},
        {"id": 6, "asset_name": "Tesla Inc", "sleeve": "Global / US"},
        {"id": 7, "asset_name": "ICICI Prudential Liquid Fund", "sleeve": "🚨 Emergency (liquid)"},
        {"id": 8, "asset_name": "Reliance Industries", "sleeve": "Direct Stocks"},
    ]
    _SLEEVES = ["Nifty 50 (core)", "Direct Stocks", "Global / US",
                "Tactical / thematic", "Nifty Next 50", "Gold"]

    _passed = 0
    _failed = 0

    def _check(desc, cond):
        global _passed, _failed
        if cond:
            _passed += 1
            print(f"PASS  {desc}")
        else:
            _failed += 1
            print(f"FAIL  {desc}")

    def _t(text):
        return parse_entry(text, _HOLDINGS, _SLEEVES)

    r = _t("invested 10000 in niftybees")
    _check("invested 10000 in niftybees -> RECORD/BUY/10000/NIFTYBEES",
           r["intent"] == "RECORD" and r["kind"] == "BUY" and r["amount"] == 10000
           and r["asset_match"] and r["asset_match"]["asset_name"] == "NIFTYBEES")

    r = _t("bought 5k gold today")
    _check("bought 5k gold today -> BUY/5000/GOLDBEES",
           r["intent"] == "RECORD" and r["kind"] == "BUY" and r["amount"] == 5000
           and r["asset_match"] and r["asset_match"]["asset_name"] == "GOLDBEES")

    r = _t("I have 50000 where should I invest")
    _check("I have 50000 where should I invest -> DEPLOY_QUERY/50000",
           r["intent"] == "DEPLOY_QUERY" and r["amount"] == 50000)

    r = _t("sip 15k midcap")
    _check("sip 15k midcap -> SIP/15000/HDFC Mid-Cap Fund",
           r["intent"] == "RECORD" and r["kind"] == "SIP" and r["amount"] == 15000
           and r["asset_match"] and r["asset_match"]["asset_name"] == "HDFC Mid-Cap Fund")

    r = _t("sold 2L junior bees")
    _check("sold 2L junior bees -> SELL/200000/JUNIORBEES",
           r["intent"] == "RECORD" and r["kind"] == "SELL" and r["amount"] == 200000
           and r["asset_match"] and r["asset_match"]["asset_name"] == "JUNIORBEES")

    r = _t("put 1 lakh in nifty")
    _check("put 1 lakh in nifty -> BUY/100000/NIFTYBEES",
           r["intent"] == "RECORD" and r["kind"] == "BUY" and r["amount"] == 100000
           and r["asset_match"] and r["asset_match"]["asset_name"] == "NIFTYBEES")

    r = _t("invested 10k in midcap negative 5500")
    _check("invested 10k in midcap negative 5500 -> amount=10000, note has -5500",
           r["intent"] == "RECORD" and r["amount"] == 10000
           and r["asset_match"] and r["asset_match"]["asset_name"] == "HDFC Mid-Cap Fund"
           and any("-5,500" in n or "5500" in n for n in r["notes"]))

    r = _t("asdkjfh qwoiuey zzz")
    _check("gibberish -> UNKNOWN", r["intent"] == "UNKNOWN")

    r = _t("")
    _check("empty string -> UNKNOWN", r["intent"] == "UNKNOWN")

    r = _t("1.5L in gold")
    _check("1.5L in gold -> BUY/150000/GOLDBEES",
           r["intent"] == "RECORD" and r["amount"] == 150000
           and r["asset_match"] and r["asset_match"]["asset_name"] == "GOLDBEES")

    r = _t("2 cr into nifty next 50")
    _check("2 cr into nifty next 50 -> BUY/20000000/JUNIORBEES",
           r["intent"] == "RECORD" and r["amount"] == 20_000_000
           and r["asset_match"] and r["asset_match"]["asset_name"] == "JUNIORBEES")

    r = _t("dividend 2000 from niftybees")
    _check("dividend 2000 from niftybees -> DIVIDEND/2000/NIFTYBEES",
           r["intent"] == "RECORD" and r["kind"] == "DIVIDEND" and r["amount"] == 2000
           and r["asset_match"] and r["asset_match"]["asset_name"] == "NIFTYBEES")

    r = _t("fd matured 100000")
    _check("fd matured 100000 -> intent RECORD/MATURITY/100000 (no confident asset)",
           r["intent"] in ("RECORD", "UNKNOWN") and r["kind"] == "MATURITY" and r["amount"] == 100000)

    r = _t("invested rs 25000 in reliance")
    _check("invested rs 25000 in reliance -> BUY/25000/Reliance Industries",
           r["intent"] == "RECORD" and r["amount"] == 25000
           and r["asset_match"] and r["asset_match"]["asset_name"] == "Reliance Industries")

    r = _t("bought tesla for 3 lakh")
    _check("bought tesla for 3 lakh -> BUY/300000/Tesla Inc",
           r["intent"] == "RECORD" and r["amount"] == 300000
           and r["asset_match"] and r["asset_match"]["asset_name"] == "Tesla Inc")

    r = _t("50k emergency fund top up")
    _check("50k emergency fund top up -> RECORD, ICICI Prudential Liquid Fund",
           r["intent"] == "RECORD" and r["amount"] == 50000
           and r["asset_match"] and r["asset_match"]["asset_name"] == "ICICI Prudential Liquid Fund")

    r = _t("bought 5000 btc")
    _check("bought 5000 btc -> no asset_match + crypto note",
           r["asset_match"] is None
           and any("Sentinel-only" in n for n in r["notes"]))

    r = _t("how should I invest 2 lakh")
    _check("how should I invest 2 lakh -> DEPLOY_QUERY/200000",
           r["intent"] == "DEPLOY_QUERY" and r["amount"] == 200000)

    r = _t("suggest something for 75000")
    _check("suggest something for 75000 -> DEPLOY_QUERY/75000",
           r["intent"] == "DEPLOY_QUERY" and r["amount"] == 75000)

    r = _t("10,000 invested in gold")
    _check("10,000 invested in gold (comma) -> BUY/10000/GOLDBEES",
           r["intent"] == "RECORD" and r["amount"] == 10000
           and r["asset_match"] and r["asset_match"]["asset_name"] == "GOLDBEES")

    r = _t("invested 20k in xyz unknown fund")
    _check("invested 20k in xyz unknown fund -> amount found, no confident asset match",
           r["amount"] == 20000 and r["asset_match"] is None)

    r = _t("profit 2000 booked in niftybees, added 5000 more")
    _check("profit 2000 booked in niftybees, added 5000 more -> amount=5000 (not the P&L number)",
           r["amount"] == 5000
           and any("profit" in n.lower() for n in r["notes"]))

    r = _t("bought crypto worth 10000")
    _check("bought crypto worth 10000 -> UNKNOWN/no asset match + Sentinel note",
           r["asset_match"] is None and any("Sentinel-only" in n for n in r["notes"]))

    r = _t("just checking in, no numbers here")
    _check("no numbers, no asset -> UNKNOWN", r["intent"] == "UNKNOWN" and r["amount"] is None)

    print(f"\n{_passed} passed, {_failed} failed out of {_passed + _failed}")
    if _failed:
        raise SystemExit(1)
