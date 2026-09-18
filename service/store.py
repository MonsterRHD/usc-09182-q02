"""追加式记录存储：事件与幂等记录只增不改，保证历史不被覆盖。"""
from __future__ import annotations

import json
import os
import threading


class EventStore:
    """JSONL 追加日志。每行一条记录：{"kind": "event"|"idem", ...}。"""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

    def append(self, record: dict) -> None:
        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")

    def load(self) -> list:
        if not os.path.exists(self.path):
            return []
        records = []
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
        return records


class MemoryStore(EventStore):
    """测试用内存存储，接口与 EventStore 一致。"""

    def __init__(self):
        self.records: list = []
        self._lock = threading.Lock()

    def append(self, record: dict) -> None:
        with self._lock:
            self.records.append(record)

    def load(self) -> list:
        return list(self.records)
