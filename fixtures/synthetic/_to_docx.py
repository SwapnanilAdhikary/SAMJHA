"""Render the JSON fixtures as KFS Word documents, so kfs/extract.py has real .docx to parse.

    PYTHONPATH=. uv run python fixtures/synthetic/_to_docx.py

Written with stdlib `zipfile` and hand-built XML rather than python-docx: a .docx is a zip
holding one XML part, and a KFS is a table, so this is a writer of about a hundred lines
against a dependency the project would otherwise not have.

Rows and labels come from `_to_pdf.py`'s `part1_rows()` and `fee_table()`, NOT from a
second copy of the label list. That is the point — the .docx and the .pdf are generated
from one source of truth, so `extract(docx) == extract(pdf)` is a real assertion about the
reader rather than an accident of two lists agreeing.

Two things here are deliberately adversarial, because they are what real Word does and
what a naive reader gets wrong:

  * **Cell text is split across several `w:t` runs.** Word breaks runs on revision-id and
    spellcheck boundaries, so "1,04,596" genuinely arrives as "1,04," + "596" in files
    people send. A reader that takes only the first `w:t` of a cell reads 1,04 — a
    hundred-fold error in a loan amount, with no exception raised. `_runs()` forces that
    split on every cell so the fixtures exercise it.
  * **One document carries a nested table** (see `NEST_IN`). `.//w:tr` finds a nested
    table's rows too, which would flatten a sub-table into its parent; the reader resolves
    each row to its nearest ancestor `w:tbl` instead. Untested code for that would be a
    liability, so one fixture tests it.

`Rs`, not `₹`, and Indian 2-2-3 grouping with commas — matching `_to_pdf.py` exactly.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

from fixtures.synthetic._to_pdf import fee_table, part1_rows
from kfs.schema import KFS

HERE = Path(__file__).parent
OUT = HERE / "docx"

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

# The one fixture that gets a nested table, to exercise nearest-ancestor row resolution.
NEST_IN = "kfs_03_consumer_durable"

CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""

RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""

BORDERS = (
    "<w:tblPr><w:tblBorders>"
    + "".join(f'<w:{e} w:val="single" w:sz="4" w:color="000000"/>'
              for e in ("top", "left", "bottom", "right", "insideH", "insideV"))
    + "</w:tblBorders></w:tblPr>"
)


def _runs(text: str) -> str:
    """Split one cell's text into several `w:t` runs, the way Word does.

    Deterministic (thirds) so the fixtures are reproducible, and lossless — concatenating
    the runs with no separator reproduces `text` exactly, which is precisely the contract
    the reader has to honour.
    """
    if len(text) < 5:
        pieces = [text]
    else:
        a, b = len(text) // 3, 2 * len(text) // 3
        pieces = [text[:a], text[a:b], text[b:]]
    # xml:space="preserve" or Word (and any conforming reader) may strip a leading or
    # trailing space, which would corrupt a value split mid-space.
    return "".join(
        f'<w:r><w:t xml:space="preserve">{escape(p)}</w:t></w:r>' for p in pieces if p
    )


def _para(text: str = "") -> str:
    return f"<w:p>{_runs(text)}</w:p>"


def _cell(text: str, nested: str = "") -> str:
    # A cell must end with a w:p even when it holds a table, or the part is invalid.
    body = _para(text) + (nested + _para() if nested else "")
    return f"<w:tc>{body}</w:tc>"


def _row(cells: list[str], nested_in: int | None = None, nested: str = "") -> str:
    out = [
        _cell(c, nested if nested_in == i else "")
        for i, c in enumerate(cells)
    ]
    return f"<w:tr>{''.join(out)}</w:tr>"


def _table(rows: list[list[str]], nested_at: tuple[int, int] | None = None,
           nested: str = "") -> str:
    body = []
    for r, cells in enumerate(rows):
        if nested_at is not None and nested_at[0] == r:
            body.append(_row(cells, nested_in=nested_at[1], nested=nested))
        else:
            body.append(_row(cells))
    return f"<w:tbl>{BORDERS}{''.join(body)}</w:tbl>"


def document_xml(k: KFS, *, nest: bool = False) -> str:
    """The whole `word/document.xml` for one KFS."""
    part1 = [[label, value] for label, value in part1_rows(k)]

    # A small sub-table inside a Part 1 cell. Its labels are intentionally NOT Annex-A
    # fields, so if the reader ever flattened it into the parent the extra rows would show
    # up as unmatched labels rather than corrupting a real one.
    nested = _table([
        ["Branch code", "BR-0194"],
        ["Sourcing channel", "Direct"],
    ]) if nest else ""

    blocks = [
        _para("KEY FACTS STATEMENT"),
        _para("RBI/2024-25/18, Annex A — Part 1. SYNTHETIC SPECIMEN, NOT A REAL LOAN."),
        # Nest inside the value cell of the LAST Part 1 row, so it sits deep in the table
        # rather than at a convenient edge.
        _table(part1, nested_at=(len(part1) - 1, 1) if nest else None, nested=nested),
        _para(),
        _para("Fees and charges"),
        _table(fee_table(k)),
    ]

    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{W}"><w:body>'
        + "".join(blocks)
        + "</w:body></w:document>"
    )


def render(k: KFS, path: Path, *, nest: bool = False) -> None:
    # ZIP_DEFLATED and a fixed date_time so regenerating produces identical bytes.
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in (
            ("[Content_Types].xml", CONTENT_TYPES),
            ("_rels/.rels", RELS),
            ("word/document.xml", document_xml(k, nest=nest)),
        ):
            info = zipfile.ZipInfo(name, date_time=(2026, 9, 7, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, data.encode("utf-8"))


def main() -> None:
    OUT.mkdir(exist_ok=True)
    for src in sorted(HERE.glob("*.json")):
        doc = KFS.model_validate(json.loads(src.read_text(encoding="utf-8")))
        dest = OUT / f"{src.stem}.docx"
        nest = src.stem == NEST_IN
        render(doc, dest, nest=nest)
        print(f"  {dest.relative_to(HERE.parent.parent)}" + ("   (nested table)" if nest else ""))
    print(f"\nwrote {len(list(OUT.glob('*.docx')))} .docx")


if __name__ == "__main__":
    main()
