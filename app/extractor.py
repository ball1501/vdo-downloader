"""Find downloadable video formats for a page/media URL.

Three-layer strategy:
1. yt-dlp's own extractors (jwplayer-style embeds, file hosts, direct media).
   YouTube embeds found this way are treated as trailers and skipped.
2. Static probe: fetch the HTML ourselves, scan for direct media URLs
   (including escaped/packed JS and multi-level iframes), skipping ad mp4s.
3. Headless-browser sniffing (see sniffer.py) for players that only fetch
   the video via JS after clicking play.

Every request carries browser-like Referer/User-Agent; the sniffed media
URL also carries the exact headers + cookies the player used, and those are
returned to the downloader for the actual file transfer.
"""

from __future__ import annotations

import re
import urllib.parse
from collections import deque

import requests
import yt_dlp

from . import sniffer

USER_AGENT = sniffer.USER_AGENT

# media URLs often appear urlencoded or with query strings; allow most chars
_MEDIA_RE = re.compile(
    r"""(?:https?:)?//[^\s"'<>\\()\[\]]+?\.(?:m3u8|mp4|mpd)[^\s"'<>\\()\[\]]*"""
)
# player configs usually hide the media under a key like file: "..." / src: "..."
_KEYVAL_RE = re.compile(
    r"""["']?(?:file|videoUrl|video_url|streamUrl|playUrl|sourceUrl|source|src|url)["']?"""
    r"""\s*[:=]\s*["']([^"']{10,})["']""",
    re.I,
)
_PACKED_RE = re.compile(
    r"eval\(function\(p,a,c,k,e,[dr]\).{0,200}?\(\s*'(.*?)'\s*,\s*(\d+)\s*,"
    r"\s*(\d+)\s*,\s*'(.*?)'\.split\('\|'\)",
    re.S,
)
_IFRAME_RE = re.compile(r"""<iframe[^>]+?src=["']([^"']+)["']""", re.I)
# iframe src like player.php?link=https://real-player/... — the real player
# URL is handed over in the query string
_QUERY_LINK_RE = re.compile(r"""[?&](?:link|url|u|embed|play|source)=(https?://[^&'"]+|[^&'"]*%3A%2F%2F[^&'"]+)""", re.I)
# JS vars that point at the page to load after an ad gate
_DESTVAR_RE = re.compile(
    r"""(?:window\.)?(?:destinationURL|destinationUrl|destUrl|redirectUrl|nextUrl|"""
    r"""mainUrl|playerUrl|embedUrl|iframeSrc)\s*=\s*["']([^"']{10,})["']"""
)

_IMAGE_EXTS = {"webp", "jpg", "jpeg", "png", "gif", "avif", "svg", "bmp", "ico"}

# trailer/advertising sources that are never the actual movie
_SKIP_DOMAIN_RE = sniffer.SKIP_RE
# tokens that mark a JS region as an ad config (ad mp4s live there)
_AD_CONTEXT_RE = re.compile(
    r"(?i)(?:adSettings|adverts|mp4Url|skipSeconds|can_skip_after|\"ad(s)?\"\s*[:=])"
)

EXTRACT_HINT = (
    "ลอง: เปิดหน้าเว็บ → กด F12 → แท็บ Network → พิมพ์ m3u8 ในช่องกรอง → "
    "กดเล่นวิดีโอ → คัดลอก URL ที่ขึ้นมาวางในช่องลิงก์ (หรือวางลิงก์ iframe ของ player โดยตรง)"
)


class ExtractError(Exception):
    pass


# ---- packed JS unpacking ---------------------------------------------------
def _decode_token(token: str, radix: int) -> int | None:
    """Inverse of Dean Edwards' packer encoder (base-36/62)."""
    n = 0
    for ch in token:
        o = ord(ch)
        if 48 <= o <= 57:
            v = o - 48
        elif 97 <= o <= 122:
            v = o - 87
        elif 65 <= o <= 90:
            v = o - 29
        else:
            return None
        if v >= radix:
            return None
        n = n * radix + v
    return n


