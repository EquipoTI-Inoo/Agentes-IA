"""
Unit tests for MetaAdsHunter streaming download logic and retry behaviors.
"""

import http.server
from pathlib import Path
import socketserver
import threading
from unittest.mock import MagicMock
import pytest

from src.scraper import MetaAdsHunter


class MockResponse:
    """Mock Response matching curl_cffi response (no context manager protocol)."""
    def __init__(self, chunks, status_code=200):
        self._chunks = chunks
        self.status_code = status_code
        self.closed = False

    def iter_content(self, chunk_size=65536):
        for chunk in self._chunks:
            yield chunk

    def close(self):
        self.closed = True


class DummyServerHandler(http.server.BaseHTTPRequestHandler):
    large_request_count = 0
    small_request_count = 0

    def do_GET(self):
        if self.path == "/small.mp4":
            DummyServerHandler.small_request_count += 1
            content = b"x" * 2048  # 2 KB
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        elif self.path == "/large.mp4":
            DummyServerHandler.large_request_count += 1
            content = b"y" * 10240  # 10 KB
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # Suppress console logging during tests


@pytest.fixture(scope="module")
def local_http_server():
    server = socketserver.TCPServer(("127.0.0.1", 0), DummyServerHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()
    server.server_close()


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


def test_download_real_local_http_server(tmp_path, local_http_server):
    """Real HTTP streaming download using actual curl_cffi session against local http.server."""
    hunter = MetaAdsHunter(output_dir=str(tmp_path))

    # Test 1: Successful download and atomic rename
    dest_path = tmp_path / "downloaded_video.mp4"
    success = hunter._download_file_streaming(
        url=f"{local_http_server}/small.mp4",
        dest_path=dest_path,
        min_bytes=1000,
        max_bytes=10000,
    )
    assert success is True
    assert dest_path.exists()
    assert dest_path.stat().st_size == 2048
    tmp_path_check = dest_path.with_suffix(".mp4.tmp")
    assert not tmp_path_check.exists()

    # Test 2: File exceeds max_bytes -> ValueError, no retry
    dest_large = tmp_path / "large_video.mp4"
    initial_count = DummyServerHandler.large_request_count
    success_large = hunter._download_file_streaming(
        url=f"{local_http_server}/large.mp4",
        dest_path=dest_large,
        min_bytes=500,
        max_bytes=4000,  # 4 KB limit < 10 KB file
    )
    assert success_large is False
    assert not dest_large.exists()
    assert not dest_large.with_suffix(".mp4.tmp").exists()
    # Verified: exactly 1 request made, NO retries
    assert DummyServerHandler.large_request_count == initial_count + 1
