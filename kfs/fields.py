"""The field manifest: which Annex-A labels must be READ rather than defaulted.

This module exists to stop one specific failure, which is the worst thing this product
could do. `kfs/schema.py` gives some fields defaults so that a partially-specified KFS can
still be constructed:

    cooling_off_period_days: int = 0        # kfs/schema.py:88
    fees: list[FeeLine] = Field(...)        # kfs/schema.py:84

and `kfs/build_clauses.py` then reads the falsy case aloud as a positive statement of fact:

    days == 0     -> "इस लोन में कूलिंग-ऑफ की अवधि नहीं है।"   (build_clauses.py:326)
    fees == []    -> "इस लोन पर कोई अलग फीस नहीं है।"          (build_clauses.py:249)

The RBI cooling-off period is a statutory right. A label this parser fails to match would
tell a borrower she does not have that right, and seal it into a sha256-hashed consent
record. So the value alone can never be trusted to mean "absent":

    fixtures/synthetic/kfs_04_gold.json declares cooling_off_period_days = 0 LEGITIMATELY.

Zero is a real answer for a gold loan and a fabrication for a two-wheeler loan, and the
integer is identical in both cases. The only thing that separates them is **whether the
document said so**. That is what this module tracks, and why required-ness is declared
here per field instead of being inferred from `KFS.model_fields` — pydantic considers `0`
and `""` perfectly satisfied.

`kfs/extract.py` today gets this right by accident: every field goes through `_text`/`_num`,
which raise on a miss, so a KFS from a PDF is always fully read. That safety is a property
of raising, and it is exactly what label-drift tolerance and a human correction step would
quietly remove. This manifest is the guard that keeps it once those exist.

⚠️ The synonym lists are a HYPOTHESIS, not a measurement. This repo has no real lender's
KFS to test against, so they are plausible drift ("Loan Proposal No.", "APR (%)") rather
than attested. Every synonym that fires is reported to the reviewer with the document's own
label quoted verbatim, precisely because the table cannot be trusted to be complete.

Self-check:  uv run python -m kfs.fields
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field as _dc_field

# Loan-type-independent: read as a string and never coerced. Kept as its own constant
# because `_to_pdf.py` writes this exact header and `_classify` finds the fee sub-table by
# it, so the two must not drift apart.
FEE_TABLE_HEADER_CELL = "payable to"


@dataclass(frozen=True)
class Field:
    """One Annex-A Part 1 row.

    `label` is the canonical spelling, which is what `fixtures/synthetic/_to_pdf.py`
    prints and what `kfs/extract.py` looks up. `required` means "must be present in the
    document for us to read this loan aloud" — NOT "pydantic has no default".
    """

    label: str
    synonyms: tuple[str, ...] = ()
    # Human-readable note shown next to a missing field on the review page, in Hindi where
    # a helper would be reading it to the borrower.
    prompt: str = ""
    required: bool = True

    @property
    def keys(self) -> tuple[str, ...]:
        return (self.label, *self.synonyms)


# Every label `kfs/extract.py:47-73` reads unconditionally. The order is Annex A's
# disclosure order, which is also `_to_pdf.py`'s row order.
PART1: tuple[Field, ...] = (
    Field("Loan proposal number", ("Loan proposal no", "Proposal number", "Application number")),
    Field("Type of loan", ("Loan type", "Nature of loan", "Facility type")),
    # Never int(): "0004512378" -> 4512378 would destroy the leading zeros this whole
    # product exists to preserve. extract.py reads it with _text for that reason.
    Field("Loan account number", ("Loan account no", "Account number", "Loan a/c no")),
    Field("Sanctioned loan amount (Rs)",
          ("Sanctioned amount (Rs)", "Loan amount sanctioned (Rs)", "Sanctioned amount")),
    # Derived, not a value: extract.py:53 does a substring test for "stage" over this
    # cell's prose. See DERIVED below.
    Field("Disbursal schedule", ("Disbursement schedule", "Mode of disbursal")),
    Field("Loan term (months)", ("Loan tenure (months)", "Tenure (months)", "Loan term")),
    Field("Type of instalments", ("Instalment type", "Repayment frequency")),
    Field("Number of EPIs", ("No. of EPIs", "Number of instalments", "No of EPI")),
    Field("EPI amount (Rs)", ("EPI (Rs)", "Instalment amount (Rs)", "EMI amount (Rs)")),
    Field("Commencement of repayment",
          ("Repayment commencement", "First instalment date", "Commencement of repayment date")),
    Field("Interest rate (% p.a.)",
          ("Interest rate (%)", "Rate of interest (% p.a.)", "Rate of interest")),
    # Derived, not a value: extract.py:38,63 lowercases this cell and also branches the
    # whole floating block on it. See DERIVED below.
    Field("Interest rate type", ("Type of interest rate", "Interest type", "Rate type")),
    Field("Annual Percentage Rate (APR) (%)",
          ("APR (%)", "Annual percentage rate (%)", "APR", "Annualised percentage rate (%)")),
    Field("Total amount to be paid (Rs)",
          ("Total repayment (Rs)", "Total amount payable (Rs)", "Total outflow (Rs)")),
    # THE field this module exists for. Required, and 0 is only believable when the
    # document actually says 0.
    Field("Cooling-off period (days)",
          ("Cooling off period (days)", "Look-up period (days)", "Cooling-off / look-up period (days)"),
          prompt="कूलिंग-ऑफ अवधि — कितने दिन?"),
    Field("Recovery agents", ("Recovery agent clause", "Recovery agents clause")),
    Field("Grievance redressal officer",
          ("Grievance officer", "Grievance redressal officer name", "Nodal officer")),
    Field("Grievance redressal phone",
          ("Grievance officer phone", "Grievance redressal contact", "Nodal officer phone")),
)

# Read by extract.py:38-45 ONLY when "Interest rate type" is not "fixed". Requiring these
# of a fixed-rate loan would reject every one of the nine fixed fixtures; not requiring
# them of a floating loan would let `FloatingInfo` be built from thin air.
FLOATING: tuple[Field, ...] = (
    Field("Benchmark", ("Benchmark rate", "External benchmark")),
    Field("Spread (%)", ("Spread over benchmark (%)", "Margin (%)")),
    Field("Reset periodicity (months)", ("Reset periodicity", "Rate reset (months)")),
    Field("Impact of 25 bps change on EPI (Rs)", ("Impact of 25 bps on EPI (Rs)",)),
    Field("Impact of 25 bps change on no. of EPIs",
          ("Impact of 25 bps on no. of EPIs", "Impact of 25 bps change on number of EPIs")),
)

# Cells whose meaning is computed from prose rather than parsed as a value. They are
# ordinary required labels for presence purposes, but a reviewer correcting one is
# correcting free text, not a number, so the intake page must not offer a numeric input.
DERIVED: frozenset[str] = frozenset({"Disbursal schedule", "Interest rate type"})

ALL: tuple[Field, ...] = PART1 + FLOATING

_BY_LABEL: dict[str, Field] = {f.label: f for f in ALL}


def norm(label: str) -> str:
    """Fold a label to its comparison key.

    Casefold, collapse every run of non-alphanumerics to one space, strip. This is what
    makes "Loan Proposal No." and "loan proposal no" the same key, and it is deliberately
    lossy in only that one way — it never drops or reorders words, so "Total amount to be
    paid" cannot collide with "Total charges".

    Note `%`, `(`, `)` and `.` all fold away, so "APR (%)" -> "apr" and
    "Interest rate (% p.a.)" -> "interest rate p a".
    """
    return re.sub(r"[^0-9a-z]+", " ", label.casefold()).strip()


# Built once. A collision here is a bug in the tables above, not a runtime condition, so it
# is asserted rather than handled.
_CANONICAL: dict[str, str] = {}
for _f in ALL:
    for _k in _f.keys:
        _n = norm(_k)
        assert _n not in _CANONICAL or _CANONICAL[_n] == _f.label, (
            f"ambiguous label {_k!r} -> {_CANONICAL.get(_n)!r} and {_f.label!r}"
        )
        _CANONICAL[_n] = _f.label


@dataclass
class Resolution:
    """The outcome of matching a document's own labels against the manifest.

    `values` is keyed by CANONICAL label, which is what `kfs/extract.py` looks up, so the
    existing `_text`/`_num` body needs no change. `matched_by` remembers the document's own
    spelling so the review page can quote it verbatim — a reviewer approving a
    synonym match deserves to see what was actually printed.
    """

    values: dict[str, str] = _dc_field(default_factory=dict)
    matched_by: dict[str, str] = _dc_field(default_factory=dict)   # canonical -> raw label
    drifted: dict[str, str] = _dc_field(default_factory=dict)      # canonical -> raw, synonym hits
    unknown: dict[str, str] = _dc_field(default_factory=dict)      # raw label -> value, no match

    def rate_kind(self) -> str:
        """Lowercased "Interest rate type", or "" when it was not read.

        Drives conditional requiredness. Empty means we do not know, and the caller must
        treat the floating block as unknown rather than absent.
        """
        return self.values.get("Interest rate type", "").strip().lower()

    def required(self) -> tuple[Field, ...]:
        kind = self.rate_kind()
        if kind and kind != "fixed":
            return tuple(f for f in ALL if f.required)
        # Unknown rate kind: require Part 1 only. "Interest rate type" is itself in PART1,
        # so a document missing it is already reported as incomplete.
        return tuple(f for f in PART1 if f.required)

    def missing(self) -> tuple[Field, ...]:
        """Required fields that were not read, or were read as an empty cell.

        An empty cell is a miss, not a zero. `_parse_money("")` is None and `_num` already
        raises on it; keeping the same verdict here means the review page and the parser
        agree about what "absent" means.
        """
        return tuple(f for f in self.required() if not self.values.get(f.label, "").strip())

    @property
    def complete(self) -> bool:
        return not self.missing()


def resolve(raw: dict[str, str]) -> Resolution:
    """Map a document's label->value dict onto canonical Annex-A labels.

    Exact spellings win over synonyms: a document containing both "APR (%)" and "Annual
    Percentage Rate (APR) (%)" resolves to the canonical cell rather than to whichever
    happened to be iterated last.
    """
    out = Resolution()

    for label, value in raw.items():
        canonical = _CANONICAL.get(norm(label))
        if canonical is None:
            out.unknown[label] = value
            continue
        exact = norm(label) == norm(canonical)
        # First writer wins unless this one is the exact canonical spelling.
        if canonical in out.values and not exact:
            continue
        out.values[canonical] = value
        out.matched_by[canonical] = label
        if exact:
            out.drifted.pop(canonical, None)
        else:
            out.drifted[canonical] = label

    return out


def prompt_for(label: str) -> str:
    f = _BY_LABEL.get(label)
    return (f.prompt if f and f.prompt else label)


def is_derived(label: str) -> bool:
    return label in DERIVED


def _demo() -> None:
    """Self-check the manifest against the committed fixture PDFs.

    Checked against genuinely PARSED documents rather than against
    `fixtures/synthetic/_to_pdf.py`'s row list: parsing is the path a real upload takes,
    and it keeps this self-check off the dev-only reportlab dependency.
    """
    from pathlib import Path

    # Deferred: kfs.extract imports this module, so a top-level import would be circular.
    from kfs.extract import _tables  # noqa: PLC0415

    root = Path(__file__).parent.parent / "fixtures/synthetic"
    pdfs = sorted((root / "pdf").glob("*.pdf"))
    if not pdfs:
        raise SystemExit("no PDFs — run: PYTHONPATH=. uv run python fixtures/synthetic/_to_pdf.py")

    # 1. Every fixture PDF resolves COMPLETE, with no unmatched and no drifted labels.
    #    This is the guard that the manifest agrees with what the parser actually sees —
    #    rename a label in one place and this fails here, with a readable message, rather
    #    than surfacing later as a confusing round-trip failure.
    fixed_seen = floating_seen = 0
    for pdf in pdfs:
        part1, fee_rows = _tables(pdf)
        res = resolve(part1)
        assert res.complete, f"{pdf.name}: incomplete: {[f.label for f in res.missing()]}"
        assert not res.unknown, f"{pdf.name}: unmatched labels {sorted(res.unknown)}"
        assert not res.drifted, f"{pdf.name}: canonical labels matched as synonyms {res.drifted}"
        assert fee_rows, f"{pdf.name}: fee sub-table not found"
        if res.rate_kind() == "fixed":
            fixed_seen += 1
            # A fixed-rate loan must NOT be asked for the floating block.
            assert all(f.label != "Benchmark" for f in res.required())
        else:
            floating_seen += 1
            assert any(f.label == "Benchmark" for f in res.required())
    assert fixed_seen and floating_seen, "fixtures no longer cover both rate kinds"

    baseline, _ = _tables(pdfs[0])

    # 2. Label drift resolves, and DIFFERENT WORDING is reported as drift rather than
    #    passing silently. Note the third one: "Cooling off period (days)" differs from
    #    the canonical spelling only by a hyphen, which norm() folds, so it is an exact
    #    match and is deliberately NOT flagged. Punctuation and case are absorbed in
    #    silence; only a genuinely different form of words is surfaced for review.
    drifted = resolve({
        "Loan Proposal No.": "SAMJHA-2026-001",
        "APR (%)": "18.5",
        "Cooling off period (days)": "3",
    })
    assert drifted.values["Loan proposal number"] == "SAMJHA-2026-001"
    assert drifted.values["Annual Percentage Rate (APR) (%)"] == "18.5"
    assert drifted.values["Cooling-off period (days)"] == "3"
    assert set(drifted.drifted) == {"Loan proposal number", "Annual Percentage Rate (APR) (%)"}
    assert drifted.matched_by["Annual Percentage Rate (APR) (%)"] == "APR (%)"
    assert drifted.matched_by["Cooling-off period (days)"] == "Cooling off period (days)"

    # 3. An exact canonical spelling beats a synonym regardless of iteration order.
    both = resolve({"APR (%)": "9.9", "Annual Percentage Rate (APR) (%)": "18.5"})
    assert both.values["Annual Percentage Rate (APR) (%)"] == "18.5"
    assert "Annual Percentage Rate (APR) (%)" not in both.drifted

    # 4. THE POINT. A document that never mentions the cooling-off period reports it
    #    missing, so it can never reach KFS's default of 0 and be read aloud as
    #    "इस लोन में कूलिंग-ऑफ की अवधि नहीं है।" — a sentence that is TRUE for fixture
    #    kfs_04_gold and a fabricated denial of a statutory right for anyone else.
    without = {k: v for k, v in baseline.items() if k != "Cooling-off period (days)"}
    assert "Cooling-off period (days)" in [f.label for f in resolve(without).missing()]

    #    An empty cell is a miss, not a zero...
    blank = baseline | {"Cooling-off period (days)": "  "}
    assert "Cooling-off period (days)" in [f.label for f in resolve(blank).missing()]
    #    ...but a declared 0 is a real answer and must NOT be reported missing.
    assert resolve(baseline | {"Cooling-off period (days)": "0"}).complete

    # 5. An unrelated cell is never guessed into a required field.
    assert resolve({"Total charges (Rs)": "9999"}).unknown == {"Total charges (Rs)": "9999"}

    # 6. Unknown rate kind does not silently demand the floating block.
    assert all(f.label != "Benchmark" for f in resolve({}).required())

    print(f"field manifest OK — {len(ALL)} labels, "
          f"{sum(len(f.synonyms) for f in ALL)} synonyms; "
          f"{fixed_seen} fixed + {floating_seen} floating fixture PDFs resolve complete")


if __name__ == "__main__":
    _demo()
