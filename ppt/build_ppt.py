"""Build ppt/samjha.pptx from the numbers this repo can actually defend.

    uv run python ppt/build_ppt.py

A generator rather than a hand-made file, so the deck cannot drift from the code. Every
figure on a slide is either produced by a `make` target or committed under
`evals/results/` — and where a number rests on a small n, the slide says so. A deck that
overstates the evidence is worse than no deck, because the first question from the floor
will be about the n.

STRUCTURE: inverted pyramid, for a 9-minute slot. The single most valuable thing — a
consent refusal on a clause that was 96.6% heard — is on slide 2, before the problem
statement, before the architecture, before anything. Everything after it is in decreasing
order of what a judge loses by not hearing it. Slide 5 is the live demo; it carries a
backup strip of screenshots so that a dead tunnel costs you the demo, not the argument.

Timings per slide are in PRESENTATION.md, which is the script this deck is the backdrop
for. Change the slide order here and that file is wrong.

Screenshots come from `video/samjha_demo.mp4`; regenerate them with the ffmpeg loop in
`ppt/README.md` if the UI changes.
"""

from __future__ import annotations

from pathlib import Path

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Emu, Inches, Pt

HERE = Path(__file__).resolve().parent
IMG = HERE / "img"
OUT = HERE / "samjha.pptx"

W, H = Inches(13.333), Inches(7.5)          # 16:9

# The product's own palette, so the deck and the screenshots are one thing.
BG = RGBColor(0x0B, 0x0D, 0x12)
PANEL = RGBColor(0x13, 0x17, 0x20)
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


def caption(s, txt, *, x, y, w, size=12, colour=DIM):
    text(s, txt, x=x, y=y, w=w, h=Inches(0.5), size=size, color=colour, line=1.2)


# ------------------------------------------------------------------ diagram parts
#
# The architecture slide is DRAWN rather than imported as an image, for the same reason
# the deck is generated: a picture in ppt/img/ would go stale the first time the pipeline
# changed and nobody would notice until a judge asked about a box that no longer exists.


def box(s, lines, *, x, y, w, h, fill=PANEL, border=LINE, size=12, head_colour=INK,
        body_colour=DIM):
    """A rounded stage box: first line is its name, the rest is what it does."""
    sh = s.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, x, y, w, h)
    sh.fill.solid()
    sh.fill.fore_color.rgb = fill
    sh.line.color.rgb = border
    sh.line.width = Pt(1.25)
    sh.shadow.inherit = False
    sh.adjustments[0] = 0.08

    tf = sh.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = MSO_ANCHOR.MIDDLE
    tf.margin_left = tf.margin_right = Inches(0.1)
    tf.margin_top = tf.margin_bottom = Inches(0.06)
    for i, ln in enumerate(lines):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = PP_ALIGN.CENTER
        p.line_spacing = 1.15
        p.space_after = Pt(2)
        r = p.add_run()
        r.text = ln
        r.font.size = Pt(size + 1 if i == 0 else size - 1.5)
        r.font.bold = i == 0
        r.font.color.rgb = head_colour if i == 0 else body_colour
        r.font.name = BODY if i == 0 else MONO
    return sh


def arrow(s, *, x, y, w, h=Inches(0.26), colour=LINE):
    a = s.shapes.add_shape(MSO_SHAPE.RIGHT_ARROW, x, y, w, h)
    a.fill.solid()
    a.fill.fore_color.rgb = colour
    a.line.fill.background()
    a.shadow.inherit = False
    return a


def _check_bounds(prs) -> None:
    """Fail the build if any shape runs off the slide.

    Every box on these slides is placed by hand-computed Emu, so the way this file breaks
    is silently: a bullet list grows one item, the last line slides past 7.5in, and the
    deck still opens looking almost right. Cheaper to assert than to notice on stage.

    Text boxes are exempt on height only — python-pptx cannot know the rendered height of
    autofit text, so the `h` passed to text() is a hint, not a measurement.
    """
    from pptx.util import Emu
    slack = Emu(int(Inches(0.05)))
    bad = []
    for i, sl in enumerate(prs.slides, 1):
        for sh in sl.shapes:
            if sh.left is None or sh.top is None:
                continue
            if sh.left < -slack or sh.top < -slack:
                bad.append(f"slide {i}: {sh.shape_type} starts off-slide at "
                           f"({sh.left / 914400:.2f}, {sh.top / 914400:.2f})in")
            if sh.left + sh.width > W + slack:
                bad.append(f"slide {i}: {sh.shape_type} runs {(sh.left + sh.width - W) / 914400:.2f}in "
                           f"past the right edge")
            # Height is only trustworthy for shapes and pictures, not autofit text boxes.
            if not sh.has_text_frame or sh.shape_type is not None:
                if sh.top + sh.height > H + slack:
                    bad.append(f"slide {i}: {sh.shape_type} runs "
                               f"{(sh.top + sh.height - H) / 914400:.2f}in past the bottom")
    if bad:
        raise SystemExit("LAYOUT OVERFLOW:\n  " + "\n  ".join(bad))


