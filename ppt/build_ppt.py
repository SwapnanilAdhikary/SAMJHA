"""Build ppt/samjha.pptx from the numbers this repo can actually defend.

    uv run python ppt/build_ppt.py

A generator rather than a hand-made file, so the deck cannot drift from the code. Every
figure on a slide is either produced by a `make` target or committed under
`evals/results/` — and where a number rests on a small n, the slide says so. A deck that
overstates the evidence is worse than no deck, because the first question from the floor
will be about the n.

Screenshots come from `video/samjha_demo.mp4`; regenerate them with the ffmpeg loop in
`ppt/README.md` if the UI changes.
"""

from __future__ import annotations

from pathlib import Path

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN
from pptx.util import Emu, Inches, Pt

HERE = Path(__file__).resolve().parent
IMG = HERE / "img"
OUT = HERE / "samjha.pptx"

W, H = Inches(13.333), Inches(7.5)          # 16:9

# The product's own palette, so the deck and the screenshots are one thing.
BG = RGBColor(0x0B, 0x0D, 0x12)
INK = RGBColor(0xF2, 0xF5, 0xFB)
DIM = RGBColor(0x98, 0xA1, 0xB6)
GO = RGBColor(0x1D, 0xB9, 0x54)
AMBER = RGBColor(0xFF, 0xB0, 0x20)
RED = RGBColor(0xFF, 0x5C, 0x5C)
LINE = RGBColor(0x24, 0x2A, 0x38)

BODY = "Helvetica Neue"
MONO = "Menlo"
# macOS ships this and it has the glyphs; without it Devanagari renders as boxes.
DEVA = "Kohinoor Devanagari"


def slide(prs, *, bg=BG):
    s = prs.slides.add_slide(prs.slide_layouts[6])       # blank
    fill = s.background.fill
    fill.solid()
    fill.fore_color.rgb = bg
    return s


def text(s, txt, *, x, y, w, h, size=18, color=INK, bold=False, font=BODY,
         align=PP_ALIGN.LEFT, space_after=6, line=1.25):
    box = s.shapes.add_textbox(x, y, w, h)
    tf = box.text_frame
    tf.word_wrap = True
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0

    lines = txt.split("\n") if isinstance(txt, str) else list(txt)
    for i, ln in enumerate(lines):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = align
        p.space_after = Pt(space_after)
        p.line_spacing = line
        run = p.add_run()
        run.text = ln
        run.font.size = Pt(size)
        run.font.bold = bold
        run.font.color.rgb = color
        run.font.name = font
    return box


def bullets(s, items, *, x, y, w, size=16, gap=0.52, colour=INK):
    """One textbox per bullet: simpler to place, and lets each carry its own colour."""
    for i, item in enumerate(items):
        body, col = item if isinstance(item, tuple) else (item, colour)
        text(s, f"·  {body}", x=x, y=y + Inches(gap * i), w=w, h=Inches(gap),
             size=size, color=col)


def rule(s, *, x, y, w, colour=LINE, thick=Pt(1.5)):
    from pptx.enum.shapes import MSO_SHAPE
    bar = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, x, y, w, thick)
    bar.fill.solid()
    bar.fill.fore_color.rgb = colour
    bar.line.fill.background()
    bar.shadow.inherit = False
    return bar


def header(s, kicker, title, *, colour=INK):
    text(s, kicker.upper(), x=Inches(0.8), y=Inches(0.55), w=Inches(11), h=Inches(0.3),
         size=12, color=DIM)
    text(s, title, x=Inches(0.8), y=Inches(0.9), w=Inches(11.7), h=Inches(0.9),
         size=32, bold=True, color=colour)
    rule(s, x=Inches(0.8), y=Inches(1.72), w=Inches(1.6), colour=GO)


def shot(s, name, *, x, y, w):
    """Place a screenshot, scaled to width, and return its height."""
    p = IMG / f"{name}.png"
    if not p.exists():
        text(s, f"[missing {p.name}]", x=x, y=y, w=w, h=Inches(0.4), size=12, color=RED)
        return Inches(0.4)
    pic = s.shapes.add_picture(str(p), x, y, width=w)
    pic.line.color.rgb = LINE
    pic.line.width = Pt(1)
    return Emu(pic.height)


def caption(s, txt, *, x, y, w):
    text(s, txt, x=x, y=y, w=w, h=Inches(0.5), size=12, color=DIM, line=1.2)


