import asyncio
import os
import re
import secrets
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, parse_qs

import yt_dlp
from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

APP_SECRET = os.getenv("APP_SECRET", "")
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "/data/downloads"))
MAX_DOWNLOAD_MB = int(os.getenv("MAX_DOWNLOAD_MB", "800"))
FILE_TTL_MINUTES = int(os.getenv("FILE_TTL_MINUTES", "60"))
MAX_ACTIVE_JOBS = max(1, min(4, int(os.getenv("MAX_ACTIVE_JOBS", "1"))))
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="WebTube", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

jobs: dict[str, dict[str, Any]] = {}
job_lock = asyncio.Lock()
active_semaphore = asyncio.Semaphore(MAX_ACTIVE_JOBS)

ALLOWED_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "music.youtube.com"}

class UrlRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2048)

class DownloadRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2048)
    format_id: str | None = Field(default=None, max_length=40)
    audio_only: bool = False

def check_auth(x_app_token: str | None):
    # Fail closed: never expose download actions when the service secret is missing.
    if not APP_SECRET:
        raise HTTPException(status_code=503, detail="服务端尚未配置 APP_SECRET，下载功能已禁用。请管理员先设置访问密钥。")
    if not secrets.compare_digest(x_app_token or "", APP_SECRET):
        raise HTTPException(status_code=401, detail="访问令牌无效。请在网页设置中配置服务端访问令牌。")

def validate_youtube_url(raw: str) -> str:
    try:
        p = urlparse(raw.strip())
    except Exception:
        raise HTTPException(400, "链接格式无效")
    if p.scheme not in ("http", "https") or (p.hostname or "").lower() not in ALLOWED_HOSTS:
        raise HTTPException(400, "只支持 youtube.com 与 youtu.be 链接")
    if (p.hostname or "").lower() == "youtu.be":
        vid = p.path.strip("/")
    else:
        vid = parse_qs(p.query).get("v", [""])[0]
        if not vid and p.path.startswith("/shorts/"):
            vid = p.path.split("/")[2] if len(p.path.split("/")) > 2 else ""
        if not vid and p.path.startswith("/embed/"):
            vid = p.path.split("/")[2] if len(p.path.split("/")) > 2 else ""
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,20}", vid or ""):
        raise HTTPException(400, "无法识别视频 ID，请粘贴单个 YouTube 视频链接")
    return f"https://www.youtube.com/watch?v={vid}"

def ydl_opts_base():
    return {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 20,
        "retries": 2,
        "extractor_retries": 2,
        "cachedir": False,
        "js_runtimes": {"node": {}},
    }

