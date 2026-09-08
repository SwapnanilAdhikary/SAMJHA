"""Document upload, and the provenance that keeps a consent record honest.

The load-bearing tests here are not about HTTP status codes. They are:

  * a document-bound call can never replay the demo fixture (whose clauses describe a
    different loan than the record's own provenance block names);
  * `synthetic_data` inside the hashed payload describes the actual source document, and
    is an uploader assertion rather than a guess;
  * a half-read document cannot become a call;
  * two concurrent borrowers get their own loans.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api import intake, main, records, store

ROOT = Path(__file__).parent.parent / "fixtures/synthetic"
PDFS = sorted((ROOT / "pdf").glob("*.pdf"))
DOCXS = sorted((ROOT / "docx").glob("*.docx"))

pytestmark = pytest.mark.skipif(
    not PDFS, reason="no fixture PDFs — run fixtures/synthetic/_to_pdf.py"
)


@pytest.fixture
def tmpenv(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMJHA_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("SAMJHA_EVENTS_DIR", str(tmp_path / "events"))
    monkeypatch.setenv("SAMJHA_KFS_DIR", str(tmp_path / "kfs"))
    monkeypatch.delenv("SAMJHA_ALLOW_REAL_DATA", raising=False)
    monkeypatch.setattr(main, "_conn", None)
    main._offsets.clear()
    return tmp_path


@pytest.fixture
def client(tmpenv):
    with TestClient(main.app) as c:
        yield c


def upload(client, path: Path, **params):
    q = "&".join(f"{k}={v}" for k, v in params.items())
    return client.post(f"/kfs?{q}" if q else "/kfs", content=path.read_bytes())


class TestUpload:
    def test_a_pdf_becomes_a_confirmed_document_with_clauses(self, client):
        r = upload(client, PDFS[0], filename="kfs.pdf", role="branch_helper")
        assert r.status_code == 200
        d = r.json()
        assert d["status"] == "confirmed" and d["missing"] == []
        assert d["method"] == "pdf_table"
        assert d["uploaded_by_role"] == "branch_helper"
        # The reviewer sees what will be SPOKEN, not just what was parsed.
        assert len(d["clauses"]) >= 10
        assert d["clauses"][0]["title_hi"]
        assert any(kv["kind"] == "account_identifier"
                   for c in d["clauses"] for kv in c["key_values"])

    @pytest.mark.skipif(not DOCXS, reason="no .docx fixtures")
    def test_word_and_pdf_produce_the_same_clauses(self, client):
        stem = DOCXS[0].stem
        a = upload(client, ROOT / "pdf" / f"{stem}.pdf").json()
        b = upload(client, ROOT / "docx" / f"{stem}.docx").json()
        assert a["method"] == "pdf_table" and b["method"] == "docx_table"
        assert [c["text"] for c in a["clauses"]] == [c["text"] for c in b["clauses"]]

    def test_format_is_decided_by_content_not_by_filename(self, client):
        """A .pdf name on JPEG bytes is refused, not handed to a parser."""
        r = client.post("/kfs?filename=totally_a_kfs.pdf", content=b"\xff\xd8\xff\xe0jpeg")
        assert r.status_code == 415

    def test_a_zip_that_is_not_a_word_document_is_refused(self, client):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("xl/workbook.xml", "<x/>")
        r = client.post("/kfs?filename=book.docx", content=buf.getvalue())
        assert r.status_code == 415
        assert "word/document.xml" in r.json()["detail"]

    def test_an_oversized_upload_is_refused(self, client, monkeypatch):
        monkeypatch.setattr(intake, "MAX_UPLOAD_BYTES", 1024)
        r = client.post("/kfs?filename=big.pdf", content=b"%PDF" + b"\x00" * 4096)
        assert r.status_code == 413

    def test_an_unknown_role_is_refused(self, client):
        assert upload(client, PDFS[0], role="whoever").status_code == 400

    def test_the_filename_is_never_used_as_a_path(self, client, tmpenv):
        d = upload(client, PDFS[0], filename="..%2F..%2Fetc%2Fpasswd.pdf").json()
        assert "/" not in d["filename"]
        # Storage is content-addressed, so the stored name is the hash.
        stored = list((tmpenv / "kfs").iterdir())
        assert [p.name for p in stored] == [f"{d['sha256']}.pdf"]

    def test_the_same_file_twice_is_one_stored_object_two_intake_events(self, client, tmpenv):
        a = upload(client, PDFS[0], role="lender_system").json()
        b = upload(client, PDFS[0], role="branch_helper").json()
        assert a["sha256"] == b["sha256"]
        assert a["doc_id"] != b["doc_id"]          # two audit trails
        assert len(list((tmpenv / "kfs").iterdir())) == 1  # one object


class TestRealBorrowerData:
    """README.md promises 'no real borrower data, ever'. Keep that true by default."""

    def test_real_data_is_refused_unless_the_server_opts_in(self, client):
        r = upload(client, PDFS[0], synthetic="false")
        assert r.status_code == 403
        assert "SAMJHA_ALLOW_REAL_DATA" in r.json()["detail"]

    def test_with_the_flag_set_the_record_says_it_is_real(self, client, monkeypatch):
        monkeypatch.setenv("SAMJHA_ALLOW_REAL_DATA", "1")
        d = upload(client, PDFS[0], synthetic="false", role="borrower").json()
        assert d["synthetic"] is False

        call = client.post("/calls", json={"doc_id": d["doc_id"]}).json()
        rec = client.get(f"/calls/{call['id']}/record").json()["record"]
        # The one assertion that matters: a record about a real document must not claim
        # to be synthetic.
        assert rec["synthetic_data"] is False
        assert rec["kfs_provenance"]["uploaded_by_role"] == "borrower"
        assert "REAL BORROWER DATA" in rec["kfs_ref"]

    def test_synthetic_is_never_inferred_from_the_document(self, client):
        """The same bytes are synthetic or not according to what the uploader declared."""
        a = upload(client, PDFS[0]).json()
        assert a["synthetic"] is True
        assert upload(client, PDFS[0], synthetic="false").status_code == 403


class TestProvenanceInTheHashedRecord:
    def test_provenance_names_the_document_and_its_hash(self, client):
        d = upload(client, PDFS[0], filename="kfs_01.pdf", role="branch_helper").json()
        call = client.post("/calls", json={"doc_id": d["doc_id"]}).json()

        sealed = client.get(f"/calls/{call['id']}/record").json()
        p = sealed["record"]["kfs_provenance"]
        assert p["sha256"] == d["sha256"]
        assert p["method"] == "pdf_table"
        assert p["filename"] == "kfs_01.pdf"
        assert p["uploaded_by_role"] == "branch_helper"
        # Inside the hashed payload, so provenance cannot drift from the clauses.
        assert sealed["hash"] == records.record_hash(sealed["record"])

    def test_provenance_has_a_stable_shape_with_no_document(self, client):
        call = client.post("/calls", json={}).json()
        p = client.get(f"/calls/{call['id']}/record").json()["record"]["kfs_provenance"]
        assert p["method"] == "unknown" and p["synthetic"] is True
        assert set(p) == set(intake.provenance(None))

    def test_a_human_correction_is_named_in_the_record(self, client, tmpenv):
        """A number a person typed must not be indistinguishable from one we read."""
        conn = store.connect()
        d = upload(client, PDFS[0]).json()
        report = dict(store.get_document(conn, d["doc_id"])["report"])
        report["fields"] = [
            f | {"value": "", "status": "missing"}
            if f["label"] == "Cooling-off period (days)" else f
            for f in report["fields"]
        ]
        store.put_document(conn, "part-1", sha256="c" * 64, method="pdf_table",
                           report=report, status="draft")

        r = client.patch("/kfs/part-1", json={"values": {"Cooling-off period (days)": "3"},
                                              "by": "clerk@branch"})
        assert r.status_code == 200 and r.json()["status"] == "confirmed"

        call = client.post("/calls", json={"doc_id": "part-1"}).json()
        p = client.get(f"/calls/{call['id']}/record").json()["record"]["kfs_provenance"]
        (c,) = p["fields_corrected"]
        assert c["label"] == "Cooling-off period (days)"
        assert c["value"] == "3" and c["source"] == "human_supplied"
        assert c["by"] == "clerk@branch"

    def test_a_non_annexa_label_cannot_be_corrected_in(self, client):
        d = upload(client, PDFS[0]).json()
        r = client.patch(f"/kfs/{d['doc_id']}", json={"values": {"Total charges (Rs)": "1"}})
        assert r.status_code == 400


class TestADraftCannotBecomeACall:
    def test_a_call_bound_to_a_draft_is_refused(self, client, tmpenv):
        conn = store.connect()
        store.put_document(conn, "draft-1", sha256="d" * 64, method="pdf_table",
                           report={"fields": [], "fee_rows": []}, status="draft")
        r = client.post("/calls", json={"doc_id": "draft-1"})
        assert r.status_code == 409
        assert "draft" in r.json()["detail"]

    def test_a_call_bound_to_an_unknown_document_is_refused(self, client):
        assert client.post("/calls", json={"doc_id": "nope"}).status_code == 404

    def test_the_agents_lookup_refuses_a_call_with_no_document(self, client):
        call = client.post("/calls", json={}).json()
        assert client.get(f"/calls/{call['id']}/kfs").status_code == 404


class TestTheFixtureNeverReplaysOverARealDocument:
    """api/demo.py reads ₹1,25,000 at 18.5% no matter what the document says."""

    def test_a_document_bound_call_may_not_fall_back(self, client, tmpenv):
        conn = store.connect()
        d = upload(client, PDFS[0]).json()
        call = client.post("/calls", json={"doc_id": d["doc_id"]}).json()
        assert call["fallback_allowed"] == 0
        assert store.allow_fallback(conn, call["id"]) is False

    def test_a_bare_call_id_still_falls_back(self, client, tmpenv):
        """The rehearsal path the project deliberately built must keep working."""
        conn = store.connect()
        call = client.post("/calls", json={}).json()
        assert store.allow_fallback(conn, call["id"]) is True

    def test_nothing_can_re_enable_fallback_on_a_bound_call(self, client, tmpenv):
        conn = store.connect()
        d = upload(client, PDFS[0]).json()
        call = client.post("/calls", json={"doc_id": d["doc_id"]}).json()
        # There is deliberately no setter. Adding one should break this.
        assert not hasattr(store, "set_fallback_allowed")
        # Even the mode-rewrite the fallback performs does not unlock it.
        store.set_mode(conn, call["id"], "demo")
        assert store.allow_fallback(conn, call["id"]) is False

    def test_the_websocket_says_so_instead_of_replaying(self, client, tmpenv, monkeypatch):
        monkeypatch.setattr(main, "FALLBACK_AFTER_S", 0.0)
        d = upload(client, PDFS[0]).json()
        call = client.post("/calls", json={"doc_id": d["doc_id"]}).json()

        notes = []
        with client.websocket_connect(f"/calls/{call['id']}/live") as ws:
            assert ws.receive_json()["type"] == "hello"
            for _ in range(6):
                ev = ws.receive_json()
                if ev["type"] == "note":
                    notes.append(ev["text"])
                    break
                # A clause_state here would mean the fixture is replaying.
                assert ev["type"] != "clause_state", "the fixture replayed over a document"

        assert notes and "will NOT be replayed" in notes[0]
        # And the record stays empty rather than describing someone else's loan.
        rec = client.get(f"/calls/{call['id']}/record").json()["record"]
        assert rec["clauses"] == []
        assert rec["consent"]["decision"] == "PENDING"


class TestTwoBorrowersTwoLoans:
    def test_each_call_resolves_its_own_document(self, client):
        """The bug the SAMJHA_KFS env var could not avoid: one process, two loans.

        Different fixtures, so the account numbers must be disjoint. If these ever match,
        someone's consent record contains another borrower's loan.
        """
        a = upload(client, PDFS[0], filename="a.pdf").json()
        b = upload(client, PDFS[1], filename="b.pdf").json()
        ca = client.post("/calls", json={"doc_id": a["doc_id"]}).json()
        cb = client.post("/calls", json={"doc_id": b["doc_id"]}).json()

        ka = client.get(f"/calls/{ca['id']}/kfs").json()
        kb = client.get(f"/calls/{cb['id']}/kfs").json()

        assert ka["kfs"]["loan_account_no"] != kb["kfs"]["loan_account_no"]
        assert ka["sha256"] == a["sha256"] and kb["sha256"] == b["sha256"]
        # Leading zeros survive upload -> store -> JSON -> lookup.
        expected = json.loads((ROOT / f"{PDFS[0].stem}.json").read_text("utf-8"))
        assert ka["kfs"]["loan_account_no"] == expected["loan_account_no"]


class TestThreeSurfaces:
    """Three pages for three different people, and the judge's one must not regress."""

    def test_each_surface_is_served(self, client):
        call = client.post("/calls", json={}).json()
        for path in ("/", "/intake", f"/c/{call['id']}", f"/record/{call['id']}"):
            r = client.get(path)
            assert r.status_code == 200, path
            assert r.headers["content-type"].startswith("text/html")

    def test_the_judge_panel_markers_are_untouched(self, client):
        """tests/test_api.py:172 asserts these literals. The intake link must not disturb
        them, and neither must anything else this change touched."""
        body = client.get("/").text
        for marker in ("LIVE CLAUSE STATE", "CONSENT BLOCKED", "PARTIALLY_HEARD"):
            assert marker in body

    def test_the_borrower_page_has_no_file_or_text_input(self, client):
        """Choosing a file is a literacy-heavy act; she is not the one who does it."""
        call = client.post("/calls", json={}).json()
        body = client.get(f"/c/{call['id']}").text
        assert 'type="file"' not in body
        assert 'type="text"' not in body

    @pytest.mark.parametrize("page", ["/", "/intake", "/c/x"])
    def test_no_surface_loads_anything_off_origin(self, client, page):
        """A demo must not depend on a CDN or a font host being reachable.

        Checked on the ATTRIBUTES rather than on the raw text: every one of these files
        also contains the string "fonts.googleapis.com" inside a comment explaining that
        it deliberately does not use it, and a substring search would match that.
        """
        body = client.get(page).text
        refs = re.findall(r'(?:src|href)\s*=\s*"([^"]*)"', body)
        assert refs, "expected at least one asset reference"
        for ref in refs:
            assert not re.match(r"(?:[a-z]+:)?//", ref), f"{page} loads {ref} off-origin"

    def test_the_borrower_page_handles_the_three_mobile_traps(self, client):
        """Each of these silently produces a call that looks fine and is not.

        Asserted as a presence check, which is weak — but a page whose whole job is to
        work on someone's phone should not lose one of these to a refactor unnoticed.
        """
        call = client.post("/calls", json={}).json()
        body = client.get(f"/c/{call['id']}").text
        assert "isSecureContext" in body       # mic denied silently on plain http
        assert "TrackSubscribed" in body       # otherwise the call is silent
        assert "startAudio" in body            # autoplay still blocked after connect
        assert "unlockAudio" in body           # the gesture must not be spent on an await
        # LiveKit accepts a dispatch with no worker registered and never assigns it, so
        # "the agent is running" and "nobody ran make agent" look identical from the
        # browser: connected, silent, forever. Verified against the live project.
        assert "AGENT_TIMEOUT_MS" in body

    def test_the_intake_page_cannot_create_duplicate_calls(self, client):
        """One click, one call. Every click used to create another one.

        A presence check, which is weak — there is no browser in this suite — but the
        guard is cheap to delete by accident and the symptom (ten orphan call rows and a
        borrower link nobody saw) is expensive to diagnose.
        """
        body = client.get("/intake").text
        assert "if (creating) return" in body
        assert 'id="call-ready"' in body       # the result renders below the button
        assert "scrollIntoView" in body        # ...and is scrolled to

    def test_the_vendored_livekit_client_is_present_and_pinned(self, client):
        r = client.get("/vendor/livekit-client.umd.min.js")
        assert r.status_code == 200, "run `make vendor`"
        assert "LivekitClient" in r.text

    def test_a_missing_prompt_is_a_404_not_a_crash(self, client):
        """The page falls back to the browser's own voice, so a demo without
        `make prompts` still speaks."""
        assert client.get("/prompts/ready.wav").status_code in (200, 404)
        assert client.get("/prompts/nope.wav").status_code == 404

    def test_static_routes_refuse_traversal_and_wrong_types(self, client):
        for path in ("/vendor/../api/main.py", "/vendor/main.py", "/prompts/ready.js",
                     "/vendor/livekit-client.umd.min.wav"):
            assert client.get(path).status_code == 404, path

    def test_health_tells_the_intake_page_what_the_server_allows(self, client):
        h = client.get("/health").json()
        assert h["allow_real_data"] is False
        assert h["agent_name"] == main.AGENT_NAME


