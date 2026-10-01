# 🎬 VDO Downloader

เครื่องมือดาวน์โหลดวิดีโอจากเว็บดูหนัง/อนิเมะที่ฝัง player ไว้ (HLS `.m3u8`, `.mp4` ฯลฯ) มาเป็นไฟล์ไว้ดูออฟไลน์ — รันเป็นเว็บในเครื่อง วางลิงก์ เลือกคุณภาพ กดดาวน์โหลด ดู progress ได้เรียลไทม์

> ⚠️ ใช้เพื่อส่วนตัว/ดูออฟไลน์เท่านั้น กรุณาสนับสนุนผู้สร้างผลงานตัวจริงด้วย

**ภาษา / Language:** [ไทย](#) · [English](#english-1)

## เริ่มใช้งาน

```bash
./run.sh
```

รันครั้งแรกจะสร้าง venv และติดตั้ง dependencies ให้เอง แล้วเปิดเบราว์เซอร์ที่ **http://localhost:8787** อัตโนมัติ (เซิร์ฟเวอร์ฟังที่ 127.0.0.1 เท่านั้น — เข้าถึงจากเครื่องนี้)

**ข้อกำหนด:** Python 3, ffmpeg (`brew install ffmpeg`) และเบราว์เซอร์ Microsoft Edge หรือ Chrome สำหรับโหมดสแกนเว็บยาก (macOS ที่ติด Edge อยู่แล้วใช้ได้ทันที — ถ้าไม่มีทั้งคู่จะติดตั้ง headless Chromium ให้ตอน `playwright install chromium`)

## วิธีใช้

1. วางลิงก์**หน้าเว็บที่ดูวิดีโออยู่** (หน้าหนัง/หน้าตอนอนิเมะ) หรือลิงก์ไฟล์ `.m3u8`/`.mp4` ตรง ๆ แล้วกด Enter
2. รอระบบตรวจสอบ — เว็บที่ซ่อนวิดีโอดี ๆ ต้องเปิดเบราว์เซอร์สแกน ใช้เวลา 30–90 วินาที
3. เลือกคุณภาพจากตาราง (เรียงจากสูงสุดก่อน) กด **เพิ่มเข้าคิวดาวน์โหลด**
4. ดูความคืบหน้าในการ์ดคิว (% / ความเร็ว / เวลาที่เหลือ) กด ✕ เพื่อยกเลิกได้
5. โฟลเดอร์ปลายทางเปลี่ยนได้ในช่อง "📁 โฟลเดอร์ปลายทาง" (ค่าเริ่มต้นคือ `downloads/`) — ไฟล์ HLS จะถูกรวมวิดีโอ+เสียงด้วย ffmpeg อัตโนมัติ

ดาวน์โหลดพร้อมกันได้ 2 งาน งานที่เหลือรอในคิว

## มันทำงานยังไง (3 ชั้น)

เว็บพวกนี้มีระบบกันโหลดหลากหลาย ตัวเครื่องมือจึงลองทีละชั้นจากเร็วไปหาแน่:

1. **yt-dlp โดยตรง** — ครอบคลุม player ฝังตัว/โฮสต์ไฟล์หลายร้อยเจ้า / ไฟล์ mp4 ลอย ๆ
   - ถ้าเจอ **YouTube embed** จะถือว่าเป็น trailer แล้วข้ามทันที (ไม่โหลด trailer มาให้)
2. **สแกน HTML แบบ static** — ดึงหน้าเว็บเอง (แนบ Referer/UA แบบเบราว์เซอร์จริง) แล้ว:
   - หา `.m3u8/.mp4/.mpd` ในหน้า รวมถึง URL ที่ถูก escape (`https:\/\/...`, `\u0026`)
   - แกะ JS แบบ packed (`eval(function(p,a,c,k,e,d))`)
   - ไล่ iframe ได้ลึก 2 ชั้น พร้อมแนบ Referer ของหน้าแม่ทุกชั้น
   - อ่าน player URL ที่แปะไว้ใน query string (`?link=...`) และตัวแปรแบบ `window.destinationURL` — ใช้ข้ามหน้าโฆษณา pre-roll ไปเปิด player จริงเลย
   - กรอง mp4 โฆษณาทิ้ง (อิงบริบท `adSettings`/`adverts`/`skipSeconds` + โดเมนโฆษณาที่รู้จัก)
3. **Abyss/tonytonychopper-family extractor** (`app/abyss.py`) — สำหรับเว็บอย่าง FairAnime: player ตระกูลนี้ซ่อน URL จริงไว้ใน JS แบบ packed หลายชั้น (`Cookie.Run('<base64>')`) — แกะด้วย Node.js แบบ static (เร็ว ไม่ต้องเปิดเบราว์เซอร์) พร้อม cookie session ตลอดสาย (server ของ m3u8 ตอบ `null` ถ้าไม่มี cookie จากขั้นก่อนหน้า) และต้องส่ง `Accept: */*` เท่านั้น (Accept แบบ HTML จะโดนตอบ `null`)
4. **สแกนด้วยเบราว์เซอร์จริง (headless Edge/Chrome ผ่าน Playwright)** — สำหรับเว็บที่ player เรียก API เอา m3u8 ตอนกดเล่น เช่น jwplayer ที่ config มีแต่ `vdoId`:
   - เปิดหน้า player (เลือกเปิด player URL ที่ขุดได้จากขั้น 2 ก่อน — ไม่ต้องเจอโฆษณา)
   - จัดการอัตโนมัติ: ย้าย `data-src` → `src` ของ lazy iframe, กดปุ่มข้ามโฆษณา/ปิด popup/กดเล่น (คลิกธรรมดาไม่ได้เมื่อไหร่สลับเป็น JS click), กดเลือกเซิร์ฟเวอร์ทีละตัว (ธีม Dooplay — server แรกมักตาย)
   - ดักฟังทั้ง request และ response — manifest ของ hls.js ที่ URL ไม่มีนามสกุลก็จับได้จาก content-type (`vnd.apple.mpegurl` / `dash+xml`) และ sniff body ของ XHR หา m3u8 ที่ซ่อนอยู่ใน JSON ของ custom player
   - ขวาง redirect ไปหน้า homepage ที่ player embed บางเจ้าทำ (เช่น zmdb.net → baidu)
   - ตอบจำลองโฆษณาที่ถูกบล็อก (สคริปต์/พิกเซล/วิดีโอพร้อม Range header) เพื่อผ่านประตูแบบ "โฆษณาต้องแสดงก่อนถึงเล่นได้"
   - ได้ URL จริง **พร้อม Referer/Origin/UA/Cookie ที่ player ใช้จริง** → ส่งต่อให้ตัวดาวน์โหลด replay ตอนโหลดไฟล์ (CDN พวกนี้เช็ค header อีกชั้น)

4. **แกะ segment ที่แปลงร่าง** (`app/ytstrip.py`) — CDN บางเจ้า (เช่น ตระกูล fastfastcdn) หุ้ม segment MPEG-TS ด้วยหัว PNG ปลอม ~545 ไบต์ และตั้งชื่อไฟล์เป็น `.webp` ทำให้ ffmpeg merge ไม่ได้ (`Postprocessing: Error opening output files`) ตัว patch ระดับ HTTP ของ yt-dlp จะตัดหัวปลอมออกและแก้ Content-Length ให้ตรงโดยอัตโนมัติ — response ปกติไม่ถูกแตะ

## ถ้าเว็บไหนยังติด

- ลองวางลิงก์ **iframe ของ player** โดยตรง (คลิกขวาที่ player → Inspect → หา `<iframe src="...">`)
- เปิด DevTools → แท็บ Network → กรอง `m3u8` → กดเล่น → คัดลอก URL มาวางตรง ๆ
- เว็บที่ต้อง login: เพิ่ม `cookiefile` ใน `app/extractor.py` (`_ydl_options`) ได้
- เว็บที่ใช้ DRM จริง (Widevine ฯลฯ) **ไม่รองรับ**
- โดเมนโฆษณาตัวใหม่ที่หลุดกรอง: เพิ่มใน `SKIP_RE` ที่ `app/sniffer.py` ได้เลย
- **ขีดจำกัดปัจจุบัน**: player ตระกูล abyssplayer (เช่น server "สำรอง 1" ของ animeruka) — ตัว sniffer ผ่านประตู overlay/โฆษณา และจับ URL ไฟล์ตอนจริงได้จาก jwplayer (แยกจากโฆษณาด้วยความยาววิดีโอ) แต่ host ของไฟล์ (GCS mediastorage) อาจตอบ 403 ให้ session ที่ไม่ใช่ player — เครื่องมือจะส่งคุณภาพ "ต้นฉบับ (original)" ให้ลองดาวน์โหลด ถ้า 403 ให้ใช้วิธีเปิดเว็บในเบราว์เซอร์จริงแล้วเอา URL มาวางแทน (หรือใช้เว็บที่แจก server แบบ mp4 ตรง)

## โครงสร้างโค้ด

```
app/
├── main.py        # FastAPI: API routes + serve หน้าเว็บ + SSE progress
├── extractor.py   # 3 ชั้น: yt-dlp → static probe → browser sniff
├── sniffer.py     # headless browser คลิกเล่น + ดักจับ media request + headers/cookies
├── downloader.py  # คิวงาน + worker threads + progress hooks + cancel + cookie replay
└── static/        # หน้าเว็บ (HTML/CSS/JS ธรรมดา ไม่ต้อง build)
```

API สำหรับใช้จากสคริปต์อื่น: `POST /api/extract` {url}, `POST /api/download` {url, format_id, media_url?, media_headers?, cookies?}, `GET /api/jobs`, `GET /api/events` (SSE)

---

# 🎬 VDO Downloader (English)

A local web tool for downloading videos from streaming sites that embed their own players (HLS `.m3u8`, `.mp4`, etc.) into offline files. Paste a link, pick a quality, hit download — with a real-time progress queue.

> ⚠️ For personal/offline use only. Please support the actual creators.

**Language / ภาษา:** [ไทย](#-vdo-downloader) · [English](#)

## Quick start

```bash
./run.sh
```

The first run creates a Python venv and installs dependencies, then opens **http://localhost:8787** automatically (the server listens on 127.0.0.1 only — your machine).

**Requirements:** Python 3, ffmpeg (`brew install ffmpeg`), and Microsoft Edge or Chrome for the headless-browser extraction mode (a bundled headless Chromium can be installed instead via `playwright install chromium`).

## Usage

1. Paste the URL of the **watch page** (movie/episode page) or a direct `.m3u8`/`.mp4` link and press Enter.
2. Wait for the scan — sites that hide their videos well need the headless browser and can take 30–90 seconds.
3. Pick a quality from the table (best first) and hit **Add to download queue**.
4. Watch the queue card for progress (% / speed / ETA); press ✕ to cancel.
5. The output folder is editable in the UI (default: `downloads/`). HLS streams are merged (video + audio) with ffmpeg automatically.

Up to 2 downloads run in parallel; the rest wait in the queue.

## How it works (4 layers)

These sites ship various anti-download mechanisms, so extraction tries each layer from fastest to heaviest:

1. **yt-dlp directly** — covers hundreds of embedded players / file hosts / bare mp4 links. YouTube embeds found this way are treated as trailers and skipped.
2. **Static HTML probe** — fetches the page itself (with browser-like Referer/User-Agent) and:
   - scans for `.m3u8/.mp4/.mpd` URLs, including escaped ones (`https:\/\/...`, `\u0026`),
   - unpacks Dean-Edwards-packed JavaScript (`eval(function(p,a,c,k,e,d))`),
   - follows iframes up to 2 levels deep with per-level Referer chaining,
   - digs player URLs out of query strings (`?link=...`) and `window.destinationURL`-style vars — jumping straight past pre-roll ad gates,
   - filters ad MP4s (via `adSettings`/`adverts`/`skipSeconds` context + known ad domains).
3. **Abyss/tonytonychopper-family extractor** (`app/abyss.py`) — for sites like FairyAnime: this player family hides the real URL inside multi-layer packed JS (`Cookie.Run('<base64>')`) — unpacked statically with Node.js (no browser needed). The whole chain shares one cookie session (the m3u8 endpoint answers `null` without the earlier steps' cookies) and requires `Accept: */*` (an HTML Accept header gets a `null` answer).
4. **Headless-browser sniffing (Playwright, Edge/Chrome)** — for players that fetch the stream via JS only when you press play (e.g. jwplayer configs with just a `vdoId`):
   - opens the player page (preferring player URLs dug up in step 2 — skipping ad gates),
   - handles lazily: moves `data-src` → `src` on lazy iframes, clicks skip-ad / close-popup / server-picker / play buttons (falling back to JS clicks when overlays intercept), cycles Dooplay-style servers one at a time (the first is usually dead),
   - captures requests AND responses — HLS manifests fetched by hls.js with extension-less tokenized URLs are caught by content-type (`vnd.apple.mpegurl` / `dash+xml`), and XHR bodies are sniffed for `#EXTM3U` / JSON-embedded `.m3u8` URLs,
   - answers blocked ad requests locally (scripts, pixels, and videos with proper 206/Content-Range handling) to pass "ads must display" gates,
   - for players that host ads and the real episode on the same CDN, a video element with a duration over 300s is the real episode — its source is captured directly,
   - returns the real URL **with the exact Referer/Origin/UA/Cookies the player used**, replayed by the downloader.

Additionally, `app/ytstrip.py` transparently unwraps CDNs that disguise video segments as fake-PNG-wrapped files (named `.webp`).

## If a site still fails

- Try pasting the player's **iframe URL** directly (right-click the player → Inspect → find `<iframe src="...">`).
- Open DevTools → Network tab → filter `m3u8` → press play → copy the URL and paste it into the tool.
- Login-required sites: add a `cookiefile` in `app/extractor.py` (`_ydl_options`).
- Sites with real DRM (Widevine etc.) are **not supported**.
- New ad domains slipping through: add them to `SKIP_RE` in `app/sniffer.py`.
- **Current limitation**: some abyss-family player hosts serve files only to live player sessions and answer 403 to plain replays — the tool still captures the episode URL and offers an "original" quality attempt; if that fails, use the DevTools method above.

## Code layout

```
app/
├── main.py        # FastAPI: API routes + web UI serving + SSE progress
├── extractor.py   # 4 layers: yt-dlp → static probe → abyss family → browser sniff
├── abyss.py       # Node-assisted unpacker for the Abyss/tonytonychopper family
├── sniffer.py     # headless browser: auto-click + capture media requests + headers/cookies
├── ytstrip.py     # unwraps fake-PNG-wrapped HLS segments at the HTTP layer
├── downloader.py  # queue + worker threads + progress hooks + cancel + cookie replay
└── static/        # web UI (plain HTML/CSS/JS, no build step)
```

API for scripts: `POST /api/extract` {url}, `POST /api/download` {url, format_id, media_url?, media_headers?, cookies?}, `GET /api/jobs`, `GET /api/events` (SSE)