@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse((Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8"))

@app.get("/api/health")
async def health():
    ffmpeg_ok = False
    try:
        result = await asyncio.to_thread(
            subprocess.run, ["ffmpeg", "-version"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=5, check=False,
        )
        ffmpeg_ok = result.returncode == 0
    except Exception:
        ffmpeg_ok = False
    return {
        "ok": True,
        "engine": "yt-dlp",
        "yt_dlp_version": yt_dlp.version.__version__,
        "ffmpeg_available": ffmpeg_ok,
    }

@app.post("/api/info")
async def get_info(req: UrlRequest, x_app_token: str | None = Header(default=None)):
    check_auth(x_app_token)
    url = validate_youtube_url(req.url)
    try:
        def extract():
            with yt_dlp.YoutubeDL(ydl_opts_base()) as ydl:
                return ydl.extract_info(url, download=False)
        info = await asyncio.to_thread(extract)
    except Exception as e:
        raise HTTPException(502, f"无法获取视频信息：{str(e)[:350]}")
    formats = []
    for f in (info.get("formats") or []):
        if not f.get("url"):
            continue
        formats.append({
            "format_id": str(f.get("format_id", "")),
            "ext": f.get("ext"),
            "resolution": f.get("resolution") or ("音频" if f.get("vcodec") == "none" else f.get("format_note")),
            "filesize": f.get("filesize") or f.get("filesize_approx"),
            "has_video": f.get("vcodec") != "none",
            "has_audio": f.get("acodec") != "none",
            "note": f.get("format_note"),
        })
    formats.sort(key=lambda x: (x["has_video"], x["resolution"] or ""), reverse=True)
    return {
        "id": info.get("id"),
        "title": info.get("title"),
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail"),
        "uploader": info.get("uploader"),
        "formats": formats[:80],
    }

async def run_download(job_id: str, url: str, format_id: str | None, audio_only: bool):
    async with active_semaphore:
        job = jobs[job_id]
        job["status"] = "downloading"
        job["started_at"] = time.time()
        outtmpl = str(DOWNLOAD_DIR / f"{job_id}.%(ext)s")
        def hook(d):
            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                got = d.get("downloaded_bytes", 0)
                job["progress"] = round(min(99, got * 100 / total), 1) if total else None
                job["downloaded_bytes"] = got
                job["total_bytes"] = total
                job["speed"] = d.get("speed")
            elif d.get("status") == "finished":
                job["progress"] = 100
                job["status"] = "processing"
        opts = ydl_opts_base() | {
            "outtmpl": outtmpl,
            "progress_hooks": [hook],
            "max_filesize": MAX_DOWNLOAD_MB * 1024 * 1024,
            "overwrites": True,
        }
        if audio_only:
            opts.update({"format": "bestaudio/best", "postprocessors": [{
                "key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"
            }]})
        elif format_id:
            opts["format"] = f"{format_id}+bestaudio/best/{format_id}/best"
            opts["merge_output_format"] = "mp4"
        else:
            opts["format"] = "bestvideo*+bestaudio/best"
            opts["merge_output_format"] = "mp4"
        try:
            def do_download():
                with yt_dlp.YoutubeDL(opts) as ydl:
                    ydl.download([url])
            await asyncio.to_thread(do_download)
            candidates = sorted(DOWNLOAD_DIR.glob(f"{job_id}.*"), key=lambda p: p.stat().st_mtime, reverse=True)
            if not candidates:
                raise RuntimeError("下载完成但没有找到输出文件")
            path = candidates[0]
            job.update({"status": "completed", "progress": 100, "file_name": path.name,
                        "file_size": path.stat().st_size, "expires_at": time.time() + FILE_TTL_MINUTES * 60})
        except Exception as e:
            for p in DOWNLOAD_DIR.glob(f"{job_id}.*"):
                try:
                    p.unlink()
                except OSError:
                    pass
            job.update({"status": "failed", "error": str(e)[:500]})
        finally:
            job["finished_at"] = time.time()

@app.post("/api/downloads")
async def create_download(req: DownloadRequest, x_app_token: str | None = Header(default=None)):
    check_auth(x_app_token)
    url = validate_youtube_url(req.url)
    if sum(1 for j in jobs.values() if j["status"] in ("queued", "downloading", "processing")) >= MAX_ACTIVE_JOBS * 3:
        raise HTTPException(429, "下载队列已满，请稍后再试")
    job_id = uuid.uuid4().hex
    jobs[job_id] = {"id": job_id, "status": "queued", "progress": 0, "created_at": time.time()}
    asyncio.create_task(run_download(job_id, url, req.format_id, req.audio_only))
    return {"id": job_id, "status": "queued"}

@app.get("/api/downloads/{job_id}")
async def get_download(job_id: str, x_app_token: str | None = Header(default=None)):
    check_auth(x_app_token)
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "任务不存在或服务已重启")
    result = {k: v for k, v in job.items() if k not in ("error",)}
    if job.get("error"):
        result["error"] = job["error"]
    if job.get("status") == "completed" and time.time() < job.get("expires_at", 0):
        result["download_url"] = f"/api/downloads/{job_id}/file"
    return result

@app.get("/api/downloads/{job_id}/file")
async def download_file(job_id: str, x_app_token: str | None = Header(default=None)):
    check_auth(x_app_token)
    job = jobs.get(job_id)
    if not job or job.get("status") != "completed":
        raise HTTPException(404, "文件尚未准备好")
    if time.time() >= job.get("expires_at", 0):
        raise HTTPException(410, "下载链接已过期，请重新下载")
    path = DOWNLOAD_DIR / job["file_name"]
    if not path.exists():
        raise HTTPException(410, "文件已清理，请重新下载")
    return FileResponse(path, filename=path.name, media_type="application/octet-stream")

async def cleanup_loop():
    while True:
        await asyncio.sleep(300)
        now = time.time()
        for p in DOWNLOAD_DIR.iterdir():
            try:
                if p.is_file() and now - p.stat().st_mtime > FILE_TTL_MINUTES * 60:
                    p.unlink()
            except OSError:
                pass
        for job_id, job in list(jobs.items()):
            if job.get("finished_at") and now - job["finished_at"] > max(FILE_TTL_MINUTES * 60, 3600):
                jobs.pop(job_id, None)

@app.on_event("startup")
async def startup_tasks():
    asyncio.create_task(cleanup_loop())
