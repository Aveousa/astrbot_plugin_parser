from __future__ import annotations

import asyncio
import importlib
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.data import ParseResult, Platform, VideoContent


@pytest.fixture
def sender_module(monkeypatch: pytest.MonkeyPatch):
    astrbot = types.ModuleType("astrbot")
    astrbot.__path__ = []
    api = types.ModuleType("astrbot.api")
    api.logger = SimpleNamespace(error=lambda *a, **k: None)
    core = types.ModuleType("astrbot.core")
    core.__path__ = []
    message = types.ModuleType("astrbot.core.message")
    message.__path__ = []
    components = types.ModuleType("astrbot.core.message.components")
    platform = types.ModuleType("astrbot.core.platform")
    platform.__path__ = []
    event_mod = types.ModuleType("astrbot.core.platform.astr_message_event")

    class BaseMessageComponent: ...

    class Image(BaseMessageComponent):
        @classmethod
        def fromFileSystem(cls, path):
            return cls()

    class Video(Image): ...
    class Record(Image): ...

    class File(BaseMessageComponent):
        def __init__(self, *args, **kwargs): pass

    class Plain(BaseMessageComponent):
        def __init__(self, *args, **kwargs): pass

    class Node(BaseMessageComponent):
        def __init__(self, *args, **kwargs): pass

    class Nodes(BaseMessageComponent):
        def __init__(self, nodes): self.nodes = nodes

    for name, value in {
        "BaseMessageComponent": BaseMessageComponent,
        "File": File,
        "Image": Image,
        "Node": Node,
        "Nodes": Nodes,
        "Plain": Plain,
        "Record": Record,
        "Video": Video,
    }.items():
        setattr(components, name, value)
    event_mod.AstrMessageEvent = object

    config = types.ModuleType("core.config")
    config.PluginConfig = object
    renderer = types.ModuleType("core.render")
    renderer.Renderer = object
    modules = {
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.core": core,
        "astrbot.core.message": message,
        "astrbot.core.message.components": components,
        "astrbot.core.platform": platform,
        "astrbot.core.platform.astr_message_event": event_mod,
        "core.config": config,
        "core.render": renderer,
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.delitem(sys.modules, "core.sender", raising=False)
    return importlib.import_module("core.sender")


def _result() -> ParseResult:
    return ParseResult(
        platform=Platform("bilibili", "Bilibili"),
        contents=[VideoContent(Path("video.mp4"))],
    )


def test_global_card_switches_override_send_group_policy(sender_module):
    renderer = SimpleNamespace()
    config = SimpleNamespace(
        single_heavy_render_card=True,
        forward_threshold=10,
        render_card_enabled=False,
        send_card_enabled=True,
    )
    sender = sender_module.MessageSender(config, renderer)

    plan = sender._build_send_plan(_result(), render_card_override=True)
    assert plan["render_card"] is False
    assert plan["send_card"] is False

    config.render_card_enabled = True
    config.send_card_enabled = False
    plan = sender._build_send_plan(_result(), render_card_override=True)
    assert plan["render_card"] is True
    assert plan["send_card"] is False
    assert plan["preview_card"] is False

    config.send_card_enabled = True
    plan = sender._build_send_plan(_result(), render_card_override=True)
    assert plan["render_card"] is True
    assert plan["send_card"] is True


def test_legacy_raw_total_switch_is_respected(sender_module):
    renderer = SimpleNamespace()
    config = SimpleNamespace(
        single_heavy_render_card=True,
        forward_threshold=2,
        card_enabled=False,
        card_render_enabled=True,
        card_send_enabled=True,
    )
    sender = sender_module.MessageSender(config, renderer)

    plan = sender._build_send_plan(_result(), render_card_override=True)
    assert plan["render_card"] is False
    assert plan["send_card"] is False


def test_renderer_exception_does_not_block_media_send(sender_module):
    class Renderer:
        async def render_card(self, result):
            raise RuntimeError("template failed")

    class Event:
        def __init__(self):
            self.sent = []

        def chain_result(self, segments):
            return segments

        async def send(self, message):
            self.sent.append(message)

        def get_self_id(self):
            return "bot"

    config = SimpleNamespace(
        single_heavy_render_card=True,
        forward_threshold=10,
        render_card_enabled=True,
        send_card_enabled=True,
        show_download_fail_tip=True,
        audio_to_file=True,
    )
    sender = sender_module.MessageSender(config, Renderer())
    event = Event()

    asyncio.run(sender.send_parse_result(event, _result()))

    # 渲染异常只跳过卡片，视频消息仍然正常发送。
    assert len(event.sent) == 1
    assert len(event.sent[0]) == 1
    assert event.sent[0][0].__class__.__name__ == "Video"
