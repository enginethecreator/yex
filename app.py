# app.py — Torrent Downloader
# libtorrent 2.0.x | FastAPI | single-file

import os
import time
import uuid
import threading
from pathlib import Path
from typing import Dict, Iterator

import libtorrent as lt
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse
from pydantic import BaseModel

# ── Config ────────────────────────────────────────────

DOWNLOADS_DIR = Path(os.environ.get("DOWNLOADS_DIR", "/tmp/downloads"))
DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)

STREAMABLE_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".mp3", ".m4a", ".flac", ".wav", ".webm"}

# ── State ─────────────────────────────────────────────

torrent_store: Dict[str, dict] = {}
store_lock = threading.Lock()

# ── libtorrent session ────────────────────────────────

def make_session() -> lt.session:
    settings = {
        "listen_interfaces": "0.0.0.0:6881",
        "enable_dht":   True,
        "enable_lsd":   True,
        "enable_upnp":  True,
        "enable_natpmp": True,
    }
    ses = lt.session(settings)
    ses.add_dht_router("router.bittorrent.com", 6881)
    ses.add_dht_router("router.utorrent.com",   6881)
    ses.add_dht_router("dht.transmissionbt.com", 6881)
    return ses

SESSION = make_session()

# ── Helpers ───────────────────────────────────────────

STATE_LABELS = [
    "queued", "checking", "downloading metadata",
    "downloading", "finished", "seeding",
    "checking resume data", "unknown"
]

def _fmt_size(b) -> str:
    try:
        b = int(b)
    except (TypeError, ValueError):
        return "—"
    if b <= 0:
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} PB"

def _safe_path(rel: str) -> Path:
    target = (DOWNLOADS_DIR / rel).resolve()
    if not str(target).startswith(str(DOWNLOADS_DIR.resolve())):
        raise HTTPException(403, "Forbidden")
    return target

def _build_file_list(h: lt.torrent_handle) -> list:
    try:
        ti = h.torrent_file()
        if not ti:
            return []
        fs = ti.files()
        result = []
        for i in range(ti.num_files()):
            rel  = fs.file_path(i)
            size = fs.file_size(i)
            result.append({
                "name":       Path(rel).name,
                "rel_path":   rel,
                "file_index": i,
                "size":       _fmt_size(size),
            })
        return result
    except Exception:
        return []

# ── Background poller ─────────────────────────────────

def _poll_loop():
    while True:
        with store_lock:
            for tid, entry in list(torrent_store.items()):
                h: lt.torrent_handle = entry.get("handle")
                if h is None or not h.is_valid():
                    continue
                s = h.status()
                state_idx   = int(s.state)
                state_label = STATE_LABELS[state_idx] if state_idx < len(STATE_LABELS) else "unknown"

                entry["progress"]   = round(s.progress * 100, 2)
                entry["state"]      = state_label
                entry["down_rate"]  = round(s.download_rate / 1024, 1)
                entry["up_rate"]    = round(s.upload_rate / 1024, 1)
                entry["peers"]      = s.num_peers
                entry["name"]       = s.name or entry.get("name", "")
                entry["total_size"] = s.total_wanted
                entry["downloaded"] = s.total_wanted_done

                # Build file list once metadata arrives
                if s.has_metadata and not entry.get("torrent_files"):
                    entry["torrent_files"] = _build_file_list(h)

                if state_label in ("finished", "seeding"):
                    entry["status"] = "completed"
                elif entry.get("status") not in ("completed", "error"):
                    entry["status"] = "downloading"

        time.sleep(1)

threading.Thread(target=_poll_loop, daemon=True).start()

# ── App ───────────────────────────────────────────────

app = FastAPI(title="Torrent Downloader")

class AddRequest(BaseModel):
    magnet: str

# ── API ───────────────────────────────────────────────

@app.post("/add")
def add_torrent(req: AddRequest):
    magnet = req.magnet.strip()
    if not magnet.startswith("magnet:"):
        raise HTTPException(400, "Only magnet links are supported")
    torrent_id = str(uuid.uuid4())
    try:
        params = lt.parse_magnet_uri(magnet)
        params.save_path = str(DOWNLOADS_DIR)
        handle = SESSION.add_torrent(params)
    except Exception as e:
        raise HTTPException(500, f"Failed to add torrent: {e}")
    with store_lock:
        torrent_store[torrent_id] = {
            "handle":        handle,
            "status":        "downloading",
            "progress":      0,
            "state":         "downloading metadata",
            "down_rate":     0,
            "up_rate":       0,
            "peers":         0,
            "name":          "",
            "total_size":    0,
            "downloaded":    0,
            "torrent_files": [],
            "magnet":        magnet,
        }
    return {"torrent_id": torrent_id}


