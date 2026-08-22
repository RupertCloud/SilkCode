"""Link Silk Code to an inference server running on another machine.

A phone is a fine place to *drive* a coding agent and a poor place to *run*
the model: the laptop already has the RAM, the GPU and the weights on disk.
This module is the bridge. It finds an OpenAI-compatible or Ollama server on
the local network, checks it is actually answering, and writes it into the
config as an ordinary provider - so the REPL, the GUI and `--model` pick it
up with no further plumbing.

Three pieces:

* ``probe``     - is there a model server at this address, what kind, which
                  models, and how long does a round trip take?
* ``discover``  - sweep the current subnet for the ports those servers use,
                  then probe whatever answered.
* ``link``      - persist a probed server as a provider and put it at the
                  front of the ``auto`` router's order.

httpx is imported inside the functions on purpose: it dominates interpreter
startup and `silkcode inference host` (the laptop-side helper) never makes a
request at all.
"""

from __future__ import annotations

import ipaddress
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .config import BUILTIN_PROVIDERS, DEFAULT_AUTO_ORDER

# Ports the local-inference servers people actually run listen on. Ordered by
# how likely a given machine is to have one, because discovery reports the
# first hit per host and a laptop running both Ollama and LM Studio should be
# reported as the Ollama it can pull models into.
KNOWN_PORTS: list[tuple[int, str]] = [
    (11434, "ollama"),        # Ollama
    (1234, "lmstudio"),       # LM Studio
    (8000, "vllm"),           # vLLM / SGLang / TGI-compat
    (8080, "llamacpp"),       # llama.cpp server, Jan
    (5001, "koboldcpp"),      # KoboldCpp
    (11435, "ollama"),        # a second Ollama (common when one is in Docker)
]

DEFAULT_LINK_NAME = "laptop"

# A LAN round trip is milliseconds; anything slower is a machine that is not
# there and we would rather find that out quickly than block the sweep.
SCAN_CONNECT_TIMEOUT = 0.35
PROBE_TIMEOUT = 4.0


class InferenceError(RuntimeError):
    """Raised when a server cannot be reached or is not a model server."""


@dataclass
class Probe:
    """What we learned by asking an address whether it serves models."""

    url: str                       # root URL as given/normalised, no trailing /
    ok: bool = False
    kind: str | None = None        # "ollama" | "openai_compat"
    server: str | None = None      # friendly guess: ollama, lmstudio, vllm...
    base_url: str | None = None    # what a provider config should use
    models: list[str] = field(default_factory=list)
    latency_ms: float | None = None
    error: str | None = None

    @property
    def host(self) -> str:
        return urlsplit(self.url).hostname or self.url

    @property
    def port(self) -> int | None:
        return urlsplit(self.url).port

    def provider_config(self, token_env: str | None = None, token: str | None = None,
                        model: str | None = None, timeout: float | None = None) -> dict:
        """The config.json entry that makes this server a Silk Code provider."""
        cfg: dict = {
            "type": "ollama" if self.kind == "ollama" else "openai_compat",
            "base_url": self.base_url or self.url,
            "remote_inference": True,
        }
        chosen = model or preferred_model(self.models)
        if chosen:
            cfg["default_model"] = chosen
        if token_env:
            cfg["api_key_env"] = token_env
        if token:
            cfg["api_key"] = token
        if timeout is not None:
            cfg["timeout"] = timeout
        return cfg


def preferred_model(models: list[str]) -> str | None:
    """Pick the model most likely to be wanted for coding.

    A laptop's Ollama usually holds an embedding model or two alongside the
    chat models; handing the agent `nomic-embed-text` because it sorted first
    would fail on the very first turn.
    """
    if not models:
        return None
    usable = [m for m in models if not _is_embedding(m)] or list(models)
    for keyword in ("coder", "code", "qwen", "devstral", "deepseek", "llama"):
        for m in usable:
            if keyword in m.lower():
                return m
    return usable[0]


def _is_embedding(model: str) -> bool:
    low = model.lower()
    return "embed" in low or low.startswith(("bge-", "gte-", "e5-"))


# ---- addresses --------------------------------------------------------------

def normalize_url(spec: str, default_port: int | None = None) -> str:
    """Turn what someone types into a root URL.

    Accepts '192.168.1.20', '192.168.1.20:11434', 'laptop.local',
    'http://laptop.local:11434' and 'https://.../v1'. A bare host with no
    port gets ``default_port`` when one is supplied, so
    `silkcode inference link 192.168.1.20` means the obvious thing.
    """
    spec = spec.strip().rstrip("/")
    if not spec:
        raise InferenceError("empty address")
    if "://" not in spec:
        spec = "http://" + spec
    parts = urlsplit(spec)
    if not parts.hostname:
        raise InferenceError(f"cannot read a host out of {spec!r}")
    # Only a bare host gets the default port. Someone who typed a path
    # ('http://box/v1') told us where the server is; inserting a port there
    # would point at a different one.
    if parts.port is None and default_port is not None and not parts.path.strip("/"):
        spec = f"{parts.scheme}://{parts.hostname}:{default_port}"
    return spec.rstrip("/")


