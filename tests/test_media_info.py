"""
Tests for Media Info extraction (video and image) using pymediainfo and Pillow
"""

from pathlib import Path
import pytest
from PIL import Image

from src.media.media_info import get_image_info, get_video_info


def test_get_video_info_bland_ai():
    video_path = Path("downloads/Bland AI/1763243594393368/video_0.mp4")
    assert video_path.exists(), f"Sample test video missing: {video_path}"

    info = get_video_info(video_path)
    assert info["duration_s"] > 900.0  # Around 951 seconds
    assert info["width"] == 1280
    assert info["height"] == 720
    assert info["aspect_ratio"] == "16:9"
    assert info["has_audio_track"] is True


def test_get_image_info(tmp_path):
    # Test vertical image (9:16)
    img_vertical = tmp_path / "test_vertical.jpg"
    im1 = Image.new("RGB", (1080, 1920), color="blue")
    im1.save(img_vertical)

    info_v = get_image_info(img_vertical)
    assert info_v["width"] == 1080
    assert info_v["height"] == 1920
    assert info_v["aspect_ratio"] == "9:16"
    assert info_v["format"] == "JPEG"

    # Test square image (1:1)
    img_square = tmp_path / "test_square.png"
    im2 = Image.new("RGB", (800, 800), color="red")
    im2.save(img_square)

    info_s = get_image_info(img_square)
    assert info_s["width"] == 800
    assert info_s["height"] == 800
    assert info_s["aspect_ratio"] == "1:1"
    assert info_s["format"] == "PNG"


def test_nonexistent_files():
    with pytest.raises(FileNotFoundError):
        get_video_info("non_existent_file.mp4")

    with pytest.raises(FileNotFoundError):
        get_image_info("non_existent_file.jpg")
