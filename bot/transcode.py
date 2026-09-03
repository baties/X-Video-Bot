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


class AudioExtractFailed(Exception):
    """ffmpeg failed to extract an audio track for transcription."""


class SubtitleBurnFailed(Exception):
    """ffmpeg failed to burn subtitles into the video."""


def probe_duration(filepath: str) -> float:
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
    duration = probe_duration(filepath)
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


def _extract_audio_sync(filepath: str, out_path: str) -> str:
    # Mono, low-bitrate: keeps the upload to OpenAI's 25 MB limit for most clips
    cmd = [
        "ffmpeg", "-y", "-i", filepath,
        "-vn", "-ac", "1", "-c:a", "libmp3lame", "-b:a", "64k",
        out_path,
    ]
    try:
        subprocess.run(cmd, capture_output=True, check=True, timeout=600)
    except subprocess.CalledProcessError as exc:
        raise AudioExtractFailed(exc.stderr.decode(errors="replace")[-500:]) from exc
    except subprocess.TimeoutExpired as exc:
        raise AudioExtractFailed("audio extraction timed out") from exc
    if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        raise AudioExtractFailed("ffmpeg produced no audio output")
    return out_path


async def extract_audio(filepath: str, out_path: str) -> str:
    """Extract a small mono audio track for transcription."""
    return await asyncio.to_thread(_extract_audio_sync, filepath, out_path)


def _burn_subtitles_sync(
    video_path: str, srt_path: str, out_path: str,
    font_name: str, font_size: int, ass_color: str,
) -> str:
    # ffmpeg filtergraph syntax needs ':' and "'" escaped inside the file path
    escaped_srt = srt_path.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    style = (
        f"FontName={font_name},FontSize={font_size},PrimaryColour={ass_color},"
        "BorderStyle=1,Outline=1,Shadow=0"
    )
    cmd = [
        "ffmpeg", "-y", "-i", video_path,
        "-vf", f"subtitles='{escaped_srt}':force_style='{style}'",
        "-c:a", "copy",
        out_path,
    ]
    try:
        subprocess.run(cmd, capture_output=True, check=True, timeout=1800)
    except subprocess.CalledProcessError as exc:
        raise SubtitleBurnFailed(exc.stderr.decode(errors="replace")[-500:]) from exc
    except subprocess.TimeoutExpired as exc:
        raise SubtitleBurnFailed("subtitle burn timed out") from exc
    if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        raise SubtitleBurnFailed("ffmpeg produced no output")
    return out_path


async def burn_subtitles(
    video_path: str, srt_path: str, out_path: str,
    font_name: str, font_size: int, ass_color: str,
) -> str:
    """Hardsub video_path with srt_path, returning the new file's path."""
    return await asyncio.to_thread(
        _burn_subtitles_sync, video_path, srt_path, out_path,
        font_name, font_size, ass_color,
    )
