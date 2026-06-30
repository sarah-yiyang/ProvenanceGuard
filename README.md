# ProvenanceGuard

A service that estimates whether submitted text is AI-generated, attaches a
transparency label, and lets creators appeal the result. The full design lives
in `planning.md`; this README covers what was built and why.

## Running it

```bash
cd ProvenanceGuard
python3 -m pip install -r requirements.txt   # first run only
python3 main.py
```

Serves on **http://localhost:5001** (port 5000 is occupied by macOS AirPlay
Receiver, which returns 403s). Requires `GROQ_API_KEY` in a `.env` file.

Endpoints: `GET /` · `POST /submit` · `GET /log` · `POST /appeal` · `GET /appeals`

```bash
curl -s -X POST http://localhost:5001/submit \
  -H "Content-Type: application/json" \
  -d '{"text":"The mitochondria is the powerhouse of the cell.","creator_id":"demo"}' \
  | python3 -m json.tool
```

## Detection signals

Every signal speaks one language: `p_ai ∈ [0,1]`, the estimated probability the
text is AI-generated. The system uses two independent signals:

**Signal 1 — LLM classifier (Groq, Llama-3.3-70B).** A single constrained prompt
asks the model to return `{"p_ai", "reason"}`. *Why an LLM:* the most reliable
tell of AI text is *semantic* — generic phrasing, hollow transitions, absence of
personal voice — and only a language model reads meaning. Groq specifically:
fast, cheap, JSON-mode support, capable enough for this judgment.

**Signal 2 — Stylometric heuristics (local, no network).** Two metrics averaged:
*burstiness* (coefficient of variation of sentence length — humans vary more) and
*lexical diversity* (type-token ratio). *Why add it:* it's a fully independent,
zero-cost, offline cross-check, and it's the fallback when the LLM call fails, so
the system degrades instead of breaking.

**Why two signals:** they fail in different ways. The LLM can be fooled by
formal human prose; stylometry can be fooled by fluent AI prose. Combining them
means a single failure mode doesn't dominate — and either can cover for the other.

**What I'd change for a real deployment:**
- Replace the LLM's self-reported probability (uncalibrated) with a purpose-built
  detector — e.g. a perplexity/log-likelihood score from a reference model, or a
  fine-tuned classifier with a real decision threshold.
- Strengthen Signal 2: sentence-length variance is a weak proxy. Token-level
  perplexity against a reference LM would discriminate far better.
- Add orthogonal signals (watermark detection, embedding-space classifiers) so
  the ensemble doesn't lean so heavily on one model.

## Confidence scoring

The two signals are combined with a **weighted average**:

```
p_ai = 0.75 * llm_score + 0.25 * heuristic_score
```

*Why weighted, and why this split:* the LLM is the stronger signal on prose, so
it carries the decision while heuristics nudge. The 0.75/0.25 split was **not
arbitrary** — it came out of calibration testing (Milestone 4). At the original
0.6/0.4, the weak heuristic signal (which clusters low, ~0.35, regardless of
input) dragged genuinely-AI text below the decision threshold. Down-weighting
it fixed that without letting it become noise. *Why keep heuristics at all:*
they're the abstain-fallback and an independent cross-check.

*Why a three-way mapping with an "uncertain" dead-zone* (`≥0.65` AI, `≤0.35`
human, between = uncertain): for a transparency tool, a confident wrong answer is
worse than an honest "I can't tell." The dead-zone forces the system to say so.

### Meaningful variation — two examples

| Submission | `llm_score` | `heuristic_score` | **combined** | Label |
|---|---|---|---|---|
| *"Artificial intelligence represents a transformative paradigm shift in modern society. Stakeholders across various sectors must collaborate to ensure responsible deployment…"* | 0.80 | 0.417 | **0.704** | 🤖 Likely AI-generated |
| *"Preheat oven to 350F. Mix flour and sugar. Add butter. Bake 25 minutes. Let cool."* | 0.50 | 0.351 | **0.463** | ❔ Uncertain |

The combined score moves from **0.704** (confident AI) down to **0.463**
(genuinely uncertain) — the scoring produces real variation, not a constant. A
casual human note (*"ok so i finally tried that ramen place…"*) lands lower still
at **0.205** → ✍️ Likely human-written.

