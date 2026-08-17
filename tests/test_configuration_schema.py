import importlib
import json
from pathlib import Path
import sys
import tempfile
import tomllib
import types
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def config_module(monkeypatch: pytest.MonkeyPatch):
    """为配置迁移测试提供最小 AstrBot 运行时桩。"""
    astrbot = types.ModuleType("astrbot")
    astrbot.__path__ = []
    api = types.ModuleType("astrbot.api")
    api.logger = SimpleNamespace(
        debug=lambda *a, **k: None,
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        error=lambda *a, **k: None,
    )
    core = types.ModuleType("astrbot.core")
    core.__path__ = []
    config_pkg = types.ModuleType("astrbot.core.config")
    config_pkg.__path__ = []
    config_mod = types.ModuleType("astrbot.core.config.astrbot_config")

    class AstrBotConfig(dict):
        def save_config(self):
            self.save_calls = getattr(self, "save_calls", 0) + 1

    config_mod.AstrBotConfig = AstrBotConfig
    star = types.ModuleType("astrbot.core.star")
    star.__path__ = []
    context_mod = types.ModuleType("astrbot.core.star.context")
    context_mod.Context = object
    utils = types.ModuleType("astrbot.core.utils")
    utils.__path__ = []
    path_mod = types.ModuleType("astrbot.core.utils.astrbot_path")
    path_mod.get_astrbot_plugin_data_path = lambda: tempfile.gettempdir()
    path_mod.get_astrbot_plugin_path = lambda: tempfile.gettempdir()

    for name, module in {
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.core": core,
        "astrbot.core.config": config_pkg,
        "astrbot.core.config.astrbot_config": config_mod,
        "astrbot.core.star": star,
        "astrbot.core.star.context": context_mod,
        "astrbot.core.utils": utils,
        "astrbot.core.utils.astrbot_path": path_mod,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.delitem(sys.modules, "core.config", raising=False)
    return importlib.import_module("core.config")


def test_configuration_exposes_card_switches_and_template_selector():
    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))

    assert schema["card_enabled"]["default"] is True
    assert schema["card_render_enabled"]["default"] is True
    assert schema["card_send_enabled"]["default"] is True
    assert schema["card_template"]["options"] == ["default", "compact", "custom"]
    assert schema["card_custom_template"]["default"] == ""
    assert "APPLE" in schema["emoji_style"]["options"]
    assert schema["single_heavy_render_card"]["invisible"] is True


def test_test_plugin_identity_is_isolated_from_original_plugin(config_module):
    metadata_name = next(
        line.split(":", 1)[1].strip()
        for line in (ROOT / "metadata.yaml").read_text(encoding="utf-8").splitlines()
        if line.startswith("name:")
    )
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert metadata_name == "astrbot_plugin_parser_test"
    assert project["project"]["name"] == "astrbot_plugin_parser_test"
    assert config_module.PluginConfig._plugin_name == "astrbot_plugin_parser_test"


def test_only_four_parser_templates_are_exposed():
    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    defaults = json.loads((ROOT / "default_template.json").read_text(encoding="utf-8"))
    supported = {"bilibili", "douyin", "xhs", "pixiv"}

    assert set(schema["parsers_template"]["templates"]) == supported
    assert {item["__template_key"] for item in defaults} == supported


def test_parser_config_migration_keeps_legacy_bilibili_preference(config_module):
    raw = config_module.AstrBotConfig(
        {
            "parsers_template": [
                {
                    "__template_key": "bilibili",
                    "enable": False,
                    "cookies": "kept-cookie",
                    "video_codecs": "HEV",
                },
                {"__template_key": "qzone", "enable": True},
            ]
        }
    )
    config = config_module.PluginConfig.__new__(config_module.PluginConfig)
    object.__setattr__(config, "_data", raw)
    object.__setattr__(config, "_children", {})
    object.__setattr__(config, "default_template_file", ROOT / "default_template.json")

    config._migrate_parser_template()

    assert [item["__template_key"] for item in raw["parsers_template"]] == [
        "bilibili",
        "douyin",
        "xhs",
        "pixiv",
    ]
    bilibili = raw["parsers_template"][0]
    assert bilibili["enable"] is False
    assert bilibili["cookies"] == "kept-cookie"
    assert bilibili["video_codec_list"] == ["HEV"]
    assert "video_codecs" not in bilibili
    assert raw.save_calls == 1
