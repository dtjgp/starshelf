"""Lifecycle tests for star_lists_ui.py run as a real process (no GitHub calls are made)."""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

UI = str(Path(__file__).resolve().parents[1] / "star_lists_ui.py")
ENV = {**os.environ, "GH_TOKEN": "dummy-token-for-tests"}
results = []


def check(name, cond, detail=""):
    results.append(cond)
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else f"\n      {detail}"), flush=True)


def start(workdir: Path, *extra: str) -> tuple[subprocess.Popen, str]:
    p = subprocess.Popen([sys.executable, "-u", UI, "--no-browser", "--dir", str(workdir), "--port", "19100", *extra],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=ENV)
    deadline, out = time.time() + 10, ""
    while time.time() < deadline:
        line = p.stdout.readline()
        out += line
        m = re.search(r"http://127\.0\.0\.1:\d+/#t=[\w-]+", line)
        if m:
            return p, m.group(0)
        if p.poll() is not None and not line:
            break
    return p, out


work = Path(tempfile.mkdtemp(prefix="sl_life_")) / "data"
lock = work / "ui_server.json"

p1, url1 = start(work)
check("first instance starts", url1.startswith("http://127.0.0.1:"), url1)
check("lock file written with mode 600", lock.exists() and oct(lock.stat().st_mode & 0o777) == "0o600",
      oct(lock.stat().st_mode) if lock.exists() else "missing")

p2, url2 = start(work)
p2.wait(timeout=5)
check("second instance reuses the first (same URL, exits 0)", url2 == url1 and p2.returncode == 0, f"{url2} rc={p2.returncode}")

p1.send_signal(signal.SIGTERM)
rest = p1.communicate(timeout=10)[0]
check("SIGTERM shuts down cleanly", p1.returncode == 0 and "已关闭" in rest, f"rc={p1.returncode} {rest!r}")
check("lock removed on exit", not lock.exists())

lock.write_text(json.dumps({"pid": 999999, "port": 1, "url": "http://127.0.0.1:1/#t=x"}))
p3, url3 = start(work)
check("stale lock is ignored", url3.startswith("http://127.0.0.1:") and url3 != "http://127.0.0.1:1/#t=x", url3)
p3.send_signal(signal.SIGTERM); p3.communicate(timeout=10)

# --exit-with-parent: launch through a shell that exits right away
script = (f"{sys.executable} -u {UI} --no-browser --exit-with-parent --dir {work} --port 19100 "
          f"> {work}/child.log 2>&1 & echo $! ; sleep 1")
child_pid = int(subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=ENV).stdout.split()[0])
alive = True
for _ in range(20):
    time.sleep(0.5)
    try:
        os.kill(child_pid, 0)
        # reap if it is our zombie (it is not our child, so this is just a liveness probe)
    except ProcessLookupError:
        alive = False
        break
log = (work / "child.log").read_text()
check("exits when the launching process is gone", not alive and "随之关闭" in log, log)
check("lock removed after parent exit", not lock.exists())

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
