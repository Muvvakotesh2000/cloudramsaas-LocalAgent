# agent_installer.py
import os
import subprocess
from pathlib import Path

TASK_NAME = os.getenv("CLOUDRAM_AGENT_TASK_NAME", "CloudRAMS-LocalAgent")

def _run(cmd: list[str]) -> tuple[int, str, str]:
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr

def install_task(python_exe: str, agent_main_path: str) -> dict:
    """
    Install agent to run at user logon using Windows Task Scheduler.
    Does NOT require admin if the user can create scheduled tasks.
    """
    python_exe = str(Path(python_exe))
    agent_main_path = str(Path(agent_main_path))

    # Run hidden (no console window) using pythonw if available
    pythonw = python_exe.replace("python.exe", "pythonw.exe")
    exe = pythonw if os.path.exists(pythonw) else python_exe

    # /RL LIMITED avoids admin requirement
    cmd = [
        "schtasks",
        "/Create",
        "/F",
        "/TN",
        TASK_NAME,
        "/SC",
        "ONLOGON",
        "/RL",
        "LIMITED",
        "/TR",
        f'"{exe}" "{agent_main_path}"',
    ]

    code, out, err = _run(cmd)
    ok = (code == 0)
    return {"ok": ok, "task": TASK_NAME, "stdout": out.strip(), "stderr": err.strip()}

def uninstall_task() -> dict:
    cmd = ["schtasks", "/Delete", "/F", "/TN", TASK_NAME]
    code, out, err = _run(cmd)
    ok = (code == 0)
    return {"ok": ok, "task": TASK_NAME, "stdout": out.strip(), "stderr": err.strip()}

def run_task_now() -> dict:
    cmd = ["schtasks", "/Run", "/TN", TASK_NAME]
    code, out, err = _run(cmd)
    ok = (code == 0)
    return {"ok": ok, "task": TASK_NAME, "stdout": out.strip(), "stderr": err.strip()}

def task_status() -> dict:
    cmd = ["schtasks", "/Query", "/TN", TASK_NAME, "/V", "/FO", "LIST"]
    code, out, err = _run(cmd)
    ok = (code == 0)
    return {"ok": ok, "task": TASK_NAME, "stdout": out.strip(), "stderr": err.strip()}
