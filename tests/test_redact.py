"""Credential scrubbing on the way out.

Tool output is sent to whichever model provider is answering - often a third
party, whose logs the user does not control. So these check two things in
tension: that recognisable secrets are removed, and that ordinary code and
prose survive untouched, because a scrubber that mangles real output would
be turned off and then protect nothing.
"""

from __future__ import annotations

import json

import pytest

from silkcode.redact import PLACEHOLDER, findings, known_secrets, redact


# --------------------------------------------------------------------------
# what must be removed
# --------------------------------------------------------------------------

@pytest.mark.parametrize("secret", [
    "sk-EXAMPLEONLYnotarealkey0000000000",
    "sk-ant-api03-abcdefghijklmnop1234",
    "xai-abcdefghijklmnopqrstuvwx",
    "ghp_16CharactersOrMoreHere00",
    "gho_16CharactersOrMoreHere00",
    "ghs_16CharactersOrMoreHere00",
    "github_pat_11ABCDEFG0abcdefghij_ABCDEFGHIJKLMNOP",
    "glpat-abcdefghijklmnopqrst",
    "xoxb-1234567890-abcdefghij",
    "AKIAIOSFODNN7EXAMPLE",
    "ASIAIOSFODNN7EXAMPLE",
    "AIzaSyD-1234567890abcdefghijklmnopqrstuvw",
])
def test_a_credential_does_not_survive(secret):
    out = redact(f"the value is {secret} and that is all")
    assert secret not in out
    assert PLACEHOLDER in out


def test_a_jwt_is_removed():
    jwt = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
           "eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk")
    assert jwt not in redact(f"Authorization: {jwt}")


def test_a_private_key_block_is_removed_entirely():
    pem = ("-----BEGIN RSA PRIVATE KEY-----\n"
           "MIIEowIBAAKCAQEAxyz\nabcdefghijklmnop\n"
           "-----END RSA PRIVATE KEY-----")
    out = redact(f"deploy key:\n{pem}\ndone")
    assert "MIIEowIBAAKCAQEAxyz" not in out
    assert "BEGIN RSA PRIVATE KEY" not in out
    assert "done" in out


def test_credentials_in_a_url_are_removed():
    out = redact("origin https://x-access-token:ghp_16CharactersOrMore00@github.com/o/r.git")
    assert "ghp_16CharactersOrMore00" not in out
    assert "github.com/o/r.git" in out, "the useful part of the URL should survive"


def test_a_token_in_a_query_string_is_removed():
    out = redact("GET https://api.example.com/v1/things?access_token=abcdef123456&page=2")
    assert "abcdef123456" not in out
    assert "page=2" in out


def test_an_assignment_is_redacted_by_its_name():
    """Even a secret with no recognisable shape, when the name says so."""
    for line in ('API_KEY=hunter2hunter2',
                 'password: "correct horse battery"',
                 "client_secret => 'zzzzzzzzzzzz'",
                 '"access_token": "0123456789abcdef"'):
        out = redact(line)
        assert PLACEHOLDER in out, line
        for leaked in ("hunter2hunter2", "correct horse battery",
                       "zzzzzzzzzzzz", "0123456789abcdef"):
            assert leaked not in out


def test_a_bearer_header_is_removed():
    out = redact("curl -H 'Authorization: Bearer abcdefghijklmnopqrst' https://x")
    assert "abcdefghijklmnopqrst" not in out


def test_environment_dumps_are_scrubbed():
    """`printenv` is an ordinary thing for an agent to run."""
    dump = ("PATH=/usr/bin\nDEEPSEEK_API_KEY=sk-EXAMPLEONLYnotarealkey0000000000\n"
            "GITHUB_TOKEN=ghp_16CharactersOrMoreHere00\nHOME=/root\n")
    out = redact(dump)
    assert "sk-EXAMPLEONLYnotarealkey0000000000" not in out
    assert "ghp_16CharactersOrMoreHere00" not in out
    assert "PATH=/usr/bin" in out and "HOME=/root" in out


# --------------------------------------------------------------------------
# what must survive  (a scrubber that mangles real output gets turned off)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "def build(a, b):\n    return a + b\n",
    "commit 73b41f94230a2148d0b4f68dc768e5bf2f71ec80",
    "task-manager and disk-usage and risk-assessment",
    "https://github.com/RupertCloud/SilkCode/pull/16",
    "Traceback (most recent call last):\n  File 'x.py', line 3",
    "-----BEGIN CERTIFICATE-----",
    "the token is short: abc",
    "import sk_learn as sk",
    "SELECT * FROM tokens WHERE id = 4",
    "npm install && npm test",
    "password prompt appeared",
])
def test_ordinary_output_is_untouched(text):
    assert redact(text) == text


def test_a_realistic_diff_is_untouched():
    diff = ("diff --git a/app.py b/app.py\n"
            "@@ -1,3 +1,4 @@\n"
            "-def add(a, b):\n"
            "+def add(a: int, b: int) -> int:\n"
            "     return a + b\n")
    assert redact(diff) == diff


def test_empty_and_none_safe():
    assert redact("") == ""


# --------------------------------------------------------------------------
# secrets this installation already knows
# --------------------------------------------------------------------------

