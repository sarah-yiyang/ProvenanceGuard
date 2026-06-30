# ProvenanceGuard

A service that estimates whether submitted text is AI-generated, attaches a
transparency label, and lets creators appeal the result. See `planning.md` for
the full design (detection signals, scoring, labels, appeals, architecture).

## Rate limiting

The `/submit` endpoint is rate-limited with Flask-Limiter, keyed by client IP:

```
@limiter.limit("10 per minute;100 per day")
```

**Reasoning — chosen to fit a real writer while blocking scripted abuse:**

- **10 per minute** — a person submitting their own work pastes a piece, reads
  the label, maybe tweaks and resubmits. Even an active writer rarely exceeds a
  handful of submissions a minute; 10 leaves comfortable headroom for normal
  use (including retries) while a flooding script trips it almost immediately.
- **100 per day** — a generous ceiling for genuine daily authoring, but it caps
  the slow-drip abuse case that stays under the per-minute limit (e.g. ~1
  request every 15 seconds all day would still be stopped at 100). It also
  bounds cost, since each submission makes a paid Groq API call.

The two limits compose: the per-minute rule stops bursts, the per-day rule stops
sustained low-rate scraping. Storage is `memory://` (single-process dev); a
multi-worker deployment would point `storage_uri` at a shared store like Redis.

### Test evidence

Sending 12 rapid POSTs to `/submit` (more than the 10/minute limit) — the first
10 succeed, the rest are rejected with HTTP 429:

```
status codes: [200, 200, 200, 200, 200, 200, 200, 200, 200, 200, 429, 429]
200 count: 10 | 429 count: 2
```

A rejected request returns:

```json
{ "error": "rate limit exceeded", "detail": "10 per 1 minute" }
```

Reproduce against a running server (note: port **5001**, since macOS AirPlay
occupies 5000):

```bash
for i in $(seq 1 12); do
  curl -s -o /dev/null -w "%{http_code}\n" -X POST http://localhost:5001/submit \
    -H "Content-Type: application/json" \
    -d '{"text": "This is a test submission for rate limit testing purposes only.", "creator_id": "ratelimit-test"}'
done
```

## Audit log

Every submission appends one structured JSON line to `audit_log.jsonl`
(JSON Lines — machine-readable, not console output). `GET /log` returns the
most recent entries as JSON. Each submission entry captures:

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

Note how carol's submission shows `review_status: "under_review"` after the
appeal, and the `appeal_submitted` event records the creator's reasoning
alongside the original classification.

## AI Usage
1. revised signal 1 prompt to avoid detecting formula language (e.g. recipe) as AI