class TestToken:
    def test_no_livekit_config_is_a_readable_503(self, client, monkeypatch):
        for var in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"):
            monkeypatch.delenv(var, raising=False)
        call = client.post("/calls", json={}).json()
        r = client.get(f"/calls/{call['id']}/token")
        assert r.status_code == 503
        assert "LIVEKIT_URL" in r.json()["detail"]

    def test_a_token_is_scoped_to_this_one_room(self, client, monkeypatch):
        monkeypatch.setenv("LIVEKIT_URL", "wss://example.livekit.cloud")
        monkeypatch.setenv("LIVEKIT_API_KEY", "APIkey")
        monkeypatch.setenv("LIVEKIT_API_SECRET", "s" * 40)

        call = client.post("/calls", json={}).json()
        r = client.get(f"/calls/{call['id']}/token")
        assert r.status_code == 200
        body = r.json()
        assert body["room"] == call["id"]

        # Decode with the vendor's own verifier rather than trusting our own construction.
        from livekit.api import TokenVerifier

        claims = TokenVerifier("APIkey", "s" * 40).verify(body["token"])
        assert claims.video.room == call["id"]
        assert claims.video.room_join is True
        assert claims.video.can_publish is True      # her microphone, for teach-back
        assert claims.video.can_subscribe is True    # the agent's voice
        assert claims.identity == f"borrower-{call['id']}"

    def test_the_api_secret_never_reaches_the_browser(self, client, monkeypatch):
        secret = "s" * 40
        monkeypatch.setenv("LIVEKIT_URL", "wss://example.livekit.cloud")
        monkeypatch.setenv("LIVEKIT_API_KEY", "APIkey")
        monkeypatch.setenv("LIVEKIT_API_SECRET", secret)
        call = client.post("/calls", json={}).json()
        assert secret not in client.get(f"/calls/{call['id']}/token").text
