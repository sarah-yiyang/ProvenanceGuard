# Provenance Guard — Planning

A service that estimates whether submitted text is AI-generated, attaches a transparency label, and lets creators appeal the result.

**Stack:** Flask · Groq (Llama-3.3-70B-Versatile) · SQLite (submissions, audit log, appeals)

---

## Variable: `p_ai`

Every signal and the final score speak one language: `p_ai ∈ [0, 1]`, the estimated probability the text is AI-generated. `p_ai = 1.0` means certainly AI; `0.0` means certainly human; `0.5` means no information either way. 

---

## Detection signals

Two independent signals, each emitting a `p_ai ∈ [0, 1]`.

### Signal 1 — LLM classifier (Groq, Llama-3.3-70B-Versatile)
- **Measures:** semantic/stylistic markers of AI authorship (generic phrasing, even structure, hedging, lack of personal specificity).
- **How:** one constrained prompt asks the model to return a JSON `{"p_ai": <0-1>, "reason": "<short>"}`. Temperature 0 for stability.
- **Output:** `p_ai` float in `[0, 1]`. On API error/timeout the signal abstains (returns `null`) rather than guessing.

### Signal 2 — Stylometric heuristics (local, no network)
- **Measures:** statistical fingerprints AI text tends to leave: low burstiness (uniform sentence length — humans vary more) and high **lexical predictability** (low type-token ratio, few rare words).
- **How:** compute two normalized sub-metrics in `[0, 1]` and average them:
  - `burstiness_ai = clamp(1 − stdev(sentence_lengths) / mean(sentence_lengths))` — lower variance ⇒ more AI-like.
  - `diversity_ai = 1 − type_token_ratio` — lower vocabulary diversity ⇒ more AI-like.
- **Output:** `p_ai = mean(burstiness_ai, diversity_ai)`, a float in `[0, 1]`.

### Combination
Weighted average, LLM trusted more because it reads meaning, not just surface stats:

```
p_ai = 0.6 * llm.p_ai + 0.4 * heuristics.p_ai
```

If the LLM signal abstains (`null`), fall back to heuristics alone (`p_ai = heuristics.p_ai`) and flag `degraded: true` on the record so the label can be softened. Both abstaining ⇒ `p_ai = 0.5` (forces an *Uncertain* label).

---

## Uncertainty representation

**Example: what `p_ai = 0.6` means:** the system's combined estimate is a 60% chance the text is AI-generated. That is not enough to assert "AI"; it falls in the **Uncertain** band below.

**Calibration (raw → trustworthy score):**
- The LLM's self-reported probability is uncalibrated, so we don't treat it as ground truth — it's one weighted input.
- Heuristic sub-metrics are squashed into `[0, 1]` with `clamp(...)` so no single outlier sentence dominates.
- Both signals are kept on the same `p_ai` scale specifically so the weighted blend is meaningful. (Post-MVP: fit weights/thresholds against a labeled sample to formally calibrate.)

**Decision bands** (a symmetric "dead zone" around 0.5 is the *Uncertain* region):

| `p_ai`        | Verdict        | Confidence read |
|---------------|----------------|-----------------|
| `≥ 0.80`      | Likely AI      | high            |
| `0.65 – 0.80` | Likely AI      | moderate        |
| `0.35 – 0.65` | **Uncertain**  | —               |
| `0.20 – 0.35` | Likely Human   | moderate        |
| `< 0.20`      | Likely Human   | high            |

Thresholds: **≥ 0.65 = likely AI**, **≤ 0.35 = likely human**, **between = uncertain**. A `degraded` result (LLM abstained) caps confidence at *moderate* regardless of `p_ai`.

---

## Transparency label design

Three exact variants (the verdict line is what the UI renders; the detail line is shown on expand):

**High-confidence AI** (`p_ai ≥ 0.80`)
> 🤖 **Likely AI-generated** — High confidence
> Our automated signals strongly indicate this text was produced by an AI system. This is an estimate, not proof. Disagree? You can appeal.

**High-confidence human** (`p_ai < 0.20`)
> ✍️ **Likely human-written** — High confidence
> Our automated signals strongly indicate this text was written by a person. This is an estimate, not proof.

**Uncertain** (`0.35 ≤ p_ai ≤ 0.65`, or any `degraded` result)
> ❔ **Uncertain** — Not enough signal
> Our automated signals can't reliably tell whether this text is AI-generated or human-written. Treat the origin as unknown.

*Moderate* bands reuse the AI/Human variants with "High confidence" swapped for "Moderate confidence." Every label states it is an estimate and (for non-human verdicts) points to appeal.

---

## Appeals workflow

- **Who:** the creator of a submission (identified by the `submission_id` returned at submit time).
- **What they provide:** `submission_id` + free-text `reason` explaining why they believe the label is wrong.
- **On receipt the system:**
  1. Validates the `submission_id` exists.
  2. Creates an `appeal` row: `{id, submission_id, reason, status: "under_review", created_at}`.
  3. Flips the submission's label status `published → under_review` (the public label shows "under review").
  4. Appends an `appeal_submitted` entry to the audit log (submission id, timestamp, reason).
