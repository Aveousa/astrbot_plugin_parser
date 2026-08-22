from random import choice
from typing import Any
from urllib.parse import parse_qs, urlparse

from msgspec import Struct, field

from ..base import ParseException


def is_legacy_play_url(url: str) -> bool:
    """Return whether *url* is the obsolete unsigned ``video_id`` endpoint."""
    parsed = urlparse(url)
    if not parsed.path.rstrip("/").endswith("/aweme/v1/play"):
        return False
    query = parse_qs(parsed.query)
    # The detail endpoint may use the same path with file_id/sign.  Those are
    # signed media URLs and must remain valid candidates.
    return "video_id" in query and not {"file_id", "sign"}.issubset(query)


class Avatar(Struct):
    url_list: list[str]


class Author(Struct):
    nickname: str
    avatar_thumb: Avatar | None = None
    avatar_medium: Avatar | None = None


class Statistics(Struct):
    """抖音作品互动统计（字段可能随接口版本缺省）。"""

    digg_count: int | str | None = None
    comment_count: int | str | None = None
    collect_count: int | str | None = None
    share_count: int | str | None = None


class PlayAddr(Struct):
    uri: str | None = None
    url_list: list[str] = field(default_factory=list)


class BitRate(Struct):
    play_addr: PlayAddr | None = None


class Cover(Struct):
    url_list: list[str] = field(default_factory=list)


class Video(Struct):
    play_addr: PlayAddr = field(default_factory=PlayAddr)
    play_addr_h264: PlayAddr | None = None
    play_addr_265: PlayAddr | None = None
    download_addr: PlayAddr | None = None
    bit_rate: list[BitRate] = field(default_factory=list)
    cover: Cover = field(default_factory=Cover)
    duration: int = 0


class Image(Struct):
    video: Video | None = None
    url_list: list[str] = field(default_factory=list)
    clip_type: int | None = None


class VideoData(Struct):
    create_time: int
    author: Author
    desc: str
    images: list[Image] | None = None
    video: Video | None = None
    statistics: Statistics | None = None

    @property
    def image_urls(self) -> list[str]:
        return [choice(image.url_list) for image in self.images] if self.images else []

    @property
    def video_url(self) -> str | None:
        if not self.video:
            return None
        # 网页详情接口可能同时返回 CDN 直链、playwm 和 download_addr；
        # 按可用性尝试，避免把已失效的旧 video_id 端点当成唯一来源。
        addresses = (
            self.video.play_addr_h264,
            self.video.play_addr,
            self.video.download_addr,
            self.video.play_addr_265,
            *(item.play_addr for item in self.video.bit_rate),
        )
        legacy_urls: list[str] = []
        for address in addresses:
            if address and address.url_list:
                urls = [url.replace("playwm", "play") for url in address.url_list]
                usable_urls = [url for url in urls if not is_legacy_play_url(url)]
                if usable_urls:
                    return choice(usable_urls)
                legacy_urls.extend(urls)
        # Preserve the historic fallback for callers that still probe this
        # endpoint for a fresh URL, but never prefer it to a signed CDN URL.
        return choice(legacy_urls) if legacy_urls else None

    @property
    def play_token(self) -> str | None:
        if not self.video:
            return None

        for play_addr in (
            self.video.play_addr_h264,
            self.video.play_addr,
            self.video.download_addr,
            self.video.play_addr_265,
            *(item.play_addr for item in self.video.bit_rate),
        ):
            if not play_addr:
                continue
            if play_addr.uri:
                return play_addr.uri
            for url in play_addr.url_list:
                query = parse_qs(urlparse(url).query)
                if video_id := query.get("video_id"):
                    return video_id[0]
        return None

    @property
    def cover_url(self) -> str | None:
        return choice(self.video.cover.url_list) if self.video else None

    @property
    def avatar_url(self) -> str | None:
        if avatar := self.author.avatar_thumb:
            return choice(avatar.url_list)
        elif avatar := self.author.avatar_medium:
            return choice(avatar.url_list)
        return None


class VideoInfoRes(Struct):
    item_list: list[VideoData] = field(default_factory=list)

    @property
    def video_data(self) -> VideoData:
        if len(self.item_list) == 0:
            raise ParseException("can't find data in videoInfoRes")
        return choice(self.item_list)


class AwemeDetailRes(Struct):
    """抖音 Web 详情接口响应，用于补全实况图媒体字段。"""

    status_code: int = 0
    aweme_detail: VideoData | None = None


class VideoOrNotePage(Struct):
    video_info_res: VideoInfoRes = field(
        name="videoInfoRes", default_factory=VideoInfoRes
    )


class LoaderData(Struct):
    video_page: VideoOrNotePage | None = field(name="video_(id)/page", default=None)
    note_page: VideoOrNotePage | None = field(name="note_(id)/page", default=None)


class RouterData(Struct):
    loader_data: LoaderData = field(name="loaderData", default_factory=LoaderData)
    errors: dict[str, Any] | None = None

    @property
    def video_data(self) -> VideoData:
        if page := self.loader_data.video_page:
            return page.video_info_res.video_data
        elif page := self.loader_data.note_page:
            return page.video_info_res.video_data
        raise ParseException(
            "can't find video_(id)/page or note_(id)/page in router data"
        )
