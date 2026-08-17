from __future__ import annotations

import asyncio
from types import SimpleNamespace

from core.clean import CacheCleaner


def test_cache_cleaner_keeps_the_existing_whole_cache_cleanup_policy(tmp_path):
    """Cards, downloaded media, emoji assets and temporary HTML share one cache.

    The renderer deliberately writes its transient HTML next to the card PNG;
    the existing scheduled ``rmtree + mkdir`` policy must continue to clear
    every kind of generated file together.
    """
    cache_dir = tmp_path / "cache"
    emoji_dir = cache_dir / "emojis"
    emoji_dir.mkdir(parents=True)
    (cache_dir / "card_example.png").write_bytes(b"card")
    (cache_dir / "card_example.html").write_text("temporary", encoding="utf-8")
    (cache_dir / "video_example.mp4").write_bytes(b"video")
    (emoji_dir / "emoji.png").write_bytes(b"emoji")

    cleaner = object.__new__(CacheCleaner)
    cleaner.cfg = SimpleNamespace(cache_dir=cache_dir)
    asyncio.run(cleaner._clean_plugin_cache())

    assert cache_dir.is_dir()
    assert list(cache_dir.iterdir()) == []