def build() -> Path:
    prs = Presentation()
    prs.slide_width, prs.slide_height = W, H

    # ---------------------------------------------------------------- 1 · title
    s = slide(prs)
    text(s, "SAMJHA", x=Inches(0.9), y=Inches(2.1), w=Inches(11), h=Inches(1.3),
         size=76, bold=True)
    text(s, "समझा", x=Inches(0.9), y=Inches(3.35), w=Inches(6), h=Inches(0.8),
         size=40, color=GO, font=DEVA)
    text(s, "Voice-native informed consent for Indian retail lending",
         x=Inches(0.95), y=Inches(4.35), w=Inches(11), h=Inches(0.5), size=22, color=DIM)
    rule(s, x=Inches(0.95), y=Inches(5.1), w=Inches(2.2), colour=GO)
    text(s, "RBI Key Facts Statement, read aloud in Hindi, clause by clause —\n"
            "with proof of what she actually heard.",
         x=Inches(0.95), y=Inches(5.4), w=Inches(11), h=Inches(1), size=17, color=INK)
    text(s, "DataForge × Pathway × Rime", x=Inches(0.95), y=Inches(6.6),
         w=Inches(11), h=Inches(0.4), size=13, color=DIM, font=MONO)

    # ---------------------------------------------------------------- 2 · problem
    s = slide(prs)
    header(s, "the problem", "She acknowledges a document she cannot read.")
    bullets(s, [
        "RBI mandates a Key Facts Statement for every retail loan — in a language the "
        "borrower understands, with informed acknowledgement.",
        "In practice: the lender emails a PDF and collects an OTP.",
        "The loan was sold on a phone call. There is no screen, and she has low reading "
        "fluency.",
    ], x=Inches(0.8), y=Inches(2.2), w=Inches(11.7), size=18, gap=0.95)
    rule(s, x=Inches(0.8), y=Inches(5.1), w=Inches(11.7))
    text(s, "The regulation is satisfied. The borrower is not informed.",
         x=Inches(0.8), y=Inches(5.4), w=Inches(11.7), h=Inches(0.6), size=24,
         bold=True, color=AMBER)
    text(s, "Remove speech and the product does not degrade — it ceases to exist.",
         x=Inches(0.8), y=Inches(6.15), w=Inches(11.7), h=Inches(0.5), size=15, color=DIM)

    # ---------------------------------------------------------------- 3 · what
    s = slide(prs)
    header(s, "what we built", "A document goes in. A defensible consent record comes out.")
    text(s,
         "KFS  (PDF or Word)\n"
         "   →  deterministic table parse, no OCR, no model in the loop\n"
         "   →  Hindi clauses, ONE key value per spoken segment\n"
         "   →  Rime Coda over /ws3, 8 kHz μ-law\n"
         "   →  LiveKit to her phone · Sarvam for her replies\n"
         "   →  consent FSM: heard → teach-back → understood\n"
         "   →  sha256-sealed record",
         x=Inches(0.8), y=Inches(2.15), w=Inches(7.2), h=Inches(3.6), size=16,
         font=MONO, line=1.55)
    text(s, "Three surfaces, three people", x=Inches(8.4), y=Inches(2.15),
         w=Inches(4.2), h=Inches(0.4), size=15, bold=True, color=GO)
    bullets(s, [
        ("/            the judge's evidence panel", DIM),
        ("/intake      the officer's review desk", DIM),
        ("/c/{id}      THE BORROWER — one tap", INK),
    ], x=Inches(8.4), y=Inches(2.7), w=Inches(4.4), size=13)
    text(s, "Choosing a PDF off a filesystem is itself a reading task, so the borrower "
            "does not do it. The record names who did (uploaded_by_role). Her part begins "
            "at “take the call”.",
         x=Inches(8.4), y=Inches(4.5), w=Inches(4.3), h=Inches(2), size=12, color=DIM)

    # ---------------------------------------------------------------- 4 · intake
    s = slide(prs)
    header(s, "the officer's desk  ·  /intake", "Nothing is ever guessed.")
    h1 = shot(s, "intake_clauses", x=Inches(0.8), y=Inches(2.05), w=Inches(7.0))
    caption(s, "The Hindi that will actually be spoken, with each value's spoken form.",
            x=Inches(0.8), y=Inches(2.05) + h1 + Inches(0.12), w=Inches(7.0))
    bullets(s, [
        "A lender's own wording is absorbed — “APR (%)”, “Loan Proposal No.”",
        ("A label we do not recognise is REPORTED, never folded into a real field", AMBER),
        ("A required field the document lacks BLOCKS the call until a human types it",
         AMBER),
    ], x=Inches(8.15), y=Inches(2.2), w=Inches(4.5), size=13, gap=1.0)
    text(s, "Because a missing cooling-off period would otherwise default to 0 — and be "
            "read aloud as “इस लोन में कूलिंग-ऑफ की अवधि नहीं है”, denying a statutory "
            "right the document may well grant.",
         x=Inches(8.15), y=Inches(5.4), w=Inches(4.5), h=Inches(1.6), size=12, color=DIM)

    # ---------------------------------------------------------------- 5 · borrower
    s = slide(prs)
    header(s, "the borrower  ·  /c/{call_id}", "One tap. Everything else is spoken.")
    shot(s, "borrower_ready", x=Inches(0.8), y=Inches(2.1), w=Inches(5.6))
    bullets(s, [
        "No file input. No text input. No navigation.",
        "Digits are large, because a digit is not reading — ₹65,000 is legible to "
        "someone who cannot read a sentence.",
        "Every instruction is spoken in Hindi, by the same Rime voice that is about to "
        "read her loan.",
        "Refusal is amber, never red: if refusal looks like rejection, she learns the "
        "honest path is the punished one.",
    ], x=Inches(6.9), y=Inches(2.2), w=Inches(5.7), size=14, gap=1.0)

    # ---------------------------------------------------------------- 6 · mechanic
    s = slide(prs)
    header(s, "the mechanic", "“Heard” means the segment carrying the number finished.")
    text(s,
         "μ-law at 8 kHz is 1 byte per sample.\n"
         "8000 bytes = 1.000 s, exactly — not an estimate.",
         x=Inches(0.8), y=Inches(2.15), w=Inches(6.4), h=Inches(1), size=17, font=MONO,
         color=GO, line=1.4)
    text(s,
         "One comprehension-critical value per flush segment. So “did she hear the APR?” "
         "reduces to “did segment k finish playing?” — arithmetic over Rime's own byte "
         "counts.\n\n"
         "It needs no vendor word timestamps, which Rime does not emit for Hindi anyway. "
         "It works in any language.",
         x=Inches(0.8), y=Inches(3.35), w=Inches(6.4), h=Inches(2.6), size=15)
    shot(s, "after_bargein", x=Inches(7.6), y=Inches(2.1), w=Inches(5.0))
    caption(s, "Barge-in mid-number: the clause goes PARTIALLY_HEARD and is re-read from "
               "the start — never resumed.",
            x=Inches(7.6), y=Inches(5.35), w=Inches(5.0))

    # ---------------------------------------------------------------- 7 · record
    s = slide(prs)
    header(s, "the artifact", "A refusal is a row, not an error.")
    h1 = shot(s, "record", x=Inches(0.8), y=Inches(2.05), w=Inches(7.6))
    bullets(s, [
        ("96.6% of the clause played — and the account number still counts as NOT heard",
         AMBER),
        "Its segment did not finish, so it was never communicated.",
        ("Consent REFUSED · flagged for human callback", RED),
        "sha256 over a canonical serialisation; provenance of the source document is "
        "inside the hash.",
    ], x=Inches(8.6), y=Inches(2.2), w=Inches(4.1), size=13, gap=1.05)

    # ---------------------------------------------------------------- 8 · result
    s = slide(prs)
    header(s, "the measured result", "Value Error Rate on account identifiers")
    text(s, "100% → 0%", x=Inches(1.0), y=Inches(2.4), w=Inches(5.0), h=Inches(1.2),
         size=58, bold=True, color=GO)
    text(s, "Deepgram nova-3", x=Inches(1.05), y=Inches(3.6), w=Inches(5), h=Inches(0.4),
         size=15, color=DIM)
    text(s, "75% → 0%", x=Inches(7.0), y=Inches(2.4), w=Inches(5.0), h=Inches(1.2),
         size=58, bold=True, color=GO)
    text(s, "Sarvam saaras", x=Inches(7.05), y=Inches(3.6), w=Inches(5), h=Inches(0.4),
         size=15, color=DIM)
    text(s, "8 kHz μ-law channel · modelId, speaker, lang, samplingRate and "
            "timeScaleFactor held constant · both arms are Rime",
         x=Inches(1.0), y=Inches(4.35), w=Inches(11), h=Inches(0.5), size=14, color=DIM)
    rule(s, x=Inches(1.0), y=Inches(5.0), w=Inches(11), colour=AMBER)
    text(s, "Read that with its n.",
         x=Inches(1.0), y=Inches(5.2), w=Inches(11), h=Inches(0.4), size=16, bold=True,
         color=AMBER)
    text(s, "The published run is 24 utterances — 4 per category — not the 120 / 20 "
            "pre-registered in evals/ACCEPTANCE.md. The direction and the mechanism are "
            "what the committed per-row evidence supports; the exact percentages rest on "
            "n=4 and should be expected to move on a full run.\n"
            "Four of six categories show NO effect, and are reported as the negative "
            "result they are.",
         x=Inches(1.0), y=Inches(5.6), w=Inches(11), h=Inches(1.4), size=13, color=DIM)

    # ---------------------------------------------------------------- 9 · rime
    s = slide(prs)
    header(s, "Rime", "Four things we measured rather than assumed.")
    bullets(s, [
        "No word timestamps in Hindi, and it fails SILENTLY. With lang omitted they do "
        "appear — but are byte-identical across sample rates while duration is not. "
        "Unusable. So we use our own flush boundaries.",
        "No cancel primitive. `clear` does not cancel in-flight synthesis and contextId "
        "came back null on every frame. Barge-in closes the socket, with a warm spare.",
        "The Rime LiveKit plugin hardcodes audioFormat=pcm, so 8 kHz μ-law is "
        "unreachable through it. Clause audio goes through our own /ws3 client.",
        ("/ws3 has NO per-flush completion event — only `chunk` and `done`. Pipeline the "
         "flushes and every chunk is attributed to segment 0. You must read each flush's "
         "`done` before sending the next. This one bit us in production.", AMBER),
    ], x=Inches(0.8), y=Inches(2.1), w=Inches(11.9), size=13.5, gap=1.15)
    text(s, "Config: coda / taru / hi · mulaw 8000 · segment=never + explicit flush · "
            "timeScaleFactor 1.0, speedAlpha never sent",
         x=Inches(0.8), y=Inches(6.7), w=Inches(11.9), h=Inches(0.4), size=11,
         color=DIM, font=MONO)

    # ---------------------------------------------------------------- 10 · honesty
    s = slide(prs)
    header(s, "what we do not claim", "Stated before the results, not after.", colour=AMBER)
    bullets(s, [
        "The telephone channel is SIMULATED. No live PSTN leg has been validated.",
        "“Heard” means played out of the speaker, ± the client jitter buffer. We measure "
        "playout, not cognition.",
        "Hindi only, and it cannot currently be otherwise — Coda is the only Rime model "
        "with Hindi at all.",
        "Document upload reads TABLES, not pictures. A photographed KFS is refused, not "
        "guessed at.",
        "The synonym table that absorbs a lender's wording is a hypothesis — there is no "
        "real lender's KFS in this repo to test it against.",
        "No authentication. A call link is a bearer capability.",
        "n = 24 on the published eval; the human listening panel has not been run.",
    ], x=Inches(0.8), y=Inches(2.15), w=Inches(11.9), size=14, gap=0.63, colour=DIM)

    # ---------------------------------------------------------------- 11 · run it
    s = slide(prs)
    header(s, "run it", "421 tests, and none of the first four need a key.")
    text(s,
         "uv sync                # builds .venv from uv.lock\n"
         "cp .env.example .env   # RIME_API_KEY is the only one you truly need\n"
         "\n"
         "make test              # 421 tests\n"
         "make demo-fixtures     # barge-in and rushed consent, offline\n"
         "make channel           # the 8 kHz μ-law chain\n"
         "make secrets           # no credential ever touched git history\n"
         "\n"
         "make serve             # terminal 1 — /, /intake, /c/{id}\n"
         "make agent             # terminal 2 — the consent worker\n"
         "\n"
         "make e2e ARGS=\"--drive\"   # upload → Rime → FSM → assert the sealed record",
         x=Inches(0.8), y=Inches(2.15), w=Inches(11.9), h=Inches(4.2), size=15,
         font=MONO, line=1.45)
    text(s, "Every performance number is backed by a committed artifact under "
            "evals/results/. ffmpeg is the one non-Python prerequisite.",
         x=Inches(0.8), y=Inches(6.45), w=Inches(11.9), h=Inches(0.6), size=13, color=DIM)

    # ---------------------------------------------------------------- 12 · close
    s = slide(prs)
    text(s, "Remove speech and the product\ndoes not degrade — it ceases to exist.",
         x=Inches(1.0), y=Inches(2.5), w=Inches(11.3), h=Inches(2), size=40, bold=True,
         line=1.25)
    rule(s, x=Inches(1.05), y=Inches(4.8), w=Inches(2.2), colour=GO)
    text(s, "video/samjha_demo.mp4   ·   RIME_EVIDENCE.md   ·   evals/ACCEPTANCE.md",
         x=Inches(1.05), y=Inches(5.15), w=Inches(11), h=Inches(0.5), size=15,
         color=DIM, font=MONO)

    prs.save(OUT)
    return OUT


if __name__ == "__main__":
    out = build()
    print(f"wrote {out}  ({out.stat().st_size / 1024:.0f} KB)")
