"""Advanced async video downloader using yt-dlp.

Highlights
----------
* Per-task temp dir, concurrency-safe.
* Smart format ladder — try compact mp4 first, fall back gracefully without
  blowing past Telegram's upload cap.
* Pre-flight size probe (no download) → skip impossible files early.
* Live progress callbacks (% + size + speed + ETA) for the bot's status edit.
* Multi-client YouTube extractor (tv_embedded → ios → mweb → web_safari)
  to bypass the most common bot-checks; cookies file supported.
* Resilient error mapping → friendly user-facing text.
"""
import asyncio
import os
import re
import shutil
import subprocess
import tempfile
import time
from typing import Callable, Optional
from urllib.parse import urlparse

import requests
import yt_dlp

# Telegram bot upload cap (~50 MB for regular bots)
MAX_BYTES = 49 * 1024 * 1024
_VIDEO_PROBE_TIMEOUT = 20

URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_SUPPORTED_HOSTS = (
    "facebook.com",
    "fb.watch",
    "instagram.com",
    "tiktok.com",
)

_UA_IOS = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 "
    "Mobile/15E148 Safari/604.1"
)

_UA_DESKTOP = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.0.0 Safari/537.36"
)


def detect_url(text: str) -> Optional[str]:
    if not text:
        return None
    m = URL_RE.search(text)
    if not m:
        return None
    url = m.group(0).rstrip(").,]>")
    host = (urlparse(url).netloc or "").lower()
    if not host:
        return None
    if host.startswith("www."):
        host = host[4:]
    if not any(host == allowed or host.endswith(f".{allowed}") for allowed in _SUPPORTED_HOSTS):
        return None
    return url


def _cookies_path(platform: str = "") -> Optional[str]:
    """Cookie jar for yt-dlp — per platform first, then a generic one."""
    env_names = []
    if platform == "instagram":
        env_names.append("IG_COOKIES_FILE")
    elif platform == "facebook":
        env_names.append("FB_COOKIES_FILE")
    elif platform == "tiktok":
        env_names.append("TIKTOK_COOKIES_FILE")
    env_names.append("COOKIES_FILE")
    if platform in ("", "generic"):
        env_names.append("YT_COOKIES_FILE")
    for name in env_names:
        p = (os.getenv(name, "") or "").strip()
        if p and os.path.exists(p):
            return p
    root = os.path.dirname(os.path.dirname(__file__))
    names = [f"{platform}_cookies.txt"] if platform else []
    names.append("cookies.txt")
    for fname in names:
        cand = os.path.join(root, fname)
        if os.path.exists(cand) and os.path.getsize(cand) > 32:
            return cand

    return None


def _download_file(url: str, path: str, referer: str = "", ua: str = "") -> int:
    """Stream a remote file to disk with a size cap. Returns bytes written (0 = fail)."""
    headers = {"User-Agent": ua or _UA_DESKTOP}
    if referer:
        headers["Referer"] = referer
    try:
        with requests.get(url, stream=True, timeout=120, headers=headers) as resp:
            resp.raise_for_status()
            total = 0
            with open(path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1 << 15):
                    if not chunk:
                        continue
                    f.write(chunk)
                    total += len(chunk)
                    if total > MAX_BYTES:
                        raise RuntimeError("too large")
        if total == 0 or _looks_like_html(path):
            raise RuntimeError("not media")
        return total
    except Exception:
        try:
            os.remove(path)
        except Exception:
            pass
        return 0



