# What to say — `make talk` script

Keep this open beside the terminal. Everything below matches `--doc 1`
(`kfs_01_two_wheeler.json`) word for word — the default when you run `make talk`.

**The loan:** ₹85,000 two-wheeler loan · 24 months · EMI ₹4,305 · account `0004512378`

**Controls:** ENTER while it speaks = interrupt · ENTER while it listens = done answering

---

## Run 1 — behave, and get consent recorded

Listen to each clause fully. Do **not** press ENTER while it's talking.

### Clause 1 — लोन की पहचान (loan identity)

> It reads: *"…आपका लोन खाता नंबर है, शून्य शून्य शून्य चार पाँच एक दो तीन सात आठ।"*
> (your loan account number is 0-0-0-4-5-1-2-3-7-8)

**You say:**
> मेरा खाता नंबर शून्य शून्य शून्य चार पाँच एक दो तीन सात आठ है।

*mera khaata number shoonya shoonya shoonya chaar paanch ek do teen saat aath hai*
— "my account number is 0004512378"

### Clause 2 — मंज़ूर रकम (sanctioned amount)

> It reads: *"आपको मंज़ूर हुई रकम है, पचासी हज़ार रुपए।"*

**You say:**
> मुझे पचासी हज़ार रुपए मिलेंगे।

*mujhe pachaasi hazaar rupaye milenge* — "I will get ₹85,000"

### Clause 3 — लोन की अवधि (tenure)

> It reads: *"आपके लोन की अवधि है, चौबीस महीने, यानी दो साल।"*

**You say:**
> दो साल, यानी चौबीस महीने।

*do saal, yaani chaubees maheene* — "two years, that is 24 months"

### Clause 4 — महीने की किस्त (monthly instalment)

> It reads: *"हर महीने की किस्त होगी, चार हज़ार तीन सौ पाँच रुपए। कुल चौबीस किस्तें…"*

**You say:**
> हर महीने चार हज़ार तीन सौ पाँच रुपए, चौबीस महीने तक।

*har maheene chaar hazaar teen sau paanch rupaye, chaubees maheene tak*
— "₹4,305 every month, for 24 months"

### Consent

> It asks: *"क्या आप इन शर्तों पर सहमत हैं?"*

**You say:**
> हाँ, मैं सहमत हूँ।

*haan, main sahmat hoon* — "yes, I agree"

**Expect:** all four clauses green `UNDERSTOOD`, then `CONSENT RECORDED`.

---

## Run 2 — interrupt it, and watch consent get refused

Run `make talk` again. This time, on **clause 1**, hit **ENTER the moment it starts saying
the account number** (*"…खाता नंबर है, शून्य…"*).

Then answer clauses 2–4 normally using the script above, and at the end say **हाँ** anyway.

**Expect:**

```
heard 2.1s of 7.5s (28%)  ->  PARTIALLY_HEARD
   account_identifier   0004512378   NOT heard
Consent is blocked on this clause.
...
CONSENT REFUSED
   clauses_not_understood:identity
```

**This is the whole product.** You said yes. It said no — because it knows you never heard
the number. That refusal goes into the record where a regulator or a borrower can see it.

---

## Run 3 — answer wrong, and watch teach-back catch it

Run `make talk`. Listen to clause 2 fully (₹85,000), then when it asks, say:

> पचास हज़ार रुपए।

*pachaas hazaar rupaye* — "₹50,000" — **wrong on purpose**

**Expect:** `teach-back: FAIL`, clause goes `TEACH_BACK_FAIL`, consent still blocked.
Hearing a number is not the same as understanding it, and the system distinguishes them.

Other wrong answers to try:
- Clause 3: **तीन साल** (*teen saal*, "three years" — it's two)
- Clause 4: **तीन हज़ार रुपए** (*teen hazaar rupaye*, "₹3,000" — it's ₹4,305)

---

## If you get stuck

- **Can't use the mic** → `make talk ARGS=--typed` and type the Devanagari above.
- **It mis-hears you** → that's Sarvam, and it's worth telling me about — ASR error on your
  real voice is data, not a bug to hide.
- **Want a harder loan** → `make talk ARGS="--doc 5"` is floating-rate; `--doc 3` has an
  account number with leading zeros, the case raw TTS destroys.
- **Start over** → Ctrl-C any time.

---

## What to tell me afterwards

1. **Does the Hindi sound natural?** It was written by an agent and no native speaker has
   read it. This is the single biggest unknown in the project.
2. **Is `taru` the right voice** for a regulated financial disclosure, or should we try
   `nadi`? Those are the only two Hindi voices Coda has.
3. **Did the account number read digit-by-digit** and clearly?
4. **Did Sarvam understand your teach-back**, or did you have to repeat yourself?
