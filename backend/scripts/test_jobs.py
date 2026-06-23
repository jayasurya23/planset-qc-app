"""Tests for the bounded analysis job queue (app/jobs.py).

Verifies the concurrency cap is honored, status transitions
(queued -> running -> done/error), queue positions, error handling, and that
job metadata (who/project/kind) is surfaced for the shared activity feed.

Usage (from backend/):  python scripts/test_jobs.py
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.jobs as J  # noqa: E402

_passed = 0
_failed = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {name}")
    else:
        _failed += 1
        print(f"  FAIL  {name}  {extra}")


def by_prefix(pfx: str) -> dict:
    return {j["id"]: j for j in J.list_jobs() if j["id"].startswith(pfx)}


# ── 1. Concurrency cap + queue positions ───────────────────────────────────
gate = threading.Event()
cur = {"n": 0}
peak = {"n": 0}
m = threading.Lock()


def blocking(i: int):
    def fn():
        with m:
            cur["n"] += 1
            peak["n"] = max(peak["n"], cur["n"])
        gate.wait(5)
        with m:
            cur["n"] -= 1
        return {"id": f"r-cap-{i}"}
    return fn


N = J.CONCURRENCY + 2
for i in range(N):
    J.submit(f"cap-{i}", "analyze", {"started_by": "tester"}, blocking(i))
time.sleep(0.4)
mine = by_prefix("cap-")
running = sum(1 for j in mine.values() if j["status"] == "running")
queued = sum(1 for j in mine.values() if j["status"] == "queued")
check("running never exceeds the cap while blocked", running <= J.CONCURRENCY, f"running={running}")
check("extras wait in the queue", queued == N - J.CONCURRENCY, f"queued={queued}")
qpos = sorted(j["queue_position"] for j in mine.values() if j["status"] == "queued")
check("queued jobs get 1-based positions", qpos == list(range(1, len(qpos) + 1)), str(qpos))
gate.set()
time.sleep(0.6)
mine = by_prefix("cap-")
check("all jobs finish after release", len(mine) == N and all(j["status"] == "done" for j in mine.values()))
check("peak concurrency never exceeded the cap", peak["n"] <= J.CONCURRENCY, f"peak={peak['n']}")


# ── 2. Status transitions + run_id on success ──────────────────────────────
ev = threading.Event()
J.submit("tx-ok", "analyze", {"started_by": "t"}, lambda: (ev.wait(5), {"id": "r-ok"})[1])
time.sleep(0.2)
check("running before completion", by_prefix("tx-ok")["tx-ok"]["status"] == "running")
ev.set()
time.sleep(0.3)
j = by_prefix("tx-ok")["tx-ok"]
check("done carries the saved run_id", j["status"] == "done" and j["run_id"] == "r-ok", str(j))
check("done reports 100%", j["pct"] == 100)


# ── 3. Error paths ─────────────────────────────────────────────────────────
def boom():
    raise RuntimeError("kaboom")


J.submit("err-raise", "analyze", {"started_by": "t"}, boom)
J.submit("err-ret", "analyze", {"started_by": "t"}, lambda: {"error": "bad pdf"})
time.sleep(0.4)
jr = by_prefix("err-raise")["err-raise"]
check("a raised exception -> error status", jr["status"] == "error")
check("the exception message is captured", "kaboom" in (jr["detail"] or jr.get("error") or ""), str(jr))
check("an {'error': ...} return -> error status", by_prefix("err-ret")["err-ret"]["status"] == "error")


# ── 4. Metadata for the shared feed ────────────────────────────────────────
J.submit(
    "meta-1", "reanalyze",
    {"project_name": "Trigo", "run_name": "v2", "started_by": "M. Puri", "created_by": "mp@x.com"},
    lambda: {"id": "r-meta"},
)
time.sleep(0.3)
j = by_prefix("meta-1")["meta-1"]
check("kind / project / who are surfaced",
      j["kind"] == "reanalyze" and j["project_name"] == "Trigo" and j["started_by"] == "M. Puri",
      str(j))


print(f"\n{_passed} passed, {_failed} failed")
sys.exit(1 if _failed else 0)
