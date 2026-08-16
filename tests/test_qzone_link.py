from __future__ import annotations

from core.qzone_link import _is_safe_url


def test_qzone_blue_link_ssrf_filter_allows_public_http_urls():
    assert _is_safe_url("https://example.com/path")
    assert _is_safe_url("http://8.8.8.8/")


def test_qzone_blue_link_ssrf_filter_blocks_local_and_non_http_urls():
    assert not _is_safe_url("http://127.0.0.1/admin")
    assert not _is_safe_url("http://10.0.0.1/")
    assert not _is_safe_url("http://[::1]/")
    assert not _is_safe_url("file:///etc/passwd")
    assert not _is_safe_url("https://user:pass@example.com/")
