"""PDF or Word -> KFS. Deterministic table parsing, Pydantic-validated, rejects on failure.

The KFS is a *standardised* form (RBI/2024-25/18, Annex A), which is the only reason this
can be deterministic rather than an LLM extraction step. Every row is a labelled
parameter/value pair and the fee sub-table has fixed columns, so we read the table grid and
look labels up. No model in the loop, and no regex over prose — the one regex here folds
punctuation and case in LABELS (`kfs/fields.norm`), never in a value.

Two rules that are load-bearing:

  * `loan_account_no` is read as a STRING and never touched. `int("0004512378")` is
    4512378 — the leading zeros this whole product exists to preserve would be destroyed
    before the delivery layer ever saw them.
  * Anything the schema rejects raises. A KFS that does not validate is not a KFS we are
    willing to read to a borrower as fact. So does anything `kfs/fields.py` marks required
    and absent: a field that fell through to a pydantic default would be read aloud as a
    positive statement about the loan. See that module's docstring — it is the whole
    reason it exists.

Three layers, so a new document format is a new grid reader and nothing else:

    read_tables(path)  -> grids      per-format: _grid_pdf | _grid_docx
    _classify(grids)   -> (part1, fee_rows)   format-agnostic, finds the fee sub-table
    fields.resolve()   -> canonical labels    absorbs a lender's own spellings

`read_tables` is the seam a camera/OCR reader plugs into: give it a grid and everything
downstream is unchanged. Vision is deliberately NOT implemented here — a photographed
document is not a table until something makes it one.

Self-check:  uv run python -m kfs.extract
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field as _dc_field
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pdfplumber

from kfs import fields
from kfs.schema import KFS, FeeLine, FloatingInfo, InstalmentDetails, InterestRate

PAYEE = {"re": "RE", "third party": "third_party"}
TIMING = {"one-time": "one_time", "recurring": "recurring"}

# WordprocessingML. One namespace, one part; a .docx is a zip and the tables are XML.
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

# Refuse absurd input before decompressing it. A .docx is XML, so it compresses hard and a
# malicious ratio is cheap to build; 40 MB of expanded XML is far more than any KFS.
MAX_DOCX_PART_BYTES = 40 * 1024 * 1024

SUPPORTED_SUFFIXES = (".pdf", ".docx")


class UnsupportedDocument(ValueError):
    """The file is not a format we can read a table grid out of."""


class IncompleteKFS(ValueError):
    """Required Annex-A fields were not found. Carries the list, for the review page."""

    def __init__(self, missing: list[str], report: ExtractReport | None = None) -> None:
        super().__init__(
            "KFS is missing required field(s): "
            + ", ".join(repr(m) for m in missing)
            + ". Refusing to build a KFS whose defaults would be read aloud as fact."
        )
        self.missing = missing
        self.report = report


# ------------------------------------------------------------------ grid readers


def read_tables(path: str | Path) -> list[list[list[str]]]:
    """Every table in the document, as grids of cell strings.

    Dispatches on suffix. Cells are already `_clean`ed, so a grid from Word and a grid
    from a PDF are interchangeable downstream.
    """
    suffix = Path(path).suffix.lower()
    if suffix == ".pdf":
        return _grid_pdf(path)
    if suffix == ".docx":
        return _grid_docx(path)
    raise UnsupportedDocument(
        f"{path}: cannot read {suffix or 'a file with no extension'!r}. "
        f"Supported: {', '.join(SUPPORTED_SUFFIXES)}."
    )


def _grid_pdf(path: str | Path) -> list[list[list[str]]]:
    with pdfplumber.open(path) as pdf:
        tables = [t for page in pdf.pages for t in (page.extract_tables() or [])]
    return [[[_clean(c or "") for c in row] for row in t] for t in tables]


def _grid_docx(path: str | Path) -> list[list[list[str]]]:
    """Read `word/document.xml`'s tables with the stdlib. No python-docx.

    Two details that would silently corrupt cells if ignored:

      * Word splits a single cell's text across many `w:t` runs on revision-id and
        spellcheck boundaries, so "1,04,596" can arrive as "1,04," + "596". Every `w:t`
        descendant of a paragraph is concatenated with no separator, and paragraphs within
        a cell are joined with a space.
      * **Nesting is resolved by nearest ancestor, at all three levels.** `.//w:tr` finds a
        nested table's rows, `.//w:tc` its cells, and — the one that is easy to miss —
        `.//w:p` finds the nested table's PARAGRAPHS, so a cell containing a sub-table
        would otherwise absorb every word of it. Reading a KFS whose last cell is
        "1800 233 7788 Branch code BR-0194 Sourcing channel Direct" is how that
        manifests. Every row, cell and paragraph is therefore attributed to its nearest
        enclosing table/row/cell, and anything belonging to something deeper is skipped.
    """
    import xml.etree.ElementTree as ET  # noqa: PLC0415  (stdlib, only needed for .docx)

    with zipfile.ZipFile(path) as z:
        try:
            info = z.getinfo("word/document.xml")
        except KeyError:
            raise UnsupportedDocument(
                f"{path}: no word/document.xml — not a Word document"
            ) from None
        if info.file_size > MAX_DOCX_PART_BYTES:
            raise UnsupportedDocument(
                f"{path}: word/document.xml expands to {info.file_size} bytes, "
                f"over the {MAX_DOCX_PART_BYTES} cap"
            )
        xml = z.read("word/document.xml")

    root = ET.fromstring(xml)
    # ElementTree has no parent pointers, so build the one lookup we need.
    parent = {child: node for node in root.iter() for child in node}

    def nearest(node, tag: str):
        """The closest enclosing element with `tag`, starting at `node` itself."""
        while node is not None:
            if node.tag == tag:
                return node
            node = parent.get(node)
        return None

    def cell_text(tc) -> str:
        # Only paragraphs whose nearest cell is THIS cell. A w:p cannot contain a table,
        # so gathering every w:t inside one paragraph is safe.
        paras = [
            "".join(t.text or "" for t in p.iter(f"{_W}t"))
            for p in tc.iter(f"{_W}p")
            if nearest(p, f"{_W}tc") is tc
        ]
        return _clean(" ".join(p for p in paras if p))

    grids: dict[object, list[list[str]]] = {tbl: [] for tbl in root.iter(f"{_W}tbl")}
    for tr in root.iter(f"{_W}tr"):
        tbl = nearest(tr, f"{_W}tbl")
        if tbl is None:
            continue
        cells = [
            cell_text(tc) for tc in tr.iter(f"{_W}tc")
            if nearest(tc, f"{_W}tr") is tr
        ]
        grids[tbl].append(cells)

    return [rows for rows in grids.values() if rows]


# ------------------------------------------------------------------ classification


def _classify(grids: list[list[list[str]]]) -> tuple[dict[str, str], list[list[str]]]:
    """(Part 1 as label -> value, fee sub-table rows without its header).

    The fee table is found by its header cell rather than by index, so a future Part 2
    table appearing between them does not silently shift the parse. The header repeats on
    every fragment (reportlab `repeatRows=1`), so a table split across pages still
    classifies.
    """
    part1: dict[str, str] = {}
    fee_rows: list[list[str]] = []

    for table in grids:
        if not table:
            continue
        header = " ".join(table[0]).lower()
        if fields.FEE_TABLE_HEADER_CELL in header:
            fee_rows += [r for r in table[1:] if any(c for c in r)]
        else:
            for row in table:
                if len(row) >= 2 and row[0] and row[1] is not None:
                    part1[row[0]] = row[1]

    return part1, fee_rows


def _tables(path: str | Path) -> tuple[dict[str, str], list[list[str]]]:
    """Read and classify in one step. The shape the parser has always consumed."""
    grids = read_tables(path)
    if not grids:
        raise UnsupportedDocument(
            f"{path}: no tables found — not a KFS in the expected format. A document that "
            f"lays the KFS out as prose, or embeds it as an image, needs a reader "
            f"read_tables() does not have."
        )
    return _classify(grids)


# ------------------------------------------------------------------ reporting


@dataclass
class FieldReport:
    """One Annex-A field as read, for the human review step.

    `document_label` is the lender's own spelling, kept verbatim: a reviewer approving a
    synonym match deserves to see what was actually printed rather than the canonical
    label we mapped it onto.
    """

    label: str
    value: str
    status: str  # "parsed" | "drifted" | "missing"
    document_label: str = ""
    derived: bool = False
    prompt: str = ""


@dataclass
class ExtractReport:
    """Everything read from one document, without judgement about whether it is enough."""

    path: str
    fields: list[FieldReport] = _dc_field(default_factory=list)
    fee_rows: list[list[str]] = _dc_field(default_factory=list)
    fee_table_found: bool = False
    unknown: dict[str, str] = _dc_field(default_factory=dict)
    rate_kind: str = ""

    @property
    def missing(self) -> list[str]:
        return [f.label for f in self.fields if f.status == "missing"]

    @property
    def drifted(self) -> list[FieldReport]:
        return [f for f in self.fields if f.status == "drifted"]

    @property
    def complete(self) -> bool:
        return not self.missing


def extract_fields(path: str | Path) -> ExtractReport:
    """Read a document and report every required field's status. Does NOT raise on a miss.

    This is what the intake page calls: a half-read document must produce a reviewable
    list, not a stack trace. `extract()` is the strict wrapper for everything else.
    """
    part1, fee_rows = _tables(path)
    res = fields.resolve(part1)
    missing = {f.label for f in res.missing()}

    report = ExtractReport(
        path=str(path),
        fee_rows=fee_rows,
        # An empty fee table is a real answer ("no fees on this loan"); a fee table that
        # was never found is not. build_clauses reads the empty case aloud, so the two
        # must stay distinguishable.
        fee_table_found=bool(fee_rows),
        unknown=dict(res.unknown),
        rate_kind=res.rate_kind(),
    )

    for f in res.required():
        value = res.values.get(f.label, "")
        report.fields.append(FieldReport(
            label=f.label,
            value=value,
            status="missing" if f.label in missing
            else ("drifted" if f.label in res.drifted else "parsed"),
            document_label=res.matched_by.get(f.label, ""),
            derived=fields.is_derived(f.label),
            prompt=fields.prompt_for(f.label),
        ))

    return report


def build_from_fields(part1: dict[str, str], fee_rows: list[list[str]]) -> KFS:
    """Canonical label -> value plus fee rows -> a validated KFS.

    Split out from `extract()` so a document whose missing fields a human supplied goes
    through exactly the same construction and validation as one read cleanly.
    """
    floating = None
    if _text(part1, "Interest rate type").lower() != "fixed":
        floating = FloatingInfo(
            benchmark=_text(part1, "Benchmark"),
            spread_pct=_num(part1, "Spread (%)"),
            reset_periodicity_months=int(_num(part1, "Reset periodicity (months)")),
            impact_25bps_on_epi=_num(part1, "Impact of 25 bps change on EPI (Rs)"),
            impact_25bps_on_num_epis=int(_num(part1, "Impact of 25 bps change on no. of EPIs")),
        )

    return KFS(
        proposal_no=_text(part1, "Loan proposal number"),
        loan_type=_text(part1, "Type of loan"),
        # Never int(): leading zeros are the payload.
        loan_account_no=_text(part1, "Loan account number"),
        sanctioned_amount_inr=_num(part1, "Sanctioned loan amount (Rs)"),
        disbursal_mode="stages" if "stage" in _text(part1, "Disbursal schedule").lower() else "upfront",
        loan_term_months=int(_num(part1, "Loan term (months)")),
        instalments=InstalmentDetails(
            type_of_instalments=_text(part1, "Type of instalments"),
            number_of_epis=int(_num(part1, "Number of EPIs")),
            epi_amount_inr=_num(part1, "EPI amount (Rs)"),
            repayment_commencement=_text(part1, "Commencement of repayment"),
        ),
        interest=InterestRate(
            pct=_num(part1, "Interest rate (% p.a.)"),
            kind=_text(part1, "Interest rate type").lower(),
        ),
        floating=floating,
        fees=[_fee(r) for r in fee_rows],
        apr_pct=_num(part1, "Annual Percentage Rate (APR) (%)"),
        total_repayment_inr=_num(part1, "Total amount to be paid (Rs)"),
        cooling_off_period_days=int(_num(part1, "Cooling-off period (days)")),
        recovery_agent_clause=_text(part1, "Recovery agents"),
        grievance_officer_name=_text(part1, "Grievance redressal officer"),
        grievance_officer_phone=_text(part1, "Grievance redressal phone"),
    )


def extract(pdf_path: str | Path) -> KFS:
    """Parse a KFS document (.pdf or .docx). Strict: raises rather than guessing.

    Raises `IncompleteKFS` if a required Annex-A field is absent, `UnsupportedDocument` if
    there is no readable table grid, and `pydantic.ValidationError` if what was read does
    not conform.
    """
    report = extract_fields(pdf_path)
    if not report.complete:
        raise IncompleteKFS(report.missing, report)
    values = {f.label: f.value for f in report.fields}
    return build_from_fields(values, report.fee_rows)


# ------------------------------------------------------------------ cell helpers


def _fee(row: list[str]) -> FeeLine:
    item, payee, timing, amount, pct, gst = (_clean(c or "") for c in (list(row) + [""] * 6)[:6])
    try:
        payable_to = PAYEE[payee.lower()]
        when = TIMING[timing.lower()]
    except KeyError:
        raise ValueError(
            f"fee row {item!r}: unrecognised payee/timing {payee!r}/{timing!r}. "
            f"Expected one of {sorted(PAYEE)} and {sorted(TIMING)}."
        ) from None
    return FeeLine(
        item=item,
        payable_to=payable_to,
        timing=when,
        amount_inr=_parse_money(amount),
        percentage=_parse_money(pct),
        net_of_gst=gst.lower().startswith("y"),
    )


def _clean(cell: str) -> str:
    """Cell text with soft wraps collapsed back to single spaces."""
    return re.sub(r"\s+", " ", cell).strip()


def _text(part1: dict[str, str], label: str) -> str:
    try:
        return part1[label]
    except KeyError:
        raise ValueError(f"KFS row missing: {label!r}. Found: {sorted(part1)}") from None


def _num(part1: dict[str, str], label: str) -> Decimal:
    value = _parse_money(_text(part1, label))
    if value is None:
        raise ValueError(f"KFS row {label!r} is not a number: {_text(part1, label)!r}")
    return value


def _parse_money(s: str) -> Decimal | None:
    """'11,800' -> Decimal('11800'). '-' and '' -> None (an absent sub-table cell)."""
    stripped = re.sub(r"[^\d.]", "", s)
    if not stripped:
        return None
    try:
        return Decimal(stripped)
    except InvalidOperation:
        # e.g. a cell reading "1.2.3" — malformed, not absent. Say which.
        raise ValueError(f"not a number: {s!r}") from None


def _demo() -> None:
    """Round-trip every generated document against the JSON fixture it came from."""
    import json

    root = Path(__file__).parent.parent / "fixtures/synthetic"
    pdfs = sorted((root / "pdf").glob("*.pdf"))
    if not pdfs:
        raise SystemExit("no PDFs — run: PYTHONPATH=. uv run python fixtures/synthetic/_to_pdf.py")

    docxs = {p.stem: p for p in sorted((root / "docx").glob("*.docx"))}

    for pdf in pdfs:
        got = extract(pdf)
        want = KFS.model_validate(json.loads((root / f"{pdf.stem}.json").read_text("utf-8")))
        assert got == want, f"{pdf.name} did not round-trip"

        # The whole proof that the format split is correct: the SAME document in Word
        # yields a byte-identical KFS. Decimal equality, so 18.5 != 18.50 would fail.
        docx = docxs.get(pdf.stem)
        same = "" if docx is None else "  == .docx"
        if docx is not None:
            assert extract(docx) == want, f"{docx.name} did not match {pdf.name}"

        print(f"  {pdf.name:<40} acct {got.loan_account_no:>16}  APR {got.apr_pct}%{same}")

    # Strictness: a document missing a required row is refused, and says which.
    part1, fee_rows = _tables(pdfs[0])
    del part1["Cooling-off period (days)"]
    try:
        build_from_fields(fields.resolve(part1).values, fee_rows)
    except ValueError as e:
        assert "Cooling-off period" in str(e)
    else:
        raise AssertionError("a missing cooling-off period must not fall through to 0")

    # And label drift is absorbed rather than refused.
    drifted = {("APR (%)" if k == "Annual Percentage Rate (APR) (%)" else k): v
               for k, v in _tables(pdfs[0])[0].items()}
    assert extract_fields(pdfs[0]).complete
    assert fields.resolve(drifted).complete, "a lender's own APR spelling must still resolve"

    n_docx = len(docxs)
    print(f"\n{len(pdfs)} PDFs round-tripped exactly, leading zeros intact"
          + (f"; {n_docx} .docx matched them" if n_docx else "; no .docx fixtures yet"))


if __name__ == "__main__":
    _demo()
