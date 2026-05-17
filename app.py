import os
import asyncio
import threading
from pathlib import Path
from datetime import datetime

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
import uvicorn

from torrentp import TorrentDownloader

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(title="Cloud Torrent Downloader")

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

# In‑memory download status (id -> dict with status, filename, started, finished)
active_downloads = {}

# ---------------------------------------------------------------------------
# Helper: clean log messages
# ---------------------------------------------------------------------------
def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

# ---------------------------------------------------------------------------
# Embedded HTML (improved file list)
# ---------------------------------------------------------------------------
HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Cloud Torrent Downloader</title>
    <style>
        body { font-family: sans-serif; max-width: 600px; margin: 40px auto; padding: 20px; background: #f5f5f5; }
        input[type="text"] { width: 80%; padding: 10px; margin-bottom: 10px; }
        button { padding: 10px 20px; background: #007bff; color: white; border: none; cursor: pointer; }
        .file-list { margin-top: 20px; background: white; padding: 15px; border-radius: 5px; box-shadow: 0 0 5px rgba(0,0,0,0.1); }
        .status { margin-top: 10px; font-size: 0.9em; color: #333; }
    </style>
</head>
<body>
    <h2>📥 Cloud Torrent Downloader</h2>
    <form action="/download" method="POST">
        <input type="text" name="torrent_url" placeholder="Paste Magnet Link or Torrent URL" required>
        <button type="submit">⬇️ Start Download</button>
    </form>

    <div class="file-list">
        <h3>📁 Completed Files</h3>
        <ul>
            {{file_list}}
        </ul>
    </div>
    <div class="status" id="log"></div>
</body>
</html>
"""

# ---------------------------------------------------------------------------
# Background worker with detailed logging
# ---------------------------------------------------------------------------
def run_torrent_async(magnet_or_url: str, download_id: str):
    """Background thread: run torrent download, update status."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        log(f"🚀 [{download_id}] Starting download: {magnet_or_url[:80]}...")
        downloader = TorrentDownloader(magnet_or_url, str(DOWNLOAD_DIR))
        active_downloads[download_id] = {
            "status": "downloading",
            "started": datetime.now().isoformat(),
            "magnet": magnet_or_url,
        }
        # Start download (blocking until complete)
        loop.run_until_complete(downloader.start_download())
        # After completion, detect the file(s)
        files = sorted(DOWNLOAD_DIR.glob("*"), key=lambda f: f.stat().st_mtime, reverse=True)
        downloaded_file = files[0] if files else None
        filename = downloaded_file.name if downloaded_file else "unknown"
        filesize = downloaded_file.stat().st_size if downloaded_file else 0

        active_downloads[download_id] = {
            "status": "completed",
            "filename": filename,
            "size": filesize,
            "started": active_downloads[download_id]["started"],
            "finished": datetime.now().isoformat(),
        }
        log(f"✅ [{download_id}] Completed: {filename} ({filesize/1024:.1f} KB)")
    except Exception as e:
        active_downloads[download_id] = {
            "status": "error",
            "error": str(e),
        }
        log(f"❌ [{download_id}] Error: {e}")

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def home():
    # Build file list with size info
    files = sorted(DOWNLOAD_DIR.iterdir(), key=lambda f: f.stat().st_mtime, reverse=True)
    file_items = ""
    for f in files:
        if f.is_file():
            size_kb = f.stat().st_size / 1024
            file_items += f'<li>📄 <a href="/files/{f.name}" download>{f.name}</a> ({size_kb:.1f} KB)</li>'
    if not file_items:
        file_items = "<li>No files downloaded yet.</li>"

    html = HTML.replace("{{file_list}}", file_items)
    return html

@app.post("/download")
async def start_download(torrent_url: str = Form(...)):
    if not torrent_url:
        return JSONResponse({"error": "No link provided"}, status_code=400)

    # Simple unique ID
    download_id = f"dl_{len(active_downloads)+1}"
    active_downloads[download_id] = {"status": "queued"}
    log(f"📌 [{download_id}] Queued")

    thread = threading.Thread(target=run_torrent_async, args=(torrent_url, download_id))
    thread.start()

    return {
        "status": "Download started in background.",
        "download_id": download_id,
        "check_progress": f"/status/{download_id}"
    }

@app.get("/status/{download_id}")
async def download_status(download_id: str):
    info = active_downloads.get(download_id)
    if not info:
        return JSONResponse({"error": "Unknown download ID"}, status_code=404)
    return info

@app.get("/files/{filename}")
async def download_file(filename: str):
    file_path = DOWNLOAD_DIR / filename
    if not file_path.exists() or not file_path.is_file():
        return JSONResponse({"error": "File not found"}, status_code=404)
    log(f"📤 Serving file: {filename}")
    return FileResponse(
        path=str(file_path),
        filename=filename,
        media_type="application/octet-stream",
    )

@app.get("/health")
async def health():
    return {"status": "ok", "files_count": len(list(DOWNLOAD_DIR.iterdir()))}

# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    log("🔥 Torrent Downloader starting...")
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
