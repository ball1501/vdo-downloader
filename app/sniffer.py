"""Headless-browser fallback: open the page for real, click play, and capture
the media requests the player makes.

Sites hide the video URL behind packed JS, nested iframes and on-demand API
calls — but the player always ends up fetching .m3u8/.mp4 over the network.
Capturing those requests also gives us the exact referer/origin/cookies the
player used, which we then replay for the download.
"""

from __future__ import annotations

import re
import threading
import time
import urllib.parse

import requests

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Macintosh OS 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

MEDIA_RE = re.compile(r"\.(?:m3u8|mp4|mpd)(?:[?#]|$)", re.I)
# hls.js/dash.js fetch manifests via XHR with tokenized URLs that often have
# no file extension — catch them by response content-type instead
_MIME_RE = re.compile(r"mpegurl|dash\+xml", re.I)
# trailer/ads domains that should never be treated as the movie
SKIP_RE = re.compile(
    r"(?:youtube|youtu\.be|youtube-nocookie|googles\.video|doubleclick|"
    r"googlesyndication|googletagmanager|cdnbm168|cdend\.com|morphify|"
    r"storage\.googleapis\.com/mediastorage)",
    re.I,
)

# selectors to click, in order; tried inside every frame of the page.
# ad-skip / popup-close buttons come first so the real player gets revealed,
# then generic play buttons.
CLICK_SELECTORS = [
    ".jw-skip",
    "#skip_button",
    "[id*='skip' i]",
    "[class*='skip' i]",
    "#banner_popup button",
    "[id*='banner' i] button",
    "[id*='popup' i] button",
    # Abyss-family ad gate: the overlay consumes one popup per click and
    # needs exactly two before it removes itself; its onclick is assigned
    # from JS so [onclick] can't see it (two aliases = two clicks)
    "#overlay",
    "div#overlay",
    ".jw-icon-playback",
    ".vjs-big-play-button",
    ".button_style button",
    ".videocontent button",
    "button[class*='play' i]",
    "[class*='play-button' i]",
    "[onclick]",
]

# player/server switchers (Dooplay-style themes etc.): clicked one at a time
# with a generous timeout, with dwell time between switches — the first
# server is often dead (error page) while later ones work. One comma-union
# selector so each element is visited exactly once.
SERVER_SELECTOR = "[data-nume], .dooplay_player_option, [data-server], [data-linkserver]"
_DEAD_FRAME_RE = re.compile(r"error|no_video|not_found|deleted|removed", re.I)


def server_matches(page) -> list:
    """All player/server-switch elements, in document order."""
    out = []
    for frame in [page] + list(page.frames):
        try:
            matches = frame.locator(SERVER_SELECTOR)
            for i in range(min(matches.count(), 6)):
                out.append((frame, matches, i))
        except Exception:  # noqa: BLE001 - selector invalid in this frame
            continue
    return out


def _has_dead_frame(page) -> bool:
    """A player iframe that openly says it has no video — switch servers fast."""
    for frame in page.frames:
        u = frame.url or ""
        if u and u != "about:blank" and _DEAD_FRAME_RE.search(u):
            return True
    return False

# lazy iframes whose real URL sits in data-src never load on their own
_LAZY_IFRAME_JS = """() => {
    for (const f of document.querySelectorAll(
        'iframe[data-src], iframe[data-lazy-src]')) {
        if (!f.getAttribute('src')) {
            const u = f.getAttribute('data-src') || f.getAttribute('data-lazy-src');
            if (u) f.setAttribute('src', u);
        }
    }
}"""


def _fixup_lazy_iframes(page) -> None:
    for frame in [page] + list(page.frames):
        try:
            frame.evaluate(_LAZY_IFRAME_JS)
        except Exception:  # noqa: BLE001 - frame gone / not ready
            continue


