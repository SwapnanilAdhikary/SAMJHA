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
import zipfile
from decimal import Decimal
from pathlib import Path

import pytest

from delivery.normalizer.hindi import digits
from kfs.build_clauses import build_clauses, inr
from kfs.extract import IncompleteKFS, UnsupportedDocument, extract, extract_fields
from kfs.finance import apr_pct, emi
from kfs.schema import KFS

FIXTURES = sorted((Path(__file__).parent.parent / "fixtures/synthetic").glob("*.json"))
PDFS = sorted((Path(__file__).parent.parent / "fixtures/synthetic/pdf").glob("*.pdf"))
DOCXS = sorted((Path(__file__).parent.parent / "fixtures/synthetic/docx").glob("*.docx"))

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
        completely is not one we are willing to read to a borrower as fact.

        The error names EVERY missing field, not just the first one it tripped over: the
        intake page shows that list to a human, and one-at-a-time discovery would mean one
        re-upload per missing row.
        """
        path = _table_pdf(tmp_path / "partial.pdf", [
            ("Loan proposal number", "PL/2026/000001"),
            ("Type of loan", "Personal Loan"),
        ])
        with pytest.raises(IncompleteKFS) as e:
            extract(path)

        # Named explicitly because this is the field whose pydantic default (0) would be
        # spoken as "इस लोन में कूलिंग-ऑफ की अवधि नहीं है।" — a fabricated denial of a
        # statutory right. See kfs/fields.py.
        assert "Cooling-off period (days)" in e.value.missing
        assert "Annual Percentage Rate (APR) (%)" in e.value.missing
        # The two rows that WERE present must not be reported missing.
        assert "Loan proposal number" not in e.value.missing
        assert "Type of loan" not in e.value.missing
        # Still a ValueError, so callers that only catch that keep working.
        assert isinstance(e.value, ValueError)


@pytest.mark.skipif(not DOCXS, reason="no .docx — run fixtures/synthetic/_to_docx.py")
class TestDocx:
    """Word support. The format split is only correct if both formats agree exactly."""

    @pytest.mark.parametrize("docx", DOCXS, ids=lambda p: p.stem)
    def test_docx_round_trips_to_the_source_fixture(self, docx):
        assert extract(docx) == load(docx.parent.parent / f"{docx.stem}.json")

    @pytest.mark.parametrize("docx", DOCXS, ids=lambda p: p.stem)
    def test_docx_and_pdf_yield_the_identical_kfs(self, docx):
        """THE assertion for the format split, on Decimal equality so 18.5 != 18.50.

        One document, two formats, one reader downstream of `read_tables`. If this holds
        for all ten, `_grid_docx` and `_grid_pdf` are interchangeable and every existing
        guarantee about the PDF path applies to Word too.
        """
        pdf = docx.parent.parent / "pdf" / f"{docx.stem}.pdf"
        if not pdf.exists():
            pytest.skip(f"no matching PDF for {docx.name}")
        assert extract(docx) == extract(pdf)

    def test_cell_text_split_across_runs_is_rejoined(self):
        """Word splits a cell across `w:t` runs; the fixtures force that split.

        Taking only the first run of "1,04,596" reads 1,04 — a hundred-fold error in a
        loan amount, with nothing raised. Assert against the JSON, which never went
        through XML at all.
        """
        from kfs.extract import read_tables

        docx = DOCXS[0]
        want = load(docx.parent.parent / f"{docx.stem}.json")

        # The fixture really is multi-run, or this test proves nothing. Counted on the
        # exact opening tags: bare "<w:t" is also a prefix of <w:tc>, <w:tr>, <w:tbl>.
        raw = zipfile.ZipFile(docx).read("word/document.xml").decode()
        text_runs, cells = raw.count("<w:t "), raw.count("<w:tc>")
        assert cells > 0
        assert text_runs > 2 * cells, (
            f"fixture is not split across runs: {text_runs} w:t for {cells} w:tc"
        )

        assert extract(docx).sanctioned_amount_inr == want.sanctioned_amount_inr
        assert any("," in c for row in read_tables(docx)[0] for c in row), \
            "expected grouped amounts to survive the run rejoin"

    def test_a_nested_table_does_not_bleed_into_its_parent_cell(self):
        """`.//w:p` finds a nested table's paragraphs, so a cell holding a sub-table would
        otherwise absorb every word of it.

        fixtures/synthetic/_to_docx.py nests a Branch code / Sourcing channel table in the
        LAST Part 1 value cell — the grievance phone. This caught a real bug on first run.
        """
        nested = next((d for d in DOCXS if d.stem == "kfs_03_consumer_durable"), None)
        assert nested is not None, "the nested-table fixture is gone; _to_docx.NEST_IN moved?"

        got = extract(nested)
        want = load(nested.parent.parent / f"{nested.stem}.json")
        assert got.grievance_officer_phone == want.grievance_officer_phone
        for leaked in ("Branch code", "BR-0194", "Sourcing channel", "Direct"):
            assert leaked not in got.grievance_officer_phone

    def test_an_unreadable_format_is_refused_by_name(self, tmp_path):
        bad = tmp_path / "scan.jpg"
        bad.write_bytes(b"\xff\xd8\xff\xe0not an image really")
        with pytest.raises(UnsupportedDocument, match=r"\.jpg"):
            extract(bad)

    def test_a_zip_that_is_not_a_word_document_is_refused(self, tmp_path):
        notdocx = tmp_path / "archive.docx"
        with zipfile.ZipFile(notdocx, "w") as z:
            z.writestr("hello.txt", "not a word document")
        with pytest.raises(UnsupportedDocument, match="word/document.xml"):
            extract(notdocx)


class TestNoFabricationFromDefaults:
    """The worst thing this system could do is state a fact it never read.

    `kfs/schema.py` defaults `cooling_off_period_days` to 0 and `fees` to [], and
    `kfs/build_clauses.py` reads both falsy cases aloud as positive statements. These
    tests name the exact Hindi sentence rather than asserting a boolean, because the
    sentence is the harm and a boolean assertion would survive a refactor that changed it.
    """

    NO_COOLING_OFF = "इस लोन में कूलिंग-ऑफ की अवधि नहीं है।"
    NO_FEES = "इस लोन पर कोई अलग फीस नहीं है।"

    def _spoken(self, kfs: KFS, clause_id: str) -> str:
        clause = next(c for c in build_clauses(kfs) if c.id == clause_id)
        return " ".join(s.text for s in clause.segments)

    def test_the_fabrication_sentences_still_exist_as_written(self, docs):
        """Guard the guard: if build_clauses rewords these, the tests below go quiet.

        kfs_04_gold declares cooling_off_period_days = 0 LEGITIMATELY, so this sentence is
        a true statement there — which is exactly why the value alone can never be used to
        detect a fabrication.
        """
        gold = next(k for p, k in docs if p.stem == "kfs_04_gold")
        assert gold.cooling_off_period_days == 0
        assert self.NO_COOLING_OFF in self._spoken(gold, "prepayment")

    def test_an_omitted_cooling_off_period_cannot_reach_the_default(self, docs):
        """A field absent from the document must never become "you have no such right"."""
        _, kfs = docs[0]
        raw = json.loads((FIXTURES[0]).read_text(encoding="utf-8"))
        assert raw["cooling_off_period_days"] != 0, "pick a fixture that HAS a cooling-off period"

        raw.pop("cooling_off_period_days")
        # Pydantic is perfectly happy: the default makes this a valid KFS...
        silently_defaulted = KFS.model_validate(raw)
        # ...and build_clauses then speaks a denial of a statutory right.
        assert self.NO_COOLING_OFF in self._spoken(silently_defaulted, "prepayment")

        # Which is why the manifest refuses it BEFORE a KFS is ever constructed.
        from kfs import fields

        part1 = {f.label: "x" for f in fields.PART1}
        del part1["Cooling-off period (days)"]
        assert "Cooling-off period (days)" in [f.label for f in fields.resolve(part1).missing()]

    def test_an_omitted_fee_table_cannot_reach_the_empty_default(self):
        raw = json.loads((FIXTURES[0]).read_text(encoding="utf-8"))
        assert raw["fees"], "pick a fixture that HAS fees"
        raw["fees"] = []
        assert self.NO_FEES in self._spoken(KFS.model_validate(raw), "fees")

        # An empty fee table is a real answer; a fee table never found is not. extract_fields
        # keeps them distinguishable so the intake page can tell a human which it saw.
        assert extract_fields(PDFS[0]).fee_table_found

    @pytest.mark.skipif(not PDFS, reason="no PDFs")
    def test_every_fixture_pdf_reports_complete(self):
        """No fixture relies on a default to be readable."""
        for pdf in PDFS:
            report = extract_fields(pdf)
            assert report.complete, f"{pdf.name} incomplete: {report.missing}"
            assert not report.drifted, f"{pdf.name} matched canonical labels as synonyms"


class TestLabelDrift:
    """A real lender's KFS does not use Annex A's exact spellings."""

    def test_a_lenders_own_spellings_resolve(self):
        from kfs import fields

        res = fields.resolve({
            "Loan Proposal No.": "PL/2026/1",
            "APR (%)": "18.5",
            "Sanctioned Amount (Rs)": "1,25,000",
            "Loan Tenure (months)": "36",
        })
        assert res.values["Loan proposal number"] == "PL/2026/1"
        assert res.values["Annual Percentage Rate (APR) (%)"] == "18.5"
        assert res.values["Sanctioned loan amount (Rs)"] == "1,25,000"
        assert res.values["Loan term (months)"] == "36"

    def test_drift_is_reported_with_the_documents_own_wording(self):
        from kfs import fields

        res = fields.resolve({"APR (%)": "18.5"})
        assert res.matched_by["Annual Percentage Rate (APR) (%)"] == "APR (%)"
        assert "Annual Percentage Rate (APR) (%)" in res.drifted

    def test_an_unrelated_label_is_never_guessed_into_a_required_field(self):
        from kfs import fields

        # "Total charges" is NOT "Total amount to be paid". Guessing would put a wrong
        # number into a consent record.
        res = fields.resolve({"Total charges (Rs)": "9,999"})
        assert res.unknown == {"Total charges (Rs)": "9,999"}
        assert "Total amount to be paid (Rs)" not in res.values

    def test_a_fixed_rate_loan_is_not_asked_for_the_floating_block(self):
        from kfs import fields

        fixed = fields.resolve({f.label: "x" for f in fields.PART1} | {"Interest rate type": "Fixed"})
        assert fixed.complete
        floating = fields.resolve({f.label: "x" for f in fields.PART1} | {"Interest rate type": "Floating"})
        assert "Benchmark" in [f.label for f in floating.missing()]


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
