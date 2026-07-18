#!/usr/bin/env python3
"""Long-lived VPS queue consumer for dashboard refresh jobs."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from radar_control_client import ControlError, claim


API = os.environ.get(
    "RADAR_CONTROL_API",
    "https://sonardeals-radar-control.krtabd120.workers.dev",
)
TOKEN = Path(os.environ.get("RADAR_CONTROL_TOKEN", "/etc/sonardeals-radar/internal-token"))
STATE = Path(os.environ.get("RADAR_STATE_DIR", "/var/lib/sonardeals-radar"))
REFRESH = Path(os.environ.get("RADAR_REFRESH_SCRIPT", "/opt/sonardeals-radar/radar_refresh.sh"))
INTERVAL = max(5, int(os.environ.get("RADAR_POLL_SECONDS", "15")))
STOP = False


def log(message: str) -> None:
    stamp = datetime.now(UTC).replace(microsecond=0).isoformat()
    print(f"{stamp} {message}", flush=True)


def stop(_signal: int, _frame: object) -> None:
    global STOP
    STOP = True


def write_job(path: Path, job: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(job, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def run_job(job: dict[str, Any]) -> int:
    job_id = str(job.get("job_id") or "").strip()
    mode = str(job.get("mode") or "smart")
    if not job_id or mode not in {"smart", "full"}:
        raise ValueError("invalid radar job")
    jobs = STATE / "jobs"
    running = jobs / f"{job_id}.running.json"
    done = jobs / f"{job_id}.done.json"
    if done.exists():
        log(f"job {job_id} was already completed")
        return 0
    write_job(running, job)
    log(f"starting {mode} job {job_id}")
    result = subprocess.run([str(REFRESH), mode, job_id], check=False)
    destination = done if result.returncode == 0 else jobs / f"{job_id}.failed.json"
    write_job(destination, {**job, "returncode": result.returncode, "finished_at": datetime.now(UTC).isoformat()})
    running.unlink(missing_ok=True)
    log(f"finished job {job_id} rc={result.returncode}")
    return result.returncode


def recover() -> None:
    for path in sorted((STATE / "jobs").glob("*.running.json")):
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
            log(f"recovering interrupted job {job.get('job_id')}")
            run_job(job)
        except Exception as error:
            log(f"recovery failed for {path.name}: {error}")


def prune() -> None:
    completed = sorted(
        [*(STATE / "jobs").glob("*.done.json"), *(STATE / "jobs").glob("*.failed.json")],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in completed[200:]:
        path.unlink(missing_ok=True)


def main() -> int:
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    (STATE / "jobs").mkdir(parents=True, exist_ok=True)
    recover()
    while not STOP:
        try:
            job = claim(API, TOKEN)
            if job:
                run_job(job)
                prune()
                continue
        except ControlError as error:
            log(str(error))
        except Exception as error:
            log(f"poller error: {error}")
        for _ in range(INTERVAL):
            if STOP:
                break
            time.sleep(1)
    log("poller stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
