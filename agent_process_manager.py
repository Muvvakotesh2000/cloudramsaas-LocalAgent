# agent_process_manager.py (LOCAL AGENT - PUBLIC SAFE)
# ✅ No AWS credentials required on local machine.
# ✅ Uses backend-issued pre-signed URLs for S3 PUT/GET.
# ✅ Backwards compatible with older agent_main.py calls (access_token/user_id optional).

import psutil
import requests
import os
import shutil
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import threading
import time
import xml.etree.ElementTree as ET
import subprocess
import logging
import win32gui
import win32con
import zipfile
import json
import tempfile
import sqlite3
from urllib.parse import urlparse, unquote
from pathlib import Path
from typing import Optional, Dict
from requests.exceptions import ReadTimeout, ConnectTimeout, ConnectionError

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("process_manager.log"), logging.StreamHandler()],
)
logger = logging.getLogger("cloudramsaas_localagent")


def get_local_tasks():
    try:
        target_tasks = ["notepad++.exe", "chrome.exe", "Code.exe"]
        tasks = []
        for p in psutil.process_iter(["pid", "name"]):
            name = (p.info.get("name") or "")
            if name in target_tasks:
                tasks.append({"pid": p.info["pid"], "name": name})
        return {"tasks": tasks}
    except Exception as e:
        logger.exception(f"Error fetching local tasks: {e}")
        return {"tasks": []}


def list_local_tasks():
    return get_local_tasks().get("tasks", [])


