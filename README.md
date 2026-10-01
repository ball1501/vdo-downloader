# 🎬 VDO Downloader

เครื่องมือดาวน์โหลดวิดีโอจากเว็บดูหนัง/อนิเมะที่ฝัง player ไว้ (HLS `.m3u8`, `.mp4` ฯลฯ) มาเป็นไฟล์ไว้ดูออฟไลน์ — รันเป็นเว็บในเครื่อง วางลิงก์ เลือกคุณภาพ กดดาวน์โหลด ดู progress ได้เรียลไทม์

> ⚠️ ใช้เพื่อส่วนตัว/ดูออฟไลน์เท่านั้น กรุณาสนับสนุนผู้สร้างผลงานตัวจริงด้วย

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
- **ขีดจำกัดปัจจุบัน**: player host บางเจ้า (เช่น abyssplayer ที่ animeruka ใช้) มี flow หลังโฆษณาที่ player bundle ของเขาควบคุมด้วย API ลับ — ตัว sniffer ผ่านประตู overlay/โฆษณาได้แล้วแต่จับยังไม่ถึง m3u8 กรณีนี้ต้องเล่นผ่านเบราว์เซอร์จริงแล้วเอา URL m3u8 จาก DevTools มาวางแทน (หรือใช้เว็บที่แจก server แบบ mp4 ตรง)

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
