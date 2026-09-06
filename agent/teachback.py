"""Teach-back grading: does the borrower's own explanation match the clause's TYPED facts?

The design constraint that shapes everything here: **the grader never produces a number.**
Every number in the record comes from the extracted KFS schema. The LLM is handed the facts
and the borrower's words and asked only for a verdict per fact — never to restate, compute,
or "correct" a value. A grader that emits numbers is a grader that can hallucinate a loan
term into a consent record, and reasons are digit-scrubbed on the way out so that cannot
happen even if the model ignores the instruction.

Grading is PER FACT, not overall. "She understood the tenure but not the APR" is the useful
answer; a single pass/fail throws away the only information a re-read could act on.

What the grader must tolerate, because Hindi borrowers actually speak this way:

  * code-switching — English loan words inside Hindi ("EMI", "interest", "account"), and
    the English word "zero" inside an otherwise Devanagari digit run (measured: Sarvam
    verbatim emits exactly that, and a Devanagari-only map drops every zero in an
    account number);
  * colloquial amounts — साढ़े तीन लाख is 350000, ढाई लाख is 250000;
  * both digit and word forms — Sarvam mode=transcribe returns "18.5", Deepgram Hindi
    returns "अठारह दशमलव पांच". Same value, different strings;
  * multiple attested spellings — निन्यानवे/निन्यानबे, अट्ठारह/अठारह.

That last pair is why nothing here compares strings. Everything is parsed to a typed value
and compared as a number, exactly as the eval scores.

Offline by design. With no OPENROUTER_API_KEY (or with the network down mid-demo) the
deterministic numeric grader below takes over. It is stricter and blunter than the LLM —
it can tell "matched" from "did not match" but reads intent poorly — and it says so in
`graded_by`, because a consent record must never be ambiguous about who graded it.

Self-check:  uv run python -m agent.teachback
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

import httpx
from indic_numtowords import num2words

from kfs.clauses import Clause, KeyValue

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "google/gemini-2.5-flash-lite"

Verdict = str  # "understood" | "wrong" | "not_mentioned"
VERDICTS = ("understood", "wrong", "not_mentioned")

# NFKC does NOT fold these. Verified — it is a compatibility mapping, and Devanagari digits
# are not compatibility characters. An explicit table is the only way.
DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")

# 0-99 are single tokens in Hindi, so the library gives us the whole reverse map for free.
# Hand-writing a hundred entries would be a hundred chances to typo one.
_WORD_TO_INT: dict[str, int] = {num2words(n, lang="hi"): n for n in range(100)}

# Attested spellings the library does not emit. Both forms are correct Hindi and ASR
# providers disagree on which they return, so both must parse.
_WORD_TO_INT |= {
    "निन्यानबे": 99, "अठारह": 18, "पन्द्रह": 15, "सोलह": 16, "सत्रह": 17,
    "उन्तीस": 29, "पचपन": 55, "चौवन": 54, "पाँच": 5, "पांच": 5, "छ": 6, "छह": 6,
}

_MULTIPLIERS = {
    "हज़ार": 10**3, "हजार": 10**3, "thousand": 10**3,
    "लाख": 10**5, "lakh": 10**5, "lakhs": 10**5, "lac": 10**5,
    "करोड़": 10**7, "करोड": 10**7, "crore": 10**7, "crores": 10**7,
    "अरब": 10**9, "billion": 10**9,
}
_HUNDRED = {"सौ", "hundred"}

# Colloquial collapse. NeMo encodes these as first-class constants — they are the natural
# spoken form of an amount, not slang. साढ़े/सव्वा/पौने modify the number that FOLLOWS.
_STANDALONE_FRACTIONS = {"डेढ़": Decimal("1.5"), "ढाई": Decimal("2.5")}
_PREFIX_FRACTIONS = {
    "साढ़े": Decimal("0.5"), "साढे": Decimal("0.5"),
    "सव्वा": Decimal("0.25"), "सवा": Decimal("0.25"),
    "पौने": Decimal("-0.25"),
}

# AI4Bharat's production pipeline substitutes the English "point" for '.', not दशमलव.
# If the gold transcript says one and the borrower says the other, both must parse.
_DECIMAL_MARKS = {"दशमलव", "point", "पॉइंट", "पाइंट"}

_DIGIT_WORDS = {
    "शून्य": "0", "सिफ़र": "0", "सिफर": "0", "जीरो": "0", "ज़ीरो": "0", "zero": "0",
    "एक": "1", "दो": "2", "तीन": "3", "चार": "4", "पाँच": "5", "पांच": "5",
    "छह": "6", "छः": "6", "छे": "6", "सात": "7", "आठ": "8", "नौ": "9",
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9",
}

# A digit-by-digit reading of an identifier is long. Shorter runs are ordinary counting
# words ("एक लाख"), and treating those as an identifier would produce false matches.
MIN_IDENTIFIER_RUN = 4

# Do NOT tokenize with \w. Devanagari matras and the nukta are Unicode category Mn, which
# Python's \w excludes, so [^\W\d_]+ shreds साढ़े into स + ढ. Split on whitespace and strip
# punctuation from the ends instead — the only approach that keeps a Devanagari word whole.
_PUNCT = "।.,;:!?'\"()[]{}%₹/\\-–—…“”‘’"

# Punctuation ENDS a number. Without this, "साढ़े तीन लाख, तीन साल" accumulates across the
# comma into 350003 — a wrong-but-plausible value, the worst kind of parser bug. '.' is
# excluded because it is a decimal point.
_BREAK_CHARS = ",।;:!?"
_BREAK = "\x00"


@dataclass
class Fact:
    """One thing the borrower has to demonstrate she understood. Value is TYPED."""

    id: str
    kind: str
    value: Decimal | int | str
    raw_text: str
    spoken_text: str


@dataclass
class FactGrade:
    fact_id: str
    verdict: Verdict
    reason: str


@dataclass
class TeachBackResult:
    transcript: str
    grades: list[FactGrade]
    graded_by: str  # "llm" | "offline"
    note: str = ""  # why we fell back, when we did
    _facts: list[Fact] = field(default_factory=list, repr=False)

    @property
    def passed(self) -> bool:
        """Every fact understood. Any wrong or unmentioned fact blocks the clause."""
        return bool(self.grades) and all(g.verdict == "understood" for g in self.grades)

    def as_dicts(self) -> list[dict]:
        return [{"fact_id": g.fact_id, "verdict": g.verdict, "reason": g.reason}
                for g in self.grades]


def facts_for(clause: Clause) -> list[Fact]:
    """The clause's key values, as gradeable facts. Ids are stable within a clause."""
    return [
        Fact(id=f"{clause.id}#{i}:{kv.kind}", kind=kv.kind, value=kv.value,
             raw_text=kv.raw_text, spoken_text=kv.spoken_text)
        for i, kv in enumerate(clause.key_values)
    ]


