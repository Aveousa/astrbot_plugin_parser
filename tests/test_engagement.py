import pytest

from core.data import EngagementStats, ParseResult, Platform


def test_engagement_stats_normalizes_platform_values():
    stats = EngagementStats.from_mapping(
        {
            "likeCount": "1.2万",
            "commentCount": {"count": "34"},
            "bookmarkCount": "5",
            "shareCount": "2亿",
        }
    )

    assert stats.likes == 12_000
    assert stats.comments == 34
    assert stats.favorites == 5
    assert stats.shares == 200_000_000


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {"like": 1, "reply": 2, "favorite": 3, "share": 4},
            (1, 2, 3, 4),
        ),
        (
            {
                "digg_count": 5,
                "comment_count": 6,
                "collect_count": 7,
                "share_count": 8,
            },
            (5, 6, 7, 8),
        ),
        (
            {
                "likedCount": 9,
                "commentCount": 10,
                "collectedCount": 11,
                "shareCount": 12,
            },
            (9, 10, 11, 12),
        ),
        (
            {"likeCount": 13, "commentCount": 14, "bookmarkCount": 15},
            (13, 14, 15, None),
        ),
    ],
    ids=["bilibili", "douyin", "xhs", "pixiv"],
)
def test_platform_stat_field_aliases(payload, expected):
    stats = EngagementStats.from_mapping(payload)
    assert (stats.likes, stats.comments, stats.favorites, stats.shares) == expected


def test_parse_result_exposes_count_aliases_and_resource_fingerprint():
    result = ParseResult(
        platform=Platform(name="xhs", display_name="小红书"),
        like_count=1,
        comment_count=2,
        favorite_count=3,
        share_count=4,
    )

    assert result.engagement.as_dict() == {
        "likes": 1,
        "comments": 2,
        "favorites": 3,
        "shares": 4,
    }
    assert (result.likes, result.comments, result.favorites, result.shares) == (1, 2, 3, 4)
    fingerprint = result.get_resource_id()
    result.like_count = 5
    # 指纹缓存语义与旧实现一致：创建后不随发送过程中的对象修改漂移。
    assert result.get_resource_id() == fingerprint


def test_engagement_counts_do_not_change_resource_identity():
    """互动数是展示数据，不能影响同一链接的原有防抖语义。"""
    first = ParseResult(
        platform=Platform(name="bilibili", display_name="Bilibili"),
        url="https://www.bilibili.com/video/BV1xx",
        like_count=1,
    )
    updated = ParseResult(
        platform=Platform(name="bilibili", display_name="Bilibili"),
        url="https://www.bilibili.com/video/BV1xx",
        like_count=99,
        comment_count=8,
        favorite_count=7,
        share_count=6,
    )

    assert first.get_resource_id() == updated.get_resource_id()


def test_new_engagement_fields_preserve_legacy_positional_constructor():
    """新增展示字段不能改变旧版 ParseResult 位置参数的含义。"""
    contents = []
    result = ParseResult(
        Platform("xhs", "小红书"),
        None,
        "标题",
        "正文",
        123,
        "https://example.test/item",
        contents,
    )

    assert result.title == "标题"
    assert result.text == "正文"
    assert result.timestamp == 123
    assert result.url == "https://example.test/item"
    assert result.contents is contents
    assert result.like_count is None
