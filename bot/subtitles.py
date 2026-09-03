"""VIP transcript feature: OpenAI Whisper transcription + translation, and
SRT/subtitle-style helpers. ffmpeg burn-in itself lives in transcode.py.
"""

import json
import logging
import math

from openai import AsyncOpenAI

from .config import Settings

log = logging.getLogger(__name__)

# code -> (display label, libass font family installed in the image, name for prompts)
LANGUAGES: dict[str, dict[str, str]] = {
    "en": {"label": "English", "font": "DejaVu Sans", "name": "English"},
    "fr": {"label": "French", "font": "DejaVu Sans", "name": "French"},
    "fa": {"label": "Persian", "font": "Nazli", "name": "Persian"},
}

_LANG_ALIASES = {
    "english": "en", "en": "en",
    "french": "fr", "fr": "fr", "français": "fr", "francais": "fr",
    "persian": "fa", "farsi": "fa", "fa": "fa",
}

_NAMED_COLORS = {
    "white": "FFFFFF", "black": "000000", "yellow": "FFFF00", "red": "FF0000",
    "green": "00FF00", "blue": "0000FF", "cyan": "00FFFF", "magenta": "FF00FF",
    "orange": "FFA500",
}


class TranscriptFailed(Exception):
    """Raised when transcription or translation fails."""


def normalize_lang(name: str | None) -> str | None:
    """Map a free-form language name/code (as returned by Whisper, or typed
    by an admin/user) to one of our supported codes, or None if unknown."""
    if not name:
        return None
    return _LANG_ALIASES.get(name.strip().lower())


def credit_cost(duration_sec: float, per_minute: int) -> int:
    """Round up to the next whole minute, minimum 1 credit."""
    minutes = max(duration_sec, 1.0) / 60
    return max(1, math.ceil(minutes * per_minute))


def to_ass_color(value: str) -> str:
    """Convert a CSS-style name or #RRGGBB hex into libass's &H00BBGGRR& format."""
    v = value.strip().lstrip("#").lower()
    hexval = _NAMED_COLORS.get(v, v)
    if len(hexval) != 6 or any(c not in "0123456789abcdef" for c in hexval):
        hexval = "FFFFFF"  # bad config -> fall back to white rather than fail the job
    rr, gg, bb = hexval[0:2], hexval[2:4], hexval[4:6]
    return f"&H00{bb}{gg}{rr}&".upper()


def _fmt_ts(seconds: float) -> str:
    ms = int(round(max(seconds, 0) * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def segments_to_srt(segments: list[dict]) -> str:
    lines = []
    n = 0
    for seg in segments:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        n += 1
        lines.append(str(n))
        lines.append(f"{_fmt_ts(seg['start'])} --> {_fmt_ts(seg['end'])}")
        lines.append(text)
        lines.append("")
    return "\n".join(lines)


async def _translate_segments(
    client: AsyncOpenAI, model: str, texts: list[str], target_name: str
) -> list[str]:
    """Translate subtitle-segment texts, preserving order and count."""
    prompt = (
        f"Translate each array item into {target_name}. Keep the same order "
        "and exactly the same number of items. Keep translations short and "
        "natural, suitable for video subtitles. Respond with ONLY this JSON "
        'shape: {"translations": ["...", "...", ...]}\n\n'
        f"Items: {json.dumps(texts, ensure_ascii=False)}"
    )
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.2,
        )
        data = json.loads(resp.choices[0].message.content)
        out = data["translations"]
    except Exception as exc:
        raise TranscriptFailed(f"Translation failed: {exc}") from exc
    if not isinstance(out, list) or len(out) != len(texts):
        raise TranscriptFailed("Translation returned a different number of lines")
    return [str(t) for t in out]


async def build_srt(audio_path: str, target_lang: str, settings: Settings) -> str:
    """Transcribe audio_path and return SRT text translated into target_lang
    ("en"/"fr"/"fa"). Raises TranscriptFailed on any OpenAI or parsing error."""
    if not settings.openai_api_key:
        raise TranscriptFailed("OPENAI_API_KEY is not configured")
    client = AsyncOpenAI(api_key=settings.openai_api_key)

    try:
        with open(audio_path, "rb") as fh:
            result = await client.audio.transcriptions.create(
                model=settings.openai_transcribe_model,
                file=fh,
                response_format="verbose_json",
            )
    except Exception as exc:
        raise TranscriptFailed(f"Transcription failed: {exc}") from exc

    raw_segments = getattr(result, "segments", None) or []
    segments = [{"start": s.start, "end": s.end, "text": s.text} for s in raw_segments]
    if not segments:
        raise TranscriptFailed("No speech detected in this video")

    detected = normalize_lang(getattr(result, "language", None))
    if detected != target_lang:
        texts = [s["text"] for s in segments]
        translated = await _translate_segments(
            client, settings.openai_translate_model, texts,
            LANGUAGES[target_lang]["name"],
        )
        for seg, text in zip(segments, translated):
            seg["text"] = text

    return segments_to_srt(segments)
