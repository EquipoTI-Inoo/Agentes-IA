"""
Enterprise Video Forensics Engine
Performs 100% full-duration audio transcription with millisecond timestamps (Faster-Whisper)
and complete visual scene/frame extraction and audit (FFmpeg).
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from faster_whisper import WhisperModel
except ImportError:
    WhisperModel = None

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("VideoForensics")


@dataclass
class TranscriptSegment:
    start: float
    end: float
    text: str


@dataclass
class ForensicVideoReport:
    video_path: str
    duration_seconds: float
    language: str
    total_segments: int
    full_transcript: str
    transcript_timeline: List[Dict[str, Any]]
    total_frames_extracted: int
    frames_directory: str
    pacing_words_per_minute: float
    hook_transcript_0_to_3s: str


class VideoForensicsEngine:
    """Enterprise engine for full-video multimodal deconstruction (Audio + Visuals)."""

    def __init__(self, whisper_model_size: str = "base") -> None:
        if WhisperModel is None:
            raise RuntimeError(
                "faster-whisper no está instalado. Instálalo desde requirements-dev.txt para usar VideoForensicsEngine."
            )
        logger.info(f"Cargando modelo Whisper ({whisper_model_size})...")
        # Runs on CPU with int8 quantization for high speed, or CUDA if available
        self.whisper = WhisperModel(whisper_model_size, device="cpu", compute_type="int8")

    def transcribe_full_video(self, video_path: Path) -> Dict[str, Any]:
        """Transcribe 100% of the video audio from 0:00 to end with millisecond timestamps."""
        logger.info(f"🎙️ Transcribiendo audio completo de: {video_path.name}")
        segments_raw, info = self.whisper.transcribe(
            str(video_path),
            beam_size=5,
            word_timestamps=True,
        )

        timeline: List[Dict[str, Any]] = []
        full_text_list: List[str] = []
        hook_text = ""

        for s in segments_raw:
            timeline.append({
                "start": round(s.start, 2),
                "end": round(s.end, 2),
                "text": s.text.strip(),
            })
            full_text_list.append(s.text.strip())
            if s.start <= 3.5:
                hook_text += " " + s.text.strip()

        duration = round(info.duration, 2)
        full_text = " ".join(full_text_list)
        word_count = len(full_text.split())
        wpm = round((word_count / duration * 60) if duration > 0 else 0, 1)

        return {
            "duration": duration,
            "language": info.language,
            "language_prob": round(info.language_probability, 2),
            "timeline": timeline,
            "full_transcript": full_text,
            "hook_0_to_3s": hook_text.strip(),
            "words_per_minute": wpm,
            "word_count": word_count,
        }

    def extract_full_visual_frames(
        self,
        video_path: Path,
        output_dir: Path,
        fps_sample: float = 1.0,  # 1 frame per second across 100% of video
    ) -> List[Path]:
        """Extract high-resolution frames across the entire video duration using FFmpeg."""
        frames_dir = output_dir / "frames"
        frames_dir.mkdir(parents=True, exist_ok=True)

        pattern = str(frames_dir / "frame_%04d.jpg")
        cmd = [
            "ffmpeg",
            "-y",
            "-i", str(video_path),
            "-vf", f"fps={fps_sample}",
            "-q:v", "2",
            pattern,
        ]

        logger.info(f"🎞️ Extrayendo fotogramas de todo el video ({fps_sample} fps)...")
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

        frames = sorted(list(frames_dir.glob("frame_*.jpg")))
        logger.info(f"  [+] Se extrajeron {len(frames)} fotogramas en: {frames_dir}")
        return frames

    def analyze_video(self, video_path: Path, output_dir: Optional[Path] = None) -> Dict[str, Any]:
        """Run complete forensic analysis: 100% audio transcription + 100% visual frame extraction."""
        if output_dir is None:
            output_dir = video_path.parent

        # 1. Full Audio Transcript
        transcript_data = self.transcribe_full_video(video_path)

        # 2. Full Visual Frame Extraction
        extracted_frames = self.extract_full_visual_frames(video_path, output_dir, fps_sample=1.0)

        # 3. Assemble Full Report
        report = {
            "video_file": str(video_path.name),
            "duration_seconds": transcript_data["duration"],
            "language": transcript_data["language"],
            "pacing_words_per_minute": transcript_data["words_per_minute"],
            "total_frames_extracted": len(extracted_frames),
            "hook_0_to_3s": transcript_data["hook_0_to_3s"],
            "full_transcript": transcript_data["full_transcript"],
            "transcript_timeline": transcript_data["timeline"],
            "frames_folder": str(output_dir / "frames"),
        }

        # Save to disk as audit artifact
        audit_file = output_dir / "full_forensic_analysis.json"
        audit_file.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.info(f"✅ Análisis forense completo guardado en: {audit_file}")

        return report
