# PRESENTATION — 9 minutes, then Q&A

What you **say**. [`DEMO.md`](DEMO.md) is what you **do** — commands, tabs, the clicking,
your Hindi lines and the failure playbook.

`ppt/samjha.pptx` is the backdrop, not the content; if the deck and this file disagree,
this file is what you say and the deck is wrong.

Structure is inverted pyramid, twice over: the talk leads with the refusal, and the demo
leads with the barge-in. A judge who walks out at minute two should still have the point.

---

## The 20-second version

> A borrower who cannot read acknowledges her loan document with an OTP. We read it to her
> in Hindi, clause by clause, and we track **which numbers actually finished playing**
> before she interrupted. She heard 96.6% of the clause carrying her account number. The
> number was in the last 3.4%. She said yes. **The system refused**, and wrote the refusal
> into a sha256-sealed record.

Say that if you lose the room, lose the wifi, or get cut off.

---

## Timing

| | Slide | Beat | Ends at |
|---|---|---|---|
| 0:00 | 1–2 | Hook: 96.6% heard, consent refused | 0:40 |
| 0:40 | 3 | Why that matters | 1:20 |
| 1:20 | 4 | Architecture | 2:05 |
| 2:05 | 5 | **DEMO** — money shot at ~2:45, not ~3:20 | 5:35 |
| 5:35 | 6 | The mechanic: 8000 bytes = 1.000 s | 6:15 |
| 6:15 | 7 | The measured result, with its n | 7:00 |
| 7:00 | 8 | Four Rime findings | 7:45 |
| 7:45 | 9 | What we do not claim | 8:15 |
| 8:15 | 10 | Close | 8:45 |

15 seconds of slack. You will use it. If you are behind at 5:35, **cut slide 8 to one
sentence** — it is the only slide whose content is fully recoverable from the repo.

---

## Setup

### T−20 min, in the green room

```bash
make serve          # terminal 1
make agent          # terminal 2 — wait for it to settle before you test
make stage          # terminal 3 — must print GO
```

**Run the whole demo on this laptop, at `http://localhost:8000`. Do not use a tunnel.**
`getUserMedia` needs a secure context, and `http://localhost` *is* one — browsers exempt
it by spec, and `web/call.html` gates on `window.isSecureContext`, nothing stricter. A
tunnel is only needed to open the borrower page on a *separate phone*, which is a second
device, a second network path and a second thing to fail in front of judges. The projector
shows your laptop either way.

Everything must say GO, and section 0 must say `SAMJHA_SKIP_INTRO  ON`. The `tunnel`
warning is expected and correct — you are not using one.

Then **run one complete call, start to finish, and do not close that tab** — you will show
its record on stage at 4:55. Barge in on clause 1, answer the rest, say हाँ at the end, and
confirm you get `CONSENT REFUSED`. Write its call_id here:

    green-room call_id: ____________________

That call exists because a full ten-clause read is **about four minutes of audio** and you
have nine minutes total. You will say so out loud. It is a real call, not a fixture.

Its identity clause will read somewhere around **75–93% heard**, not the 96.6% on slide 2
— slide 2 is a screenshot of an earlier record, and in fast mode the clause is ~9.2s rather
than 20.9s, so the fraction lands differently. Nobody will notice, and if they do, the
answer is that they are two different calls. Do not try to hit 96.6% by hand.

### T−5 min, on the podium

Four tabs, in this order, already open:

| Tab | URL | State it should be in |
|---|---|---|
| 1 | `/` | `demo/demo_kfs.pdf` already uploaded and parsed. **Do not click Create the call yet.** |
| 2 | `/c/{id}` | Opened after you click Create. Green button untouched. |
| 3 | `/panel` | The evidence panel, live |
| 4 | `/record/{green-room-id}` | The complete refusal from the green room |

Plus: `demo/evidence_ab.wav` in a player, ready, volume tested.

**And the borrower's lines, on a second screen or printed:**

```bash
make answers            # what YOU say back, for demo/demo_kfs.pdf
```

Every line it prints is checked against the real teach-back grader before it is shown, so
a line on that card is one the grader has already accepted. It reads the document you pass
it, so if you demo a different KFS, re-run it — the numbers you have to say back come out
of that document and nowhere else.

**`make agent` is the one people forget.** Without it the borrower waits on a screen that
never changes. `make stage` catches it.

### Fast mode — read this before you rehearse

`SAMJHA_SKIP_INTRO=1` drops the two segments that open the identity clause: the greeting,
and *"I will read one thing at a time, you may stop me any time."* On `demo/demo_kfs.pdf`
those are **12.6 seconds before the account number even starts.**