def test_a_known_secret_is_removed_whatever_shape_it_has(tmp_path, monkeypatch):
    """A configured key is removed by value, so it does not need to match a
    pattern - which is what covers a provider whose format we do not know."""
    odd = "totally-unrecognisable-value-9999"
    out = redact(f"the key is {odd}", extra=(odd,))
    assert odd not in out


def test_a_short_known_value_is_not_used(monkeypatch):
    """Replacing a 3-character 'secret' would gut ordinary text."""
    assert redact("a cat sat on the mat", extra=("cat",)) == "a cat sat on the mat"


def test_known_secrets_reads_config_and_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("SILKCODE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-from-the-environment-123")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_from_the_environment_123")
    from silkcode.config import Config

    config = Config.load()
    config.set_provider("custom", {"api_key": "stored-in-the-config-123"})
    config.save()

    values = known_secrets(Config.load())
    assert "sk-from-the-environment-123" in values
    assert "ghp_from_the_environment_123" in values
    assert "stored-in-the-config-123" in values


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------

def test_findings_names_the_kinds_without_printing_them():
    text = "ghp_16CharactersOrMoreHere00 and AKIAIOSFODNN7EXAMPLE"
    names = findings(text)
    assert "github-token" in names and "aws-key-id" in names
    assert "ghp_16CharactersOrMoreHere00" not in json.dumps(names)


# --------------------------------------------------------------------------
# the agent: what actually reaches the provider
# --------------------------------------------------------------------------

def test_a_secret_printed_by_a_tool_never_reaches_the_provider(tmp_path, monkeypatch):
    """The point of the whole module. `printenv` is an ordinary thing for an
    agent to run, and the conversation goes to a third party."""
    import json as _json

    from conftest import FakeProvider
    from silkcode.agent import Agent
    from silkcode.permissions import PermissionManager
    from silkcode.providers.base import ChatResult, ToolCall, Usage
    from silkcode.workspace import Workspace

    monkeypatch.setenv("SILKCODE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-EXAMPLEONLYnotarealkey0000000000")
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / ".env").write_text(
        "GITHUB_TOKEN=ghp_16CharactersOrMoreHere00\nDEBUG=1\n")

    provider = FakeProvider([
        ChatResult(tool_calls=[ToolCall(id="c1", name="read_file",
                                        arguments=_json.dumps({"path": ".env"}))],
                   usage=Usage(1, 1)),
        ChatResult(content="done", usage=Usage(1, 1)),
    ])
    agent = Agent(provider, "m", Workspace(workspace),
                  PermissionManager("agent"))
    agent.run_turn("show me the env file")

    sent = _json.dumps(provider.calls, default=str)
    assert "ghp_16CharactersOrMoreHere00" not in sent, \
        "a token from the workspace was sent to the provider"
    assert "sk-EXAMPLEONLYnotarealkey0000000000" not in sent, \
        "the user's own API key was sent to the provider"
    assert "DEBUG=1" in sent, "the rest of the file should still be readable"


def test_redaction_can_be_turned_off(tmp_path, monkeypatch):
    """It is a backstop, not a boundary, so it must be possible to see the
    real output when that is what you need."""
    import json as _json

    from conftest import FakeProvider
    from silkcode.agent import Agent
    from silkcode.permissions import PermissionManager
    from silkcode.providers.base import ChatResult, ToolCall, Usage
    from silkcode.workspace import Workspace

    monkeypatch.setenv("SILKCODE_HOME", str(tmp_path / "home"))
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / ".env").write_text("GITHUB_TOKEN=ghp_16CharactersOrMoreHere00\n")

    provider = FakeProvider([
        ChatResult(tool_calls=[ToolCall(id="c1", name="read_file",
                                        arguments=_json.dumps({"path": ".env"}))],
                   usage=Usage(1, 1)),
        ChatResult(content="done", usage=Usage(1, 1)),
    ])
    agent = Agent(provider, "m", Workspace(workspace), PermissionManager("agent"),
                  redact_output=False)
    agent.run_turn("show me the env file")
    assert "ghp_16CharactersOrMoreHere00" in _json.dumps(provider.calls, default=str)


def test_a_broken_pattern_never_loses_the_tool_result(tmp_path, monkeypatch):
    """If redaction fails the agent must still get its output - it needs it
    to make progress, and this layer is only a backstop."""
    import json as _json

    from conftest import FakeProvider
    from silkcode.agent import Agent
    from silkcode.permissions import PermissionManager
    from silkcode.providers.base import ChatResult, ToolCall, Usage
    from silkcode.workspace import Workspace

    monkeypatch.setenv("SILKCODE_HOME", str(tmp_path / "home"))
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "notes.txt").write_text("important content\n")

    monkeypatch.setattr("silkcode.redact.redact",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))

    provider = FakeProvider([
        ChatResult(tool_calls=[ToolCall(id="c1", name="read_file",
                                        arguments=_json.dumps({"path": "notes.txt"}))],
                   usage=Usage(1, 1)),
        ChatResult(content="done", usage=Usage(1, 1)),
    ])
    agent = Agent(provider, "m", Workspace(workspace), PermissionManager("agent"))
    agent.run_turn("read notes")
    assert "important content" in _json.dumps(provider.calls, default=str)
