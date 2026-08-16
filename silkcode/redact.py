"""Strip credentials out of text before it leaves the machine.

Tool output goes straight into the conversation, and the conversation goes to
whichever model provider is answering - often a third party, whose logs are
outside the user's control. So anything a command happens to print is
published. A `printenv`, a `cat .env`, a stack trace carrying a connection
string, a curl transcript with an `Authorization` header: none of these are
attacks, they are ordinary things an agent does, and each one can carry a
live secret out of the room.

This is a backstop, not a boundary. It cannot recognise every secret, and a
determined agent can always encode one past it. It exists so that the
routine, accidental case - by far the likeliest - does not become a
disclosure. The boundaries are elsewhere: credentials the sandbox never holds
as a string (see gitproxy.py), a config file only its owner can read, and a
permission gate on the commands that would go looking.

Patterns are written against published credential formats rather than
adapted from another implementation, so this file carries no licence but
this project's own.
"""

from __future__ import annotations

import re

PLACEHOLDER = "[redacted]"

# Recognisable credential shapes. Each is anchored on a vendor's published
# prefix and a plausible length, so ordinary prose and code cannot trip them:
# a bare "token" or a short hex string is not enough.
PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    # OpenAI-style and the many providers that copied the shape, plus xAI.
    # \b-anchored so "task-", "disk-", "risk-" cannot contribute the "sk-".
    ("api-key", re.compile(r"\b(?:sk|rk)[-_][A-Za-z0-9_-]{16,}")),
    ("xai-key", re.compile(r"\bxai-[A-Za-z0-9_-]{16,}")),
    # GitHub: classic PATs, OAuth/user/server/refresh tokens, fine-grained.
    ("github-token", re.compile(r"\bgh[posur]_[A-Za-z0-9]{16,}")),
    ("github-token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}")),
    ("gitlab-token", re.compile(r"\bglpat-[A-Za-z0-9_-]{16,}")),
    ("slack-token", re.compile(r"\bxox[abps]-[A-Za-z0-9-]{10,}")),
    ("aws-key-id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    # Google keys are AIza + 35, but pinning the exact length makes the
    # backstop brittle against a format tweak for no benefit.
    ("google-key", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}")),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}")),
    # A JWT: three base64url segments. Deployment keys and OIDC tokens.
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    # Whole PEM block, body included. (?s) so "." spans the newlines.
    ("private-key", re.compile(
        r"(?s)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----")),
    ("bearer", re.compile(r"(?i)\b(?:bearer|token)\s+[A-Za-z0-9._~+/=-]{16,}")),
    # user:secret@host in a URL - how a token ends up in a git remote.
    ("url-credentials", re.compile(r"(?<=://)[^\s/:@]+:[^\s/@]{6,}(?=@)")),
)

# KEY=value / "key": "value" forms, where the name says it is a secret. This
# is what catches a credential whose own shape is unremarkable.
#
# A quoted value is taken whole, spaces included - a passphrase is a likely
# secret and an unlikely variable name - while an unquoted one stops at the
# first space. The length floors keep `password = ""` and `token: x` out.
ASSIGNMENT = re.compile(
    r"""(?ix)
    (?<![\w.-])
    (?P<name>
        # an optional prefix, which must end at a separator so that the
        # keyword is a word of its own: client_secret and
        # aws_secret_access_key match, monkey does not
        (?:[\w.-]*[_.-])?
        (?:key|token|secret|password|passwd|credential|passphrase)
    )\b
    # the optional quote closes a JSON key: "access_token": "..."
    (?P<sep>["']?\s*(?::|=>|=)\s*)
    (?:
        (?P<quote>["'])(?P<quoted>[^"'\n]{6,})(?P=quote)
      | (?P<bare>[^\s"',;&)]{8,})
    )
    """,
)


def _mask_assignment(match: re.Match) -> str:
    head = match.group("name") + match.group("sep")
    if match.group("quoted") is not None:
        quote = match.group("quote")
        return f"{head}{quote}{PLACEHOLDER}{quote}"
    return head + PLACEHOLDER

# Query parameters that carry a credential in a URL.
SENSITIVE_PARAMS = (
    "access_token", "api_key", "apikey", "auth", "client_secret", "code",
    "id_token", "key", "password", "refresh_token", "secret", "session",
    "signature", "sig", "token",
)
QUERY_PARAM = re.compile(
    r"(?i)([?&](?:" + "|".join(SENSITIVE_PARAMS) + r")=)[^&\s\"'<>]+")


def redact(text: str, extra: tuple[str, ...] = ()) -> str:
    """Return `text` with anything that looks like a credential removed.

    `extra` holds literal secrets this process already knows - a configured
    API key, a GitHub token - which are removed whatever shape they take,
    because a known secret needs no pattern to recognise it.
    """
    if not text:
        return text
    for secret in extra:
        # A short value would match far too much ordinary text; a real
        # credential is never this short.
        if secret and len(secret) >= 8:
            text = text.replace(secret, PLACEHOLDER)
    for _, pattern in PATTERNS:
        text = pattern.sub(PLACEHOLDER, text)
    text = QUERY_PARAM.sub(r"\1" + PLACEHOLDER, text)
    return ASSIGNMENT.sub(_mask_assignment, text)


def findings(text: str) -> list[str]:
    """Names of the credential kinds present in `text`. For explaining a
    redaction to the user without printing what was redacted."""
    found = []
    for name, pattern in PATTERNS:
        if pattern.search(text) and name not in found:
            found.append(name)
    return found


def known_secrets(config) -> tuple[str, ...]:
    """Literal credentials this installation holds: provider API keys,
    whether stored in the config or named through the environment, and the
    GitHub token."""
    import os

    values: list[str] = []
    for cfg in (config.providers or {}).values():
        if not isinstance(cfg, dict):
            continue
        if cfg.get("api_key"):
            values.append(str(cfg["api_key"]))
        env = cfg.get("api_key_env")
        if env and os.environ.get(env):
            values.append(os.environ[env])
    github = (config.data.get("github") or {}) if hasattr(config, "data") else {}
    for key in ("token", "refresh_token"):
        if github.get(key):
            values.append(str(github[key]))
    env = github.get("token_env", "GITHUB_TOKEN")
    if os.environ.get(env):
        values.append(os.environ[env])
    return tuple(dict.fromkeys(v for v in values if v))
