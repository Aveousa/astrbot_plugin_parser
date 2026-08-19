from __future__ import annotations

import importlib
import sys
import tempfile
import types
from types import SimpleNamespace

import pytest
from yarl import URL


@pytest.fixture
def download_module(monkeypatch: pytest.MonkeyPatch):
    logger = SimpleNamespace(
        debug=lambda *a, **k: None,
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        error=lambda *a, **k: None,
        exception=lambda *a, **k: None,
    )
    astrbot = types.ModuleType("astrbot")
    astrbot.__path__ = []
    api = types.ModuleType("astrbot.api")
    api.logger = logger
    core = types.ModuleType("astrbot.core")
    core.__path__ = []
    config_pkg = types.ModuleType("astrbot.core.config")
    config_pkg.__path__ = []
    config_mod = types.ModuleType("astrbot.core.config.astrbot_config")
    config_mod.AstrBotConfig = dict
    star = types.ModuleType("astrbot.core.star")
    star.__path__ = []
    context_mod = types.ModuleType("astrbot.core.star.context")
    context_mod.Context = object
    utils = types.ModuleType("astrbot.core.utils")
    utils.__path__ = []
    path_mod = types.ModuleType("astrbot.core.utils.astrbot_path")
    path_mod.get_astrbot_plugin_data_path = lambda: tempfile.gettempdir()
    path_mod.get_astrbot_plugin_path = lambda: tempfile.gettempdir()

    modules = {
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.core": core,
        "astrbot.core.config": config_pkg,
        "astrbot.core.config.astrbot_config": config_mod,
        "astrbot.core.star": star,
        "astrbot.core.star.context": context_mod,
        "astrbot.core.utils": utils,
        "astrbot.core.utils.astrbot_path": path_mod,
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.delitem(sys.modules, "core.download", raising=False)
    monkeypatch.delitem(sys.modules, "core.config", raising=False)
    monkeypatch.delitem(sys.modules, "core.utils", raising=False)
    return importlib.import_module("core.download")


def test_request_url_preserves_percent_encoded_signed_query(download_module):
    raw_url = (
        "https://p3-sign.douyinpic.com/tos-cn/example.webp?"
        "x-signature=DkHnjnMPzQbZ%2FkkFsDF2zzqjLzg%3D&from=327834062"
    )

    prepared = download_module.Downloader._request_url(raw_url)

    assert isinstance(prepared, URL)
    assert str(prepared) == raw_url
    assert "x-signature=DkHnjnMPzQbZ%2FkkFsDF2zzqjLzg%3D" in prepared.raw_query_string


def test_request_url_keeps_plain_url_as_string(download_module):
    raw_url = "https://example.com/media/image.webp?from=327834062"

    assert download_module.Downloader._request_url(raw_url) == raw_url
