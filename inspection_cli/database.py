"""持久化层：SQLite 数据库操作"""
from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterator, Optional


VALID_STATUSES = {"unconfirmed", "confirmed", "false_positive", "closed"}

BATCH_STATUS_PENDING = "pending"
BATCH_STATUS_COMPLETED = "completed"
BATCH_STATUS_PARTIAL = "partial"

ITEM_STATUS_SUCCESS = "success"
ITEM_STATUS_SKIPPED = "skipped"
ITEM_STATUS_CONFLICT = "conflict"
ITEM_STATUS_ERROR = "error"

CONFLICT_STRATEGY_SKIP = "skip"
CONFLICT_STRATEGY_ABORT = "abort"
CONFLICT_STRATEGY_FORCE = "force"
VALID_CONFLICT_STRATEGIES = {CONFLICT_STRATEGY_SKIP, CONFLICT_STRATEGY_ABORT, CONFLICT_STRATEGY_FORCE}


@dataclass
class SourceRecord:
    """来源巡检记录"""
    id: str
    device_id: str
    event_time: str
    issue_type: str
    severity: str
    description: str = ""
    source_file: str = ""
    source_row: int = 0
    import_time: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "device_id": self.device_id,
            "event_time": self.event_time,
            "issue_type": self.issue_type,
            "severity": self.severity,
            "description": self.description,
            "source_file": self.source_file,
            "source_row": self.source_row,
            "import_time": self.import_time,
        }


@dataclass
class Event:
    """归并后的事件"""
    id: str
    device_id: str
    first_seen: str
    last_seen: str
    issue_type: str
    severity: str
    status: str = "unconfirmed"
    handler: str = ""
    note: str = ""
    record_count: int = 0
    record_ids: list[str] = field(default_factory=list)
    version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.id,
            "status": self.status,
            "device_id": self.device_id,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "severity": self.severity,
            "issue_type": self.issue_type,
            "record_count": self.record_count,
            "handler": self.handler,
            "note": self.note,
            "source_record_ids": ",".join(self.record_ids),
            "version": self.version,
        }


@dataclass
class Annotation:
    """标注历史记录"""
    id: str
    event_id: str
    old_status: str
    new_status: str
    handler: str
    note: str
    annotate_time: str


@dataclass
class BatchOperation:
    """批量操作记录"""
    id: str
    operation_type: str
    status: str
    operator: str
    filters: str
    updates: str
    total_count: int
    success_count: int
    skipped_count: int
    conflict_count: int
    error_count: int
    conflict_strategy: str
    created_at: str
    completed_at: str = ""


@dataclass
class BatchOperationItem:
    """批量操作单项记录"""
    id: str
    batch_id: str
    event_id: str
    old_version: int
    new_version: int
    old_status: str
    new_status: str
    old_handler: str
    new_handler: str
    old_note: str
    new_note: str
    status: str
    reason: str
    processed_at: str


@dataclass
class BatchTemplate:
    """批量任务模板"""
    id: str
    name: str
    description: str
    filters: str
    updates: str
    conflict_strategy: str
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        import json
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "filters": json.loads(self.filters),
            "updates": json.loads(self.updates),
            "conflict_strategy": self.conflict_strategy,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def describe(self) -> str:
        """输出模板简要描述"""
        import json
        filter_dict = json.loads(self.filters)
        update_dict = json.loads(self.updates)
        parts = []
        if filter_dict.get("event_ids"):
            parts.append(f"事件ID: {', '.join(filter_dict['event_ids'])}")
        if filter_dict.get("device_ids"):
            parts.append(f"设备: {', '.join(filter_dict['device_ids'])}")
        if filter_dict.get("statuses"):
            parts.append(f"状态筛选: {', '.join(filter_dict['statuses'])}")
        if filter_dict.get("time_from"):
            parts.append(f"起始时间: {filter_dict['time_from']}")
        if filter_dict.get("time_to"):
            parts.append(f"结束时间: {filter_dict['time_to']}")
        filter_desc = "; ".join(parts) if parts else "无筛选"

        update_parts = []
        if update_dict.get("status"):
            update_parts.append(f"状态→{update_dict['status']}")
        if update_dict.get("handler"):
            update_parts.append(f"处理人→{update_dict['handler']}")
        if update_dict.get("note") is not None:
            update_parts.append(f"备注→{update_dict['note'] or '(空)'}")
        update_desc = "; ".join(update_parts) if update_parts else "无更新"

        return f"筛选: {filter_desc} | 更新: {update_desc} | 冲突策略: {self.conflict_strategy}"