def local_ipv4() -> str | None:
    """This machine's address on the network it would route through.

    No packet is sent - a connected UDP socket only makes the kernel choose a
    source address, which is exactly the question being asked.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(0.5)
            sock.connect(("192.0.2.1", 9))  # TEST-NET-1: reserved, never routed
            return sock.getsockname()[0]
    except OSError:
        return None


def local_ipv4_addresses() -> list[str]:
    """Every IPv4 address this machine answers on, loopback excluded.

    Used by `inference host` to tell the user which address to type into the
    phone. getaddrinfo on the hostname covers the common cases without asking
    for a dependency or shelling out to `ip`/`ifconfig`.
    """
    found: list[str] = []
    primary = local_ipv4()
    if primary and not primary.startswith("127."):
        found.append(primary)
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        infos = []
    for info in infos:
        addr = info[4][0]
        if not addr.startswith("127.") and addr not in found:
            found.append(addr)
    return found


def subnet_hosts(ip: str, prefix: int = 24, limit: int = 512) -> list[str]:
    """The addresses to sweep, nearest first.

    Nearest first matters: a laptop and a phone on the same router usually get
    adjacent DHCP leases, so the answer tends to be a few addresses away and
    the sweep can report it while the far end of the range is still running.
    """
    try:
        net = ipaddress.ip_network(f"{ip}/{prefix}", strict=False)
    except ValueError as exc:
        raise InferenceError(f"not a usable address: {ip} ({exc})") from exc
    if net.num_addresses > limit:
        raise InferenceError(
            f"/{prefix} is {net.num_addresses} addresses - too many to sweep. "
            "Use a smaller range (--prefix 24) or name the host directly.")
    self_ip = ipaddress.ip_address(ip)
    others = [h for h in net.hosts() if h != self_ip]
    others.sort(key=lambda h: abs(int(h) - int(self_ip)))
    return [str(h) for h in others]


def port_open(host: str, port: int, timeout: float = SCAN_CONNECT_TIMEOUT) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False




# ---- probing ----------------------------------------------------------------

def probe(url: str, token: str | None = None, timeout: float = PROBE_TIMEOUT,
          client=None) -> Probe:
    """Ask an address what it is. Never raises - the answer is in ``Probe.ok``.

    Three shapes are tried in order, which also covers a URL pasted with the
    ``/v1`` already on it:

      {url}/api/tags   -> Ollama's native listing
      {url}/v1/models  -> an OpenAI-compatible server rooted at {url}
      {url}/models     -> the same, when {url} already ends in /v1
    """
    import httpx

    root = normalize_url(url)
    result = Probe(url=root)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    owned = client is None
    client = client or httpx.Client(timeout=timeout, follow_redirects=True)
    try:
        for path, kind, base in (
            ("/api/tags", "ollama", root),
            ("/v1/models", "openai_compat", f"{root}/v1"),
            ("/models", "openai_compat", root),
        ):
            # Timed per request, not across the loop: an OpenAI-compatible
            # server answers on the second or third try, and charging it for
            # the 404s before it would report double its real latency - which
            # is the number `inference ping` exists to show.
            started = time.monotonic()
            try:
                resp = client.get(root + path, headers=headers, timeout=timeout)
            except httpx.HTTPError as exc:
                result.error = _readable_error(exc, root)
                return result
            if resp.status_code == 401 or resp.status_code == 403:
                result.error = (f"HTTP {resp.status_code}: the server wants credentials. "
                                "Pass --token (or --token-env) when linking.")
                return result
            if resp.status_code >= 400:
                continue
            models = _models_from(resp, kind)
            if models is None:
                continue  # answered, but not with a model listing
            result.ok = True
            result.kind = kind
            result.base_url = base
            result.models = models
            result.latency_ms = round((time.monotonic() - started) * 1000, 1)
            result.server = _server_name(kind, urlsplit(root).port)
            return result
        result.error = "reachable, but nothing there answers like a model server"
        return result
    finally:
        if owned:
            client.close()


def _models_from(resp, kind: str) -> list[str] | None:
    """The model names in a listing response, or None if it is not one."""
    try:
        data = resp.json()
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    if kind == "ollama":
        entries = data.get("models")
        if not isinstance(entries, list):
            return None
        return sorted(m["name"] for m in entries
                      if isinstance(m, dict) and m.get("name"))
    entries = data.get("data")
    if not isinstance(entries, list):
        return None
    return sorted(m["id"] for m in entries if isinstance(m, dict) and m.get("id"))


def _server_name(kind: str, port: int | None) -> str:
    if kind == "ollama":
        return "ollama"
    for known_port, name in KNOWN_PORTS:
        if known_port == port:
            return name
    return "openai-compatible"


def _readable_error(exc: Exception, root: str) -> str:
    """Turn an httpx failure into something that suggests the fix.

    These are the two failures a phone actually hits, and the difference
    matters: a refused connection means the server is bound to loopback on the
    laptop, a timeout usually means the laptop's firewall is dropping the
    packets. Saying so beats printing the exception class.
    """
    import httpx

    if isinstance(exc, httpx.ConnectError):
        return (f"connection refused by {urlsplit(root).hostname} - the server is running "
                "but is not listening on the network (see: silkcode inference host)")
    if isinstance(exc, httpx.TimeoutException):
        return (f"no answer from {urlsplit(root).hostname} - wrong address, a different "
                "network, or a firewall dropping the connection")
    return f"{type(exc).__name__}: {exc}"


def measure_chat(cfg: dict, model: str, api_key: str | None = None,
                 prompt: str = "Reply with the single word: pong") -> tuple[str, float]:
    """A real round trip through the provider stack: (reply, milliseconds).

    Listing models proves the server is up; it does not prove it can load
    weights and generate. On a laptop the first token can be tens of seconds
    behind the listing while the model is paged in, and that is exactly the
    surprise worth finding before starting a session, not during one.
    """
    from .providers import ProviderError, build_provider

    provider = build_provider("ping", cfg, api_key=api_key)
    started = time.monotonic()
    try:
        result = provider.chat(model, [{"role": "user", "content": prompt}])
    except ProviderError as exc:
        raise InferenceError(str(exc)) from exc
    return result.content.strip(), round((time.monotonic() - started) * 1000, 1)


# ---- discovery --------------------------------------------------------------

def discover(hosts: list[str] | None = None, ports: list[int] | None = None,
             prefix: int = 24, connect_timeout: float = SCAN_CONNECT_TIMEOUT,
             probe_timeout: float = PROBE_TIMEOUT, workers: int = 64,
             token: str | None = None, on_progress=None) -> list[Probe]:
    """Sweep the network for model servers and probe whatever answered.

    Two passes on purpose. A TCP connect is cheap enough to try against every
    address in the subnet; an HTTP probe is not, so only the handful of open
    ports get one.
    """
    ports = ports or [port for port, _ in KNOWN_PORTS]
    if hosts is None:
        own = local_ipv4()
        if not own:
            raise InferenceError(
                "this machine has no network address to sweep from - connect to "
                "Wi-Fi, or name the laptop directly: silkcode inference link <host>")
        hosts = subnet_hosts(own, prefix=prefix)
    targets = [(host, port) for host in hosts for port in ports]

    open_ports: list[tuple[str, int]] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for (host, port), is_open in zip(
                targets, pool.map(lambda t: port_open(*t, timeout=connect_timeout), targets)):
            if is_open:
                open_ports.append((host, port))
                if on_progress:
                    on_progress(host, port)

    found: list[Probe] = []
    seen_hosts: set[str] = set()
    with ThreadPoolExecutor(max_workers=min(16, max(1, len(open_ports) or 1))) as pool:
        results = pool.map(
            lambda t: probe(f"http://{t[0]}:{t[1]}", token=token, timeout=probe_timeout),
            open_ports)
        for result in results:
            if result.ok and result.host not in seen_hosts:
                seen_hosts.add(result.host)
                found.append(result)
    found.sort(key=lambda p: (p.latency_ms if p.latency_ms is not None else 1e9))
    return found


# ---- persistence ------------------------------------------------------------

def link(config, name: str, result: Probe, model: str | None = None,
         token: str | None = None, token_env: str | None = None,
         timeout: float | None = None, make_default: bool = True) -> dict:
    """Save a probed server as a provider and prefer it from then on.

    The provider is also pushed to the front of the ``auto`` router's order, so
    `--model auto` reaches for the laptop first and falls through to the cloud
    providers by itself when the laptop is asleep or on another network.
    """
    cfg = result.provider_config(token_env=token_env, token=token, model=model,
                                 timeout=timeout)
    config.set_provider(name, cfg)
    order = list(config.data.get("auto_order") or DEFAULT_AUTO_ORDER)
    config.data["auto_order"] = [name] + [n for n in order if n != name]
    if make_default and cfg.get("default_model"):
        config.data["default_model"] = f"{name}/{cfg['default_model']}"
    config.save()
    return config.providers[name]


def unlink(config, name: str) -> bool:
    """Remove a linked provider. False if there was nothing by that name."""
    providers = config.data.get("providers") or {}
    if name not in providers:
        return False
    providers.pop(name)
    config.data["auto_order"] = [n for n in (config.data.get("auto_order") or []) if n != name]
    default = config.data.get("default_model") or ""
    if default == name or default.startswith(f"{name}/"):
        config.data.pop("default_model", None)
    config.providers.pop(name, None)
    if name in BUILTIN_PROVIDERS:
        # Linking over a built-in name (`--name ollama`) only ever overrode it.
        # Removing the override puts the built-in back, rather than leaving this
        # Config with a provider the next `Config.load()` would still have.
        config.providers[name] = dict(BUILTIN_PROVIDERS[name])
    config.save()
    return True


def linked_providers(config) -> dict[str, dict]:
    """Providers that were added by `inference link`, newest config wins."""
    return {name: cfg for name, cfg in config.providers.items()
            if cfg.get("remote_inference")}
