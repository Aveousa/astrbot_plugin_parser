from __future__ import annotations

import asyncio
import importlib
import sys
import tempfile
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.data import DynamicContent, ImageContent, ParseResult, Platform, VideoContent
from core.exception import DownloadException


@pytest.fixture
def renderer_module(monkeypatch: pytest.MonkeyPatch):
    """Provide the smallest AstrBot import surface needed by ``core.render``."""
    astrbot = types.ModuleType("astrbot")
    astrbot.__path__ = []
    api = types.ModuleType("astrbot.api")
    api.logger = SimpleNamespace(
        debug=lambda *a, **k: None,
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        error=lambda *a, **k: None,
        exception=lambda *a, **k: None,
    )
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
    monkeypatch.delitem(sys.modules, "core.render", raising=False)
    return importlib.import_module("core.render")


class _Config:
    card_enabled = True
    card_template = "custom"
    card_custom_template = "custom"
    emoji_style = "APPLE"

    def __init__(self, root: Path):
        self.cache_dir = root / "cache"
        self.template_dir = root / "templates"
        self.plugin_dir = root / "plugin"
        self.cache_dir.mkdir()
        self.template_dir.mkdir()
        self.plugin_dir.mkdir()


@pytest.mark.parametrize("template", ["default", "compact", "apple"])
def test_builtin_templates_use_bundled_douyin_sans(
    renderer_module, tmp_path: Path, template: str
):
    config = _Config(tmp_path)
    config.card_template = template
    renderer = renderer_module.Renderer(config)
    result = ParseResult(
        platform=Platform("test", "Test"),
        title="字体测试",
        like_count=1234,
        comment_count=56,
        favorite_count=78,
        share_count=90,
    )

    font_path = renderer_module.Renderer._CARD_FONT_PATH
    assert font_path.name == "douyin_sans.otf"
    assert font_path.read_bytes()[:4] == b"OTTO"

    html = renderer.render_html(result, asyncio.run(renderer._result_context(result)))
    assert f'url("{font_path.resolve().as_uri()}")' in html
    assert 'font-family: "Douyin Sans"' in html
    assert "HYSongYunLangHeiW-1" not in html
    assert 'data-card-root' in html
    for icon_name in ("like.png", "comment.png", "favorites.png", "share.png"):
        assert (font_path.parent / icon_name).resolve().as_uri() in html


class _FakePage:
    def __init__(self):
        self.goto_calls: list[tuple[str, dict]] = []
        self.wait_calls: list[tuple[str, dict]] = []
        self.screenshot_calls: list[dict] = []
        self.closed = False

    async def goto(self, url: str, **kwargs):
        self.goto_calls.append((url, kwargs))

    async def wait_for_function(self, expression: str, **kwargs):
        self.wait_calls.append((expression, kwargs))

    async def screenshot(self, **kwargs):
        self.screenshot_calls.append(kwargs)
        Path(kwargs["path"]).write_bytes(b"png")

    async def close(self):
        self.closed = True


class _FakeElement:
    def __init__(self, page: "_FakeCardPage"):
        self.page = page
        self.first = self

    async def count(self):
        return 1

    async def screenshot(self, **kwargs):
        self.page.element_screenshot_calls.append(kwargs)
        Path(kwargs["path"]).write_bytes(b"element-png")


class _FakeCardPage(_FakePage):
    def __init__(self):
        super().__init__()
        self.element_screenshot_calls: list[dict] = []
        self.locator_calls: list[str] = []

    def locator(self, selector: str):
        self.locator_calls.append(selector)
        return _FakeElement(self)


class _FakeContext:
    def __init__(self):
        self.page = _FakePage()
        self.closed = False

    async def new_page(self):
        return self.page

    async def close(self):
        self.closed = True


