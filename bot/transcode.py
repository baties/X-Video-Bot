"""Compress oversized videos to fit under the Telegram bot upload limit."""

import asyncio
import json
import logging
import os
import subprocess

log = logging.getLogger(__name__)

AUDIO_BITRATE = 128_000  # bits/s
MIN_VIDEO_BITRATE = 250_000  # below this, the result is unwatchable — give up
SAFETY_MARGIN = 0.92  # target 92% of the limit to absorb container overhead


class CompressionImpossible(Exception):
    """Video is too long to fit the size limit at watchable quality."""


class CompressionFailed(Exception):
    """ffmpeg/ffprobe failed."""


def _probe_duration(filepath: str) -> float:
    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "json", filepath,
            ],
            capture_output=True, text=True, check=True, timeout=60,
        )
        return float(json.loads(out.stdout)["format"]["duration"])
    except Exception as exc:
        raise CompressionFailed(f"ffprobe failed: {exc}") from exc


def _compress_sync(filepath: str, max_size: int) -> str:
    duration = _probe_duration(filepath)
    if duration <= 0:
        raise CompressionFailed("Could not determine video duration")

    target_bits = int(max_size * SAFETY_MARGIN) * 8
    video_bitrate = int(target_bits / duration) - AUDIO_BITRATE

    if video_bitrate < MIN_VIDEO_BITRATE:
        raise CompressionImpossible(
            f"Would need {video_bitrate // 1000} kbps video for {duration:.0f}s"
        )

    out_path = os.path.splitext(filepath)[0] + ".compressed.mp4"
    cmd = [
        "ffmpeg", "-y", "-i", filepath,
        "-c:v", "libx264", "-preset", "fast",
        "-b:v", str(video_bitrate),
        "-maxrate", str(int(video_bitrate * 1.2)),
        "-bufsize", str(video_bitrate * 2),
        "-vf", "scale='min(1280,iw)':-2",  # cap width, keep aspect, even height
        "-c:a", "aac", "-b:a", str(AUDIO_BITRATE),
        "-movflags", "+faststart",
        out_path,
    ]
    try:
        subprocess.run(cmd, capture_output=True, check=True, timeout=1800)
    except subprocess.CalledProcessError as exc:
        raise CompressionFailed(exc.stderr.decode(errors="replace")[-500:]) from exc
    except subprocess.TimeoutExpired as exc:
        raise CompressionFailed("ffmpeg timed out") from exc

    if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        raise CompressionFailed("ffmpeg produced no output")
    return out_path


async def compress_to_fit(filepath: str, max_size: int) -> str:
    """Re-encode video to fit under max_size bytes. Returns new file path."""
    return await asyncio.to_thread(_compress_sync, filepath, max_size)
