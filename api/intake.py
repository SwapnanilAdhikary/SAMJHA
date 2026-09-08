"""Document intake: uploaded bytes -> a reviewable KFS draft, with provenance attached.

Pure logic, no FastAPI. `api/main.py` supplies the bytes; everything about deciding what
they are, where they go, what was read, and what a human changed lives here so it can be
self-checked without a server.

Three rules the rest of the system depends on:

  * **Format comes from magic bytes, never the filename.** A file called `kfs.pdf` that is
    really a zip, or `scan.docx` that is really a JPEG, is refused rather than handed to a
    parser that will fail confusingly. The filename is untrusted input and is stored only
    as a label — never used as a path.

  * **Source bytes are content-addressed** at `data/kfs/{sha256}.{ext}`, so the document a
    consent record quotes can be re-verified against the record years later, and two
    uploads of the same file are the same stored object. The filename never touches the
    filesystem, which also disposes of path traversal.

  * **A draft is not a KFS.** A document whose required fields were not all read gets
    `status='draft'` and `kfs=NULL`; `confirm()` refuses while anything is missing. Nothing
    downstream can start a call from a document that has no KFS, so a half-read loan cannot
    become a call that reads 11 of 12 clauses as though nothing were wrong.

On real borrower data: `synthetic` is an UPLOADER ASSERTION, never inferred, and the server
refuses a non-synthetic upload unless `SAMJHA_ALLOW_REAL_DATA` is set. README.md's promise
("All data is synthetic. No real borrower data, ever.") therefore stays true by default
rather than by hope, and the record carries the assertion plus who made it.

Self-check:  uv run python -m api.intake
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import time
import uuid
import zipfile
from pathlib import Path

from kfs import fields as kfs_fields
from kfs.extract import (
    ExtractReport,
    UnsupportedDocument,
    build_from_fields,
    extract_fields,
)
from kfs.schema import KFS

# 10 MB. A KFS is two tables; anything larger is a mistake or an attack, and the cap is
# enforced while streaming so the bytes are never all held at once.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024

PDF_MAGIC = b"%PDF"
ZIP_MAGIC = b"PK\x03\x04"

MEDIA_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
METHODS = {".pdf": "pdf_table", ".docx": "docx_table"}

# Who performed the upload. Required, and recorded in the hashed consent record: reading a
# KFS aloud is a regulated disclosure, and an auditor should be able to see whether one
# person filled every role.
UPLOAD_ROLES = ("lender_system", "branch_helper", "borrower")

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._ -]")


class RealDataRefused(ValueError):
    """A non-synthetic upload was attempted on a server not configured to accept one."""


def allow_real_data() -> bool:
    """Read per call, not at import, so a test can flip it."""
    return os.environ.get("SAMJHA_ALLOW_REAL_DATA", "").strip().lower() in ("1", "true", "yes")


def kfs_dir() -> Path:
    return Path(os.environ.get("SAMJHA_KFS_DIR", "data/kfs"))


def sniff(data: bytes) -> tuple[str, str]:
    """(suffix, media_type) from the bytes themselves.

    A zip is only accepted as .docx once `word/document.xml` is actually present — "PK" is
    every zip on earth, including .xlsx, .odt and a plain archive, and each of those would
    otherwise reach the Word reader and fail somewhere less legible.
    """
    if data.startswith(PDF_MAGIC):
        return ".pdf", MEDIA_TYPES[".pdf"]

    if data.startswith(ZIP_MAGIC):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                names = set(z.namelist())
        except zipfile.BadZipFile:
            raise UnsupportedDocument("looks like a zip but is corrupt") from None
        if "word/document.xml" in names:
            return ".docx", MEDIA_TYPES[".docx"]
        raise UnsupportedDocument(
            "a zip without word/document.xml — an .xlsx or .odt is not a Word document"
        )

    raise UnsupportedDocument(
        "unrecognised file. Upload a KFS as PDF or Word (.docx); "
        "a photograph or scan needs a reader this build does not have."
    )


def safe_label(filename: str) -> str:
    """The filename as a DISPLAY label only. Never a path — storage is content-addressed."""
    return _SAFE_NAME.sub("_", Path(filename or "").name)[:120]


def store_bytes(data: bytes) -> tuple[str, Path]:
    """Write to `data/kfs/{sha256}.{ext}`. Returns (sha256, path). Idempotent."""
    digest = hashlib.sha256(data).hexdigest()
    suffix, _ = sniff(data)
    directory = kfs_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{digest}{suffix}"
    if not path.exists():
        path.write_bytes(data)
    return digest, path


def report_to_dict(report: ExtractReport) -> dict:
    return {
        "fields": [
            {"label": f.label, "value": f.value, "status": f.status,
             "document_label": f.document_label, "derived": f.derived, "prompt": f.prompt}
            for f in report.fields
        ],
        "fee_rows": report.fee_rows,
        "fee_table_found": report.fee_table_found,
        "unknown": report.unknown,
        "rate_kind": report.rate_kind,
    }


def values_of(report: dict, corrections: list[dict] | None = None) -> dict[str, str]:
    """Canonical label -> value, with human corrections layered on top."""
    values = {f["label"]: f["value"] for f in report.get("fields", []) if f.get("value")}
    for c in corrections or []:
        if c.get("value"):
            values[c["label"]] = c["value"]
    return values


def missing_of(report: dict, corrections: list[dict] | None = None) -> list[str]:
    """Required labels still absent after corrections.

    Re-resolved rather than trusting the stored statuses, because a correction to
    "Interest rate type" changes WHICH fields are required — a document corrected from
    fixed to floating suddenly needs the five benchmark rows.
    """
    return [f.label for f in kfs_fields.resolve(values_of(report, corrections)).missing()]


def build_kfs(report: dict, corrections: list[dict] | None = None) -> KFS:
    """The one construction path, for cleanly-read and human-corrected documents alike."""
    return build_from_fields(values_of(report, corrections), report.get("fee_rows") or [])


def draft(conn, data: bytes, *, filename: str = "", synthetic: bool = True,
          uploaded_by_role: str = "lender_system") -> dict:
    """Store an uploaded document and read what it says. Never raises on a missing field.

    Raises `UnsupportedDocument` for bytes we cannot read a table grid from,
    `RealDataRefused` for a non-synthetic upload the server is not configured to accept,
    and `ValueError` for an unknown role or an oversized payload.
    """
    from api import store  # noqa: PLC0415  (avoids an import cycle with api.main)

    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError(f"{len(data)} bytes exceeds the {MAX_UPLOAD_BYTES} cap")
    if uploaded_by_role not in UPLOAD_ROLES:
        raise ValueError(f"uploaded_by_role must be one of {UPLOAD_ROLES}, got {uploaded_by_role!r}")
    if not synthetic and not allow_real_data():
        raise RealDataRefused(
            "This server accepts synthetic documents only. Set SAMJHA_ALLOW_REAL_DATA=1 to "
            "accept a real borrower's KFS, and understand that README.md's 'no real "
            "borrower data, ever' no longer describes the deployment."
        )

    suffix, media_type = sniff(data)
    digest, path = store_bytes(data)

    report = extract_fields(path)
    as_dict = report_to_dict(report)

    kfs_json = None
    if report.complete:
        # Validate now, while there is a human present to be told. A document that reads
        # completely but does not conform is a draft too, not a call waiting to happen.
        try:
            kfs_json = build_kfs(as_dict).model_dump_json()
        except Exception as e:  # noqa: BLE001 — pydantic, Decimal, or a bad fee row
            as_dict["validation_error"] = f"{type(e).__name__}: {e}"

    # Content hash plus a short unique tail: the same file uploaded twice by two officers
    # is two intake events with two roles and two audit trails, over one stored object.
    doc_id = f"{digest[:12]}-{uuid.uuid4().hex[:6]}"

    return store.put_document(
        conn, doc_id,
        sha256=digest, filename=safe_label(filename), media_type=media_type,
        byte_size=len(data), method=METHODS[suffix], synthetic=synthetic,
        uploaded_by_role=uploaded_by_role, kfs_json=kfs_json, report=as_dict,
        status="confirmed" if kfs_json else "draft",
    )


def apply_corrections(conn, doc_id: str, supplied: dict[str, str], *, by: str = "") -> dict:
    """Record human-supplied values for fields the document did not yield.

    Every correction is appended with who and when, and the original `report` is left
    untouched: the record must be able to show that a number came from a person rather
    than from the document, and which person.
    """
    from api import store  # noqa: PLC0415

    doc = store.get_document(conn, doc_id)
    if doc is None:
        raise KeyError(doc_id)

    report = doc["report"]
    known = {f.label for f in kfs_fields.ALL}
    corrections = list(doc["corrections"])

    for label, value in supplied.items():
        if label not in known:
            raise ValueError(f"{label!r} is not an Annex-A field")
        if not str(value).strip():
            continue
        corrections = [c for c in corrections if c["label"] != label]
        corrections.append({
            "label": label,
            "value": str(value).strip(),
            "source": "human_supplied",
            "by": by,
            "at": time.time(),
        })

    still_missing = missing_of(report, corrections)
    kfs_json = None
    patch: dict = {"corrections": corrections}
    if not still_missing:
        try:
            kfs_json = build_kfs(report, corrections).model_dump_json()
        except Exception as e:  # noqa: BLE001
            patch["report"] = report | {"validation_error": f"{type(e).__name__}: {e}"}

    patch["kfs"] = kfs_json
    patch["status"] = "confirmed" if kfs_json else "draft"
    return store.update_document(conn, doc_id, **patch)


def provenance(doc: dict | None) -> dict:
    """The block that goes INSIDE the hashed consent record.

    Always the same shape, `method: "unknown"` when there is no document, so a reader can
    tell "we did not record this" from "there was nothing to record" without the key
    appearing and disappearing between records.
    """
    if not doc:
        return {
            "doc_id": "", "filename": "", "sha256": "", "media_type": "",
            "byte_size": 0, "method": "unknown", "uploaded_at": None,
            "uploaded_by_role": "", "synthetic": True, "fields_corrected": [],
        }
    return {
        "doc_id": doc["doc_id"],
        "filename": doc["filename"],
        "sha256": doc["sha256"],
        "media_type": doc["media_type"],
        "byte_size": doc["byte_size"],
        "method": doc["method"],
        "uploaded_at": doc["created_at"],
        "uploaded_by_role": doc["uploaded_by_role"],
        "synthetic": bool(doc["synthetic"]),
        "fields_corrected": [
            {k: c.get(k) for k in ("label", "value", "source", "by", "at")}
            for c in doc.get("corrections") or []
        ],
    }


def kfs_of(doc: dict | None) -> KFS | None:
    if not doc or not doc.get("kfs"):
        return None
    return KFS.model_validate_json(doc["kfs"])


def _demo() -> None:
    """Self-check with no server: a real PDF and a real .docx, plus the refusal paths."""
    import json
    import tempfile

    from api import store

    root = Path(__file__).parent.parent / "fixtures/synthetic"
    pdf = root / "pdf/kfs_01_two_wheeler.pdf"
    docx = root / "docx/kfs_01_two_wheeler.docx"
    if not pdf.exists():
        raise SystemExit("no fixture PDF — run fixtures/synthetic/_to_pdf.py")

    with tempfile.TemporaryDirectory() as td:
        os.environ["SAMJHA_KFS_DIR"] = str(Path(td) / "kfs")
        os.environ.pop("SAMJHA_ALLOW_REAL_DATA", None)
        conn = store.connect(":memory:")

        # ---- sniffing is by content, not by name
        assert sniff(pdf.read_bytes())[0] == ".pdf"
        assert sniff(docx.read_bytes())[0] == ".docx"
        for bad, why in ((b"\xff\xd8\xff\xe0 jpeg", "unrecognised"),
                         (b"", "unrecognised")):
            try:
                sniff(bad)
            except UnsupportedDocument as e:
                assert why in str(e)
            else:
                raise AssertionError(f"{bad!r} must be refused")

        # A zip that is not a Word document is refused by name, not by a parser crash.
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("xl/workbook.xml", "<x/>")
        try:
            sniff(buf.getvalue())
        except UnsupportedDocument as e:
            assert "word/document.xml" in str(e)
        else:
            raise AssertionError("an .xlsx must not be accepted as .docx")

        # ---- a clean PDF confirms straight away, with a KFS attached
        doc = draft(conn, pdf.read_bytes(), filename="../../etc/passwd.pdf",
                    uploaded_by_role="branch_helper")
        assert doc["status"] == "confirmed", doc["report"].get("validation_error")
        assert doc["method"] == "pdf_table" and doc["synthetic"] is True
        # The filename is a label, sanitised, and never a path.
        assert "/" not in doc["filename"] and doc["filename"].endswith("passwd.pdf")
        want = KFS.model_validate(json.loads((root / "kfs_01_two_wheeler.json").read_text("utf-8")))
        assert kfs_of(doc) == want
        # Stored content-addressed, and the bytes re-verify against the recorded hash.
        stored = Path(os.environ["SAMJHA_KFS_DIR"]) / f"{doc['sha256']}.pdf"
        assert hashlib.sha256(stored.read_bytes()).hexdigest() == doc["sha256"]

        # ---- the same document as Word yields the identical KFS
        wdoc = draft(conn, docx.read_bytes(), filename="kfs.docx")
        assert wdoc["method"] == "docx_table"
        assert kfs_of(wdoc) == want, "the .docx path must agree with the .pdf path"
        assert wdoc["doc_id"] != doc["doc_id"] and wdoc["sha256"] != doc["sha256"]

        # ---- a document missing the cooling-off row stays a DRAFT with no KFS...
        report = dict(doc["report"])
        report["fields"] = [
            f | {"value": "", "status": "missing"} if f["label"] == "Cooling-off period (days)" else f
            for f in report["fields"]
        ]
        partial = store.put_document(conn, "partial-000001", sha256="a" * 64,
                                     method="pdf_table", report=report, status="draft")
        assert partial["kfs"] is None
        assert missing_of(report) == ["Cooling-off period (days)"]

        # ...and a human supplying it is recorded as the source, not silently merged.
        fixed = apply_corrections(conn, "partial-000001",
                                  {"Cooling-off period (days)": "3"}, by="clerk@branch")
        assert fixed["status"] == "confirmed"
        assert kfs_of(fixed).cooling_off_period_days == 3
        (c,) = fixed["corrections"]
        assert c["label"] == "Cooling-off period (days)" and c["source"] == "human_supplied"
        assert c["by"] == "clerk@branch"
        assert provenance(fixed)["fields_corrected"][0]["value"] == "3"

        # A blank correction does not satisfy a required field.
        blank = apply_corrections(conn, "partial-000001", {"Cooling-off period (days)": "  "})
        assert blank["status"] == "confirmed"  # already satisfied above, unchanged
        try:
            apply_corrections(conn, "partial-000001", {"Total charges (Rs)": "9999"})
        except ValueError as e:
            assert "Annex-A" in str(e)
        else:
            raise AssertionError("a non-Annex-A label must not be accepted as a correction")

        # ---- real borrower data is refused unless the server opts in
        try:
            draft(conn, pdf.read_bytes(), synthetic=False)
        except RealDataRefused as e:
            assert "SAMJHA_ALLOW_REAL_DATA" in str(e)
        else:
            raise AssertionError("a non-synthetic upload must be refused by default")

        os.environ["SAMJHA_ALLOW_REAL_DATA"] = "1"
        real = draft(conn, pdf.read_bytes(), synthetic=False, uploaded_by_role="borrower")
        assert real["synthetic"] is False
        assert provenance(real)["synthetic"] is False
        del os.environ["SAMJHA_ALLOW_REAL_DATA"]

        # ---- an unknown role is refused; provenance has a stable shape with no document
        try:
            draft(conn, pdf.read_bytes(), uploaded_by_role="whoever")
        except ValueError as e:
            assert "uploaded_by_role" in str(e)
        else:
            raise AssertionError("an unknown role must be refused")

        assert provenance(None)["method"] == "unknown"
        assert set(provenance(None)) == set(provenance(doc))

        print(f"intake OK — pdf and docx agree, provenance kept, "
              f"real data refused by default (doc_id {doc['doc_id']})")


if __name__ == "__main__":
    _demo()
