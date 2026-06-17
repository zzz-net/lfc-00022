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


TEMPLATE_IMPORT_LOG_STATUS_SUCCESS = "success"
TEMPLATE_IMPORT_LOG_STATUS_PARTIAL = "partial"
TEMPLATE_IMPORT_LOG_STATUS_FAILED = "failed"
TEMPLATE_IMPORT_LOG_STATUS_ROLLED_BACK = "rolled_back"

VALID_TEMPLATE_IMPORT_LOG_STATUSES = {
    TEMPLATE_IMPORT_LOG_STATUS_SUCCESS,
    TEMPLATE_IMPORT_LOG_STATUS_PARTIAL,
    TEMPLATE_IMPORT_LOG_STATUS_FAILED,
    TEMPLATE_IMPORT_LOG_STATUS_ROLLED_BACK,
}

TEMPLATE_IMPORT_CONFLICT_SKIP = "skip"
TEMPLATE_IMPORT_CONFLICT_OVERWRITE = "overwrite"
TEMPLATE_IMPORT_CONFLICT_RENAME = "rename"
VALID_TEMPLATE_IMPORT_CONFLICT_STRATEGIES = {
    TEMPLATE_IMPORT_CONFLICT_SKIP,
    TEMPLATE_IMPORT_CONFLICT_OVERWRITE,
    TEMPLATE_IMPORT_CONFLICT_RENAME,
}


TEMPLATE_VERSION_OP_CREATE = "create"
TEMPLATE_VERSION_OP_UPDATE = "update"
TEMPLATE_VERSION_OP_OVERWRITE = "overwrite"
TEMPLATE_VERSION_OP_IMPORT = "import"
TEMPLATE_VERSION_OP_DELETE_BACKUP = "delete_backup"
TEMPLATE_VERSION_OP_ROLLBACK = "rollback"
VALID_TEMPLATE_VERSION_OPERATIONS = {
    TEMPLATE_VERSION_OP_CREATE,
    TEMPLATE_VERSION_OP_UPDATE,
    TEMPLATE_VERSION_OP_OVERWRITE,
    TEMPLATE_VERSION_OP_IMPORT,
    TEMPLATE_VERSION_OP_DELETE_BACKUP,
    TEMPLATE_VERSION_OP_ROLLBACK,
}


