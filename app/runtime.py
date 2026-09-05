"""24/7 supervision: keep the bot alive, and keep it honest while it is.

The trading loop already runs forever; this module is what makes "forever"
survivable without a human watching:

* **heartbeat + stall watchdog** — if the loop stops ticking (a wedged await, a
  feed deadlock) the supervisor restarts it instead of silently doing nothing,
* **daily roll** — resets the day's loss budget and trade counter at a
  configured UTC hour,
* **auto-resume** — optionally clears a risk halt after a cooling-off period,
* **maintenance window** — an hour range where the bot flattens/pauses (venue
  maintenance, funding roll, your sleep schedule),
* **uptime accounting** — so you can see it really has been running.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("runtime")

DEFAULTS: dict[str, Any] = {
    "always_on": True,
    "stall_timeout_sec": 90.0,
    "auto_restart_loop": True,
    "daily_reset_hour_utc": 0,
    "auto_resume_halt": False,
    "auto_resume_after_min": 60.0,
    "maintenance_enabled": False,
    "maintenance_start_hour_utc": 3,
    "maintenance_end_hour_utc": 4,
    "flatten_in_maintenance": False,
    "heartbeat_sec": 30.0,
}


class Supervisor:
    """Watchdog around the robot loop."""

    def __init__(self, robot: Any, path: Path):
        self.robot = robot
        self.path = path
        self.cfg: dict[str, Any] = dict(DEFAULTS)
        self.started = time.time()
        self.restarts = 0
        self.last_heartbeat = 0.0
        self.last_daily_reset = ""
        self.events: list[dict[str, Any]] = []
        self.in_maintenance = False
        self._task: asyncio.Task | None = None
        self.load()

    # ------------------------------------------------------------------ io
    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text("utf-8"))
        except Exception:
            return
        if isinstance(raw, dict):
            self.cfg.update({k: v for k, v in raw.items() if k in DEFAULTS})

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.cfg, indent=2), "utf-8")
            os.replace(tmp, self.path)
        except Exception as exc:  # pragma: no cover - disk only
            log.warning("could not persist runtime config: %s", exc)

    def configure(self, patch: dict[str, Any]) -> dict[str, Any]:
        for key, value in (patch or {}).items():
            if key not in DEFAULTS:
                continue
            default = DEFAULTS[key]
            try:
                if isinstance(default, bool):
                    self.cfg[key] = bool(value)
                elif isinstance(default, int) and not isinstance(default, bool):
                    self.cfg[key] = int(value) % 24 if "hour" in key else int(value)
                else:
                    self.cfg[key] = float(value)
            except (TypeError, ValueError):
                continue
        self.save()
        return dict(self.cfg)

    # ------------------------------------------------------------- events
    def note(self, kind: str, detail: str) -> None:
        self.events.insert(0, {"ts": time.time(), "kind": kind, "detail": detail})
        self.events = self.events[:60]
        log.info("supervisor: %s — %s", kind, detail)

    # -------------------------------------------------------------- checks
    def _hour(self) -> int:
        return int(time.gmtime().tm_hour)

    def maintenance_now(self) -> bool:
        if not self.cfg["maintenance_enabled"]:
            return False
        start = int(self.cfg["maintenance_start_hour_utc"]) % 24
        end = int(self.cfg["maintenance_end_hour_utc"]) % 24
        hour = self._hour()
        return start <= hour < end if start <= end else (hour >= start or hour < end)

    async def tick(self) -> dict[str, Any]:
        """One supervision pass. Returns what it did (used by the tests)."""
        did: dict[str, Any] = {}
        robot = self.robot
        now = time.time()

        # 1. stall watchdog
        age = now - float(getattr(robot, "last_loop", 0) or 0)
        running = bool(getattr(robot, "running", False))
        if running and age > float(self.cfg["stall_timeout_sec"]):
            did["stalled_sec"] = round(age, 1)
            if self.cfg["auto_restart_loop"]:
                self.restarts += 1
                self.note("restart", f"loop stalled for {age:.0f}s — restarting")
                try:
                    await robot.stop()
                except Exception as exc:
                    log.warning("supervisor stop failed: %s", exc)
                try:
                    await robot.start()
                    did["restarted"] = True
                except Exception as exc:
                    log.error("supervisor restart failed: %s", exc)
                    did["restart_failed"] = str(exc)

        # 2. daily roll
        stamp = time.strftime("%Y-%m-%d", time.gmtime())
        if stamp != self.last_daily_reset and self._hour() == int(self.cfg["daily_reset_hour_utc"]):
            self.last_daily_reset = stamp
            try:
                robot.risk.reset_day(robot.mark_equity)
                if hasattr(robot, "edge"):
                    robot.edge.trades_today = 0
                    robot.edge.consecutive_losses = 0
                self.note("daily-reset", f"loss budget and counters reset for {stamp}")
                did["daily_reset"] = stamp
            except Exception as exc:
                log.warning("daily reset failed: %s", exc)

        # 3. auto-resume after a halt
        if self.cfg["auto_resume_halt"] and getattr(robot.risk, "halted", False):
            since = now - float(getattr(robot.risk, "last_loss_ts", 0) or 0)
            if since > float(self.cfg["auto_resume_after_min"]) * 60:
                robot.risk.halted = False
                robot.risk.halt_reason = ""
                robot.risk.reset_day(robot.mark_equity)
                self.note("auto-resume", f"halt cleared after {since / 60:.0f} min")
                did["resumed"] = True

        # 4. maintenance window
        maint = self.maintenance_now()
        if maint != self.in_maintenance:
            self.in_maintenance = maint
            if maint:
                robot.paused = True
                self.note("maintenance", "entering maintenance window — new entries paused")
                if self.cfg["flatten_in_maintenance"]:
                    try:
                        await robot.flatten()
                        did["flattened"] = True
                    except Exception as exc:
                        log.warning("maintenance flatten failed: %s", exc)
            else:
                robot.paused = False
                self.note("maintenance", "maintenance window over — trading resumed")
            did["maintenance"] = maint

        self.last_heartbeat = now
        return did

    # --------------------------------------------------------------- loop
    async def run(self) -> None:
        self.note("start", "supervisor online")
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:  # pragma: no cover
                raise
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("supervisor tick failed: %s", exc)
            await asyncio.sleep(max(5.0, float(self.cfg["heartbeat_sec"])))

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:  # pragma: no cover - shutdown only
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    # -------------------------------------------------------------- views
    def status(self) -> dict[str, Any]:
        now = time.time()
        up = now - self.started
        loop_age = now - float(getattr(self.robot, "last_loop", 0) or 0)
        return {
            "cfg": dict(self.cfg),
            "defaults": dict(DEFAULTS),
            "uptime_sec": round(up, 1),
            "uptime_human": _human(up),
            "started": self.started,
            "restarts": self.restarts,
            "loop_age_sec": round(loop_age, 1),
            "loop_healthy": loop_age < float(self.cfg["stall_timeout_sec"]),
            "loops": int(getattr(self.robot, "loops", 0) or 0),
            "running": bool(getattr(self.robot, "running", False)),
            "paused": bool(getattr(self.robot, "paused", False)),
            "halted": bool(getattr(self.robot.risk, "halted", False)),
            "halt_reason": getattr(self.robot.risk, "halt_reason", ""),
            "in_maintenance": self.in_maintenance,
            "next_daily_reset_hour_utc": int(self.cfg["daily_reset_hour_utc"]),
            "heartbeat": self.last_heartbeat,
            "events": self.events[:20],
        }


def _human(seconds: float) -> str:
    seconds = int(max(0, seconds))
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return f"{d}d {h}h {m}m"
    if h:
        return f"{h}h {m}m"
    return f"{m}m {s}s"