def platform_from_url(url: str) -> str:
    host = (urlparse(url or "").netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host.endswith("tiktok.com"):
        return "tiktok"
    if host.endswith("instagram.com"):
        return "instagram"
    if host.endswith("facebook.com") or host == "fb.watch":
        return "facebook"
    return "generic"


def _normalize_url(url: str) -> str:
    low = (url or "").lower()
    needs_expand = any([
        "vt.tiktok.com/" in low,
        "vm.tiktok.com/" in low,
        "fb.watch/" in low,
        "facebook.com/share/" in low,
        "facebook.com/reel/" in low,
        "instagram.com/share/" in low,
    ])
    if not needs_expand:
        return url
    try:
        resp = requests.get(
            url,
            headers={
                "User-Agent": _UA_DESKTOP,
                "Referer": "https://www.google.com/",
            },
            timeout=15,
            allow_redirects=True,
        )
        final_url = (resp.url or "").strip()
        if final_url and final_url.startswith("http"):
            return final_url
    except Exception:
        pass
    return url


def _extract_share_target(url: str) -> str:
    """Best-effort expansion for short/share links before yt-dlp touches them."""
    normalized = _normalize_url(url)
    low = (normalized or "").lower()
    if not any(marker in low for marker in (
        "facebook.com/share/",
        "facebook.com/reel/",
        "facebook.com/reels/",
        "fb.watch/",
        "instagram.com/share/",
        "instagram.com/reel/",
        "instagram.com/reels/",
    )):
        return normalized
    try:
        # HEAD first — faster, avoids hanging on huge HTML bodies.
        try:
            resp = requests.head(
                normalized,
                headers={
                    "User-Agent": _UA_IOS,
                    "Referer": "https://www.facebook.com/",
                },
                timeout=10,
                allow_redirects=True,
            )
        except Exception:
            resp = requests.get(
                normalized,
                headers={
                    "User-Agent": _UA_IOS,
                    "Referer": "https://www.facebook.com/",
                },
                timeout=10,
                allow_redirects=True,
                stream=True,
            )
            try:
                resp.close()
            except Exception:
                pass
        final_url = (resp.url or "").strip()
        content_type = (resp.headers.get("content-type") or "").lower()
        if resp.status_code < 400 and final_url.startswith("http") and "text/html" not in content_type:
            return final_url
        if resp.status_code < 400 and final_url.startswith("http") and final_url != normalized:
            return final_url
    except Exception:
        pass
    return normalized


def _run_ffmpeg(args: list[str], timeout: int = 240) -> None:
    proc = subprocess.run(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="ignore").strip()
        raise RuntimeError(err or "ffmpeg failed")


def _looks_like_html(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            head = f.read(2048).lower()
    except Exception:
        return False
    return any(marker in head for marker in (
        b"<!doctype html",
        b"<html",
        b"<head",
        b"<body",
        b"facebook helps you connect",
    ))


_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".gif"}


def _is_image(path: str) -> bool:
    if os.path.splitext(path or "")[1].lower() in _IMAGE_EXTS:
        return True
    try:
        with open(path, "rb") as f:
            head = f.read(12)
    except Exception:
        return False
    return (head.startswith(b"\xff\xd8\xff")            # jpeg
            or head.startswith(b"\x89PNG")              # png
            or head[:4] == b"RIFF" and head[8:12] == b"WEBP")



def _probe_media(path: str) -> dict:
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=codec_name,width,height,pix_fmt:format=duration",
            "-of", "default=noprint_wrappers=1:nokey=0",
            path,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=_VIDEO_PROBE_TIMEOUT,
        check=False,
    )
    data: dict[str, str | int] = {}
    if proc.returncode != 0:
        return data
    for line in proc.stdout.decode("utf-8", errors="ignore").splitlines():
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k in {"width", "height"}:
            try:
                data[k] = int(v)
            except Exception:
                pass
        elif k == "duration":
            try:
                data[k] = max(0, int(float(v) + 0.5))
            except Exception:
                pass
        else:
            data[k] = v.strip()
    return data


def _ensure_telegram_media(path: str, audio_only: bool) -> str:
    """Convert/remux media to Telegram-friendly formats when needed."""
    if not path or not os.path.exists(path):
        return path
    if _looks_like_html(path):
        raise RuntimeError(
            "Downloaded page instead of media stream. The site likely returned a login/consent page."
        )

    base, ext = os.path.splitext(path)
    ext = ext.lower()

    if audio_only:
        if ext == ".mp3":
            return path
        target = base + ".mp3"
        _run_ffmpeg([
            "ffmpeg", "-y", "-i", path,
            "-vn", "-c:a", "libmp3lame", "-b:a", "128k",
            target,
        ], timeout=180)
    else:
        meta = _probe_media(path)
        needs_full_transcode = (
            ext not in {".mp4", ".m4v", ".mov"}
            or meta.get("codec_name") != "h264"
            or meta.get("pix_fmt") != "yuv420p"
            or not meta.get("width")
            or not meta.get("height")
        )
        if needs_full_transcode:
            target = base + ".fixed.mp4"
            _run_ffmpeg([
                "ffmpeg", "-y", "-i", path,
                "-map", "0:v:0", "-map", "0:a:0?",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
                "-pix_fmt", "yuv420p",
                "-profile:v", "baseline",
                "-level", "3.1",
                "-c:a", "aac", "-ac", "2", "-b:a", "128k",
                "-movflags", "+faststart",
                target,
            ], timeout=300)
        else:
            target = base + ".tg.mp4"
            try:
                _run_ffmpeg([
                    "ffmpeg", "-y", "-i", path,
                    "-map", "0:v:0", "-map", "0:a:0?",
                    "-c:v", "copy",
                    "-c:a", "aac", "-ac", "2", "-b:a", "128k",
                    "-movflags", "+faststart",
                    target,
                ], timeout=240)
            except Exception:
                target = base + ".recode.mp4"
                _run_ffmpeg([
                    "ffmpeg", "-y", "-i", path,
                    "-map", "0:v:0", "-map", "0:a:0?",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
                    "-pix_fmt", "yuv420p",
                    "-profile:v", "baseline",
                    "-level", "3.1",
                    "-c:a", "aac", "-ac", "2", "-b:a", "128k",
                    "-movflags", "+faststart",
                    target,
                ], timeout=300)

    if not os.path.exists(target):
        return path
    if os.path.getsize(target) > MAX_BYTES:
        try:
            os.remove(target)
        except Exception:
            pass
        raise RuntimeError("Converted file is still too large for Telegram.")
    try:
        if os.path.abspath(target) != os.path.abspath(path):
            os.remove(path)
    except Exception:
        pass
    return target