@dataclass
class TemplateVersion:
    """模板版本快照"""
    id: str
    template_id: str
    template_name: str
    version: int
    description: str
    filters: str
    updates: str
    conflict_strategy: str
    operation_type: str
    operator: str
    source_file: str
    parent_version: int
    branch_tag: str
    snapshot_at: str
    change_summary: str

    def to_dict(self) -> dict[str, Any]:
        import json
        return {
            "id": self.id,
            "template_id": self.template_id,
            "template_name": self.template_name,
            "version": self.version,
            "description": self.description,
            "filters": json.loads(self.filters),
            "updates": json.loads(self.updates),
            "conflict_strategy": self.conflict_strategy,
            "operation_type": self.operation_type,
            "operator": self.operator,
            "source_file": self.source_file,
            "parent_version": self.parent_version,
            "branch_tag": self.branch_tag,
            "snapshot_at": self.snapshot_at,
            "change_summary": self.change_summary,
        }


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

    def to_export_dict(self) -> dict[str, Any]:
        """导出为可跨环境传输的字典（不含内部ID和时间戳）"""
        import json
        return {
            "name": self.name,
            "description": self.description,
            "filters": json.loads(self.filters),
            "updates": json.loads(self.updates),
            "conflict_strategy": self.conflict_strategy,
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

                CREATE TABLE IF NOT EXISTS template_import_logs (
                    id TEXT PRIMARY KEY,
                    operation_type TEXT NOT NULL,
                    operator TEXT DEFAULT '',
                    source_file TEXT DEFAULT '',
                    total_count INTEGER DEFAULT 0,
                    success_count INTEGER DEFAULT 0,
                    skipped_count INTEGER DEFAULT 0,
                    overwritten_count INTEGER DEFAULT 0,
                    renamed_count INTEGER DEFAULT 0,
                    error_count INTEGER DEFAULT 0,
                    conflict_strategy TEXT DEFAULT 'skip',
                    status TEXT NOT NULL,
                    error_message TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    completed_at TEXT DEFAULT ''
                );

                CREATE INDEX IF NOT EXISTS idx_tpl_import_logs_created
                    ON template_import_logs(created_at);
                CREATE INDEX IF NOT EXISTS idx_tpl_import_logs_status
                    ON template_import_logs(status);

                CREATE TABLE IF NOT EXISTS template_import_items (
                    id TEXT PRIMARY KEY,
                    import_log_id TEXT NOT NULL,
                    template_name TEXT NOT NULL,
                    final_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT DEFAULT '',
                    FOREIGN KEY (import_log_id) REFERENCES template_import_logs(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_tpl_import_items_log
                    ON template_import_items(import_log_id);

                CREATE TABLE IF NOT EXISTS template_versions (
                    id TEXT PRIMARY KEY,
                    template_id TEXT NOT NULL,
                    template_name TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    description TEXT DEFAULT '',
                    filters TEXT NOT NULL,
                    updates TEXT NOT NULL,
                    conflict_strategy TEXT NOT NULL,
                    operation_type TEXT NOT NULL,
                    operator TEXT DEFAULT '',
                    source_file TEXT DEFAULT '',
                    parent_version INTEGER DEFAULT 0,
                    branch_tag TEXT DEFAULT '',
                    snapshot_at TEXT NOT NULL,
                    change_summary TEXT DEFAULT ''
                );

                CREATE INDEX IF NOT EXISTS idx_tpl_versions_tpl_id
                    ON template_versions(template_id);
                CREATE INDEX IF NOT EXISTS idx_tpl_versions_name
                    ON template_versions(template_name);
                CREATE INDEX IF NOT EXISTS idx_tpl_versions_snapshot
                    ON template_versions(snapshot_at);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_tpl_versions_unique
                    ON template_versions(template_id, version);
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

    # ============ 模板导入导出日志操作 ============

    def create_template_import_log(self, operation_type: str, operator: str,
                                   source_file: str, total_count: int,
                                   conflict_strategy: str) -> str:
        """创建模板导入/导出日志记录"""
        log_id = "TPL-LOG-" + uuid.uuid4().hex[:12].upper()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO template_import_logs
                   (id, operation_type, operator, source_file, total_count,
                    success_count, skipped_count, overwritten_count,
                    renamed_count, error_count, conflict_strategy,
                    status, created_at)
                   VALUES (?, ?, ?, ?, ?, 0, 0, 0, 0, 0, ?, 'pending', ?)""",
                (log_id, operation_type, operator, source_file, total_count,
                 conflict_strategy, now)
            )
        return log_id

    def update_template_import_log_counts(self, log_id: str, success_count: int,
                                          skipped_count: int, overwritten_count: int,
                                          renamed_count: int, error_count: int) -> None:
        """更新模板导入日志计数"""
        with self._conn() as conn:
            conn.execute(
                """UPDATE template_import_logs SET
                   success_count=?, skipped_count=?, overwritten_count=?,
                   renamed_count=?, error_count=?
                   WHERE id=?""",
                (success_count, skipped_count, overwritten_count,
                 renamed_count, error_count, log_id)
            )

    def complete_template_import_log(self, log_id: str, status: str,
                                     error_message: str = "") -> None:
        """标记模板导入日志为完成"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._conn() as conn:
            conn.execute(
                "UPDATE template_import_logs SET status=?, error_message=?, completed_at=? WHERE id=?",
                (status, error_message, now, log_id)
            )

    def add_template_import_item(self, log_id: str, template_name: str,
                                 final_name: str, status: str,
                                 reason: str = "") -> None:
        """添加模板导入单项记录"""
        item_id = str(uuid.uuid4())
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO template_import_items
                   (id, import_log_id, template_name, final_name, status, reason)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (item_id, log_id, template_name, final_name, status, reason)
            )

    def get_template_import_log(self, log_id: str):
        """获取模板导入日志"""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM template_import_logs WHERE id = ?",
                (log_id,)
            ).fetchone()
            if not row:
                return None
            return dict(row)

    def get_template_import_items(self, log_id: str) -> list[dict]:
        """获取模板导入日志的所有单项记录"""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM template_import_items WHERE import_log_id = ? ORDER BY id",
                (log_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    def get_recent_template_import_logs(self, limit: int = 20) -> list[dict]:
        """获取最近的模板导入导出日志"""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM template_import_logs ORDER BY created_at DESC LIMIT ?",
                (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    def delete_templates_by_names(self, names: list[str]) -> int:
        """按名称批量删除模板（用于回滚），返回实际删除数量"""
        if not names:
            return 0
        placeholders = ",".join(["?"] * len(names))
        with self._conn() as conn:
            cur = conn.execute(
                f"DELETE FROM batch_templates WHERE name IN ({placeholders})",
                names
            )
            return cur.rowcount

    # ============ TemplateVersion 操作 ============

    def get_next_template_version(self, template_id: str) -> int:
        """获取指定模板的下一个版本号"""
        with self._conn() as conn:
            cur = conn.execute(
                "SELECT COALESCE(MAX(version), 0) FROM template_versions WHERE template_id = ?",
                (template_id,)
            )
            return cur.fetchone()[0] + 1

    def insert_template_version(self, template_id: str, template_name: str,
                                version: int, description: str,
                                filters: str, updates: str, conflict_strategy: str,
                                operation_type: str, operator: str = "",
                                source_file: str = "", parent_version: int = 0,
                                branch_tag: str = "", change_summary: str = "") -> str:
        """插入模板版本快照，返回版本记录ID"""
        version_id = "TPL-VER-" + uuid.uuid4().hex[:12].upper()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO template_versions
                   (id, template_id, template_name, version, description,
                    filters, updates, conflict_strategy, operation_type,
                    operator, source_file, parent_version, branch_tag,
                    snapshot_at, change_summary)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (version_id, template_id, template_name, version, description,
                 filters, updates, conflict_strategy, operation_type,
                 operator, source_file, parent_version, branch_tag,
                 now, change_summary)
            )
        return version_id

    def get_template_version(self, version_id: str) -> Optional[TemplateVersion]:
        """按ID获取模板版本"""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM template_versions WHERE id = ?",
                (version_id,)
            ).fetchone()
            if not row:
                return None
            return TemplateVersion(**dict(row))

    def get_template_version_by_number(self, template_id: str, version: int) -> Optional[TemplateVersion]:
        """按模板ID和版本号获取模板版本"""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM template_versions WHERE template_id = ? AND version = ?",
                (template_id, version)
            ).fetchone()
            if not row:
                return None
            return TemplateVersion(**dict(row))

    def get_template_versions(self, template_id: str) -> list[TemplateVersion]:
        """获取指定模板的所有版本，按版本号降序"""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM template_versions WHERE template_id = ? ORDER BY version DESC",
                (template_id,)
            ).fetchall()
            return [TemplateVersion(**dict(r)) for r in rows]

    def get_template_versions_by_name(self, template_name: str) -> list[TemplateVersion]:
        """按模板名称获取所有版本（可能包含同名模板分叉），按快照时间降序"""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM template_versions WHERE template_name = ? ORDER BY snapshot_at DESC",
                (template_name,)
            ).fetchall()
            return [TemplateVersion(**dict(r)) for r in rows]

    def delete_template_versions(self, template_id: str) -> int:
        """删除指定模板的所有版本记录，返回删除数量"""
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM template_versions WHERE template_id = ?",
                (template_id,)
            )
            return cur.rowcount


