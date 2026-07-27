"""
Standalone debug script — run this to see exactly where transcript fetching fails.
Usage: python subtitle-scraper/debug_transcript.py iF8crBezySA

Interactive debugging tool: every print() emits diagnostic data to stdout,
which IS the product. Intentionally left as print(), not logging.
"""
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

# E402: this is a linear top-to-bottom debug script, not a module — the
# load_dotenv() above must run before anything reads the environment.
import yt_dlp  # noqa: E402

video_id = sys.argv[1] if len(sys.argv) > 1 else "iF8crBezySA"
language_codes = ["de", "de-DE", "en"]
url = f"https://www.youtube.com/watch?v={video_id}"
browser = os.getenv("YTDLP_COOKIES_BROWSER", "")

print(f"video_id        : {video_id}")
print(f"url             : {url}")
print(f"language_codes  : {language_codes}")
print(f"cookies browser : {browser!r}")
print()

# ── Step 1: extract_info ────────────────────────────────────────────────────
print("── Step 1: extract_info(download=False) ──")
ydl_opts_no_cookies = {
    "quiet": False,
    "no_warnings": False,
}
ydl_opts_with_cookies = {
    "quiet": False,
    "no_warnings": False,
    # Node.js solves the n-challenge so the web client works with cookies.
    "js_runtimes": {"node": {}},
    "remote_components": ["ejs:github"],
    **({"cookiesfrombrowser": (browser,)} if browser else {}),
}

try:
    with yt_dlp.YoutubeDL(ydl_opts_with_cookies) as ydl:
        info = ydl.extract_info(url, download=False)
    print("  extract_info: OK")
except Exception as e:
    print(f"  extract_info FAILED: {e}")
    sys.exit(1)

# ── Step 2: inspect subtitle data ───────────────────────────────────────────
print()
print("── Step 2: available subtitles ──")
subtitles = info.get("subtitles") or {}
auto_captions = info.get("automatic_captions") or {}

print(f"  manual subtitle langs    : {sorted(subtitles.keys())}")
print(f"  auto-caption langs       : {sorted(auto_captions.keys())[:20]} ...")

for lang in language_codes:
    for pool_name, pool in [("manual", subtitles), ("auto", auto_captions)]:
        entries = pool.get(lang)
        if entries:
            exts = [e.get("ext") for e in entries]
            print(f"  [{pool_name}] {lang}: {exts}")

# ── Step 3: pick best subtitle ──────────────────────────────────────────────
print()
print("── Step 3: pick best subtitle ──")
_EXT_PREF = ("json3", "vtt", "srv3", "srv2", "srv1", "ttml")
chosen_lang = None
chosen_sub = None

for lang in language_codes:
    for pool_name, pool in [("manual", subtitles), ("auto", auto_captions)]:
        subs = pool.get(lang)
        if not subs:
            continue
        for ext in _EXT_PREF:
            entry = next((s for s in subs if s.get("ext") == ext), None)
            if entry:
                chosen_lang = lang
                chosen_sub = entry
                print(f"  chosen: lang={lang} ext={ext} pool={pool_name}")
                break
        if chosen_lang:
            break
    if chosen_lang:
        break

if not chosen_lang:
    print("  NO SUBTITLE FOUND for requested languages")
    sys.exit(1)

sub_url = chosen_sub["url"]
print(f"  url (first 80 chars): {sub_url[:80]}...")

# ── Step 4: download subtitle via ydl.urlopen (no cookies) ──────────────────
print()
print("── Step 4: ydl.urlopen(sub_url) — no cookies ──")
try:
    with yt_dlp.YoutubeDL(ydl_opts_no_cookies) as ydl:
        response = ydl.urlopen(sub_url)
        content = response.read()
    print(f"  download OK — {len(content)} bytes")
    print(f"  first 200 chars: {content[:200]}")
    print()
    print("All steps passed.")
    sys.exit(0)
except Exception as e:
    print(f"  urlopen FAILED: {e}")

# ── Step 5: fallback — let yt-dlp write subtitles to a tempdir ───────────────
import tempfile  # noqa: E402  (linear debug script; kept beside the step that uses it)
print()
print("── Step 5: tempdir writesubtitles (no cookies, skip_download) ──")
try:
    with tempfile.TemporaryDirectory() as tmpdir:
        opts = {
            "skip_download": True,
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": language_codes,
            "outtmpl": f"{tmpdir}/%(id)s",
            "quiet": False,
            "no_warnings": False,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        files = list(Path(tmpdir).glob(f"{video_id}.*.*"))
        print(f"  downloaded files: {[f.name for f in files]}")
        if files:
            print(f"  first file size: {files[0].stat().st_size} bytes")
            print(f"  first 200 chars: {files[0].read_text()[:200]}")
except Exception as e:
    print(f"  tempdir FAILED: {e}")

print()
print("Done.")
