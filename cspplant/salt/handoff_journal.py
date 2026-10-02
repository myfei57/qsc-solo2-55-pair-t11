"""切换台账持久化。

台账是一份只增不改的 JSONL 流水，和审计流水同目录、同样 fsync。每次切换落一条
:class:`~cspplant.salt.record.HandoffRecord`；控制台与审计比对时以
``handoff_id`` 关联同一次操作。单行追加在文件锁内完成，掉电最多留下最后一行
半截——恢复时跳过最后一个解析不了的残行，前面的记录一条不丢。
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any

from ..store.jsonfile import JsonStore
from .record import HandoffRecord


class HandoffJournal:
    """切换台账：内存副本 + JSONL 追加落盘。"""

    def __init__(self, store: JsonStore, memory_limit: int = 512) -> None:
        self._store = store
        self._path = os.path.join(store.root, "salt", "handoffs.jsonl")
        self._records: list[HandoffRecord] = []
        self._memory_limit = max(1, memory_limit)
        self._lock = threading.RLock()
        self._restore()

    def _restore(self) -> None:
        if not os.path.isfile(self._path):
            return
        with open(self._path, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
        # 末尾残行（掉电）跳过，其余行必须合法，合法行不允许静默写错。
        for index, line in enumerate(lines):
            text = line.strip()
            if not text:
                continue
            try:
                self._records.append(HandoffRecord.from_dict(json.loads(text)))
            except json.JSONDecodeError:
                if index != len(lines) - 1:
                    raise
                self._records_skipped_tail = True

    def append(self, record: HandoffRecord) -> dict[str, Any]:
        """追加一条切换记录（内存 + 落盘），返回其字典形式。"""

        with self._lock:
            payload = record.as_dict()
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            with open(self._path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            self._records.append(record)
            if len(self._records) > self._memory_limit:
                del self._records[: len(self._records) - self._memory_limit]
        return payload

    def records(self) -> list[HandoffRecord]:
        """返回全部台账记录（按时间升序）。"""

        with self._lock:
            return list(self._records)

    def latest(self) -> HandoffRecord | None:
        """返回最近一次切换记录。"""

        with self._lock:
            return self._records[-1] if self._records else None

    def by_id(self, handoff_id: str) -> HandoffRecord | None:
        """按切换编号取回记录。"""

        with self._lock:
            for record in reversed(self._records):
                if record.handoff_id == handoff_id:
                    return record
        return None

    def count(self) -> int:
        with self._lock:
            return len(self._records)

    def verify_ids(self, ids: set[str]) -> list[str]:
        """核对给定的切换编号是否都在台账里，返回缺失编号。"""

        with self._lock:
            have = {record.handoff_id for record in self._records}
        return sorted(set(ids) - have)