# ============ 工单模块常量 ============

TICKET_STATUS_OPEN = "open"
TICKET_STATUS_ASSIGNED = "assigned"
TICKET_STATUS_IN_PROGRESS = "in_progress"
TICKET_STATUS_COMPLETED = "completed"
TICKET_STATUS_REVOKED = "revoked"

VALID_TICKET_STATUSES = {
    TICKET_STATUS_OPEN,
    TICKET_STATUS_ASSIGNED,
    TICKET_STATUS_IN_PROGRESS,
    TICKET_STATUS_COMPLETED,
    TICKET_STATUS_REVOKED,
}

TICKET_PRIORITY_LOW = "low"
TICKET_PRIORITY_MEDIUM = "medium"
TICKET_PRIORITY_HIGH = "high"
TICKET_PRIORITY_CRITICAL = "critical"

DEFAULT_TICKET_PRIORITIES = [
    TICKET_PRIORITY_LOW,
    TICKET_PRIORITY_MEDIUM,
    TICKET_PRIORITY_HIGH,
    TICKET_PRIORITY_CRITICAL,
]

TICKET_STATUS_LABELS = {
    TICKET_STATUS_OPEN: "待处理",
    TICKET_STATUS_ASSIGNED: "已分配",
    TICKET_STATUS_IN_PROGRESS: "处理中",
    TICKET_STATUS_COMPLETED: "已完成",
    TICKET_STATUS_REVOKED: "已撤回",
}