def _ydl_base(url: str) -> dict:
    opts: dict = {
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": 3,
        "fragment_retries": 5,
        "socket_timeout": 30,
        "nocheckcertificate": True,
        "geo_bypass": True,
        "concurrent_fragment_downloads": 1,
        "merge_output_format": "mp4",
        "http_headers": {
            "User-Agent": _UA_DESKTOP,
            "Accept-Language": "en-US,en;q=0.9",
        },
        "extractor_retries": 2,
    }
    platform = platform_from_url(url)
    if platform == "tiktok":
        opts["http_headers"].update({
            "Referer": "https://www.tiktok.com/",
            "Origin": "https://www.tiktok.com",
        })
    elif platform in {"facebook", "instagram"}:
        opts["http_headers"].update({
            "Referer": f"https://www.{platform}.com/",
        })
        opts["format_sort"] = [
            "hasvid",
            "quality",
            "res",
            "fps",
            "vcodec:h264",
            "acodec:aac",
            "ext:mp4:m4a",
        ]
    cookies = _cookies_path(platform)
    if cookies:
        opts["cookiefile"] = cookies
    return opts



# Format ladder (descending preference). Each tier stays under MAX_BYTES.
_FORMAT_LADDER = [
    f"best[ext=mp4][filesize<=?{MAX_BYTES}][filesize_approx<=?{MAX_BYTES}]",
    f"best[filesize<=?{MAX_BYTES}][filesize_approx<=?{MAX_BYTES}]",
    "best[ext=mp4]/best",
    "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]",
    "bv*+ba/b",
    "best",
]

# Audio-only ladder
_AUDIO_LADDER = [
    f"ba[ext=m4a][filesize<=?{MAX_BYTES}][filesize_approx<=?{MAX_BYTES}]/"
    f"ba[filesize<=?{MAX_BYTES}][filesize_approx<=?{MAX_BYTES}]",
    "bestaudio[ext=m4a]/bestaudio/best",
    "best",
]


def _make_progress_hook(cb: Optional[Callable[[dict], None]]):
    if not cb:
        return None
    last = {"t": 0.0}

    def hook(d: dict):
        try:
            now = time.time()
            if d.get("status") == "downloading":
                if now - last["t"] < 1.2:  # throttle
                    return
                last["t"] = now
            cb({
                "status": d.get("status"),
                "downloaded": d.get("downloaded_bytes") or 0,
                "total": d.get("total_bytes") or d.get("total_bytes_estimate") or 0,
                "speed": d.get("speed") or 0,
                "eta": d.get("eta") or 0,
            })
        except Exception:
            pass
    return hook


def _probe(url: str) -> dict:
    """Extract metadata without downloading — used to skip oversized files."""
    url = _normalize_url(url)
    opts = _ydl_base(url)
    opts["skip_download"] = True
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        if "entries" in info:
            info = info["entries"][0]
        return info or {}


def _pick_best_size(info: dict) -> int:
    fmts = info.get("formats") or []
    best = 0
    for f in fmts:
        sz = f.get("filesize") or f.get("filesize_approx") or 0
        if sz and sz < MAX_BYTES and sz > best:
            best = sz
    return best


def _tikwm_download(url: str, workdir: str, audio_only: bool) -> Optional[dict]:
    """Reliable TikTok fallback via the public tikwm.com endpoint.

    Handles video, music-only, and photo ("slideshow") posts.
    Returns info dict on success, None on failure.
    """
    data = None
    for attempt in range(2):
        try:
            r = requests.post(
                "https://www.tikwm.com/api/",
                data={"url": url, "hd": "1", "count": "12"},
                headers={"User-Agent": _UA_DESKTOP},
                timeout=25,
            )
            j = r.json()
            if r.status_code == 200 and j.get("code") == 0:
                data = j.get("data") or {}
                break
        except Exception:
            pass
        time.sleep(1.0)
    if data is None:
        return None
    d = data
    title = (d.get("title") or "TikTok")[:200]
    uploader = ((d.get("author") or {}).get("nickname") or "TikTok")

    # Photo / slideshow posts
    pics = d.get("images") or []
    if pics and not audio_only:
        paths = []
        for i, pic in enumerate(pics[:10]):
            if not isinstance(pic, str):
                continue
            if pic.startswith("/"):
                pic = "https://www.tikwm.com" + pic
            p = os.path.join(workdir, f"tt_{i}.jpg")
            if _download_file(pic, p, referer="https://www.tikwm.com/"):
                paths.append(p)
        if paths:
            return {
                "path": None,
                "images": paths,
                "size": sum(os.path.getsize(p) for p in paths),
                "title": title,
                "uploader": uploader,
                "duration": 0,
                "ext": "jpg",
                "thumbnail": d.get("cover"),
                "webpage_url": url,
                "audio_only": False,
            }

    if audio_only:
        media, ext = d.get("music"), "mp3"
    else:
        media, ext = (d.get("hdplay") or d.get("play") or d.get("wmplay")), "mp4"
    if not media:
        return None
    if media.startswith("/"):
        media = "https://www.tikwm.com" + media
    path = os.path.join(workdir, f"tiktok_{int(time.time())}.{ext}")
    size = _download_file(media, path, referer="https://www.tikwm.com/")
    if not size:
        return None
    try:
        path = _ensure_telegram_media(path, audio_only=audio_only)
        size = os.path.getsize(path)
    except Exception:
        pass
    return {
        "path": path,
        "size": size,
        "title": title,
        "uploader": uploader,
        "duration": int(d.get("duration") or 0),
        "ext": os.path.splitext(path)[1].lstrip(".") or ext,
        "thumbnail": d.get("cover"),
        "webpage_url": url,
        "audio_only": audio_only,
    }



