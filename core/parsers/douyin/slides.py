from random import choice

from msgspec import Struct, field


class PlayAddr(Struct):
    uri: str | None = None
    url_list: list[str] = field(default_factory=list)


class Cover(Struct):
    url_list: list[str] = field(default_factory=list)


class Video(Struct):
    play_addr: PlayAddr = field(default_factory=PlayAddr)
    play_addr_h264: PlayAddr | None = None
    cover: Cover = field(default_factory=Cover)
    duration: int = 0


class Image(Struct):
    video: Video | None = None
    url_list: list[str] = field(default_factory=list)
    clip_type: int | None = None


class Avatar(Struct):
    url_list: list[str]


class Author(Struct):
    nickname: str
    # avatar_larger: Avatar
    avatar_thumb: Avatar


class Statistics(Struct):
    digg_count: int | str | None = None
    comment_count: int | str | None = None
    collect_count: int | str | None = None
    share_count: int | str | None = None


class SlidesData(Struct):
    author: Author
    desc: str
    create_time: int
    images: list[Image]
    statistics: Statistics | None = None

    @property
    def name(self) -> str:
        return self.author.nickname

    @property
    def avatar_url(self) -> str:
        return choice(self.author.avatar_thumb.url_list)

    @property
    def image_urls(self) -> list[str]:
        return [choice(image.url_list) for image in self.images]

    @property
    def dynamic_urls(self) -> list[str]:
        return [
            choice(image.video.play_addr.url_list)
            for image in self.images
            if image.video
            and image.clip_type != 5
            and image.video.play_addr.url_list
        ]


class SlidesInfo(Struct):
    aweme_details: list[SlidesData] = field(default_factory=list)