| | without the flag | with it |
|---|---|---|
| Account number starts at | 12.6s | **0.0s** |
| Identity clause runs | 20.9s | **9.0–9.5s** |
| Whole call, 10 clauses | 159.3s | **145.7s** |
| First teach-back question | ~28s | **~15s** |
| Your barge-in window | 13s–20s | **7s–9s into the audio** |

**These are measured, not estimated** — `make e2e ARGS="--doc demo/demo_kfs.pdf --drive"`
on 2026-09-19, real Rime Coda over `/ws3`. All checks passed, refusal included.

Note the clause came back **8.97s, 9.29s and 9.45s on three consecutive synthesis calls**.
Rime is not byte-identical run to run, so do not count seconds — **go by ear, and cut as
the last digits are being spoken.**

### The silence after the tap — know about this or you will panic

There is **about five seconds of nothing** between tapping the green button and the first
Hindi word. Measured: 1.8s to open the Rime socket and its warm spare, 2.7s for the first
segment to synthesize, plus the LiveKit join. It is not broken and it is not your wifi.

Fill it, do not apologise for it:

> "It is opening a websocket to Rime and synthesizing her first sentence at eight
> kilohertz. That pause is the only one in the call — every later clause is synthesized
> while the previous one is still playing."

Then the audio starts, and you have ~9 seconds before you must interrupt.

It drops **no** Annex-A field and **no** key value — only the sentence inviting her to
interrupt. Say that out loud if anyone asks why the call opens so abruptly. The agent
writes a `note` into the call's event log whenever it is on, and it shows in the panel
ticker, so the record does not quietly differ from the product.

**Stay on `demo/demo_kfs.pdf`.** A shorter account number is faster but tightens the
barge-in window: `kfs_03_consumer_durable.pdf` gets you to the first question in ~9s, but
leaves you a **2.4-second** window instead of 3. Two seconds is not enough under stage
pressure, and demo_kfs is also the document behind slide 2's screenshot.

**Unset it afterwards.** `make stage` warns while it is on.

---

## The script

### 0:00 — Slide 1, then straight to Slide 2

Do not introduce yourself first. Open on the number.

> **[Slide 2]** This is a consent refusal. The borrower heard ninety-six point six percent
> of the clause. Her loan account number was in the part she did not hear — she interrupted
> three tenths of a second before that segment finished. Then she was asked if she agreed,
> and she said yes.
>
> The system refused her consent, flagged a human callback, and sealed the refusal into the
> record with the same durability it would have given a grant.
>
> That refusal is the product. Everything else is how we earn the right to make it.

*Beat. Then slide 3.*

### 0:40 — Slide 3, the problem

> RBI requires a Key Facts Statement for every retail loan, in a language the borrower
> understands, with informed acknowledgement. In practice the lender emails a PDF and
> collects an OTP.
>
> The loan was sold to her on a phone call. There is no screen. She has low reading
> fluency. She acknowledges a document she cannot read — and the regulation is satisfied.
>
> Remove speech from this and the product does not get worse. It stops existing.

### 1:20 — Slide 4, architecture

Point, do not read.

> A document goes in — PDF or Word, parsed deterministically off the table grid. No OCR, no
> vision model, nothing guessed. A required field the document does not yield **blocks the
> call** until a human types it, because a missing cooling-off period defaulting to zero
> makes the agent tell her she has no cooling-off right at all.
>
> Clauses become Hindi, with exactly **one comprehension-critical value per spoken
> segment**. Rime Coda speaks them over an eight-kilohertz mu-law telephone channel.
>
> **[point at the green band]** This is the only part of the diagram a good team would not
> have built the same way. Mu-law at eight kilohertz is one byte per sample, so a segment's
> byte count *is* its duration. "Did she hear the APR" becomes "did segment four finish" —
> arithmetic over Rime's own output, cross-checked against LiveKit's measured playout.
> No vendor timestamps, which Rime does not emit for Hindi anyway. No model in that path.

### 2:05 — Slide 5, DEMO

**Stop talking about the slide. Go to tab 1.**

**D+0:00 · / — intake (30s).** Scroll to the parsed fields.

> This document is already read. Every Annex-A field, with what it parsed from. A label we
> do not recognise is reported, never folded into a real field. And this — **[scroll to
> section 3]** — is the Hindi that will actually be spoken, with each value's spoken form.
> Nothing here is generated at call time.

Click **Create the call** → **Open the borrower page**.

**D+0:30 · /c/{id} — tab 2 (75s).**

> This is her page. One green button. The loan amount in forty-four-point digits, because a
> digit is not reading. Everything else is spoken.