def _rearm_if_paused(page, tried: dict) -> None:
    """Players often pause again after a pre-roll finishes (or autoplay is
    blocked mid-flow) — and some pre-rolls simply stall on their last frame.
    Force stalled videos to their end (firing the ad-complete handlers) and
    re-arm play buttons while nothing has been captured yet."""
    for frame in [page] + list(page.frames):
        try:
            paused = frame.evaluate(
                "() => { const v = document.querySelector('video');"
                " if (!v || v.readyState < 1) return false;"
                " if (v.paused && v.duration && v.currentTime < v.duration - 0.2)"
                " { try { v.currentTime = v.duration; } catch (e) {} }"
                " return v.paused; }"
            )
        except Exception:  # noqa: BLE001
            continue
        if not paused:
            continue
        prefix = frame.url or "page"
        for key in [
            k for k in tried
            if k[0] == prefix and k[1] in _PLAY_AGAIN_SELECTORS and tried[k] == _DONE
        ]:
            del tried[key]


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
    playlists = [m for m in captured if m.get("mime") or ".m3u8" in m["url"].lower()]
    mp4s = [m for m in captured if re.search(r"\.mp4(?:[?#]|$)", m["url"], re.I)]
    for m in playlists:
        if _is_master_playlist(m["url"], m.get("referer"), user_agent):
            return m
    return (playlists + mp4s or [None])[0]


def _relevant_cookies(cookies: list[dict], *hosts: str) -> list[dict]:
    hosts = [h for h in hosts if h]
    keep = []
    for c in cookies:
        d = (c.get("domain") or "").lstrip(".")
        if any(h == d or h.endswith("." + d) or d in h for h in hosts):
            keep.append(c)
    return keep


_PLAY_AGAIN_SELECTORS = (".jw-icon-playback", ".vjs-big-play-button", "button[class*='play' i]", "[class*='play-button' i]")


def _try_clicks(page, tried: dict, max_retries: int = 6) -> None:
    """Click plausible skip/close/play buttons across every frame (each match
    once per frame URL).

    `tried` maps (frame, selector, index) -> attempt count; failed clicks are
    retried up to `max_retries` times because ad-skip buttons often only
    become clickable seconds after the player starts.

    Clicking a player-setup button ([onclick], e.g. sampler/server pickers)
    re-creates the video element, so the play buttons of that frame are
    re-armed for the next cycle."""
    frames = [page] + list(page.frames)
    rearm = set()
    for frame in frames:
        key_prefix = getattr(frame, "url", "") or "page"
        for sel in CLICK_SELECTORS:
            if sel == "[onclick]":
                if _click_matches(frame, key_prefix, sel, tried, max_retries):
                    rearm.add(key_prefix)
            else:
                _click_matches(frame, key_prefix, sel, tried, max_retries)

    for prefix in rearm:
        for key in [k for k in tried if k[0] == prefix and k[1] in _PLAY_AGAIN_SELECTORS]:
            del tried[key]


_PLAY_GUARD_SELECTORS = (".jw-icon-playback", ".vjs-big-play-button")


_DONE = "done"


