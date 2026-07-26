"""Video download logic (yt-dlp wrapper) and platform registry."""

import asyncio
import logging
import os
import re
from dataclasses import dataclass

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
    }
    if settings.cookies_file:
        opts["cookiefile"] = settings.cookies_file
    if settings.proxy_url:
        opts["proxy"] = settings.proxy_url
    return opts


def _download_sync(url: str, platform: Platform, out_dir: str, settings: Settings) -> DownloadResult:
    ydl_opts = _build_ydl_opts(platform, out_dir, settings)
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
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
    url: str, platform: Platform, out_dir: str, settings: Settings
) -> DownloadResult:
    """Download a video without blocking the event loop."""
    return await asyncio.to_thread(_download_sync, url, platform, out_dir, settings)
