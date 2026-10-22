"""
Tests for Media Info extraction (video and image) using pymediainfo and Pillow
"""

from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from src.media.media_info import get_image_info, get_video_info


def test_get_video_info_bland_ai():
    video_path = Path("downloads/Bland AI/1763243594393368/video_0.mp4")
    if not video_path.exists():  # downloads/ is not part of the repository
        pytest.skip(f"Sample video not available in this checkout: {video_path}")

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


# --- duration, ratio and audio track: deterministic cases (pymediainfo output faked) ---------------

def _fake_parse(monkeypatch, *tracks):
    monkeypatch.setattr("src.media.media_info.MediaInfo.parse", lambda path: SimpleNamespace(tracks=list(tracks)))


def _track(kind, **kw):
    return SimpleNamespace(track_type=kind, duration=kw.get("duration"), width=kw.get("width"), height=kw.get("height"))


@pytest.mark.parametrize("width, height, expected", [
    (1080, 1920, "9:16"), (1920, 1080, "16:9"), (1080, 1080, "1:1"), (1080, 1350, "4:5"), (1280, 960, "1.33"),
])
def test_aspect_ratio_classification_from_container_metadata(tmp_path, monkeypatch, width, height, expected):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    _fake_parse(monkeypatch, _track("General", duration=12000), _track("Video", width=width, height=height), _track("Audio"))
    info = get_video_info(video)
    assert (info["width"], info["height"], info["aspect_ratio"]) == (width, height, expected)
    assert info["duration_s"] == 12.0  # milliseconds -> seconds


def test_video_without_audio_track_reports_false_and_with_audio_true(tmp_path, monkeypatch):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    _fake_parse(monkeypatch, _track("General", duration=5500), _track("Video", width=720, height=1280))
    silent = get_video_info(video)
    assert silent["has_audio_track"] is False and silent["duration_s"] == 5.5
    _fake_parse(monkeypatch, _track("General", duration=5500), _track("Video", width=720, height=1280), _track("Audio"))
    assert get_video_info(video)["has_audio_track"] is True


def test_missing_duration_and_dimensions_do_not_crash(tmp_path, monkeypatch):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    _fake_parse(monkeypatch)  # no tracks at all
    info = get_video_info(video)
    assert info == {"duration_s": 0.0, "width": 0, "height": 0, "aspect_ratio": "unknown", "has_audio_track": False}


# --- the same checks on REAL files created on the fly (needs PyAV, installed with requirements-dev) ---

def _make_video(path, with_audio, width=72, height=128, seconds=2, fps=10):
    av = pytest.importorskip("av")
    np = pytest.importorskip("numpy")
    try:
        with av.open(str(path), "w") as container:
            video = container.add_stream("libx264", rate=fps)
            video.width, video.height, video.pix_fmt = width, height, "yuv420p"
            audio = container.add_stream("aac", rate=44100, layout="mono") if with_audio else None
            for i in range(seconds * fps):
                frame = av.VideoFrame.from_ndarray(np.full((height, width, 3), i * 5 % 255, dtype="uint8"), format="rgb24")
                for packet in video.encode(frame):
                    container.mux(packet)
            for packet in video.encode():
                container.mux(packet)
            if audio is not None:
                samples = np.zeros((1, 1024), dtype="float32")
                for i in range(int(seconds * 44100 / 1024) + 1):
                    frame = av.AudioFrame.from_ndarray(samples, format="fltp", layout="mono")
                    frame.sample_rate, frame.pts = 44100, i * 1024
                    for packet in audio.encode(frame):
                        container.mux(packet)
                for packet in audio.encode():
                    container.mux(packet)
    except Exception as exc:  # codec not bundled in this PyAV build
        pytest.skip(f"cannot create a sample video here: {exc}")


def test_real_video_duration_ratio_and_silent_track(tmp_path):
    silent, spoken = tmp_path / "silent.mp4", tmp_path / "with_audio.mp4"
    _make_video(silent, with_audio=False)
    _make_video(spoken, with_audio=True)

    info = get_video_info(silent)
    assert info["width"] == 72 and info["height"] == 128 and info["aspect_ratio"] == "9:16"
    assert info["duration_s"] == pytest.approx(2.0, abs=0.3)
    assert info["has_audio_track"] is False  # video with no audio track

    with_audio = get_video_info(spoken)
    assert with_audio["has_audio_track"] is True and with_audio["duration_s"] == pytest.approx(2.0, abs=0.3)
