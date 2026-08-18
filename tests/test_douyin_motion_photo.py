import asyncio
from pathlib import Path
from types import SimpleNamespace

import msgspec
import pytest

from core.parsers.douyin import DouyinParser, signature
from core.parsers.douyin.motion_photo import XMP_HEADER, build_motion_photo
from core.parsers.douyin.slides import Image as SlidesImage
from core.parsers.douyin.slides import PlayAddr as SlidesPlayAddr
from core.parsers.douyin.slides import Video as SlidesVideo
from core.parsers.douyin.video import AwemeDetailRes, Image, PlayAddr, Video


def test_build_motion_photo_injects_xmp_and_appends_video(tmp_path: Path):
    jpeg = b"\xff\xd8\xff\xdbjpeg-data\xff\xd9"
    video = b"\x00\x00\x00\x18ftypmp42mp4-data"
    image_path = tmp_path / "cover.jpg"
    video_path = tmp_path / "live.mp4"
    output_path = tmp_path / "motion.jpg"
    image_path.write_bytes(jpeg)
    video_path.write_bytes(video)

    result = build_motion_photo(image_path, video_path, output_path)

    output = result.read_bytes()
    assert output[:4] == b"\xff\xd8\xff\xe1"
    app1_length = int.from_bytes(output[4:6], "big")
    assert output[6 : 6 + app1_length - 2].startswith(XMP_HEADER)
    assert b'GCamera:MotionPhoto="1"' in output
    assert f'Item:Length="{len(video)}"'.encode() in output
    assert output.endswith(video)


def test_build_motion_photo_rejects_non_jpeg_cover(tmp_path: Path):
    image_path = tmp_path / "cover.webp"
    video_path = tmp_path / "live.mp4"
    output_path = tmp_path / "motion.jpg"
    image_path.write_bytes(b"RIFF-not-a-jpeg")
    video_path.write_bytes(b"video")

    with pytest.raises(ValueError, match="not a JPEG"):
        build_motion_photo(image_path, video_path, output_path)

    assert not output_path.exists()


def test_motion_photo_video_url_prefers_h264_uri():
    parser = object.__new__(DouyinParser)
    image = Image(
        clip_type=5,
        url_list=["https://example.com/cover.jpeg"],
        video=Video(
            play_addr=PlayAddr(url_list=["https://example.com/fallback.mp4"]),
            play_addr_h264=PlayAddr(uri="live-video-id"),
        ),
    )

    assert parser._motion_photo_video_url(image) == (
        "https://aweme.snssdk.com/aweme/v1/play/"
        "?video_id=live-video-id&ratio=1080p&line=0"
    )


def test_aweme_detail_schema_decodes_motion_photo_metadata():
    response = msgspec.json.decode(
        msgspec.json.encode(
            {
                "status_code": 0,
                "aweme_detail": {
                    "create_time": 0,
                    "author": {"nickname": "tester"},
                    "desc": "live photo",
                    "images": [
                        {
                            "clip_type": 5,
                            "url_list": ["https://example.com/cover.jpeg"],
                            "video": {
                                "play_addr_h264": {"uri": "live-video-id"}
                            },
                        }
                    ],
                },
            }
        ),
        type=AwemeDetailRes,
    )

    assert response.aweme_detail is not None
    image = response.aweme_detail.images[0]
    assert image.clip_type == 5
    assert image.video.play_addr_h264.uri == "live-video-id"


def test_slides_dynamic_urls_exclude_motion_photo_clips():
    live = SlidesImage(
        clip_type=5,
        video=SlidesVideo(
            play_addr=SlidesPlayAddr(url_list=["https://example.com/live.mp4"])
        ),
    )
    dynamic = SlidesImage(
        clip_type=2,
        video=SlidesVideo(
            play_addr=SlidesPlayAddr(url_list=["https://example.com/dynamic.mp4"])
        ),
    )

    from core.parsers.douyin.slides import Author, Avatar, SlidesData

    slides = SlidesData(
        author=Author(nickname="tester", avatar_thumb=Avatar(url_list=[])),
        desc="demo",
        create_time=0,
        images=[live, dynamic],
    )

    assert slides.dynamic_urls == ["https://example.com/dynamic.mp4"]


def test_download_motion_photo_packages_and_cleans_intermediate_files(
    tmp_path: Path,
):
    jpeg = b"\xff\xd8\xff\xdbjpeg-data\xff\xd9"
    video = b"\x00\x00\x00\x18ftypmp42mp4-data"

    class FakeDownloader:
        async def download_img(self, _url: str, *, img_name: str, **_kwargs) -> Path:
            path = tmp_path / img_name
            path.write_bytes(jpeg)
            return path

        async def download_video(
            self, _url: str, *, video_name: str, **_kwargs
        ) -> Path:
            path = tmp_path / video_name
            path.write_bytes(video)
            return path

    parser = object.__new__(DouyinParser)
    parser.cfg = SimpleNamespace(
        cache_dir=tmp_path,
        proxy=None,
        parser=SimpleNamespace(douyin=SimpleNamespace(use_proxy=False)),
    )
    parser.downloader = FakeDownloader()
    parser.ios_headers = {"User-Agent": "test", "Cookie": "secret"}

    result = asyncio.run(
        parser._download_motion_photo(
            "https://example.com/cover.jpeg",
            "https://example.com/live.mp4",
            headers={"User-Agent": "test"},
            referer="https://www.iesdouyin.com/share/note/1234567890123456789/",
        )
    )

    assert result.name.startswith("motion_")
    assert result.suffix == ".jpg"
    assert result.read_bytes().endswith(video)
    assert not list(tmp_path.glob(".motion_*"))


def test_a_bogus_matches_reference_vector(monkeypatch: pytest.MonkeyPatch):
    query = (
        "device_platform=webapp&aid=6383&channel=channel_pc_web&"
        "aweme_id=7643801277823001529"
    )
    user_agent = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 "
        "Safari/537.36 Edg/130.0.0.0"
    )
    monkeypatch.setattr(signature.time, "time", lambda: 1786896000.123)
    signature.random.seed(20260816)

    result = signature.generate_a_bogus(query, user_agent)

    assert result == (
        "DfR0Mfu2p3jifjSt5RCLfY3q6lbVYQEC0SVkMD2fUBDPyL39HMTa9exoqk4v2FEj"
        "Fs/jIeSjy4hbO3KDrQAj8rmUHWwoWdQ2m6RdKl5Q5I0j53iruyR0nt8F4kG-FeeM-"
        "iA3xOvsy75nFbw0AoK75JIlO6ZCcHgOEisnO9W="
    )
