"""Headless-browser fallback: open the page for real, click play, and capture
the media requests the player makes.

Sites hide the video URL behind packed JS, nested iframes and on-demand API
calls — but the player always ends up fetching .m3u8/.mp4 over the network.
Capturing those requests also gives us the exact referer/origin/cookies the
player used, which we then replay for the download.
"""

from __future__ import annotations

import re
import time
import urllib.parse

import requests

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Macintosh OS 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

MEDIA_RE = re.compile(r"\.(?:m3u8|mp4|mpd)(?:[?#]|$)", re.I)
# trailer/ads domains that should never be treated as the movie
SKIP_RE = re.compile(
    r"(?:youtube|youtu\.be|youtube-nocookie|googles\.video|doubleclick|"
    r"googlesyndication|googletagmanager|cdnbm168)",
    re.I,
)

# selectors to click, in order; tried inside every frame of the page.
# ad-skip / popup-close buttons come first so the real player gets revealed.
CLICK_SELECTORS = [
    ".jw-skip",
    "#skip_button",
    "[id*='skip' i]",
    "[class*='skip' i]",
    "#banner_popup button",
    "[id*='banner' i] button",
    "[id*='popup' i] button",
    ".jw-icon-playback",
    ".jwplayer",
    ".vjs-big-play-button",
    ".button_style button",
    ".videocontent button",
    "button[class*='play' i]",
    "[class*='play-button' i]",
    "video",
]


def _host(url: str) -> str:
    try:
        return urllib.parse.urlparse(url).hostname or ""
    except ValueError:
        return ""


def _is_master_playlist(url: str, referer: str | None, user_agent: str) -> bool:
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": user_agent, **({"Referer": referer} if referer else {})},
            timeout=10,
        )
        text = resp.text[:4000]
        return "#EXTM3U" in text and "#EXT-X-STREAM-INF" in text
    except requests.RequestException:
        return False


def _pick(captured: list[dict], user_agent: str) -> dict | None:
    """Prefer master m3u8 > any m3u8 > mp4 (ads are usually plain mp4)."""
    m3u8s = [m for m in captured if ".m3u8" in m["url"].lower()]
    mp4s = [m for m in captured if re.search(r"\.mp4(?:[?#]|$)", m["url"], re.I)]
    for m in m3u8s:
        if _is_master_playlist(m["url"], m.get("referer"), user_agent):
            return m
    return (m3u8s + mp4s or [None])[0]


def _relevant_cookies(cookies: list[dict], *hosts: str) -> list[dict]:
    hosts = [h for h in hosts if h]
    keep = []
    for c in cookies:
        d = (c.get("domain") or "").lstrip(".")
        if any(h == d or h.endswith("." + d) or d in h for h in hosts):
            keep.append(c)
    return keep


def _try_clicks(page, tried: set) -> None:
    """Click plausible skip/close/play buttons across the top frame and iframes."""
    frames = [page] + list(page.frames)
    for frame in frames:
        key_prefix = getattr(frame, "url", "") or "page"
        for sel in CLICK_SELECTORS:
            key = (key_prefix, sel)
            if key in tried:
                continue
            try:
                loc = frame.locator(sel).first
                if loc.count() == 0:
                    continue  # not there yet — retry next cycle
                tried.add(key)
                loc.click(timeout=800)
            except Exception:  # noqa: BLE001 - absent / not clickable yet
                continue


def _master(captured: list[dict], user_agent: str) -> dict | None:
    for m in captured:
        if ".m3u8" in m["url"].lower() and _is_master_playlist(m["url"], m.get("referer"), user_agent):
            return m
    return None


def sniff_media(
    url: str,
    timeout: float = 30.0,
    user_agent: str = USER_AGENT,
    referer: str | None = None,
) -> dict | None:
    """Open `url` in a headless browser and return the main media request.

    Returns {"url", "headers", "cookies", "title"} or None when the player
    never loads a video. Waits for a *master* m3u8 before bailing early so a
    pre-roll ad mp4 is never mistaken for the movie; at deadline the best
    captured request is used as fallback.
    """
    from playwright.sync_api import sync_playwright

    captured: list[dict] = []
    title = None

    def on_request(req) -> None:
        u = req.url
        if SKIP_RE.search(u) or not MEDIA_RE.search(u):
            return
        if any(m["url"] == u for m in captured):
            return
        h = req.headers
        captured.append(
            {
                "url": u,
                "referer": h.get("referer"),
                "origin": h.get("origin"),
                "user_agent": h.get("user-agent") or user_agent,
            }
        )

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="msedge", headless=True)
        except Exception:  # noqa: BLE001 - fall back to bundled chromium
            browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=user_agent, ignore_https_errors=True)
        page = context.new_page()
        # kill ad popups, but never the main page (the "page" event also fires
        # for pages created via new_page())
        context.on("page", lambda popup: popup.close() if popup is not page else None)
        page.on("request", on_request)

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=20000, referer=referer)
        except Exception:  # noqa: BLE001 - slow ad scripts must not stop the sniff
            pass
        try:
            title = page.title()
        except Exception:  # noqa: BLE001
            pass

        tried: set = set()
        deadline = time.time() + timeout
        while time.time() < deadline:
            _try_clicks(page, tried)
            best = _master(captured, user_agent)
            if best:
                break
            try:
                page.wait_for_timeout(2000)
            except Exception:  # noqa: BLE001 - page navigated away
                break

        cookies = context.cookies()
        browser.close()

    best = _master(captured, user_agent) or _pick(captured, user_agent)
    if not best:
        return None

    headers = {"User-Agent": best.get("user_agent") or user_agent}
    if best.get("referer"):
        headers["Referer"] = best["referer"]
    if best.get("origin"):
        headers["Origin"] = best["origin"]

    return {
        "url": best["url"],
        "headers": headers,
        "cookies": _relevant_cookies(cookies, _host(best["url"]), _host(best.get("referer") or "")),
        "title": title,
    }
