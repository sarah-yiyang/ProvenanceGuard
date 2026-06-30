"""Provenance Guard — M3 skeleton: /submit route + Signal 1 (LLM classifier).

Per planning.md:
  - p_ai in [0, 1] = estimated probability the text is AI-generated.
  - Signal 1 (Groq Llama-3.3-70B) returns p_ai, or None when it abstains
    (API error/timeout) rather than guessing.
"""

import json
import os
import re
import statistics
import uuid
from datetime import datetime, timezone

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from groq import Groq

load_dotenv()  # pull GROQ_API_KEY from .env into the environment

app = Flask(__name__)

GROQ_MODEL = "llama-3.3-70b-versatile"

# Append-only JSON Lines stores (survive restarts). M5 may migrate these to
# SQLite (see planning.md stack). The audit log holds submission + appeal
# events; appeals.jsonl holds appeal records the reviewer queue reads.
_HERE = os.path.dirname(os.path.abspath(__file__))
AUDIT_LOG_PATH = os.path.join(_HERE, "audit_log.jsonl")
APPEALS_PATH = os.path.join(_HERE, "appeals.jsonl")


def _now_iso():
    """UTC timestamp with millisecond precision and a trailing Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _read_jsonl(path):
    try:
        with open(path) as f:
            return [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        return []


def _append_jsonl(path, entry):
    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")


def append_log(entry):
    _append_jsonl(AUDIT_LOG_PATH, entry)


def get_log(limit=50):
    """Return up to `limit` most-recent audit entries, newest first."""
    return _read_jsonl(AUDIT_LOG_PATH)[::-1][:limit]


def find_submission(content_id):
    """Return the submission audit entry for content_id, or None."""
    for e in _read_jsonl(AUDIT_LOG_PATH):
        if e.get("event") != "appeal_submitted" and e.get("content_id") == content_id:
            return e
    return None


def combine_signals(llm_p_ai, heur_p_ai):
    """Blend the two signals into a single (p_ai, degraded) per planning.md.

    Weighted average (LLM trusted more because it reads meaning):
        p_ai = 0.75 * llm + 0.25 * heuristics
    The 0.25 heuristic weight (down from 0.4) reflects calibration testing:
    on prose, stylometry is weakly discriminative and biased low, so a higher
    weight drags confident LLM scores below threshold. See test_calibration.py.
    Fallbacks: if the LLM abstained, use heuristics alone and flag `degraded`;
    if both abstained, return 0.5 (forces an Uncertain verdict).
    """
    if llm_p_ai is None and heur_p_ai is None:
        return 0.5, True
    if llm_p_ai is None:
        return heur_p_ai, True          # LLM abstained -> heuristics only
    if heur_p_ai is None:
        return llm_p_ai, False          # heuristics uncomputable (rare) -> LLM only
    return 0.75 * llm_p_ai + 0.25 * heur_p_ai, False


def attribution_from_p_ai(p_ai):
    """Map a p_ai score to a verdict using the planning.md decision bands."""
    if p_ai is None:
        return "uncertain"
    if p_ai >= 0.65:
        return "likely_ai"
    if p_ai <= 0.35:
        return "likely_human"
    return "uncertain"


# Detail text per planning.md. Moderate-confidence labels reuse the same detail
# and only swap the confidence word in the verdict line.
_LABEL_DETAIL = {
    "ai": ("Our automated signals strongly indicate this text was produced by "
           "an AI system. This is an estimate, not proof. Disagree? You can appeal."),
    "human": ("Our automated signals strongly indicate this text was written by "
              "a person. This is an estimate, not proof."),
    "uncertain": ("Our automated signals can't reliably tell whether this text "
                  "is AI-generated or human-written. Treat the origin as unknown."),
}


def make_label(p_ai, degraded=False):
    """Map a combined score to the transparency label (planning.md variants).

    Returns {variant, confidence, verdict, detail}. A `degraded` result (LLM
    abstained) is forced to Uncertain regardless of score.
    """
    attribution = "uncertain" if degraded else attribution_from_p_ai(p_ai)

    if attribution == "likely_ai":
        confidence = "High" if p_ai >= 0.80 else "Moderate"
        return {
            "variant": "ai",
            "confidence": confidence,
            "verdict": f"🤖 Likely AI-generated — {confidence} confidence",
            "detail": _LABEL_DETAIL["ai"],
        }
    if attribution == "likely_human":
        confidence = "High" if p_ai < 0.20 else "Moderate"
        return {
            "variant": "human",
            "confidence": confidence,
            "verdict": f"✍️ Likely human-written — {confidence} confidence",
            "detail": _LABEL_DETAIL["human"],
        }
    return {
        "variant": "uncertain",
        "confidence": None,
        "verdict": "❔ Uncertain — Not enough signal",
        "detail": _LABEL_DETAIL["uncertain"],
    }

# Instantiated lazily so the app still imports without a key set (e.g. tests
# of signal_llm with a stubbed client).
_client = None


def _groq_client():
    global _client
    if _client is None:
        try:
            api_key = os.environ["GROQ_API_KEY"]
        except KeyError:
            raise RuntimeError(
                "GROQ_API_KEY is not set. This is a config error, not a model "
                "abstain — set the key before running Signal 1."
            )
        _client = Groq(api_key=api_key)
    return _client


CLASSIFIER_PROMPT = (
    "You are a detector of AI-generated text. Estimate the probability the "
    "passage was written by an AI system.\n"
    "Genuine signals of AI authorship: generic or padded phrasing, hollow "
    "transitions, hedging, even rhetorical balance, and an absence of personal "
    "voice or specific lived detail.\n"
    "Do NOT treat these as AI signals on their own: brevity, terseness, "
    "imperative mood, or formulaic genres (recipes, instructions, factual "
    "statements, lists). Humans routinely write this way. When the passage is "
    "too short or too formulaic to judge confidently, return p_ai near 0.5 "
    "rather than committing.\n"
    "Respond ONLY with JSON of the form "
    '{"p_ai": <number 0-1>, "reason": "<short explanation>"}, where p_ai is '
    "your estimated probability the text is AI-generated."
)


# --- Signal 1: LLM classifier -------------------------------------------------

def signal_llm(text):
    """Return p_ai in [0, 1] from the Groq classifier, or None if it abstains.

    Abstains (returns None) on any API error/timeout or malformed response so
    the combiner can fall back to heuristics and flag the result `degraded`.
    """
    client = _groq_client()  # outside try: a missing key is a config error, not an abstain
    try:
        resp = client.chat.completions.create(
            model=GROQ_MODEL,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": CLASSIFIER_PROMPT},
                {"role": "user", "content": text},
            ],
        )
        data = json.loads(resp.choices[0].message.content)
        p_ai = float(data["p_ai"])
    except Exception:
        # Abstain on any API error/timeout or malformed response.
        return None

    if not 0.0 <= p_ai <= 1.0:
        return None
    return p_ai


# --- Signal 2: stylometric heuristics (local, no network) ---------------------

def _clamp(x):
    return max(0.0, min(1.0, x))


def signal_heuristics(text):
    """Return p_ai in [0, 1] from local stylometry, or None if uncomputable.

    Two sub-metrics, each in [0, 1], averaged (per planning.md):
      - burstiness_ai: low sentence-length variance reads as AI-like.
      - diversity_ai:  low vocabulary diversity (type-token ratio) reads AI-like.
    Returns None when there are too few words/sentences to measure.
    """
    sentences = [s for s in re.split(r"[.!?]+", text) if s.strip()]
    words = re.findall(r"\b\w+\b", text.lower())
    if not words or not sentences:
        return None

    # Burstiness: coefficient of variation of sentence lengths. Humans vary
    # sentence length more; uniform lengths (low variance) read as AI-like.
    sent_lengths = [len(re.findall(r"\b\w+\b", s)) for s in sentences]
    mean_len = statistics.mean(sent_lengths)
    if len(sent_lengths) < 2 or mean_len == 0:
        burstiness_ai = 1.0  # can't measure variance -> treat as maximally uniform
    else:
        cv = statistics.pstdev(sent_lengths) / mean_len
        burstiness_ai = _clamp(1.0 - cv)

    # Diversity: type-token ratio. Low TTR (repetitive vocabulary) reads AI-like.
    type_token_ratio = len(set(words)) / len(words)
    diversity_ai = _clamp(1.0 - type_token_ratio)

    return (burstiness_ai + diversity_ai) / 2.0


# --- Routes -------------------------------------------------------------------

@app.route("/")
def home():
    return "Provenance Guard is running."


@app.route("/submit", methods=["POST"])
def submit():
    """Accept {text}, run both signals, combine, return a decision record.

    M3: Signal 1 only. M4: Signal 2 + combiner fill the real confidence and
    attribution. M5 adds the human-readable label and SQLite.
    """
    body = request.get_json(silent=True) or {}
    text = (body.get("text") or "").strip()
    if not text:
        return jsonify({"error": "missing 'text'"}), 400

    llm_p_ai = signal_llm(text)            # Signal 1; None if it abstained
    heur_p_ai = signal_heuristics(text)    # Signal 2; None if uncomputable
    p_ai, degraded = combine_signals(llm_p_ai, heur_p_ai)

    entry = {
        "content_id": uuid.uuid4().hex,
        "creator_id": body.get("creator_id", "anonymous"),
        "timestamp": _now_iso(),
        "attribution": attribution_from_p_ai(p_ai),  # 3 categories via decision bands
        "confidence": round(p_ai, 3),                # combined score
        "llm_score": llm_p_ai,                       # Signal 1 (None if abstained)
        "heuristic_score": None if heur_p_ai is None else round(heur_p_ai, 3),  # Signal 2
        "label": make_label(p_ai, degraded),         # transparency label text
        "status": "degraded" if degraded else "classified",
        "review_status": "published",                # flips to under_review on appeal
        "text": text,                                # retained for the appeal reviewer
    }
    append_log(entry)
    return jsonify(entry)


@app.route("/appeal", methods=["POST"])
def appeal():
    """Accept {content_id, reason} from a creator who disputes a label.

    Per planning.md: validate the submission exists, open an appeal record
    (status under_review), flip the submission's review_status, and log an
    appeal_submitted audit event.
    """
    body = request.get_json(silent=True) or {}
    content_id = (body.get("content_id") or "").strip()
    reason = (body.get("reason") or "").strip()
    if not content_id or not reason:
        return jsonify({"error": "content_id and reason are required"}), 400

    submission = find_submission(content_id)
    if submission is None:
        return jsonify({"error": "unknown content_id"}), 404

    record = {
        "appeal_id": uuid.uuid4().hex,
        "content_id": content_id,
        "reason": reason,
        "status": "under_review",
        "created_at": _now_iso(),
    }
    _append_jsonl(APPEALS_PATH, record)
    # Audit the status flip published -> under_review (append-only log).
    append_log({
        "event": "appeal_submitted",
        "content_id": content_id,
        "timestamp": _now_iso(),
        "reason": reason,
        "review_status": "under_review",
    })
    return jsonify(record), 201


def submission_review_status(content_id):
    """Derived status: under_review if an open appeal exists, else published."""
    for ap in _read_jsonl(APPEALS_PATH):
        if ap["content_id"] == content_id and ap["status"] == "under_review":
            return "under_review"
    return "published"


@app.route("/appeals", methods=["GET"])
def appeals_queue():
    """Reviewer queue: open appeals joined with their submission (planning.md)."""
    queue = []
    for ap in _read_jsonl(APPEALS_PATH):
        if ap["status"] != "under_review":
            continue
        sub = find_submission(ap["content_id"]) or {}
        queue.append({
            "appeal_id": ap["appeal_id"],
            "content_id": ap["content_id"],
            "reason": ap["reason"],
            "submitted_at": sub.get("timestamp"),
            "text": sub.get("text"),
            "label": sub.get("label"),
            "confidence": sub.get("confidence"),
            "llm_score": sub.get("llm_score"),
            "heuristic_score": sub.get("heuristic_score"),
            "attribution": sub.get("attribution"),
        })
    return jsonify({"queue": queue})


@app.route("/log", methods=["GET"])
def get_log_route():
    limit = request.args.get("limit", default=50, type=int)
    return jsonify({"entries": get_log(limit)})


if __name__ == "__main__":
    # Port 5000 is taken by macOS AirPlay Receiver (ControlCenter), which
    # returns 403s — use 5001 to avoid the clash.
    app.run(port=5001, debug=True)