class ProcessManager:
    def __init__(self):
        # Buckets
        self.BUCKET_NAME = os.getenv("NOTEPAD_BUCKET_NAME", "notepadfiles")
        self.VSCODE_BUCKET = os.getenv("VSCODE_BUCKET_NAME", "cloudram-vscode")

        # Backend for presign
        self.backend_url = os.getenv(
            "BACKEND_URL",
            "https://cloudramsaas-backend.onrender.com",
        ).rstrip("/")

        self.sync_running = False
        appdata = os.environ.get("APPDATA", "")
        self.notepad_dir = os.path.join(appdata, "Notepad++")
        self.backup_dir = os.path.join(self.notepad_dir, "backup")

        base_dir = os.path.dirname(os.path.abspath(__file__))
        self.unsaved_temp_dir = os.path.join(base_dir, "unsaved_files")
        os.makedirs(self.unsaved_temp_dir, exist_ok=True)

        self.tracked_files = set()
        self.file_record_path = "notepad_file_paths.txt"

        self.vm_ip = None

        # Store auth context for background sync
        self._last_access_token: Optional[str] = None
        self._last_user_id: Optional[str] = None

        self.load_tracked_files()

    # ==================================================
    # ✅ Presigned URL helpers (Backend -> S3)
    # ==================================================
    def _auth_headers(self, access_token: str) -> Dict[str, str]:
        return {"Authorization": f"Bearer {access_token}"}

    def _require_auth_context(self, access_token: Optional[str], user_id: Optional[str]):
        access_token = access_token or self._last_access_token
        user_id = user_id or self._last_user_id
        if not access_token or not user_id:
            raise RuntimeError(
                "Missing access_token/user_id. Provide them from the browser, "
                "or ensure they were stored from a previous action."
            )
        return access_token, user_id

    def _presign_put(self, access_token: str, user_id: str, bucket: str, key: str, content_type: str):
        url = f"{self.backend_url}/s3/sign_put"
        payload = {
            "user_id": user_id,
            "bucket": bucket,
            "key": key,
            "content_type": content_type or "application/octet-stream",
        }
        r = requests.post(url, json=payload, headers=self._auth_headers(access_token), timeout=20)
        if r.status_code != 200:
            raise RuntimeError(f"presign PUT failed: {r.status_code} {r.text}")
        return r.json()["url"]

    def _presign_get(self, access_token: str, user_id: str, bucket: str, key: str):
        url = f"{self.backend_url}/s3/sign_get"
        payload = {"user_id": user_id, "bucket": bucket, "key": key}
        r = requests.post(url, json=payload, headers=self._auth_headers(access_token), timeout=20)
        if r.status_code != 200:
            raise RuntimeError(f"presign GET failed: {r.status_code} {r.text}")
        return r.json()["url"]

    def _upload_via_presigned_put(self, presigned_url: str, local_path: str, content_type: str):
        logger.info(f"Uploading via presigned PUT: {local_path}")
        with open(local_path, "rb") as f:
            put = requests.put(
                presigned_url,
                data=f,
                headers={"Content-Type": content_type or "application/octet-stream"},
                timeout=120,
            )
        if put.status_code not in (200, 201, 204):
            raise RuntimeError(f"PUT upload failed: {put.status_code} {put.text}")

    def _download_via_presigned_get(self, presigned_url: str, local_path: str):
        logger.info(f"Downloading via presigned GET -> {local_path}")
        r = requests.get(presigned_url, stream=True, timeout=120)
        if r.status_code != 200:
            raise RuntimeError(f"GET download failed: {r.status_code} {r.text}")
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        with open(local_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)

    def _upload_file_presigned(
        self,
        access_token: str,
        user_id: str,
        bucket: str,
        key: str,
        local_path: str,
        content_type: str,
    ):
        presigned = self._presign_put(access_token, user_id, bucket, key, content_type)
        self._upload_via_presigned_put(presigned, local_path, content_type)

    # ==================================================
    # VSCode CLI helper
    # ==================================================
    def _find_code_cli(self):
        r"""Returns a usable 'code' CLI command (code/cmd path) if available."""
        try:
            subprocess.check_output(["code", "--version"], stderr=subprocess.STDOUT, text=True, timeout=5)
            return ["code"]
        except Exception:
            pass

        candidates = [
            os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Programs\Microsoft VS Code\bin\code.cmd"),
            os.path.join(os.environ.get("ProgramFiles", ""), r"Microsoft VS Code\bin\code.cmd"),
            os.path.join(os.environ.get("ProgramFiles(x86)", ""), r"Microsoft VS Code\bin\code.cmd"),
        ]
        for c in candidates:
            if c and os.path.exists(c):
                return [c]
        return None

    # ==================================================
    # Zipping helpers
    # ==================================================
    def _zip_dir(self, folder_path: str, zip_path: str):
        logger.info(f"Zipping folder {folder_path} -> {zip_path}")
        base = os.path.basename(os.path.normpath(folder_path))  # keep real folder name
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for root, _, files in os.walk(folder_path):
                for f in files:
                    full = os.path.join(root, f)
                    rel = os.path.relpath(full, folder_path)
                    arcname = os.path.join(base, rel)  # include top folder
                    zf.write(full, arcname)

    def _zip_file(self, file_path: str, zip_path: str):
        logger.info(f"Zipping file {file_path} -> {zip_path}")
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.write(file_path, os.path.basename(file_path))

    # ==================================================
    # VSCode open path detection
    # ==================================================
    def _detect_vscode_open_path(self):
        """
        Returns (opened_path, kind) where kind is 'folder' or 'workspace'.
        Prefer:
        1) code --status
        2) state.vscdb
        """
        code_cli = self._find_code_cli()
        if code_cli:
            try:
                out = subprocess.check_output(code_cli + ["--status"], stderr=subprocess.STDOUT, text=True, timeout=5)
                for line in out.splitlines():
                    line_stripped = line.strip()

                    if line_stripped.lower().startswith("folder ("):
                        path = line_stripped.split(":", 1)[-1].strip()
                        if os.path.isdir(path):
                            logger.info(f"[VSCode detect] code --status folder: {path}")
                            return path, "folder"

                    if line_stripped.lower().startswith("workspace ("):
                        path = line_stripped.split(":", 1)[-1].strip()
                        if os.path.isfile(path) and path.lower().endswith(".code-workspace"):
                            logger.info(f"[VSCode detect] code --status workspace: {path}")
                            return path, "workspace"
            except Exception as e:
                logger.warning(f"[VSCode detect] code --status failed: {e}")

        try:
            appdata = os.environ.get("APPDATA", "")
            db_path = os.path.join(appdata, r"Code\User\globalStorage\state.vscdb")
            if not os.path.exists(db_path):
                logger.warning(f"[VSCode detect] state DB not found: {db_path}")
                return None, None

            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute("SELECT value FROM ItemTable WHERE key = ?", ("history.recentlyOpenedPathsList",))
            row = cur.fetchone()
            conn.close()

            if not row or not row[0]:
                return None, None

            payload = json.loads(row[0])
            entries = payload.get("entries", [])
            for ent in entries:
                uri = ent.get("folderUri") or ent.get("fileUri") or ent.get("workspace", {}).get("configURIPath")
                if not uri:
                    continue

                if isinstance(uri, str) and uri.startswith("file:"):
                    u = urlparse(uri)
                    path = unquote(u.path)

                    if len(path) >= 3 and path[0] == "/" and path[2] == ":":
                        path = path[1:]

                    if path.lower().endswith(".code-workspace") and os.path.isfile(path):
                        logger.info(f"[VSCode detect] state.vscdb workspace: {path}")
                        return path, "workspace"

                    if os.path.isdir(path):
                        logger.info(f"[VSCode detect] state.vscdb folder: {path}")
                        return path, "folder"
        except Exception as e:
            logger.warning(f"[VSCode detect] state.vscdb parse failed: {e}")

        return None, None

    def _collect_vscode_config_bundle(self):
        """
        Bundles VSCode user settings/keybindings/snippets and extensions list
        Returns (zip_path, meta)
        """
        appdata = os.environ.get("APPDATA")
        if not appdata:
            return None, {"warning": "APPDATA not found"}

        user_dir = os.path.join(appdata, "Code", "User")
        settings = os.path.join(user_dir, "settings.json")
        keybindings = os.path.join(user_dir, "keybindings.json")
        snippets_dir = os.path.join(user_dir, "snippets")

        tmpdir = tempfile.mkdtemp(prefix="cloudram_vscode_cfg_")
        staging = os.path.join(tmpdir, "vscode_user")
        os.makedirs(staging, exist_ok=True)

        meta = {"included": []}

        def copy_if_exists(src):
            if src and os.path.exists(src):
                dest = os.path.join(staging, os.path.basename(src))
                shutil.copy2(src, dest)
                meta["included"].append(src)

        copy_if_exists(settings)
        copy_if_exists(keybindings)

        if os.path.isdir(snippets_dir):
            dest_snips = os.path.join(staging, "snippets")
            shutil.copytree(snippets_dir, dest_snips, dirs_exist_ok=True)
            meta["included"].append(snippets_dir)

        ext_list_path = os.path.join(staging, "extensions.txt")
        try:
            out = subprocess.check_output(["code", "--list-extensions"], stderr=subprocess.STDOUT, text=True, timeout=10)
            with open(ext_list_path, "w", encoding="utf-8") as f:
                f.write(out)
            meta["included"].append("code --list-extensions")
        except Exception as e:
            meta["warning"] = f"Could not read extensions list: {e}"

        zip_path = os.path.join(tmpdir, "vscode_config.zip")
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for root, _, files in os.walk(staging):
                for f in files:
                    full = os.path.join(root, f)
                    rel = os.path.relpath(full, staging)
                    zf.write(full, rel)

        return zip_path, meta

    # ==================================================
    # ✅ VSCode migration (presigned URLs)
    # ==================================================
    def migrate_vscode_project(self, vm_ip: str, user_id: str, access_token: Optional[str] = None):
        """
        1) detect open folder/workspace
        2) zip project
        3) zip vscode config
        4) generate deps bundle (freeze + meta)
        5) upload all to S3 using backend presigned URLs
        6) close Code.exe locally
        7) call VM to download+extract+apply config+install deps+open
        """
        if access_token:
            self._last_access_token = access_token
        self._last_user_id = user_id

        access_token, user_id = self._require_auth_context(access_token, user_id)

        opened_path, kind = self._detect_vscode_open_path()
        if not opened_path:
            return False, None, "VSCode is running but I couldn't detect an open folder/workspace."

        project_name = os.path.basename(os.path.normpath(opened_path))

        tmpdir = tempfile.mkdtemp(prefix="cloudram_vscode_")
        proj_zip = os.path.join(tmpdir, f"{project_name}.zip")

        try:
            if kind == "workspace":
                with open(opened_path, "r", encoding="utf-8") as f:
                    ws = json.load(f)

                folders = ws.get("folders", [])
                if not folders:
                    self._zip_file(opened_path, proj_zip)
                else:
                    first = folders[0].get("path")
                    if not first:
                        self._zip_file(opened_path, proj_zip)
                    else:
                        base = os.path.dirname(opened_path)
                        abs_folder = os.path.abspath(os.path.join(base, first))
                        if os.path.isdir(abs_folder):
                            self._zip_dir(abs_folder, proj_zip)
                        else:
                            self._zip_file(opened_path, proj_zip)
            else:
                self._zip_dir(opened_path, proj_zip)
        except Exception as e:
            return False, opened_path, f"Failed to zip VSCode project: {e}"

        cfg_zip, _cfg_meta = self._collect_vscode_config_bundle()
        if not cfg_zip:
            return False, opened_path, "Failed to bundle VSCode config (APPDATA issue)."

        stamp = str(int(time.time()))
        proj_key = f"users/{user_id}/projects/{project_name}/{stamp}/{project_name}.zip"
        cfg_key = f"users/{user_id}/projects/{project_name}/{stamp}/vscode_config.zip"

        try:
            project_root = self._find_project_root_for_backend(opened_path, kind)
            freeze_path, meta_path = self._make_dep_bundle(project_root)
        except Exception as e:
            return False, opened_path, f"Failed to generate dependency bundle: {e}"

        dep_key_freeze = f"users/{user_id}/projects/{project_name}/{stamp}/deps_freeze.txt"
        dep_key_meta = f"users/{user_id}/projects/{project_name}/{stamp}/deps_hint.json"

        try:
            self._upload_file_presigned(access_token, user_id, self.VSCODE_BUCKET, proj_key, proj_zip, "application/zip")
            self._upload_file_presigned(access_token, user_id, self.VSCODE_BUCKET, cfg_key, cfg_zip, "application/zip")
            self._upload_file_presigned(access_token, user_id, self.VSCODE_BUCKET, dep_key_freeze, freeze_path, "text/plain")
            self._upload_file_presigned(access_token, user_id, self.VSCODE_BUCKET, dep_key_meta, meta_path, "application/json")
        except Exception as e:
            return False, opened_path, f"S3 upload via presigned URL failed: {e}"

        # Close VSCode locally
        try:
            subprocess.call(["taskkill", "/F", "/IM", "Code.exe"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            logger.warning(f"Could not taskkill Code.exe: {e}")

        
        # Tell VM to pull + open
        try:
            payload = {
                "user_id": user_id,
                "project_name": project_name,
                "project_s3_bucket": self.VSCODE_BUCKET,
                "project_s3_key": proj_key,
                "config_s3_bucket": self.VSCODE_BUCKET,
                "config_s3_key": cfg_key,
                "opened_path_kind": kind,
                "deps_s3_bucket": self.VSCODE_BUCKET,
                "deps_s3_key": dep_key_freeze,
                "deps_meta_s3_key": dep_key_meta,
            }

            r = requests.post(f"http://{vm_ip}:5000/setup_vscode", json=payload, timeout=60)
            if r.status_code != 200:
                return False, opened_path, f"VM setup_vscode failed: {r.status_code} {r.text}"

            job_id = (r.json() or {}).get("job_id")
            if not job_id:
                return False, opened_path, "VM did not return job_id."

            # ✅ timeout-tolerant polling
            deadline = time.time() + (60 * 10)  # 10 minutes
            last_status = None

            while time.time() < deadline:
                try:
                    s = requests.get(
                        f"http://{vm_ip}:5000/vscode_setup_status/{job_id}",
                        timeout=30,   # bump from 10
                    )

                    if s.status_code == 200:
                        j = s.json() or {}
                        last_status = j
                        st = j.get("status")

                        if st == "done":
                            return True, opened_path, None
                        if st == "error":
                            return False, opened_path, f"VM setup error: {j.get('message')}"

                    # if non-200 just keep polling
                except (ReadTimeout, ConnectTimeout, ConnectionError) as e:
                    # ✅ VM is busy or temporarily not responding — keep polling
                    logger.warning(f"VM status poll transient issue: {e}")

                time.sleep(5)

            # Timeout: return best info we have
            if last_status:
                return False, opened_path, f"Timed out waiting for VM. Last status: {last_status}"
            return False, opened_path, "Timed out waiting for VM to finish VSCode setup."

        except Exception as e:
            return False, opened_path, f"Could not contact VM: {e}"

    # ==================================================
    # Notepad tracking
    # ==================================================
    def load_tracked_files(self):
        if os.path.exists(self.file_record_path):
            with open(self.file_record_path, "r") as f:
                self.tracked_files = set(line.strip() for line in f)
                logger.info(f"Loaded {len(self.tracked_files)} tracked files from record")

    def force_notepad_session_save(self):
        try:
            def enum_windows_callback(hwnd, results):
                if "notepad++" in win32gui.GetWindowText(hwnd).lower():
                    results.append(hwnd)

            windows = []
            win32gui.EnumWindows(enum_windows_callback, windows)
            if not windows:
                logger.warning("No Notepad++ window found to force session save")
                return False

            hwnd = windows[0]
            logger.info(f"Found Notepad++ window handle: {hwnd}")

            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
            time.sleep(1)

            notepad_running = any(
                (proc.info.get("name") or "").lower() == "notepad++.exe"
                for proc in psutil.process_iter(["pid", "name"])
            )
            if not notepad_running:
                logger.info("Notepad++ closed after WM_CLOSE, restarting...")
                notepad_exe = r"C:\Program Files\Notepad++\notepad++.exe"
                if not os.path.exists(notepad_exe):
                    notepad_exe = r"C:\Program Files (x86)\Notepad++\notepad++.exe"
                subprocess.Popen([notepad_exe])
                time.sleep(3)

            logger.info("Forced Notepad++ session save")
            return True
        except Exception as e:
            logger.error(f"Failed to force Notepad++ session save: {e}")
            return False

    def get_current_open_files(self):
        open_files = []

        notepad_proc = None
        for proc in psutil.process_iter(["pid", "name"]):
            if (proc.info.get("name") or "").lower() == "notepad++.exe":
                notepad_proc = proc
                break

        if notepad_proc:
            try:
                all_files = notepad_proc.open_files()
                logger.info(f"All open files in Notepad++ via psutil: {[f.path for f in all_files]}")
                for file in all_files:
                    file_path = file.path
                    if (
                        file_path.lower().endswith((".txt", ".cpp", ".py", ".html"))
                        and "notepad++" not in file_path.lower()
                        and os.path.isfile(file_path)
                    ):
                        open_files.append(file_path)
                        logger.info(f"Found open file via psutil: {file_path}")
            except psutil.AccessDenied:
                logger.warning("Access denied while trying to get open files from Notepad++ process")
            except Exception as e:
                logger.error(f"Error getting open files via psutil: {e}")

        if not open_files:
            logger.info("No files found via psutil, falling back to session.xml")
            session_path = os.path.join(self.notepad_dir, "session.xml")
            if not os.path.exists(session_path):
                logger.error("session.xml not found.")
                return open_files

            try:
                tree = ET.parse(session_path)
                root = tree.getroot()
                for file_node in root.iter("File"):
                    file_path = file_node.get("filename")
                    if file_path and os.path.isfile(file_path):
                        open_files.append(file_path)
                        logger.info(f"Found open file in session.xml: {file_path}")
            except Exception as e:
                logger.error(f"Failed to parse session.xml: {e}")

        open_files = list(dict.fromkeys(open_files))
        logger.info(f"Final list of open files: {open_files}")
        return open_files

    def get_unsaved_backup_files(self):
        time.sleep(2)
        backups = []
        if os.path.exists(self.backup_dir):
            backup_files = os.listdir(self.backup_dir)
            logger.info(f"Backup files found in {self.backup_dir}: {backup_files}")
            for file in backup_files:
                full_path = os.path.join(self.backup_dir, file)
                if os.path.isfile(full_path):
                    dest = os.path.join(self.unsaved_temp_dir, file)
                    shutil.copy2(full_path, dest)
                    backups.append(dest)
                    logger.info(f"Backed up unsaved file: {file}")
        else:
            logger.warning(f"Backup directory {self.backup_dir} does not exist")
        return backups

    def _refresh_notepad_session(self, files_to_open, unsaved_files):
        try:
            logger.info("Terminating Notepad++ to refresh state...")
            subprocess.call(["taskkill", "/F", "/IM", "notepad++.exe"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(2)

            notepad_exe = r"C:\Program Files\Notepad++\notepad++.exe"
            if not os.path.exists(notepad_exe):
                notepad_exe = r"C:\Program Files (x86)\Notepad++\notepad++.exe"
            if not os.path.exists(notepad_exe):
                raise FileNotFoundError("Notepad++ executable not found.")

            all_files = list(dict.fromkeys(files_to_open + unsaved_files))
            command = [notepad_exe] + all_files
            logger.info(f"Restarting Notepad++ with updated files: {all_files}")
            subprocess.Popen(command)
            time.sleep(3)

            logger.info("Restart complete.")
            return True
        except Exception as e:
            logger.error(f"Error refreshing Notepad++ session: {e}")
            return False

    # ==================================================
    # ✅ Task migration (backwards compatible)
    # ==================================================
    def move_task_to_cloud(
        self,
        task_name,
        vm_ip,
        sync_state: bool = False,
        access_token: Optional[str] = None,
        user_id: Optional[str] = None,
    ):
        logger.info(f"move_task_to_cloud called for {task_name} to VM {vm_ip}")
        self.vm_ip = vm_ip

        # store context for background sync if provided
        if access_token:
            self._last_access_token = access_token
        if user_id:
            self._last_user_id = user_id

        task = next(
            (
                p
                for p in psutil.process_iter(["pid", "name"])
                if (p.info.get("name") or "").lower() == task_name.lower()
            ),
            None,
        )
        if not task:
            logger.error(f"Task {task_name} not found locally")
            return False

        pid = task.info["pid"]

        if task_name.lower() == "notepad++.exe" and sync_state:
            # must have token+user for presign uploads
            try:
                access_token, user_id = self._require_auth_context(access_token, user_id)
            except Exception as e:
                logger.error(f"Notepad sync requested but auth context missing: {e}")
                return False

            logger.info("Extracting Notepad++ session info...")
            self.force_notepad_session_save()

            files_to_track = self.get_current_open_files()
            logger.info(f"Files to track after get_current_open_files: {files_to_track}")

            unsaved_files = self.get_unsaved_backup_files()
            logger.info(f"Unsaved files detected: {unsaved_files}")

            logger.info("Refreshing Notepad++ session...")
            self._refresh_notepad_session(files_to_track, unsaved_files)

            self.tracked_files = set(files_to_track)

            for unsaved_file in unsaved_files:
                base_name = os.path.basename(unsaved_file)
                corresponding_file = None
                for tracked in files_to_track:
                    if base_name in tracked:
                        corresponding_file = tracked
                        break
                if corresponding_file:
                    self.tracked_files.add(corresponding_file)
                else:
                    docs_dir = os.path.join(
                        os.environ.get("USERPROFILE", os.path.expanduser("~")),
                        "Documents",
                        "NotepadSync",
                    )
                    os.makedirs(docs_dir, exist_ok=True)
                    new_file_path = os.path.join(docs_dir, base_name)
                    shutil.copy2(unsaved_file, new_file_path)
                    self.tracked_files.add(new_file_path)
                    logger.info(f"Added new unsaved file to tracked files: {new_file_path}")

            self._update_tracked_file_list(self.tracked_files)
            logger.info(f"Tracked files after update: {self.tracked_files}")

            logger.info("Uploading tracked files via presigned URLs...")
            self._upload_tracked_files_to_s3(access_token=access_token, user_id=user_id)

            self.start_notepad_auto_sync(vm_ip)

            logger.info("Force killing Notepad++ after refresh...")
            subprocess.call(["taskkill", "/F", "/IM", "notepad++.exe"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(1)

        else:
            try:
                logger.info(f"Terminating {task_name} (PID: {pid})...")
                proc = psutil.Process(pid)
                proc.terminate()
                proc.wait(timeout=5)
            except psutil.NoSuchProcess:
                logger.warning(f"{task_name} already terminated.")
            except Exception as e:
                logger.error(f"Error terminating {task_name}: {e}")
                return False

        # Start on VM
        try:
            logger.info(f"Sending POST to VM: http://{vm_ip}:5000/run_task with task={task_name}")
            response = requests.post(f"http://{vm_ip}:5000/run_task", json={"task": task_name}, timeout=30)
            logger.info(f"Response: {response.status_code} - {response.text}")
            return response.status_code == 200
        except Exception as e:
            logger.error(f"Could not contact VM: {e}")
            return False

    # ==================================================
    # Notepad upload via presigned URLs (user-scoped keys)
    # ==================================================
    def _update_tracked_file_list(self, current_files):
        previous_files = set()
        if os.path.exists(self.file_record_path):
            with open(self.file_record_path, "r") as f:
                previous_files = set(line.strip() for line in f)

        updated_files = previous_files.union(current_files)
        with open(self.file_record_path, "w") as f:
            for file in sorted(updated_files):
                f.write(file + "\n")

        logger.info(f"Updated tracked files list with {len(updated_files)} files")

    def _notepad_key(self, user_id: str, filename: str) -> str:
        safe_name = os.path.basename(filename)
        return f"users/{user_id}/notepad/{safe_name}"

    def _upload_tracked_files_to_s3(self, access_token: str, user_id: str):
        for file_path in self.tracked_files:
            if os.path.exists(file_path):
                try:
                    self.sync_specific_file(file_path, access_token=access_token, user_id=user_id)
                except Exception as e:
                    logger.error(f"Upload error: {e}")
            else:
                logger.warning(f"Tracked file not found, can't upload: {file_path}")

    def sync_specific_file(self, file_path: str, access_token: Optional[str] = None, user_id: Optional[str] = None):
        if not os.path.exists(file_path):
            logger.warning(f"Can't sync non-existent file: {file_path}")
            return

        try:
            access_token, user_id = self._require_auth_context(access_token, user_id)
        except Exception:
            logger.warning("No access_token/user_id available for background sync. Skipping upload.")
            return

        basename = os.path.basename(file_path)
        key = self._notepad_key(user_id, basename)

        try:
            presigned = self._presign_put(access_token, user_id, self.BUCKET_NAME, key, "application/octet-stream")
            self._upload_via_presigned_put(presigned, file_path, "application/octet-stream")
            logger.info(f"Synced file to s3://{self.BUCKET_NAME}/{key}")

            # Notify VM to sync this file if we have a VM IP
            if self.vm_ip:
                try:
                    response = requests.post(
                        f"http://{self.vm_ip}:5000/sync_notepad_files",
                        json={
                            "file": basename,          # legacy
                            "bucket": self.BUCKET_NAME,
                            "key": key,                # new
                            "user_id": user_id,        # new
                        },
                        timeout=15,
                    )
                    logger.info(f"VM notification response: {response.status_code}")
                except Exception as e:
                    logger.error(f"Failed to notify VM of file change: {e}")

        except Exception as e:
            logger.error(f"Error syncing file {file_path}: {e}")

    # ✅ Called by agent_main.py (/sync_notepad)
    def sync_notepad_files(self, vm_ip: Optional[str] = None, upload: bool = True, access_token: Optional[str] = None, user_id: Optional[str] = None):
        """
        Best-effort "sync now" for notepad tracked files (UPLOAD only for MVP).
        """
        if vm_ip:
            self.vm_ip = vm_ip
        if not upload:
            logger.info("sync_notepad_files(upload=False) ignored for MVP (no list/head without AWS creds).")
            return

        access_token, user_id = self._require_auth_context(access_token, user_id)

        if not self.tracked_files:
            logger.warning("No tracked Notepad++ files. Nothing to sync.")
            return

        self._upload_tracked_files_to_s3(access_token=access_token, user_id=user_id)
        logger.info("Notepad upload sync completed.")

    def start_notepad_auto_sync(self, vm_ip):
        if self.sync_running:
            logger.info("Auto-sync already running.")
            return

        self.vm_ip = vm_ip

        class NotepadFileEventHandler(FileSystemEventHandler):
            def __init__(self, manager):
                self.manager = manager
                self.last_modified = {}

            def on_modified(self, event):
                if event.is_directory:
                    return

                file_path = event.src_path

                is_tracked = False
                if file_path in self.manager.tracked_files:
                    is_tracked = True

                for tracked in self.manager.tracked_files:
                    if os.path.basename(file_path) == os.path.basename(tracked):
                        is_tracked = True
                        file_path = tracked

                if is_tracked:
                    current_time = time.time()
                    if file_path in self.last_modified and current_time - self.last_modified[file_path] < 2:
                        return

                    self.last_modified[file_path] = current_time
                    logger.info(f"Detected file save: {file_path}")
                    self.manager.sync_specific_file(file_path)

        def run_watcher():
            event_handler = NotepadFileEventHandler(self)
            observer = Observer()

            watched_dirs = set([self.notepad_dir])

            for file_path in self.tracked_files:
                parent_dir = os.path.dirname(file_path)
                if os.path.exists(parent_dir):
                    watched_dirs.add(parent_dir)

            for directory in watched_dirs:
                logger.info(f"Watching for changes in: {directory}")
                observer.schedule(event_handler, directory, recursive=True)

            observer.start()
            self.sync_running = True

            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                observer.stop()
            observer.join()

        thread = threading.Thread(target=run_watcher, daemon=True)
        thread.start()
        logger.info("File watcher thread started")

    # ==================================================
    # ✅ Save project from VM -> Local (presigned GET)
    # ==================================================
    def save_project_from_vm_to_local(
        self,
        vm_ip: str,
        user_id: str,
        project_name: str,
        local_base: str,
        access_token: Optional[str] = None,
    ):
        """
        Backwards compatible: access_token optional but required for download.
        """
        project_name = os.path.basename(project_name.strip().rstrip("\\/"))
        if not project_name:
            return False, "Invalid project_name"

        if access_token:
            self._last_access_token = access_token
        self._last_user_id = user_id

        try:
            access_token, user_id = self._require_auth_context(access_token, user_id)
        except Exception as e:
            return False, f"Missing auth for download: {e}"

        try:
            r = requests.post(
                f"http://{vm_ip}:5000/export_project",
                json={"user_id": user_id, "project_name": project_name},
                timeout=120,
            )
        except Exception as e:
            return False, f"VM export request failed: {e}"

        if r.status_code != 200:
            return False, f"VM export failed: {r.status_code} {r.text}"

        data = r.json()
        bucket = data["bucket"]
        key = data["export_key"]

        tmpdir = tempfile.mkdtemp(prefix="cloudram_export_")
        zip_path = os.path.join(tmpdir, f"{project_name}.zip")

        try:
            presigned_get = self._presign_get(access_token, user_id, bucket, key)
            self._download_via_presigned_get(presigned_get, zip_path)
        except Exception as e:
            return False, f"S3 download via presigned URL failed: {e}"

        target_dir = os.path.join(local_base, project_name)
        os.makedirs(target_dir, exist_ok=True)

        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                names = [n for n in zf.namelist() if n and not n.endswith("/")]
                has_top_folder = any(n.replace("\\", "/").startswith(project_name + "/") for n in names)

                if has_top_folder:
                    zf.extractall(local_base)
                else:
                    zf.extractall(target_dir)
        except Exception as e:
            return False, f"Extract failed: {e}"

        return True, f"Saved to {target_dir}"

    # ==================================================
    # Existing helpers (unchanged)
    # ==================================================
    def _find_project_root_for_backend(self, opened_path: str, kind: str):
        if kind == "folder":
            return os.path.abspath(opened_path)
        return os.path.abspath(os.path.dirname(opened_path))

    def _make_dep_bundle(self, project_dir: str):
        tmpdir = tempfile.mkdtemp(prefix="cloudram_deps_")
        deps_path = os.path.join(tmpdir, "deps.txt")
        meta_path = os.path.join(tmpdir, "deps_meta.json")

        project_dir = os.path.abspath(project_dir)
        meta = {"strategy": None, "project_dir": project_dir}

        req = os.path.join(project_dir, "requirements.txt")
        pyproject = os.path.join(project_dir, "pyproject.toml")

        venv_py = os.path.join(project_dir, ".venv", "Scripts", "python.exe")
        best_python = venv_py if os.path.exists(venv_py) else "python"

        if os.path.exists(req):
            shutil.copy2(req, deps_path)
            meta["strategy"] = "requirements.txt"
        elif os.path.exists(pyproject):
            try:
                out = subprocess.check_output(
                    ["poetry", "export", "-f", "requirements.txt", "--without-hashes"],
                    cwd=project_dir,
                    stderr=subprocess.STDOUT,
                    text=True,
                    timeout=60,
                )
                with open(deps_path, "w", encoding="utf-8") as f:
                    f.write(out)
                meta["strategy"] = "poetry_export"
            except Exception as e:
                meta["strategy"] = "pyproject_present_but_export_failed"
                meta["warning"] = str(e)

        if meta["strategy"] is None or meta["strategy"] == "pyproject_present_but_export_failed":
            try:
                out = subprocess.check_output(
                    [best_python, "-m", "pip", "freeze"],
                    cwd=project_dir,
                    stderr=subprocess.STDOUT,
                    text=True,
                    timeout=60,
                )
                with open(deps_path, "w", encoding="utf-8") as f:
                    f.write(out)
                meta["strategy"] = "pip_freeze"
                meta["python_used"] = best_python
            except Exception as e:
                meta["strategy"] = "failed"
                meta["error"] = str(e)
                meta["python_used"] = best_python

        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)

        return deps_path, meta_path
