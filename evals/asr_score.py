"""Score whether a comprehension-critical value survived TTS -> phone channel -> ASR.

This is the most delicate part of the harness, because every obvious shortcut silently
fakes the result. Four traps, all confirmed in our own day-1 battery transcripts
(evals/results/battery/*.txt):

  * Providers disagree on numeral format for the SAME audio. Sarvam mode=transcribe
    returns `18.5 प्रतिशत`; Deepgram nova-3 hi returns `अठारह दशमलव पांच प्रतिशत`. A digit
    regex scores Deepgram ~0% on every item — an artifact, not a result. So: every clip
    goes through Sarvam TWICE (transcribe + verbatim) and a value counts as recovered if
    EITHER pass matches, and everything is scored on PARSED TYPED VALUES, never strings.
  * NFKC does NOT fold Devanagari digits. U+0966-U+096F needs an explicit map.
  * Hindi has multiple attested spellings of one number: निन्यानवे/निन्यानबे (व~ब),
    अट्ठारह/अठारह (gemination), पाँच/पांच (chandrabindu~anusvara), हज़ार/हजार (nukta).
    `_norm` folds all four classes so string identity is never load-bearing.
  * Sarvam verbatim mixes scripts INSIDE one number: `नौ आठ चार zero नौ`. A Devanagari-only
    word->digit map drops every zero in an account number — which is exactly the leading-zero
    property the primary claim rests on. Latin "zero"/"o" are in the digit table.

For `account_identifier` the comparison is on the full digit STRING including leading
zeros, never on an int, because 000512348899 and 512348899 are different accounts.

Self-check (no network):  uv run python -m evals.asr_score
"""

from __future__ import annotations

import re
import sys
import unicodedata
from decimal import Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from indic_numtowords import num2words  # noqa: E402  (lexicon source, see below)

DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")

# Aspirated geminate clusters written both ways in Hindi orthography. Folding the cluster
# to its aspirate makes अट्ठारह == अठारह, अट्ठाईस == अठाईस, and so on, with one rule
# instead of a variant list that will always be missing the next one.
_GEMINATES = [("ट्ठ", "ठ"), ("त्थ", "थ"), ("क्ख", "ख"), ("प्फ", "फ"), ("च्छ", "छ"),
              ("ब्भ", "भ"), ("ड्ढ", "ढ"), ("ग्घ", "घ"), ("ज्झ", "झ"), ("द्ध", "ध")]


def _norm(w: str) -> str:
    """Fold the orthographic variation that is NOT a difference in value.

    ब -> व is safe to apply blanket-wise inside a number lexicon (बीस/वीस, बारह/वारह have
    no व-initial homographs) and it is what makes निन्यानबे and निन्यानवे the same key.
    """
    w = unicodedata.normalize("NFD", w).replace("़", "")  # nukta: हज़ार == हजार
    w = unicodedata.normalize("NFC", w)
    w = w.replace("ँ", "ं")  # chandrabindu -> anusvara: पाँच == पांच
    for a, b in _GEMINATES:
        w = w.replace(a, b)
    return w.replace("ब", "व").lower()


# Four more folds, each one a spelling difference we actually measured between what the
# delivery layer emits and what an ASR returns for the SAME number:
#   अनुस्वार   पैंतालीस ~ पैतालीस
#   ए matra    तिरेसठ ~ तिरसठ            (63)
#   long vowel बाईस ~ बाइस, अट्ठाईस ~ अट्ठाइस  (22, 28)
#   gemination इकत्तीस ~ इकतीस           (31)
# Applied as a closure, and verified collision-free over 0-99 — so no amount of folding can
# merge two different numbers into one key.
_DOUBLED = re.compile(r"(.)्\1")
_LONG_VOWELS = str.maketrans("ईीऊू", "इिउु")
_FOLDS = (lambda s: s.replace("ं", ""), lambda s: s.replace("े", ""),
          lambda s: s.translate(_LONG_VOWELS), lambda s: _DOUBLED.sub(r"\1", s))