def _unpack_packed_js(text: str) -> list[str]:
    """Return the unpacked bodies of eval(function(p,a,c,k,e,d)) blocks."""
    out = []
    for payload, radix_s, _count, keys in _PACKED_RE.findall(text)[:10]:
        words = keys.split("|")

        def sub(m: re.Match) -> str:
            i = _decode_token(m.group(0), int(radix_s))
            if i is not None and i < len(words):
                return words[i]
            return m.group(0)

        out.append(re.sub(r"[0-9a-zA-Z]+", sub, payload))
    return out


# ---- URL scanning ----------------------------------------------------------
def _absolute(url: str, base: str) -> str | None:
    if url.startswith("//"):
        return "https:" + url
    if not url.startswith("http"):
        return urllib.parse.urljoin(base, url)
    return url


def _unescape(text: str) -> str:
    """Undo JSON/JS escaping that hides URLs, e.g. https:\\/\\/cdn\\/file.m3u8."""
    text = text.replace("\\/", "/")
    text = re.sub(r"\\u002[fF]", "/", text)
    text = re.sub(r"\\u0026", "&", text)
    text = re.sub(r"\\u003[dD]", "=", text)
    text = re.sub(r"\\u003[fF]", "?", text)
    return text


def _is_ad_trap(context: str, url: str) -> bool:
    return bool(_SKIP_DOMAIN_RE.search(url)) or bool(_AD_CONTEXT_RE.search(context))


def _scan_media_urls(text: str, base_url: str) -> list[str]:
    text = _unescape(text)
    found = []
    for m in _MEDIA_RE.finditer(text):
        context = text[max(0, m.start() - 400): m.end() + 100]
        url = _absolute(m.group(0), base_url)
        if url and url not in found and not _is_ad_trap(context, url):
            found.append(url)
    # player configs that reference the media under a key (file/src/source: ...)
    for m in _KEYVAL_RE.finditer(text):
        url = _absolute(_unescape(m.group(1)).strip(), base_url)
        context = text[max(0, m.start() - 200): m.end()]
        if (
            url
            and url not in found
            and re.search(r"\.(?:m3u8|mp4|mpd)(?:[?#]|$)", url, re.I)
            and not _is_ad_trap(context, url)
        ):
            found.append(url)
    return found


def _fetch(url: str, referer: str, timeout: int = 12) -> str:
    resp = requests.get(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Referer": referer,
            "Accept-Language": "th,en;q=0.8",
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or "utf-8"
    return resp.text


_PAGE_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)
_GENERIC_TITLES = {"master", "playlist", "index", "video", "main", "hls", "stream"}


def _page_title(html: str) -> str | None:
    m = _PAGE_TITLE_RE.search(html)
    if not m:
        og = re.search(
            r"""<meta[^>]+property=["']og:title["'][^>]+content=["']([^"']+)["']""",
            html,
            re.I,
        ) or re.search(
            r"""<meta[^>]+content=["']([^"']+)["'][^>]+property=["']og:title["']""",
            html,
            re.I,
        )
        return og.group(1).strip() if og else None
    return m.group(1).strip()


def _choose_title(info_title: str | None, page_title: str | None, url: str) -> str:
    """Prefer the info title unless it's a generic m3u8 filename."""
    if info_title and info_title.strip().lower() not in _GENERIC_TITLES:
        return info_title
    if page_title:
        return page_title
    return info_title or url