class _FakeBrowser:
    def __init__(self):
        self.context = _FakeContext()
        self.new_context_calls: list[dict] = []
        self.closed = False

    def is_connected(self):
        return not self.closed

    async def new_context(self, **kwargs):
        self.new_context_calls.append(kwargs)
        return self.context

    async def close(self):
        self.closed = True


class _FakeChromium:
    def __init__(self, browser: _FakeBrowser):
        self.browser = browser
        self.launch_calls: list[dict] = []

    async def launch(self, **kwargs):
        self.launch_calls.append(kwargs)
        return self.browser


class _FakePlaywright:
    def __init__(self):
        self.browser = _FakeBrowser()
        self.chromium = _FakeChromium(self.browser)
        self.stopped = False

    async def stop(self):
        self.stopped = True


class _FakePlaywrightManager:
    def __init__(self, playwright: _FakePlaywright):
        self.playwright = playwright
        self.start_calls = 0

    def __call__(self):
        return self

    async def start(self):
        self.start_calls += 1
        return self.playwright


def test_total_card_switch_blocks_renderer(renderer_module, tmp_path: Path):
    config = _Config(tmp_path)
    config.card_enabled = False
    renderer = renderer_module.Renderer(config)
    result = ParseResult(platform=Platform("xhs", "\u5c0f\u7ea2\u4e66"), title="skip")

    assert asyncio.run(renderer.render_card(result)) is None


def test_video_without_cover_uses_error_placeholder(renderer_module, tmp_path: Path, monkeypatch):
    config = _Config(tmp_path)
    renderer = renderer_module.Renderer(config)
    renderer._emoji_source = None
    error_cover = tmp_path / "err.png"
    error_cover.write_bytes(b"error")
    monkeypatch.setattr(renderer_module.Renderer, "_RESOURCES_DIR", tmp_path)

    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    result = ParseResult(
        platform=Platform("bilibili", "Bilibili"),
        title="无封面视频",
        contents=[VideoContent(video, cover=None)],
    )

    context = asyncio.run(renderer._result_context(result))
    assert context["card"]["contents"][0]["uri"] == error_cover.resolve().as_uri()


def test_video_with_failed_cover_uses_error_placeholder(
    renderer_module, tmp_path: Path, monkeypatch
):
    config = _Config(tmp_path)
    renderer = renderer_module.Renderer(config)
    renderer._emoji_source = None
    error_cover = tmp_path / "error.png"
    error_cover.write_bytes(b"error")
    monkeypatch.setattr(renderer_module.Renderer, "_RESOURCES_DIR", tmp_path)

    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")

    async def make_context():
        async def failed_cover_download():
            raise DownloadException("HTTP 403 Forbidden")

        result = ParseResult(
            platform=Platform("douyin", "抖音"),
            title="封面下载失败的视频",
            contents=[VideoContent(video, cover=asyncio.create_task(failed_cover_download()))],
        )
        return await renderer._result_context(result)

    context = asyncio.run(make_context())
    assert context["card"]["contents"][0]["uri"] == error_cover.resolve().as_uri()


@pytest.mark.parametrize("template", ["default", "apple"])
def test_placeholder_description_does_not_render_an_intro_box(
    renderer_module, tmp_path: Path, template: str
):
    config = _Config(tmp_path)
    config.card_template = template
    renderer = renderer_module.Renderer(config)
    renderer._emoji_source = None

    video = tmp_path / "video.mp4"
    cover = tmp_path / "cover.jpg"
    video.write_bytes(b"video")
    cover.write_bytes(b"cover")
    result = ParseResult(
        platform=Platform("bilibili", "Bilibili"),
        title="无简介视频",
        text="简介：-",
        contents=[VideoContent(video, cover=cover)],
    )

    context = asyncio.run(renderer._result_context(result))
    html = renderer.render_html(result, context)
    assert context["card"]["text"] is None
    assert 'class="video-description"' not in html
    assert "简介：-" not in html


