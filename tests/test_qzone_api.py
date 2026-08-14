from __future__ import annotations

import importlib
import sys
import types
from types import SimpleNamespace

import pytest


class FakeResponse:
    def __init__(self, *, status=200, payload=None, text=None, headers=None):
        self.status = status
        self._payload = payload
        self._text = text
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self, content_type=None):
        return self._payload

    async def text(self):
        if self._text is not None:
            return self._text
        return ""


class FakeSession:
    def __init__(self, *, posts=None, gets=None):
        self.posts = list(posts or [])
        self.gets = list(gets or [])
        self.post_calls = []
        self.get_calls = []

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        if not self.posts:
            raise AssertionError("unexpected POST")
        return self.posts.pop(0)

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        if not self.gets:
            raise AssertionError("unexpected GET")
        return self.gets.pop(0)


class FakeCookieJar:
    def __init__(self, values=None):
        self.values = dict(values or {})
        self.updated = []

    def get(self, *, domain=None, path="/", secure=True):
        return dict(self.values)

    def get_cookie_header_for_url(self, url):
        return "; ".join(f"{k}={v}" for k, v in self.values.items())

    def update_from_response(self, values):
        self.updated.extend(values)


@pytest.fixture
def qzone_api(monkeypatch: pytest.MonkeyPatch):
    logger = SimpleNamespace(
        debug=lambda *args, **kwargs: None,
        info=lambda *args, **kwargs: None,
        warning=lambda *args, **kwargs: None,
    )
    astrbot_pkg = types.ModuleType("astrbot")
    astrbot_pkg.__path__ = []
    api_module = types.ModuleType("astrbot.api")
    api_module.logger = logger
    monkeypatch.setitem(sys.modules, "astrbot", astrbot_pkg)
    monkeypatch.setitem(sys.modules, "astrbot.api", api_module)

    cookie_module = types.ModuleType("core.cookie")
    cookie_module.CookieJar = FakeCookieJar
    monkeypatch.setitem(sys.modules, "core.cookie", cookie_module)
    monkeypatch.delitem(sys.modules, "core.qzone_api", raising=False)
    return importlib.import_module("core.qzone_api")


def test_qzone_gtk_matches_hash33(qzone_api):
    assert qzone_api.qzone_gtk("abc") == 193485963


@pytest.mark.asyncio
async def test_snowluma_get_credentials_uses_domain_and_bearer_token(qzone_api):
    response = FakeResponse(
        payload={
            "status": "ok",
            "retcode": 0,
            "data": {
                "cookies": "uin=o12345; p_skey=fresh-pskey; skey=s",
                "token": 11,
                "csrf_token": 22,
            },
        }
    )
    session = FakeSession(posts=[response])
    provider = qzone_api.SnowLumaCredentialProvider(
        session,
        base_url="http://127.0.0.1:3000/",
        access_token="test-token",
        cache_seconds=300,
    )

    credentials = await provider.get()

    assert credentials.cookies.startswith("uin=o12345")
    assert credentials.token == 11
    assert credentials.csrf_token == 22
    assert len(session.post_calls) == 1
    url, kwargs = session.post_calls[0]
    assert url == "http://127.0.0.1:3000/get_credentials"
    assert kwargs["json"] == {"domain": "qzone.qq.com"}
    assert kwargs["headers"]["Authorization"] == "Bearer test-token"


@pytest.mark.asyncio
async def test_snowluma_credentials_are_cached_and_force_refreshable(qzone_api):
    first = FakeResponse(
        payload={"retcode": 0, "data": {"cookies": "uin=o1; p_skey=old"}}
    )
    second = FakeResponse(
        payload={"retcode": 0, "data": {"cookies": "uin=o1; p_skey=new"}}
    )
    session = FakeSession(posts=[first, second])
    provider = qzone_api.SnowLumaCredentialProvider(
        session, base_url="http://127.0.0.1:3000", cache_seconds=300
    )

    assert (await provider.get()).cookies.endswith("p_skey=old")
    assert (await provider.get()).cookies.endswith("p_skey=old")
    assert len(session.post_calls) == 1

    assert (await provider.get(force=True)).cookies.endswith("p_skey=new")
    assert len(session.post_calls) == 2


@pytest.mark.asyncio
async def test_snowluma_invalid_credentials_fail_closed(qzone_api):
    session = FakeSession(
        posts=[FakeResponse(payload={"retcode": 0, "data": {"cookies": "uin=o1"}})]
    )
    provider = qzone_api.SnowLumaCredentialProvider(
        session, base_url="http://127.0.0.1:3000"
    )

    assert await provider.get() is None
    assert "p_skey" in provider.last_error


def test_manual_cookie_auth_is_supported(qzone_api):
    jar = FakeCookieJar({"uin": "o12345", "p_skey": "manual-key", "skey": "s"})
    client = qzone_api.QZoneApiClient(FakeSession(), jar)

    auth = client.auth()

    assert auth is not None
    assert auth.uin == "12345"
    assert auth.uin_cookie == "o12345"
    assert auth.p_skey == "manual-key"
    assert auth.gtk == qzone_api.qzone_gtk("manual-key")


def test_auth_failure_detection_handles_qzone_login_failures(qzone_api):
    assert qzone_api.QZoneApiClient._looks_like_auth_failure({"code": -3000})
    assert qzone_api.QZoneApiClient._looks_like_auth_failure({"message": "请先登录"})
    assert qzone_api.QZoneApiClient._looks_like_auth_failure({}, 401)
    assert not qzone_api.QZoneApiClient._looks_like_auth_failure({"code": 0, "data": {}})


@pytest.mark.asyncio
async def test_object_response_supports_json5_callback(qzone_api):
    response = FakeResponse(text="callback({code: 0, data: {name: 'ok'}})")

    payload = await qzone_api.QZoneApiClient._read_object_response(response)

    assert payload == {"code": 0, "data": {"name": "ok"}}


@pytest.mark.asyncio
async def test_api_refreshes_snowluma_once_after_minus_3000(qzone_api):
    class Provider:
        enabled = True

        def __init__(self):
            self.calls = []
            self.invalidated = 0

        async def get(self, *, force=False):
            self.calls.append(force)
            key = "new" if force else "old"
            return qzone_api.SnowLumaCredentials(
                cookies=f"uin=o123; p_skey={key}",
                csrf_token=99 if force else 88,
            )

        def invalidate(self):
            self.invalidated += 1

    session = FakeSession(
        gets=[
            FakeResponse(text='{"code": -3000, "message": "请先登录"}'),
            FakeResponse(text='{"code": 0, "data": {"feeds": []}}'),
        ]
    )
    provider = Provider()
    client = qzone_api.QZoneApiClient(
        session, None, credential_provider=provider
    )

    payload = await client.get_photo_feed(host_uin="123")

    assert payload["code"] == 0
    assert provider.invalidated == 1
    assert provider.calls == [False, True, False]
    assert len(session.get_calls) == 2
