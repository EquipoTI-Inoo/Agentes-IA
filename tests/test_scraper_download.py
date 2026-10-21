"""
Unit tests for MetaAdsHunter streaming download logic and retry behaviors.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from src.scraper import MetaAdsHunter


class MockResponse:
    def __init__(self, chunks, status_code=200):
        self._chunks = chunks
        self.status_code = status_code

    def iter_content(self, chunk_size=65536):
        for chunk in self._chunks:
            yield chunk

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass


def test_download_streaming_does_not_retry_on_max_bytes_exceeded(tmp_path):
    """Verify that exceeding max_bytes raises ValueError and does NOT retry (attempt == 1)."""
    hunter = MetaAdsHunter(output_dir=str(tmp_path))
    dest_path = tmp_path / "large_video.mp4"

    call_count = 0

    def mock_get(url, stream=True, timeout=120):
        nonlocal call_count
        call_count += 1
        # Chunks exceeding max_bytes (set max_bytes to 500 bytes for test)
        return MockResponse([b"x" * 300, b"x" * 300])

    hunter.collector.client.session.get = MagicMock(side_effect=mock_get)

    success = hunter._download_file_streaming(
        url="http://fake.video/ad.mp4",
        dest_path=dest_path,
        max_bytes=500,
        min_bytes=100,
    )

    assert success is False
    # Crucial assertion: Must NOT retry when ValueError (max_bytes) occurs
    assert call_count == 1
    # Temporary file must be cleaned up
    tmp_file = dest_path.with_suffix(".mp4.tmp")
    assert not tmp_file.exists()
    assert not dest_path.exists()


def test_download_streaming_retries_on_network_error(tmp_path):
    """Verify that network or HTTP errors trigger retry attempts."""
    hunter = MetaAdsHunter(output_dir=str(tmp_path))
    dest_path = tmp_path / "valid_video.mp4"

    call_count = 0

    def mock_get(url, stream=True, timeout=120):
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            # Simulate transient network failure on first attempt
            raise ConnectionError("Connection reset by peer")
        # Succeed on 2nd attempt
        return MockResponse([b"valid data " * 100])

    hunter.collector.client.session.get = MagicMock(side_effect=mock_get)

    success = hunter._download_file_streaming(
        url="http://fake.video/ad.mp4",
        dest_path=dest_path,
        max_bytes=50000,
        min_bytes=100,
    )

    assert success is True
    # Verify that it retried and succeeded on attempt 2
    assert call_count == 2
    assert dest_path.exists()
    assert dest_path.stat().st_size > 100