def _keys(word: str) -> list[str]:
    """Every spelling of `word` that is the same value, most faithful first."""
    out = [_norm(word)]
    for fold in _FOLDS:
        out += [k for k in map(fold, out) if k not in out]
    return out


def _lookup(table: dict, word: str):
    for k in _keys(word):
        if k in table:
            return table[k]
    return None


# The 0-99 lexicon is DERIVED from indic-numtowords rather than hand-typed: it is the same
# library the delivery layer verbalises with, so the scorer cannot drift from the speaker.
WORDS: dict[str, Decimal] = {}


def _learn(word: str, value) -> None:
    for k in _keys(word):
        WORDS.setdefault(k, Decimal(str(value)))


for _n in range(100):
    _learn(num2words(_n, lang="hi"), _n)
_learn("छह", 6)  # indic-numtowords says छः; every ASR here says छह. Both are attested.
# Day-of-month ordinals: Coda reads `1 मई` as `पहली मई`, which is correct Hindi and would
# otherwise score as a lost date.
for _w in ("पहली", "पहला", "प्रथम"):
    _learn(_w, 1)

# Colloquial collapse — first-class constants in NeMo's Hindi grammar, and what a borrower
# actually says. डेढ़/ढाई stand alone; साढ़े/सवा/पौने modify the number that FOLLOWS them.
for _w, _v in [("डेढ़", "1.5"), ("ढाई", "2.5"), ("सवा", "1.25")]:
    _learn(_w, _v)
MODIFIERS = {_norm("साढ़े"): Decimal("0.5"), _norm("सवा"): Decimal("0.25"),
             _norm("सव्वा"): Decimal("0.25"), _norm("पौने"): Decimal("-0.25")}

# English digit words: Sarvam verbatim emits Latin "zero" inside Devanagari digit runs, and
# Deepgram code-switches on Hinglish input.
_EN = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]
for _i, _w in enumerate(_EN):
    _learn(_w, _i)
# ...and their Devanagari transliterations, which Sarvam's verbatim mode actually returns
# ("जीरो सा पांच बारह" is a real transcript from this corpus). वन and सेवन are also ordinary
# Hindi words (forest, consumption); neither occurs in a KFS, so the collision is accepted.
for _i, _w in enumerate(["जीरो", "वन", "टू", "थ्री", "फोर", "फाइव", "सिक्स", "सेवन", "एट", "नाइन"]):
    _learn(_w, _i)

HUNDRED = {_norm("सौ"), "hundred"}
MULTIPLIERS = {_norm("हज़ार"): 10**3, "thousand": 10**3,
               _norm("लाख"): 10**5, "lakh": 10**5, "lac": 10**5,
               _norm("करोड़"): 10**7, "crore": 10**7,
               _norm("अरब"): 10**9, "billion": 10**9, "million": 10**6,
               _norm("खरब"): 10**11}
DECIMAL_MARKS = {_norm("दशमलव"), "point", _norm("पॉइंट"), _norm("प्वाइंट"), "."}

# Single-digit tokens, for reading a SEQUENCE rather than a quantity.
DIGIT_WORDS = {k: str(int(v)) for k, v in WORDS.items() if v == v.to_integral() and 0 <= v <= 9}
DIGIT_WORDS[_norm("ओ")] = "0"  # spoken "oh" for zero
DIGIT_WORDS["o"] = "0"
DIGIT_WORDS["oh"] = "0"

MONTHS_HI = {
    1: "जनवरी", 2: "फ़रवरी", 3: "मार्च", 4: "अप्रैल", 5: "मई", 6: "जून",
    7: "जुलाई", 8: "अगस्त", 9: "सितंबर", 10: "अक्टूबर", 11: "नवंबर", 12: "दिसंबर",
}
# Both the anusvara and the conjunct spellings are attested and ASR returns either.
_MONTH_ALT = {9: ["सितम्बर", "september", "sept"], 11: ["नवम्बर", "november"],
              12: ["दिसम्बर", "december"], 2: ["february"], 1: ["january"], 3: ["march"],
              4: ["april"], 5: ["may"], 6: ["june"], 7: ["july"], 8: ["august"],
              10: ["अक्तूबर", "october"]}
