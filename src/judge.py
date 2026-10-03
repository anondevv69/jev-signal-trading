"""Shared TypeSafe Noul gate for the decision layer.

FAIL-CLOSED (unlike intents.py, which is fail-open): any error, timeout, or
unparseable output returns None, and callers must treat None as "no judgment"
-- skipping the action, never acting blind. A missing judgment must never
become an approval.

Shells out to the typesafe CLI (same pattern as intents.py), which carries the
stored custom.typesafe-ai credential.
"""

import json
import os
import subprocess
import sys

last_error = ""


def _parse_noul(stdout: str, key: str):
    """Extract the noul float from CLI output. CLI shapes observed:
    {"<key>": {"type": "noul", "noul": 0.31}}  or  {"answers": {...}}."""
    try:
        data = json.loads(stdout)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    answers = data.get("answers", data)
    if not isinstance(answers, dict):
        return None
    node = answers.get(key, {})
    if not isinstance(node, dict):
        return None
    val = node.get("noul")
    return float(val) if isinstance(val, (int, float)) else None


def ask_noul(state: str, instructions: str, key: str = "q",
             timeout: int = 45, typesafe_bin: str = None):
    """Ask one Noul question. Returns float in [0,1], or None on any failure
    (fail-closed: the caller must skip the action). Never raises."""
    global last_error
    if typesafe_bin is None:
        typesafe_bin = os.environ.get(
            "TYPESAFE_BIN",
            os.path.expanduser("~/workspace/skills/typesafe-ai/bin/typesafe"),
        )
    questions = {key: {"type": "noul", "instructions": instructions}}
    try:
        proc = subprocess.run(
            [typesafe_bin, "--state", state[:4000],
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
            val = _parse_noul(proc.stdout, key)
            if val is not None:
                return max(0.0, min(1.0, val))
            last_error = f"unparseable typesafe output: {proc.stdout[:200]}"
    print(f"judge: {last_error} (fail-closed -> None)", file=sys.stderr)
    return None