TICKET_PRIORITY_LABELS = {
    TICKET_PRIORITY_LOW: "低",
    TICKET_PRIORITY_MEDIUM: "中",
    TICKET_PRIORITY_HIGH: "高",
    TICKET_PRIORITY_CRITICAL: "紧急",
}

TICKET_LOG_OP_CREATE = "create"
TICKET_LOG_OP_ASSIGN = "assign"
TICKET_LOG_OP_CLAIM = "claim"
TICKET_LOG_OP_COMPLETE = "complete"
TICKET_LOG_OP_REVOKE = "revoke"
TICKET_LOG_OP_UPDATE = "update"
TICKET_LOG_OP_IMPORT = "import"

VALID_TICKET_LOG_OPS = {
    TICKET_LOG_OP_CREATE,
    TICKET_LOG_OP_ASSIGN,
    TICKET_LOG_OP_CLAIM,
    TICKET_LOG_OP_COMPLETE,
    TICKET_LOG_OP_REVOKE,
    TICKET_LOG_OP_UPDATE,
    TICKET_LOG_OP_IMPORT,
}

TICKET_IMPORT_CONFLICT_SKIP = "skip"
TICKET_IMPORT_CONFLICT_ABORT = "abort"
TICKET_IMPORT_CONFLICT_FORCE = "force"
VALID_TICKET_IMPORT_CONFLICT_STRATEGIES = {
    TICKET_IMPORT_CONFLICT_SKIP,
    TICKET_IMPORT_CONFLICT_ABORT,
    TICKET_IMPORT_CONFLICT_FORCE,
}


@dataclass
class Ticket:
    """工单"""
    id: str
    title: str
    description: str
    priority: str
    status: str
    assignee: str
    creator: str
    due_time: str = ""
    steps: str = ""
    note: str = ""
    created_at: str = ""
    updated_at: str = ""
    completed_at: str = ""
    version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticket_id": self.id,
            "title": self.title,
            "description": self.description,
            "priority": self.priority,
            "status": self.status,
            "assignee": self.assignee,
            "creator": self.creator,
            "due_time": self.due_time,
            "steps": self.steps,
            "note": self.note,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
            "version": self.version,
        }


