"""Driving Silk Code from a phone while the model runs on a laptop.

The failure this feature exists to prevent is a quiet one: a phone that
"has a model configured" and only finds out mid-session that the laptop is
asleep, bound to loopback, or holding nothing but an embedding model. So the
tests below care less about happy-path plumbing than about what each failure
tells the user.
"""

from __future__ import annotations

import contextlib
import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from silkcode.cli.main import main
from silkcode.config import Config
from silkcode.inference import (InferenceError, discover, link, linked_providers,
                                measure_chat, normalize_url, port_open,
                                preferred_model, probe, subnet_hosts, unlink)


class StubServer:
    """A local server that answers like Ollama, like an OpenAI-compatible
    endpoint, or like neither - whichever the test needs."""

    def __init__(self, flavour: str = "ollama", models: list[str] | None = None,
                 status: int = 200, reply: str = "pong"):
        models = models if models is not None else ["qwen2.5-coder:7b", "llama3:8b"]
        server = self
        self.paths: list[str] = []
        self.flavour = flavour
        self.models = models
        self.status = status
        self.reply = reply

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, payload):
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                server.paths.append(self.path)
                if server.status != 200:
                    return self._send(server.status, {"error": "nope"})
                if self.path == "/api/tags" and server.flavour == "ollama":
                    return self._send(200, {"models": [{"name": m} for m in server.models]})
                if self.path == "/v1/models" and server.flavour == "openai":
                    return self._send(200, {"data": [{"id": m} for m in server.models]})
                if self.path == "/" and server.flavour == "not-a-model-server":
                    return self._send(200, {"hello": "i am a printer"})
                self._send(404, {"error": "not found"})

            def do_POST(self):
                server.paths.append(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                self._send(200, {"choices": [{"message": {"content": server.reply},
                                              "finish_reason": "stop"}]})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def port(self) -> int:
        return self.httpd.server_address[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server():
    made: list[StubServer] = []

    def make(**kwargs):
        stub = StubServer(**kwargs)
        made.append(stub)
        return stub

    yield make
    for stub in made:
        stub.close()


def dead_url() -> str:
    """A port nothing is listening on: bind one, then let it go."""
    import socket
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}"


def run(argv) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        code = main(argv)
    return code, out.getvalue()


# ---- addresses --------------------------------------------------------------

def test_a_bare_host_gets_the_ollama_port():
    """Someone reading an IP off their laptop screen types the IP, not a URL."""
    assert normalize_url("192.168.1.20", default_port=11434) == "http://192.168.1.20:11434"
    assert normalize_url("laptop.local", default_port=11434) == "http://laptop.local:11434"


def test_an_address_that_says_more_is_left_alone():
    assert normalize_url("192.168.1.20:1234", default_port=11434) == "http://192.168.1.20:1234"
    assert normalize_url("https://box/v1", default_port=11434) == "https://box/v1"
    assert normalize_url("http://box:8000/v1/") == "http://box:8000/v1"


def test_an_address_with_no_host_is_refused():
    with pytest.raises(InferenceError):
        normalize_url("://")


def test_the_sweep_starts_with_the_neighbours():
    """A phone and a laptop on one router get adjacent DHCP leases far more
    often than not, so the answer is usually a few addresses away."""
    hosts = subnet_hosts("192.168.1.20", prefix=24)
    assert "192.168.1.20" not in hosts          # never probe ourselves
    assert len(hosts) == 253
    assert hosts[:4] == ["192.168.1.19", "192.168.1.21", "192.168.1.18", "192.168.1.22"]


def test_a_sweep_too_big_to_finish_is_refused_not_attempted():
    with pytest.raises(InferenceError, match="too many"):
        subnet_hosts("10.0.0.5", prefix=16)


# ---- what the model list means ----------------------------------------------

def test_an_embedding_model_is_never_offered_for_coding():
    """Ollama installs usually hold an embedding model, and it sorts first.
    Handing it to the agent fails on the very first turn."""
    assert preferred_model(["nomic-embed-text:latest", "qwen2.5-coder:7b"]) == "qwen2.5-coder:7b"
    assert preferred_model(["bge-m3", "llama3:8b"]) == "llama3:8b"


def test_a_coder_model_wins_over_a_general_one():
    assert preferred_model(["llama3:8b", "qwen2.5-coder:7b"]) == "qwen2.5-coder:7b"


def test_an_all_embedding_server_still_offers_something():
    """Better a model that may not work than a None the caller has to handle."""
    assert preferred_model(["nomic-embed-text"]) == "nomic-embed-text"
    assert preferred_model([]) is None


# ---- probing ----------------------------------------------------------------

def test_an_ollama_server_is_recognised(server):
    stub = server(flavour="ollama")
    result = probe(stub.url)
    assert result.ok
    assert result.kind == "ollama"
    assert result.server == "ollama"
    assert result.base_url == stub.url          # OllamaProvider appends /v1 itself
    assert result.models == ["llama3:8b", "qwen2.5-coder:7b"]
    assert result.latency_ms is not None


def test_an_openai_compatible_server_is_recognised(server):
    stub = server(flavour="openai", models=["local-model"])
    result = probe(stub.url)
    assert result.ok
    assert result.kind == "openai_compat"
    assert result.base_url == f"{stub.url}/v1"
    assert result.models == ["local-model"]


def test_a_url_that_already_ends_in_v1_is_not_doubled(server):
    """People paste the URL LM Studio shows them, which includes /v1."""
    stub = server(flavour="openai", models=["local-model"])
    result = probe(f"{stub.url}/v1")
    assert result.ok
    assert result.base_url == f"{stub.url}/v1"      # not .../v1/v1
    assert result.models == ["local-model"]


def test_a_server_that_is_not_a_model_server_says_so(server):
    stub = server(flavour="not-a-model-server")
    result = probe(stub.url)
    assert not result.ok
    assert "model server" in result.error


def test_a_closed_port_blames_loopback_binding_not_the_network():
    """This is the single most common failure: the laptop is awake, Ollama is
    running, and it is listening on 127.0.0.1 only. The message has to point
    at that, because 'connection refused' sends people to their router."""
    result = probe(dead_url(), timeout=1.0)
    assert not result.ok
    assert "not listening on the network" in result.error
    assert "silkcode inference host" in result.error


def test_a_server_wanting_credentials_asks_for_a_token(server):
    stub = server(status=401)
    result = probe(stub.url)
    assert not result.ok
    assert "--token" in result.error


def test_probe_never_raises_on_a_nonsense_host():
    result = probe("http://no-such-host.invalid:11434", timeout=1.0)
    assert not result.ok and result.error


# ---- a real generation round trip -------------------------------------------

def test_a_round_trip_measures_the_model_not_just_the_server(server):
    stub = server(flavour="openai", reply="pong")
    reply, elapsed = measure_chat(
        {"type": "openai_compat", "base_url": f"{stub.url}/v1"}, "local-model")
    assert reply == "pong"
    assert elapsed >= 0
    assert "/v1/chat/completions" in stub.paths


def test_a_failed_round_trip_surfaces_as_an_inference_error():
    with pytest.raises(InferenceError):
        measure_chat({"type": "openai_compat", "base_url": f"{dead_url()}/v1",
                      "retries": 0}, "m")


# ---- discovery --------------------------------------------------------------

def test_discovery_finds_a_server_on_a_named_host(server):
    stub = server(flavour="ollama")
    found = discover(hosts=["127.0.0.1"], ports=[stub.port], connect_timeout=1.0)
    assert [r.url for r in found] == [stub.url]
    assert found[0].models


def test_discovery_reports_one_entry_per_machine(server):
    """A laptop running both Ollama and LM Studio is still one laptop; two
    lines for the same address is noise the user has to disambiguate."""
    first = server(flavour="ollama")
    second = server(flavour="openai")
    found = discover(hosts=["127.0.0.1"], ports=[first.port, second.port],
                     connect_timeout=1.0)
    assert len(found) == 1


def test_discovery_on_an_empty_network_finds_nothing():
    import socket
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        free = sock.getsockname()[1]
    assert discover(hosts=["127.0.0.1"], ports=[free], connect_timeout=0.5) == []


def test_port_open_agrees_with_reality(server):
    stub = server()
    assert port_open("127.0.0.1", stub.port, timeout=1.0)
    stub.close()
    assert not port_open("127.0.0.1", stub.port, timeout=0.5)


# ---- linking ----------------------------------------------------------------

def test_linking_makes_the_laptop_the_default_model(tmp_path, server):
    stub = server(flavour="ollama")
    config = Config({}, path=tmp_path / "config.json")
    link(config, "laptop", probe(stub.url))

    reloaded = Config.load(tmp_path / "config.json")
    name, cfg, model = reloaded.resolve_model()
    assert (name, model) == ("laptop", "qwen2.5-coder:7b")
    assert cfg["base_url"] == stub.url
    assert cfg["type"] == "ollama"
    assert reloaded.data["auto_order"][0] == "laptop"


def test_linking_leaves_the_default_alone_when_asked(tmp_path, server):
    stub = server(flavour="ollama")
    config = Config({"default_model": "deepseek"}, path=tmp_path / "config.json")
    link(config, "laptop", probe(stub.url), make_default=False)
    assert Config.load(tmp_path / "config.json").default_model == "deepseek"


def test_a_linked_server_is_marked_as_one(tmp_path, server):
    stub = server(flavour="openai")
    config = Config({}, path=tmp_path / "config.json")
    link(config, "laptop", probe(stub.url))
    assert set(linked_providers(config)) == {"laptop"}
    assert "ollama" not in linked_providers(config)   # a builtin is not a link


def test_relinking_does_not_stack_up_auto_order_entries(tmp_path, server):
    stub = server(flavour="ollama")
    config = Config({}, path=tmp_path / "config.json")
    for _ in range(3):
        link(config, "laptop", probe(stub.url))
    assert config.data["auto_order"].count("laptop") == 1


def test_unlinking_puts_the_default_model_back(tmp_path, server):
    stub = server(flavour="ollama")
    config = Config({}, path=tmp_path / "config.json")
    link(config, "laptop", probe(stub.url))
    assert unlink(config, "laptop")

    reloaded = Config.load(tmp_path / "config.json")
    assert "laptop" not in (reloaded.data.get("providers") or {})
    assert "laptop" not in reloaded.data.get("auto_order", [])
    assert reloaded.default_model == "deepseek"       # the built-in fallback


def test_unlinking_something_that_was_never_linked_is_not_an_error(tmp_path):
    config = Config({}, path=tmp_path / "config.json")
    assert unlink(config, "laptop") is False


# ---- the auto router and an absent laptop -----------------------------------

def test_auto_skips_a_linked_server_that_is_not_answering(tmp_path, server):
    """The laptop is asleep. `auto` has to notice and move on, or every
    session started away from home dies on its first turn."""
    stub = server(flavour="ollama")
    config = Config({}, path=tmp_path / "config.json")
    link(config, "laptop", probe(stub.url))
    stub.close()

    config = Config.load(tmp_path / "config.json")
    config.data["providers"]["cloud"] = {
        "type": "openai_compat", "base_url": "https://cloud.example/v1",
        "api_key": "k", "default_model": "cloud-model",
    }
    config = Config(config.data, path=config.path)
    config.data["auto_order"] = ["laptop", "cloud"]
    name, _cfg, model = config.resolve_model("auto")
    assert (name, model) == ("cloud", "cloud-model")


def test_auto_does_not_trust_a_token_as_proof_a_laptop_is_awake(tmp_path):
    """A credential says nothing about reachability. Before this was fixed a
    linked, token-protected server was picked without ever being contacted."""
    config = Config({
        "auto_order": ["laptop", "cloud"],
        "providers": {
            "laptop": {"type": "openai_compat", "base_url": f"{dead_url()}/v1",
                       "api_key": "secret", "default_model": "local-model",
                       "remote_inference": True, "probe_timeout": 0.5},
            "cloud": {"type": "openai_compat", "base_url": "https://cloud.example/v1",
                      "api_key": "k", "default_model": "cloud-model"},
        },
    }, path=tmp_path / "config.json")
    name, _cfg, _model = config.resolve_model("auto")
    assert name == "cloud"


# ---- the command line -------------------------------------------------------

@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("SILKCODE_HOME", str(tmp_path))
    return tmp_path


def test_link_then_status_then_unlink(home, server):
    stub = server(flavour="ollama")

    code, out = run(["inference", "link", stub.url])
    assert code == 0, out
    assert "Linked 'laptop'" in out
    assert "qwen2.5-coder:7b" in out

    code, out = run(["inference"])
    assert code == 0, out
    assert "laptop" in out and "up (" in out

    code, out = run(["inference", "unlink"])
    assert code == 0, out
    assert "Unlinked 'laptop'" in out

    code, out = run(["inference"])
    assert "No inference server linked" in out


def test_linking_something_unreachable_fails_loudly(home):
    code, out = run(["inference", "link", dead_url()])
    assert code == 1
    assert "silkcode inference host" in out
    assert not (home / "config.json").exists(), "a failed link must not be saved"


def test_a_laptop_that_is_merely_asleep_can_be_linked_anyway(home):
    code, out = run(["inference", "link", dead_url(), "--force", "--model", "qwen2.5-coder"])
    assert code == 0, out
    assert "did not answer" in out
    assert Config.load(home / "config.json").resolve_model()[0] == "laptop"


def test_status_reports_a_link_that_has_gone_away(home, server):
    stub = server(flavour="ollama")
    assert run(["inference", "link", stub.url])[0] == 0
    stub.close()
    code, out = run(["inference"])
    assert code == 1, "a status command whose server is down exits non-zero"
    assert "unreachable" in out
    assert "not listening on the network" in out


def test_ping_measures_the_server_and_then_the_model(home, server):
    stub = server(flavour="openai", models=["local-model"], reply="pong")
    assert run(["inference", "link", stub.url])[0] == 0

    code, out = run(["inference", "ping", "--count", "2"])
    assert code == 0, out
    assert out.count("reply in") == 2
    assert "2/2 answered" in out

    code, out = run(["inference", "ping", "--chat"])
    assert code == 0, out
    assert "pong" in out


def test_ping_can_take_an_address_with_nothing_linked(home, server):
    stub = server(flavour="ollama")
    code, out = run(["inference", "ping", stub.url, "--count", "1"])
    assert code == 0, out
    assert "1/1 answered" in out


def test_ping_with_nothing_to_ping_says_what_to_do(home):
    code, out = run(["inference", "ping"])
    assert code == 1
    assert "silkcode inference link" in out


def test_ping_a_dead_server_explains_loopback_binding(home):
    code, out = run(["inference", "ping", dead_url(), "--count", "1"])
    assert code == 1
    assert "silkcode inference host" in out


def test_discover_finds_the_stub_and_offers_the_link_command(home, server):
    stub = server(flavour="ollama")
    code, out = run(["inference", "discover", "--host", "127.0.0.1",
                     "--port", str(stub.port)])
    assert code == 0, out
    assert stub.url in out
    assert f"silkcode inference link {stub.url}" in out


def test_discover_finding_nothing_points_at_the_host_command(home):
    code, out = run(["inference", "discover", "--host", "127.0.0.1", "--port", "9"])
    assert code == 1
    assert "silkcode inference host" in out


def test_host_names_the_command_that_opens_a_loopback_ollama(home, server):
    """`inference host` runs on the laptop; its whole job is turning
    'connection refused' on the phone into one command to paste."""
    stub = server(flavour="ollama")
    code, out = run(["inference", "host", "--port", str(stub.port)])
    assert code == 0, out
    assert "reachable at" in out or "no network address" in out


def test_an_unknown_subcommand_lists_the_real_ones(home):
    code, out = run(["inference", "frobnicate"])
    assert code == 1
    assert "discover" in out and "link" in out


# ---- the cloud providers are still right there ------------------------------

def test_linking_a_laptop_leaves_the_direct_cloud_providers_alone(home, server, monkeypatch):
    """Silk Code talks straight to DeepSeek and Kimi, and linking a laptop is an
    addition, not a replacement: the cloud is what answers when the laptop is
    asleep, and `--model deepseek` still goes direct."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    stub = server(flavour="ollama")
    code, out = run(["inference", "link", stub.url])
    assert code == 0, out
    assert "Direct to a cloud provider" in out
    assert "silkcode --model deepseek" in out

    config = Config.load(home / "config.json")
    name, cfg, model = config.resolve_model("deepseek")
    assert (name, model) == ("deepseek", "deepseek-chat")
    assert cfg["base_url"].startswith("https://api.deepseek.com")
    assert "deepseek" in config.data["auto_order"]
    assert "kimi" in config.data["auto_order"]


def test_status_says_which_cloud_providers_could_take_over(home, server, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.delenv("MOONSHOT_API_KEY", raising=False)
    stub = server(flavour="ollama")
    assert run(["inference", "link", stub.url])[0] == 0

    code, out = run(["inference"])
    assert code == 0, out
    assert "Direct to a cloud provider: deepseek" in out
    assert "not set up:" in out and "kimi" in out


def test_a_provider_needing_onboarding_is_not_advertised_as_ready(home, monkeypatch):
    """Cloudflare's URL has an {account_id} hole in it until you fill it."""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "t")
    code, out = run(["inference"])
    assert code == 0
    assert "No cloud provider is set up yet" in out
    assert "not set up:" in out and "cloudflare" in out
    assert "available at any time" not in out


def test_a_token_in_the_config_file_is_flagged(home, server):
    stub = server(flavour="ollama")
    code, out = run(["inference", "link", stub.url, "--token", "s3cret"])
    assert code == 0, out
    assert "prefer --token-env" in out
    assert Config.load(home / "config.json").providers["laptop"]["api_key"] == "s3cret"


def test_a_token_from_the_environment_is_referenced_not_copied(home, server, monkeypatch):
    """The secret itself must never land in config.json - only its variable name."""
    monkeypatch.setenv("LAPTOP_TOKEN", "s3cret")
    stub = server(flavour="ollama")
    assert run(["inference", "link", stub.url, "--token-env", "LAPTOP_TOKEN"])[0] == 0

    saved = (home / "config.json").read_text()
    assert "LAPTOP_TOKEN" in saved
    assert "s3cret" not in saved
    assert Config.load(home / "config.json").api_key_for(
        Config.load(home / "config.json").providers["laptop"]) == "s3cret"


def test_unlinking_a_name_that_shadowed_a_builtin_restores_it(tmp_path, server):
    """`link --name ollama` overrides the built-in provider rather than
    replacing it; unlinking has to leave the built-in standing."""
    stub = server(flavour="ollama")
    config = Config({}, path=tmp_path / "config.json")
    link(config, "ollama", probe(stub.url))
    assert config.providers["ollama"]["base_url"] == stub.url

    assert unlink(config, "ollama")
    assert config.providers["ollama"]["base_url"] == "http://localhost:11434"
    assert Config.load(tmp_path / "config.json").providers["ollama"]["base_url"] \
        == "http://localhost:11434"


def test_latency_is_the_servers_answer_not_the_probes_that_missed(server):
    """An OpenAI-compatible server answers on the second URL tried. Timing from
    the start of the search would bill it for the 404 that came first, and that
    number is the whole point of `inference ping`."""
    ollama = server(flavour="ollama")
    openai = server(flavour="openai")
    direct = probe(ollama.url).latency_ms          # answers on the first try
    searched = probe(openai.url).latency_ms        # answers on the second

    assert direct is not None and searched is not None
    # Both are one loopback request; the search must not make the second look
    # like two. A generous bound keeps this from turning into a timing flake.
    assert searched < direct + 25, (direct, searched)
