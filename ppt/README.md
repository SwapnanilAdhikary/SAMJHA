# ppt/

**`samjha.pptx`** — 10 slides, 16:9, dark to match the product.

```bash
uv run python ppt/build_ppt.py
```

It is generated from `build_ppt.py` rather than hand-made, so the deck cannot quietly
drift from the code. Every figure on a slide is either produced by a `make` target or
committed under `evals/results/`. The build also asserts that no shape runs off the slide
— every box here is placed by hand-computed Emu, and the way that breaks is silently.

**The deck is the backdrop for [`../PRESENTATION.md`](../PRESENTATION.md), not the
content.** That file carries the 9-minute timing, the demo beats and the failure playbook.
Change the slide order here and that file is wrong.

## The slides

Inverted pyramid: the most valuable thing a judge could take away is on slide 2, before
the problem statement and before the architecture.

| # | Slide | Why here |
|---|---|---|
| 1 | Title | 10 seconds |
| 2 | **96.6% heard → CONSENT REFUSED** | The single most valuable 30 seconds of the talk |
| 3 | The problem — she acknowledges a document she cannot read | Now that they care |
| 4 | **Architecture**, drawn not imported | The green band is the only unusual part |
| 5 | **DEMO** — near-textless, plus a backup screenshot strip | 3.5 of the 9 minutes |
| 6 | The mechanic — 8000 bytes = 1.000 s | The demo raises the question; this answers it |
| 7 | The measured result, **with its n** | |
| 8 | Four Rime findings we measured rather than assumed | First slide to cut if behind |
| 9 | What we do not claim | |
| 10 | Close + run it | |

Slide 7 carries the n=24 caveat on the same slide as the headline numbers, deliberately.
The first question from the floor will be about the n, and it is better answered before it
is asked. Do not delete that block without also editing `README.md` and `PRESENTATION.md`,
which say the same thing.

Slide 5 is deliberately almost textless — you are demoing, not reading. The screenshot
strip on it is a lifeboat: if the tunnel dies you still have the three surfaces on screen
while you talk.

Slide 4 is **drawn with python-pptx shapes rather than imported as an image**, for the same
reason the deck is generated at all: a PNG in `img/` would go stale the first time the
pipeline changed and nobody would notice until a judge asked about a box that no longer
exists. The same architecture is in `README.md` as mermaid, which GitHub renders.

## Screenshots

`img/*.png` are frames from `../video/samjha_demo.mp4`. Regenerate after a UI change:

```bash
cd ..
for s in "9:intake_clauses" "13:borrower_ready" "40:after_bargein"; do
  t=${s%%:*}; n=${s##*:}
  ffmpeg -y -v error -ss $t -i video/samjha_demo.mp4 -frames:v 1 -vf scale=1280:-1 "ppt/img/$n.png"
done
# the record shot is cropped to the top, where the refusal and the hash are
ffmpeg -y -v error -ss 172 -i video/samjha_demo.mp4 -frames:v 1 \
  -vf "scale=1280:-1,crop=1280:600:0:0" ppt/img/record.png
uv run python ppt/build_ppt.py
```

## Fonts

Body is Helvetica Neue, code is Menlo, Devanagari is Kohinoor Devanagari — all macOS
system fonts, none embedded. On a machine without them PowerPoint will substitute, and the
Devanagari on slides 1 and 4 is the part that will look wrong. Export to PDF before
presenting anywhere that is not this laptop.
