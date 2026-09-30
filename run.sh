#!/bin/bash
# รัน VDO Downloader — สร้าง venv + ติดตั้ง dependencies อัตโนมัติครั้งแรก
set -e
cd "$(dirname "$0")"

# ให้เจอ ffmpeg ที่ติดตั้งผ่าน Homebrew เสมอ
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

if [ ! -d .venv ]; then
  echo "ติดตั้งครั้งแรก: สร้าง venv + ติดตั้ง dependencies..."
  python3 -m venv .venv
  .venv/bin/pip install --quiet --upgrade pip
  .venv/bin/pip install --quiet -r requirements.txt
fi

PORT=8787
echo "เริ่มเซิร์ฟเวอร์ที่ http://localhost:$PORT (กด Ctrl+C เพื่อหยุด)"
( sleep 2 && open "http://localhost:$PORT" ) &

exec .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "$PORT"
