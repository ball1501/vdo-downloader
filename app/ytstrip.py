"""Strip disguised PNG headers off HLS segments.

Some CDNs (e.g. the fastfastcdn family) wrap every MPEG-TS / fMP4 segment
in a small fake PNG header (~545 bytes) and name the files .webp, so naive
downloaders produce "PNG" files that ffmpeg cannot merge. The site's own
player strips the prefix before demuxing.

We do the same transparently: a patched yt-dlp request handler peeks at each
response; if it is a fake-PNG-wrapped media segment, the PNG block is cut off
and Content-Length adjusted by the same amount (urllib3 enforces the original
Content-Length at the protocol level, so the remaining bytes must still be
consumed in full). Anything that is not a PNG followed by real media bytes
passes through untouched.
"""

from __future__ import annotations

import yt_dlp
from yt_dlp.networking import Response

PNG_SIG = b"\x89PNG\r\n\x1a\n"
_TS_SYNC = 0x47  # MPEG-TS packet sync byte
_PROBE = 512  # bytes after IEND needed to decide strip vs pass-through
_MAX_SCAN = 4 * 1024 * 1024  # give up if the PNG header is bigger than this


def _is_media(head: bytes) -> bool:
    """Heuristic: do these bytes look like a real video stream?"""
    if not head:
        return False
    if head[0] == _TS_SYNC and (len(head) < 188 or head[188] == _TS_SYNC):
        return True  # MPEG-TS
    if head[:3] == b"ID3":
        return True  # MP3/AAC with ID3
    if b"ftyp" in head[:64]:
        return True  # fMP4
    return False


def _png_payload_end(buf: bytes) -> int | None:
    """Walk PNG chunks in `buf`; return offset just past IEND, or None if the
    header is not a complete PNG block (yet)."""
    pos = 8
    n = len(buf)
    while pos + 8 <= n:
        length = int.from_bytes(buf[pos:pos + 4], "big")
        ctype = buf[pos + 4:pos + 8]
        end = pos + 8 + length + 4  # header + data + crc
        if end > n:
            return None  # need more bytes
        if ctype == b"IEND":
            return end
        pos = end
    return None


class _Prefeed:
    """File-like that yields held-back bytes first, then the real stream."""

    def __init__(self, resp, pre: bytes):
        self._resp = resp
        self._pre = pre

    def read(self, n=-1):
        if self._pre:
            if n is None or n < 0:
                data, self._pre = self._pre + self._resp.read(n), b""
            else:
                data, self._pre = self._pre[:n], self._pre[n:]
            return data
        return self._resp.read(n)

    def readable(self) -> bool:
        return True

    @property
    def closed(self) -> bool:
        return bool(getattr(self._resp, "closed", False))

    def close(self) -> None:
        self._resp.close()


def _copy_headers(resp) -> dict | None:
    try:
        return {k: v for k, v in resp.headers.items()}
    except Exception:  # noqa: BLE001
        return None


def _wrap(resp):
    """Peek the first bytes of `resp`; rebuild it as a Response, stripping a
    fake PNG prefix (and fixing Content-Length) when one is present."""
    try:
        head = b""
        while len(head) < 8:
            chunk = resp.read(8 - len(head))
            if not chunk:
                break
            head += chunk

        stripped_at = None
        if head[:8] == PNG_SIG:
            buf = head
            while True:
                end = _png_payload_end(buf)
                if end is not None:
                    if _is_media(buf[end:end + _PROBE]) or (
                        len(buf) - end >= _PROBE and b"ftyp" in buf[end:end + 256]
                    ):
                        stripped_at = end
                    break
                if len(buf) > _MAX_SCAN:
                    break
                chunk = resp.read(8192)
                if not chunk:
                    break
                buf += chunk

        pre = buf[stripped_at:] if stripped_at is not None else head
        headers = _copy_headers(resp)
        if headers is not None and stripped_at is not None:
            cl = headers.get("Content-Length")
            if cl and cl.isdigit():
                headers["Content-Length"] = str(int(cl) - stripped_at)

        return Response(
            fp=_Prefeed(resp, pre),
            url=getattr(resp, "url", ""),
            headers=headers if headers is not None else getattr(resp, "headers", {}),
            status=getattr(resp, "status", 200),
            extensions=getattr(resp, "extensions", None),
        )
    except Exception:  # noqa: BLE001 - never break the download over this
        return resp


def install() -> None:
    """Patch yt-dlp's request handlers so every response goes through the
    stripper. Idempotent. Class names differ across yt-dlp versions
    (RequestsRH / RequestsRequestHandler), so try each."""
    if getattr(install, "_done", False):
        return
    install._done = True

    patched_any = False
    candidates = [
        ("yt_dlp.networking._requests", ("RequestsRH", "RequestsRequestHandler")),
        ("yt_dlp.networking._urllib", ("UrllibRH", "UrllibRequestHandler")),
    ]
    for module_name, class_names in candidates:
        try:
            module = __import__(module_name, fromlist=class_names)
            cls = next(
                (getattr(module, n) for n in class_names if hasattr(module, n)), None
            )
            if cls is None:
                continue
            original = cls._send

            def _send(self, request, _original=original):
                return _wrap(_original(self, request))

            cls._send = _send
            patched_any = True
        except Exception:  # noqa: BLE001 - yt-dlp internals may move around
            continue

    if not patched_any:
        import sys

        print(
            "ytstrip: ไม่พบ request handler ของ yt-dlp — ป้องกัน segment หุ้ม PNG จะไม่ทำงาน",
            file=sys.stderr,
        )
