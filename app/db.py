"""SQLite 持久化层。

设计要点：
- 当前生效输入（病例、亲属、配额、锁定）整体存为一个 JSON 行，带单调递增的
  dataset_version；任何一项输入变动（哪怕改回原值）都使 version +1。
- version 递增与“旧清单失效标记”在同一个 IMMEDIATE 事务内完成，保证两个浏览器
  窗口交错操作时也不会出现状态与失效标记不一致。
- 已发布清单连同其输入依据整行持久化，并带 invalidated 标记；失效后永不可再
  发布或下载，但记录保留可审计。
- 所有写操作失败即回滚，绝不修改源数据。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .models import ActiveInput


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class StaleVersionError(Exception):
    """预览/发布所依据的版本已经过期。"""


class Database:
    def __init__(self, path: str):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    dataset_version INTEGER NOT NULL,
                    input_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS version_log (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    dataset_version INTEGER NOT NULL,
                    change_type TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS published_manifests (
                    manifest_id TEXT PRIMARY KEY,
                    dataset_version INTEGER NOT NULL,
                    input_json TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    invalidated INTEGER NOT NULL DEFAULT 0,
                    invalidated_at TEXT
                );
                """
            )
            row = self._conn.execute("SELECT COUNT(*) AS n FROM state").fetchone()
            if row["n"] == 0:
                empty = ActiveInput().to_json()
                self._conn.execute(
                    "INSERT INTO state (id, dataset_version, input_json, updated_at)"
                    " VALUES (1, 0, ?, ?)",
                    (json.dumps(empty, ensure_ascii=False), _now()),
                )
                self._conn.commit()

    # ------------------------------------------------------------------ #
    # 读
    # ------------------------------------------------------------------ #
    def get_state(self) -> Tuple[int, ActiveInput]:
        with self._lock:
            row = self._conn.execute(
                "SELECT dataset_version, input_json FROM state WHERE id = 1"
            ).fetchone()
            return row["dataset_version"], ActiveInput.from_json(
                json.loads(row["input_json"])
            )

    def get_version(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT dataset_version FROM state WHERE id = 1"
            ).fetchone()
            return row["dataset_version"]

    def list_manifests(self) -> List[Dict[str, Any]]:
        current_version = self.get_version()
        with self._lock:
            rows = self._conn.execute(
                "SELECT manifest_id, dataset_version, created_at, invalidated,"
                " invalidated_at, result_json FROM published_manifests"
                " ORDER BY created_at DESC, manifest_id DESC"
            ).fetchall()
        out = []
        for r in rows:
            result = json.loads(r["result_json"])
            expired = bool(r["invalidated"]) or r["dataset_version"] != current_version
            out.append(
                {
                    "manifest_id": r["manifest_id"],
                    "dataset_version": r["dataset_version"],
                    "current_version": current_version,
                    "created_at": r["created_at"],
                    "invalidated": bool(r["invalidated"]),
                    "invalidated_at": r["invalidated_at"],
                    "expired": expired,
                    "case_count": result.get("stats", {}).get("case_count"),
                    "counts": result.get("stats", {}).get("counts"),
                }
            )
        return out

    def get_manifest(self, manifest_id: str) -> Optional[Dict[str, Any]]:
        current_version = self.get_version()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM published_manifests WHERE manifest_id = ?",
                (manifest_id,),
            ).fetchone()
        if row is None:
            return None
        expired = bool(row["invalidated"]) or row["dataset_version"] != current_version
        return {
            "manifest_id": row["manifest_id"],
            "dataset_version": row["dataset_version"],
            "current_version": current_version,
            "input": json.loads(row["input_json"]),
            "result": json.loads(row["result_json"]),
            "created_at": row["created_at"],
            "invalidated": bool(row["invalidated"]),
            "invalidated_at": row["invalidated_at"],
            "expired": expired,
        }

    # ------------------------------------------------------------------ #
    # 写（每个方法一个 IMMEDIATE 事务；异常回滚，源数据不变）
    # ------------------------------------------------------------------ #
    def _commit_new_input(
        self, new_input: ActiveInput, change_type: str, detail: Dict[str, Any]
    ) -> int:
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT dataset_version FROM state WHERE id = 1"
                ).fetchone()
                new_version = row["dataset_version"] + 1
                self._conn.execute(
                    "UPDATE state SET dataset_version = ?, input_json = ?, updated_at = ?"
                    " WHERE id = 1",
                    (
                        new_version,
                        json.dumps(new_input.to_json(), ensure_ascii=False),
                        _now(),
                    ),
                )
                # 所有此前发布的清单随输入变动永久失效
                self._conn.execute(
                    "UPDATE published_manifests SET invalidated = 1, invalidated_at = ?"
                    " WHERE invalidated = 0",
                    (_now(),),
                )
                self._conn.execute(
                    "INSERT INTO version_log (dataset_version, change_type, detail,"
                    " created_at) VALUES (?, ?, ?, ?)",
                    (
                        new_version,
                        change_type,
                        json.dumps(detail, ensure_ascii=False),
                        _now(),
                    ),
                )
                self._conn.commit()
                return new_version
            except Exception:
                self._conn.rollback()
                raise

    def replace_cases(self, cases) -> int:
        _, current = self.get_state()
        new_input = ActiveInput(
            cases=list(cases),
            relatives=[],  # 病例整体替换后，旧亲属对可能引用已删除患者，须清空重传
            quotas=current.quotas,
            locks={cid: g for cid, g in current.locks.items() if cid in {c.case_id for c in cases}},
        )
        return self._commit_new_input(
            new_input,
            "upload_cases",
            {"case_count": len(cases), "relatives_cleared": True,
             "locks_dropped": sorted(set(current.locks) - set(new_input.locks))},
        )

    def replace_relatives(self, relatives) -> int:
        _, current = self.get_state()
        new_input = ActiveInput(
            cases=current.cases,
            relatives=list(relatives),
            quotas=current.quotas,
            locks=current.locks,
        )
        return self._commit_new_input(
            new_input, "upload_relatives", {"pair_count": len(relatives)}
        )

    def update_quotas(self, quotas: Dict[Tuple[int, int], int]) -> int:
        _, current = self.get_state()
        new_input = ActiveInput(
            cases=current.cases,
            relatives=current.relatives,
            quotas=dict(quotas),
            locks=current.locks,
        )
        return self._commit_new_input(
            new_input,
            "update_quotas",
            {f"g{g}_label{l}": quotas[(g, l)] for g in (1, 2) for l in (0, 1)},
        )

    def update_locks(self, locks: Dict[str, int]) -> int:
        _, current = self.get_state()
        new_input = ActiveInput(
            cases=current.cases,
            relatives=current.relatives,
            quotas=current.quotas,
            locks=dict(locks),
        )
        return self._commit_new_input(
            new_input, "update_locks", {"lock_count": len(locks), "locks": dict(sorted(locks.items()))}
        )

    def publish_manifest(
        self, expected_version: int, input_data: dict, result: dict
    ) -> Dict[str, Any]:
        manifest_id = uuid.uuid4().hex
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                # 事务内再次核对版本，堵住“检查后、写入前”的交错窗口
                row = self._conn.execute(
                    "SELECT dataset_version, input_json FROM state WHERE id = 1"
                ).fetchone()
                if row["dataset_version"] != expected_version:
                    raise StaleVersionError(
                        f"输入已从版本 {expected_version} 变动到"
                        f" {row['dataset_version']}，该预览已永久失效，不能发布。"
                    )
                now = _now()
                self._conn.execute(
                    "INSERT INTO published_manifests (manifest_id, dataset_version,"
                    " input_json, result_json, created_at, invalidated)"
                    " VALUES (?, ?, ?, ?, ?, 0)",
                    (
                        manifest_id,
                        expected_version,
                        json.dumps(input_data, ensure_ascii=False),
                        json.dumps(result, ensure_ascii=False),
                        now,
                    ),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        manifest = self.get_manifest(manifest_id)
        assert manifest is not None
        return manifest

    def version_log(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT dataset_version, change_type, detail, created_at"
                " FROM version_log ORDER BY seq DESC LIMIT 100"
            ).fetchall()
        return [
            {
                "dataset_version": r["dataset_version"],
                "change_type": r["change_type"],
                "detail": json.loads(r["detail"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
