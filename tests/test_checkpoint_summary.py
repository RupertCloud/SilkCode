"""Compaction checkpoints on a light model.

Stage-2 compaction used to drop old turns entirely - "re-read files if you
need that information." With a light model configured, the dropped turns are
summarized into one checkpoint first, written with nac's discipline: user
constraints marked active/satisfied/superseded, and unverified outcomes
labeled as reported rather than upgraded into facts.
"""

from __future__ import annotations

import json

import pytest

from silkcode.agent.loop import CHECKPOINT_MARKER, Agent
from silkcode.checkpoints import Checkpoints
from silkcode.lightmodel import (
    CHECKPOINT_OUTPUT_CHARS,
    CHECKPOINT_PROMPT,
    checkpoint_summarizer,
)
from silkcode.permissions import PermissionManager
from silkcode.providers.base import ModelProvider
from silkcode.workspace import Workspace


class _NoCallProvider(ModelProvider):
    def __init__(self):
        super().__init__("nocall")

    def chat(self, model, messages, tools=None):
        raise AssertionError("the main model must not be called by compaction")

    def list_models(self):
        return []


def _agent(ws, summarizer=None, budget=60):
    return Agent(_NoCallProvider(), "m", ws, PermissionManager("edit"),
                 checkpoints=Checkpoints(), max_context_tokens=budget,
                 summarizer=summarizer)


@pytest.fixture
def ws(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("SILKCODE_HOME", str(home))
    root = tmp_path / "repo"
    root.mkdir()
    return Workspace(root)


def _fill(agent, turns=6):
    for i in range(turns):
        agent.messages.append({"role": "user", "content": f"user request {i} " + "x" * 80})
        agent.messages.append({"role": "assistant", "content": f"assistant reply {i} " + "y" * 80})


# ---- the checkpoint mechanism ------------------------------------------------

def test_dropped_turns_become_a_checkpoint(ws):
    captured = {}

    def summarizer(transcript):
        captured["transcript"] = transcript
        return "## Work state\nreply 0 established the baseline."

    agent = _agent(ws, summarizer)
    _fill(agent)
    agent._compact()

    assert "user request 0" in captured["transcript"], \
        "the dropped turn never reached the summarizer"
    checkpoint = agent.messages[1]
    assert checkpoint["role"] == "assistant", \
        "a checkpoint must never read as the user speaking"
    assert checkpoint["content"].startswith(CHECKPOINT_MARKER)
    assert "established the baseline" in checkpoint["content"]


def test_checkpoints_fold_instead_of_stacking(ws):
    """A second compaction feeds the first checkpoint back into the input;
    two compactions must not leave two checkpoint messages."""
    inputs = []

    def summarizer(transcript):
        inputs.append(transcript)
        return f"checkpoint {len(inputs)}"

    agent = _agent(ws, summarizer)
    _fill(agent)
    agent._compact()
    _fill(agent)
    agent._compact()

    checkpoints = [m for m in agent.messages
                   if str(m.get("content", "")).startswith(CHECKPOINT_MARKER)]
    assert len(checkpoints) == 1
    assert "checkpoint 2" in checkpoints[0]["content"]
    assert "checkpoint 1" in inputs[1], "the old checkpoint was not folded in"


def test_a_failing_summarizer_never_breaks_compaction(ws):
    def summarizer(transcript):
        raise RuntimeError("light model is down")

    agent = _agent(ws, summarizer)
    _fill(agent)
    agent._compact()
    assert agent.trimmed_messages > 0, "compaction still has to trim"
    assert not any(str(m.get("content", "")).startswith(CHECKPOINT_MARKER)
                   for m in agent.messages)


def test_no_summarizer_means_exactly_the_old_behavior(ws):
    agent = _agent(ws, summarizer=None)
    _fill(agent)
    agent._compact()
    assert not any(str(m.get("content", "")).startswith(CHECKPOINT_MARKER)
                   for m in agent.messages)
    assert agent.trimmed_messages > 0


# ---- the discipline in the prompt --------------------------------------------

def test_the_prompt_carries_both_nac_rules():
    assert "active, satisfied, or superseded" in CHECKPOINT_PROMPT
    assert "reported, not verified" in CHECKPOINT_PROMPT
    assert "do not upgrade a claim into a fact" in CHECKPOINT_PROMPT
    assert "Do not continue the task" in CHECKPOINT_PROMPT


# ---- the light model ---------------------------------------------------------

def test_no_light_model_configured_means_no_summarizer(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("SILKCODE_HOME", str(home))
    (home / "config.json").write_text(json.dumps({"default_model": "deepseek"}))
    from silkcode.config import Config
    assert checkpoint_summarizer(Config.load()) is None


def test_the_light_model_gets_the_checkpoint_prompt_and_only_the_tail(
        tmp_path, stub_server, monkeypatch):
    from conftest import sse_response
    from silkcode.config import Config

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("SILKCODE_HOME", str(home))
    server = stub_server([
        sse_response(content="## Work state\nsummarized",
                     usage={"prompt_tokens": 5, "completion_tokens": 5}),
    ])
    server.thread.start()
    try:
        (home / "config.json").write_text(json.dumps({
            "default_model": "stub",
            "light_model": "cheap",
            "providers": {
                "stub": {"type": "openai_compat", "base_url": "http://127.0.0.1:1",
                          "default_model": "stub-model"},
                "cheap": {"type": "openai_compat", "base_url": server.base_url,
                           "default_model": "tiny-model"},
            },
        }))
        summarizer = checkpoint_summarizer(Config.load())
        assert summarizer is not None
        result = summarizer("early text " + "z" * 100_000)
        assert result == "## Work state\nsummarized"
    finally:
        server.httpd.shutdown()
        server.httpd.server_close()

    sent = server.requests[0]
    assert sent["model"] == "tiny-model", "the light model, not the session's"
    system = next(m["content"] for m in sent["messages"] if m["role"] == "system")
    assert system == CHECKPOINT_PROMPT
    user = next(m["content"] for m in sent["messages"] if m["role"] == "user")
    assert "early text" not in user, "the transcript must be tail-clipped"
    assert len(user) <= 24_000


def test_checkpoint_output_is_bounded(tmp_path, stub_server, monkeypatch):
    from conftest import sse_response
    from silkcode.config import Config

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("SILKCODE_HOME", str(home))
    server = stub_server([
        sse_response(content="w" * 50_000,
                     usage={"prompt_tokens": 5, "completion_tokens": 5}),
    ])
    server.thread.start()
    try:
        (home / "config.json").write_text(json.dumps({
            "default_model": "cheap",
            "light_model": "cheap",
            "providers": {"cheap": {"type": "openai_compat", "base_url": server.base_url,
                                     "default_model": "tiny"}},
        }))
        summary = checkpoint_summarizer(Config.load())("transcript")
    finally:
        server.httpd.shutdown()
        server.httpd.server_close()
    assert len(summary) == CHECKPOINT_OUTPUT_CHARS


def test_the_repl_and_gui_both_pass_a_summarizer():
    """Wiring, pinned the same way the redirect choices are: by reading the
    call sites. A summarizer built and never passed is a silent no-op."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "silkcode"
    for name in ("cli/repl.py", "gui/server.py"):
        text = (root / name).read_text()
        assert "summarizer=" in text, f"{name} builds an Agent without a summarizer"
