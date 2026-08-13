"""GitHub App sign-in via the OAuth device flow.

Developers authorize Silk Code like installing any GitHub App: the app shows
a short code, they enter it at github.com/login/device and click Authorize.
No personal access tokens. User tokens are short-lived (ghu_...) and are
refreshed automatically with the stored refresh token (ghr_...).

The project ships a public client id of the registered "Silk Code" GitHub
App (client ids are not secrets in the device flow). Registering the app is
a one-time maintainer step - see docs/GITHUB_APP.md.
"""

from __future__ import annotations

import time
from typing import Callable

import httpx

GITHUB_BASE = "https://github.com"

# Set after the maintainer registers the Silk Code GitHub App (see
# docs/GITHUB_APP.md). Users can always override with `github.client_id`
# in their config or `silkcode connect github --client-id <id>`.
DEFAULT_GITHUB_CLIENT_ID: str | None = None

# Test hook: replaced to inject a mock transport.
_make_client = lambda: httpx.Client(timeout=30.0)  # noqa: E731


class DeviceFlowError(RuntimeError):
    pass


class DeviceFlow:
    def __init__(self, client_id: str, base_url: str = GITHUB_BASE,
                 sleep: Callable[[float], None] = time.sleep):
        if not client_id:
            raise DeviceFlowError("a GitHub App client id is required for device-flow sign-in")
        self.client_id = client_id
        self.base_url = base_url.rstrip("/")
        self._sleep = sleep
        self._client = _make_client()

    def _post(self, path: str, data: dict) -> dict:
        try:
            resp = self._client.post(f"{self.base_url}{path}", data=data,
                                     headers={"Accept": "application/json"})
        except httpx.HTTPError as exc:
            raise DeviceFlowError(f"GitHub request failed: {exc}") from exc
        if resp.status_code >= 400:
            raise DeviceFlowError(f"GitHub error {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    def start(self) -> dict:
        """Begin the flow. Returns user_code, verification_uri, device_code,
        interval, and expires_in."""
        data = self._post("/login/device/code", {"client_id": self.client_id})
        if "device_code" not in data:
            raise DeviceFlowError(f"unexpected device-code response: {data}")
        return data

    def poll(self, device_code: str, interval: int = 5, expires_in: int = 900) -> dict:
        """Poll until the user authorizes. Returns the token payload
        (access_token, and refresh_token/expires_in for GitHub Apps)."""
        deadline = time.monotonic() + expires_in
        wait = max(int(interval), 1)
        while time.monotonic() < deadline:
            self._sleep(wait)
            data = self._post("/login/oauth/access_token", {
                "client_id": self.client_id,
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            })
            error = data.get("error")
            if not error:
                if "access_token" in data:
                    return data
                raise DeviceFlowError(f"unexpected token response: {data}")
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                wait += int(data.get("interval", 5))
                continue
            if error == "access_denied":
                raise DeviceFlowError("authorization was denied in the browser")
            if error == "expired_token":
                break
            raise DeviceFlowError(f"device flow failed: {error}")
        raise DeviceFlowError("the sign-in code expired before it was authorized; try again")

    def refresh(self, refresh_token: str) -> dict:
        data = self._post("/login/oauth/access_token", {
            "client_id": self.client_id,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        })
        if data.get("error") or "access_token" not in data:
            raise DeviceFlowError(f"token refresh failed: {data.get('error', 'no access token')}")
        return data


def store_token(config, data: dict) -> None:
    """Persist a device-flow token payload into the Silk Code config."""
    github_cfg = config.data.setdefault("github", {})
    github_cfg["token"] = data["access_token"]
    if data.get("refresh_token"):
        github_cfg["refresh_token"] = data["refresh_token"]
    if data.get("expires_in"):
        github_cfg["token_expires_at"] = time.time() + int(data["expires_in"]) - 60
    else:
        github_cfg.pop("token_expires_at", None)
    config.save()


def client_id_from(config_data: dict) -> str | None:
    return ((config_data.get("github") or {}).get("client_id")) or DEFAULT_GITHUB_CLIENT_ID
