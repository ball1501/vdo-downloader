"""Local web server for the video downloader UI."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator
from starlette.concurrency import run_in_threadpool

from . import extractor
from .downloader import JobManager

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_OUTDIR = str(BASE_DIR / "downloads")

app = FastAPI(title="VDO Downloader")
manager = JobManager(default_outdir=DEFAULT_OUTDIR)


class ExtractRequest(BaseModel):
    url: str

    @field_validator("url")
    @classmethod
    def _require_http(cls, v: str) -> str:
        v = v.strip()
        if not v.startswith(("http://", "https://")):
            raise ValueError("ต้องเป็นลิงก์ http/https เท่านั้น")
        return v


class DownloadRequest(BaseModel):
    url: str
    format_id: str = "best"
    title: str | None = None
    outdir: str | None = None
    media_url: str | None = None
    media_headers: dict[str, str] | None = None
    cookies: list[dict] | None = None


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.post("/api/extract")
async def api_extract(req: ExtractRequest) -> dict:
    try:
        return await run_in_threadpool(extractor.extract_formats, req.url)
    except extractor.ExtractError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/download")
async def api_download(req: DownloadRequest) -> dict:
    outdir = req.outdir.strip() if req.outdir else None
    if outdir:
        outdir = os.path.abspath(os.path.expanduser(outdir))
        root = os.path.abspath(os.sep)
        if not outdir.startswith(root) or outdir == root:
            raise HTTPException(status_code=400, detail="โฟลเดอร์ปลายทางไม่ถูกต้อง")
    job = manager.submit(
        req.url,
        req.format_id,
        outdir,
        req.title,
        media_url=req.media_url,
        media_headers=req.media_headers,
        cookies=req.cookies,
    )
    return {"job_id": job.id}


@app.get("/api/jobs")
async def api_jobs() -> dict:
    return {"jobs": manager.snapshot()}


@app.post("/api/jobs/{job_id}/cancel")
async def api_cancel(job_id: str) -> dict:
    if not manager.cancel(job_id):
        raise HTTPException(status_code=404, detail="ไม่พบงานนี้ หรืองานจบไปแล้ว")
    return {"ok": True}


@app.get("/api/config")
async def api_config() -> dict:
    return {"default_outdir": DEFAULT_OUTDIR}


@app.get("/api/events")
async def api_events(request: Request) -> StreamingResponse:
    async def stream():
        last_version = -1
        while True:
            if await request.is_disconnected():
                break
            version = manager.version
            if version != last_version:
                last_version = version
                data = json.dumps(manager.snapshot(), ensure_ascii=False)
                yield f"event: jobs\ndata: {data}\n\n"
            else:
                yield ": ping\n\n"
            await asyncio.sleep(0.5)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