def _probe(url: str, max_pages: int = 6) -> dict:
    """BFS through the page and its iframes (with referer chaining).

    Returns {"media": [(media_url, referer)], "players": [(player_page_url,
    referer)], "iframes": [(iframe_url, referer)]} — media sorted m3u8-first;
    "players" are direct handles to the real player (query-string links and
    post-ad destination vars) that the browser sniffer should open directly.
    """
    media: list[tuple[str, str]] = []
    players: list[tuple[str, str]] = []
    iframes: list[tuple[str, str]] = []
    seen: set[str] = set()
    visited: set[str] = set()
    queue: deque[tuple[str, str, int]] = deque([(url, url, 0)])
    page_title: str | None = None

    while queue and len(visited) < max_pages:
        page_url, referer, depth = queue.popleft()
        if page_url in visited:
            continue
        visited.add(page_url)
        try:
            html = _fetch(page_url, referer)
        except requests.RequestException:
            continue
        if depth == 0:
            page_title = _page_title(html)

        for text in [html] + _unpack_packed_js(html)[:5]:
            for m in _scan_media_urls(text, page_url):
                if m not in seen:
                    seen.add(m)
                    media.append((m, page_url))
            for dest in _DESTVAR_RE.findall(text):
                dest_url = _absolute(_unescape(dest).strip(), page_url)
                if dest_url and dest_url not in seen and not _SKIP_DOMAIN_RE.search(dest_url):
                    seen.add(dest_url)
                    players.append((dest_url, page_url))

        for iframe in _IFRAME_RE.findall(html)[:6]:
            iframe_url = _absolute(iframe, page_url)
            if not iframe_url or _SKIP_DOMAIN_RE.search(iframe_url):
                continue
            if iframe_url not in seen:
                seen.add(iframe_url)
                iframes.append((iframe_url, page_url))
            # real player URL handed over via ?link= / ?url= query params
            qm = _QUERY_LINK_RE.search(iframe_url)
            if qm:
                target = urllib.parse.unquote(qm.group(1))
                target = _absolute(target, page_url)
                if target and target not in seen and not _SKIP_DOMAIN_RE.search(target):
                    seen.add(target)
                    players.append((target, page_url))
            if depth < 2:
                queue.append((iframe_url, page_url, depth + 1))

    # video (m3u8) candidates before plain mp4 — ads are usually mp4
    media.sort(key=lambda c: not c[0].lower().split("?")[0].endswith(".m3u8"))
    return {"media": media[:8], "players": players[:4], "iframes": iframes[:4], "page_title": page_title}


# ---- yt-dlp extraction -----------------------------------------------------
def _ydl_options(referer: str, timeout: int = 25, extra_headers: dict | None = None) -> dict:
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": referer,
        "Accept-Language": "th,en;q=0.8",
    }
    if extra_headers:
        headers.update(extra_headers)
    return {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": timeout,
        "retries": 2,
        "http_headers": headers,
    }


def _extract_info(url: str, referer: str, timeout: int = 25, extra_headers: dict | None = None) -> dict:
    with yt_dlp.YoutubeDL(_ydl_options(referer, timeout, extra_headers)) as ydl:
        return ydl.extract_info(url, download=False)


def _is_youtube(info: dict) -> bool:
    ex = (info.get("extractor_key") or info.get("extractor") or "").lower()
    return "youtube" in ex


def _format_label(f: dict) -> str:
    if f.get("height"):
        label = f"{f['height']}p"
        if f.get("fps") and f["fps"] not in (25, 30):
            label += str(f["fps"])
        return label
    return f.get("format_note") or f.get("resolution") or f.get("format_id") or f.get("ext", "??")


def _formats_from_info(info: dict) -> list[dict]:
    rows = []
    for f in info.get("formats") or []:
        vcodec = f.get("vcodec")
        acodec = f.get("acodec")
        ext = (f.get("ext") or "").lower()
        if not f.get("url"):
            continue
        if ext in _IMAGE_EXTS:
            continue  # posters/thumbnails picked up by the generic extractor
        if vcodec == "none" and acodec == "none":
            continue  # neither audio nor video (images, html, ...)
        if vcodec == "none" and acodec != "none":
            continue  # audio-only stream (kept only if nothing else remains)
        rows.append(
            {
                "format_id": str(f.get("format_id")),
                "label": _format_label(f),
                "ext": f.get("ext"),
                "height": f.get("height") or 0,
                "tbr": f.get("tbr") or 0,
                "filesize": f.get("filesize") or f.get("filesize_approx"),
                "vcodec": vcodec,
                "acodec": acodec,
            }
        )

    # if the whole entry is a single direct file there may be no formats list
    if not rows and info.get("url"):
        rows.append(
            {
                "format_id": "best",
                "label": info.get("ext", "video"),
                "ext": info.get("ext"),
                "height": info.get("height") or 0,
                "tbr": info.get("tbr") or 0,
                "filesize": info.get("filesize") or info.get("filesize_approx"),
                "vcodec": info.get("vcodec"),
                "acodec": info.get("acodec"),
            }
        )

    if not rows:
        return rows

    # keep only the best bitrate per quality label, sorted best-first
    best: dict[str, dict] = {}
    for row in rows:
        cur = best.get(row["label"])
        if cur is None or row["tbr"] > cur["tbr"]:
            best[row["label"]] = row
    result = sorted(
        best.values(), key=lambda r: (r["height"], r["tbr"]), reverse=True
    )
    return result


