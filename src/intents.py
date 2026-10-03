"""Intent classification via TypeSafe Choice gate.

Shells out to the typesafe CLI (~/workspace/skills/typesafe-ai/bin/typesafe)
to classify each candidate message into one of:
    call | scan-request | warning | deployment | neutral | bot-output

FAIL-OPEN: any error (missing CLI, timeout, bad output, API outage) returns
"neutral" and records the error. Classification must never block ingestion.

Prompts are kept short to minimize token cost.
"""

import json
import subprocess
import sys

INTENTS = ("call", "scan-request", "warning", "deployment", "neutral", "bot-output")

INSTRUCTIONS = (
    "Classify the author's intent regarding the token/contract mentioned. "
    "Reply with exactly one label."
)

CRITERIA = {
    "call": "Genuine buy endorsement, reported buy, or strong recommendation "
            "('ape in', 'this is the one', 'just bought', 'sending').",
    "scan-request": "Asking whether the token is safe or requesting a scan/check "
                    "('is this safe?', 'can someone scan this', 'thoughts on this').",
    "warning": "Tells others NOT to buy, or flags scam/honeypot/rug/dev dump.",
    "deployment": "The author is announcing their OWN token launch or deployment.",
    "neutral": "Info sharing, chart links, off-topic, or unclear intent.",
    "bot-output": "Automated bot reply (e.g. Rick scan results), not a human opinion.",
}

last_error = ""


def _parse_choice(stdout: str):
    """Defensively extract the chosen label from CLI output."""
    try:
        data = json.loads(stdout)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    # CLI shapes: {"answers": {"intent": {...}}} or {"intent": {...}}
    answers = data.get("answers", data) if isinstance(data, dict) else {}
    node = answers.get("intent", {})
    if not isinstance(node, dict):
        return None
    for key in ("choice", "value", "answer", "label", "selected"):
        val = node.get(key)
        if isinstance(val, str) and val in INTENTS:
            return val
    # some CLIs echo the label as the whole node
    if isinstance(node, str) and node in INTENTS:
        return node
    return None


def classify(text: str, token_hint: str = "", typesafe_bin: str = None,
             timeout: int = 30) -> str:
    """Classify one message. Never raises; fail-open returns 'neutral'."""
    global last_error
    import os
    if typesafe_bin is None:
        typesafe_bin = os.environ.get(
            "TYPESAFE_BIN",
            os.path.expanduser("~/workspace/skills/typesafe-ai/bin/typesafe"),
        )
    state = f"Message: {text[:800]}"
    if token_hint:
        state += f"\nToken: {token_hint[:120]}"
    questions = {"intent": {"type": "choice", "instructions": INSTRUCTIONS,
                            "criteria": CRITERIA}}
    try:
        proc = subprocess.run(
            [typesafe_bin, "--state", state,
             "--questions", json.dumps(questions)],
            capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError as e:
        last_error = f"typesafe CLI not found: {e}"
    except subprocess.TimeoutExpired:
        last_error = "typesafe call timed out"
    except Exception as e:
        last_error = f"typesafe error: {e}"
    else:
        if proc.returncode != 0:
            last_error = f"typesafe exit {proc.returncode}: {proc.stderr[:200]}"
        else:
            label = _parse_choice(proc.stdout)
            if label:
                return label
            last_error = f"unparseable typesafe output: {proc.stdout[:200]}"
    print(f"intents: {last_error} (fail-open -> neutral)", file=sys.stderr)
    return "neutral"


def classify_batch(items, **kwargs):
    """Classify a list of (text, token_hint); returns list of labels."""
    return [classify(t, h, **kwargs) for t, h in items]
