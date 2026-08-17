from __future__ import annotations

import asyncio
import importlib
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.data import (
    ImageContent,
    ParseResult,
    Platform,
    SendGroup,
    VideoContent,
)


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
        def __init__(self, path=None):
            self.path = path

        @classmethod
        def fromFileSystem(cls, path):
            return cls(path)

    class Video(Image): ...
    class Record(Image): ...

    class File(BaseMessageComponent):
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    class Plain(BaseMessageComponent):
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    class Node(BaseMessageComponent):
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs
            self.content = kwargs.get("content")

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


class _Event:
    def __init__(self):
        self.sent = []

    def chain_result(self, segments):
        return segments

    async def send(self, message):
        self.sent.append(message)

    def get_self_id(self):
        return "bot"


class _Renderer:
    def __init__(self, card_path: Path = Path("card.png")):
        self.card_path = card_path
        self.calls: list[ParseResult] = []

    async def render_card(self, result: ParseResult):
        self.calls.append(result)
        return self.card_path


def _card_config(**overrides):
    config = {
        "forward_threshold": 2,
        "render_card_enabled": True,
        "send_card_enabled": True,
        "show_download_fail_tip": True,
        "audio_to_file": True,
    }
    config.update(overrides)
    return SimpleNamespace(**config)


def test_global_card_switches_govern_result_card(sender_module):
    renderer = _Renderer()
    config = SimpleNamespace(
        forward_threshold=10,
        render_card_enabled=False,
        send_card_enabled=True,
        show_download_fail_tip=True,
        audio_to_file=True,
    )
    sender = sender_module.MessageSender(config, renderer)
    event = _Event()

    assert not asyncio.run(sender._send_result_card(event, _result()))
    assert not renderer.calls

    config.render_card_enabled = True
    config.send_card_enabled = False
    assert not asyncio.run(sender._send_result_card(event, _result()))
    assert not renderer.calls

    config.send_card_enabled = True
    assert asyncio.run(sender._send_result_card(event, _result()))
    assert len(renderer.calls) == 1
    assert len(event.sent) == 1


def test_legacy_raw_total_switch_is_respected(sender_module):
    renderer = _Renderer()
    config = SimpleNamespace(
        forward_threshold=2,
        card_enabled=False,
        card_render_enabled=True,
        card_send_enabled=True,
        show_download_fail_tip=True,
        audio_to_file=True,
    )
    sender = sender_module.MessageSender(config, renderer)
    event = _Event()

    assert not asyncio.run(sender._send_result_card(event, _result()))
    assert not renderer.calls


@pytest.mark.parametrize("platform_name", ["bilibili", "douyin", "xhs", "pixiv"])
def test_global_card_is_sent_once_for_every_platform(
    sender_module, platform_name: str
):
    image = ImageContent(Path("image.png"))
    result = ParseResult(
        platform=Platform(platform_name, platform_name),
        contents=[image],
        # A legacy per-group false preference cannot suppress the global card.
        send_groups=[SendGroup(contents=[image], force_merge=False, render_card=False)],
    )
    renderer = _Renderer()
    sender = sender_module.MessageSender(_card_config(), renderer)
    event = _Event()

    asyncio.run(sender.send_parse_result(event, result))

    assert renderer.calls == [result]
    assert len(event.sent) == 2
    assert event.sent[0][0].path == "card.png"
    assert event.sent[1][0].path == "image.png"


def test_card_does_not_make_a_single_image_folded(sender_module):
    image = ImageContent(Path("image.png"))
    result = ParseResult(platform=Platform("douyin", "抖音"), contents=[image])
    renderer = _Renderer()
    sender = sender_module.MessageSender(_card_config(forward_threshold=2), renderer)
    event = _Event()

    asyncio.run(sender.send_parse_result(event, result))

    assert len(event.sent) == 2
    assert event.sent[0][0].path == "card.png"
    assert event.sent[1][0].__class__.__name__ == "Image"
    assert event.sent[1][0].path == "image.png"


def test_douyin_gallery_card_precedes_folded_media(sender_module):
    images = [ImageContent(Path("one.png")), ImageContent(Path("two.png"))]
    result = ParseResult(
        platform=Platform("douyin", "抖音"),
        contents=images,
        send_groups=[SendGroup(contents=images, force_merge=True)],
    )
    renderer = _Renderer()
    sender = sender_module.MessageSender(_card_config(), renderer)
    event = _Event()

    asyncio.run(sender.send_parse_result(event, result))

    assert renderer.calls == [result]
    assert len(event.sent) == 2
    assert event.sent[0][0].path == "card.png"
    folded = event.sent[1][0]
    assert folded.__class__.__name__ == "Nodes"
    assert [node.content[0].path for node in folded.nodes] == ["one.png", "two.png"]


def test_legacy_card_group_does_not_duplicate_result_card(sender_module):
    image = ImageContent(Path("image.png"))
    result = ParseResult(
        platform=Platform("pixiv", "Pixiv"),
        contents=[image],
        send_groups=[
            SendGroup(contents=[], force_merge=False, render_card=True),
            SendGroup(contents=[image], force_merge=False, render_card=False),
        ],
    )
    renderer = _Renderer()
    sender = sender_module.MessageSender(_card_config(), renderer)
    event = _Event()

    asyncio.run(sender.send_parse_result(event, result))

    assert renderer.calls == [result]
    assert len(event.sent) == 2
    assert event.sent[0][0].path == "card.png"
    assert event.sent[1][0].path == "image.png"


def test_renderer_exception_does_not_block_media_send(sender_module):
    class Renderer:
        async def render_card(self, result):
            raise RuntimeError("template failed")

    config = _card_config(forward_threshold=10)
    sender = sender_module.MessageSender(config, Renderer())
    event = _Event()

    asyncio.run(sender.send_parse_result(event, _result()))

    # 渲染异常只跳过卡片，视频消息仍然正常发送。
    assert len(event.sent) == 1
    assert len(event.sent[0]) == 1
    assert event.sent[0][0].__class__.__name__ == "Video"


def test_failed_card_keeps_explicit_gallery_forwarding(sender_module):
    class Renderer:
        async def render_card(self, result):
            raise RuntimeError("browser unavailable")

    images = [ImageContent(Path("one.png")), ImageContent(Path("two.png"))]
    result = ParseResult(
        platform=Platform("douyin", "抖音"),
        contents=images,
        send_groups=[SendGroup(contents=images, force_merge=True)],
    )
    sender = sender_module.MessageSender(_card_config(), Renderer())
    event = _Event()

    asyncio.run(sender.send_parse_result(event, result))

    assert len(event.sent) == 1
    assert event.sent[0][0].__class__.__name__ == "Nodes"
    assert len(event.sent[0][0].nodes) == 2