def test_image_gallery_ignores_failed_items_and_keeps_completed_image(
    renderer_module, tmp_path: Path
):
    config = _Config(tmp_path)
    renderer = renderer_module.Renderer(config)
    renderer._emoji_source = None
    completed = tmp_path / "completed.png"
    completed.write_bytes(b"image")
    result = ParseResult(
        platform=Platform("xhs", "小红书"),
        title="图集",
        contents=[ImageContent(tmp_path / "missing.png"), ImageContent(completed)],
    )

    context = asyncio.run(renderer._result_context(result))
    contents = context["card"]["contents"]
    assert contents[0]["uri"] is None
    assert contents[1]["uri"] == completed.resolve().as_uri()


def test_builtin_template_only_renders_available_statistics(renderer_module, tmp_path: Path):
    config = _Config(tmp_path)
    config.card_template = "default"
    renderer = renderer_module.Renderer(config)
    renderer._emoji_source = None

    empty = ParseResult(platform=Platform("pixiv", "Pixiv"), title="empty")
    empty_html = renderer.render_html(empty, asyncio.run(renderer._result_context(empty)))
    assert 'class="stats"' not in empty_html

    counted = ParseResult(
        platform=Platform("pixiv", "Pixiv"), title="counted", favorite_count=0
    )
    counted_html = renderer.render_html(
        counted, asyncio.run(renderer._result_context(counted))
    )
    assert 'class="stats"' in counted_html
    assert "\u6536\u85cf" in counted_html


def test_default_template_places_video_description_below_preview_and_truncates(
    renderer_module, tmp_path: Path
):
    config = _Config(tmp_path)
    config.card_template = "default"
    renderer = renderer_module.Renderer(config)
    renderer._emoji_source = None

    video_file = tmp_path / "video.mp4"
    video_cover = tmp_path / "video-cover.jpg"
    video_file.write_bytes(b"video")
    video_cover.write_bytes(b"cover")
    description = "视频简介" + "甲" * 116 + "此段超出上限，不能展示"
    result = ParseResult(
        platform=Platform("bilibili", "Bilibili"),
        title="视频",
        text=description,
        contents=[VideoContent(video_file, cover=video_cover, duration=80)],
    )

    html = renderer.render_html(result, asyncio.run(renderer._result_context(result)))

    assert 'class="media-grid single"' in html
    assert 'class="video-description"' in html
    assert html.index('class="media-grid single"') < html.index(
        'class="video-description"'
    )
    assert description[:120] in html
    assert "此段超出上限，不能展示" not in html
    assert description[:120] not in html[: html.index('class="media-grid single"')]