MONTH_KEYS: dict[str, int] = {}
for _m, _name in MONTHS_HI.items():
    for _v in [_name, *_MONTH_ALT.get(_m, [])]:
        for _k in _keys(_v):
            MONTH_KEYS.setdefault(_k, _m)

# `\w` alone SHATTERS Devanagari: matras, anusvara and virama are combining marks (Mn),
# which Python's re does not treat as word characters, so पाँच tokenizes as प + च. The
# explicit block range fixes that while excluding the danda U+0964-0965 as punctuation.
# A bare "." is a token because Deepgram writes the decimal separator as punctuation:
# `अठारह. पांच प्रतिशत` is a real transcript of 18.5. It only counts as a decimal point
# when a number is already in progress, so an ordinary full stop is harmless.
_TOKEN = re.compile(r"[0-9][0-9,.]*|[\wऀ-ॣ॰-ॿ]+|[,;।]|\.", re.UNICODE)

# A clause boundary ends a number. Without this, "साढ़े तीन लाख, तीन साल" accumulates ACROSS
# the comma into 350003 — a wrong-but-plausible value that no listener said and that would
# silently corrupt scoring rather than crashing.
#
# Note the two ways these characters were invisible before:
#   - a standalone comma matched no alternative in _TOKEN and was dropped;
#   - danda U+0964 and double danda U+0965 sit OUTSIDE both Devanagari ranges above
#     (ऀ-ॣ is U+0900-U+0963), so Hindi sentence ends were dropped too.
# A comma INSIDE a number ("9,157") is still consumed by the numeric alternative first,
# which is why that branch has to stay ahead of this one.
BOUNDARY = {",", ";", "।", "॥"}


def tokenize(text: str) -> list[str]:
    """Devanagari digits to ASCII first — NFKC will not do this.

    `13. 97` is rejoined into one token: Deepgram writes a spoken decimal point as a full
    stop AND puts a space after it, and two separate numbers is not what was said.
    """
    toks = _TOKEN.findall(text.translate(DEVANAGARI_DIGITS).replace("‌", "").replace("‍", ""))
    out: list[str] = []
    for tok in toks:
        prev = out[-1] if out else ""
        if (tok[0].isdigit() and prev[:1].isdigit() and prev.endswith(".")
                and prev.count(".") == 1):
            out[-1] = prev + tok
        else:
            out.append(tok)
    return out


def _numeric(tok: str) -> Decimal | None:
    if not tok[0].isdigit():
        return None
    try:
        return Decimal(tok.replace(",", "").rstrip("."))
    except InvalidOperation:
        return None


