# agent_config.py
import os
from pathlib import Path

# -----------------------------
# Network / Security
# -----------------------------
AGENT_HOST = os.getenv("CLOUDRAM_AGENT_HOST", "127.0.0.1")
AGENT_PORT = int(os.getenv("CLOUDRAM_AGENT_PORT", "7071"))

# Optional auth token (recommended for public usage)
# If set, agent expects header: X-AGENT-TOKEN: <token>
AGENT_TOKEN = os.getenv("CLOUDRAM_AGENT_TOKEN", "").strip()

# Allowed origins (browser UI) - keep tight
# Your Render frontend domain + localhost for dev
ALLOWED_ORIGINS = [
    o.strip()
    for o in os.getenv(
        "CLOUDRAM_AGENT_ALLOWED_ORIGINS",
        "http://localhost:5000,http://127.0.0.1:5000,https://cloudramsaas-frontend.onrender.com",
    ).split(",")
    if o.strip()
]

# -----------------------------
# Storage / Logs
# -----------------------------
APPDATA = os.environ.get("LOCALAPPDATA") or str(Path.home())
BASE_DIR = Path(os.getenv("CLOUDRAM_AGENT_DATA_DIR", Path(APPDATA) / "CloudRAMSAgent"))

LOG_DIR = BASE_DIR / "logs"
CACHE_DIR = BASE_DIR / "cache"
DOWNLOADS_DIR = BASE_DIR / "downloads"

for d in (LOG_DIR, CACHE_DIR, DOWNLOADS_DIR):
    d.mkdir(parents=True, exist_ok=True)

# -----------------------------
# Feature flags / limits
# -----------------------------
MAX_ZIP_MB = int(os.getenv("CLOUDRAM_AGENT_MAX_ZIP_MB", "250"))  # safety
MAX_DOWNLOAD_MB = int(os.getenv("CLOUDRAM_AGENT_MAX_DOWNLOAD_MB", "500"))  # safety

# Whitelist for folder zipping (optional):
# if empty -> allow any path, but strongly recommended to keep a safe base.
# Example: E:\Kotesh\Projects
SAFE_BASE_DIRS = [
    p.strip()
    for p in os.getenv("CLOUDRAM_AGENT_SAFE_BASE_DIRS", "").split(",")
    if p.strip()
]
