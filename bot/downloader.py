"""Video download logic (yt-dlp wrapper) and platform registry."""

import asyncio
import logging
import os
import re
from dataclasses import dataclass

import httpx
import yt_dlp

from .config import Settings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Platform:
    key: str
    label: str
    url_re: re.Pattern
    ydl_format: str


PLATFORMS: dict[str, Platform] = {
    "x": Platform(
        key="x",
        label="X.com",
        url_re=re.compile(r"https?://(?:www\.)?(?:x\.com|twitter\.com)/\w+/status/\d+"),
        ydl_format="best[ext=mp4][filesize<50M]/best[ext=mp4]/best",
    ),
    "youtube": Platform(
        key="youtube",
        label="YouTube",
        url_re=re.compile(
            r"https?://(?:www\.|m\.)?(?:youtube\.com/(?:watch\?\S*v=|shorts/)[\w-]+|youtu\.be/[\w-]+)\S*"
        ),
        # Cap resolution so files stay under the 50 MB bot limit more often
        ydl_format="bv*[ext=mp4][height<=720]+ba[ext=m4a]/b[ext=mp4][height<=720]/b",
    ),
    "instagram": Platform(
        key="instagram",
        label="Instagram",
        url_re=re.compile(r"https?://(?:www\.)?instagram\.com/(?:reel|reels|p|tv)/[\w-]+"),
        ydl_format="best[ext=mp4]/best",
    ),
}


class DownloadFailed(Exception):
    """Raised when the video cannot be downloaded."""


class NoVideoFound(Exception):
    """Raised when the post contains no downloadable video."""


class VideoTooLarge(Exception):
    """Raised when the source is known to exceed the caller's size limit
    before any video bytes are downloaded."""

    def __init__(self, size: int) -> None:
        super().__init__(f"{size} bytes")
        self.size = size


@dataclass(frozen=True)
class DownloadResult:
    filepath: str
    title: str
    size: int


def detect_platform(text: str) -> tuple[Platform, str] | None:
    """Return (platform, url) for the first supported link in text, or None."""
    for platform in PLATFORMS.values():
        match = platform.url_re.search(text)
        if match:
            return platform, match.group(0)
    return None


def _build_ydl_opts(platform: Platform, out_dir: str, settings: Settings) -> dict:
    extractor_args: dict = {
        # Alternate player clients often bypass YouTube's bot check
        "youtube": {"player_client": ["android", "web_safari", "web"]},
    }
    if settings.pot_provider_url:
        # bgutil-ytdlp-pot-provider plugin (PO tokens, no account needed)
        extractor_args["youtubepot-bgutilhttp"] = {
            "base_url": [settings.pot_provider_url]
        }

    opts: dict = {
        "outtmpl": os.path.join(out_dir, "%(id)s.%(ext)s"),
        "format": platform.ydl_format,
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "extractor_args": extractor_args,
        # Bound every socket op so a stalled connection can't hang a worker forever
        "socket_timeout": settings.download_timeout,
    }
    if settings.cookies_file:
        opts["cookiefile"] = settings.cookies_file
    if settings.proxy_url:
        opts["proxy"] = settings.proxy_url
    return opts


def _estimate_size(info: dict) -> int | None:
    """Best-effort total byte size for the format(s) yt-dlp selected, or None
    if the source doesn't report it (common for HLS/live and some formats)."""
    formats = info.get("requested_formats")
    if formats:
        sizes = [f.get("filesize") or f.get("filesize_approx") for f in formats]
        return sum(sizes) if all(sizes) else None
    return info.get("filesize") or info.get("filesize_approx")


def _download_sync(
    url: str, platform: Platform, out_dir: str, settings: Settings, size_limit: int | None
) -> DownloadResult:
    ydl_opts = _build_ydl_opts(platform, out_dir, settings)
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            if size_limit is not None:
                # Probe metadata only, so oversized videos never cost bandwidth/time
                probe = ydl.extract_info(url, download=False)
                estimated = _estimate_size(probe)
                if estimated is not None and estimated > size_limit:
                    raise VideoTooLarge(estimated)
            info = ydl.extract_info(url, download=True)
            filepath = ydl.prepare_filename(info)
            # merge_output_format can change the extension
            if not os.path.exists(filepath):
                filepath = os.path.splitext(filepath)[0] + ".mp4"
    except yt_dlp.utils.DownloadError as exc:
        raise DownloadFailed(str(exc)) from exc

    if not filepath or not os.path.exists(filepath):
        raise NoVideoFound(url)

    return DownloadResult(
        filepath=filepath,
        title=(info.get("title") or "")[:1024],
        size=os.path.getsize(filepath),
    )


async def download_video(
    url: str,
    platform: Platform,
    out_dir: str,
    settings: Settings,
    size_limit: int | None = None,
) -> DownloadResult:
    """Download a video without blocking the event loop.

    size_limit, if given, rejects known-oversized sources before downloading
    (raises VideoTooLarge); it's a best-effort pre-check, not a guarantee —
    the caller must still verify the final file size.
    """
    return await asyncio.to_thread(
        _download_sync, url, platform, out_dir, settings, size_limit
    )


async def wait_for_pot_provider(
    url: str, timeout: float = 20.0, interval: float = 1.0
) -> bool:
    """Poll the PO token provider until it responds or timeout elapses.

    Best-effort only: never raises. Returns whether it became reachable, so
    the caller can log a warning but must still start the bot either way —
    the provider container may just be slower to boot than the bot.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    async with httpx.AsyncClient(timeout=2.0) as client:
        while True:
            try:
                await client.get(url)
                return True
            except httpx.HTTPError:
                pass
            if asyncio.get_event_loop().time() >= deadline:
                return False
            await asyncio.sleep(interval)