@app.get("/status/{torrent_id}")
def get_status(torrent_id: str):
    with store_lock:
        entry = torrent_store.get(torrent_id)
    if not entry:
        raise HTTPException(404, "Torrent not found")
    return {
        "status":        entry["status"],
        "progress":      entry["progress"],
        "state":         entry["state"],
        "down_rate":     entry["down_rate"],
        "up_rate":       entry["up_rate"],
        "peers":         entry["peers"],
        "name":          entry["name"],
        "total_size":    _fmt_size(entry["total_size"]),
        "downloaded":    _fmt_size(entry["downloaded"]),
        "torrent_files": entry.get("torrent_files", []),
    }


@app.get("/files")
def list_files():
    results = []
    for p in sorted(DOWNLOADS_DIR.rglob("*")):
        if p.is_file():
            rel = str(p.relative_to(DOWNLOADS_DIR))
            results.append({
                "name":       p.name,
                "rel_path":   rel,
                "size":       _fmt_size(p.stat().st_size),
                "streamable": p.suffix.lower() in STREAMABLE_EXTS,
            })
    return {"files": results}


@app.get("/download-file")
def download_file(path: str):
    target = _safe_path(path)
    if not target.exists() or not target.is_file():
        raise HTTPException(404, "File not found")
    return FileResponse(
        path=str(target),
        filename=target.name,
        media_type="application/octet-stream",
    )


@app.get("/stream-file")
def stream_completed_file(path: str):
    """Stream an already-completed on-disk file."""
    target = _safe_path(path)
    if not target.exists() or not target.is_file():
        raise HTTPException(404, "File not found")

    def _read(p: Path, chunk: int = 65536) -> Iterator[bytes]:
        with open(p, "rb") as f:
            while True:
                data = f.read(chunk)
                if not data:
                    break
                yield data

    return StreamingResponse(
        _read(target),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{target.name}"',
            "Content-Length":      str(target.stat().st_size),
        },
    )


@app.get("/stream/{torrent_id}")
def stream_torrent_file(torrent_id: str, file_index: int = 0):
    """
    Stream a specific file from an active torrent as pieces arrive.
    - Sets sequential download so libtorrent downloads pieces in order.
    - Boosts priority of the first 5 pieces so file headers arrive fast.
    - Yields bytes to the client as soon as each chunk is on disk.
    """
    with store_lock:
        entry = torrent_store.get(torrent_id)
    if not entry:
        raise HTTPException(404, "Torrent not found")

    h: lt.torrent_handle = entry["handle"]
    if not h.is_valid():
        raise HTTPException(410, "Torrent handle is no longer valid")

    # Sequential mode: pieces downloaded in-order from piece 0
    h.set_sequential_download(True)

    # Boost the first 5 pieces (file container header for video)
    try:
        ti_check = h.torrent_file()
        if ti_check:
            for i in range(min(5, ti_check.num_pieces())):
                h.piece_priority(i, 7)
    except Exception:
        pass

    # Wait up to 60s for metadata
    waited = 0
    while not h.status().has_metadata and waited < 60:
        time.sleep(1)
        waited += 1
    if not h.status().has_metadata:
        raise HTTPException(504, "Timed out waiting for torrent metadata")

    ti = h.torrent_file()
    if not ti:
        raise HTTPException(500, "Could not read torrent file info")
    if file_index >= ti.num_files():
        raise HTTPException(400, f"file_index {file_index} out of range (torrent has {ti.num_files()} files)")

    rel_path  = ti.files().file_path(file_index)
    file_path = DOWNLOADS_DIR / rel_path
    file_name = Path(rel_path).name
    file_size = ti.files().file_size(file_index)

    file_path.parent.mkdir(parents=True, exist_ok=True)

    def byte_generator(path: Path, chunk: int = 65536) -> Iterator[bytes]:
        # Wait up to 30s for libtorrent to create the file on first piece write
        waited_file = 0
        while not path.exists() and waited_file < 30:
            time.sleep(0.5)
            waited_file += 0.5

        with open(path, "rb") as f:
            while True:
                data = f.read(chunk)
                if data:
                    yield data
                else:
                    s = h.status()
                    # Seeding = fully downloaded, real EOF
                    if s.is_seeding or int(s.state) == 5:
                        break
                    # Pieces still arriving — wait briefly then retry
                    time.sleep(0.3)

    headers = {
        "Content-Disposition": f'attachment; filename="{file_name}"',
        "Accept-Ranges":       "none",
    }
    if file_size > 0:
        headers["Content-Length"] = str(file_size)

    return StreamingResponse(
        byte_generator(file_path),
        media_type="application/octet-stream",
        headers=headers,
    )


