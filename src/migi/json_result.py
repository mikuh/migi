from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import time
from typing import Literal
from typing import Any

from migi import __version__


@dataclass
class ResultBuilder:
    command: str
    started_at: float

    @classmethod
    def start(cls, command: str) -> "ResultBuilder":
        return cls(command=command, started_at=time.perf_counter())

    def _meta(self) -> dict[str, Any]:
        return {
            "duration_ms": round((time.perf_counter() - self.started_at) * 1000, 2),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "version": __version__,
        }

    def ok(self, code: str, message: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "ok": True,
            "command": self.command,
            "code": code,
            "message": message,
            "data": data or {},
            "error": None,
            "meta": self._meta(),
        }

    def fail(
        self,
        code: str,
        message: str,
        error_type: str,
        detail: str,
        hint: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "ok": False,
            "command": self.command,
            "code": code,
            "message": message,
            "data": data or {},
            "error": {
                "type": error_type,
                "detail": detail,
                "hint": hint,
            },
            "meta": self._meta(),
        }


def _to_compact(payload: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {
        "ok": bool(payload.get("ok")),
        "cmd": payload.get("command"),
        "code": payload.get("code"),
    }
    if compact["ok"]:
        compact["data"] = payload.get("data", {})
    else:
        compact["error"] = payload.get("error")
        data = payload.get("data")
        if data:
            compact["data"] = data
    return compact


def emit_json(payload: dict[str, Any], mode: Literal["compact", "full"] = "compact") -> None:
    if mode == "full":
        output = payload
    else:
        output = _to_compact(payload)
    print(json.dumps(output, ensure_ascii=False))