_IG_SHORTCODE_RE = re.compile(r"instagram\.com/(?:[A-Za-z0-9_.]+/)?(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)",
                              re.IGNORECASE)
_IG_APP_ID = "936619743392459"
_IG_B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


def _ig_shortcode(url: str) -> Optional[str]:
    m = _IG_SHORTCODE_RE.search(url or "")
    return m.group(1) if m else None


def _ig_media_id(shortcode: str) -> Optional[str]:
    """Instagram shortcodes are base64(media_id) — decode back to the numeric id."""
    try:
        n = 0
        for ch in shortcode:
            n = n * 64 + _IG_B64.index(ch)
        return str(n) if n else None
    except Exception:
        return None


def _ig_img_index(url: str) -> Optional[int]:
    m = re.search(r"[?&]img_index=(\d+)", url or "")
    try:
        return int(m.group(1)) if m else None
    except Exception:
        return None


def _ig_canonical(url: str) -> str:
    """Drop tracking params (?igsh=…) but keep the carousel index."""
    code = _ig_shortcode(url)
    if not code:
        return url
    kind = "reel" if re.search(r"/reels?/", url or "", re.IGNORECASE) else "p"
    idx = _ig_img_index(url)
    base = f"https://www.instagram.com/{kind}/{code}/"
    return f"{base}?img_index={idx}" if idx else base


# --- Cobalt resolver -------------------------------------------------------
# Public cobalt instances return *typed* media (video vs photo) and full
# carousel pickers, so a reel never degrades into its cover image.
_COBALT_INSTANCES = (
    "https://co.otomir23.me",
    "https://cobalt-backend.canine.tools",
    "https://api.cobalt.tools",
    "https://cobalt-api.kwiatekmiki.com",
)


def _cobalt_resolve(url: str, audio_only: bool = False) -> list[dict]:
    """Resolve a social URL into [{'type': 'video'|'photo', 'url': …}, …]."""
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": _UA_DESKTOP,
    }
    body = {
        "url": url,
        "videoQuality": "1080",
        "filenameStyle": "basic",
        "alwaysProxy": False,
    }
    if audio_only:
        body["downloadMode"] = "audio"
        body["audioFormat"] = "mp3"

    for base in _COBALT_INSTANCES:
        h = dict(headers)
        try:
            s = requests.post(base + "/session", headers={"User-Agent": _UA_DESKTOP,
                                                          "Accept": "application/json"},
                              timeout=15)
            if s.status_code == 200:
                tok = (s.json() or {}).get("token")
                if tok:
                    h["Authorization"] = f"Bearer {tok}"
        except Exception:
            pass
        try:
            r = requests.post(base + "/", json=body, headers=h, timeout=60)
            if r.status_code != 200:
                continue
            data = r.json() or {}
        except Exception:
            continue

        status = data.get("status")
        out: list[dict] = []
        if status in {"redirect", "tunnel", "stream"} and data.get("url"):
            out.append({"type": "audio" if audio_only else "video", "url": data["url"]})
        elif status == "picker":
            for it in (data.get("picker") or []):
                u = it.get("url")
                if not u:
                    continue
                kind = (it.get("type") or "photo").lower()
                out.append({"type": "video" if kind in {"video", "gif"} else "photo",
                            "url": u})
            if data.get("audio") and audio_only:
                out.append({"type": "audio", "url": data["audio"]})
        if out:
            return out
    return []


def _media_ext(url: str, fallback: str) -> str:
    path = urlparse(url or "").path.lower()
    for e in (".mp4", ".mov", ".webm", ".jpg", ".jpeg", ".png", ".webp", ".mp3", ".m4a"):
        if path.endswith(e):
            return e
    return fallback


def _to_jpeg(path: str) -> str:
    """Normalise .webp / .heic stills to JPEG so Telegram always accepts them."""
    ext = os.path.splitext(path)[1].lower()
    if ext in {".jpg", ".jpeg", ".png"}:
        return path
    out = os.path.splitext(path)[0] + ".jpg"
    try:
        proc = subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", path, "-frames:v", "1", out],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90, check=False,
        )
        if proc.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 1024:
            try:
                os.remove(path)
            except Exception:
                pass
            return out
    except Exception:
        pass
    return path



def _netscape_cookies(path: str) -> dict:
    """Read a Netscape cookie jar into a simple name→value dict."""
    jar: dict = {}
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) >= 7:
                    jar[parts[5]] = parts[6]
    except Exception:
        pass
    return jar


def _ig_cookie_jar() -> dict:
    p = _cookies_path("instagram")
    return _netscape_cookies(p) if p else {}


def _unescape_media_url(raw: str) -> str:
    try:
        out = raw.encode("utf-8").decode("unicode_escape")
    except Exception:
        out = raw
    out = out.replace("\\/", "/").replace("&amp;", "&")
    return out.strip()


def _ig_pages(code: str) -> list[str]:
    return [
        f"https://www.instagram.com/p/{code}/embed/captioned/",
        f"https://www.instagram.com/reel/{code}/embed/captioned/",
        f"https://www.instagram.com/p/{code}/embed/",
        f"https://www.ddinstagram.com/p/{code}/",
        f"https://ddinstagram.com/reel/{code}/",
        f"https://kkinstagram.com/p/{code}/",
        f"https://kkinstagram.com/reel/{code}/",
        f"https://www.instagramez.com/p/{code}/",
        f"https://imginn.com/p/{code}/",
    ]


