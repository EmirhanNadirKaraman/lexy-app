"""
Transcript fetching via yt-dlp.
Replaces youtube-transcript-api, which is easily IP-blocked by YouTube.
yt-dlp mimics a real browser, handles authentication via browser cookies,
and is actively maintained to stay ahead of YouTube's bot detection.
It's already a pipeline dependency (used for metadata).
"""
import json
import logging
import os
import random
import re
import time
from pathlib import Path
from typing import Optional

import yt_dlp

logger = logging.getLogger(__name__)

CACHE_DIR = Path(__file__).parent / "transcript_cache"
CACHE_DIR.mkdir(exist_ok=True)


# ---------------------------------------------------------------------------
# Subtitle parsers
# ---------------------------------------------------------------------------

def _parse_json3(data: dict) -> list[dict]:
    """Parse YouTube's JSON3 subtitle format into pipeline segments."""
    segments = []
    for event in data.get("events", []):
        segs = event.get("segs")
        if not segs:
            continue
        start = event.get("tStartMs", 0) / 1000.0
        duration = event.get("dDurationMs", 0) / 1000.0
        text = "".join(s.get("utf8", "") for s in segs).strip()
        if text and text != "\n":
            segments.append({"text": text, "start": start, "duration": duration})
    return segments


def _vtt_timestamp_to_seconds(ts: str) -> float:
    parts = ts.strip().split(":")
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    return int(parts[0]) * 60 + float(parts[1])


def _parse_vtt(content: str) -> list[dict]:
    """Parse WebVTT subtitle format into pipeline segments."""
    segments = []
    for block in re.split(r"\n{2,}", content.strip()):
        lines = block.strip().splitlines()
        ts_line = next((line for line in lines if "-->" in line), None)
        if ts_line is None:
            continue
        try:
            start_ts, end_ts = ts_line.split("-->")
            start = _vtt_timestamp_to_seconds(start_ts)
            end = _vtt_timestamp_to_seconds(end_ts.split()[0])
            duration = end - start
        except (ValueError, IndexError):
            continue
        text_lines = [
            re.sub(r"<[^>]+>", "", line).strip()
            for line in lines[lines.index(ts_line) + 1:]
        ]
        text = " ".join(t for t in text_lines if t)
        if text:
            segments.append({"text": text, "start": start, "duration": duration})
    return segments


def _parse_subtitle_file(path: Path) -> list[dict]:
    """Parse a subtitle file based on its extension."""
    if path.suffix == ".json3":
        with open(path) as f:
            return _parse_json3(json.load(f))
    with open(path) as f:
        return _parse_vtt(f.read())


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def _cache_path(video_id: str, language: str) -> Path:
    return CACHE_DIR / f"{video_id}_{language}.json"


def _load_cache(video_id: str, language_codes: list[str]) -> tuple[Optional[list], Optional[str]]:
    for lang in language_codes:
        path = _cache_path(video_id, lang)
        if path.exists():
            with open(path) as f:
                return json.load(f), lang
    return None, None


