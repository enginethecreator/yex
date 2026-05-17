import os
import asyncio
import threading
from pathlib import Path

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, FileResponse
import uvicorn

from torrentp import TorrentDownloader

app = FastAPI(title="Cloud Torrent Downloader")
DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)
active_downloads = {}

# Background worker unchanged...

@app.get("/", response_class=HTMLResponse)
async def home():
    files = sorted([f.name for f in DOWNLOAD_DIR.iterdir() if f.is_file()])
    file_items = "\n".join(
        f'<li><a href="/files/{f}" download>{f}</a></li>' for f in files
    ) if files else "<li>No files downloaded yet.</li>"

    return f"""<!DOCTYPE html>
<html>
<head>
    <title>Cloud Torrent Downloader</title>
    <style>
        body {{ font-family: sans-serif; max-width: 600px; margin: 40px auto; padding: 20px; }}
        input[type="text"] {{ width: 80%; padding: 10px; margin-bottom: 10px; }}
        button {{ padding: 10px 20px; background: #007bff; color: white; border: none; cursor: pointer; }}
        .file-list {{ margin-top: 20px; background: #f8f9fa; padding: 15px; border-radius: 5px; }}
    </style>
</head>
<body>
    <h2>Cloud Torrent Downloader</h2>
    <form action="/download" method="POST">
        <input type="text" name="torrent_url" placeholder="Paste Magnet Link or Torrent URL here" required>
        <button type="submit">Start Download</button>
    </form>
    <div class="file-list">
        <h3>Completed Downloads</h3>
        <ul>
            {file_items}
        </ul>
    </div>
</body>
</html>"""

@app.post("/download")
async def start_download(torrent_url: str = Form(...)):
    # ... unchanged ...

@app.get("/files/{filename}")
async def download_file(filename: str):
    # ... unchanged ...

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
