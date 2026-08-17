from pathlib import Path

from core.data import DynamicContent, ImageContent
from core.parsers.douyin import DouyinParser


def test_douyin_gallery_groups_use_source_item_count_for_folding():
    # 一张实况图可解析为静态图和动态媒体两个段，但仍应作为单张作品发送。
    single_live_contents = [
        ImageContent(Path("single.jpg")),
        DynamicContent(Path("single.mp4")),
    ]
    single_group = DouyinParser._gallery_send_groups(single_live_contents, 1)

    assert len(single_group) == 1
    assert single_group[0].contents == single_live_contents
    assert single_group[0].force_merge is False

    multiple_contents = [
        ImageContent(Path("one.jpg")),
        ImageContent(Path("two.jpg")),
    ]
    multiple_group = DouyinParser._gallery_send_groups(multiple_contents, 2)

    assert len(multiple_group) == 1
    assert multiple_group[0].contents == multiple_contents
    assert multiple_group[0].force_merge is True


def test_douyin_gallery_groups_skip_empty_media():
    assert DouyinParser._gallery_send_groups([], 1) == []