def grade(clause: Clause, transcript: str, *, api_key: str | None = None,
          timeout: float = 20.0) -> TeachBackResult:
    """Grade a spoken explanation. Uses the LLM when a key is available, offline otherwise."""
    facts = facts_for(clause)
    if not facts:
        return TeachBackResult(transcript, [], "offline", note="clause carries no key values")

    key = api_key if api_key is not None else os.environ.get("OPENROUTER_API_KEY", "")
    if not key.strip():
        return grade_offline(facts, transcript, note="no OPENROUTER_API_KEY")

    try:
        return _grade_llm(facts, transcript, key.strip(), timeout)
    except Exception as e:
        # A grading failure must never become an implicit pass, and must never stall a
        # live call. Fall back, and record that we did.
        return grade_offline(facts, transcript,
                             note=f"llm unavailable: {type(e).__name__}: {str(e)[:120]}")


# ------------------------------------------------------------------------- LLM

_SYSTEM = """You grade whether a borrower, speaking Hindi (often mixed with English loan \
words), has correctly explained specific facts about her loan back to us.

You are given each FACT with its exact value. You are given her words. For each fact decide:
  "understood"   — her words convey that value correctly, in any form
  "wrong"        — she stated that fact but with a different value
  "not_mentioned"— she did not address that fact at all

Rules you must follow:
- NEVER write a number, a digit, or a spelled-out numeral in your output. Not in the
  reason, not anywhere. The values are already known to us; your job is only to judge.
- Accept code-switching freely. English words inside Hindi are normal speech, not an error.
- Accept colloquial amounts as correct (e.g. Hindi half/quarter forms of a large number).
- Accept either spelled-out words or digits. Accept alternative Hindi spellings.
- Approximations of a precise amount are "wrong", not "understood".
- Reasons must be at most 12 words, in English, and must contain no numerals.

Reply with JSON only: {"grades":[{"fact_id":"...","verdict":"...","reason":"..."}]}
Include every fact_id exactly once."""


