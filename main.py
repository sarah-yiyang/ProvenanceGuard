"""Provenance Guard — M3 skeleton: /submit route + Signal 1 (LLM classifier).

Per planning.md:
  - p_ai in [0, 1] = estimated probability the text is AI-generated.
  - Signal 1 (Groq Llama-3.3-70B) returns p_ai, or None when it abstains
    (API error/timeout) rather than guessing.
"""

import json
import os
import uuid
from datetime import datetime, timezone

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from groq import Groq

load_dotenv()  # pull GROQ_API_KEY from .env into the environment

app = Flask(__name__)

GROQ_MODEL = "llama-3.3-70b-versatile"

# Structured audit log, one JSON object per line (JSON Lines). Survives
# restarts and stays append-only. M5 may migrate this to SQLite (see
# planning.md stack); the appeal endpoint looks submissions up by content_id.
AUDIT_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audit_log.jsonl")


def _now_iso():
    """UTC timestamp with millisecond precision and a trailing Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def append_log(entry):
    with open(AUDIT_LOG_PATH, "a") as f:
        f.write(json.dumps(entry) + "\n")


def get_log(limit=50):
    """Return up to `limit` most-recent entries, newest first."""
    try:
        with open(AUDIT_LOG_PATH) as f:
            entries = [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        return []
    return entries[::-1][:limit]


def attribution_from_p_ai(p_ai):
    """Map a p_ai score to a verdict using the planning.md decision bands."""
    if p_ai is None:
        return "uncertain"
    if p_ai >= 0.65:
        return "likely_ai"
    if p_ai <= 0.35:
        return "likely_human"
    return "uncertain"

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
    "You are a detector of AI-generated text. Judge whether the passage was "
    "written by an AI system, looking for generic phrasing, even structure, "
    "hedging, and a lack of personal specificity.\n"
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


# --- Routes -------------------------------------------------------------------

@app.route("/")
def home():
    return "Provenance Guard is running."


@app.route("/submit", methods=["POST"])
def submit():
    """Accept {text}, run Signal 1, return a decision record.

    M3: Signal 1 only, with placeholder confidence/label. M4 adds Signal 2 +
    the combiner (real confidence); M5 adds the real label and SQLite.
    """
    body = request.get_json(silent=True) or {}
    text = (body.get("text") or "").strip()
    if not text:
        return jsonify({"error": "missing 'text'"}), 400

    llm_p_ai = signal_llm(text)  # Signal 1; None if it abstained

    entry = {
        "content_id": uuid.uuid4().hex,
        "creator_id": body.get("creator_id", "anonymous"),
        "timestamp": _now_iso(),
        "attribution": attribution_from_p_ai(llm_p_ai),
        "confidence": None,            # placeholder — M4 combiner fills this
        "llm_score": llm_p_ai,         # Signal 1 score (None if abstained)
        "status": "degraded" if llm_p_ai is None else "classified",
        "text": text,                  # retained so the appeal reviewer sees the original
    }
    append_log(entry)
    return jsonify(entry)


@app.route("/log", methods=["GET"])
def get_log_route():
    limit = request.args.get("limit", default=50, type=int)
    return jsonify({"entries": get_log(limit)})


if __name__ == "__main__":
    # Port 5000 is taken by macOS AirPlay Receiver (ControlCenter), which
    # returns 403s — use 5001 to avoid the clash.
    app.run(port=5001, debug=True)
