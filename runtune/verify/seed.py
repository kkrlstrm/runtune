"""Propose verify tasks from the sessions behind a candidate's evidence.

A capability exists because agents kept doing something the long way. The requests that
led them there are the natural tasks: the same request, run with and without the drafted
skill. `record.turns.find` reads each sample's turn back from the host's own transcript
(the prompt, the command's output, the final answer); this module turns those into task
drafts.

Only turns where the request led the agent there directly are used: the call is among the
turn's first few and the turn is short. In real sessions most inline calls sit deep inside
long, conversational work ("okay yes, run it"), and those requests are not tasks; a later
request that opens as a continuation ("yes, redeploy", "promote as is") is skipped too.

A drafted task is never run as written. It carries `"reviewed": false` and
`verify.load_tasks` refuses the file until a person has read it, because:

  - the prompt is verbatim (redacted), and may depend on context the run will not have;
  - the expected answer is a GUESS: a number that appears both in what the command printed
    and in a short final answer. When there is none, `expect` is left as a placeholder,
    which `load_tasks` also refuses;
  - the answer came from the live system on the day it ran. In strict mode the run cannot
    reach that system, so the task needs mocks, a local fixture, or `--live`.
"""

from __future__ import annotations

import re

from ..evidence.redact import redact
from ..record import turns

MAX_PROMPT = 1500
MAX_CALL_INDEX = 3      # the agent reached the old path within its first few calls
MAX_TURN_CALLS = 12     # and the whole turn was short: one request, not an afternoon of work
MAX_ANSWER = 600        # a number in a long answer matches the output by coincidence
# a later request that opens like this answers or continues the conversation before it
_CONTINUES = re.compile(r"(?i)^\s*(?:yes|yeah|yep|no|ok(?:ay)?|sure|great|perfect|good|thanks|cool|right|"
                        r"all right|alright|go ahead|do it|do that|proceed|continue|promote|apply|ship|"
                        r"now|and|also|but|so|then|let'?s|this|that|these|those|it|same)\b")
_FACT = re.compile(r"(?<![\w.])(?:\d{1,3}(?:,\d{3})+|\d+\.\d+|\d{2,})(?![\w.])")


def expectation(prompt: str, output: str, answer: str) -> dict | None:
    """A fact the command produced and the agent reported: a number that appears in both
    the command's output and the final answer, and not in the prompt (that would be the
    question restated). None when there is no such number."""
    asked = set(_FACT.findall(prompt))
    printed = set(_FACT.findall(output)) | {f.replace(",", "") for f in _FACT.findall(output)}
    for tok in _FACT.findall(answer):
        if tok in asked or tok.replace(",", "") in asked:
            continue
        if re.fullmatch(r"(?:19|20)\d\d", tok):        # a year is context, not a result
            continue
        if tok in printed or tok.replace(",", "") in printed:
            return {"regex": r"(?<![\d.,])" + re.escape(tok).replace(",", ",?") + r"(?![\d])"}
    return None


def tasks(samples: list, limit: int = 5, pattern: str | None = None) -> tuple[list, list]:
    """-> (task drafts, notes on samples that could not be used). With `pattern` (the
    measure's `old` regex), each sampled session is searched for its most direct turn
    reaching the old path, not only the call the sample names."""
    out, notes, seen = [], [], set()
    for ref in samples:
        if len(out) >= limit:
            break
        if (ref.get("actor") or "root") != "root":
            notes.append(f"{ref.get('source')} {ref.get('ts')}: a sub-agent call; its prompt was written by its parent")
            continue
        t = (turns.matching(ref, pattern) or [None])[0] if pattern else None
        t = t or turns.find(ref)
        if not t:
            notes.append(f"{ref.get('source')} {ref.get('ts')}: the session's transcript is gone or does not "
                         "contain this call" if ref.get("session") else
                         f"{ref.get('source')} {ref.get('ts')}: recorded before samples kept a session id")
            continue
        prompt = redact(t["prompt"], MAX_PROMPT + 1)
        if len(prompt) > MAX_PROMPT:
            notes.append(f"{ref['source']} {ref['ts']}: the request is {len(t['prompt'])} characters, too long to reuse")
            continue
        if t["call_index"] >= MAX_CALL_INDEX or t["turn_calls"] > MAX_TURN_CALLS:
            notes.append(f"{ref['source']} {ref['ts']}: call {t['call_index'] + 1} of {t['turn_calls']} in its turn; "
                         "the request was a larger job, not one this artifact answers")
            continue
        if t["turn_index"] and _CONTINUES.match(prompt):
            notes.append(f"{ref['source']} {ref['ts']}: request {t['turn_index'] + 1} in its session continues an "
                         f"earlier exchange (\"{prompt[:40]}\"); without that context it is not a task")
            continue
        if prompt in seen:
            continue
        seen.add(prompt)
        expect = expectation(t["prompt"], t["output"], t["answer"]) if len(t["answer"]) <= MAX_ANSWER else None
        out.append({
            "name": f"seen-{len(out) + 1}",
            "prompt": prompt,
            "expect": expect or {"contains": "<what a correct answer contains>"},
            "reviewed": False,
            "from_evidence": {
                "source": ref["source"], "session": ref["session"], "ts": ref["ts"],
                "command": redact(t["command"], 300), "output": redact(t["output"], 600),
                "answer": redact(t["answer"], 600),
                "call": f"{t['call_index'] + 1} of {t['turn_calls']} in its turn",
                "request": f"{t['turn_index'] + 1} in its session" + (" (later requests may rely on earlier ones)"
                                                                       if t["turn_index"] else ""),
                "expect_basis": ("a number in both the command's output and the answer" if expect
                                 else "none found, or the answer was too long to guess from; write it"),
            },
        })
    return out, notes
