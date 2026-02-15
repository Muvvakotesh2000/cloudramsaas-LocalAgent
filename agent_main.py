# agent_main.py
import os
import shutil
import zipfile
import uuid
from pathlib import Path
from typing import Optional

import requests
from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from agent_config import (
    AGENT_HOST,
    AGENT_PORT,
    AGENT_TOKEN,
    ALLOWED_ORIGINS,
    CACHE_DIR,
    DOWNLOADS_DIR,
    MAX_ZIP_MB,
    MAX_DOWNLOAD_MB,
    SAFE_BASE_DIRS,
)

# You already have this file — we import your implementation.
# It should expose something like list_local_tasks() or get_local_tasks().
import agent_process_manager as pm

from agent_installer import install_task, uninstall_task, run_task_now, task_status

app = FastAPI(title="CloudRAMS Local Agent", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS or ["http://localhost"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -----------------------------
# Auth helper (optional token)
# -----------------------------
def require_token(x_agent_token: Optional[str]):
    if not AGENT_TOKEN:
        return  # token not enforced
    if (x_agent_token or "").strip() != AGENT_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized (bad agent token)")

def _is_path_allowed(p: Path) -> bool:
    # If SAFE_BASE_DIRS empty => allow all (not recommended)
    if not SAFE_BASE_DIRS:
        return True
    try:
        rp = p.resolve()
    except Exception:
        return False
    for base in SAFE_BASE_DIRS:
        try:
            if rp.is_relative_to(Path(base).resolve()):
                return True
        except Exception:
            # Python <3.9 fallback
            try:
                rb = Path(base).resolve()
                if str(rp).lower().startswith(str(rb).lower()):
                    return True
            except Exception:
                pass
    return False

def _zip_dir(src_dir: Path, zip_path: Path):
    base = src_dir.name
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for root, _, files in os.walk(src_dir):
            for fn in files:
                full = Path(root) / fn
                rel = full.relative_to(src_dir)
                arc = Path(base) / rel
                zf.write(full, str(arc))

def _size_mb(path: Path) -> float:
    return path.stat().st_size / (1024 * 1024)

# -----------------------------
# Models
# -----------------------------
class ZipFolderRequest(BaseModel):
    folder_path: str

class UploadToUrlRequest(BaseModel):
    file_path: str
    put_url: str
    content_type: str = "application/zip"

class DownloadFromUrlRequest(BaseModel):
    url: str
    filename: Optional[str] = None  # if not provided, auto-generate

class InstallAutorunRequest(BaseModel):
    python_exe: Optional[str] = None  # defaults to current interpreter

# -----------------------------
# Endpoints
# -----------------------------
@app.get("/health")
def health():
    return {"ok": True, "service": "cloudrams-local-agent"}

@app.get("/running_tasks")
def running_tasks(x_agent_token: Optional[str] = Header(default=None)):
    require_token(x_agent_token)

    # Your pm module should provide one of these. Adjust here if needed:
    if hasattr(pm, "get_local_tasks"):
        return pm.get_local_tasks()
    if hasattr(pm, "list_local_tasks"):
        return {"tasks": pm.list_local_tasks()}
    raise HTTPException(status_code=500, detail="agent_process_manager missing get_local_tasks/list_local_tasks")

@app.post("/zip_folder")
def zip_folder(req: ZipFolderRequest, x_agent_token: Optional[str] = Header(default=None)):
    require_token(x_agent_token)

    src = Path(req.folder_path).expanduser()
    if not src.exists() or not src.is_dir():
        raise HTTPException(status_code=400, detail=f"Folder not found: {src}")

    if not _is_path_allowed(src):
        raise HTTPException(status_code=403, detail="Folder not allowed by SAFE_BASE_DIRS policy")

    zip_name = f"{src.name}_{uuid.uuid4().hex[:8]}.zip"
    zip_path = CACHE_DIR / zip_name

    # Clean old file if exists
    if zip_path.exists():
        zip_path.unlink(missing_ok=True)

    try:
        _zip_dir(src, zip_path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Zip failed: {e}")

    if _size_mb(zip_path) > MAX_ZIP_MB:
        zip_path.unlink(missing_ok=True)
        raise HTTPException(status_code=413, detail=f"Zip too large (> {MAX_ZIP_MB} MB)")

    return {"ok": True, "zip_path": str(zip_path), "zip_mb": round(_size_mb(zip_path), 2)}

@app.post("/upload_to_url")
def upload_to_url(req: UploadToUrlRequest, x_agent_token: Optional[str] = Header(default=None)):
    require_token(x_agent_token)

    file_path = Path(req.file_path).expanduser()
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=400, detail=f"File not found: {file_path}")

    try:
        with open(file_path, "rb") as f:
            r = requests.put(
                req.put_url,
                data=f,
                headers={"Content-Type": req.content_type},
                timeout=120,
            )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Upload failed: {e}")

    if r.status_code not in (200, 201, 204):
        raise HTTPException(status_code=502, detail=f"Upload failed: {r.status_code} {r.text[:500]}")

    return {"ok": True, "status_code": r.status_code}

@app.post("/download_from_url")
def download_from_url(req: DownloadFromUrlRequest, x_agent_token: Optional[str] = Header(default=None)):
    require_token(x_agent_token)

    filename = req.filename or f"download_{uuid.uuid4().hex[:8]}"
    out_path = DOWNLOADS_DIR / filename

    # prevent weird paths
    out_path = DOWNLOADS_DIR / Path(filename).name

    try:
        with requests.get(req.url, stream=True, timeout=120) as r:
            if r.status_code != 200:
                raise HTTPException(status_code=502, detail=f"Download failed: {r.status_code}")

            total = 0
            with open(out_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 256):
                    if not chunk:
                        continue
                    f.write(chunk)
                    total += len(chunk)
                    if total > MAX_DOWNLOAD_MB * 1024 * 1024:
                        f.close()
                        out_path.unlink(missing_ok=True)
                        raise HTTPException(status_code=413, detail=f"Download too large (> {MAX_DOWNLOAD_MB} MB)")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Download error: {e}")

    return {"ok": True, "saved_to": str(out_path), "size_mb": round(out_path.stat().st_size / (1024 * 1024), 2)}

# -----------------------------
# Autorun installer endpoints
# -----------------------------
@app.post("/install_autorun")
def install_autorun(req: InstallAutorunRequest, x_agent_token: Optional[str] = Header(default=None)):
    require_token(x_agent_token)

    python_exe = req.python_exe or shutil.which("python") or sys.executable  # type: ignore
    agent_main_path = str(Path(__file__).resolve())

    if not python_exe:
        raise HTTPException(status_code=500, detail="python_exe not found")

    return install_task(python_exe=python_exe, agent_main_path=agent_main_path)

@app.post("/uninstall_autorun")
def uninstall_autorun(x_agent_token: Optional[str] = Header(default=None)):
    require_token(x_agent_token)
    return uninstall_task()

@app.post("/run_autorun_now")
def run_autorun(x_agent_token: Optional[str] = Header(default=None)):
    require_token(x_agent_token)
    return run_task_now()

@app.get("/autorun_status")
def autorun_status(x_agent_token: Optional[str] = Header(default=None)):
    require_token(x_agent_token)
    return task_status()

# -----------------------------
# Run
# -----------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=AGENT_HOST, port=AGENT_PORT)