- **Reviewer queue view:** a list of `under_review` appeals, each showing — original text, the assigned label + `p_ai` + per-signal scores, the creator's reason, and the submission timestamp. Reviewer can uphold or overturn; either action logs `appeal_resolved` and sets status to `resolved`.

---

## Anticipated edge cases

System will handle these poorly; documented so reviewers and users know the limits.

1. **Short or templated text** (≤ 2 sentences, or boilerplate like a job-posting blurb). Burstiness/diversity are statistically meaningless on so few tokens, and the LLM has little to read — these get pushed toward *Uncertain* or misclassified as AI because they're terse and uniform. *Mitigation:* below a minimum token count, force the *Uncertain* label rather than reporting a confident verdict.

2. **Heavily-edited or AI-assisted human writing.** A human draft polished by Grammarly, or an AI draft a person rewrote, is genuinely a blend — there is no correct binary answer. Our signals will land mid-range and the result is honestly *Uncertain*, but users may expect a definitive call.

3. **Highly formulaic human genres** — sonnets, legal clauses, recipe steps, technical API docs. Their intentional uniformity (regular meter, fixed structure, controlled vocabulary) mimics the low-burstiness/low-diversity fingerprint of AI text, biasing the heuristic signal toward AI. The 0.7-weighted LLM signal partly counteracts this, which is a reason the heuristic weight is kept low.

---

## Architecture

### Submission Flow
```
                          +------------------+
                          |   Client/User    |
                          +--------+---------+
                                   |
                     POST /submit {text}
                                   |
                           [Rate Limiter]
                                   |
                                   v
                       +----------------------+
                       |  Submit Endpoint     |
                       +----------+-----------+
                                  |
                               raw text
                                  |
             +--------------------+--------------------+
             |                                         |
             v                                         v
+---------------------------+         +------------------------------+
| Signal 1                  |         | Signal 2                     |
| Groq Llama-3.3-70B        |         | Stylometric Heuristics       |
| p_ai (0-1)                |         | burstiness, lexical diversity|
+-------------+-------------+         +--------------+---------------+
              | p_ai                                | p_ai
              +--------------------+----------------+
                                   |
                                   v
                     +----------------------------+
                     | Confidence Combiner        |
                     | p_ai = 0.7*llm + 0.3*heur  |
                     +-------------+--------------+
                                   |
                       p_ai + per-signal scores
                                   |
                                   v
                     +----------------------------+
                     | Transparency Label         |
                     +-------------+--------------+
                                   |
                           decision record
                                   |
                  +----------------+----------------+
                  |                                 |
                  v                                 v
           +---------------+                +---------------+
           | Audit Log     |                | API Response  |
           +---------------+                +---------------+
```

### Appeal Flow
```
Client
  |
POST /appeal {submission_id, reason}
  |
  v
+-------------------+
| Appeal Endpoint   |
+---------+---------+
          |
 status -> "under_review"  ----> Audit Log
          |
          v
     API Response  ----> Reviewer Queue (uphold / overturn)
```

**Narrative.** On **submission**, text is rate-limited, fanned out to both signals in parallel, blended into a single `p_ai`, mapped to a transparency label, and persisted as a decision record to the audit log before the response returns. On **appeal**, the creator references their `submission_id`; the submission flips to `under_review`, the appeal is logged and queued, and a human reviewer later upholds or overturns it.

---

## AI Tool Plan

How each implementation milestone is handed to the AI coding tool: spec provided → ask → verification.

### M3 — Submission endpoint + first signal
- **Provide:** [Detection signals](#detection-signals) (Signal 1 + combination contract) and the [submission-flow diagram](#submission-flow).
- **Ask for:** Flask app skeleton (`/submit` route, SQLite setup, audit-log helper) and `signal_llm(text) -> p_ai` calling Groq with the JSON-constrained prompt.
- **Verify:** call `signal_llm` directly on a few obviously-AI and obviously-human samples and confirm `p_ai` separates them, *before* wiring it into `/submit`.

### M4 — Second signal + confidence scoring
- **Provide:** [Detection signals](#detection-signals), [Uncertainty representation](#uncertainty-representation), and the [submission-flow diagram](#submission-flow).
- **Ask for:** `signal_heuristics(text) -> p_ai` (burstiness + diversity) and `combine(llm, heur) -> p_ai` with the abstain/`degraded` fallback rules.
- **Check:** scores vary meaningfully — clearly-AI text lands ≥ 0.65, clearly-human ≤ 0.35, and the LLM-abstain path falls back to heuristics and sets `degraded`.

### M5 — Production layer (labels + appeals)
- **Provide:** [Transparency label design](#transparency-label-design), [Appeals workflow](#appeals-workflow), and both [architecture diagrams](#architecture).
- **Ask for:** `make_label(p_ai, degraded) -> {variant, text}` and the `/appeal` endpoint (status flip, appeal row, audit entry) plus the reviewer-queue read.
- **Verify:** all three label variants are reachable across the `p_ai` range (and `degraded` forces *Uncertain*); an appeal flips the submission to `under_review` and writes an `appeal_submitted` audit entry.
```
