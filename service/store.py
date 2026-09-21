"""事件存储：追加式日志，支持持久化与断点续读。

约定（见 docs/domain.md）：
- 每条事件分配连续递增的 seq，作为断线重连的“最后确认点”；
- 事件只追加、不覆盖，审计记录因此不可篡改；
- 提供 path 时持久化为 JSONL（每行一条事件），重启后按序重放。
"""
from __future__ import annotations

import json
import threading
from pathlib import Path


class EventStore:
    """追加式事件日志。"""

    def __init__(self, path: str | Path | None = None):
        self._path = Path(path) if path else None
        self._lock = threading.Lock()
        self.events: list[dict] = []
        if self._path and self._path.exists():
            with self._path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        self.events.append(json.loads(line))

    @property
    def latest_seq(self) -> int:
        """当前最大事件序号；空日志为 0。"""
        return self.events[-1]["seq"] if self.events else 0

    def append(self, event: dict) -> dict:
        """分配 seq 并追加事件；持久化与内存保持同一份内容。"""
        with self._lock:
            event["seq"] = self.latest_seq + 1
            self.events.append(event)
            if self._path:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(event, ensure_ascii=False) + "\n")
            return event

    def since(self, seq: int) -> tuple[list[dict], int]:
        """返回 seq 之后的事件与最新 seq，供断线重连续传。"""
        with self._lock:
            return [e for e in self.events if e["seq"] > seq], self.latest_seq
