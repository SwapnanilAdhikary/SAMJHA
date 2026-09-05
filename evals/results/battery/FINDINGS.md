# Day-1 battery — measured findings

Run 2026-09-05, `modelId=coda`, `speaker=taru`, `/ws3`. Raw protocol transcript in
`frames.json`; clips in this directory.

---

## 1. The `lang`-omitted loophole: opens, then closes

Rime's docs state timestamps are emitted "only when `lang` is `en`/`eng` or `es`/`spa`,
**or when `lang` is omitted**." That last clause was the one documented route to word
timings on a Hindi voice. We tested it.

**It half-works, and the half that fails is the half we needed.**

| Connection | `timestamps` event |
|---|---|
| `lang=hi`, `speaker=taru` | ❌ none (61 frames: 60 chunk + 1 done) |
| `lang` omitted, `speaker=taru` | ✅ **emitted** |

And the payload is genuinely Hindi — correctly tokenized Devanagari, not romanized
fallback:

```json
{"words": ["आपका","ब्याज","दर","चौदह","प्रतिशत","है।"],
 "start": [0, 0.72212, 1.62477, 1.98583, 2.70795, 3.97166],
 "end":   [0.72212, 1.62477, 1.98583, 2.70795, 3.97166, 4.51325]}
```

Three parallel arrays, **not** an array of `{word,start,end}` objects.

### Why it is still unusable: the timestamps are not on the audio's time base

Same text, same connection parameters, only `audioFormat`/`samplingRate` varied:

| Text | Format | Audio duration | `end[-1]` | ratio |
|---|---|---|---|---|
| 1 (30 ch) | mulaw 8000 | 3.13 s | 4.51325 | 1.442 |
| 1 (30 ch) | pcm 22050 | 3.69 s | **4.51325** | 1.223 |
| 2 (49 ch) | mulaw 8000 | 4.73 s | 7.04067 | 1.489 |
| 2 (49 ch) | pcm 22050 | 4.89 s | **7.04067** | 1.440 |

**The timestamp values are byte-identical across sample rates while the delivered audio
duration is not.** They therefore describe some internal reference timeline, not the
stream we receive. The ratio is not constant either (1.223 → 1.489), so no scaling factor
recovers alignment.

Audio was verified complete, not truncated: a clean `done` event arrived after 63 chunks
in every run.

### Consequence

The consent mechanic does **not** use Rime word timestamps, in any language. It uses:

1. **one key value per flush segment** — we own utterance boundaries via `segment=never`
   plus explicit flush, and
2. **byte-count duration** — mu-law at 8 kHz is 1 byte per sample, so 8000 bytes = 1.000 s
   exactly (verified in `evals/channel.py`), cross-checked against LiveKit's measured
   `PlaybackFinishedEvent.playback_position`.

This was chosen from the docs before the test and is now confirmed by measurement. Framing
it as a workaround would be wrong: it is vendor-independent, language-independent, and
exact, where the vendor path is none of those.

---

## 2. Protocol shape, confirmed live

- Audio arrives base64 in JSON `chunk` events (`data` field), not binary frames.
- `contextId` came back `null` on every frame even on a single-context connection —
  further reason not to build fencing on it, consistent with Rime's own statement that it
  "does not maintain multiple simultaneous context IDs."
- Event types observed: `chunk`, `timestamps`, `done`.
- `{"text": ...}` → `{"operation":"flush"}` → `{"operation":"eos"}` is accepted.

---

## 3. Duration signals, pending listening

Durations alone suggest different readings between arms. **Not yet confirmed by ear** —
these are hints for what to listen for, not results.

| Clip | Text | Duration |
|---|---|---|
| `1a_devanagari` | Devanagari input | 2.97 s |
| `1b_romanized` | romanized input | **6.25 s** ← 2.1× longer for equivalent content |
| `3a_raw_grouped` | `₹1,04,596` | 8.57 s |
| `3b_raw_plain` | `104596` | 7.21 s |
| `3c_normalized` | verbalised | 6.89 s |
| `4a_bare_run` | `9157114007` | 7.29 s |
| `4b_grouped_run` | `9,157,114,007` | 7.93 s |
| `6a_pct_raw` | `18.5%` | 4.57 s |

The `1b` result is the notable one: romanized input takes over twice as long as Devanagari
for the same sentence, which is what you would expect if it were being spelled or
mispronounced rather than read.

**Open — requires a native listener:** whether `lang`-omitted audio is still intelligible
Hindi, and whether `3a` is actually mangled. Those decide the headline claim.