*What I'd change for real:* the thresholds were tuned on a handful of examples,
not a labeled dataset — they're directionally sound but not statistically
calibrated. I'd fit them (and the weights) on real labeled data, and I'd separate
the stored `confidence` (currently just `p_ai`) from a true *verdict confidence*
(`2·|p_ai − 0.5|`), since a low `p_ai` for a human verdict reads confusingly as
"low confidence" when the system is actually quite sure it's human.

## Transparency label variants

The label text changes with the score. Three variants (exact displayed text):

**High-confidence AI** (`p_ai ≥ 0.80`)
> 🤖 **Likely AI-generated — High confidence**
> Our automated signals strongly indicate this text was produced by an AI system. This is an estimate, not proof. Disagree? You can appeal.

**High-confidence human** (`p_ai < 0.20`)
> ✍️ **Likely human-written — High confidence**
> Our automated signals strongly indicate this text was written by a person. This is an estimate, not proof.

**Uncertain** (`0.35 ≤ p_ai ≤ 0.65`, or any degraded result)
> ❔ **Uncertain — Not enough signal**
> Our automated signals can't reliably tell whether this text is AI-generated or human-written. Treat the origin as unknown.

Scores in the moderate bands (`0.65–0.80` AI, `0.20–0.35` human) reuse the AI/
human text with "High confidence" swapped for "Moderate confidence."

## Appeals workflow

A creator who disputes a label submits `POST /appeal` with the `content_id` and
`creator_reasoning`. The endpoint validates the submission exists, flips its
`review_status` from `published` to `under_review` **in storage**, logs an
`appeal_submitted` event alongside the original classification, and returns a
confirmation. There is no automated re-classification — a human reviewer works
the queue (`GET /appeals`), which shows the original text, label, both signal
scores, and the creator's reasoning.

## Rate limiting

The `/submit` endpoint is rate-limited with Flask-Limiter, keyed by client IP:

```
@limiter.limit("10 per minute;100 per day")
```

**Reasoning — fits a real writer while blocking scripted abuse:**

- **10 per minute** — a person pastes a piece, reads the label, tweaks, resubmits.
  Even an active writer rarely exceeds a handful a minute; 10 leaves headroom for
  normal use and retries, while a flooding script trips it almost immediately.
- **100 per day** — generous for genuine daily authoring, but it caps the
  slow-drip abuser who stays under the per-minute limit, and it bounds cost since
  each submission is a paid Groq API call.

The two compose: per-minute stops bursts, per-day stops sustained scraping.
Storage is `memory://` (single-process dev); a multi-worker deployment would point
`storage_uri` at a shared store like Redis.

### Test evidence

12 rapid POSTs to `/submit` — first 10 succeed, the rest are rejected with 429:

```
status codes: [200, 200, 200, 200, 200, 200, 200, 200, 200, 200, 429, 429]
200 count: 10 | 429 count: 2
```

A rejected request returns `{"error": "rate limit exceeded", "detail": "10 per 1 minute"}`.

```bash
for i in $(seq 1 12); do
  curl -s -o /dev/null -w "%{http_code}\n" -X POST http://localhost:5001/submit \
    -H "Content-Type: application/json" \
    -d '{"text": "This is a test submission for rate limit testing purposes only.", "creator_id": "ratelimit-test"}'
done
```

## Audit log

Every submission appends one structured JSON line to `audit_log.jsonl`
(JSON Lines — machine-readable, not console output). `GET /log` returns the most
recent entries. Each submission entry captures:

| Field | Meaning |
|-------|---------|
| `timestamp` | UTC time of classification (ms precision) |
| `content_id` | unique submission ID (used by `/appeal`) |
| `attribution` | `likely_ai` / `likely_human` / `uncertain` |
| `confidence` | combined score (0–1) |
| `llm_score` | Signal 1 (Groq) score |
| `heuristic_score` | Signal 2 (stylometry) score |
| `label` | the transparency label shown to users |
| `review_status` | `published`, or `under_review` once an appeal is filed |

Sample entries (3 submissions + 1 appeal event):

