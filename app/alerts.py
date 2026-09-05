"""User-defined alert rules.

Same rule schema as the strategy builder, but evaluated against screener rows.
Each rule has a cooldown, a severity, and optional actions (auto-watch the
symbol, POST to a webhook).
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any

from app.config import ROOT
from app.rules import context_from_row, evaluate_rule, validate_rule

log = logging.getLogger("alerts")

STORE_PATH = ROOT / "data" / "alert_rules.json"

SEVERITIES = ("info", "warn", "critical")


def normalize_alert(spec: dict[str, Any]) -> dict[str, Any]:
    out = dict(spec or {})
    out["id"] = str(out.get("id") or "al_" + uuid.uuid4().hex[:8])
    out["name"] = (str(out.get("name") or "Untitled alert")).strip()[:60]
    out["enabled"] = bool(out.get("enabled", True))
    out["rule"] = out.get("rule") or {"op": "all", "rules": []}
    out["symbols"] = [str(s).upper().replace("-", "/") for s in (out.get("symbols") or [])][:40]
    out["cooldown_sec"] = max(30.0, float(out.get("cooldown_sec", 300) or 300))
    sev = str(out.get("severity") or "info").lower()
    out["severity"] = sev if sev in SEVERITIES else "info"
    out["message"] = (str(out.get("message") or "")).strip()[:160]
    out["auto_watch"] = bool(out.get("auto_watch", False))
    out["webhook"] = (str(out.get("webhook") or "")).strip()[:300]
    out["created_ts"] = float(out.get("created_ts") or time.time())
    out["updated_ts"] = time.time()
    out["hits"] = int(out.get("hits") or 0)
    return out


def validate_alert(spec: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not str(spec.get("name") or "").strip():
        errors.append("name is required")
    rule = spec.get("rule")
    if not rule or not (rule.get("rules") if isinstance(rule, dict) else rule):
        errors.append("add at least one condition")
    errors.extend(validate_rule(rule))
    hook = str(spec.get("webhook") or "")
    if hook and not hook.startswith(("http://", "https://")):
        errors.append("webhook must be an http(s) URL")
    return errors


class AlertEngine:
    def __init__(self, path: Path = STORE_PATH, history: int = 200):
        self.path = path
        self.rules: dict[str, dict[str, Any]] = {}
        self.history: list[dict[str, Any]] = []
        self.max_history = history
        self._last_fire: dict[tuple[str, str], float] = {}
        self.load()

    # persistence ---------------------------------------------------------
    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text("utf-8"))
        except Exception as exc:  # pragma: no cover
            log.warning("alert store unreadable: %s", exc)
            return
        for spec in raw.get("rules", []):
            spec = normalize_alert(spec)
            self.rules[spec["id"]] = spec

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"version": 1, "rules": list(self.rules.values())}, indent=2), "utf-8"
        )
        tmp.replace(self.path)

    # crud ----------------------------------------------------------------
    def list(self) -> list[dict[str, Any]]:
        return sorted(self.rules.values(), key=lambda r: r.get("updated_ts", 0), reverse=True)

    def upsert(self, spec: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        errors = validate_alert(spec)
        if errors:
            return None, errors
        norm = normalize_alert(spec)
        prev = self.rules.get(norm["id"])
        if prev:
            norm["hits"] = prev.get("hits", 0)
            norm["created_ts"] = prev.get("created_ts", norm["created_ts"])
        self.rules[norm["id"]] = norm
        self.save()
        return norm, []

    def delete(self, rule_id: str) -> bool:
        if rule_id in self.rules:
            del self.rules[rule_id]
            self.save()
            return True
        return False

    def toggle(self, rule_id: str, enabled: bool | None = None) -> bool | None:
        rule = self.rules.get(rule_id)
        if not rule:
            return None
        rule["enabled"] = (not rule["enabled"]) if enabled is None else bool(enabled)
        self.save()
        return rule["enabled"]

    # evaluation ----------------------------------------------------------
    def evaluate(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Return newly triggered alerts (respecting per-symbol cooldowns)."""
        fired: list[dict[str, Any]] = []
        now = time.time()
        for rule in self.rules.values():
            if not rule.get("enabled"):
                continue
            scope = set(rule.get("symbols") or [])
            for row in rows:
                sym = row.get("symbol", "")
                if scope and sym not in scope:
                    continue
                key = (rule["id"], sym)
                if now - self._last_fire.get(key, 0.0) < rule["cooldown_sec"]:
                    continue
                try:
                    ok, trace = evaluate_rule(rule["rule"], context_from_row(row))
                except Exception as exc:  # pragma: no cover
                    log.debug("alert %s failed: %s", rule["id"], exc)
                    continue
                if not ok:
                    continue
                self._last_fire[key] = now
                rule["hits"] = int(rule.get("hits", 0)) + 1
                text = rule.get("message") or f"{sym} matched {rule['name']}"
                event = {
                    "ts": now,
                    "kind": "rule",
                    "rule_id": rule["id"],
                    "rule": rule["name"],
                    "severity": rule["severity"],
                    "symbol": sym,
                    "text": text.replace("{symbol}", sym)
                    .replace("{price}", f"{row.get('last', 0):g}")
                    .replace("{alpha}", f"{row.get('alpha', 0):g}"),
                    "trace": trace[:5],
                    "auto_watch": bool(rule.get("auto_watch")),
                    "webhook": rule.get("webhook") or "",
                    "row": {
                        k: row.get(k)
                        for k in ("last", "alpha", "quality", "rsi", "adx", "change_pct", "grade")
                    },
                }
                fired.append(event)
                self.history.insert(0, event)
        if fired:
            self.history = self.history[: self.max_history]
            self.save()
        return fired

    def recent(self, n: int = 40) -> list[dict[str, Any]]:
        return self.history[:n]


