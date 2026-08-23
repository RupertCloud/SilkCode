import time
import urllib.parse

import httpx
import pytest

import silkcode.github_oauth as gho
from silkcode.config import Config
from silkcode.github import get_token
from silkcode.github_oauth import DeviceFlow, DeviceFlowError, client_id_from, store_token


def mock_flow(handler, monkeypatch):
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(gho, "_make_client", lambda: httpx.Client(transport=transport))


def form(request):
    return dict(urllib.parse.parse_qsl(request.content.decode()))


def test_device_flow_start_and_poll_success(monkeypatch):
    polls = {"n": 0}

    def handler(request):
        if request.url.path == "/login/device/code":
            assert form(request)["client_id"] == "cid"
            return httpx.Response(200, json={
                "device_code": "dev123", "user_code": "ABCD-1234",
                "verification_uri": "https://github.com/login/device",
                "interval": 1, "expires_in": 900,
            })
        polls["n"] += 1
        if polls["n"] == 1:
            return httpx.Response(200, json={"error": "authorization_pending"})
        if polls["n"] == 2:
            return httpx.Response(200, json={"error": "slow_down", "interval": 2})
        return httpx.Response(200, json={
            "access_token": "ghu_tok", "refresh_token": "ghr_ref", "expires_in": 28800,
        })

    mock_flow(handler, monkeypatch)
    sleeps = []
    flow = DeviceFlow("cid", sleep=sleeps.append)
    info = flow.start()
    assert info["user_code"] == "ABCD-1234"
    data = flow.poll(info["device_code"], interval=1)
    assert data["access_token"] == "ghu_tok"
    assert len(sleeps) == 3
    assert sleeps[-1] > sleeps[0]  # slow_down increased the wait


def test_device_flow_denied(monkeypatch):
    def handler(request):
        if request.url.path == "/login/device/code":
            return httpx.Response(200, json={"device_code": "d", "user_code": "u",
                                             "verification_uri": "v", "interval": 1})
        return httpx.Response(200, json={"error": "access_denied"})

    mock_flow(handler, monkeypatch)
    flow = DeviceFlow("cid", sleep=lambda s: None)
    with pytest.raises(DeviceFlowError, match="denied"):
        flow.poll("d", interval=1)


def test_device_flow_refresh(monkeypatch):
    def handler(request):
        data = form(request)
        assert data["grant_type"] == "refresh_token"
        assert data["refresh_token"] == "ghr_old"
        return httpx.Response(200, json={"access_token": "ghu_new", "refresh_token": "ghr_new",
                                         "expires_in": 28800})

    mock_flow(handler, monkeypatch)
    data = DeviceFlow("cid").refresh("ghr_old")
    assert data["access_token"] == "ghu_new"


def test_requires_client_id():
    with pytest.raises(DeviceFlowError, match="client id"):
        DeviceFlow("")


def test_store_token_and_client_id_resolution(tmp_path):
    config = Config({}, path=tmp_path / "config.json")
    store_token(config, {"access_token": "ghu_1", "refresh_token": "ghr_1", "expires_in": 28800})
    saved = Config.load(tmp_path / "config.json")
    assert saved.data["github"]["token"] == "ghu_1"
    assert saved.data["github"]["refresh_token"] == "ghr_1"
    assert saved.data["github"]["token_expires_at"] > time.time()

    assert client_id_from({"github": {"client_id": "abc"}}) == "abc"
    assert client_id_from({}) == gho.DEFAULT_GITHUB_CLIENT_ID
    assert client_id_from({}) == "Iv23liIVrIULoOVsdX9b"


def test_get_token_env_beats_stored(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "env_tok")
    config = Config({"github": {"token": "stored"}}, path=tmp_path / "c.json")
    assert get_token(config) == "env_tok"


def test_get_token_valid_stored(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    config = Config({"github": {"token": "stored", "token_expires_at": time.time() + 3600}},
                    path=tmp_path / "c.json")
    assert get_token(config) == "stored"


def test_get_token_refreshes_expired(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    def handler(request):
        return httpx.Response(200, json={"access_token": "ghu_fresh", "refresh_token": "ghr_2",
                                         "expires_in": 28800})

    mock_flow(handler, monkeypatch)
    config = Config({"github": {"token": "ghu_stale", "refresh_token": "ghr_1",
                                "client_id": "cid", "token_expires_at": time.time() - 10}},
                    path=tmp_path / "c.json")
    assert get_token(config) == "ghu_fresh"
    assert config.data["github"]["token"] == "ghu_fresh"
    assert config.data["github"]["refresh_token"] == "ghr_2"


def test_get_token_expired_without_refresh(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    config = Config({"github": {"token": "ghu_stale", "token_expires_at": time.time() - 10}},
                    path=tmp_path / "c.json")
    assert get_token(config) is None
