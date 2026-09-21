"""仅追加事件存储。

每个事件包含 event_id / seq / event_time（调用方提供的业务时间）/
received_at（服务端接收时间）。存储按文件追加写入，重启后顺序重放，
满足“审计记录不得以覆盖方式修改”的领域约定。
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Optional


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class EventStore:
    def __init__(self, path: Optional[str] = None):
        self._path = path
        self._lock = threading.RLock()
        self._events: list[dict] = []
        self._seq = 0
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    event = json.loads(line)
                    self._events.append(event)
                    self._seq = max(self._seq, event["seq"])

    @property
    def all_events(self) -> list[dict]:
        with self._lock:
            return list(self._events)

    def append(
        self,
        event_type: str,
        payload: dict,
        *,
        event_id: Optional[str] = None,
        event_time: Optional[str] = None,
        dedupe_key: Optional[str] = None,
    ) -> dict:
        """追加事件。

        同一 dedupe_key 已存在时视为重复上报：直接返回既有事件，
        不产生新事件（幂等）。
        """
        with self._lock:
            if dedupe_key is not None:
                for existing in self._events:
                    if existing.get("dedupe_key") == dedupe_key:
                        return existing
            self._seq += 1
            event = {
                "event_id": event_id or f"evt-{self._seq}",
                "seq": self._seq,
                "type": event_type,
                "event_time": event_time or utc_now(),
                "received_at": utc_now(),
                "dedupe_key": dedupe_key,
                "payload": payload,
            }
            self._events.append(event)
            if self._path:
                tmp = self._path + ".tmp"
                # 崩溃安全：先写临时文件再原子替换，保证日志可完整重放。
                with open(tmp, "w", encoding="utf-8") as fh:
                    for e in self._events:
                        fh.write(json.dumps(e, ensure_ascii=False) + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, self._path)
            return event

    def replay(self, handler: Callable[[dict], None]) -> None:
        with self._lock:
            events = list(self._events)
        for event in events:
            handler(event)

    def since_seq(self, seq: int) -> list[dict]:
        """断线重连：返回最后确认点之后的事件。"""
        with self._lock:
            return [e for e in self._events if e["seq"] > seq]

    def reset(self) -> None:
        with self._lock:
            self._events = []
            self._seq = 0
            if self._path and os.path.exists(self._path):
                os.remove(self._path)
