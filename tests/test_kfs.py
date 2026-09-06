"""The document layer's invariants.

Four of these are not style checks — break one and the consent mechanic becomes a lie:

  * a Segment carries AT MOST ONE key value (heard-through accounting is per segment);
  * a Segment that DECLARES a value actually speaks it (otherwise the ledger records a
    value as heard that was never uttered);
  * account identifiers keep their leading zeros end to end, JSON -> PDF -> clause;
  * no segment exceeds Rime's 1000-character cap, which returns HTTP 400.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path

import pytest

from delivery.normalizer.hindi import digits
from kfs.build_clauses import build_clauses, inr
from kfs.extract import extract
from kfs.finance import apr_pct, emi
from kfs.schema import KFS

FIXTURES = sorted((Path(__file__).parent.parent / "fixtures/synthetic").glob("*.json"))
PDFS = sorted((Path(__file__).parent.parent / "fixtures/synthetic/pdf").glob("*.pdf"))

RIME_CHAR_CAP = 1000

# Devanagari digits U+0966-U+096F alongside ASCII. NFKC does NOT fold these, which is why
# they are listed explicitly here and in the eval scorer.
ANY_DIGIT = re.compile(r"[0-9०-९]")


def load(path: Path) -> KFS:
    return KFS.model_validate(json.loads(path.read_text(encoding="utf-8")))


def tokens(text: str) -> list[str]:
    """Words, with Devanagari punctuation stripped. Token matching, not substring:
    'एक' must not match inside 'एक-एक'."""
    return re.sub(r"[।,.?!]", " ", text).split()


def contains_phrase(text: str, phrase: str) -> bool:
    hay, needle = tokens(text), tokens(phrase)
    if not needle:
        return False
    return any(hay[i : i + len(needle)] == needle for i in range(len(hay) - len(needle) + 1))


@pytest.fixture(scope="module")
def docs() -> list[tuple[Path, KFS]]:
    assert FIXTURES, "no fixtures — run: PYTHONPATH=. uv run python fixtures/synthetic/_generate.py"
    return [(p, load(p)) for p in FIXTURES]


class TestFixtures:
    def test_ten_documents(self):
        assert len(FIXTURES) == 10

    @pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
    def test_validates(self, path):
        load(path)  # pydantic raises if it does not conform

    @pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
    def test_within_stated_ranges(self, path):
        """The ranges the corpus was designed to cover; a fixture outside them is a typo."""
        doc = load(path)
        assert Decimal("25000") <= doc.sanctioned_amount_inr <= Decimal("500000")
        assert Decimal("14") <= doc.apr_pct <= Decimal("34")
        assert 12 <= doc.loan_term_months <= 60

    @pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
    def test_emi_and_apr_are_internally_consistent(self, path):
        """APR is monthly IRR x 12 on the NET disbursed amount — recomputed, not trusted."""
        doc = load(path)
        assert doc.instalments.epi_amount_inr == emi(
            doc.sanctioned_amount_inr, doc.interest.pct, doc.loan_term_months
        )
        assert doc.apr_pct == apr_pct(
            doc.net_disbursed_inr, doc.instalments.epi_amount_inr, doc.instalments.number_of_epis
        )
        # Fees make the APR exceed the nominal rate. If it does not, the fee sub-table or
        # net_disbursed is wrong — the exact error that makes a lender look compliant.
        assert doc.apr_pct > doc.interest.pct

    def test_account_numbers_vary_in_shape(self):
        """Lengths 10-16, and several with leading zeros. Both are the failure surface."""
        accounts = [load(p).loan_account_no for p in FIXTURES]
        assert all(10 <= len(a) <= 16 for a in accounts), accounts
        assert len({len(a) for a in accounts}) >= 5
        assert sum(a.startswith("0") for a in accounts) >= 4

    def test_one_document_is_floating_with_a_reset_clause(self):
        floating = [load(p) for p in FIXTURES if load(p).floating is not None]
        assert len(floating) == 1
        f = floating[0].floating
        assert f.reset_periodicity_months > 0
        assert f.impact_25bps_on_epi is not None


class TestClauses:
    @pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
    def test_every_clause_has_at_least_one_segment(self, path):
        clauses = build_clauses(load(path))
        assert clauses
        for clause in clauses:
            assert clause.segments, f"{clause.id} has no segments"

    @pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
    def test_clause_ids_are_unique(self, path):
        ids = [c.id for c in build_clauses(load(path))]
        assert len(ids) == len(set(ids))

    @pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
    def test_no_segment_carries_more_than_one_key_value(self, path):
        """THE invariant. Checked structurally AND in the text.

        Structurally it is one field, so the way this actually breaks is a segment whose
        text mentions a second value it does not declare — at which point "did segment k
        finish playing?" no longer answers "did she hear the APR?".
        """
        for clause in build_clauses(load(path)):
            for seg in clause.segments:
                others = [kv for kv in clause.key_values if kv is not seg.key_value]
                rest = seg.text
                if seg.key_value is not None:
                    rest = rest.replace(seg.key_value.spoken_text, " ")
                for kv in others:
                    assert not contains_phrase(rest, kv.spoken_text), (
                        f"{path.stem}/{clause.id}: segment also speaks {kv.kind}"
                        f"={kv.value!r}\n  {seg.text}"
                    )

    @pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
    def test_declared_value_is_actually_spoken(self, path):
        """A segment claiming to carry a value must utter it, or the ledger lies."""
        for clause in build_clauses(load(path)):
            for seg in clause.segments:
                if seg.key_value is not None:
                    assert seg.key_value.spoken_text in seg.text, f"{clause.id}: {seg.text}"

    @pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
    def test_segments_are_under_rimes_character_cap(self, path):
        """1000 chars per request; beyond it Rime returns HTTP 400."""
        for clause in build_clauses(load(path)):
            for seg in clause.segments:
                assert len(seg.text) < RIME_CHAR_CAP, f"{clause.id}: {len(seg.text)} chars"

    @pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
    def test_no_unverbalised_digits_reach_the_tts(self, path):
        """Every number is words by the time it is sent. A stray digit run is exactly the
        input the day-1 pilot showed Rime reads as an Indian-scale quantity."""
        for clause in build_clauses(load(path)):
            for seg in clause.segments:
                assert not ANY_DIGIT.search(seg.text), f"{clause.id}: {seg.text}"

    @pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
    def test_all_six_categories_are_reachable(self, path):
        """The corpus reports all six categories, including the four negative results."""
        kinds = {kv.kind for c in build_clauses(load(path)) for kv in c.key_values}
        assert {"rupee_amount", "percentage_apr", "tenure_months", "emi_amount"} <= kinds
        assert "account_identifier" in kinds
        assert "date_deadline" in kinds or load(path).cooling_off_period_days == 0

    @pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
    def test_typed_values_are_never_strings_except_identifiers(self, path):
        for clause in build_clauses(load(path)):
            for kv in clause.key_values:
                if kv.kind == "account_identifier":
                    assert isinstance(kv.value, str)
                else:
                    assert isinstance(kv.value, (int, Decimal)) and not isinstance(kv.value, bool)


class TestLeadingZeros:
    """The pilot's worst raw failure: 000512348899 -> 95058000000. Guarded at every hop."""

    @pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
    def test_account_survives_json_to_clause(self, path):
        doc = load(path)
        acct = doc.loan_account_no
        (kv,) = [
            kv
            for c in build_clauses(doc)
            if c.id == "identity"
            for kv in c.key_values
            if kv.kind == "account_identifier"
        ]
        assert kv.value == acct
        assert isinstance(kv.value, str)
        # One spoken token per input digit — no digits dropped, none invented. NeMo's
        # telephone grammar adds a spurious leading शून्य here; ours must not.
        assert len(kv.spoken_text.split()) == len(acct)

    @pytest.mark.parametrize("path", [p for p in FIXTURES if load(p).loan_account_no.startswith("0")],
                             ids=lambda p: p.stem)
    def test_leading_zeros_are_spoken(self, path):
        acct = load(path).loan_account_no
        leading = len(acct) - len(acct.lstrip("0"))
        assert digits(acct).split()[:leading] == ["शून्य"] * leading