def _first_entry(info: dict) -> dict:
    """Collapse playlists down to their first entry."""
    entries = info.get("entries")
    if entries:
        for entry in entries:
            if entry:
                merged = dict(entry)
                merged.setdefault("title", info.get("title"))
                return merged
    return info


# ---- main entry point ------------------------------------------------------
def extract_formats(url: str, allow_browser: bool = True) -> dict:
    """Resolve a page/media URL into {title, formats[], source, media_url,
    media_headers, cookies}.

    `media_url` is what the downloader should actually fetch (may differ from
    `url` when the real video was found in an iframe / via the browser).
    Raises ExtractError when nothing playable is found.
    """
    errors: list[str] = []

    # 1) yt-dlp directly on the page
    try:
        info = _first_entry(_extract_info(url, referer=url))
        if _is_youtube(info):
            errors.append("ข้าม YouTube embed (น่าจะเป็น trailer)")
        else:
            formats = _formats_from_info(info)
            if formats:
                return {
                    "title": info.get("title") or url,
                    "formats": formats,
                    "source": "yt-dlp",
                    "media_url": url,
                    "media_headers": None,
                    "cookies": None,
                }
            errors.append("yt-dlp ไม่พบรูปแบบวิดีโอในหน้านี้")
    except Exception as exc:  # noqa: BLE001 - report the reason to the user
        errors.append(f"yt-dlp: {exc}")

    # 2) static probe of the HTML (incl. packed JS + iframes)
    probed = _probe(url)
    for media_url, referer in probed["media"]:
        try:
            info = _first_entry(
                _extract_info(media_url, referer=referer, extra_headers={"Referer": referer})
            )
            if _is_youtube(info):
                continue
            formats = _formats_from_info(info)
            if formats:
                return {
                    "title": _choose_title(info.get("title"), probed.get("page_title"), url),
                    "formats": formats,
                    "source": f"probe: {media_url}",
                    "media_url": media_url,
                    "media_headers": {"Referer": referer, "User-Agent": USER_AGENT},
                    "cookies": None,
                }
        except Exception:  # noqa: BLE001 - try the next candidate
            continue

    # 3) headless browser: open the real player page directly (skipping ad
    #    gates found in the HTML), click play, capture the media request
    if allow_browser:
        targets: list[tuple[str, str]] = []
        for t in probed["players"] + probed["iframes"]:
            if t not in targets:
                targets.append(t)
        targets.append((url, url))

        sniffed_any = False
        for target, ref in targets[:3]:
            sniff = None
            try:
                sniff = sniffer.sniff_media(
                    target, user_agent=USER_AGENT, referer=ref, timeout=30
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(f"browser: {exc}")
            if not sniff:
                continue
            sniffed_any = True
            try:
                info = _first_entry(
                    _extract_info(sniff["url"], referer=ref, extra_headers=sniff["headers"])
                )
                formats = _formats_from_info(info)
                if formats:
                    return {
                        "title": _choose_title(
                            info.get("title") or sniff.get("title"),
                            probed.get("page_title"),
                            url,
                        ),
                        "formats": formats,
                        "source": f"browser: {sniff['url']}",
                        "media_url": sniff["url"],
                        "media_headers": sniff["headers"],
                        "cookies": sniff.get("cookies"),
                    }
            except Exception as exc:  # noqa: BLE001
                errors.append(f"browser extract: {exc}")
        if not sniffed_any and not any(e.startswith("browser:") for e in errors):
            errors.append("browser: player ไม่ได้โหลดวิดีโอ (อาจต้องกดเลือกเซิร์ฟเวอร์/กดเล่นเอง)")

    raise ExtractError(
        "ไม่พบไฟล์วิดีโอในลิงก์นี้ (" + (" | ".join(errors) if errors else "ไม่ทราบสาเหตุ")
        + ")\n\n💡 " + EXTRACT_HINT
    )
