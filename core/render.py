"""HTML card renderer backed by Jinja2 and Playwright Chrome Headless Shell."""

from __future__ import annotations

import asyncio
import os
import re
import uuid
from html import escape as html_escape
from pathlib import Path
from typing import Any, ClassVar

from astrbot.api import logger

from .config import PluginConfig
from .data import (
    AudioContent,
    DynamicContent,
    FileContent,
    GraphicsContent,
    ImageContent,
    MediaContent,
    ParseResult,
    TextContent,
    VideoContent,
)
from .exception import DownloadException

try:
    from jinja2 import (
        ChoiceLoader,
        Environment,
        FileSystemLoader,
        TemplateNotFound,
        select_autoescape,
    )
    from markupsafe import Markup, escape

    _JINJA_AVAILABLE = True
except ImportError:  # pragma: no cover - 依赖应由 requirements.txt 提供
    ChoiceLoader = Environment = FileSystemLoader = TemplateNotFound = select_autoescape = None  # type: ignore[assignment,misc]
    Markup = str  # type: ignore[assignment,misc]
    _JINJA_AVAILABLE = False

try:
    from playwright.async_api import async_playwright

    _PLAYWRIGHT_AVAILABLE = True
    _PLAYWRIGHT_IMPORT_ERROR: Exception | None = None
except Exception as exc:  # pragma: no cover - requirements.txt 提供依赖
    # Playwright 是可选的卡片能力。缺少依赖时保留原媒体发送流程，
    # 但不切换到其他图片渲染器。
    async_playwright = None  # type: ignore[assignment]
    _PLAYWRIGHT_AVAILABLE = False
    _PLAYWRIGHT_IMPORT_ERROR = exc

try:
    from apilmoji import EmojiCDNSource

    _APILMOJI_AVAILABLE = True
except ImportError:  # pragma: no cover - 可选的表情图片增强
    EmojiCDNSource = None  # type: ignore[assignment,misc]
    _APILMOJI_AVAILABLE = False


# Unicode Emoji（含 ZWJ 序列、变体选择器、肤色、键帽和旗帜 tag 序列）。
# HTML 模板中的 .emoji--apple 类会优先使用 Apple Color Emoji，系统缺少该
# 字体时再回退。键帽/©️/™️ 等是 iOS 分享文案中常见、但不在 1Fxxx 区间的表情。
_EMOJI_BASE = (
    r"(?:[\U0001F1E6-\U0001FAFF\u2600-\u27BF\u00A9\u00AE\u203C\u2049"
    r"\u2122\u2139\u3030\u303D\u3297\u3299]|[0-9#*]\uFE0F?\u20E3)"
)
_EMOJI_SUFFIX = r"(?:[\uFE0E\uFE0F\U000E0020-\U000E007F]|[\U0001F3FB-\U0001F3FF])*"
_EMOJI_CHARACTER = rf"{_EMOJI_BASE}{_EMOJI_SUFFIX}"
_EMOJI_RE = re.compile(
    rf"(?:[\U0001F1E6-\U0001F1FF]{{2}}|{_EMOJI_CHARACTER}(?:\u200d{_EMOJI_CHARACTER})*)"
)