@pytest.mark.skipif(not PDFS, reason="no PDFs — run fixtures/synthetic/_to_pdf.py")
class TestExtract:
    @pytest.mark.parametrize("pdf", PDFS, ids=lambda p: p.stem)
    def test_pdf_round_trips_to_the_source_fixture(self, pdf):
        assert extract(pdf) == load(pdf.parent.parent / f"{pdf.stem}.json")

    @pytest.mark.parametrize("pdf", PDFS, ids=lambda p: p.stem)
    def test_account_number_is_read_as_a_string(self, pdf):
        got = extract(pdf).loan_account_no
        want = load(pdf.parent.parent / f"{pdf.stem}.json").loan_account_no
        assert got == want  # int() would silently eat the leading zeros

    def test_a_pdf_with_no_tables_is_rejected(self, tmp_path):
        path = _table_pdf(tmp_path / "prose.pdf", [])
        with pytest.raises(ValueError, match="no tables"):
            extract(path)

    def test_a_pdf_missing_required_rows_is_rejected(self, tmp_path):
        """Reject rather than return a half-filled KFS. A document we cannot read
        completely is not one we are willing to read to a borrower as fact."""
        path = _table_pdf(tmp_path / "partial.pdf", [
            ("Loan proposal number", "PL/2026/000001"),
            ("Type of loan", "Personal Loan"),
        ])
        with pytest.raises(ValueError, match="KFS row missing"):
            extract(path)


def _table_pdf(path: Path, rows: list[tuple[str, str]]) -> Path:
    """A minimal bordered two-column PDF, for the reject paths only."""
    from reportlab.lib import colors
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Table, TableStyle
    from reportlab.lib.styles import getSampleStyleSheet

    style = getSampleStyleSheet()["BodyText"]
    flowables = [Paragraph("KEY FACTS STATEMENT", style)]
    if rows:
        table = Table([[Paragraph(a, style), Paragraph(b, style)] for a, b in rows])
        table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.black)]))
        flowables.append(table)
    SimpleDocTemplate(str(path)).build(flowables)
    return path


def test_indian_grouping():
    """2-2-3 from the right, as a KFS prints it — not thousands separators."""
    assert inr(Decimal("104596")) == "1,04,596"
    assert inr(Decimal("500000")) == "5,00,000"
    assert inr(Decimal("850")) == "850"
    assert inr(Decimal("12494")) == "12,494"