def _ig_api_media(code: str) -> tuple[Optional[str], list[str], str, str]:
    """Query Instagram's web API (works well once cookies are configured).

    Returns (video_url, image_urls, title, uploader).
    """
    mid = _ig_media_id(code)
    if not mid:
        return None, [], "", ""
    cookies = _ig_cookie_jar()
    headers = {
        "User-Agent": _UA_DESKTOP,
        "X-IG-App-ID": _IG_APP_ID,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": f"https://www.instagram.com/p/{code}/",
    }
    endpoints = [
        f"https://www.instagram.com/api/v1/media/{mid}/info/",
        f"https://i.instagram.com/api/v1/media/{mid}/info/",
    ]
    for ep in endpoints:
        try:
            r = requests.get(ep, headers=headers, cookies=cookies or None, timeout=20)
            if r.status_code != 200:
                continue
            data = r.json()
        except Exception:
            continue
        items = (data or {}).get("items") or []
        if not items:
            continue
        item = items[0]
        uploader = ((item.get("user") or {}).get("username") or "Instagram")
        cap = ((item.get("caption") or {}) or {}).get("text") or ""
        title = cap.strip()[:200]

        def _pick(node) -> tuple[Optional[str], Optional[str]]:
            vids = node.get("video_versions") or []
            if vids:
                return vids[0].get("url"), None
            cands = ((node.get("image_versions2") or {}).get("candidates") or [])
            if cands:
                return None, cands[0].get("url")
            return None, None

        carousel = item.get("carousel_media") or []
        if carousel:
            imgs: list[str] = []
            for node in carousel:
                v, i = _pick(node)
                if v:
                    return v, [], title, uploader
                if i:
                    imgs.append(i)
            if imgs:
                return None, imgs, title, uploader
        v, i = _pick(item)
        if v:
            return v, [], title, uploader
        if i:
            return None, [i], title, uploader
    return None, [], "", ""


def _ig_fallback_download(url: str, workdir: str, audio_only: bool) -> Optional[dict]:
    """Instagram fallback: cobalt → web API → embed page → public mirrors.

    Handles reels/videos, single photos, and mixed carousels — used whenever
    yt-dlp's Instagram extractor is blocked (very common from server IPs).
    """
    code = _ig_shortcode(url)
    if not code:
        return None

    video_url: Optional[str] = None
    images: list[str] = []
    title = ""
    uploader = "Instagram"

    # 0) Cobalt — typed media, so a reel is never downgraded to its cover image.
    try:
        items = _cobalt_resolve(url, audio_only=audio_only)
    except Exception:
        items = []
    if items:
        if audio_only:
            au = next((i["url"] for i in items if i["type"] in {"audio", "video"}), None)
            if au:
                video_url = au
        else:
            vids = [i for i in items if i["type"] == "video"]
            photos = [i["url"] for i in items if i["type"] == "photo"]
            if vids:
                video_url = vids[0]["url"]
            else:
                # A post URL can contain ?img_index=N simply because it was
                # copied while that slide was open. Download the complete
                # carousel so Telegram can render it as one album/slider.
                images = photos


    # 1) Official web API (best quality; honours cookies when configured)
    if not video_url and not images:
        try:
            v, imgs, t, u = _ig_api_media(code)
            if t:
                title = t
            if u:
                uploader = u
            if v:
                video_url = v
            elif imgs and not audio_only:
                images = imgs
        except Exception:
            pass


    # 2) HTML scraping of embed pages / public mirrors
    if not video_url and not images:
        for cand in _ig_pages(code):
            try:
                r = requests.get(
                    cand,
                    headers={
                        "User-Agent": _UA_IOS,
                        "Accept-Language": "en-US,en;q=0.9",
                        "Referer": "https://www.instagram.com/",
                    },
                    timeout=15,
                    allow_redirects=True,
                )
                if r.status_code != 200 or not r.text:
                    continue
                html = r.text
                m = (
                    re.search(r'"video_url":"([^"]+\.mp4[^"]*)"', html)
                    or re.search(r'"contentUrl":"([^"]+\.mp4[^"]*)"', html)
                    or re.search(r'"playback_url":"([^"]+)"', html)
                    or re.search(r'property="og:video"\s+content="([^"]+)"', html)
                    or re.search(r'property="og:video:secure_url"\s+content="([^"]+)"', html)
                    or re.search(r'<video[^>]+src="([^"]+\.mp4[^"]*)"', html)
                    or re.search(r'href="(https?://[^"]+\.mp4[^"]*)"', html)
                )
                tm = re.search(r"<title>([^<]+)</title>", html)
                if tm and not title:
                    title = tm.group(1).strip()[:200]
                um = (re.search(r'"owner":\{"username":"([^"]+)"', html)
                      or re.search(r'"author_name":"([^"]+)"', html))
                if um:
                    uploader = um.group(1)
                if m:
                    video_url = _unescape_media_url(m.group(1))
                    break
                if not audio_only:
                    found = re.findall(r'"display_url":"([^"]+)"', html) \
                        or re.findall(r'property="og:image"\s+content="([^"]+)"', html) \
                        or re.findall(r'class="EmbeddedMediaImage"[^>]+src="([^"]+)"', html)
                    for f in found:
                        u2 = _unescape_media_url(f)
                        if u2.startswith("http") and u2 not in images:
                            images.append(u2)
                    if images:
                        break
            except Exception:
                continue

    if video_url:
        path = os.path.join(workdir, f"ig_{code}.mp4")
        size = _download_file(video_url, path, referer="https://www.instagram.com/", ua=_UA_IOS)
        if size:
            if audio_only:
                try:
                    path = _ensure_telegram_media(path, audio_only=True)
                    size = os.path.getsize(path)
                except Exception:
                    return None
            else:
                try:
                    path = _ensure_telegram_media(path, audio_only=False)
                    size = os.path.getsize(path)
                except Exception:
                    return None
            meta = _probe_media(path) if not audio_only else {}
            if not audio_only and not meta.get("duration"):
                return None
            return {
                "path": path,
                "size": size,
                "title": title or "Instagram",
                "uploader": uploader,
                "duration": meta.get("duration") or 0,
                "ext": os.path.splitext(path)[1].lstrip(".") or "mp4",
                "width": meta.get("width") or 0,
                "height": meta.get("height") or 0,
                "thumbnail": None,
                "webpage_url": url,
                "audio_only": audio_only,
            }

    if images and not audio_only:
        paths = []
        for i, iu in enumerate(images[:10]):
            p = os.path.join(workdir, f"ig_{code}_{i}{_media_ext(iu, '.jpg')}")
            if _download_file(iu, p, referer="https://www.instagram.com/", ua=_UA_IOS):
                paths.append(_to_jpeg(p))

        if paths:
            return {
                "path": None,
                "images": paths,
                "size": sum(os.path.getsize(p) for p in paths),
                "title": title or "Instagram",
                "uploader": uploader,
                "duration": 0,
                "ext": "jpg",
                "thumbnail": None,
                "webpage_url": url,
                "audio_only": False,
            }
    return None



