"""Abyss/tonytonychopper-family player extractor (static, Node-assisted).

Ported from the user's abyss_player_downloader. Players in this family hide
the HLS master URL inside a Dean-Edwards-packed script whose payload calls
Cookie.Run('<base64>') with further eval layers — unpacked here with a Node
subprocess for fidelity (regex unpacking can't reconstruct nested evals).

Chain for fairyanime-style pages:
    watch page → /base/<id> script (webmainapp + playback id)
    → <mainapp>playback/v/<id>/ (the ad-free variant; referer = /f/ variant)
    → inner iframe = real player page (packed JS) → Node unpack → master m3u8

Pages that themselves contain the packed Cookie.Run player are handled too.
The returned master URL is then handed to yt-dlp (with the player's
Referer/Origin) for variant listing and the actual download.
"""

from __future__ import annotations

import re
import subprocess
import urllib.parse

import requests
import urllib3

from .sniffer import USER_AGENT

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# the whole chain MUST share one cookie jar: the m3u8 server answers "null"
# unless the cookies from the earlier steps are replayed
_session = requests.Session()
_session.verify = False
_session.headers.update({"User-Agent": USER_AGENT, "Accept": "*/*"})


def _fetch(url: str, referer: str | None = None, timeout: int = 15) -> str:
    headers = {"Accept-Language": "en-US,en;q=0.9,th;q=0.8"}
    if referer:
        headers["Referer"] = referer
    resp = _session.get(url, headers=headers, timeout=timeout)
    resp.encoding = "utf-8"
    return resp.text


def _session_cookies() -> list[dict]:
    out = []
    for c in _session.cookies:
        out.append(
            {
                "name": c.name,
                "value": c.value,
                "domain": c.domain,
                "path": c.path,
                "secure": bool(c.secure),
                "expires": int(c.expires) if c.expires else None,
            }
        )
    return out


_NODE_UNPACK = r"""
const fs = require('fs');
let html = fs.readFileSync(0, 'utf-8');

const match = html.match(/eval\(function\(p,a,c,k,e,d\)[\s\S]*?\.split\('\|'\),0,\{\}\)\)/);
if (!match) {
    process.exit(1);
}

let evalStr = match[0].replace(/^eval/, '');
const unpacked1 = eval(evalStr);

let rawB64 = '';
const Cookie = {
    Run: function(val) {
        rawB64 = val;
    }
};
eval(unpacked1);

if (!rawB64) {
    process.exit(2);
}

const decoded = Buffer.from(rawB64, 'base64').toString('utf-8');
let evalStr2 = decoded.trim().replace(/^eval/, '');
const unpacked2 = eval(evalStr2);

let result = '';
let evalStr3 = unpacked2.replace(/eval\s*\(/, 'result = (');
eval(evalStr3);

const hlsMatch = result.match(/hls\s*=\s*["']([^"']+)["']/);
if (hlsMatch) {
    console.log(hlsMatch[1]);
} else {
    process.exit(3);
}
"""


def _node_unpack_hls(player_html: str) -> str | None:
    """Unpack the multi-layer player JS with Node; fall back to plain regex."""
    try:
        proc = subprocess.run(
            ["node", "-e", _NODE_UNPACK],
            input=player_html,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    except Exception:  # noqa: BLE001 - no node / timeout: use regex fallbacks
        pass

    m = re.search(r'hls\s*=\s*["\']([^"\']+\.m3u8[^"\']*)["\']', player_html)
    if m:
        return m.group(1)
    m = re.search(r'file:\s*["\']([^"\']+\.m3u8[^"\']*)["\']', player_html)
    if m:
        return m.group(1)
    return None


def _page_title(html: str) -> str | None:
    m = re.search(r"<title>([^<]+)</title>", html)
    if not m:
        return None
    return m.group(1).split("-")[0].strip() or None


def extract(url: str, html: str | None = None) -> dict | None:
    """Resolve an Abyss-family page into {media_url, headers, player_url,
    title, cookies}. Returns None when the page isn't one of these players."""
    _session.cookies.clear()  # fresh chain per extraction
    try:
        html = html or _fetch(url)
    except requests.RequestException:
        return None

    title = _page_title(html)
    player_url = None
    player_html = None

    # fairyanime-style: watch page carries a /base/<id> player script
    base_match = re.search(r'src="([^"]*/base/[^"]+)"', html)
    if base_match:
        base_script_url = base_match.group(1)
        if not base_script_url.startswith("http"):
            base_script_url = urllib.parse.urljoin(url, base_script_url)
        base_js = _fetch(base_script_url, referer=url)

        mainapp_m = re.search(r"var\s+webmainapp\s*=\s*'([^']+)'", base_js)
        mainapp = mainapp_m.group(1) if mainapp_m else ""
        playback_m = re.search(r"playback/[a-z]/([a-zA-Z0-9_-]+)/", base_js)
        if not mainapp or not playback_m:
            return None
        vid = playback_m.group(1)

        gate_url = f"{mainapp}playback/f/{vid}/"
        player_url = f"{mainapp}playback/v/{vid}/"
        try:
            playback_v_html = _fetch(player_url, referer=gate_url)
        except requests.RequestException:
            return None
        iframe_m = re.search(r'<iframe[^>]+src="([^"]+)"', playback_v_html)
        if not iframe_m:
            return None
        inner = iframe_m.group(1)
        if inner.startswith("//"):
            inner = "https:" + inner
        try:
            player_html = _fetch(inner, referer=player_url)
        except requests.RequestException:
            return None
        player_url = inner
    elif "Cookie.Run(" in html or re.search(r"eval\(function\(p,a,c,k,e,d\)", html):
        # the page itself is the packed player
        player_url = url
        player_html = html
    else:
        return None

    master = _node_unpack_hls(player_html) if player_html else None
    if not master:
        return None
    if master.startswith("//"):
        master = "https:" + master

    parsed = urllib.parse.urlparse(player_url)
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": player_url,
        "Origin": f"{parsed.scheme}://{parsed.netloc}",
        # the m3u8 endpoint answers "null" to HTML Accept headers
        "Accept": "*/*",
    }
    return {
        "media_url": master,
        "headers": headers,
        "player_url": player_url,
        "title": title,
        "cookies": _session_cookies(),
    }
