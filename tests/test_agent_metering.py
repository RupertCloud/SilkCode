"""The two hooks a metered host needs: a spend gate and a per-call usage event.

Both exist so a platform can run this agent for someone else's money. The
mechanism lives here; the policy - who is over their cap - belongs to whoever
supplies the check.
"""

import json

from conftest import FakeProvider

from silkcode.agent import Agent
from silkcode.checkpoints import Checkpoints
from silkcode.permissions import PermissionManager
from silkcode.providers.base import ChatResult, ToolCall, Usage
from silkcode.workspace import Workspace


def tc(name, **args):
    return ToolCall(id=f"call_{name}", name=name, arguments=json.dumps(args))


def make_agent(tmp_path, results, events=None, before_model_call=None):
    return Agent(
        FakeProvider(results),
        "fake-model",
        Workspace(tmp_path),
        PermissionManager("edit", asker=lambda p: "no"),
        checkpoints=Checkpoints(),
        on_event=(lambda kind, data: events.append((kind, data))) if events is not None else None,
        before_model_call=before_model_call,
    )


# ---- the spend gate ----------------------------------------------------------

def test_no_check_means_no_gate(tmp_path):
    """The CLI and the local GUI pass nothing and must run unmetered."""
    agent = make_agent(tmp_path, [ChatResult(content="done", usage=Usage(1, 1))])
    assert agent.run_turn("hi") == "done"


def test_a_check_that_returns_a_reason_stops_the_turn_before_any_model_call(tmp_path):
    calls = []
    agent = make_agent(
        tmp_path,
        [ChatResult(content="should never be reached", usage=Usage(1, 1))],
        before_model_call=lambda: (calls.append(1), "Stopped: monthly cap reached.")[1],
    )
    assert agent.run_turn("hi") == "Stopped: monthly cap reached."
    assert len(calls) == 1
    # the model was never asked, so nothing was spent
    assert agent.usage.total_tokens == 0


def test_the_gate_is_re_checked_before_every_call_not_only_the_first(tmp_path):
    """A turn can make many calls; a cap reached mid-turn must still bite."""
    budget = {"left": 2}

    def check():
        if budget["left"] <= 0:
            return "Stopped: budget exhausted."
        budget["left"] -= 1
        return None

    agent = make_agent(tmp_path, [
        ChatResult(tool_calls=[tc("read_file", path="a.txt")], usage=Usage(1, 1)),
        ChatResult(tool_calls=[tc("read_file", path="a.txt")], usage=Usage(1, 1)),
        ChatResult(content="never reached", usage=Usage(1, 1)),
    ], before_model_call=check)
    (tmp_path / "a.txt").write_text("x")

    assert agent.run_turn("read it twice") == "Stopped: budget exhausted."


def test_stopping_leaves_a_history_with_no_stranded_tool_calls(tmp_path):
    """The point of gating before the call rather than after it.

    Stopping once the model has answered would leave tool_calls with no
    results, which the next request rejects.
    """
    budget = {"left": 1}

    def check():
        if budget["left"] <= 0:
            return "Stopped: budget exhausted."
        budget["left"] -= 1
        return None

    (tmp_path / "a.txt").write_text("x")
    agent = make_agent(tmp_path, [
        ChatResult(tool_calls=[tc("read_file", path="a.txt")], usage=Usage(1, 1)),
        ChatResult(content="never reached", usage=Usage(1, 1)),
    ], before_model_call=check)
    agent.run_turn("read it")

    for i, message in enumerate(agent.messages):
        if message.get("role") == "assistant" and message.get("tool_calls"):
            answered = {m.get("tool_call_id") for m in agent.messages[i + 1:]
                        if m.get("role") == "tool"}
            for call in message["tool_calls"]:
                assert call["id"] in answered, "a tool call was left without a result"


def test_a_check_that_raises_stops_rather_than_letting_the_turn_spend(tmp_path):
    def check():
        raise RuntimeError("ledger unreachable")

    agent = make_agent(tmp_path, [ChatResult(content="never reached", usage=Usage(1, 1))],
                       before_model_call=check)
    reply = agent.run_turn("hi")
    assert "spend check failed" in reply
    assert "ledger unreachable" in reply
    assert agent.usage.total_tokens == 0


def test_stopping_emits_a_stopped_event_with_the_reason(tmp_path):
    events = []
    agent = make_agent(tmp_path, [ChatResult(content="x", usage=Usage(1, 1))],
                       events=events, before_model_call=lambda: "Stopped: cap reached.")
    agent.run_turn("hi")
    stopped = [d for k, d in events if k == "stopped"]
    assert stopped == [{"reason": "Stopped: cap reached."}]


# ---- the usage event ---------------------------------------------------------

def test_one_usage_event_per_model_call_not_per_turn(tmp_path):
    """A turn can make several calls, and only per-call figures reconcile
    against a provider's own record."""
    (tmp_path / "a.txt").write_text("x")
    events = []
    agent = make_agent(tmp_path, [
        ChatResult(tool_calls=[tc("read_file", path="a.txt")], usage=Usage(10, 5)),
        ChatResult(content="done", usage=Usage(20, 7)),
    ], events=events)
    agent.run_turn("read it")

    usage_events = [d for k, d in events if k == "usage"]
    assert len(usage_events) == 2
    assert [(u["prompt_tokens"], u["completion_tokens"]) for u in usage_events] == [(10, 5), (20, 7)]
    # and the accumulated total still matches the sum of the events
    assert agent.usage.prompt_tokens == 30
    assert agent.usage.completion_tokens == 12


def test_the_usage_event_identifies_the_call_and_separates_cache_tokens(tmp_path):
    events = []
    agent = make_agent(tmp_path, [
        ChatResult(content="done",
                   usage=Usage(prompt_tokens=10, completion_tokens=5,
                               cache_write_tokens=100, cache_read_tokens=900)),
    ], events=events)
    agent.session_id = 7
    agent.run_turn("hi")

    usage = [d for k, d in events if k == "usage"][0]
    assert usage["model"] == "fake-model"
    assert usage["provider"] == agent.provider.name
    assert usage["session_id"] == 7
    assert usage["prompt_tokens"] == 10
    assert usage["cache_write_tokens"] == 100
    assert usage["cache_read_tokens"] == 900
    # cost is absent on purpose: prices belong to whoever is billing
    assert "cost" not in usage


def test_a_call_reporting_no_usage_emits_nothing(tmp_path):
    events = []
    agent = make_agent(tmp_path, [ChatResult(content="done", usage=None)], events=events)
    agent.run_turn("hi")
    assert [k for k, _ in events if k == "usage"] == []