**Tap it.** Rime starts reading her account number in Hindi, immediately.

> That is Rime Coda, `taru`, Hindi, over the telephone channel — reading zero, zero, zero,
> two, four, eight… digit by digit. Sent to Rime raw, that same number comes back as an
> Indian-scale *quantity*, and the three leading zeros disappear entirely.

**INTERRUPT as it reaches the last two or three digits.**
*(~9.2s of audio. Cut between 7s and 9s — by ear, not by counting; the clause measured
8.97 / 9.29 / 9.45s on three runs. Cutting in the first second gives you 0% heard, which
is the boring case. `make e2e` cuts at 8.8s and records 92.7% heard.)*

**Cut to tab 3, the panel.**

> There. `PARTIALLY_HEARD`. The account number is struck through — **not heard**. Consent
> is now blocked on this clause, and nothing she says can unblock it.
>
> And watch what it does next: **it starts the clause over.** It never resumes. Half a
> disclosure is not a disclosure.

**D+1:45 · the live record (40s).** Open `/record/{live-id}`.

> Consent permitted: **no** — blocked by `identity`. That is the live call, thirty seconds
> old.

**D+2:25 · the green-room record — tab 4 (30s).**

> To show you the finished artifact I have to be honest about time: a full ten-clause read
> is about four minutes of audio, and I have nine minutes. **This is a real call I ran
> twenty minutes ago** — same code, same document, no fixture.
>
> `CONSENT REFUSED`. Flagged for human callback. The sha256 over a canonical
> serialisation — and the hash of the source document is *inside* that hash, so the record
> cannot be separated from the paper it describes.
>
> A refusal is a row here. Not an error, not a dropped call. A result.

**D+2:55 · evidence_ab.wav (25s).** Full screen, nothing else on it.

> Sixteen seconds. One account number, twice. First sent to Rime unprocessed. Then through
> our delivery layer. Same model, same speaker, same channel.

**Play it. Say nothing over it.**

> Unprocessed, Coda reads that as an Indian-scale quantity — nine *billion*. A borrower
> confirming a nine-billion-rupee number has not been informed of anything.

### 5:35 — Slide 6, the mechanic

> One key value per flush segment. Eight thousand bytes is one second, exactly — not an
> estimate, not a model's guess. It needs no vendor timestamps and it works in any
> language, which matters because Hindi is where the vendor timestamps do not exist.

### 6:15 — Slide 7, the result — **do not skip the n**

> Value Error Rate on account identifiers: a hundred percent to zero on Deepgram,
> seventy-five to zero on Sarvam. Model, speaker, language, sampling rate and time scale
> all held constant. Both arms are Rime — this is not a TTS benchmark.
>
> **Read that with its n.** This run is twenty-four utterances, four per category. Our
> pre-registered test says a hundred and twenty. The direction and the mechanism are what
> the committed per-row evidence supports; the exact percentages will move on a full run.
>
> And four of the six categories show **no effect at all**. Rime handles Indian digit
> grouping, percentages and tenures correctly on its own — Rime's own documentation says
> most applications should not pre-normalise, and for four categories out of six it was
> right. We report that, because a result you only publish when it flatters you is not a
> result.
>
> The claim and the acceptance criteria were committed before any product code existed.
> The git timestamp is the proof.

### 7:00 — Slide 8, Rime

Four one-liners. Do not elaborate unless asked.

> Four things we measured rather than assumed. Hindi has no word timestamps and fails
> silently, not loudly. There is no cancel primitive, so barge-in closes the socket and a
> warm spare hides the reconnect from her. The LiveKit plugin hardcodes PCM, so the
> telephone channel is unreachable through it — clause audio goes through our own client.
>
> And the one that bit us in production: `/ws3` has no per-flush completion event. Pipeline
> your flushes and every chunk gets attributed to segment zero — which, in a product whose
> whole claim is per-segment attribution, silently faked the central result. Read each
> flush's `done` before you send the next. That is in `RIME_EVIDENCE.md` with the frame
> counts.

### 7:45 — Slide 9, what we do not claim

Read it fast and flatly. Speed here reads as confidence.

> The telephone channel is simulated — no live PSTN leg. "Heard" means played out of the
> speaker; we measure playout, not cognition. Hindi only, because Coda is the only Rime
> model with Hindi at all. Tables, not pictures. No authentication. The human listening
> panel has not been run — until it has, every comprehension claim rests on an ASR proxy.
>
> All of that was written down before the results, not after.

### 8:15 — Slide 10, close

