from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Callable

from fanfictl.config import Settings
from fanfictl.keystore import RuntimeAPIKey
from fanfictl.storage import atomic_write_text


class DailyQuotaExceeded(RuntimeError):
    pass


@dataclass
class KeyQuotaSnapshot:
    key_id: str
    source: str
    is_default: bool
    minute_used: int
    minute_limit: int
    daily_used: int
    daily_limit: int
    minute_remaining: int
    daily_remaining: int
    last_error: str | None
    available: bool


@dataclass
class QuotaSnapshot:
    minute_used: int
    minute_limit: int
    daily_used: int
    daily_limit: int
    minute_remaining: int
    daily_remaining: int
    reset_at: str
    last_error: str | None
    available: bool
    keys: list[KeyQuotaSnapshot]


class QuotaTracker:
    def __init__(
        self,
        settings: Settings,
        keys: list[RuntimeAPIKey],
        *,
        now_func: Callable[[], float] | None = None,
        sleep_func: Callable[[float], None] | None = None,
    ) -> None:
        self.settings = settings
        self.keys = keys
        self.path = settings.output_dir / ".quota.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._now = now_func or time.time
        self._sleep = sleep_func or time.sleep

    def acquire_request_slot(self) -> RuntimeAPIKey:
        while True:
            with self._lock:
                state = self._normalize_state(self._load_state())
                now = self._now()
                wait_seconds: float | None = None

                for key in self.keys:
                    key_state = state["keys"].setdefault(
                        key.id, self._empty_key_state()
                    )
                    key_state["request_timestamps"] = [
                        ts for ts in key_state["request_timestamps"] if now - ts < 60
                    ]

                    rpd, rpm = self.settings.rpd_limit, self.settings.rpm_limit
                    if rpd and key_state["daily_count"] >= rpd:
                        continue

                    cooldown = key_state.get("cooldown_until", 0) - now
                    if cooldown <= 0 and (
                        not rpm or len(key_state["request_timestamps"]) < rpm
                    ):
                        key_state["request_timestamps"].append(now)
                        key_state["daily_count"] += 1
                        key_state["last_error"] = None
                        state["last_error"] = None
                        self._save_state(state)
                        return key

                    rpm_wait = (
                        key_state["request_timestamps"][0] + 60 - now
                        if rpm and len(key_state["request_timestamps"]) >= rpm
                        else 0
                    )
                    candidate_wait = max(cooldown, rpm_wait, 0.25)
                    wait_seconds = (
                        candidate_wait
                        if wait_seconds is None
                        else min(wait_seconds, candidate_wait)
                    )

                self._save_state(state)

            if wait_seconds is None:
                raise DailyQuotaExceeded(
                    f"Daily request limit reached across all keys ({self.settings.rpd_limit} per key)."
                )
            self._sleep(wait_seconds)

    def record_quota_error(self, key_id: str, message: str) -> None:
        """Mark the key as rate-limited by the API: unusable for the next minute."""
        with self._lock:
            state = self._normalize_state(self._load_state())
            key_state = state["keys"].setdefault(key_id, self._empty_key_state())
            key_state["cooldown_until"] = self._now() + 60
            key_state["last_error"] = message
            state["last_error"] = message
            self._save_state(state)

    def snapshot(self) -> QuotaSnapshot:
        with self._lock:
            state = self._normalize_state(self._load_state())
            now = self._now()
            key_snapshots: list[KeyQuotaSnapshot] = []

            for key in self.keys:
                key_state = state["keys"].setdefault(key.id, self._empty_key_state())
                minute_used = len(
                    [ts for ts in key_state["request_timestamps"] if now - ts < 60]
                )
                daily_used = int(key_state["daily_count"])
                key_snapshots.append(
                    KeyQuotaSnapshot(
                        key_id=key.id,
                        source=key.source,
                        is_default=key.is_default,
                        minute_used=minute_used,
                        minute_limit=self.settings.rpm_limit,
                        daily_used=daily_used,
                        daily_limit=self.settings.rpd_limit,
                        minute_remaining=max(
                            self.settings.rpm_limit - minute_used, 0
                        ),
                        daily_remaining=max(self.settings.rpd_limit - daily_used, 0),
                        last_error=key_state.get("last_error"),
                        available=not self.settings.rpd_limit
                        or daily_used < self.settings.rpd_limit,
                    )
                )

            key_count = max(len(key_snapshots), 1)
            minute_used = sum(item.minute_used for item in key_snapshots)
            daily_used = sum(item.daily_used for item in key_snapshots)
            minute_limit = self.settings.rpm_limit * key_count
            daily_limit = self.settings.rpd_limit * key_count
            return QuotaSnapshot(
                minute_used=minute_used,
                minute_limit=minute_limit,
                daily_used=daily_used,
                daily_limit=daily_limit,
                minute_remaining=max(minute_limit - minute_used, 0),
                daily_remaining=max(daily_limit - daily_used, 0),
                reset_at=self._next_reset_iso(),
                last_error=state.get("last_error"),
                available=any(item.available for item in key_snapshots)
                if key_snapshots
                else False,
                keys=key_snapshots,
            )

    def daily_limit_reached(self) -> bool:
        return not self.snapshot().available

    def _normalize_state(self, state: dict) -> dict:
        today = self._today_key()
        state.setdefault("last_error", None)
        state.setdefault("keys", {})
        for key in self.keys:
            key_state = state["keys"].setdefault(key.id, self._empty_key_state())
            if key_state.get("day_key") != today:
                key_state["day_key"] = today
                key_state["daily_count"] = 0
            key_state.setdefault("request_timestamps", [])
            key_state.setdefault("last_error", None)
            key_state["request_timestamps"] = [
                float(ts) for ts in key_state["request_timestamps"]
            ]
        return state

    def _load_state(self) -> dict:
        if not self.path.exists():
            return {"last_error": None, "keys": {}}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _save_state(self, state: dict) -> None:
        atomic_write_text(self.path, json.dumps(state, indent=2))

    def _today_key(self) -> str:
        return datetime.now(UTC).date().isoformat()

    def _next_reset_iso(self) -> str:
        tomorrow = (datetime.now(UTC) + timedelta(days=1)).date()
        return f"{tomorrow.isoformat()} 00:00 UTC"

    @staticmethod
    def _empty_key_state() -> dict:
        return {
            "day_key": None,
            "daily_count": 0,
            "request_timestamps": [],
            "last_error": None,
        }