def build() -> Path:
    prs = Presentation()
    prs.slide_width, prs.slide_height = W, H

    # ================================================================ 1 · title
    s = slide(prs)
    text(s, "SAMJHA", x=Inches(0.9), y=Inches(2.2), w=Inches(11), h=Inches(1.3),
         size=76, bold=True)
    text(s, "समझा", x=Inches(0.9), y=Inches(3.45), w=Inches(6), h=Inches(0.8),
         size=40, color=GO, font=DEVA)
    rule(s, x=Inches(0.95), y=Inches(4.55), w=Inches(2.2), colour=GO)
    text(s, "She cannot read the loan document.\nSo we prove what she heard.",
         x=Inches(0.95), y=Inches(4.9), w=Inches(11), h=Inches(1.4), size=28, line=1.3)
    text(s, "DataForge × Pathway × Rime", x=Inches(0.95), y=Inches(6.65),
         w=Inches(11), h=Inches(0.4), size=13, color=DIM, font=MONO)

    # ================================================================ 2 · the moment
    # Inverted pyramid: the most valuable 15 seconds of the whole talk, first.
    s = slide(prs)
    header(s, "the thing to remember", "She heard 96.6% of the clause.", colour=INK)
    text(s, "96.6%", x=Inches(0.8), y=Inches(2.25), w=Inches(4.2), h=Inches(1.5),
         size=80, bold=True, color=AMBER)
    caption(s, "of the clause played before she interrupted", x=Inches(0.85),
            y=Inches(3.65), w=Inches(4.2), size=14)
    text(s, "CONSENT REFUSED", x=Inches(0.8), y=Inches(4.4), w=Inches(5.4), h=Inches(0.8),
         size=38, bold=True, color=RED)
    text(s, "The account number lived in the last segment.\n"
            "That segment never finished.\n"
            "She said yes. The system said no.",
         x=Inches(0.8), y=Inches(5.3), w=Inches(5.6), h=Inches(1.6), size=17, line=1.45)
    shot(s, "record", x=Inches(6.9), y=Inches(2.25), w=Inches(5.7))
    caption(s, "The sealed record. A refusal is a row, not an error.",
            x=Inches(6.9), y=Inches(6.55), w=Inches(5.7))

    # ================================================================ 3 · problem
    s = slide(prs)
    header(s, "why that matters", "She acknowledges a document she cannot read.")
    bullets(s, [
        "RBI: every retail loan needs a Key Facts Statement she understands.",
        "In practice: the lender emails a PDF and collects an OTP.",
        "The loan was sold on a phone call. There is no screen.",
    ], x=Inches(0.8), y=Inches(2.4), w=Inches(11.7), size=22, gap=0.95)
    rule(s, x=Inches(0.8), y=Inches(5.25), w=Inches(11.7))
    text(s, "The regulation is satisfied. The borrower is not informed.",
         x=Inches(0.8), y=Inches(5.55), w=Inches(11.7), h=Inches(0.6), size=26,
         bold=True, color=AMBER)
    text(s, "Remove speech and the product does not degrade — it ceases to exist.",
         x=Inches(0.8), y=Inches(6.4), w=Inches(11.7), h=Inches(0.5), size=15, color=DIM)

    # ================================================================ 4 · architecture
    s = slide(prs)
    header(s, "architecture", "Document in. Sealed consent record out.")

    stages = [
        (["KFS", "PDF or .docx"], LINE),
        (["PARSE", "table grid", "no OCR, no model"], LINE),
        (["SPEAK", "Rime Coda /ws3", "mulaw 8 kHz", "1 value per segment"], GO),
        (["CALL", "LiveKit", "/c/{id} · one tap", "Sarvam hears her"], LINE),
        (["RECORD", "sha256-sealed", "+ doc provenance"], AMBER),
    ]
    bx, by, bh = Inches(0.8), Inches(2.1), Inches(1.5)
    aw = Inches(0.4)
    bw = Emu(int((Inches(11.733) - aw * (len(stages) - 1)) / len(stages)))
    for i, (lines, edge) in enumerate(stages):
        x = Emu(int(bx + i * (bw + aw)))
        box(s, lines, x=x, y=by, w=bw, h=bh, border=edge)
        if i < len(stages) - 1:
            arrow(s, x=Emu(int(x + bw + Inches(0.06))),
                  y=Emu(int(by + bh / 2 - Inches(0.13))), w=Emu(int(aw - Inches(0.12))))

    # The band below is the only part a competent team would not have built the same way.
    band = s.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(0.8), Inches(4.0),
                              Inches(11.733), Inches(2.25))
    band.fill.solid()
    band.fill.fore_color.rgb = PANEL
    band.line.color.rgb = GO
    band.line.width = Pt(1.5)
    band.shadow.inherit = False
    band.adjustments[0] = 0.06
    band.text_frame.text = ""

    text(s, "THE ONE EDGE THAT IS THE PRODUCT", x=Inches(1.05), y=Inches(4.2),
         w=Inches(11), h=Inches(0.3), size=11, color=GO)

    inner = [
        (["Rime's bytes", "8000 B = 1.000 s", "exact, not estimated"], GO),
        (["HEARD?", "did THIS number's", "segment finish?"], AMBER),
        (["UNDERSTOOD?", "she says it back", "in her own words"], AMBER),
        (["REFUSED / RECORDED", "both true, every value", "or consent is blocked"], RED),
    ]
    iy, ih = Inches(4.65), Inches(1.35)
    iaw = Inches(0.36)
    ibw = Emu(int((Inches(11.2) - iaw * (len(inner) - 1)) / len(inner)))
    for i, (lines, edge) in enumerate(inner):
        x = Emu(int(Inches(1.05) + i * (ibw + iaw)))
        box(s, lines, x=x, y=iy, w=ibw, h=ih, fill=BG, border=edge, size=11)
        if i < len(inner) - 1:
            arrow(s, x=Emu(int(x + ibw + Inches(0.05))),
                  y=Emu(int(iy + ih / 2 - Inches(0.12))), w=Emu(int(iaw - Inches(0.1))),
                  h=Inches(0.24), colour=GO)

    caption(s, "No vendor word timestamps — Rime emits none for Hindi. No model in the "
               "loop. Arithmetic over Rime's own output, cross-checked against LiveKit's "
               "measured playout.",
            x=Inches(0.8), y=Inches(6.45), w=Inches(11.733), size=13)

    # ================================================================ 5 · demo
    # Near-textless on purpose: you are talking, and the product is on the other screen.
    # The strip is the lifeboat — if the tunnel dies, you still have the three surfaces.
    s = slide(prs)
    text(s, "DEMO", x=Inches(0.9), y=Inches(0.75), w=Inches(11), h=Inches(1.4),
         size=88, bold=True, color=GO)
    text(s, "Upload a real KFS  ·  take the call in Hindi  ·  interrupt the account "
            "number  ·  read the refusal",
         x=Inches(0.95), y=Inches(2.2), w=Inches(11.7), h=Inches(0.5), size=19, color=DIM)

    strip = [("intake_clauses", "/ — the officer"),
             ("borrower_ready", "/c/{id} — the borrower"),
             ("after_bargein", "/panel — the evidence panel")]
    sx, sw = Inches(0.9), Inches(3.75)
    for i, (name, cap) in enumerate(strip):
        x = Emu(int(sx + i * (sw + Inches(0.32))))
        h1 = shot(s, name, x=x, y=Inches(3.1), w=sw)
        caption(s, cap, x=x, y=Emu(int(Inches(3.1) + h1 + Inches(0.12))), w=sw)

    # ================================================================ 6 · mechanic
    s = slide(prs)
    header(s, "the mechanic", "“Heard” is arithmetic, not a claim.")
    text(s, "8000 bytes = 1.000 s", x=Inches(0.8), y=Inches(2.25), w=Inches(6.4),
         h=Inches(0.8), size=36, bold=True, font=MONO, color=GO)
    caption(s, "μ-law at 8 kHz is one byte per sample. Exactly.",
            x=Inches(0.85), y=Inches(3.1), w=Inches(6.4), size=15)
    bullets(s, [
        "One key value per flush segment.",
        "“Did she hear the APR?” → “did segment k finish?”",
        "Needs no vendor timestamps. Works in any language.",
        ("Barge-in mid-number → the clause is re-read from the start, never resumed.",
         AMBER),
    ], x=Inches(0.8), y=Inches(3.85), w=Inches(6.4), size=15, gap=0.72)
    shot(s, "after_bargein", x=Inches(7.6), y=Inches(2.25), w=Inches(5.0))
    caption(s, "PARTIALLY_HEARD. The value is struck through, and consent is blocked "
               "on this clause.",
            x=Inches(7.6), y=Inches(5.5), w=Inches(5.0))

    # ================================================================ 7 · result
    s = slide(prs)
    header(s, "the measured result", "Value Error Rate on account identifiers")
    text(s, "100% → 0%", x=Inches(1.0), y=Inches(2.35), w=Inches(5.0), h=Inches(1.2),
         size=62, bold=True, color=GO)
    caption(s, "Deepgram nova-3", x=Inches(1.05), y=Inches(3.6), w=Inches(5), size=15)
    text(s, "75% → 0%", x=Inches(7.0), y=Inches(2.35), w=Inches(5.0), h=Inches(1.2),
         size=62, bold=True, color=GO)
    caption(s, "Sarvam saaras", x=Inches(7.05), y=Inches(3.6), w=Inches(5), size=15)
    caption(s, "8 kHz μ-law · model, speaker, lang, sampling rate and timeScaleFactor "
               "held constant · both arms are Rime",
            x=Inches(1.0), y=Inches(4.25), w=Inches(11), size=14)
    rule(s, x=Inches(1.0), y=Inches(4.95), w=Inches(11), colour=AMBER)
    text(s, "Read that with its n.", x=Inches(1.0), y=Inches(5.2), w=Inches(11),
         h=Inches(0.4), size=18, bold=True, color=AMBER)
    bullets(s, [
        ("n = 24 utterances, 4 per category — not the 120 pre-registered.", DIM),
        ("Four of six categories show NO effect. We report them.", DIM),
        ("Claim written and committed before any product code existed.", DIM),
    ], x=Inches(1.0), y=Inches(5.75), w=Inches(11), size=14, gap=0.45)

    # ================================================================ 8 · rime
    s = slide(prs)
    header(s, "Rime", "Four things we measured rather than assumed.")
    bullets(s, [
        "No Hindi word timestamps — and it fails silently, not loudly.",
        "No cancel primitive. Barge-in closes the socket; a warm spare hides the reconnect.",
        "The LiveKit plugin hardcodes pcm, so 8 kHz μ-law is unreachable through it.",
        ("/ws3 has no per-flush event. Pipeline the flushes and every chunk is "
         "attributed to segment 0. This one bit us in production.", AMBER),
    ], x=Inches(0.8), y=Inches(2.4), w=Inches(11.9), size=19, gap=1.05)
    rule(s, x=Inches(0.8), y=Inches(6.4), w=Inches(11.9))
    caption(s, "coda / taru / hi · mulaw 8000 · segment=never + explicit flush · "
               "timeScaleFactor 1.0, speedAlpha never sent   —   RIME_EVIDENCE.md",
            x=Inches(0.8), y=Inches(6.65), w=Inches(11.9), size=12)

    # ================================================================ 9 · honesty
    s = slide(prs)
    header(s, "what we do not claim", "Stated before the results, not after.", colour=AMBER)
    bullets(s, [
        "The telephone channel is SIMULATED. No live PSTN leg.",
        "“Heard” means played out of the speaker. We measure playout, not cognition.",
        "Hindi only — Coda is the only Rime model with Hindi at all.",
        "Tables, not pictures. A photographed KFS is refused, not guessed at.",
        "No authentication. A call link is a bearer capability.",
        "The human listening panel has not been run.",
    ], x=Inches(0.8), y=Inches(2.4), w=Inches(11.9), size=19, gap=0.78, colour=DIM)

    # ================================================================ 10 · close
    s = slide(prs)
    text(s, "Remove speech and the product\ndoes not degrade — it ceases to exist.",
         x=Inches(1.0), y=Inches(1.9), w=Inches(11.3), h=Inches(2), size=40, bold=True,
         line=1.25)
    rule(s, x=Inches(1.05), y=Inches(4.15), w=Inches(2.2), colour=GO)
    text(s,
         "uv sync  &&  make test        # 421 tests, no key needed\n"
         "make stage                    # GO / NO-GO before a live call\n"
         "make e2e ARGS=\"--drive\"       # upload → Rime → FSM → assert the record",
         x=Inches(1.05), y=Inches(4.5), w=Inches(11.3), h=Inches(1.5), size=16,
         font=MONO, line=1.5)
    caption(s, "README.md (architecture + executive summary) · RIME_EVIDENCE.md · "
               "evals/ACCEPTANCE.md · video/samjha_demo.mp4",
            x=Inches(1.05), y=Inches(6.45), w=Inches(11.3), size=14)

    _check_bounds(prs)
    prs.save(OUT)
    return OUT


if __name__ == "__main__":
    out = build()
    print(f"wrote {out}  ({out.stat().st_size / 1024:.0f} KB)  "
          f"{len(Presentation(out).slides)} slides")