def _click_matches(frame, key_prefix: str, sel: str, tried: dict, max_retries: int) -> None:
    try:
        matches = frame.locator(sel)
        count = matches.count()
    except Exception:  # noqa: BLE001 - invalid selector in this frame
        return
    for i in range(min(count, 4)):
        key = (key_prefix, sel, i)
        state = tried.get(key)
        if state == _DONE or (isinstance(state, int) and state >= max_retries):
            continue
        # pressing play only makes sense once the media has metadata — an
        # earlier click on a half-initialized player is silently swallowed
        if sel in _PLAY_GUARD_SELECTORS:
            try:
                ready = frame.evaluate(
                    "() => { const v = document.querySelector('video');"
                    " return v ? v.readyState >= 1 : true; }"
                )
            except Exception:  # noqa: BLE001 - frame busy; try anyway
                ready = True
            if not ready:
                continue  # don't burn the attempt, retry next cycle
        # a skip button still counting down ("ข้ามโฆษณาใน 3 วิ") is a no-op —
        # wait until its text has no digits left
        if sel == ".jw-skip":
            try:
                txt = frame.evaluate(
                    "() => { const s = document.querySelector('.jw-skip');"
                    " return s ? s.innerText : ''; }"
                )
                if txt and re.search(r"\d", txt):
                    continue
            except Exception:  # noqa: BLE001
                pass
        try:
            matches.nth(i).click(timeout=800)
            tried[key] = _DONE  # succeeded — never click again (it would toggle)
        except Exception:  # noqa: BLE001 - blocked by an overlay: force a DOM click
            try:
                matches.nth(i).evaluate("el => el.click()")
                tried[key] = _DONE
            except Exception:  # noqa: BLE001 - element really gone
                tried[key] = (state if isinstance(state, int) else 0) + 1


def _master(captured: list[dict], user_agent: str) -> dict | None:
    for m in captured:
        if (m.get("mime") or ".m3u8" in m["url"].lower()) and _is_master_playlist(
            m["url"], m.get("referer"), user_agent
        ):
            return m
    return None


_TINY_MP4: bytes | None = None
_TINY_GIF = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04"
    b"\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D"
    b"\x01\x00;"
)


def _tiny_mp4() -> bytes:
    """A 0.2s black mp4 so forced-ad players see their pre-roll 'complete'."""
    global _TINY_MP4
    if _TINY_MP4 is None:
        import os
        import shutil
        import subprocess
        import tempfile

        ffmpeg = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
        try:
            fd, path = tempfile.mkstemp(suffix=".mp4")
            os.close(fd)
            subprocess.run(
                [ffmpeg, "-y", "-loglevel", "error", "-f", "lavfi",
                 "-i", "color=black:s=128x128:d=0.2", "-c:v", "libx264",
                 "-pix_fmt", "yuv420p", "-movflags", "+faststart", path],
                check=True, timeout=30,
            )
            with open(path, "rb") as fh:
                _TINY_MP4 = fh.read()
            os.unlink(path)
        except Exception:  # noqa: BLE001 - no ffmpeg: empty body fallback
            _TINY_MP4 = b""
    return _TINY_MP4


def _fulfill_ad(route) -> None:
    """Answer blocked ad requests locally: media gets a tiny instant video,
    images a 1px gif, everything else an empty 200 — so 'ads are working'
    checks pass and the real player is revealed.

    Media responses honor Range requests (206 + Content-Range) because
    video elements refuse to play a 200 answer to a range request."""
    req = route.request
    url = req.url
    if url.endswith((".jpg", ".jpeg", ".png", ".gif")):
        route.fulfill(status=200, content_type="image/gif", body=_TINY_GIF)
        return

    if ".mp4" in url:
        body, ctype = _tiny_mp4(), "video/mp4"
    else:
        route.fulfill(status=200, body="", content_type="text/plain")
        return

    headers = {"Accept-Ranges": "bytes"}
    status = 200
    rng = (req.headers or {}).get("range")
    if body and rng:
        m = re.match(r"bytes=(\d*)-(\d*)", rng)
        if m:
            start = int(m.group(1) or 0)
            end = int(m.group(2)) if m.group(2) else len(body) - 1
            end = min(end, len(body) - 1)
            if 0 <= start <= end:
                headers["Content-Range"] = f"bytes {start}-{end}/{len(body)}"
                body = body[start:end + 1]
                status = 206
    route.fulfill(status=status, content_type=ctype, body=body, headers=headers)