def _fb_fallback_download(url: str, workdir: str, audio_only: bool) -> Optional[dict]:
    """Facebook fallback: scrape the mobile/basic page for a direct stream."""
    targets = [url]
    low = (url or "").lower()
    if "facebook.com" in low:
        targets.append(re.sub(r"//(www\.|m\.|web\.)?facebook\.com", "//m.facebook.com", url, count=1))
        targets.append(re.sub(r"//(www\.|m\.|web\.)?facebook\.com", "//mbasic.facebook.com", url, count=1))

    media_url = None
    images: list[str] = []
    title = "Facebook"
    for cand in dict.fromkeys(targets):
        try:
            r = requests.get(
                cand,
                headers={
                    "User-Agent": _UA_IOS,
                    "Accept-Language": "en-US,en;q=0.9",
                    "Referer": "https://www.facebook.com/",
                },
                timeout=20,
                allow_redirects=True,
            )
            if r.status_code != 200 or not r.text:
                continue
            html = r.text
            m = (
                re.search(r'"browser_native_hd_url":"([^"]+)"', html)
                or re.search(r'"browser_native_sd_url":"([^"]+)"', html)
                or re.search(r'"playable_url_quality_hd":"([^"]+)"', html)
                or re.search(r'"playable_url":"([^"]+)"', html)
                or re.search(r'hd_src(?:_no_ratelimit)?:"([^"]+)"', html)
                or re.search(r'sd_src(?:_no_ratelimit)?:"([^"]+)"', html)
                or re.search(r'property="og:video:url"\s+content="([^"]+)"', html)
            )
            tm = re.search(r"<title>([^<]+)</title>", html)
            if tm:
                title = tm.group(1).strip()[:200] or title
            if m:
                media_url = _unescape_media_url(m.group(1))
                break
            if not audio_only:
                for f in re.findall(r'property="og:image"\s+content="([^"]+)"', html):
                    u = _unescape_media_url(f)
                    if u.startswith("http") and u not in images:
                        images.append(u)
                if images:
                    break
        except Exception:
            continue

    if media_url:
        path = os.path.join(workdir, f"fb_{int(time.time())}.mp4")
        size = _download_file(media_url, path, referer="https://www.facebook.com/", ua=_UA_IOS)
        if size:
            try:
                path = _ensure_telegram_media(path, audio_only=audio_only)
                size = os.path.getsize(path)
            except Exception:
                if audio_only:
                    return None
            return {
                "path": path,
                "size": size,
                "title": title,
                "uploader": "Facebook",
                "duration": 0,
                "ext": os.path.splitext(path)[1].lstrip(".") or "mp4",
                "thumbnail": None,
                "webpage_url": url,
                "audio_only": audio_only,
            }

    if images and not audio_only:
        paths = []
        for i, iu in enumerate(images[:10]):
            p = os.path.join(workdir, f"fb_{i}.jpg")
            if _download_file(iu, p, referer="https://www.facebook.com/", ua=_UA_IOS):
                paths.append(p)
        if paths:
            return {
                "path": None,
                "images": paths,
                "size": sum(os.path.getsize(p) for p in paths),
                "title": title,
                "uploader": "Facebook",
                "duration": 0,
                "ext": "jpg",
                "thumbnail": None,
                "webpage_url": url,
                "audio_only": False,
            }
    return None



