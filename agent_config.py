# local_agent/agent_config.py
import os
from pathlib import Path

# -----------------------------
# Network / Security
# -----------------------------
AGENT_HOST = os.getenv("CLOUDRAM_AGENT_HOST", "127.0.0.1")
AGENT_PORT = int(os.getenv("CLOUDRAM_AGENT_PORT", "7071"))

# Optional auth token (recommended)
# If set, agent expects header: X-AGENT-TOKEN: <token>
AGENT_TOKEN = os.getenv("CLOUDRAM_AGENT_TOKEN", "").strip()

# Allowed origins (browser UI) - keep tight
ALLOWED_ORIGINS = [
    o.strip()
    for o in os.getenv(
        "CLOUDRAM_AGENT_ALLOWED_ORIGINS",
        "http://localhost:5000,http://127.0.0.1:5000,https://cloudramsaas-frontend.onrender.com",
    ).split(",")
    if o.strip()
]

# -----------------------------
# Render Backend (for presigned URLs)
# -----------------------------
# This is your PUBLIC Render backend URL, e.g.:
# https://cloudramsaas-backend.onrender.com
BACKEND_BASE_URL = os.getenv("CLOUDRAM_BACKEND_URL", "").rstrip("/")

# Optional: if your backend needs an API key header (NOT required in your current backend/main.py)
BACKEND_API_KEY = os.getenv("CLOUDRAM_BACKEND_API_KEY", "").strip()
BACKEND_API_KEY_HEADER = os.getenv("CLOUDRAM_BACKEND_API_KEY_HEADER", "X-APP-API-KEY").strip()

# -----------------------------
# S3 Buckets (logical names only; actual access via presigned URLs)
# -----------------------------
NOTEPAD_BUCKET = os.getenv("CLOUDRAM_NOTEPAD_BUCKET", "notepadfiles").strip()
VSCODE_BUCKET = os.getenv("CLOUDRAM_VSCODE_BUCKET", "cloudram-vscode").strip()

# Key policy: backend enforces users/<user_id>/...
# Keep this consistent with backend/main.py _require_user_scoped_key()
S3_USER_PREFIX_TEMPLATE = os.getenv("CLOUDRAM_S3_USER_PREFIX_TEMPLATE", "users/{user_id}/").strip()

# -----------------------------
# Storage / Logs
# -----------------------------
APPDATA = os.environ.get("LOCALAPPDATA") or str(Path.home())
BASE_DIR = Path(os.getenv("CLOUDRAM_AGENT_DATA_DIR", str(Path(APPDATA) / "CloudRAMSAgent")))

LOG_DIR = BASE_DIR / "logs"
CACHE_DIR = BASE_DIR / "cache"
DOWNLOADS_DIR = BASE_DIR / "downloads"

for d in (LOG_DIR, CACHE_DIR, DOWNLOADS_DIR):
    d.mkdir(parents=True, exist_ok=True)

# -----------------------------
# Feature flags / limits
# -----------------------------
MAX_ZIP_MB = int(os.getenv("CLOUDRAM_AGENT_MAX_ZIP_MB", "250"))       # safety
MAX_DOWNLOAD_MB = int(os.getenv("CLOUDRAM_AGENT_MAX_DOWNLOAD_MB", "500"))  # safety

# Upload/read timeouts (agent -> backend, agent -> S3 presigned PUT, agent -> VM)
HTTP_TIMEOUT_SECONDS = int(os.getenv("CLOUDRAM_AGENT_HTTP_TIMEOUT_SECONDS", "120"))
VM_HTTP_TIMEOUT_SECONDS = int(os.getenv("CLOUDRAM_AGENT_VM_HTTP_TIMEOUT_SECONDS", "30"))

# Whitelist for folder zipping (optional):
# if empty -> allow any path, but strongly recommended to keep a safe base.
# Example: E:\Kotesh\Projects
SAFE_BASE_DIRS = [
    p.strip()
    for p in os.getenv("CLOUDRAM_AGENT_SAFE_BASE_DIRS", "").split(",")
    if p.strip()
]
