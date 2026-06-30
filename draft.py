"""Provenance Guard — M3 skeleton: /submit route + Signal 1 (LLM classifier).

Per planning.md:
  - p_ai in [0, 1] = estimated probability the text is AI-generated.
  - Signal 1 (Groq Llama-3.3-70B) returns p_ai, or None when it abstains
    (API error/timeout) rather than guessing.
"""

import json
import os

from flask import Flask, jsonify, request
from groq import Groq

app = Flask(__name__)

GROQ_MODEL = "llama-3.3-70b-versatile"

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
    """Accept {text}, run Signal 1, return p_ai.

    M3 stub: wires in Signal 1 only. M4 adds Signal 2 + the combiner; M5 adds
    the transparency label, audit log, and persistence.
    """
    body = request.get_json(silent=True) or {}
    text = (body.get("text") or "").strip()
    if not text:
        return jsonify({"error": "missing 'text'"}), 400

    p_ai = signal_llm(text)
    return jsonify({
        "p_ai": p_ai,
        "degraded": p_ai is None,  # LLM abstained; M4 falls back to heuristics
    })


@app.route("/log", methods=["GET"])
def get_log():
    ...  # M5: return the audit log


if __name__ == "__main__":
    app.run(port=5000, debug=True)