class Database:
    """SQLite 数据库封装"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        """初始化数据库表"""
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS source_records (
                    id TEXT PRIMARY KEY,
                    device_id TEXT NOT NULL,
                    event_time TEXT NOT NULL,
                    issue_type TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    description TEXT DEFAULT '',
                    source_file TEXT DEFAULT '',
                    source_row INTEGER DEFAULT 0,
                    import_time TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_records_device
                    ON source_records(device_id);
                CREATE INDEX IF NOT EXISTS idx_records_time
                    ON source_records(event_time);

                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY,
                    device_id TEXT NOT NULL,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    issue_type TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    status TEXT DEFAULT 'unconfirmed',
                    handler TEXT DEFAULT '',
                    note TEXT DEFAULT '',
                    record_count INTEGER DEFAULT 0,
                    version INTEGER DEFAULT 1
                );

                CREATE INDEX IF NOT EXISTS idx_events_device
                    ON events(device_id);
                CREATE INDEX IF NOT EXISTS idx_events_status
                    ON events(status);
                CREATE INDEX IF NOT EXISTS idx_events_first_seen
                    ON events(first_seen);
                CREATE INDEX IF NOT EXISTS idx_events_last_seen
                    ON events(last_seen);

                CREATE TABLE IF NOT EXISTS event_records (
                    event_id TEXT NOT NULL,
                    record_id TEXT NOT NULL,
                    PRIMARY KEY (event_id, record_id),
                    FOREIGN KEY (event_id) REFERENCES events(id) ON DELETE CASCADE,
                    FOREIGN KEY (record_id) REFERENCES source_records(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS annotations (
                    id TEXT PRIMARY KEY,
                    event_id TEXT NOT NULL,
                    old_status TEXT NOT NULL,
                    new_status TEXT NOT NULL,
                    handler TEXT NOT NULL,
                    note TEXT DEFAULT '',
                    annotate_time TEXT NOT NULL,
                    FOREIGN KEY (event_id) REFERENCES events(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_annotations_event
                    ON annotations(event_id);
                CREATE INDEX IF NOT EXISTS idx_annotations_time
                    ON annotations(annotate_time);

                CREATE TABLE IF NOT EXISTS batch_operations (
                    id TEXT PRIMARY KEY,
                    operation_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    operator TEXT NOT NULL,
                    filters TEXT NOT NULL,
                    updates TEXT NOT NULL,
                    total_count INTEGER DEFAULT 0,
                    success_count INTEGER DEFAULT 0,
                    skipped_count INTEGER DEFAULT 0,
                    conflict_count INTEGER DEFAULT 0,
                    error_count INTEGER DEFAULT 0,
                    conflict_strategy TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    completed_at TEXT DEFAULT ''
                );

                CREATE INDEX IF NOT EXISTS idx_batch_ops_status
                    ON batch_operations(status);
                CREATE INDEX IF NOT EXISTS idx_batch_ops_created
                    ON batch_operations(created_at);

                CREATE TABLE IF NOT EXISTS batch_operation_items (
                    id TEXT PRIMARY KEY,
                    batch_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    old_version INTEGER NOT NULL,
                    new_version INTEGER NOT NULL,
                    old_status TEXT NOT NULL,
                    new_status TEXT NOT NULL,
                    old_handler TEXT NOT NULL,
                    new_handler TEXT NOT NULL,
                    old_note TEXT NOT NULL,
                    new_note TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT DEFAULT '',
                    processed_at TEXT NOT NULL,
                    FOREIGN KEY (batch_id) REFERENCES batch_operations(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_batch_items_batch
                    ON batch_operation_items(batch_id);
                CREATE INDEX IF NOT EXISTS idx_batch_items_event
                    ON batch_operation_items(event_id);
                CREATE INDEX IF NOT EXISTS idx_batch_items_status
                    ON batch_operation_items(status);

                CREATE TABLE IF NOT EXISTS batch_templates (
                    id TEXT PRIMARY KEY,
                    name TEXT UNIQUE NOT NULL,
                    description TEXT DEFAULT '',
                    filters TEXT NOT NULL,
                    updates TEXT NOT NULL,
                    conflict_strategy TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_templates_name
                    ON batch_templates(name);
            """)

    # ============ SourceRecord 操作 ============

    def record_exists(self, record_id: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "SELECT COUNT(*) FROM source_records WHERE id = ?",
                (record_id,)
            )
            return cur.fetchone()[0] > 0

    def insert_record(self, record: SourceRecord) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO source_records
                   (id, device_id, event_time, issue_type, severity,
                    description, source_file, source_row, import_time)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (record.id, record.device_id, record.event_time,
                 record.issue_type, record.severity, record.description,
                 record.source_file, record.source_row, record.import_time)
            )

    def get_all_records(self) -> list[SourceRecord]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM source_records ORDER BY event_time ASC"
            ).fetchall()
            return [SourceRecord(**dict(r)) for r in rows]

    def get_record_ids(self) -> set[str]:
        with self._conn() as conn:
            rows = conn.execute("SELECT id FROM source_records").fetchall()
            return {r[0] for r in rows}

    # ============ Event 操作 ============

    def clear_events(self) -> None:
        """清空事件表（用于重新归并，但不清空来源记录）"""
        with self._conn() as conn:
            conn.execute("DELETE FROM event_records")
            conn.execute("DELETE FROM events")

    def insert_event(self, event: Event) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO events
                   (id, device_id, first_seen, last_seen, issue_type,
                    severity, status, handler, note, record_count, version)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (event.id, event.device_id, event.first_seen, event.last_seen,
                 event.issue_type, event.severity, event.status,
                 event.handler, event.note, event.record_count, event.version)
            )
            for rid in event.record_ids:
                conn.execute(
                    "INSERT OR IGNORE INTO event_records (event_id, record_id) VALUES (?, ?)",
                    (event.id, rid)
                )

    def update_event(self, event: Event) -> None:
        with self._conn() as conn:
            conn.execute(
                """UPDATE events SET
                   device_id=?, first_seen=?, last_seen=?, issue_type=?,
                   severity=?, status=?, handler=?, note=?, record_count=?, version=?
                   WHERE id=?""",
                (event.device_id, event.first_seen, event.last_seen,
                 event.issue_type, event.severity, event.status,
                 event.handler, event.note, event.record_count,
                 event.version, event.id)
            )

    def update_event_with_version(self, event: Event, expected_version: int) -> bool:
        """带版本检查的更新，用于乐观锁。返回 True 表示更新成功，False 表示版本冲突。"""
        event.version = expected_version + 1
        with self._conn() as conn:
            cur = conn.execute(
                """UPDATE events SET
                   status=?, handler=?, note=?, version=?
                   WHERE id=? AND version=?""",
                (event.status, event.handler, event.note, event.version,
                 event.id, expected_version)
            )
            return cur.rowcount > 0

    def get_event(self, event_id: str) -> Optional[Event]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM events WHERE id = ?", (event_id,)
            ).fetchone()
            if not row:
                return None
            d = dict(row)
            rids = conn.execute(
                "SELECT record_id FROM event_records WHERE event_id = ? ORDER BY record_id",
                (event_id,)
            ).fetchall()
            d["record_ids"] = [r[0] for r in rids]
            return Event(**d)

    def get_all_events(self) -> list[Event]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM events ORDER BY first_seen ASC"
            ).fetchall()
            events = []
            for row in rows:
                d = dict(row)
                rids = conn.execute(
                    "SELECT record_id FROM event_records WHERE event_id = ? ORDER BY record_id",
                    (d["id"],)
                ).fetchall()
                d["record_ids"] = [r[0] for r in rids]
                events.append(Event(**d))
            return events

    def event_exists(self, event_id: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "SELECT COUNT(*) FROM events WHERE id = ?", (event_id,)
            )
            return cur.fetchone()[0] > 0

    # ============ Annotation 操作 ============

    def add_annotation(self, event_id: str, old_status: str,
                       new_status: str, handler: str, note: str) -> str:
        ann_id = str(uuid.uuid4())
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO annotations
                   (id, event_id, old_status, new_status, handler, note, annotate_time)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (ann_id, event_id, old_status, new_status, handler, note, now)
            )
        return ann_id

    def get_last_annotation(self, event_id: str) -> Optional[Annotation]:
        with self._conn() as conn:
            row = conn.execute(
                """SELECT * FROM annotations
                   WHERE event_id = ?
                   ORDER BY rowid DESC LIMIT 1""",
                (event_id,)
            ).fetchone()
            if not row:
                return None
            return Annotation(**dict(row))

    def delete_annotation(self, annotation_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM annotations WHERE id = ?", (annotation_id,)
            )

    def get_annotations_for_event(self, event_id: str) -> list[Annotation]:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM annotations
                   WHERE event_id = ?
                   ORDER BY rowid ASC""",
                (event_id,)
            ).fetchall()
            return [Annotation(**dict(r)) for r in rows]

    def get_annotation_count(self, event_id: str) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                "SELECT COUNT(*) FROM annotations WHERE event_id = ?", (event_id,)
            )
            return cur.fetchone()[0]

    # ============ 事件过滤查询 ============

    def filter_events(self,
                      event_ids: Optional[list[str]] = None,
                      device_ids: Optional[list[str]] = None,
                      statuses: Optional[list[str]] = None,
                      time_from: Optional[str] = None,
                      time_to: Optional[str] = None) -> list[Event]:
        """按条件筛选事件"""
        conditions: list[str] = []
        params: list[Any] = []

        if event_ids:
            placeholders = ",".join(["?"] * len(event_ids))
            conditions.append(f"e.id IN ({placeholders})")
            params.extend(event_ids)

        if device_ids:
            placeholders = ",".join(["?"] * len(device_ids))
            conditions.append(f"e.device_id IN ({placeholders})")
            params.extend(device_ids)

        if statuses:
            placeholders = ",".join(["?"] * len(statuses))
            conditions.append(f"e.status IN ({placeholders})")
            params.extend(statuses)

        if time_from:
            conditions.append("e.last_seen >= ?")
            params.append(time_from)

        if time_to:
            conditions.append("e.first_seen <= ?")
            params.append(time_to)

        where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""

        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM events e {where_clause} ORDER BY e.first_seen ASC",
                params
            ).fetchall()
            events = []
            for row in rows:
                d = dict(row)
                rids = conn.execute(
                    "SELECT record_id FROM event_records WHERE event_id = ? ORDER BY record_id",
                    (d["id"],)
                ).fetchall()
                d["record_ids"] = [r[0] for r in rids]
                events.append(Event(**d))
            return events

    # ============ BatchOperation 操作 ============

    def create_batch_operation(self, operation_type: str, operator: str,
                               filters: str, updates: str,
                               total_count: int, conflict_strategy: str) -> str:
        """创建批量操作记录"""
        batch_id = "BATCH-" + uuid.uuid4().hex[:12].upper()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO batch_operations
                   (id, operation_type, status, operator, filters, updates,
                    total_count, success_count, skipped_count, conflict_count,
                    error_count, conflict_strategy, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, 0, 0, ?, ?)""",
                (batch_id, operation_type, BATCH_STATUS_PENDING, operator,
                 filters, updates, total_count, conflict_strategy, now)
            )
        return batch_id

    def update_batch_operation_counts(self, batch_id: str, success_count: int,
                                      skipped_count: int, conflict_count: int,
                                      error_count: int) -> None:
        """更新批量操作计数"""
        total = success_count + skipped_count + conflict_count + error_count
        with self._conn() as conn:
            conn.execute(
                """UPDATE batch_operations SET
                   success_count=?, skipped_count=?, conflict_count=?, error_count=?
                   WHERE id=?""",
                (success_count, skipped_count, conflict_count, error_count, batch_id)
            )

    def complete_batch_operation(self, batch_id: str) -> None:
        """标记批量操作为完成"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._conn() as conn:
            row = conn.execute(
                """SELECT success_count, skipped_count, conflict_count, error_count, total_count
                   FROM batch_operations WHERE id=?""",
                (batch_id,)
            ).fetchone()
            if not row:
                return
            success, skipped, conflict, error, total = row
            if success == total:
                status = BATCH_STATUS_COMPLETED
            elif success + skipped + conflict + error > 0:
                status = BATCH_STATUS_PARTIAL
            else:
                status = BATCH_STATUS_COMPLETED
            conn.execute(
                "UPDATE batch_operations SET status=?, completed_at=? WHERE id=?",
                (status, now, batch_id)
            )

    def add_batch_operation_item(self, item: BatchOperationItem) -> None:
        """添加批量操作单项记录"""
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO batch_operation_items
                   (id, batch_id, event_id, old_version, new_version,
                    old_status, new_status, old_handler, new_handler,
                    old_note, new_note, status, reason, processed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (item.id, item.batch_id, item.event_id, item.old_version,
                 item.new_version, item.old_status, item.new_status,
                 item.old_handler, item.new_handler, item.old_note,
                 item.new_note, item.status, item.reason, item.processed_at)
            )

    def get_batch_operation(self, batch_id: str) -> Optional[BatchOperation]:
        """获取批量操作记录"""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM batch_operations WHERE id = ?",
                (batch_id,)
            ).fetchone()
            if not row:
                return None
            return BatchOperation(**dict(row))

    def get_batch_operation_items(self, batch_id: str) -> list[BatchOperationItem]:
        """获取批量操作的所有单项记录"""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM batch_operation_items WHERE batch_id = ? ORDER BY processed_at",
                (batch_id,)
            ).fetchall()
            return [BatchOperationItem(**dict(r)) for r in rows]

    def get_recent_batch_operations(self, limit: int = 20) -> list[BatchOperation]:
        """获取最近的批量操作记录"""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM batch_operations ORDER BY created_at DESC LIMIT ?",
                (limit,)
            ).fetchall()
            return [BatchOperation(**dict(r)) for r in rows]

    def cleanup_old_batch_operations(self, days: int) -> int:
        """清理指定天数前的批量操作记录，返回删除的记录数"""
        from datetime import timedelta
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM batch_operations WHERE created_at < ?",
                (cutoff,)
            )
            return cur.rowcount

    # ============ BatchTemplate 操作 ============

    def insert_template(self, template_id: str, name: str, description: str,
                      filters: str, updates: str, conflict_strategy: str,
                      created_at: str, updated_at: str) -> None:
        """插入模板"""
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO batch_templates
                   (id, name, description, filters, updates, conflict_strategy,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (template_id, name, description, filters, updates,
                 conflict_strategy, created_at, updated_at)
            )

    def update_template(self, template_id: str, description: str,
                        filters: str, updates: str, conflict_strategy: str,
                        updated_at: str) -> None:
        """更新模板"""
        with self._conn() as conn:
            conn.execute(
                """UPDATE batch_templates SET
                   description=?, filters=?, updates=?,
                   conflict_strategy=?, updated_at=?
                   WHERE id=?""",
                (description, filters, updates, conflict_strategy,
                 updated_at, template_id)
            )

    def get_template(self, template_id: str) -> Optional[BatchTemplate]:
        """按ID获取模板"""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM batch_templates WHERE id = ?",
                (template_id,)
            ).fetchone()
            if not row:
                return None
            return BatchTemplate(**dict(row))

    def get_template_by_name(self, name: str) -> Optional[BatchTemplate]:
        """按名称获取模板"""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM batch_templates WHERE name = ?",
                (name,)
            ).fetchone()
            if not row:
                return None
            return BatchTemplate(**dict(row))

    def get_all_templates(self) -> list[BatchTemplate]:
        """获取所有模板，按创建时间倒序"""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM batch_templates ORDER BY created_at DESC"
            ).fetchall()
            return [BatchTemplate(**dict(r)) for r in rows]

    def delete_template(self, template_id: str) -> None:
        """删除模板"""
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM batch_templates WHERE id = ?",
                (template_id,)
            )