def _grade_llm(facts: list[Fact], transcript: str, key: str,
               timeout: float) -> TeachBackResult:
    payload = {
        "model": MODEL,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": json.dumps({
                "facts": [{"fact_id": f.id, "kind": f.kind, "value": str(f.value),
                           "as_written": f.raw_text} for f in facts],
                "borrower_said": transcript,
            }, ensure_ascii=False)},
        ],
    }
    r = httpx.post(OPENROUTER_URL, headers={"Authorization": f"Bearer {key}"},
                   json=payload, timeout=timeout)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")

    content = r.json()["choices"][0]["message"]["content"]
    data = json.loads(_strip_fences(content))
    by_id = {g.get("fact_id"): g for g in data.get("grades", []) if isinstance(g, dict)}

    grades = []
    for f in facts:
        g = by_id.get(f.id, {})
        verdict = g.get("verdict")
        if verdict not in VERDICTS:
            # A malformed verdict is not a pass. Missing evidence blocks the clause.
            verdict, reason = "not_mentioned", "grader returned no usable verdict"
        else:
            reason = _scrub_digits(str(g.get("reason", "")))[:120]
        grades.append(FactGrade(f.id, verdict, reason))

    return TeachBackResult(transcript, grades, "llm", _facts=facts)


def _strip_fences(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.MULTILINE).strip()
    return s


def _scrub_digits(s: str) -> str:
    """Belt and braces on the never-produce-a-number rule.

    The prompt forbids numerals; this makes the prompt unnecessary. A model that ignores
    the instruction still cannot get a digit into the consent record.
    """
    return re.sub(r"[\d०-९]+", "…", s).strip()


# --------------------------------------------------------------------- offline

def grade_offline(facts: list[Fact], transcript: str, *, note: str = "") -> TeachBackResult:
    """Deterministic numeric matching. No network, no model, no judgement of intent.

    Coarser than the LLM on purpose: it can say whether a value was recovered, and it
    distinguishes "said something else" from "said nothing", but it cannot read meaning.
    """
    candidates = parse_values(transcript)
    sequences = digit_sequences(transcript)
    said_something = bool(candidates or sequences)

    grades = []
    for f in facts:
        if _matches(f, candidates, sequences):
            grades.append(FactGrade(f.id, "understood", "value matched exactly"))
        elif said_something:
            grades.append(FactGrade(f.id, "wrong", "no matching value in the answer"))
        else:
            grades.append(FactGrade(f.id, "not_mentioned", "no value spoken"))

    return TeachBackResult(transcript, grades, "offline", note=note, _facts=facts)


def _matches(fact: Fact, candidates: list[Decimal], sequences: list[str]) -> bool:
    if fact.kind == "account_identifier" or isinstance(fact.value, str):
        # Leading zeros are the point. `000512...` != `512...`, and the day-1 pilot found
        # the raw arm losing exactly those zeros.
        target = re.sub(r"\D", "", str(fact.value))
        return bool(target) and target in sequences

    try:
        target = Decimal(str(fact.value))
    except InvalidOperation:
        return False

    if any(c == target for c in candidates):
        return True
    # A tenure is legitimately spoken in years: "तीन साल" for 36 महीने. The delivery layer
    # says it both ways, so the grader must accept it back either way.
    return fact.kind == "tenure_months" and any(c * 12 == target for c in candidates)


def fold_digits(text: str) -> str:
    return text.translate(DEVANAGARI_DIGITS)


def _tokenize(text: str) -> list[str]:
    # Indian grouping commas are part of the number, not a separator: 1,04,596 is one value.
    folded = re.sub(r"(?<=\d),(?=\d)", "", fold_digits(text).lower())
    out = []
    for raw in folded.split():
        tok = raw.strip(_PUNCT)
        if tok:
            out.append(tok)
        if any(ch in _BREAK_CHARS for ch in raw):
            out.append(_BREAK)
    return out


def digit_sequences(text: str) -> list[str]:
    """Digit strings the borrower spoke, leading zeros intact.

    Two sources: literal digit runs (Sarvam mode=transcribe) and runs of spoken digit
    words (Sarvam verbatim, Deepgram Hindi). Short word-runs are excluded — see
    MIN_IDENTIFIER_RUN.
    """
    folded = fold_digits(text)
    out = [m.group() for m in re.finditer(r"\d{%d,}" % MIN_IDENTIFIER_RUN, folded)]

    run: list[str] = []
    for tok in _tokenize(folded):
        if tok in _DIGIT_WORDS:
            run.append(_DIGIT_WORDS[tok])
            continue
        if len(run) >= MIN_IDENTIFIER_RUN:
            out.append("".join(run))
        run = []
    if len(run) >= MIN_IDENTIFIER_RUN:
        out.append("".join(run))
    return out


