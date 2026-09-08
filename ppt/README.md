# ppt/

**`samjha.pptx`** — 12 slides, 16:9, dark to match the product.

```bash
uv run python ppt/build_ppt.py
```

It is generated from `build_ppt.py` rather than hand-made, so the deck cannot quietly
drift from the code. Every figure on a slide is either produced by a `make` target or
committed under `evals/results/`.

## The slides

| # | Slide |
|---|---|
| 1 | Title |
| 2 | The problem — she acknowledges a document she cannot read |
| 3 | What we built — document in, sealed consent record out; three surfaces |
| 4 | `/intake` — nothing is ever guessed |
| 5 | `/c/{id}` — one tap, everything else spoken |
| 6 | The mechanic — "heard" means the segment carrying the number finished |
| 7 | The artifact — a refusal is a row, not an error |
| 8 | The measured result, **with its n** |
| 9 | Four Rime findings we measured rather than assumed |
| 10 | What we do not claim |
| 11 | Run it |
| 12 | Close |

Slide 8 carries the n=24 caveat on the same slide as the headline numbers, deliberately.
The first question from the floor will be about the n, and it is better answered before it
is asked. Do not delete that block without also editing `README.md`, which says the same
thing.

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