def parse_numbers(text: str) -> list[Decimal]:
    """Every number the transcript asserts, as typed values.

    Two consecutive sub-100 words are emitted as SEPARATE numbers, not summed: real Hindi
    never says "पाँच सात" for 57, so a run of them is a digit sequence being read aloud and
    summing it would invent a value nobody said.
    """
    out: list[Decimal] = []
    total = current = Decimal(0)
    seen = pending_unit = False
    frac = ""
    in_frac = False
    mod: Decimal | None = None

    def flush() -> None:
        nonlocal total, current, seen, pending_unit, frac, in_frac, mod
        if seen:
            v = total + current
            if frac:
                v = Decimal(f"{v}.{frac}")
            out.append(v)
        total = current = Decimal(0)
        seen = pending_unit = in_frac = False
        frac = ""
        mod = None

    for tok in tokenize(text):
        # A clause boundary settles whatever has accumulated. flush() also clears in_frac,
        # so "अठारह दशमलव पांच, तीन साल" does not drag the fraction across the comma.
        if tok in BOUNDARY:
            flush()
            continue

        num = _numeric(tok)
        key = _norm(tok)

        if in_frac:
            if num is not None:
                frac += re.sub(r"\D", "", tok)
                continue
            digit = _lookup(DIGIT_WORDS, tok)
            if digit is not None:
                frac += digit
                continue
            flush()
            # fall through: this token may itself start a new number

        if num is not None:
            flush()
            out.append(num)
            continue
        if key in DECIMAL_MARKS:
            if seen:
                in_frac = True
            continue
        if key in MODIFIERS:
            mod = MODIFIERS[key]
            continue
        if key in HUNDRED:
            current = (current if seen else Decimal(1)) * 100
            seen, pending_unit = True, False
            continue
        mult = _lookup(MULTIPLIERS, tok)
        if mult is not None:
            total += (current if seen else Decimal(1)) * mult
            current, seen, pending_unit = Decimal(0), True, False
            continue

        val = _lookup(WORDS, tok)
        if val is not None:
            if pending_unit:  # two sub-100 words in a row = a sequence, not a sum
                flush()
            if mod is not None:
                val, mod = val + mod, None
            current += val
            seen, pending_unit = True, True
            continue

        flush()

    flush()
    return out


def digit_strings(text: str) -> list[str]:
    """Maximal runs of things read as SINGLE DIGITS, as strings (leading zeros intact).

    Adjacent numeric tokens are joined because ASR punctuates digit runs unpredictably:
    Sarvam returned `9,15,71,14,007` and `9157, 1,14,007` for the same account number.
    A run breaks on any non-digit word, which is what makes the raw arm's
    `नौ अरब पंद्रह करोड़ ...` correctly score as NOT a recovered digit sequence.
    """
    runs, cur = [], ""
    for tok in tokenize(text):
        if tok[0].isdigit():
            cur += re.sub(r"\D", "", tok)
            continue
        d = _lookup(DIGIT_WORDS, tok)
        if d is not None:
            cur += d
        elif cur:
            runs.append(cur)
            cur = ""
    if cur:
        runs.append(cur)
    return runs


def recovered(item: dict, transcript: str) -> bool:
    """Did this transcript recover the item's ground-truth value EXACTLY?"""
    kind, gt = item["type"], item["value"]

    if kind == "account_identifier":
        return str(gt) in digit_strings(transcript)

    if kind == "date_deadline":
        y, m, d = (int(p) for p in str(gt).split("-"))
        nums = set(parse_numbers(transcript))
        months = {v for t in tokenize(transcript)
                  if (v := _lookup(MONTH_KEYS, t)) is not None}
        # A digits-only ASR pass writes the month as a number (15/03/2026), so accept either.
        return (m in months or Decimal(m) in nums) and Decimal(d) in nums and Decimal(y) in nums

    return Decimal(str(gt)) in set(parse_numbers(transcript))


# --------------------------------------------------------------- ASR calls
# Reference implementations live in scripts/smoke.py and are reused, not rewritten: they
# already encode that saarika:v2/v2.5 are gone and that Deepgram must be lang="hi", never
# "multi" (documented Hindi->Spanish misdetection on Hinglish).
def transcribe(wav: Path, scorer: str, mode: str = "transcribe") -> str:
    from scripts.smoke import deepgram_transcribe, sarvam_transcribe

    if scorer == "sarvam":
        return sarvam_transcribe(wav, mode=mode)
    if scorer == "deepgram":
        return deepgram_transcribe(wav)
    raise ValueError(f"unknown scorer {scorer!r}")


# Sarvam's `mode` deterministically controls numeral format, so the SAME audio is sent
# twice and either pass may satisfy the item. Deepgram has one pass.
PASSES = {"sarvam": ("transcribe", "verbatim"), "deepgram": ("default",)}