def sniff_media(
    url: str,
    timeout: float = 85.0,
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

    def _remember(req, mime: bool = False) -> None:
        u = req.url
        if any(m["url"] == u for m in captured):
            return
        h = req.headers
        captured.append(
            {
                "url": u,
                "referer": h.get("referer"),
                "origin": h.get("origin"),
                "user_agent": h.get("user-agent") or user_agent,
                "mime": mime,
            }
        )

    def on_request(req) -> None:
        u = req.url
        if SKIP_RE.search(u) or not MEDIA_RE.search(u):
            return
        _remember(req)

    def on_response(resp) -> None:
        # manifests fetched by hls.js/dash.js: the URL shape is unreliable,
        # the content-type is the trustworthy signal
        try:
            ct = (resp.headers or {}).get("content-type", "")
        except Exception:  # noqa: BLE001
            return
        u = resp.url
        if SKIP_RE.search(u) or not _MIME_RE.search(ct):
            return
        if any(m["url"] == u for m in captured):
            return
        _remember(resp.request, mime=True)

    with sync_playwright() as p:
        launch_args = ["--autoplay-policy=no-user-gesture-required", "--mute-audio"]
        try:
            browser = p.chromium.launch(channel="msedge", headless=True, args=launch_args)
        except Exception:  # noqa: BLE001 - fall back to bundled chromium
            browser = p.chromium.launch(headless=True, args=launch_args)
        context = browser.new_context(user_agent=user_agent, ignore_https_errors=True)
        page = context.new_page()

        # Some players refuse to start unless their ads "worked": they count
        # ad popups and probe ad scripts. Play along just enough — let popups
        # live a few seconds (browser close reaps them) and satisfy blocked
        # ad-script probes with an empty 200.
        def _delayed_close(popup) -> None:
            def _close():
                try:
                    popup.close()
                except Exception:  # noqa: BLE001
                    pass
            timer = threading.Timer(5.0, _close)
            timer.daemon = True
            timer.start()

        context.on("page", _delayed_close)
        # ad scripts / pixels / ad videos: served locally so players that gate
        # playback on "ads displayed" proceed (their real domains are
        # DNS-blocked here, which they would otherwise count as AdBlock)
        for pattern in (
            "**://pagead2.googlesyndication.com/**",
            "**://*.googlesyndication.com/**",
            "**://*.doubleclick.net/**",
            "**://www.googletagmanager.com/**",
            "**://*.histats.com/**",
            "**://cdend.com/**",
            "**://*.cdend.com/**",
            "**://googles.video/**",
            "**://storage.googleapis.com/mediastorage/**",
            "**://pixel.morphify.net/**",
            "**://*.morphify.net/**",
        ):
            try:
                context.route(pattern, _fulfill_ad)
            except Exception:  # noqa: BLE001
                pass

        page.on("request", on_request)
        page.on("response", on_response)

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=20000, referer=referer)
        except Exception:  # noqa: BLE001 - slow ad scripts must not stop the sniff
            pass
        try:
            title = page.title()
        except Exception:  # noqa: BLE001
            pass

        tried: dict = {}
        deadline = time.time() + timeout
        servers = None  # enumerated lazily; server buttons may load late
        server_i = 0
        last_switch = time.time()
        baseline = 0
        while time.time() < deadline:
            _fixup_lazy_iframes(page)
            _try_clicks(page, tried)
            _rearm_if_paused(page, tried)

            # a manifest (master or variant) is all we need — stop clicking
            if any(m.get("mime") or ".m3u8" in m["url"].lower() for m in captured):
                break

            # give the current server time to prove itself, then try the next
            if servers is None:
                servers = server_matches(page)
            needed_dwell = 6 if _has_dead_frame(page) else 35
            if (
                servers
                and server_i < len(servers)
                and len(captured) == baseline
                and time.time() - last_switch >= needed_dwell
            ):
                frame, matches, i = servers[server_i]
                server_i += 1
                last_switch = time.time()
                baseline = len(captured)
                try:
                    matches.nth(i).click(timeout=2500)
                except Exception:  # noqa: BLE001 - blocked by an overlay etc.
                    pass

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
