# Hosting Silk Code for several people — slice 1

One container per user, a proxy in front, and no authentication yet. This is
the first of three slices towards the hosted product in
[docs/CLOUD.md](../docs/CLOUD.md) §M2, and it exists to prove the shape before
sign-in is built.

```
browser ──▶ proxy.py ──▶ 127.0.0.1:PORT ──▶ container: silkcode gui
            (routes by      (published        SILKCODE_HOME=/data/.silkcode
             cookie,         loopback-only)   their key, their repos
             injects the
             access token)
```

## Read this before you expose anything

The agent runs shell commands — that is the product. So a Silk Code daemon
anyone can reach is **remote code execution for whoever reaches it**. Two
consequences shape everything here:

- **One container per user, never a shared process.** Otherwise the first user
  to sign up can read every other user's API keys. The container is the tenant
  boundary (CLOUD.md §2).
- **`/_login` in `proxy.py` believes whatever name you hand it.** There is no
  authentication in this slice. The proxy therefore refuses to bind to
  anything but loopback, and you reach it over an SSH tunnel.

Plain Docker also shares the host kernel. For an invited group that is a
defensible risk; for public signup it is not, and that is where the isolation
work in CLOUD.md §7 stops being optional.

## Requirements

A Linux host with Docker and Python 3.10+. No other dependencies — the proxy is
standard library only.

## Run it

```bash
# 1. Build the runner image (from the repository root)
deploy/silkrun build

# 2. Give someone a workspace
deploy/silkrun start alice
deploy/silkrun key alice deepseek sk-...      # or: SILK_API_KEY=... silkrun key alice deepseek

# 3. Start the proxy (loopback only)
python3 deploy/proxy.py --port 8080
```

From your laptop:

```bash
ssh -L 8080:127.0.0.1:8080 you@your-instance
```

Then open <http://127.0.0.1:8080>, pick the user, and the GUI loads.

```bash
deploy/silkrun list            # who is running, on which port
deploy/silkrun logs alice      # that user's daemon output
deploy/silkrun stop alice      # remove the container (the volume survives)
```

## How it is put together

**The user never holds the access token.** Each container gets a 192-bit token;
`silkrun` records it in `$SILK_STATE/<user>.json` (mode 0600) and the proxy adds
`Authorization: Bearer …` on the way through.

That takes one extra step to be true. The daemon hands its own token back as
`Set-Cookie: silk_token=…` on `GET /`, so that a browser which opened
`/?token=…` keeps it for the fetches that follow. Relayed as-is, that would put
the container's shell credential in the browser — the one thing this
arrangement exists to prevent — so the proxy strips that cookie from every
response. Nothing is lost: each request is authenticated at the proxy from
state the browser never sees, which is why the API still answers 200 with only
the routing cookie present.

**The original `Host` header is forwarded unchanged.** The daemon refuses any
request whose `Origin` disagrees with the `Host` it was reached on
(`gui/server.py`, `_same_origin`) — that is what stops another website starting
an agent turn on your behalf. Rewriting `Host` to `127.0.0.1:PORT` would make
every browser request look cross-site and break the GUI entirely, so the proxy
leaves it alone and the protection survives end to end.

**Responses without `Content-Length` are streamed, not buffered.** `/api/events`
is Server-Sent Events; the proxy relays it with `read1()` and flushes each
chunk. Buffering would hang the interface with no error.

**Keys live in the user's volume, not the container's environment.** An
environment variable shows up in `docker inspect` and in the host's process
list, which puts one user's key within reach of anyone with host access.
`silkrun key` writes into `$SILKCODE_HOME/config.json` inside the volume
instead, mode 0600.

**Containers are capped and stripped**: `--memory`, `--cpus`, `--pids-limit`,
`--cap-drop ALL`, `--security-opt no-new-privileges`, non-root uid 10001, and
the port published to host loopback only. Tune the caps with `$SILK_MEMORY`,
`$SILK_CPUS`, `$SILK_PIDS`.

## What was verified

Tested against a real `silkcode gui` daemon (not containerised — the build host
had no Docker daemon):

| | Result |
| --- | --- |
| Daemon rejects a tokenless request | 401 |
| Proxy injects the token; GUI loads | 200, 131 KB |
| Token in the response **body** | 0 occurrences |
| Token in the response **headers** | 0 occurrences — the daemon's `Set-Cookie: silk_token` is stripped |
| API with only the routing cookie | 200 — so dropping that cookie costs nothing |
| API through the proxy | 200, correct JSON |
| Same-origin browser request | 200 |
| Cross-site `Origin` | 403 — protection survives the proxy |
| SSE frame through the proxy | arrived in 1.56 s versus 1.56 s direct — no buffering |
| Proxy binding beyond loopback | refused, exit 64 |
| Unknown user / no cookie | 404 / 401 |

**Not yet verified:** the Dockerfile and `silkrun` have not been run against a
live Docker daemon. Expect to iterate on the image the first time you build it.

## What is missing

- **Slice 2 — authentication.** GitHub OAuth web flow replacing `/_login`, real
  user records, and keys entered by the user rather than by an operator.
- **Slice 3 — the front door.** Landing page, session list, key management in
  the UI, an idle reaper so stopped work stops costing money, and TLS via Caddy.

Until slice 2 exists, this stays behind an SSH tunnel.
