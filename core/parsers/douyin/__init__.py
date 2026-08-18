import re
from asyncio import create_task, gather, to_thread
from dataclasses import dataclass
from pathlib import Path
from random import choice
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import urlencode, urlparse
from uuid import uuid4

import msgspec
from aiohttp import ClientError
from astrbot.api import logger

from ...config import PluginConfig
from ...cookie import CookieJar
from ...data import ImageContent, SendGroup
from ...utils import generate_file_name, safe_unlink
from ..base import (
    BaseParser,
    Downloader,
    ParseException,
    Platform,
    handle,
)

if TYPE_CHECKING:
    from ...data import ParseResult


@dataclass(slots=True)
class ProbedVideo:
    url: str
    size: int
    headers: dict[str, str]


class DouyinParser(BaseParser):
    # 平台信息
    platform: ClassVar[Platform] = Platform(name="douyin", display_name="抖音")
    PLAY_RATIOS: ClassVar[tuple[str, ...]] = ("1080p", "720p", "540p", "360p")
    TTWID_REGISTER_URL: ClassVar[str] = (
        "https://ttwid.bytedance.com/ttwid/union/register/"
    )

    @staticmethod
    def _gallery_send_groups(contents: list, item_count: int) -> list[SendGroup]:
        """为抖音图集保留“卡片在前、媒体按图集折叠”的发送语义。

        信息卡片由 ``MessageSender`` 针对整个 ``ParseResult`` 独立发送；这里
        仅明确图集媒体的合并方式。以原始作品项数判断，而不是以解析后的媒体
        段数判断，确保单张实况图在封装或静态回退时都不会被折叠。
        """
        if not contents:
            return []
        return [SendGroup(contents=contents, force_merge=item_count > 1)]

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.mycfg = config.parser.douyin
        self.cookiejar = CookieJar(config, self.mycfg, domain="douyin.com")
        self._set_cookies()

    def _set_cookies(self, cookies_str: str = ""):
        """设置cookie到请求头"""
        cookies_str = cookies_str or self.cookiejar.cookies_str
        if cookies_str:
            self.ios_headers["Cookie"] = cookies_str
            self.android_headers["Cookie"] = cookies_str

    def _sync_headers_for_url(self, url: str) -> dict[str, str]:
        headers = self.ios_headers.copy()
        headers.pop("Cookie", None)
        if cookies_str := self.cookiejar.get_cookie_header_for_url(url):
            headers["Cookie"] = cookies_str
        elif self._is_iesdouyin_url(url):
            if cookies_str := self.cookiejar.get_cookie_header(domain="iesdouyin.com"):
                headers["Cookie"] = cookies_str
        return headers

    @staticmethod
    def _is_iesdouyin_url(url: str) -> bool:
        hostname = urlparse(url).hostname or ""
        return hostname == "iesdouyin.com" or hostname.endswith(".iesdouyin.com")

    def _has_ttwid(self) -> bool:
        cookies = self.cookiejar.get(domain="iesdouyin.com") or {}
        return bool(cookies.get("ttwid"))

    # https://v.douyin.com/_2ljF4AmKL8
    @handle("v.douyin", r"v\.douyin\.com/[a-zA-Z0-9_\-]+")
    @handle("jx.douyin", r"jx\.douyin\.com/[a-zA-Z0-9_\-]+")
    async def _parse_short_link(self, searched: re.Match[str]):
        url = f"https://{searched.group(0)}"
        return await self.parse_with_redirect(url)

    # https://www.douyin.com/video/7521023890996514083
    # https://www.douyin.com/note/7469411074119322899
    @handle("", r"(?<![A-Za-z0-9_/=:%?&.-])(?P<vid>\d{18,20})(?!\d)")
    @handle("aweme_id", r"aweme_id[=:/\s]+(?P<vid>\d{10,})")
    @handle("aweme", r"aweme/(?P<vid>\d{10,})")
    @handle("douyin", r"douyin\.com/(?P<ty>video|note)/(?P<vid>\d+)")
    @handle("iesdouyin", r"iesdouyin\.com/share/(?P<ty>slides|video|note)/(?P<vid>\d+)")
    @handle("m.douyin", r"m\.douyin\.com/share/(?P<ty>slides|video|note)/(?P<vid>\d+)")
    # https://jingxuan.douyin.com/m/video/7574300896016862490?app=yumme&utm_source=copy_link
    @handle(
        "jingxuan.douyin",
        r"jingxuan\.douyin.com/m/(?P<ty>slides|video|note)/(?P<vid>\d+)",
    )
    async def _parse_douyin(self, searched: re.Match[str]):
        ty = searched.groupdict().get("ty") or "video"
        vid = searched.group("vid")
        logger.debug(f"[抖音] 解析类型: {ty}, ID: {vid}")
        if ty == "slides":
            return await self.parse_slides(vid)

        await self.ensure_ttwid()
        share_url = self._build_iesdouyin_url(ty, vid)
        logger.debug(f"[抖音] 使用 canonical share 页解析: {share_url}")

        try:
            return await self.parse_video(share_url)
        except ParseException as e:
            logger.warning(f"[抖音] canonical share 页解析失败 {share_url}, 错误: {e}")
            raise ParseException("分享已删除或资源直链提取失败, 请稍后再试") from e

    @staticmethod
    def _build_iesdouyin_url(ty: str, vid: str) -> str:
        return f"https://www.iesdouyin.com/share/{ty}/{vid}/"

    @staticmethod
    def _build_m_douyin_url(ty: str, vid: str) -> str:
        return f"https://m.douyin.com/share/{ty}/{vid}/"

    async def ensure_ttwid(self) -> None:
        if self._has_ttwid():
            return

        logger.debug("[抖音] 当前缺少匿名 ttwid，尝试注册")
        headers = self.ios_headers.copy()
        headers.update(
            {
                "Content-Type": "application/json",
                "Referer": "https://www.iesdouyin.com/",
            }
        )
        payload = {
            "region": "cn",
            "aid": 1768,
            "needFid": False,
            "service": "www.iesdouyin.com",
            "union": True,
            "fid": "",
        }
        try:
            async with self.session.post(
                self.TTWID_REGISTER_URL,
                json=payload,
                headers=headers,
            ) as resp:
                if resp.status >= 400:
                    raise ParseException(f"ttwid register status: {resp.status}")
                set_cookie_headers = resp.headers.getall("Set-Cookie", [])
                self.cookiejar.update_from_response(set_cookie_headers)
                self._set_cookies()
                body = await resp.json(content_type=None)
        except (ClientError, TimeoutError, ValueError) as e:
            raise ParseException("ttwid register failed") from e

        if not isinstance(body, dict):
            raise ParseException("ttwid register returned invalid body")

        if callback_url := body.get("redirect_url"):
            callback_headers = self._sync_headers_for_url(callback_url)
            callback_headers["Referer"] = "https://www.iesdouyin.com/"
            try:
                async with self.session.get(
                    callback_url,
                    headers=callback_headers,
                    allow_redirects=False,
                ) as resp:
                    if resp.status >= 400:
                        raise ParseException(f"ttwid callback status: {resp.status}")
                    set_cookie_headers = resp.headers.getall("Set-Cookie", [])
                    self.cookiejar.update_from_response(set_cookie_headers)
                    self._set_cookies()
            except (ClientError, TimeoutError) as e:
                raise ParseException("ttwid callback failed") from e

        if not self._has_ttwid():
            raise ParseException("ttwid register returned no cookie")

    async def parse_with_redirect(self, url: str) -> "ParseResult":
        """先重定向再解析，并更新 cookies"""
        logger.debug(f"[抖音] 短链重定向请求: {url}")
        async with self.session.get(
            url, headers=self.ios_headers, allow_redirects=False
        ) as resp:
            logger.debug(f"[抖音] 短链重定向响应状态码: {resp.status}")
            # 从响应中提取 Set-Cookie 并更新
            set_cookie_headers = resp.headers.getall("Set-Cookie", [])
            self.cookiejar.update_from_response(set_cookie_headers)
            self._set_cookies()

            # 只有在状态码是重定向状态码时才获取 Location
            redirect_url = url
            if resp.status in (301, 302, 303, 307, 308):
                redirect_url = resp.headers.get("Location", url)
                logger.debug(f"[抖音] 重定向到: {redirect_url}")

        if redirect_url == url:
            raise ParseException(f"无法重定向 URL: {url}")

        keyword, searched = self.search_url(redirect_url)
        return await self.parse(keyword, searched)

    async def parse_video(self, url: str):
        await self.ensure_ttwid()
        share_headers = self._sync_headers_for_url(url)
        async with self.session.get(
            url, headers=share_headers, allow_redirects=False
        ) as resp:
            if resp.status != 200:
                raise ParseException(f"status: {resp.status}")
            text = await resp.text()
            set_cookie_headers = resp.headers.getall("Set-Cookie", [])
            self.cookiejar.update_from_response(set_cookie_headers)
            self._set_cookies()

        pattern = re.compile(
            pattern=r"window\._ROUTER_DATA\s*=\s*(.*?)</script>",
            flags=re.DOTALL,
        )
        matched = pattern.search(text)

        if not matched or not matched.group(1):
            logger.debug("[抖音] 未在HTML中找到 window._ROUTER_DATA")
            raise ParseException("can't find _ROUTER_DATA in html")

        logger.debug("[抖音] 成功提取 window._ROUTER_DATA")

        from .video import RouterData

        video_data = msgspec.json.decode(
            matched.group(1).strip(), type=RouterData
        ).video_data
        aweme_id = self._extract_aweme_id(url)
        detail_data = None
        if aweme_id and video_data.images:
            detail_data = await self.fetch_signed_aweme_detail(aweme_id)
            if detail_data and detail_data.images:
                has_richer_images = any(
                    image.clip_type is not None or image.video
                    for image in detail_data.images
                )
                if has_richer_images:
                    logger.info(
                        "[抖音] 已使用登录详情接口补全图文媒体数据: "
                        f"图片={len(detail_data.images)}"
                    )
                    video_data.images = detail_data.images
        logger.debug(
            f"[抖音] 解析成功 - 作者: {video_data.author.nickname}, 描述: {video_data.desc[:50]}..."
        )
        # 使用新的简洁构建方式
        contents = []
        send_groups: list[SendGroup] = []

        # 添加图片内容
        if video_data.images:
            logger.debug(f"[抖音] 检测到图文内容，图片数量: {len(video_data.images)}")
            contents.extend(
                self._create_douyin_image_contents(
                    video_data.images,
                    headers=self.ios_headers,
                    referer=url,
                )
            )
            send_groups = self._gallery_send_groups(contents, len(video_data.images))

        # 添加视频内容
        elif video_data.video:
            cover_url = video_data.cover_url
            duration = video_data.video.duration if video_data.video else 0
            logger.debug(f"[抖音] 检测到视频内容，时长: {duration}秒")
            video_headers = self._build_media_headers(url)
            video_url = None
            if play_token := video_data.play_token:
                try:
                    probed = await self.probe_video_url(play_token, url)
                    video_url = probed.url
                    video_headers = probed.headers
                    logger.debug(
                        f"[抖音] play 端点探测成功，文件大小: {probed.size} 字节"
                    )
                except ParseException as e:
                    logger.warning(f"[抖音] play 端点探测失败，回退 play_addr: {e}")
            video_url = video_url or video_data.video_url
            if video_url:
                contents.append(
                    self.create_video_content(
                        video_url, cover_url, duration, headers=video_headers
                    )
                )

        # 构建作者
        author = self.create_author(
            video_data.author.nickname, video_data.avatar_url, headers=self.ios_headers
        )
        raw_stats = video_data.statistics or (
            detail_data.statistics if detail_data else None
        )
        engagement = self.engagement_from_mapping(
            {
                "like": getattr(raw_stats, "digg_count", None),
                "comment": getattr(raw_stats, "comment_count", None),
                "favorite": getattr(raw_stats, "collect_count", None),
                "share": getattr(raw_stats, "share_count", None),
            }
        )

        return self.result(
            title=video_data.desc,
            author=author,
            contents=contents,
            send_groups=send_groups,
            timestamp=video_data.create_time,
            like_count=engagement.likes,
            comment_count=engagement.comments,
            favorite_count=engagement.favorites,
            share_count=engagement.shares,
            extra=self._motion_photo_extra(video_data.images),
        )

    @staticmethod
    def _extract_aweme_id(url: str) -> str | None:
        if matched := re.search(r"/(?:video|note)/(?P<vid>\d{10,})", url):
            return matched.group("vid")
        return None

    @staticmethod
    def _motion_photo_extra(images: list[Any] | None) -> dict[str, bool]:
        if any(image.clip_type == 5 for image in images or []):
            return {"has_motion_photo": True}
        return {}

    async def fetch_signed_aweme_detail(self, aweme_id: str):
        """使用已配置的登录 Cookie 获取完整图文与实况媒体字段。"""
        desktop_url = "https://www.douyin.com/"
        if not self.cookiejar.get_cookie_header_for_url(desktop_url):
            logger.info("[抖音] 未配置登录 Cookie，跳过图文详情补全")
            return None

        from .signature import generate_a_bogus, generate_ms_token, generate_verify_fp
        from .video import AwemeDetailRes

        user_agent = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 "
            "Safari/537.36 Edg/130.0.0.0"
        )
        for attempt in range(2):
            cookie_header = self.cookiejar.get_cookie_header_for_url(desktop_url)
            cookies = self.cookiejar.get(domain="www.douyin.com")
            verify_fp = cookies.get("s_v_web_id") or generate_verify_fp()
            params = {
                "device_platform": "webapp",
                "aid": "6383",
                "channel": "channel_pc_web",
                "pc_client_type": "1",
                "publish_video_strategy_type": "2",
                "pc_libra_divert": "Windows",
                "version_code": "290100",
                "version_name": "29.1.0",
                "cookie_enabled": "true",
                "screen_width": "1920",
                "screen_height": "1080",
                "browser_language": "zh-CN",
                "browser_platform": "Win32",
                "browser_name": "Edge",
                "browser_version": "130.0.0.0",
                "browser_online": "true",
                "engine_name": "Blink",
                "engine_version": "130.0.0.0",
                "os_name": "Windows",
                "os_version": "10",
                "cpu_core_num": "12",
                "device_memory": "8",
                "platform": "PC",
                "downlink": "10",
                "effective_type": "4g",
                "round_trip_time": "100",
                "msToken": cookies.get("msToken") or generate_ms_token(),
                "verifyFp": verify_fp,
                "fp": verify_fp,
                "aweme_id": aweme_id,
            }
            query = urlencode(params)
            try:
                signature = generate_a_bogus(query, user_agent)
            except (RuntimeError, ValueError) as e:
                logger.warning(f"[抖音] 详情接口签名生成失败，保留分享页数据: {e}")
                return None

            url = (
                "https://www.douyin.com/aweme/v1/web/aweme/detail/"
                f"?{query}&a_bogus={signature}"
            )
            headers = self.headers.copy()
            headers.update(
                {
                    "Accept": "application/json, text/plain, */*",
                    "Referer": f"https://www.douyin.com/note/{aweme_id}",
                    "User-Agent": user_agent,
                    "Cookie": cookie_header,
                }
            )

            failure_reason: str | None = None
            try:
                async with self.session.get(url, headers=headers) as resp:
                    status = resp.status
                    body = await resp.read()
                    self.cookiejar.update_from_response(
                        resp.headers.getall("Set-Cookie", [])
                    )
                    self._set_cookies()
            except (ClientError, TimeoutError) as e:
                logger.info(f"[抖音] 登录详情接口请求失败，保留分享页数据: {e}")
                return None

            response = None
            if status >= 400:
                failure_reason = f"HTTP {status}"
            elif not body:
                failure_reason = "空响应"
            else:
                try:
                    response = msgspec.json.decode(body, type=AwemeDetailRes)
                except msgspec.DecodeError:
                    failure_reason = "响应无法解析"

            if response is not None:
                if response.status_code == 0 and response.aweme_detail:
                    if attempt:
                        logger.info("[抖音] Web 会话恢复后详情接口重试成功")
                    return response.aweme_detail
                failure_reason = f"无作品数据, status_code={response.status_code}"

            if attempt == 0:
                logger.info(
                    f"[抖音] 登录详情接口返回{failure_reason}，"
                    "尝试初始化 Web 会话后重试"
                )
                if await self._initialize_web_session(aweme_id, user_agent):
                    continue
                logger.info("[抖音] Web 会话初始化失败，保留分享页静态数据")
                return None

            logger.info(
                f"[抖音] 登录详情接口重试后仍返回{failure_reason}，"
                "保留分享页静态数据"
            )
            return None

        return None

    async def _initialize_web_session(self, aweme_id: str, user_agent: str) -> bool:
        """访问一次桌面作品页并持久化刷新后的 Web Cookie。"""
        page_url = f"https://www.douyin.com/note/{aweme_id}"
        headers = self.headers.copy()
        headers.update(
            {
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "image/avif,image/webp,*/*;q=0.8"
                ),
                "Referer": "https://www.douyin.com/",
                "User-Agent": user_agent,
            }
        )
        if cookie_header := self.cookiejar.get_cookie_header_for_url(page_url):
            headers["Cookie"] = cookie_header

        try:
            async with self.session.get(
                page_url,
                headers=headers,
                allow_redirects=True,
            ) as resp:
                if resp.status >= 400:
                    logger.info(
                        f"[抖音] Web 会话初始化请求不可用，状态码: {resp.status}"
                    )
                    return False
                await resp.read()
                responses = (*resp.history, resp)
                set_cookie_headers = [
                    header
                    for response in responses
                    for header in response.headers.getall("Set-Cookie", [])
                ]
        except (ClientError, TimeoutError) as e:
            logger.info(f"[抖音] Web 会话初始化请求失败: {e}")
            return False

        self.cookiejar.update_from_response(set_cookie_headers)
        self._set_cookies()
        logger.info("[抖音] Web 会话初始化完成")
        return True

    @staticmethod
    def _build_play_url(video_id: str, ratio: str) -> str:
        return (
            f"https://aweme.snssdk.com/aweme/v1/play/?video_id={video_id}&ratio={ratio}"
        )

    def _build_media_headers(self, referer: str) -> dict[str, str]:
        headers = self.ios_headers.copy()
        headers.pop("Cookie", None)
        headers["Referer"] = referer
        return headers

    def _create_douyin_image_contents(
        self,
        images: list[Any],
        *,
        headers: dict[str, str],
        referer: str,
    ) -> list[ImageContent]:
        """创建普通图片或已封装的抖音实况图下载任务。"""
        contents: list[ImageContent] = []
        clip_types = [image.clip_type for image in images]
        live_count = sum(clip_type == 5 for clip_type in clip_types)
        video_source_count = sum(
            self._motion_photo_video_url(image) is not None for image in images
        )
        logger.info(
            "[抖音] 图文媒体检测: "
            f"图片={len(images)}, clip_type={clip_types}, "
            f"实况图={live_count}, 可用视频源={video_source_count}"
        )

        for index, image in enumerate(images):
            if not image.url_list:
                continue

            image_url = (
                self._motion_photo_cover_url(image.url_list)
                if image.clip_type == 5
                else choice(image.url_list)
            )
            video_url = self._motion_photo_video_url(image)
            if image.clip_type == 5 and video_url:
                logger.info(f"[抖音] 检测到实况图，开始封装: index={index}")
                task = create_task(
                    self._download_motion_photo(
                        image_url,
                        video_url,
                        headers=headers,
                        referer=referer,
                    ),
                    name=f"douyin_motion_photo_{index}",
                )
                contents.append(ImageContent(task))
                continue

            if image.clip_type == 5:
                logger.warning(
                    f"[抖音] 实况图缺少视频地址，回退发送静态图: index={index}"
                )

            task = self.downloader.download_img(
                image_url,
                headers=headers,
                proxy=self.proxy,
            )
            contents.append(ImageContent(task))
        return contents

    @staticmethod
    def _motion_photo_cover_url(urls: list[str]) -> str:
        for url in urls:
            if urlparse(url).path.lower().endswith((".jpg", ".jpeg")):
                return url
        return urls[0]

    def _motion_photo_video_url(self, image: Any) -> str | None:
        if not image.video:
            return None

        addresses = [image.video.play_addr_h264, image.video.play_addr]
        for address in addresses:
            if not address:
                continue
            if address.uri:
                play_url = self._build_play_url(address.uri, self.PLAY_RATIOS[0])
                return f"{play_url}&line=0"
            if address.url_list:
                return address.url_list[0]
        return None

    @staticmethod
    async def _cleanup_motion_photo_files(
        paths: set[Path],
        *,
        reason: str,
    ) -> None:
        if not paths:
            return

        await gather(*(safe_unlink(path) for path in paths))
        remaining = [path.name for path in paths if path.exists()]
        if remaining:
            logger.warning(
                f"[抖音] {reason}，Motion Photo 中间文件未能完全清理: "
                + ", ".join(remaining)
            )
            return
        logger.info(
            f"[抖音] {reason}，Motion Photo 中间文件清理完成: "
            f"数量={len(paths)}"
        )

    async def _download_motion_photo(
        self,
        image_url: str,
        video_url: str,
        *,
        headers: dict[str, str],
        referer: str,
    ) -> Path:
        cache_key = f"{image_url}|{video_url}"
        cache_stem = Path(generate_file_name(cache_key)).stem
        output_path = self.cfg.cache_dir / f"motion_{cache_stem}.jpg"
        if output_path.exists():
            return output_path

        work_id = uuid4().hex
        image_task = self.downloader.download_img(
            image_url,
            img_name=f".motion_{cache_stem}_{work_id}_cover.jpg",
            headers=headers,
            proxy=self.proxy,
        )
        video_task = self.downloader.download_video(
            video_url,
            video_name=f".motion_{cache_stem}_{work_id}_clip.mp4",
            headers=self._build_media_headers(referer),
            proxy=self.proxy,
        )
        image_result, video_result = await gather(
            image_task,
            video_task,
            return_exceptions=True,
        )

        if isinstance(image_result, BaseException):
            if isinstance(video_result, Path):
                await self._cleanup_motion_photo_files(
                    {video_result},
                    reason="静态封面下载失败",
                )
            raise image_result
        if isinstance(video_result, BaseException):
            logger.warning(
                f"[抖音] 实况片段下载失败，回退发送静态图: {video_result}"
            )
            return image_result

        from .motion_photo import build_motion_photo

        try:
            result = await to_thread(
                build_motion_photo,
                image_result,
                video_result,
                output_path,
            )
        except (OSError, ValueError) as e:
            logger.warning(f"[抖音] Motion Photo 封装失败，回退发送静态图: {e}")
            await self._cleanup_motion_photo_files(
                {video_result},
                reason="Motion Photo 封装失败",
            )
            return image_result

        logger.info(f"[抖音] Motion Photo 封装完成: {result.name}")
        intermediate_paths = {
            path for path in (image_result, video_result) if path != result
        }
        await self._cleanup_motion_photo_files(
            intermediate_paths,
            reason="Motion Photo 封装成功",
        )
        return result

    async def probe_video_url(self, video_id: str, referer: str) -> ProbedVideo:
        probed_by_size: dict[int, ProbedVideo] = {}

        for ratio in self.PLAY_RATIOS:
            play_url = self._build_play_url(video_id, ratio)
            headers = self._build_media_headers(referer)
            headers["Range"] = "bytes=0-1"
            try:
                async with self.session.get(
                    play_url,
                    headers=headers,
                    allow_redirects=True,
                ) as resp:
                    if resp.status >= 400:
                        logger.debug(
                            f"[抖音] ratio={ratio} 探测失败，状态码: {resp.status}"
                        )
                        continue
                    size = self._extract_response_size(resp.headers)
                    if size <= 0:
                        logger.debug(f"[抖音] ratio={ratio} 未拿到有效文件大小")
                        continue
                    final_url = str(resp.url)
            except (ClientError, TimeoutError) as e:
                logger.debug(f"[抖音] ratio={ratio} 探测请求失败: {e}")
                continue

            probed_by_size.setdefault(
                size, ProbedVideo(final_url, size, self._build_media_headers(referer))
            )

        if not probed_by_size:
            raise ParseException("can't probe play endpoint")

        return max(probed_by_size.values(), key=lambda item: item.size)

    @staticmethod
    def _extract_response_size(headers) -> int:
        if content_range := headers.get("Content-Range"):
            if matched := re.search(r"/(\d+)\s*$", content_range):
                return int(matched.group(1))
        if content_length := headers.get("Content-Length"):
            try:
                return int(content_length)
            except ValueError:
                return 0
        return 0

    async def parse_slides(self, video_id: str):
        url = "https://www.iesdouyin.com/web/api/v2/aweme/slidesinfo/"
        params = {
            "aweme_ids": f"[{video_id}]",
            "request_source": "200",
        }
        logger.debug(f"[抖音] 请求参数: {params}")
        async with self.session.get(
            url, params=params, headers=self.android_headers
        ) as resp:
            logger.debug(f"[抖音] 幻灯片API响应状态码: {resp.status}")
            resp.raise_for_status()
            # 从响应中提取 Set-Cookie 并更新
            set_cookie_headers = resp.headers.getall("Set-Cookie", [])
            self.cookiejar.update_from_response(set_cookie_headers)
            self._set_cookies()

            from .slides import SlidesInfo

            response_text = await resp.read()
            logger.debug(f"[抖音] 幻灯片API响应体大小: {len(response_text)} 字节")
            slides_data = msgspec.json.decode(
                response_text, type=SlidesInfo
            ).aweme_details[0]
        logger.debug(
            f"[抖音] 幻灯片解析成功 - 作者: {slides_data.name}, 描述: {slides_data.desc[:50]}..."
        )
        detail_data = None
        if slides_data.images:
            detail_data = await self.fetch_signed_aweme_detail(video_id)
            if detail_data and detail_data.images:
                has_richer_images = any(
                    image.clip_type == 5 or image.video
                    for image in detail_data.images
                )
                if has_richer_images:
                    slides_data.images = detail_data.images
                    logger.info(
                        "[抖音] 已使用登录详情接口补全幻灯片媒体数据: "
                        f"图片={len(detail_data.images)}"
                    )
        contents = []

        # 添加图片内容
        if slides_data.images:
            logger.debug(f"[抖音] 检测到幻灯片图片，数量: {len(slides_data.images)}")
            contents.extend(
                self._create_douyin_image_contents(
                    slides_data.images,
                    headers=self.android_headers,
                    referer=self._build_iesdouyin_url("slides", video_id),
                )
            )

        # 添加动态内容
        if dynamic_urls := slides_data.dynamic_urls:
            logger.debug(f"[抖音] 检测到幻灯片动态效果，数量: {len(dynamic_urls)}")
            contents.extend(
                self.create_dynamic_contents(dynamic_urls, headers=self.android_headers)
            )

        # 构建作者
        author = self.create_author(
            slides_data.name, slides_data.avatar_url, headers=self.android_headers
        )
        raw_stats = slides_data.statistics or (
            detail_data.statistics if detail_data else None
        )
        engagement = self.engagement_from_mapping(
            {
                "like": getattr(raw_stats, "digg_count", None),
                "comment": getattr(raw_stats, "comment_count", None),
                "favorite": getattr(raw_stats, "collect_count", None),
                "share": getattr(raw_stats, "share_count", None),
            }
        )

        return self.result(
            title=slides_data.desc,
            author=author,
            contents=contents,
            send_groups=self._gallery_send_groups(
                contents, len(slides_data.images)
            ),
            timestamp=slides_data.create_time,
            like_count=engagement.likes,
            comment_count=engagement.comments,
            favorite_count=engagement.favorites,
            share_count=engagement.shares,
            extra=self._motion_photo_extra(slides_data.images),
        )