def parse_values(text: str) -> list[Decimal]:
    """Every number in the text, as typed values. Indic scale, colloquial forms, decimals.

    Accumulator over tokens: units and सौ build a group, a multiplier (हज़ार/लाख/करोड़)
    banks it, and a decimal mark switches to digit-by-digit fractional reading.
    """
    values: list[Decimal] = []
    total = Decimal(0)
    group = Decimal(0)
    seen = False  # a numeric token contributed; a bare साढ़े on its own is not a number
    prefix = Decimal(0)  # pending साढ़े / सव्वा / पौने adjustment to the number that follows
    frac: list[str] | None = None

    def flush() -> None:
        nonlocal total, group, seen, prefix, frac
        if seen:
            v = total + group + prefix
            if frac:
                v += Decimal("0." + "".join(frac))
            values.append(v)
        total, group, seen, prefix, frac = Decimal(0), Decimal(0), False, Decimal(0), None

    for tok in _tokenize(text):
        if frac is not None:
            if tok in _DIGIT_WORDS:
                frac.append(_DIGIT_WORDS[tok])
                continue
            if tok.isdigit():
                frac.append(tok)
                continue
            flush()

        if tok in _DECIMAL_MARKS and seen:
            frac = []
            continue

        if tok in _PREFIX_FRACTIONS:
            flush()
            prefix = _PREFIX_FRACTIONS[tok]
            continue

        if _STANDALONE_FRACTIONS.get(tok) is not None:
            flush()
            group, seen = _STANDALONE_FRACTIONS[tok], True  # type: ignore[assignment]
            continue

        if tok in _MULTIPLIERS:
            # "लाख" with no preceding count means one lakh.
            total += ((group or Decimal(1)) + prefix) * _MULTIPLIERS[tok]
            group, prefix, seen = Decimal(0), Decimal(0), True
            continue

        if tok in _HUNDRED:
            group = (group or Decimal(1)) * 100
            seen = True
            continue

        if tok in _WORD_TO_INT:
            group += _WORD_TO_INT[tok]
            seen = True
            continue

        if re.fullmatch(r"\d+(?:\.\d+)?", tok):
            flush()
            values.append(Decimal(tok))
            continue

        flush()

    flush()
    return values


def _demo() -> None:
    """Offline grader self-check. Runs with no key and no network."""
    from kfs.clauses import Segment

    assert parse_values("साढ़े तीन लाख") == [Decimal(350000)]
    assert parse_values("ढाई लाख रुपए") == [Decimal(250000)]
    assert parse_values("अठारह दशमलव पांच प्रतिशत") == [Decimal("18.5")]
    assert parse_values("18.5%") == [Decimal("18.5")]
    assert parse_values("एक लाख चार हज़ार पाँच सौ छियानवे") == [Decimal(104596)]
    assert digit_sequences("शून्य शून्य शून्य पाँच एक दो") == ["000512"]
    assert digit_sequences("नौ आठ चार zero नौ") == ["98409"]  # Latin 'zero' inside Devanagari

    clause = Clause(
        id="emi", title_hi="मासिक किस्त",
        segments=[Segment(
            text="आपकी मासिक किस्त चार हज़ार तीन सौ बयासी रुपए है।",
            key_value=KeyValue("emi_amount", Decimal(4382), "₹4,382", "चार हज़ार ..."),
            audio=b"\x00" * 8000,
        )],
    )

    ok = grade_offline(facts_for(clause), "मेरी किस्त चार हज़ार तीन सौ बयासी रुपए है")
    assert ok.passed, ok.grades
    assert ok.graded_by == "offline"

    wrong = grade_offline(facts_for(clause), "किस्त पाँच हज़ार रुपए")
    assert not wrong.passed and wrong.grades[0].verdict == "wrong"

    silent = grade_offline(facts_for(clause), "मुझे नहीं पता")
    assert silent.grades[0].verdict == "not_mentioned"

    assert _scrub_digits("said 4382 correctly") == "said … correctly"

    for label, res in [("correct", ok), ("wrong", wrong), ("silent", silent)]:
        g = res.grades[0]
        print(f"  {label:>8}: {g.verdict:<14} {g.reason}")
    print("\nteach-back offline grader OK")


if __name__ == "__main__":
    _demo()