def score_clip(item: dict, wav: Path, scorer: str) -> dict:
    """Transcribe and score one clip. Returns the row that lands in item_level.csv.

    `matched_pass` is recorded because it is diagnostic, not bookkeeping: a value that
    matches only on the digits pass may have been SPOKEN as a quantity and reconstructed by
    the provider's inverse text normalization. That is the day-1 scoring artifact
    (ACCEPTANCE.md amendment 1) and this column is how it stays visible.
    """
    texts, matched = {}, ""
    for mode in PASSES[scorer]:
        try:
            texts[mode] = transcribe(wav, scorer, mode)
        except Exception as e:  # a provider error must not be scored as a miss
            texts[mode] = ""
            texts.setdefault("_error", f"{type(e).__name__}: {e}")
        if not matched and texts[mode] and recovered(item, texts[mode]):
            matched = mode
    modes = PASSES[scorer]
    return {
        "scorer": scorer,
        "transcript_1": texts.get(modes[0], ""),
        "transcript_2": texts.get(modes[1], "") if len(modes) > 1 else "",
        "matched_pass": matched,
        "recovered": bool(matched),
        "error": texts.get("_error", ""),
    }


def agreement(rows: list[dict]) -> dict:
    """Inter-scorer agreement on the recovered/not judgement, plus Cohen's kappa.

    Reported alongside the per-scorer numbers rather than instead of them: two scorers that
    agree 95% of the time are one measurement, and saying so is the point.
    """
    pairs = {}
    for r in rows:
        pairs.setdefault((r["item_id"], r["arm"]), {})[r["scorer"]] = r["recovered"]
    both = [(v["sarvam"], v["deepgram"]) for v in pairs.values()
            if "sarvam" in v and "deepgram" in v]
    if not both:
        return {"n": 0}

    n = len(both)
    obs = sum(a == b for a, b in both) / n
    pa = sum(a for a, _ in both) / n
    pb = sum(b for _, b in both) / n
    exp = pa * pb + (1 - pa) * (1 - pb)
    kappa = (obs - exp) / (1 - exp) if exp < 1 else 1.0
    return {"n": n, "observed_agreement": round(obs, 4), "cohens_kappa": round(kappa, 4),
            "sarvam_recovery": round(pa, 4), "deepgram_recovery": round(pb, 4)}


def _demo() -> None:
    """Self-check on the parser. No network. Cases taken from real battery transcripts."""
    assert parse_numbers("आपके लोन की कुल राशि एक लाख चार हज़ार पाँच सौ छियानवे रुपये है।") == [Decimal(104596)]
    assert Decimal("18.5") in parse_numbers("वार्षिक ब्याज दर अठारह दशमलव पांच प्रतिशत है.")
    assert Decimal("18.5") in parse_numbers("यानी साढ़े अठारह प्रतिशत")
    assert Decimal("99.99") in parse_numbers("निन्यानबे दशमलव नौ नौ")  # ब variant
    assert Decimal(2026) in parse_numbers("पंद्रह मार्च दो हज़ार छब्बीस")
    assert Decimal(123) in parse_numbers("१२३")  # Devanagari digits; NFKC will not do this

    # The headline: digit-by-digit is a STRING, and the raw arm's quantity reading is not it.
    assert "9157114007" in digit_strings("आपका खाता नंबर नौ एक पांच सात एक एक चार शून्य शून्य सात है.")
    assert "9157114007" in digit_strings("आपका खाता नंबर 9,15,71,14,007 है।")
    assert "9157114007" not in digit_strings("नौ अरब पंद्रह करोड़ इकहत्तर लाख चौदह हज़ार सात")

    # Latin "zero" inside a Devanagari run — miss this and every leading zero disappears.
    assert digit_strings("शून्य zero शून्य पांच एक दो") == ["000512"]

    print("parser OK —", len(WORDS), "lexicon keys")


if __name__ == "__main__":
    _demo()