@app.get("/health")
def health():
    return {"status": "ok"}


# ── UI ────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def home():
    return r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>TORR.DROP</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@400;700;800&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0a0a0a;--bg2:#111;--bg3:#181818;
  --border:#222;--border2:#2e2e2e;
  --accent:#e8ff47;--accent2:#b8cc2a;
  --red:#ff4545;--green:#47ff8e;--blue:#47b4ff;--purple:#b47fff;
  --text:#c8c8c8;--dim:#505050;--white:#f0f0f0;
  --mono:'JetBrains Mono',monospace;
  --display:'Syne',sans-serif;
}
html,body{min-height:100%;background:var(--bg);color:var(--text);font-family:var(--display);overflow-x:hidden}
body::after{
  content:'';position:fixed;inset:0;pointer-events:none;z-index:9998;opacity:.4;
  background-image:url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)' opacity='0.03'/%3E%3C/svg%3E");
}
.shell{max-width:860px;margin:0 auto;padding:48px 24px 80px}
.logo{font-size:52px;font-weight:800;letter-spacing:-.02em;color:var(--white);line-height:1}
.logo span{color:var(--accent)}
.tagline{font-family:var(--mono);font-size:11px;color:var(--dim);letter-spacing:.14em;text-transform:uppercase;margin-top:8px}
.header-line{height:1px;background:linear-gradient(90deg,var(--accent) 0%,transparent 60%);margin-top:20px;margin-bottom:48px}
.section-label{font-family:var(--mono);font-size:9px;letter-spacing:.18em;text-transform:uppercase;color:var(--dim);margin-bottom:12px;display:flex;align-items:center;gap:10px}
.section-label::after{content:'';flex:1;height:1px;background:var(--border)}
/* input */
.input-block{margin-bottom:32px}
.input-row{display:flex;border:1px solid var(--border2);border-radius:3px;overflow:hidden;transition:border-color .2s}
.input-row:focus-within{border-color:var(--accent)}
.input-prefix{font-family:var(--mono);font-size:10px;color:var(--accent);background:rgba(232,255,71,.05);border-right:1px solid var(--border2);padding:0 14px;display:flex;align-items:center;white-space:nowrap;letter-spacing:.06em;user-select:none}
#magnet-input{flex:1;background:transparent;border:none;outline:none;color:var(--white);font-family:var(--mono);font-size:12px;padding:14px 16px;min-width:0}
#magnet-input::placeholder{color:var(--dim)}
.btn-add{font-family:var(--mono);font-size:11px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;border:none;background:var(--accent);color:#000;padding:0 24px;cursor:pointer;transition:background .15s;white-space:nowrap;flex-shrink:0}
.btn-add:hover{background:var(--accent2)}
.btn-add:disabled{background:#3a3d00;color:#6a6f00;cursor:not-allowed}
.status-msg{font-family:var(--mono);font-size:10px;color:var(--dim);margin-top:8px;min-height:16px}
.status-msg.err{color:var(--red)}
/* torrent cards */
.torrents-block{margin-bottom:36px}
.torrent-card{background:var(--bg2);border:1px solid var(--border);border-radius:3px;overflow:hidden;margin-bottom:10px;position:relative}
.torrent-card::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;background:linear-gradient(90deg,var(--accent),transparent 70%)}
.tc-header{padding:12px 16px;display:flex;align-items:center;gap:12px;border-bottom:1px solid var(--border)}
.tc-name{font-family:var(--mono);font-size:12px;color:var(--white);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tc-state{font-family:var(--mono);font-size:9px;letter-spacing:.1em;text-transform:uppercase;padding:3px 8px;border-radius:2px;flex-shrink:0}
.tc-state.downloading{background:rgba(71,180,255,.1);color:var(--blue);border:1px solid rgba(71,180,255,.2)}
.tc-state.completed,.tc-state.seeding{background:rgba(71,255,142,.1);color:var(--green);border:1px solid rgba(71,255,142,.2)}
.tc-state.checking,.tc-state.queued{background:rgba(232,255,71,.07);color:var(--accent);border:1px solid rgba(232,255,71,.15)}
.tc-state.error{background:rgba(255,69,69,.1);color:var(--red);border:1px solid rgba(255,69,69,.2)}
.tc-body{padding:12px 16px}
.tc-track{width:100%;height:3px;background:var(--border2);margin-bottom:12px;overflow:hidden}
.tc-fill{height:100%;background:linear-gradient(90deg,var(--accent),var(--green));transition:width .6s ease}
.tc-meta{display:flex;gap:20px;flex-wrap:wrap}
.tc-stat{display:flex;flex-direction:column;gap:2px}
.tc-stat-key{font-family:var(--mono);font-size:9px;color:var(--dim);text-transform:uppercase;letter-spacing:.1em}
.tc-stat-val{font-family:var(--mono);font-size:13px;color:var(--white);font-weight:600}
.tc-pct{font-family:'Syne',sans-serif;font-size:28px;font-weight:800;color:var(--accent);line-height:1;margin-left:auto;align-self:center}
/* per-file rows inside active card */
.tc-files{border-top:1px solid var(--border);margin-top:14px;padding-top:12px}
.tc-files-label{font-family:var(--mono);font-size:9px;color:var(--dim);text-transform:uppercase;letter-spacing:.14em;margin-bottom:8px}
.tc-file-row{display:flex;align-items:center;gap:10px;padding:7px 0;border-bottom:1px solid var(--border);font-family:var(--mono);font-size:11px}
.tc-file-row:last-child{border-bottom:none}
.tc-file-name{flex:1;color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tc-file-ext{font-size:9px;background:var(--bg3);border:1px solid var(--border2);padding:2px 6px;border-radius:2px;color:var(--dim);flex-shrink:0}
.tc-file-size{color:var(--dim);white-space:nowrap;font-size:10px;flex-shrink:0}
.tc-file-actions{display:flex;gap:6px;flex-shrink:0}
/* files table */
.files-block{margin-bottom:36px}
.files-table{width:100%;border-collapse:collapse}
.files-table th{font-family:var(--mono);font-size:9px;letter-spacing:.14em;text-transform:uppercase;color:var(--dim);text-align:left;padding:8px 12px;border-bottom:1px solid var(--border);background:var(--bg3)}
.files-table td{font-family:var(--mono);font-size:11px;color:var(--text);padding:10px 12px;border-bottom:1px solid var(--border);vertical-align:middle}
.files-table tr:last-child td{border-bottom:none}
.files-table tr:hover td{background:rgba(255,255,255,.02)}
.td-name{color:var(--white);word-break:break-all}
.td-size{color:var(--dim);white-space:nowrap}
.td-actions{text-align:right;white-space:nowrap}
/* shared button styles */
.btn{font-family:var(--mono);font-size:9px;letter-spacing:.1em;text-transform:uppercase;border-radius:2px;cursor:pointer;padding:5px 11px;transition:all .15s;white-space:nowrap}
.btn-dl{background:transparent;border:1px solid var(--border2);color:var(--accent)}
.btn-dl:hover{background:rgba(232,255,71,.07);border-color:var(--accent)}
.btn-stream{background:transparent;border:1px solid rgba(180,127,255,.3);color:var(--purple)}
.btn-stream:hover{background:rgba(180,127,255,.08);border-color:var(--purple)}
/* misc */
.empty-state{font-family:var(--mono);font-size:11px;color:var(--dim);text-align:center;padding:32px;letter-spacing:.06em}
.spinner{display:inline-block;width:10px;height:10px;border:1.5px solid var(--border2);border-top-color:var(--accent);border-radius:50%;animation:spin .7s linear infinite;vertical-align:middle;margin-right:6px}
@keyframes spin{to{transform:rotate(360deg)}}
@keyframes fadeUp{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:translateY(0)}}
.anim{animation:fadeUp .3s ease forwards}
</style>
</head>
<body>
<div class="shell">

  <header class="anim">
    <div class="logo">TORR<span>.</span>DROP</div>
    <div class="tagline">BitTorrent acquisition // libtorrent 2.0 // stream or download per file</div>
    <div class="header-line"></div>
  </header>

  <div class="input-block anim">
    <div class="section-label">Add torrent</div>
    <div class="input-row">
      <div class="input-prefix">MAGNET://</div>
      <input id="magnet-input" type="text" placeholder="magnet:?xt=urn:btih:…" spellcheck="false" autocomplete="off"/>
      <button class="btn-add" id="btn-add" onclick="addTorrent()">Add</button>
    </div>
    <div class="status-msg" id="add-msg"></div>
  </div>

  <div class="torrents-block">
    <div class="section-label">Active downloads</div>
    <div id="torrents-list">
      <div class="empty-state" id="no-downloads">No active downloads</div>
    </div>
  </div>

  <div class="files-block">
    <div class="section-label">Downloaded files</div>
    <div id="files-container">
      <div class="empty-state" id="no-files">No files yet</div>
    </div>
  </div>

</div>
<script>
const activePollers = {};

const STREAMABLE = new Set(['mp4','mkv','avi','mov','mp3','m4a','flac','wav','webm']);

function ext(name) {
  return name.split('.').pop().toLowerCase();
}

function isStreamable(name) {
  return STREAMABLE.has(ext(name));
}

function escHtml(s) {
  return String(s)
    .replace(/&/g,'&amp;')
    .replace(/</g,'&lt;')
    .replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;');
}
function escAttr(s) {
  return String(s).replace(/'/g,"\\'");
}

function setMsg(msg, isErr=false) {
  const el = document.getElementById('add-msg');
  el.innerHTML = msg;
  el.className = 'status-msg' + (isErr ? ' err' : '');
}

async function addTorrent() {
  const input  = document.getElementById('magnet-input');
  const magnet = input.value.trim();
  if (!magnet)                          { setMsg('Paste a magnet link first.', true); return; }
  if (!magnet.startsWith('magnet:'))    { setMsg('Only magnet:// links are supported.', true); return; }

  const btn = document.getElementById('btn-add');
  btn.disabled = true;
  setMsg('<span class="spinner"></span> Adding…');

  try {
    const res  = await fetch('/add', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({magnet})
    });
    const data = await res.json();
    if (!res.ok) { setMsg(data.detail || 'Failed to add torrent.', true); return; }

    input.value = '';
    setMsg('Torrent added — tracking…');
    document.getElementById('no-downloads').style.display = 'none';
    createCard(data.torrent_id);
    startPolling(data.torrent_id);
  } catch(e) {
    setMsg('Network error: ' + e.message, true);
  } finally {
    btn.disabled = false;
  }
}

function createCard(tid) {
  const list = document.getElementById('torrents-list');
  const card = document.createElement('div');
  card.className = 'torrent-card anim';
  card.id = 'card-' + tid;
  card.innerHTML = `
    <div class="tc-header">
      <div class="tc-name" id="name-${tid}">Fetching metadata…</div>
      <div class="tc-state downloading" id="state-${tid}">connecting</div>
    </div>
    <div class="tc-body">
      <div class="tc-track"><div class="tc-fill" id="fill-${tid}" style="width:0%"></div></div>
      <div class="tc-meta">
        <div class="tc-stat"><div class="tc-stat-key">Down</div><div class="tc-stat-val" id="down-${tid}">— KB/s</div></div>
        <div class="tc-stat"><div class="tc-stat-key">Up</div><div class="tc-stat-val" id="up-${tid}">— KB/s</div></div>
        <div class="tc-stat"><div class="tc-stat-key">Peers</div><div class="tc-stat-val" id="peers-${tid}">0</div></div>
        <div class="tc-stat"><div class="tc-stat-key">Size</div><div class="tc-stat-val" id="size-${tid}">—</div></div>
        <div class="tc-pct" id="pct-${tid}">0%</div>
      </div>
      <div class="tc-files" id="tc-files-${tid}" style="display:none">
        <div class="tc-files-label">Files in torrent</div>
        <div id="tc-files-list-${tid}"></div>
      </div>
    </div>
  `;
  list.appendChild(card);
}

function startPolling(tid) {
  const interval = setInterval(async () => {
    try {
      const res = await fetch('/status/' + tid);
      if (!res.ok) return;
      const d = await res.json();
      updateCard(tid, d);
      if (d.status === 'completed') {
        clearInterval(activePollers[tid]);
        delete activePollers[tid];
        refreshFiles();
      }
    } catch(e) {}
  }, 1200);
  activePollers[tid] = interval;
}

function updateCard(tid, d) {
  const stateClass =
    (d.state.includes('seed') || d.status === 'completed') ? 'completed' :
    d.state.includes('check') ? 'checking' :
    d.state.includes('queue') ? 'queued'   :
    d.status === 'error'      ? 'error'     : 'downloading';

  const g = (id) => document.getElementById(id + '-' + tid);

  if (g('name') && d.name)  g('name').textContent  = d.name;
  if (g('state')) { g('state').textContent = d.state; g('state').className = 'tc-state ' + stateClass; }
  if (g('fill'))  g('fill').style.width   = d.progress + '%';
  if (g('pct'))   g('pct').textContent    = Math.round(d.progress) + '%';
  if (g('down'))  g('down').textContent   = d.down_rate + ' KB/s';
  if (g('up'))    g('up').textContent     = d.up_rate + ' KB/s';
  if (g('peers')) g('peers').textContent  = d.peers;
  if (g('size'))  g('size').textContent   = d.total_size || '—';

  // Render per-file list once metadata arrives (render once only)
  if (d.torrent_files && d.torrent_files.length > 0) {
    const section = document.getElementById('tc-files-' + tid);
    const list    = document.getElementById('tc-files-list-' + tid);
    if (section && list && list.children.length === 0) {
      section.style.display = 'block';
      d.torrent_files.forEach(f => {
        const streamable = isStreamable(f.name);
        const row = document.createElement('div');
        row.className = 'tc-file-row';
        row.innerHTML = `
          <div class="tc-file-name" title="${escHtml(f.rel_path)}">${escHtml(f.name)}</div>
          <span class="tc-file-ext">.${ext(f.name)}</span>
          <div class="tc-file-size">${escHtml(f.size)}</div>
          <div class="tc-file-actions">
            ${streamable
              ? `<button class="btn btn-stream" onclick="window.open('/stream/${tid}?file_index=${f.file_index}','_blank')">Stream</button>`
              : ''}
            <button class="btn btn-dl" onclick="window.open('/stream/${tid}?file_index=${f.file_index}','_blank')">Download</button>
          </div>
        `;
        list.appendChild(row);
      });
    }
  }
}

async function refreshFiles() {
  try {
    const res  = await fetch('/files');
    const data = await res.json();
    const container = document.getElementById('files-container');

    if (!data.files || data.files.length === 0) {
      container.innerHTML = '<div class="empty-state">No files yet</div>';
      return;
    }

    const rows = data.files.map(f => {
      const streamBtn = f.streamable
        ? `<button class="btn btn-stream" style="margin-right:6px"
             onclick="window.open('/stream-file?path=${encodeURIComponent(f.rel_path)}','_blank')">Stream</button>`
        : '';
      const dlBtn = `<button class="btn btn-dl"
          onclick="window.open('/download-file?path=${encodeURIComponent(f.rel_path)}','_blank')">Download</button>`;
      return `
        <tr>
          <td class="td-name">${escHtml(f.name)}</td>
          <td class="td-size">${escHtml(f.size)}</td>
          <td class="td-actions">${streamBtn}${dlBtn}</td>
        </tr>`;
    }).join('');

    container.innerHTML = `
      <table class="files-table">
        <thead><tr>
          <th>File</th><th>Size</th><th style="text-align:right">Actions</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>`;
  } catch(e) {}
}

document.getElementById('magnet-input').addEventListener('keydown', e => {
  if (e.key === 'Enter') addTorrent();
});

setInterval(refreshFiles, 5000);
refreshFiles();
</script>
</body>
</html>"""