def _sync_download(url: str, workdir: str, progress: Optional[Callable] = None,
                   audio_only: bool = False) -> dict:
    url = _extract_share_target(url)
    outtmpl = os.path.join(workdir, "%(id).40s.%(ext)s")
    platform = platform_from_url(url)
    if platform == "generic":
        raise RuntimeError("[generic] Only Facebook, Instagram, and TikTok links are allowed.")

    if platform == "instagram":
        # Strip ?igsh= / utm tracking — those frequently break extraction.
        url = _ig_canonical(url)

    # TikTok: try tikwm.com first — it bypasses most yt-dlp issues.
    if platform == "tiktok":
        tik = _tikwm_download(url, workdir, audio_only)
        if tik:
            return tik

    # Instagram: the typed resolver chain (cobalt → web API → mirrors) beats
    # yt-dlp from datacenter IPs, and it never returns a cover image for a reel.
    if platform == "instagram":
        ig = _ig_fallback_download(url, workdir, audio_only)
        if ig:
            return ig



    # Pre-flight probe (non-fatal if it fails — some sites block extraction-only).
    try:
        probe = _probe(url)
        dur = probe.get("duration") or 0
        # crude bitrate floor — 1 hour @ 128kbps is already ~57MB; warn early
        if dur and dur > 7200:
            raise RuntimeError("Video too long to fit Telegram's 50 MB limit.")
    except RuntimeError:
        raise
    except Exception:
        pass  # tolerate probe failure

    last_err: Optional[Exception] = None
    raw_errors: list[str] = []
    hook = _make_progress_hook(progress)
    ladder = _AUDIO_LADDER if audio_only else _FORMAT_LADDER

    for tier_idx, fmt in enumerate(ladder):
        opts = _ydl_base(url)
        opts["outtmpl"] = outtmpl
        opts["format"] = fmt
        if tier_idx < max(0, len(ladder) - 2):
            opts["max_filesize"] = MAX_BYTES
        if hook:
            opts["progress_hooks"] = [hook]
        if audio_only:
            opts["postprocessors"] = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }]
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                if not info:
                    raise RuntimeError("No media metadata returned.")
                entries = info.get("entries") if isinstance(info, dict) else None
                if entries:
                    entries = [e for e in entries if e]
                    if not entries:
                        raise RuntimeError("No downloadable entries.")
                    # Photo carousel → collect every downloaded image
                    pics = []
                    for e in entries:
                        p = ydl.prepare_filename(e)
                        if os.path.exists(p) and _is_image(p):
                            pics.append(p)
                    if pics and not audio_only:
                        return {
                            "path": None,
                            "images": pics[:10],
                            "size": sum(os.path.getsize(p) for p in pics[:10]),
                            "title": (info.get("title") or "")[:200],
                            "uploader": info.get("uploader") or info.get("channel") or "",
                            "duration": 0,
                            "ext": "jpg",
                            "thumbnail": info.get("thumbnail"),
                            "webpage_url": info.get("webpage_url") or url,
                            "audio_only": False,
                        }
                    info = entries[0]
                path = ydl.prepare_filename(info)
                if not os.path.exists(path):
                    base, _ = os.path.splitext(path)
                    for ext in (".mp3", ".m4a", ".mp4", ".mkv", ".webm", ".mov",
                                ".opus", ".ogg", ".jpg", ".jpeg", ".png", ".webp"):
                        if os.path.exists(base + ext):
                            path = base + ext
                            break
                if not os.path.exists(path):
                    raise RuntimeError("Downloaded file vanished.")
                if _is_image(path) and not audio_only:
                    return {
                        "path": None,
                        "images": [path],
                        "size": os.path.getsize(path),
                        "title": (info.get("title") or "")[:200],
                        "uploader": info.get("uploader") or info.get("channel") or "",
                        "duration": 0,
                        "ext": os.path.splitext(path)[1].lstrip("."),
                        "thumbnail": info.get("thumbnail"),
                        "webpage_url": info.get("webpage_url") or url,
                        "audio_only": False,
                    }
                path = _ensure_telegram_media(path, audio_only=audio_only)

                size = os.path.getsize(path)
                if size == 0:
                    raise RuntimeError("Empty download.")
                if size > MAX_BYTES:
                    # Try next, smaller tier.
                    try: os.remove(path)
                    except Exception: pass
                    last_err = RuntimeError(
                        f"Too large at tier {tier_idx+1} ({size/1024/1024:.1f} MB)."
                    )
                    continue
                meta = _probe_media(path) if not audio_only else {}
                return {
                    "path": path,
                    "size": size,
                    "title": (info.get("title") or "")[:200],
                    "uploader": info.get("uploader") or info.get("channel") or "",
                    "duration": info.get("duration") or 0,
                    "ext": os.path.splitext(path)[1].lstrip("."),
                    "width": meta.get("width") or info.get("width") or 0,
                    "height": meta.get("height") or info.get("height") or 0,
                    "thumbnail": info.get("thumbnail"),
                    "webpage_url": info.get("webpage_url") or url,
                    "audio_only": audio_only,
                }
        except yt_dlp.utils.DownloadError as e:
            last_err = e
            msg = str(e).lower()
            raw_errors.append(str(e)[:500])
            if "max-filesize" in msg or "file is larger" in msg \
               or "requested format is not available" in msg:
                continue
            # blocked / auth walls → stop hammering tiers, go straight to fallbacks
            if any(t in msg for t in (
                "cannot parse data", "no video formats found", "sign in to confirm",
                "login required", "private", "rate-limit", "age",
            )):
                break
            continue
        except Exception as e:
            last_err = e
            raw_errors.append(str(e)[:500])
            continue

    # Platform-specific last-ditch fallbacks once yt-dlp gave up.
    if platform == "tiktok":
        tik = _tikwm_download(url, workdir, audio_only)
        if tik:
            return tik

    if platform == "instagram":
        ig = _ig_fallback_download(url, workdir, audio_only)
        if ig:
            return ig

    if platform == "facebook":
        fb = _fb_fallback_download(url, workdir, audio_only)
        if fb:
            return fb


    # Final universal fallback: let yt-dlp choose whatever single best stream exists,
    # then normalize it for Telegram with ffmpeg.
    try:
        opts = _ydl_base(url)
        opts["outtmpl"] = outtmpl
        opts["format"] = "best"
        opts.pop("max_filesize", None)
        if hook:
            opts["progress_hooks"] = [hook]
        if audio_only:
            opts["postprocessors"] = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }]
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if "entries" in info:
                info = info["entries"][0]
            path = ydl.prepare_filename(info)
            if not os.path.exists(path):
                base, _ = os.path.splitext(path)
                for ext in (".mp3", ".m4a", ".mp4", ".mkv", ".webm", ".mov", ".opus", ".ogg"):
                    if os.path.exists(base + ext):
                        path = base + ext
                        break
            if not os.path.exists(path):
                raise RuntimeError("Downloaded file vanished.")
            path = _ensure_telegram_media(path, audio_only=audio_only)
            size = os.path.getsize(path)
            if size > MAX_BYTES:
                raise RuntimeError("Converted file is still too large for Telegram.")
            meta = _probe_media(path) if not audio_only else {}
            return {
                "path": path,
                "size": size,
                "title": (info.get("title") or "")[:200],
                "uploader": info.get("uploader") or info.get("channel") or "",
                "duration": info.get("duration") or 0,
                "ext": os.path.splitext(path)[1].lstrip("."),
                "width": meta.get("width") or info.get("width") or 0,
                "height": meta.get("height") or info.get("height") or 0,
                "thumbnail": info.get("thumbnail"),
                "webpage_url": info.get("webpage_url") or url,
                "audio_only": audio_only,
            }
    except Exception as e:
        last_err = e

    if last_err:
        raise RuntimeError(f"[{platform}] {last_err}")
    raise RuntimeError(f"[{platform}] Download failed after all fallbacks.")