def test_playwright_failure_skips_card_without_fallback(
    renderer_module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    config = _Config(tmp_path)
    renderer = renderer_module.Renderer(config)
    renderer._emoji_source = None

    async def fails(html: str, target: Path, *, base_url: str | None = None) -> bool:
        return False

    monkeypatch.setattr(renderer, "_render_playwright_png", fails)
    result = ParseResult(platform=Platform("xhs", "\u5c0f\u7ea2\u4e66"), title="failure")

    assert asyncio.run(renderer.render_card(result)) is None
    assert result.render_image is None
    assert not list(config.cache_dir.glob("card_*.png"))


def test_custom_jinja_template_uses_playwright_png(
    renderer_module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    config = _Config(tmp_path)
    (tmp_path / "templates" / "custom.html").write_text(
        "<html><body><h1>{{ card.title|emoji }}</h1>"
        "<p>{{ card.stats.likes|format_count }}</p></body></html>",
        encoding="utf-8",
    )
    renderer = renderer_module.Renderer(config)
    renderer._emoji_source = None
    assert renderer.template_dirs[0] == config.template_dir
    assert renderer._template_base_dir() == config.template_dir
    assert renderer_module.Renderer._TEMPLATES_DIR in renderer.template_dirs
    assert {"default", "compact", "apple", "custom"} <= set(renderer.available_templates())
    result = ParseResult(
        platform=Platform("xhs", "\u5c0f\u7ea2\u4e66"),
        title="Apple \U0001f44b\U0001f3fd 1\ufe0f\u20e3 \u260e\ufe0f",
        like_count=12_000,
    )

    context = asyncio.run(renderer._result_context(result))
    html = renderer.render_html(result, context)
    assert "1.2\u4e07" in html
    assert "emoji--apple" in html

    calls: list[tuple[str, Path, str | None]] = []

    async def screenshot(html: str, target: Path, *, base_url: str | None = None) -> bool:
        calls.append((html, target, base_url))
        target.write_bytes(b"\x89PNG\r\n\x1a\n")
        return True

    monkeypatch.setattr(renderer, "_render_playwright_png", screenshot)
    output = asyncio.run(renderer.render_card(result))
    assert output is not None and output.read_bytes().startswith(b"\x89PNG")
    assert calls and calls[0][2] == str(config.template_dir)


def test_playwright_screenshot_uses_headless_shell_and_removes_temp_html(
    renderer_module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    config = _Config(tmp_path)
    renderer = renderer_module.Renderer(config)
    fake = _FakePlaywright()
    manager = _FakePlaywrightManager(fake)
    monkeypatch.setattr(renderer_module, "_PLAYWRIGHT_AVAILABLE", True)
    monkeypatch.setattr(renderer_module, "async_playwright", manager)

    target = config.cache_dir / "card.png"
    assert asyncio.run(
        renderer._render_playwright_png(
            "<html><head></head><body>card</body></html>",
            target,
            base_url=str(config.template_dir),
        )
    )
    assert target.read_bytes() == b"png"
    assert not list(config.cache_dir.glob("*.html"))
    assert fake.chromium.launch_calls[0]["headless"] is True
    assert fake.browser.new_context_calls[0]["viewport"]["width"] == 760
    assert fake.browser.context.page.goto_calls[0][0].startswith("file:///")
    assert fake.browser.context.page.screenshot_calls[0]["full_page"] is True
    assert fake.browser.context.page.closed is True

    # The browser is retained until plugin shutdown, so subsequent cards avoid
    # another shell launch.
    assert asyncio.run(renderer.start())
    assert len(fake.chromium.launch_calls) == 1
    asyncio.run(renderer.close())
    assert fake.browser.context.closed is True
    assert fake.browser.closed is True
    assert fake.stopped is True


def test_playwright_screenshot_crops_marked_card_root(
    renderer_module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    config = _Config(tmp_path)
    renderer = renderer_module.Renderer(config)
    fake = _FakePlaywright()
    fake.browser.context.page = _FakeCardPage()
    manager = _FakePlaywrightManager(fake)
    monkeypatch.setattr(renderer_module, "_PLAYWRIGHT_AVAILABLE", True)
    monkeypatch.setattr(renderer_module, "async_playwright", manager)

    target = config.cache_dir / "card-root.png"
    assert asyncio.run(
        renderer._render_playwright_png(
            '<main data-card-root>card</main>', target,
            base_url=str(config.template_dir),
        )
    )
    page = fake.browser.context.page
    assert page.locator_calls == ["[data-card-root]"]
    assert page.element_screenshot_calls[0]["type"] == "png"
    assert "full_page" not in page.element_screenshot_calls[0]
    assert target.read_bytes() == b"element-png"


def test_base_url_injection_preserves_existing_base(renderer_module, tmp_path: Path):
    base = str(tmp_path / "assets")
    injected = renderer_module.Renderer._inject_base_url(
        "<html><head><title>x</title></head><body></body></html>", base
    )
    assert "<base href=\"file:///" in injected
    assert "assets/\"" in injected

    existing = "<html><head><base href=\"https://example.test/\"></head></html>"
    assert renderer_module.Renderer._inject_base_url(existing, base) == existing


def test_apple_emoji_sequences_are_kept_together(renderer_module):
    tokens = renderer_module.Renderer._emoji_tokens(
        "\U0001f44b\U0001f3fd 1\ufe0f\u20e3 \u260e\ufe0f \U0001f1e8\U0001f1f3 \U0001f468\u200d\U0001f469\u200d\U0001f467\u200d\U0001f466"
    )
    assert {
        "\U0001f44b\U0001f3fd",
        "1\ufe0f\u20e3",
        "\u260e\ufe0f",
        "\U0001f1e8\U0001f1f3",
        "\U0001f468\u200d\U0001f469\u200d\U0001f467\u200d\U0001f466",
    } <= tokens


def test_apple_template_uses_first_visual_as_the_only_cover(
    renderer_module, tmp_path: Path
):
    config = _Config(tmp_path)
    config.card_template = "apple"
    renderer = renderer_module.Renderer(config)
    renderer._emoji_source = None

    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    gallery = ParseResult(
        platform=Platform("douyin", "抖音"),
        title="多图集",
        contents=[ImageContent(first), ImageContent(second)],
    )
    gallery_html = renderer.render_html(
        gallery, asyncio.run(renderer._result_context(gallery))
    )

    assert first.resolve().as_uri() in gallery_html
    assert second.resolve().as_uri() not in gallery_html
    assert 'data-cover-kind="image"' in gallery_html
    assert 'data-media-count="2"' in gallery_html

    single = ParseResult(
        platform=Platform("xhs", "小红书"),
        title="单图",
        contents=[ImageContent(first)],
    )
    single_html = renderer.render_html(single, asyncio.run(renderer._result_context(single)))

    assert first.resolve().as_uri() in single_html
    assert 'data-media-count="1"' in single_html

    video_file = tmp_path / "video.mp4"
    video_cover = tmp_path / "video-cover.jpg"
    video_file.write_bytes(b"video")
    video_cover.write_bytes(b"cover")
    video = ParseResult(
        platform=Platform("bilibili", "Bilibili"),
        title="视频",
        text="视频简介" + "甲" * 116 + "此段超出上限，不能展示",
        contents=[VideoContent(video_file, cover=video_cover, duration=80)],
    )
    video_html = renderer.render_html(video, asyncio.run(renderer._result_context(video)))

    assert video_cover.resolve().as_uri() in video_html
    assert video_file.resolve().as_uri() not in video_html
    assert 'data-cover-kind="video"' in video_html
    assert 'class="cover__play"' not in video_html
    assert 'class="cover__duration"' not in video_html
    assert 'class="video-description"' in video_html
    assert video_html.index('class="cover cover--video"') < video_html.index(
        'class="video-description"'
    )
    assert "视频简介" + "甲" * 116 in video_html
    assert "此段超出上限，不能展示" not in video_html
    assert "视频简介" not in video_html[: video_html.index('class="cover cover--video"')]
    assert "aspect-ratio: 1.72 / 1" not in video_html
    assert "height: auto;" in video_html

    source_video = tmp_path / "motion.mp4"
    dynamic_cover = tmp_path / "motion.gif"
    second_dynamic_cover = tmp_path / "motion-second.gif"
    source_video.write_bytes(b"motion")
    dynamic_cover.write_bytes(b"gif")
    second_dynamic_cover.write_bytes(b"second gif")
    dynamic = ParseResult(
        platform=Platform("pixiv", "Pixiv"),
        title="动态图片",
        contents=[
            DynamicContent(source_video, gif_path=dynamic_cover),
            DynamicContent(source_video, gif_path=second_dynamic_cover),
        ],
    )
    dynamic_html = renderer.render_html(
        dynamic, asyncio.run(renderer._result_context(dynamic))
    )

    assert dynamic_cover.resolve().as_uri() in dynamic_html
    assert second_dynamic_cover.resolve().as_uri() not in dynamic_html
    assert 'data-cover-kind="dynamic"' in dynamic_html
    assert 'data-media-count="2"' in dynamic_html