ALERT_TEMPLATES = [
    {
        "name": "Timeframes aligned",
        "severity": "warn",
        "message": "{symbol} timeframes aligned — MTF score {mtf_score}, agreement {mtf_agreement}%",
        "rule": {
            "op": "all",
            "rules": [
                {"left": "mtf_score", "cmp": ">", "right": 45},
                {"left": "mtf_agreement", "cmp": ">", "right": 70},
            ],
        },
    },
    {
        "name": "HTF trend, LTF oversold",
        "severity": "info",
        "message": "{symbol} 1h trend up with 15m RSI {rsi_15m} — pullback entry",
        "rule": {
            "op": "all",
            "rules": [
                {"left": "trend_1h", "cmp": "==", "right": "up"},
                {"left": "rsi_15m", "cmp": "<", "right": 35},
            ],
        },
    },
    {
        "name": "Overbought on every frame",
        "severity": "warn",
        "message": "{symbol} overbought on {mtf_overbought} timeframes — exhaustion risk",
        "rule": {"op": "all", "rules": [{"left": "mtf_overbought", "cmp": ">", "right": 1}]},
    },
    {
        "name": "High-probability forecast",
        "severity": "warn",
        "message": "{symbol} forecast {forecast_dir} — {prob_up}% up, {exp_move}% expected",
        "rule": {
            "op": "all",
            "rules": [
                {"left": "forecast_conf", "cmp": ">", "right": 60},
                {"left": "forecast_edge", "cmp": ">", "right": 0.35},
            ],
        },
    },
    {
        "name": "Alpha breakout",
        "severity": "warn",
        "message": "{symbol} alpha {alpha} breaking out",
        "rule": {
            "op": "all",
            "rules": [
                {"left": "alpha", "cmp": ">", "right": 72},
                {"left": "breakout", "cmp": "is_true"},
            ],
        },
    },
    {
        "name": "Volume shock",
        "severity": "info",
        "message": "{symbol} volume shock",
        "rule": {"op": "all", "rules": [{"left": "vol_z", "cmp": ">", "right": 3}]},
    },
    {
        "name": "Risk spike",
        "severity": "critical",
        "message": "{symbol} volatility spike — tighten stops",
        "rule": {
            "op": "all",
            "rules": [
                {"left": "atr_pct", "cmp": ">", "right": 1.5},
                {"left": "risk_score", "cmp": ">", "right": 70},
            ],
        },
    },
    {
        "name": "Grade A setup",
        "severity": "warn",
        "message": "{symbol} grade A confluence at {price}",
        "rule": {
            "op": "all",
            "rules": [
                {"left": "grade", "cmp": "==", "right": "A"},
                {"left": "signal_count", "cmp": ">=", "right": 7},
            ],
        },
    },
]