async def download(url: str, progress: Optional[Callable] = None,
                   audio_only: bool = False) -> dict:
    """Download a video, audio or image post. Returns an info dict or raises."""
    workdir = tempfile.mkdtemp(prefix="dl_")
    try:
        info = await asyncio.to_thread(_sync_download, url, workdir, progress, audio_only)
        images = info.get("images") or []
        if images:
            # keep only images that fit Telegram's photo limit
            kept = [p for p in images if os.path.exists(p) and 0 < os.path.getsize(p) <= MAX_BYTES]
            if not kept:
                raise RuntimeError("No usable images found.")
            info["images"] = kept
            info["size"] = sum(os.path.getsize(p) for p in kept)
        elif (info.get("size") or 0) > MAX_BYTES:
            try: os.remove(info["path"])
            except Exception: pass
            raise RuntimeError(
                f"File too large for Telegram ({info['size']/1024/1024:.1f} MB). "
                f"Max {MAX_BYTES/1024/1024:.0f} MB."
            )
        info["_workdir"] = workdir
        return info
    except Exception:
        shutil.rmtree(workdir, ignore_errors=True)
        raise



def user_error_text(err: Exception) -> str:
    msg = str(err or "Download failed").strip()
    low = msg.lower()
    platform = "generic"
    m = re.match(r"^\[([a-z0-9_:-]+)\]\s*(.*)$", msg, flags=re.IGNORECASE)
    if m:
        platform = m.group(1).lower()
        low = m.group(2).lower()
    # Keep a couple of useful, neutral cases — everything else collapses to the
    # single clean "no downloadable video" message in bold English.
    if "too long" in low or "too large" in low or "max " in low or "converted file is still too large" in low:
        return "<b>File is too large to send on Telegram ❌</b>"
    if "only facebook, instagram, and tiktok links are allowed" in low or "unsupported url" in low:
        return "<b>Only Facebook, Instagram and TikTok links are supported ❌</b>"
    if "timed out" in low or "timeout" in low:
        return "<b>The site took too long to respond. Please try again ❌</b>"
    if platform == "instagram" and any(t in low for t in (
        "login", "cookies", "isn't available to everyone", "certain audiences",
        "restricted", "private", "csrf", "rate-limit", "429",
    )):
        return ("<b>Instagram blocked this download 🔒</b>\n"
                "This post is login-restricted. Ask the owner to add an "
                "Instagram cookie file (<code>IG_COOKIES_FILE</code>) to unlock it.")
    return "<b>No downloadable video was found ❌</b>"



def cleanup(info: dict):
    wd = info.get("_workdir")
    if wd:
        shutil.rmtree(wd, ignore_errors=True)