@dataclass
class TicketLog:
    """工单流转日志"""
    id: str
    ticket_id: str
    operation: str
    operator: str
    old_status: str
    new_status: str
    old_assignee: str
    new_assignee: str
    note: str
    operated_at: str


@dataclass
class TicketEvent:
    """工单-事件关联"""
    ticket_id: str
    event_id: str


def _init_ticket_tables(conn: sqlite3.Connection) -> None:
    """初始化工单相关表（独立函数，便于数据库初始化时调用）"""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS tickets (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            description TEXT DEFAULT '',
            priority TEXT NOT NULL DEFAULT 'medium',
            status TEXT NOT NULL DEFAULT 'open',
            assignee TEXT DEFAULT '',
            creator TEXT NOT NULL,
            due_time TEXT DEFAULT '',
            steps TEXT DEFAULT '',
            note TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT DEFAULT '',
            version INTEGER DEFAULT 1
        );

        CREATE INDEX IF NOT EXISTS idx_tickets_status
            ON tickets(status);
        CREATE INDEX IF NOT EXISTS idx_tickets_priority
            ON tickets(priority);
        CREATE INDEX IF NOT EXISTS idx_tickets_assignee
            ON tickets(assignee);
        CREATE INDEX IF NOT EXISTS idx_tickets_created
            ON tickets(created_at);
        CREATE INDEX IF NOT EXISTS idx_tickets_due
            ON tickets(due_time);

        CREATE TABLE IF NOT EXISTS ticket_logs (
            id TEXT PRIMARY KEY,
            ticket_id TEXT NOT NULL,
            operation TEXT NOT NULL,
            operator TEXT NOT NULL,
            old_status TEXT NOT NULL,
            new_status TEXT NOT NULL,
            old_assignee TEXT NOT NULL,
            new_assignee TEXT NOT NULL,
            note TEXT DEFAULT '',
            operated_at TEXT NOT NULL,
            FOREIGN KEY (ticket_id) REFERENCES tickets(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_ticket_logs_ticket
            ON ticket_logs(ticket_id);
        CREATE INDEX IF NOT EXISTS idx_ticket_logs_time
            ON ticket_logs(operated_at);

        CREATE TABLE IF NOT EXISTS ticket_events (
            ticket_id TEXT NOT NULL,
            event_id TEXT NOT NULL,
            PRIMARY KEY (ticket_id, event_id),
            FOREIGN KEY (ticket_id) REFERENCES tickets(id) ON DELETE CASCADE,
            FOREIGN KEY (event_id) REFERENCES events(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_ticket_events_event
            ON ticket_events(event_id);
    """)


def _ensure_ticket_tables(db: "Database") -> None:
    """确保工单表存在（用于向后兼容，旧数据库可能没有工单表）"""
    with db._conn() as conn:
        _init_ticket_tables(conn)


def _generate_ticket_id() -> str:
    """生成工单ID"""
    return "TKT-" + uuid.uuid4().hex[:12].upper()


def _add_ticket_tables_to_init(db_class):
    """将工单表初始化添加到 Database._init_db 中"""
    original_init = db_class._init_db

    def new_init_db(self):
        original_init(self)
        with self._conn() as conn:
            _init_ticket_tables(conn)

    db_class._init_db = new_init_db
    return db_class


Database = _add_ticket_tables_to_init(Database)


def _add_ticket_methods(db_class):
    """将工单相关方法添加到 Database 类中"""

    def insert_ticket(self, ticket: Ticket, event_ids: list[str] | None = None) -> None:
        """插入工单"""
        event_ids = event_ids or []
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO tickets
                   (id, title, description, priority, status, assignee,
                    creator, due_time, steps, note, created_at, updated_at,
                    completed_at, version)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (ticket.id, ticket.title, ticket.description, ticket.priority,
                 ticket.status, ticket.assignee, ticket.creator, ticket.due_time,
                 ticket.steps, ticket.note, ticket.created_at, ticket.updated_at,
                 ticket.completed_at, ticket.version)
            )
            for eid in event_ids:
                conn.execute(
                    "INSERT OR IGNORE INTO ticket_events (ticket_id, event_id) VALUES (?, ?)",
                    (ticket.id, eid)
                )

    def get_ticket(self, ticket_id: str) -> Optional[Ticket]:
        """按ID获取工单"""
        _ensure_ticket_tables(self)
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM tickets WHERE id = ?", (ticket_id,)
            ).fetchone()
            if not row:
                return None
            return Ticket(**dict(row))

    def ticket_exists(self, ticket_id: str) -> bool:
        """检查工单是否存在"""
        _ensure_ticket_tables(self)
        with self._conn() as conn:
            cur = conn.execute(
                "SELECT COUNT(*) FROM tickets WHERE id = ?", (ticket_id,)
            )
            return cur.fetchone()[0] > 0

    def update_ticket(self, ticket: Ticket) -> None:
        """更新工单"""
        with self._conn() as conn:
            conn.execute(
                """UPDATE tickets SET
                   title=?, description=?, priority=?, status=?, assignee=?,
                   due_time=?, steps=?, note=?, updated_at=?, completed_at=?, version=?
                   WHERE id=?""",
                (ticket.title, ticket.description, ticket.priority, ticket.status,
                 ticket.assignee, ticket.due_time, ticket.steps, ticket.note,
                 ticket.updated_at, ticket.completed_at, ticket.version, ticket.id)
            )

    def update_ticket_with_version(self, ticket: Ticket, expected_version: int) -> bool:
        """带版本检查的更新，返回 True 表示成功"""
        ticket.version = expected_version + 1
        with self._conn() as conn:
            cur = conn.execute(
                """UPDATE tickets SET
                   title=?, description=?, priority=?, status=?, assignee=?,
                   due_time=?, steps=?, note=?, updated_at=?, completed_at=?, version=?
                   WHERE id=? AND version=?""",
                (ticket.title, ticket.description, ticket.priority, ticket.status,
                 ticket.assignee, ticket.due_time, ticket.steps, ticket.note,
                 ticket.updated_at, ticket.completed_at, ticket.version,
                 ticket.id, expected_version)
            )
            return cur.rowcount > 0

    def delete_ticket(self, ticket_id: str) -> None:
        """删除工单"""
        with self._conn() as conn:
            conn.execute("DELETE FROM tickets WHERE id = ?", (ticket_id,))

    def get_all_tickets(self) -> list[Ticket]:
        """获取所有工单"""
        _ensure_ticket_tables(self)
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM tickets ORDER BY created_at DESC"
            ).fetchall()
            return [Ticket(**dict(r)) for r in rows]

    def filter_tickets(self,
                       statuses: list[str] | None = None,
                       priorities: list[str] | None = None,
                       assignees: list[str] | None = None,
                       creators: list[str] | None = None) -> list[Ticket]:
        """按条件筛选工单"""
        _ensure_ticket_tables(self)
        conditions: list[str] = []
        params: list[Any] = []

        if statuses:
            placeholders = ",".join(["?"] * len(statuses))
            conditions.append(f"status IN ({placeholders})")
            params.extend(statuses)

        if priorities:
            placeholders = ",".join(["?"] * len(priorities))
            conditions.append(f"priority IN ({placeholders})")
            params.extend(priorities)

        if assignees:
            placeholders = ",".join(["?"] * len(assignees))
            conditions.append(f"assignee IN ({placeholders})")
            params.extend(assignees)

        if creators:
            placeholders = ",".join(["?"] * len(creators))
            conditions.append(f"creator IN ({placeholders})")
            params.extend(creators)

        where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""

        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM tickets {where_clause} ORDER BY created_at DESC",
                params
            ).fetchall()
            return [Ticket(**dict(r)) for r in rows]

    def add_ticket_event(self, ticket_id: str, event_id: str) -> None:
        """添加工单-事件关联"""
        with self._conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO ticket_events (ticket_id, event_id) VALUES (?, ?)",
                (ticket_id, event_id)
            )

    def add_ticket_events(self, ticket_id: str, event_ids: list[str]) -> None:
        """批量添加工单-事件关联"""
        with self._conn() as conn:
            for eid in event_ids:
                conn.execute(
                    "INSERT OR IGNORE INTO ticket_events (ticket_id, event_id) VALUES (?, ?)",
                    (ticket_id, eid)
                )

    def get_ticket_event_ids(self, ticket_id: str) -> list[str]:
        """获取工单关联的事件ID列表"""
        _ensure_ticket_tables(self)
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT event_id FROM ticket_events WHERE ticket_id = ? ORDER BY event_id",
                (ticket_id,)
            ).fetchall()
            return [r[0] for r in rows]

    def get_event_ticket_ids(self, event_id: str) -> list[str]:
        """获取事件关联的工单ID列表"""
        _ensure_ticket_tables(self)
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT ticket_id FROM ticket_events WHERE event_id = ? ORDER BY ticket_id",
                (event_id,)
            ).fetchall()
            return [r[0] for r in rows]

    def get_open_tickets_for_event(self, event_id: str) -> list[Ticket]:
        """获取事件关联的未完成工单"""
        _ensure_ticket_tables(self)
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT t.* FROM tickets t
                   JOIN ticket_events te ON t.id = te.ticket_id
                   WHERE te.event_id = ?
                     AND t.status NOT IN ('completed', 'revoked')
                   ORDER BY t.created_at DESC""",
                (event_id,)
            ).fetchall()
            return [Ticket(**dict(r)) for r in rows]

    def add_ticket_log(self, ticket_id: str, operation: str, operator: str,
                       old_status: str, new_status: str,
                       old_assignee: str, new_assignee: str,
                       note: str = "") -> str:
        """添加工单流转日志"""
        log_id = "TLOG-" + uuid.uuid4().hex[:10].upper()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO ticket_logs
                   (id, ticket_id, operation, operator, old_status, new_status,
                    old_assignee, new_assignee, note, operated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (log_id, ticket_id, operation, operator, old_status, new_status,
                 old_assignee, new_assignee, note, now)
            )
        return log_id

    def get_ticket_logs(self, ticket_id: str) -> list[TicketLog]:
        """获取工单的所有流转日志"""
        _ensure_ticket_tables(self)
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM ticket_logs WHERE ticket_id = ? ORDER BY operated_at ASC",
                (ticket_id,)
            ).fetchall()
            return [TicketLog(**dict(r)) for r in rows]

    def get_ticket_count(self) -> int:
        """获取工单总数"""
        _ensure_ticket_tables(self)
        with self._conn() as conn:
            cur = conn.execute("SELECT COUNT(*) FROM tickets")
            return cur.fetchone()[0]

    db_class.insert_ticket = insert_ticket
    db_class.get_ticket = get_ticket
    db_class.ticket_exists = ticket_exists
    db_class.update_ticket = update_ticket
    db_class.update_ticket_with_version = update_ticket_with_version
    db_class.delete_ticket = delete_ticket
    db_class.get_all_tickets = get_all_tickets
    db_class.filter_tickets = filter_tickets
    db_class.add_ticket_event = add_ticket_event
    db_class.add_ticket_events = add_ticket_events
    db_class.get_ticket_event_ids = get_ticket_event_ids
    db_class.get_event_ticket_ids = get_event_ticket_ids
    db_class.get_open_tickets_for_event = get_open_tickets_for_event
    db_class.add_ticket_log = add_ticket_log
    db_class.get_ticket_logs = get_ticket_logs
    db_class.get_ticket_count = get_ticket_count

    return db_class


Database = _add_ticket_methods(Database)