```json
{"content_id": "534f6afa…", "creator_id": "alice", "timestamp": "2026-06-30T21:34:15.412Z", "attribution": "likely_ai", "confidence": 0.704, "llm_score": 0.8, "heuristic_score": 0.417, "label": {"variant": "ai", "confidence": "Moderate", "verdict": "🤖 Likely AI-generated — Moderate confidence"}, "review_status": "published"}
{"content_id": "b353a731…", "creator_id": "bob", "timestamp": "2026-06-30T21:34:15.778Z", "attribution": "likely_human", "confidence": 0.205, "llm_score": 0.2, "heuristic_score": 0.219, "label": {"variant": "human", "confidence": "Moderate", "verdict": "✍️ Likely human-written — Moderate confidence"}, "review_status": "published"}
{"content_id": "482e8158…", "creator_id": "carol", "timestamp": "2026-06-30T21:34:16.116Z", "attribution": "uncertain", "confidence": 0.463, "llm_score": 0.5, "heuristic_score": 0.351, "label": {"variant": "uncertain", "confidence": null, "verdict": "❔ Uncertain — Not enough signal"}, "review_status": "under_review"}
{"event": "appeal_submitted", "content_id": "482e8158…", "timestamp": "2026-06-30T21:34:16.123Z", "creator_reasoning": "I wrote this recipe myself.", "review_status": "under_review", "original_classification": {"attribution": "uncertain", "confidence": 0.463, "llm_score": 0.5, "heuristic_score": 0.351}}
```

Carol's submission shows `review_status: "under_review"` after the appeal, and
the `appeal_submitted` event records the creator's reasoning alongside the
original classification.

## Known limitations

**Fluent, well-written AI text is nearly invisible to Signal 2 — and can slip
past the system.** Signal 2 only measures *surface uniformity* (sentence-length
variance, vocabulary repetition). Modern AI prose with varied sentence lengths
and a rich vocabulary scores *low* on it — in testing, a clearly-AI corporate
paragraph scored just **0.417** on the stylometric signal. So Signal 2 doesn't
just miss good AI; it actively pulls the combined score *down* toward "human" for
exactly the most capable AI output. The system leans on the LLM signal to catch
these, and if that signal is also fooled (or abstains), polished AI text can be
labeled human or uncertain. This is structural, not a tuning issue: stylometry
measures form, and good AI no longer has a distinctive form.

**Formal or non-native human writing skews toward "AI."** Signal 1 keys on
"generic phrasing, even structure, lack of personal voice" — properties that
academic writing, ESL writing, and corporate prose genuinely share. In testing, a
real human paragraph about monetary policy scored 0.70 on the LLM signal. The
system honestly lands these on "uncertain" rather than a false accusation, but it
will under-credit human authors who write formally.

## Spec reflection

**How the spec helped:** writing the label variants and decision bands in
`planning.md` *before* coding gave `make_label()` an exact contract — the
implementation was a near-mechanical translation, and there was never ambiguity
about which text appears at which score. Defining `p_ai` as one shared quantity
up front also kept both signals and the combiner on the same scale, which made
the weighted blend meaningful instead of comparing apples to oranges.

**How the implementation diverged:** the spec specified combiner weights of
`0.6/0.4` (LLM/heuristics). Calibration testing showed this miscalibrated
confident-AI text *down* into "uncertain," because the heuristic signal is biased
low. I changed the weights to **0.75/0.25** and updated `planning.md` to match.
The spec was the right starting hypothesis; the divergence was driven by evidence
the spec couldn't have anticipated without running the signals on real inputs.

## AI usage

**1. Fixing false positives on formulaic text.** I directed the AI to revise the
Signal 1 prompt so formulaic genres (recipes, instructions) wouldn't be
flagged as AI merely for being short and uniform. It produced a prompt that names
the real AI markers and explicitly tells the model that brevity and imperative
mood are not AI evidence. I verified the fix: a recipe dropped from `p_ai 0.90` to
`0.50` (now correctly "uncertain") with no regression on clearly-AI text — and I
kept the revision after confirming the trade-off (clearly-AI dipped 0.90→0.80,
still high) was acceptable.

**2. Diagnosing and recalibrating the combiner.** I directed the AI to run 4
deliberately chosen inputs and explain why a clearly-AI paragraph was landing
"uncertain." It traced the cause to Signal 2 being biased low and dragging the
blend below threshold, then tested candidate weights. It proposed a range; I
overrode its options by choosing 0.75/0.25 specifically, because at 0.8/0.2 a
known-human formal paragraph wrongly flipped to "likely AI" — I picked the weight
that fixed the AI case without breaking the human case.

