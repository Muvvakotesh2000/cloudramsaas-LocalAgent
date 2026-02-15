# CloudRAMS Local Agent (Windows)

This agent runs on the user's computer and exposes localhost-only endpoints for:
- listing local processes
- zipping folders
- uploading to presigned URLs
- downloading files to local disk
- installing an autorun task (Task Scheduler)

## 1) Install
```bash
cd LOCAL_AGENT
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