class Renderer:
    """将 ``ParseResult`` 以选中的 HTML 模板渲染为 PNG。

    模板发现顺序（前者可覆盖后者）：

    1. ``<插件数据目录>/templates``，用于用户自定义模板；
    2. ``<插件目录>/templates``，用于随插件发布的模板；
    3. ``core/templates``，内置兜底模板。
    """

    BUILTIN_TEMPLATE_NAMES: ClassVar[tuple[str, ...]] = ("default", "compact")
    _TEMPLATES_DIR: ClassVar[Path] = Path(__file__).with_name("templates")
    _EMOJI_FETCH_TIMEOUT_SECONDS: ClassVar[float] = 3.0
    _BROWSER_VIEWPORT_WIDTH: ClassVar[int] = 760
    # ``full_page=True`` captures the whole document. A short viewport avoids
    # padding compact cards to a large fixed canvas while preserving their width.
    _BROWSER_VIEWPORT_HEIGHT: ClassVar[int] = 100
    _BROWSER_START_TIMEOUT_MS: ClassVar[int] = 30_000
    _PAGE_LOAD_TIMEOUT_MS: ClassVar[int] = 30_000
    _ASSET_WAIT_TIMEOUT_MS: ClassVar[int] = 5_000

    @staticmethod
    def _log_exception(message: str) -> None:
        """兼容 AstrBot 不同版本 logger 的异常日志接口。"""
        log_exception = getattr(logger, "exception", None)
        if callable(log_exception):
            log_exception(message)
        else:
            logger.error(message)

    @staticmethod
    def _log_warning(message: str) -> None:
        log_warning = getattr(logger, "warning", None)
        if callable(log_warning):
            log_warning(message)
        else:
            logger.error(message)

    def __init__(self, config: PluginConfig):
        self.cfg = config
        self.template_dirs = self._template_dirs()
        try:
            self.environment = self._build_environment()
        except Exception as exc:
            # 模板环境初始化属于可选卡片能力，不能阻断解析器和媒体发送。
            self._log_exception(f"Jinja2 卡片模板环境初始化失败: {exc}")
            self.environment = None
        self._emoji_uri_cache: dict[str, str | None] = {}
        self._emoji_path_cache: dict[str, Path | None] = {}
        self._emoji_source = None
        # Playwright browser objects are intentionally kept for the renderer
        # lifetime. Starting a headless shell for every card was the main
        # source of latency in the previous implementation.
        self._playwright: Any | None = None
        self._browser: Any | None = None
        self._browser_context: Any | None = None
        self._browser_lock: asyncio.Lock | None = None
        if _APILMOJI_AVAILABLE and EmojiCDNSource is not None:
            try:
                emoji_style = self._normalized_emoji_style()
                cache_dir = Path(getattr(self.cfg, "cache_dir", self._TEMPLATES_DIR)) / "emojis"
                self._emoji_source = EmojiCDNSource(
                    base_url=str(getattr(self.cfg, "emoji_cdn", "https://emojicdn.elk.sh")),
                    style=emoji_style,
                    cache_dir=cache_dir,
                )
            except Exception as exc:
                # 表情资源是增强项；初始化失败不能让解析插件无法加载。
                self._log_warning(
                    f"Apple Emoji 资源初始化失败，将保留 Unicode 表情: {exc}"
                )
                self._emoji_source = None

    @classmethod
    def load_resources(cls) -> None:
        """保留启动期资源加载接口，浏览器资源由实例生命周期管理。

        原实现会在插件初始化时预加载 Pillow 字体和图片；HTML 方案不再
        需要该 IO。保留方法可让旧的 ``initialize`` 调用保持兼容。
        """
        # 内置模板随插件发布，不在安装目录执行写操作；用户模板目录由
        # PluginConfig 在数据目录中创建。
        return

    @staticmethod
    def _browser_is_connected(browser: Any | None) -> bool:
        if browser is None:
            return False
        try:
            connected = getattr(browser, "is_connected", False)
            return bool(connected() if callable(connected) else connected)
        except Exception:
            return False

    async def _close_browser_unlocked(self) -> None:
        """关闭浏览器资源；调用方负责串行化。"""
        context, browser, playwright = (
            self._browser_context,
            self._browser,
            self._playwright,
        )
        self._browser_context = None
        self._browser = None
        self._playwright = None

        for resource, method_name in (
            (context, "close"),
            (browser, "close"),
            (playwright, "stop"),
        ):
            method = getattr(resource, method_name, None)
            if not callable(method):
                continue
            try:
                await method()
            except Exception as exc:
                self._log_warning(f"关闭 Playwright 资源失败: {exc}")

    async def start(self) -> bool:
        """启动并复用 Chrome Headless Shell。

        Playwright 在 ``headless=True`` 时会选择其安装的
        ``chromium-headless-shell``。浏览器只启动一次，后续卡片共用同一
        个进程和上下文，以避免每张卡片重复付出浏览器启动开销。
        """
        if not _PLAYWRIGHT_AVAILABLE or async_playwright is None:
            logger.error(
                "Playwright 不可用，跳过卡片渲染；请安装依赖并执行 "
                "python -m playwright install chromium-headless-shell: "
                f"{_PLAYWRIGHT_IMPORT_ERROR!r}"
            )
            return False

        if self._browser_is_connected(self._browser) and self._browser_context:
            return True

        if self._browser_lock is None:
            self._browser_lock = asyncio.Lock()

        async with self._browser_lock:
            if self._browser_is_connected(self._browser) and self._browser_context:
                return True
            if self._browser is not None or self._playwright is not None:
                await self._close_browser_unlocked()

            try:
                self._playwright = await async_playwright().start()
                launch_args = ["--disable-dev-shm-usage"]
                # Chromium's sandbox cannot start as root on most Linux images;
                # keep the safer default everywhere else.
                if (
                    os.name != "nt"
                    and hasattr(os, "geteuid")
                    and os.geteuid() == 0
                ):
                    launch_args.append("--no-sandbox")
                self._browser = await self._playwright.chromium.launch(
                    headless=True,
                    args=launch_args,
                    timeout=self._BROWSER_START_TIMEOUT_MS,
                )
                self._browser_context = await self._browser.new_context(
                    viewport={
                        "width": self._BROWSER_VIEWPORT_WIDTH,
                        "height": self._BROWSER_VIEWPORT_HEIGHT,
                    },
                    device_scale_factor=1,
                    color_scheme="light",
                )
                logger.info("Playwright Chrome Headless Shell 已启动")
                return True
            except Exception as exc:
                await self._close_browser_unlocked()
                self._log_exception(
                    "Playwright Chrome Headless Shell 启动失败，跳过卡片渲染；"
                    "请执行 python -m playwright install chromium-headless-shell: "
                    f"{exc}"
                )
                return False

    async def close(self) -> None:
        """在插件卸载时释放复用的浏览器进程。"""
        if self._browser_lock is None:
            await self._close_browser_unlocked()
            return
        async with self._browser_lock:
            await self._close_browser_unlocked()

    @staticmethod
    def _inject_base_url(html: str, base_url: str | None) -> str:
        """让 ``file://`` 页面中的相对模板资源可被 Chrome 读取。"""
        if not base_url or re.search(r"<base\b", html, flags=re.IGNORECASE):
            return html
        try:
            if "://" in base_url:
                uri = base_url
            else:
                uri = Path(base_url).resolve().as_uri()
            if not uri.endswith("/"):
                uri += "/"
            tag = f'<base href="{html_escape(uri, quote=True)}">'
            head = re.search(r"<head\b[^>]*>", html, flags=re.IGNORECASE)
            if head:
                return f"{html[:head.end()]}{tag}{html[head.end():]}"
            return f"{tag}{html}"
        except (OSError, TypeError, ValueError):
            return html

    def _template_dirs(self) -> list[Path]:
        data_dir = getattr(self.cfg, "template_dir", None)
        if data_dir is None:
            data_dir = Path(getattr(self.cfg, "cache_dir", self._TEMPLATES_DIR)) / "templates"
        plugin_dir = Path(getattr(self.cfg, "plugin_dir", self._TEMPLATES_DIR))
        writable_data_dir = Path(data_dir)
        try:
            writable_data_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            # 仍保留该搜索项；若不可写，Jinja2 会继续尝试插件/内置模板。
            self._log_warning(f"用户卡片模板目录不可用: {writable_data_dir} ({exc})")
        candidates = [writable_data_dir, plugin_dir / "templates", self._TEMPLATES_DIR]
        result: list[Path] = []
        for directory in candidates:
            if directory not in result and (
                directory == writable_data_dir or directory.is_dir()
            ):
                result.append(directory)
        return result

    def _build_environment(self):
        if not _JINJA_AVAILABLE:
            return None
        assert ChoiceLoader is not None
        assert Environment is not None
        assert FileSystemLoader is not None
        assert select_autoescape is not None
        environment = Environment(
            loader=ChoiceLoader([FileSystemLoader(str(path)) for path in self.template_dirs]),
            autoescape=select_autoescape(("html", "xml")),
            trim_blocks=True,
            lstrip_blocks=True,
        )
        environment.filters.update(
            format_count=self.format_count,
            emoji=self._emoji_markup,
            file_uri=self._file_uri,
        )
        return environment

    def available_templates(self) -> list[str]:
        """列出当前模板搜索路径中的模板名（不含扩展名）。"""
        names: set[str] = set(self.BUILTIN_TEMPLATE_NAMES)
        for directory in self.template_dirs:
            names.update(path.stem for path in directory.glob("*.html"))
        return sorted(names)

    @staticmethod
    def format_count(value: int | None) -> str:
        """模板使用的紧凑数字格式化器。"""
        if value is None:
            return ""
        if value >= 100_000_000:
            return f"{value / 100_000_000:.1f}".rstrip("0").rstrip(".") + "亿"
        if value >= 10_000:
            return f"{value / 10_000:.1f}".rstrip("0").rstrip(".") + "万"
        return str(value)

    def _normalized_emoji_style(self) -> str:
        """将配置值收敛为安全的 CDN 缓存目录/HTML 类名片段。"""
        return re.sub(
            r"[^a-z0-9_-]",
            "",
            str(getattr(self.cfg, "emoji_style", "APPLE") or "APPLE").lower(),
        ) or "apple"

    @staticmethod
    def _emoji_tokens(value: object) -> set[str]:
        return {matched.group(0) for matched in _EMOJI_RE.finditer(str(value or ""))}

    async def _prepare_emoji_assets(self, result: ParseResult) -> None:
        """预热本地表情图片缓存，失败时仍保留 Unicode 回退。

        网络下载设置了短超时，卡片渲染不会因为某个 CDN 不可达而阻塞
        主发送流程。已经下载过的 Apple/其他风格资源会直接复用。
        """
        if self._emoji_source is None:
            return
        values: list[object] = []

        def collect(item: ParseResult) -> None:
            values.extend((item.platform.display_name, item.title, item.text, item.extra_info))
            if item.author:
                values.extend((item.author.name, item.author.description))
            for content in item.contents:
                values.extend(
                    (getattr(content, "text", None), getattr(content, "alt", None))
                )

        collect(result)
        if result.repost:
            collect(result.repost)
        tokens: set[str] = set()
        for value in values:
            tokens.update(self._emoji_tokens(value))
        tokens = {token for token in tokens if token not in self._emoji_uri_cache}
        if not tokens:
            return

        async def resolve(token: str) -> tuple[str, Path | None]:
            try:
                path = await asyncio.wait_for(
                    self._emoji_source.get_emoji(token),
                    timeout=self._EMOJI_FETCH_TIMEOUT_SECONDS,
                )
                return token, Path(path) if path else None
            except Exception:
                return token, None

        for token, path in await asyncio.gather(*(resolve(token) for token in tokens)):
            self._emoji_path_cache[token] = path
            self._emoji_uri_cache[token] = self._file_uri(path)

    def _emoji_markup(self, value: object) -> Markup:
        """安全地保留 Unicode 表情，并赋予 Apple 风格的字体优先级。

        iOS 分享文本中的组合表情（国旗、肤色、ZWJ 家庭等）会被作为一个
        序列包裹，避免模板转义或 CSS 分词导致表情拆分。未安装 Apple 字体
        的服务器会依次回退到 Segoe/Noto Emoji，不会丢失原始 Unicode。
        """
        text = str(value or "")
        if not _JINJA_AVAILABLE:
            return Markup(text)

        pieces: list[Markup] = []
        start = 0
        style = self._normalized_emoji_style()
        for matched in _EMOJI_RE.finditer(text):
            pieces.append(escape(text[start : matched.start()]))
            classes = f"emoji emoji--{style}"
            token = matched.group(0)
            if uri := self._emoji_uri_cache.get(token):
                pieces.append(
                    Markup(
                        f'<img class="{classes} emoji-image" src="{escape(uri)}" '
                        f'alt="{escape(token)}">'
                    )
                )
            else:
                pieces.append(Markup(f'<span class="{classes}">{escape(token)}</span>'))
            start = matched.end()
        pieces.append(escape(text[start:]))
        return Markup("".join(str(piece) for piece in pieces))

    @staticmethod
    def _file_uri(value: object | None) -> str | None:
        if value is None:
            return None
        try:
            path = Path(value)
            return path.resolve().as_uri() if path.exists() else None
        except (OSError, TypeError, ValueError):
            return None

    @staticmethod
    async def _media_path(content: MediaContent) -> Path | None:
        """卡片只取视频封面；其他可展示媒体取自身图片路径。"""
        try:
            if isinstance(content, VideoContent):
                return await content.get_cover_path()
            if isinstance(content, (ImageContent, GraphicsContent)):
                return await content.get_path()
        except (DownloadException, OSError, RuntimeError):
            return None
        return None

    async def _content_context(self, content: MediaContent) -> dict[str, Any]:
        path = await self._media_path(content)
        kind = "media"
        if isinstance(content, VideoContent):
            kind = "video"
        elif isinstance(content, ImageContent):
            kind = "image"
        elif isinstance(content, GraphicsContent):
            kind = "graphics"
        elif isinstance(content, AudioContent):
            kind = "audio"
        elif isinstance(content, FileContent):
            kind = "file"
        elif isinstance(content, DynamicContent):
            kind = "dynamic"
        elif isinstance(content, TextContent):
            kind = "text"

        return {
            "kind": kind,
            "path": path,
            "uri": self._file_uri(path),
            "text": getattr(content, "text", None),
            "alt": getattr(content, "alt", None),
            "duration": getattr(content, "duration", None),
            "name": getattr(content, "name", None),
        }

    async def _result_context(self, result: ParseResult, *, depth: int = 0) -> dict[str, Any]:
        if depth == 0:
            await self._prepare_emoji_assets(result)
        avatar_path = None
        if result.author:
            try:
                avatar_path = await result.author.get_avatar_path()
            except (DownloadException, OSError, RuntimeError):
                avatar_path = None

        contents = [await self._content_context(content) for content in result.contents]
        stats = result.engagement.as_dict()
        stat_items = [
            {"key": "likes", "label": "点赞", "icon": "♡", "value": stats["likes"]},
            {"key": "comments", "label": "评论", "icon": "◌", "value": stats["comments"]},
            {"key": "favorites", "label": "收藏", "icon": "☆", "value": stats["favorites"]},
            {"key": "shares", "label": "转发", "icon": "↗", "value": stats["shares"]},
        ]
        card: dict[str, Any] = {
            "platform": {"name": result.platform.name, "display_name": result.platform.display_name},
            "author": {
                "name": result.author.name,
                "description": result.author.description,
                "avatar": avatar_path,
                "avatar_uri": self._file_uri(avatar_path),
            }
            if result.author
            else None,
            "title": result.title,
            "text": result.text,
            "timestamp": result.timestamp,
            "datetime": result.formatted_datetime(),
            "url": result.url,
            "extra": result.extra,
            "extra_info": result.extra_info,
            "stats": stats,
            "stat_items": [item for item in stat_items if item["value"] is not None],
            "contents": contents,
            "repost": None,
        }
        # 防止第三方解析器意外构造循环转发对象。
        if result.repost and depth < 1:
            card["repost"] = (
                await self._result_context(result.repost, depth=depth + 1)
            )["card"]
        return {"card": card}

    def _template_name(self) -> str:
        requested = str(getattr(self.cfg, "card_template", "default") or "default").strip()
        if requested == "custom":
            custom_name = str(getattr(self.cfg, "card_custom_template", "") or "").strip()
            # 兼容旧配置直接把 card_template 写成模板名的用法。
            requested = custom_name or requested
        # 禁止从配置注入目录路径；同名的用户模板依然可以被发现。
        requested = Path(requested).name.removesuffix(".html")
        return requested or "default"

    def _template_base_dir(self) -> Path:
        """返回最终使用模板所在目录，供相对静态资源解析。

        ``ChoiceLoader`` 的第一个命中项会覆盖后续目录；这里沿用完全相同
        的顺序。这样用户模板除了 HTML 本身，还可以安全地引用同目录的
        图片、字体或 CSS，而不必把资源复制到数据目录的根部。
        """
        requested = f"{self._template_name()}.html"
        for directory in self.template_dirs:
            if (directory / requested).is_file():
                return directory
        for directory in self.template_dirs:
            if (directory / "default.html").is_file():
                return directory
        return self.template_dirs[0]

    def render_html(self, result: ParseResult, context: dict[str, Any]) -> str:
        """渲染 HTML，公开此方法方便模板开发和单元测试。"""
        if not self.environment:
            raise RuntimeError("Jinja2 不可用，无法渲染卡片模板")

        template_name = f"{self._template_name()}.html"
        try:
            template = self.environment.get_template(template_name)
        except TemplateNotFound as exc:
            logger.error(f"卡片模板不存在，跳过卡片渲染: {template_name}")
            raise RuntimeError(f"卡片模板不存在: {template_name}") from exc

        return template.render(
            result=result,
            card=context["card"],
            # 顶层别名便于用户模板迁移和快速编写。
            platform=context["card"]["platform"],
            author=context["card"]["author"],
            stats=context["card"]["stats"],
            contents=context["card"]["contents"],
            extra=context["card"]["extra"],
            config=self.cfg,
            template_name=self._template_name(),
            emoji_style=str(getattr(self.cfg, "emoji_style", "APPLE") or "APPLE").lower(),
        )

    async def _render_playwright_png(
        self, html: str, target: Path, *, base_url: str | None = None
    ) -> bool:
        """用 Chrome Headless Shell 将渲染后的 HTML 截图为 PNG。

        临时 HTML 与最终 PNG 都位于插件缓存目录。临时 HTML 在本次渲染
        完成后立即删除，最终 PNG 则沿用 ``CacheCleaner`` 的原有清理周期。
        """
        if not await self.start():
            return False
        context = self._browser_context
        if context is None:
            return False

        # Use a hidden unique name so direct callers may keep their own rendered
        # HTML preview next to a PNG without it being removed by this renderer.
        temporary_html = target.with_name(f".{target.stem}_{uuid.uuid4().hex}.html")
        page: Any | None = None
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary_html.write_text(
                self._inject_base_url(html, base_url),
                encoding="utf-8",
            )
            page = await context.new_page()
            await page.goto(
                temporary_html.resolve().as_uri(),
                wait_until="load",
                timeout=self._PAGE_LOAD_TIMEOUT_MS,
            )

            # 等待本地图片和字体完成解码，避免截图偶发出现空白封面。
            for expression, label in (
                (
                    "Array.from(document.images).every((image) => image.complete)",
                    "图片",
                ),
                (
                    "document.fonts ? document.fonts.status === 'loaded' : true",
                    "字体",
                ),
            ):
                try:
                    await page.wait_for_function(
                        expression,
                        timeout=self._ASSET_WAIT_TIMEOUT_MS,
                    )
                except Exception as exc:
                    # 资源超时不阻止截图；浏览器仍会按 HTML 的实际状态输出。
                    self._log_warning(f"等待卡片{label}资源超时，继续截图: {exc}")

            await page.screenshot(
                path=str(target.resolve()),
                type="png",
                full_page=True,
            )
            return target.exists() and target.stat().st_size > 0
        except Exception as exc:
            self._log_exception(f"Playwright PNG 卡片渲染失败: {exc}")
            return False
        finally:
            if page is not None:
                try:
                    await page.close()
                except Exception as exc:
                    self._log_warning(f"关闭卡片渲染页面失败: {exc}")
            try:
                temporary_html.unlink(missing_ok=True)
            except OSError as exc:
                self._log_warning(f"清理卡片临时 HTML 失败: {exc}")

    async def render_card(self, result: ParseResult) -> Path | None:
        """将解析实体渲染为缓存 PNG；失败只返回 ``None``，不影响媒体发送。"""
        render_enabled = getattr(self.cfg, "render_card_enabled", None)
        if render_enabled is None:
            render_enabled = bool(
                getattr(self.cfg, "card_enabled", True)
                and getattr(self.cfg, "card_render_enabled", True)
            )
        else:
            # 兼容只暴露派生属性之外的旧配置对象：总开关始终优先。
            render_enabled = bool(
                render_enabled and getattr(self.cfg, "card_enabled", True)
            )
        if not bool(render_enabled):
            return None
        target: Path | None = None
        try:
            context = await self._result_context(result)
            html = self.render_html(result, context)
            target = self.cfg.cache_dir / f"card_{uuid.uuid4().hex}.png"
            rendered = await self._render_playwright_png(
                html,
                target,
                base_url=str(self._template_base_dir()),
            )
            if not rendered:
                # 严格使用 Playwright。失败时不生成替代卡片，让 sender
                # 继续既有的原媒体发送路径。
                try:
                    target.unlink(missing_ok=True)
                except OSError as cleanup_error:
                    self._log_warning(f"清理失败的卡片文件失败: {cleanup_error}")
                logger.error("卡片渲染失败，已跳过卡片发送")
                result.render_image = None
                return None
            result.render_image = target
            return target
        except Exception as exc:
            if target is not None:
                try:
                    target.unlink(missing_ok=True)
                except OSError as cleanup_error:
                    self._log_warning(f"清理失败的卡片文件失败: {cleanup_error}")
            result.render_image = None
            self._log_exception(f"卡片渲染出现未处理异常，已跳过卡片发送: {exc}")
            return None