def _save_cache(video_id: str, language: str, snippets: list) -> None:
    with open(_cache_path(video_id, language), "w") as f:
        json.dump(snippets, f, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_with_retries(
    video_id: str,
    language_codes: list[str],
    proxy_provider: str = "none",   # kept for API compat; yt-dlp handles anti-bot natively
    max_retries: int = 3,
    initial_backoff: float = 2.0,
) -> tuple[list, str, str]:
    """
    Fetch a transcript via yt-dlp with retries, backoff, and local caching.
    Prefers manually created subtitles; falls back to auto-generated.

    Args:
        video_id:       YouTube video ID (not a URL)
        language_codes: Language codes to try in order, e.g. ["de", "de-DE", "de-AT"]
        proxy_provider: Ignored — kept so callers don't need to change their signatures
        max_retries:    Network retry attempts
        initial_backoff: Starting sleep between retries (doubles each attempt)

    Returns:
        (snippets, actual_language_code, transcript_source)
        snippets: list of {"text": str, "start": float, "duration": float}
        transcript_source: "manual" or "auto"

    Raises:
        ValueError:  No subtitles available for this video in any requested language
        Exception:   Network failure after all retries
    """
    cached, cached_lang = _load_cache(video_id, language_codes)
    if cached is not None:
        return cached, cached_lang

    browser = os.getenv("YTDLP_COOKIES_BROWSER", "")
    url = f"https://www.youtube.com/watch?v={video_id}"

    _EXT_PREF = ("json3", "vtt", "srv3", "srv2", "srv1", "ttml")

    # Two option sets tried in order:
    #   1. No cookies — uses android VR client, no n-challenge needed, no account risk.
    #   2. With cookies — uses web client + Node.js/EJS to solve n-challenge.
    #      Only reached if the cookieless attempt is rate-limited (HTTP 429).
    #      Configure YTDLP_COOKIES_BROWSER to a browser with a throwaway YouTube
    #      account to avoid any risk to your main account.
    _option_sets = [
        {"quiet": True, "no_warnings": True},
    ]
    if browser:
        _option_sets.append({
            "quiet": True,
            "no_warnings": True,
            "js_runtimes": {"node": {}},
            "remote_components": ["ejs:github"],
            "cookiesfrombrowser": (browser,),
        })

    last_error: Optional[Exception] = None

    for ydl_opts in _option_sets:
        backoff = initial_backoff
        for attempt in range(max_retries):
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=False)

                # Manual tracks only. info["automatic_captions"] is deliberately
                # NOT read: machine-transcribed captions are too unreliable to be
                # safe learning material, so a video with no manual track in the
                # target language is skipped rather than downgraded (TODO #35
                # product policy, 2026-05-23).
                subtitles = info.get("subtitles") or {}

                chosen_lang: Optional[str] = None
                chosen_sub: Optional[dict] = None
                chosen_source: str = "auto"

                for lang in language_codes:
                    # Only use manually uploaded subtitles — skip auto-generated captions.
                    # Manual keys sometimes have a video-specific suffix,
                    # e.g. "de-XwLwiJMB_Xs" instead of plain "de".
                    subs = subtitles.get(lang) or next(
                        (v for k, v in subtitles.items() if k.startswith(f"{lang}-")),
                        None,
                    )
                    if not subs:
                        continue
                    for ext in _EXT_PREF:
                        entry = next((s for s in subs if s.get("ext") == ext), None)
                        if entry:
                            chosen_lang = lang
                            chosen_sub = entry
                            chosen_source = "manual"
                            break
                    if chosen_lang:
                        break

                if not chosen_lang or not chosen_sub:
                    manual_langs = sorted(subtitles.keys())
                    raise ValueError(
                        f"No manual subtitles for {video_id} in {language_codes}. "
                        f"Manual available: {manual_langs} "
                        f"(video has auto-captions only)"
                        if not manual_langs else
                        f"No manual subtitles for {video_id} in {language_codes}. "
                        f"Manual available: {manual_langs}"
                    )

                sub_url = chosen_sub["url"]
                sub_ext = chosen_sub.get("ext", "vtt")

                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    response = ydl.urlopen(sub_url)
                    content = response.read()
                if isinstance(content, bytes):
                    content = content.decode("utf-8")

                if sub_ext == "json3":
                    snippets = _parse_json3(json.loads(content))
                else:
                    snippets = _parse_vtt(content)

                if not snippets:
                    raise ValueError(f"Empty transcript for {video_id}")

                _save_cache(video_id, chosen_lang, snippets)
                return snippets, chosen_lang, chosen_source

            except ValueError:
                raise  # no subtitles exist — permanent, don't retry

            except Exception as e:
                last_error = e
                is_rate_limited = "429" in str(e) or "Too Many Requests" in str(e)
                if is_rate_limited:
                    break  # skip remaining retries; try next option set (with cookies)
                if attempt < max_retries - 1:
                    wait = backoff + random.uniform(0, backoff * 0.1)
                    logger.warning(
                        "Transcript fetch attempt %d/%d failed for %s: %s. Retrying in %.1fs...",
                        attempt + 1, max_retries, video_id, e, wait,
                    )
                    time.sleep(wait)
                    backoff *= 2

    raise last_error or Exception(f"Failed to fetch transcript for {video_id}")
