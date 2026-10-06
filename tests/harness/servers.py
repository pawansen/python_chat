"""Start/stop the ORIGINAL Node service and the Python service side by side.

Both run against the same local MySQL test DB, moto DynamoDB and local Redis
(separate cache DB indexes). The Node process runs from an empty working
directory so ``dotenv`` never loads the real project ``.env``; the source
tree of the Node project is only read, never written (LCC OTA disabled).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

from tests.harness import env


def _wait(port: int, timeout: float = 120) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            httpx.get(f"http://127.0.0.1:{port}/livez", timeout=2, trust_env=False)
            return
        except httpx.HTTPError:
            time.sleep(0.5)
    raise RuntimeError(f"server on {port} did not start")


def start(kind: str) -> subprocess.Popen:
    run_dir = env.RUN_DIR / kind
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / ".env").write_text("# intentionally empty — the harness passes env vars\n", encoding="utf-8")
    process_env = {**os.environ, **env.server_env(kind), "NODE_CHAT_DIR": str(env.NODE_PROJECT), "PYTHONIOENCODING": "utf-8"}
    log = open(run_dir / "server.log", "wb")  # noqa: SIM115
    if kind == "node":
        # express.static('public') is CWD-relative; production runs from the project root.
        import shutil

        shutil.copytree(env.NODE_PROJECT / "public", run_dir / "public", dirs_exist_ok=True)
        preload = (env.HARNESS_DIR / "node_preload.mjs").as_uri()
        cmd = ["node", "--import", preload, str(env.NODE_PROJECT / "src" / "server.js")]
        port = env.NODE_PORT
    else:
        process_env["PYTHONPATH"] = str(env.ROOT)
        cmd = [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(env.PY_PORT),
               "--log-level", "warning", "--app-dir", str(env.ROOT)]
        port = env.PY_PORT
    proc = subprocess.Popen(cmd, cwd=run_dir, env=process_env, stdout=log, stderr=subprocess.STDOUT)
    try:
        _wait(port)
    except RuntimeError:
        proc.kill()
        raise RuntimeError(f"{kind} failed to start; see {run_dir / 'server.log'}") from None
    return proc


def stop(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()


def log_path(kind: str) -> Path:
    return env.RUN_DIR / kind / "server.log"