> Four hundred and twenty-one tests, and the first four commands need no API key at all.
> Every number on these slides is backed by a committed artifact.
>
> Remove speech and this product does not degrade. It ceases to exist.
>
> Thank you.

---

## When it breaks

Decide fast and keep talking. The audience forgives a failure; it does not forgive two
minutes of silent clicking.

| What died | What you do | What you say |
|---|---|---|
| Tunnel / wifi | Tab 3, click **▶ DEMO MODE** | "The network is gone, so this is a scripted call — but through the same ingest path, the same state machine, and the same hashing code. The record will say `mode: demo`, because it would be dishonest if it didn't." |
| Rime 502 mid-call | Keep going; it recovers | "That is the warm-spare path — a 502 used to kill the call three clauses in. It doesn't any more." |
| Mic denied on the borrower page | Click the mic button once more, then stop fighting it | Skip to tab 4, the green-room record, and play `evidence_ab.wav` |
| Page says "this page is not secure" | You opened it on a tunnel/LAN IP, not localhost | Re-open at `http://localhost:8000/c/{id}` |
| `make agent` not running | Nothing will happen at all | Fall back to DEMO MODE. Do not debug on stage. |
| The call opens with a long greeting | `SAMJHA_SKIP_INTRO` did not reach the agent | Let it run — barge in at ~16s instead of ~6s. Everything else is identical. |
| Everything is gone | `video/samjha_demo.mp4`, from 0:28 | "This is a recording of the same flow. The barge-in is at thirty seconds." |
| You are at 6:00 and still demoing | Stop the demo mid-sentence | Jump to slide 7. The n caveat is the one thing you cannot skip. |

---

## Q&A — 2 to 4 minutes

Answer in one sentence, then stop. Offer the file only if they want more.

**"Isn't n=24 too small to claim anything?"**
> Yes, for the exact percentages. No, for the direction on account identifiers, where the
> baseline failed five out of five with a named mechanism — Coda reads digit strings as
> Indian-scale quantities, and leading zeros vanish. The per-row data is committed; the
> full 120 is one command and forty minutes of Sarvam credit.

**"How do you know she understood, rather than just heard?"**
> We don't, and we say so on the slide. "Heard" is playout position, measured. "Understood"
> is teach-back — she says the value back in her own words and it is graded against the
> parsed number. Both are proxies for comprehension, and the human listening panel that
> would test the proxy has not been run yet.

**"What if she just says yes to everything?"**
> Then she is refused. A clause that is not UNDERSTOOD blocks consent as an FSM rule, and
> nothing she says can override it. There are three independent refusal grounds and
> `agent/rushed_consent.py` runs them in order.

**"Why not use Rime's word timestamps?"**
> They do not exist for Hindi, and they fail silently — no `timestamps` event, no error.
> With `lang` omitted they do appear, but the values are byte-identical across sample rates
> while the audio duration is not, so they do not describe the stream you received. We use
> our own flush boundaries instead, which is vendor-independent and exact.

**"Is this a TTS benchmark? How does Rime compare to ElevenLabs?"**
> It isn't and we don't. Both arms of every experiment are Rime with identical request
> parameters. The variable is our text layer, not the vendor.

**"Can it do languages other than Hindi?"**
> Not today, and not with a config change — Coda is the only Rime model with Hindi at all,
> so other Indian languages need a different TTS vendor. The mechanic itself is
> language-independent: byte counts are byte counts.

**"Would a real lender's KFS parse?"**
> Unknown, and that is on the limitations slide. The synonym table that absorbs a lender's
> own label spellings is a hypothesis — there is no real lender's document in the repo to
> test it against, which is why every synonym that fires is surfaced to the reviewer.

**"Why did the call skip the greeting?"**
> Because I have nine minutes. There is a flag that drops the greeting and the "you may
> interrupt me" line so the call opens on the account number — it drops no Annex-A field
> and no key value, and the call's event log records that it was on. A real borrower hears
> the full version.

**"What's the business model / who pays?"**
> The lender, because the refusal is cheaper than the dispute. A sealed record that shows
> exactly which numbers she heard is the artifact a lender wants when a borrower claims she
> was never told the APR.

**"What would you build next?"**
> The human listening panel, then the live PSTN leg. In that order — the panel decides
> whether the central claim survives contact with actual listeners, and there is no point
> scaling a channel for a claim that hasn't been validated.

---

## Two things to say out loud, because they score

1. *"We measure and demo over a **simulated** telephone channel. We have not validated a
   live PSTN leg."*
2. *"The Hindi has **not** been reviewed by a native speaker."* Say it before a judge who
   speaks Hindi says it for you.
