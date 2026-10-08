import json
import pytest

from runtune.experiment_selection import assess_candidate, wilson_interval
from runtune.experiment_registry import append_hypothesis, read_history


def test_wilson_bounds():
    assert wilson_interval(0, 10)[0] == 0
    assert wilson_interval(10, 10)[1] == 1
    with pytest.raises(ValueError):
        wilson_interval(11, 10)


def test_selection_holds_weak_or_costly_results():
    a = {"successes": 40, "trials": 100, "total_cost": 10}
    b = {"successes": 90, "trials": 100, "total_cost": 12}
    assert assess_candidate(a, b)["recommendation"] == "hold"
    assert "cost_budget_exceeded" in assess_candidate(a, b)["reasons"]
    assert assess_candidate(a, {**b, "total_cost": 10})["recommendation"] == "eligible_for_review"
    assert "cost_unavailable" in assess_candidate(a, {"successes": 90, "trials": 100})["reasons"]


def test_history_tamper_detection(tmp_path):
    p = tmp_path / "history.jsonl"
    append_hypothesis(p, hypothesis_id="h1", hypothesis="fewer retries", candidate_sha="abc",
                      verdict="proposed", evidence={"case": "x"})
    append_hypothesis(p, hypothesis_id="h1", hypothesis="fewer retries", candidate_sha="abc",
                      verdict="rejected", evidence={"reason": "no gain"})
    assert len(read_history(p)) == 2
    with pytest.raises(ValueError):
        append_hypothesis(p, hypothesis_id="h1", hypothesis="fewer retries", candidate_sha="abc",
                          verdict="rejected", evidence={})
    lines = p.read_text().splitlines()
    lines[0] = lines[0].replace("fewer retries", "more retries")
    p.write_text("\n".join(lines) + "\n")
    with pytest.raises(ValueError):
        read_history(p)
