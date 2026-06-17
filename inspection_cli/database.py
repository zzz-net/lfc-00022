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


# ============ 值班排班模块常量 ============

DUTY_ROLE_LEADER = "leader"
DUTY_ROLE_ENGINEER = "engineer"
DUTY_ROLE_OPERATOR = "operator"
DUTY_ROLE_MANAGER = "manager"

VALID_DUTY_ROLES = {
    DUTY_ROLE_LEADER,
    DUTY_ROLE_ENGINEER,
    DUTY_ROLE_OPERATOR,
    DUTY_ROLE_MANAGER,
}

DUTY_SHIFT_MORNING = "morning"
DUTY_SHIFT_AFTERNOON = "afternoon"
DUTY_SHIFT_NIGHT = "night"
DUTY_SHIFT_DAY = "day"
DUTY_SHIFT_CUSTOM = "custom"

VALID_DUTY_SHIFTS = {
    DUTY_SHIFT_MORNING,
    DUTY_SHIFT_AFTERNOON,
    DUTY_SHIFT_NIGHT,
    DUTY_SHIFT_DAY,
    DUTY_SHIFT_CUSTOM,
}

DUTY_SHIFT_TIME_RANGES = {
    DUTY_SHIFT_MORNING: ("08:00", "16:00"),
    DUTY_SHIFT_AFTERNOON: ("16:00", "00:00"),
    DUTY_SHIFT_NIGHT: ("00:00", "08:00"),
    DUTY_SHIFT_DAY: ("09:00", "18:00"),
}

DUTY_HANDOVER_STATUS_ACTIVE = "active"
DUTY_HANDOVER_STATUS_REVOKED = "revoked"

VALID_DUTY_HANDOVER_STATUSES = {
    DUTY_HANDOVER_STATUS_ACTIVE,
    DUTY_HANDOVER_STATUS_REVOKED,
}

DUTY_ESCALATION_STATUS_PENDING = "pending"
DUTY_ESCALATION_STATUS_ACKNOWLEDGED = "acknowledged"
DUTY_ESCALATION_STATUS_ESCALATED = "escalated"
DUTY_ESCALATION_STATUS_RESOLVED = "resolved"
DUTY_ESCALATION_STATUS_CLOSED = "closed"

VALID_DUTY_ESCALATION_STATUSES = {
    DUTY_ESCALATION_STATUS_PENDING,
    DUTY_ESCALATION_STATUS_ACKNOWLEDGED,
    DUTY_ESCALATION_STATUS_ESCALATED,
    DUTY_ESCALATION_STATUS_RESOLVED,
    DUTY_ESCALATION_STATUS_CLOSED,
}

DUTY_IMPORT_CONFLICT_SKIP = "skip"
DUTY_IMPORT_CONFLICT_ABORT = "abort"
DUTY_IMPORT_CONFLICT_FORCE = "force"

VALID_DUTY_IMPORT_CONFLICT_STRATEGIES = {
    DUTY_IMPORT_CONFLICT_SKIP,
    DUTY_IMPORT_CONFLICT_ABORT,
    DUTY_IMPORT_CONFLICT_FORCE,
}


@dataclass
class DutyTeam:
    """班组"""
    id: str
    name: str
    description: str = ""
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "team_id": self.id,
            "name": self.name,
            "description": self.description,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def to_export_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
        }


@dataclass
class DutyMember:
    """值班人员"""
    id: str
    team_id: str
    name: str
    role: str
    phone: str = ""
    email: str = ""
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "member_id": self.id,
            "team_id": self.team_id,
            "name": self.name,
            "role": self.role,
            "phone": self.phone,
            "email": self.email,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def to_export_dict(self) -> dict[str, Any]:
        return {
            "team_name": "",
            "name": self.name,
            "role": self.role,
            "phone": self.phone,
            "email": self.email,
        }


@dataclass
class DutySchedule:
    """排班记录"""
    id: str
    team_id: str
    member_id: str
    shift_type: str
    schedule_date: str
    start_time: str
    end_time: str
    escalation_level: int = 1
    note: str = ""
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schedule_id": self.id,
            "team_id": self.team_id,
            "member_id": self.member_id,
            "shift_type": self.shift_type,
            "schedule_date": self.schedule_date,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "escalation_level": self.escalation_level,
            "note": self.note,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def to_export_dict(self) -> dict[str, Any]:
        return {
            "team_name": "",
            "member_name": "",
            "shift_type": self.shift_type,
            "schedule_date": self.schedule_date,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "escalation_level": self.escalation_level,
            "note": self.note,
        }


@dataclass
class DutyEscalationLevel:
    """升级层级定义"""
    id: str
    team_id: str
    level: int
    name: str
    response_minutes: int = 30
    escalation_minutes: int = 60
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "level_id": self.id,
            "team_id": self.team_id,
            "level": self.level,
            "name": self.name,
            "response_minutes": self.response_minutes,
            "escalation_minutes": self.escalation_minutes,
            "created_at": self.created_at,
        }


@dataclass
class DutyHandover:
    """交班记录"""
    id: str
    team_id: str
    from_member_id: str
    to_member_id: str
    operator_member_id: str
    schedule_id: Optional[str]
    handed_at: str
    note: str = ""
    status: str = DUTY_HANDOVER_STATUS_ACTIVE
    revoked_at: Optional[str] = None
    revoked_by: Optional[str] = None
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "handover_id": self.id,
            "team_id": self.team_id,
            "from_member_id": self.from_member_id,
            "to_member_id": self.to_member_id,
            "operator_member_id": self.operator_member_id,
            "schedule_id": self.schedule_id,
            "handed_at": self.handed_at,
            "note": self.note,
            "status": self.status,
            "revoked_at": self.revoked_at,
            "revoked_by": self.revoked_by,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class DutyEscalationLog:
    """升级命中日志"""
    id: str
    event_id: str
    event_title: str
    team_id: str
    member_id: str
    escalation_level: int
    event_time: str
    hit_time: str
    status: str = DUTY_ESCALATION_STATUS_PENDING
    schedule_id: Optional[str] = None
    handover_note: str = ""
    acknowledged_at: Optional[str] = None
    escalated_to: Optional[str] = None
    escalated_at: Optional[str] = None
    resolved_at: Optional[str] = None
    note: str = ""
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "log_id": self.id,
            "event_id": self.event_id,
            "event_title": self.event_title,
            "team_id": self.team_id,
            "schedule_id": self.schedule_id,
            "member_id": self.member_id,
            "escalation_level": self.escalation_level,
            "event_time": self.event_time,
            "hit_time": self.hit_time,
            "status": self.status,
            "handover_note": self.handover_note,
            "acknowledged_at": self.acknowledged_at,
            "escalated_to": self.escalated_to,
            "escalated_at": self.escalated_at,
            "resolved_at": self.resolved_at,
            "note": self.note,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class DutyTimeWindow:
    """时间窗口（用于匹配特殊时段）"""
    id: str
    team_id: str
    name: str
    start_time: str
    end_time: str
    days_of_week: str = ""
    priority: int = 1
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "window_id": self.id,
            "team_id": self.team_id,
            "name": self.name,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "days_of_week": self.days_of_week,
            "priority": self.priority,
            "created_at": self.created_at,
        }


def _init_duty_tables(conn: sqlite3.Connection) -> None:
    """初始化值班排班相关表"""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS duty_teams (
            id TEXT PRIMARY KEY,
            name TEXT UNIQUE NOT NULL,
            description TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS duty_members (
            id TEXT PRIMARY KEY,
            team_id TEXT NOT NULL,
            name TEXT NOT NULL,
            role TEXT NOT NULL,
            phone TEXT DEFAULT '',
            email TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (team_id) REFERENCES duty_teams(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_duty_members_team
            ON duty_members(team_id);
        CREATE INDEX IF NOT EXISTS idx_duty_members_name
            ON duty_members(name);

        CREATE TABLE IF NOT EXISTS duty_escalation_levels (
            id TEXT PRIMARY KEY,
            team_id TEXT NOT NULL,
            level INTEGER NOT NULL,
            name TEXT NOT NULL,
            response_minutes INTEGER DEFAULT 30,
            escalation_minutes INTEGER DEFAULT 60,
            created_at TEXT NOT NULL,
            FOREIGN KEY (team_id) REFERENCES duty_teams(id) ON DELETE CASCADE,
            UNIQUE (team_id, level)
        );

        CREATE INDEX IF NOT EXISTS idx_duty_esc_levels_team
            ON duty_escalation_levels(team_id);

        CREATE TABLE IF NOT EXISTS duty_time_windows (
            id TEXT PRIMARY KEY,
            team_id TEXT NOT NULL,
            name TEXT NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            days_of_week TEXT DEFAULT '',
            priority INTEGER DEFAULT 1,
            created_at TEXT NOT NULL,
            FOREIGN KEY (team_id) REFERENCES duty_teams(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_duty_windows_team
            ON duty_time_windows(team_id);

        CREATE TABLE IF NOT EXISTS duty_schedules (
            id TEXT PRIMARY KEY,
            team_id TEXT NOT NULL,
            member_id TEXT NOT NULL,
            shift_type TEXT NOT NULL,
            schedule_date TEXT NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            escalation_level INTEGER DEFAULT 1,
            note TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (team_id) REFERENCES duty_teams(id) ON DELETE CASCADE,
            FOREIGN KEY (member_id) REFERENCES duty_members(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_duty_sched_team_date
            ON duty_schedules(team_id, schedule_date);
        CREATE INDEX IF NOT EXISTS idx_duty_sched_member
            ON duty_schedules(member_id);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_duty_sched_unique
            ON duty_schedules(team_id, schedule_date, start_time, end_time);

        CREATE TABLE IF NOT EXISTS duty_handovers (
            id TEXT PRIMARY KEY,
            team_id TEXT NOT NULL,
            from_member_id TEXT NOT NULL,
            to_member_id TEXT NOT NULL,
            operator_member_id TEXT NOT NULL,
            schedule_id TEXT,
            handed_at TEXT NOT NULL,
            note TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active',
            revoked_at TEXT,
            revoked_by TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (team_id) REFERENCES duty_teams(id) ON DELETE CASCADE,
            FOREIGN KEY (from_member_id) REFERENCES duty_members(id) ON DELETE CASCADE,
            FOREIGN KEY (to_member_id) REFERENCES duty_members(id) ON DELETE CASCADE,
            FOREIGN KEY (operator_member_id) REFERENCES duty_members(id) ON DELETE CASCADE,
            FOREIGN KEY (schedule_id) REFERENCES duty_schedules(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_duty_handover_team
            ON duty_handovers(team_id);
        CREATE INDEX IF NOT EXISTS idx_duty_handover_schedule
            ON duty_handovers(schedule_id);
        CREATE INDEX IF NOT EXISTS idx_duty_handover_time
            ON duty_handovers(handed_at);
        CREATE INDEX IF NOT EXISTS idx_duty_handover_status
            ON duty_handovers(status);

        CREATE TABLE IF NOT EXISTS duty_escalation_logs (
            id TEXT PRIMARY KEY,
            event_id TEXT NOT NULL,
            event_title TEXT NOT NULL,
            team_id TEXT NOT NULL,
            schedule_id TEXT,
            member_id TEXT NOT NULL,
            escalation_level INTEGER NOT NULL,
            event_time TEXT NOT NULL,
            hit_time TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            handover_note TEXT DEFAULT '',
            acknowledged_at TEXT,
            resolved_at TEXT,
            escalated_to TEXT,
            escalated_at TEXT,
            note TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (team_id) REFERENCES duty_teams(id) ON DELETE CASCADE,
            FOREIGN KEY (schedule_id) REFERENCES duty_schedules(id) ON DELETE CASCADE,
            FOREIGN KEY (member_id) REFERENCES duty_members(id) ON DELETE CASCADE,
            FOREIGN KEY (escalated_to) REFERENCES duty_members(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_duty_log_event
            ON duty_escalation_logs(event_id);
        CREATE INDEX IF NOT EXISTS idx_duty_log_team
            ON duty_escalation_logs(team_id);
        CREATE INDEX IF NOT EXISTS idx_duty_log_member
            ON duty_escalation_logs(member_id);
        CREATE INDEX IF NOT EXISTS idx_duty_log_time
            ON duty_escalation_logs(hit_time);
        CREATE INDEX IF NOT EXISTS idx_duty_log_status
            ON duty_escalation_logs(status);
    """)


def _ensure_duty_tables(db: "Database") -> None:
    """确保值班排班表存在"""
    with db._conn() as conn:
        _init_duty_tables(conn)


def _add_duty_tables_to_init(db_class):
    """将值班排班表初始化添加到 Database._init_db 中"""
    original_init = db_class._init_db

    def new_init_db(self):
        original_init(self)
        with self._conn() as conn:
            _init_duty_tables(conn)

    db_class._init_db = new_init_db
    return db_class


Database = _add_duty_tables_to_init(Database)


def _generate_duty_id(prefix: str) -> str:
    """生成值班排班相关ID"""
    return prefix + uuid.uuid4().hex[:12].upper()


def _add_duty_methods(db_class):
    """将值班排班相关方法添加到 Database 类中"""

    # ============ Team 操作 ============

    def insert_duty_team(self, team: DutyTeam) -> None:
        """插入班组"""
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO duty_teams
                   (id, name, description, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (team.id, team.name, team.description, team.created_at, team.updated_at)
            )

    def update_duty_team(self, team: DutyTeam) -> None:
        """更新班组"""
        with self._conn() as conn:
            conn.execute(
                """UPDATE duty_teams SET
                   name=?, description=?, updated_at=?
                   WHERE id=?""",
                (team.name, team.description, team.updated_at, team.id)
            )

    def get_duty_team(self, team_id: str) -> Optional[DutyTeam]:
        """按ID获取班组"""
        _ensure_duty_tables(self)
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM duty_teams WHERE id = ?", (team_id,)
            ).fetchone()
            if not row:
                return None
            return DutyTeam(**dict(row))

    def get_duty_team_by_name(self, name: str) -> Optional[DutyTeam]:
        """按名称获取班组"""
        _ensure_duty_tables(self)
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM duty_teams WHERE name = ?", (name,)
            ).fetchone()
            if not row:
                return None
            return DutyTeam(**dict(row))

    def get_all_duty_teams(self) -> list[DutyTeam]:
        """获取所有班组"""
        _ensure_duty_tables(self)
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM duty_teams ORDER BY created_at DESC"
            ).fetchall()
            return [DutyTeam(**dict(r)) for r in rows]

    def duty_team_exists(self, team_id: str) -> bool:
        """检查班组是否存在"""
        _ensure_duty_tables(self)
        with self._conn() as conn:
            cur = conn.execute(
                "SELECT COUNT(*) FROM duty_teams WHERE id = ?", (team_id,)
            )
            return cur.fetchone()[0] > 0

    def duty_team_name_exists(self, name: str) -> bool:
        """检查班组名称是否存在"""
        _ensure_duty_tables(self)
        with self._conn() as conn:
            cur = conn.execute(
                "SELECT COUNT(*) FROM duty_teams WHERE name = ?", (name,)
            )
            return cur.fetchone()[0] > 0

    def delete_duty_team(self, team_id: str) -> None:
        """删除班组"""
        with self._conn() as conn:
            conn.execute("DELETE FROM duty_teams WHERE id = ?", (team_id,))

    # ============ Member 操作 ============

    def insert_duty_member(self, member: DutyMember) -> None:
        """插入值班人员"""
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO duty_members
                   (id, team_id, name, role, phone, email, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (member.id, member.team_id, member.name, member.role,
                 member.phone, member.email, member.created_at, member.updated_at)
            )

    def update_duty_member(self, member: DutyMember) -> None:
        """更新值班人员"""
        with self._conn() as conn:
            conn.execute(
                """UPDATE duty_members SET
                   team_id=?, name=?, role=?, phone=?, email=?, updated_at=?
                   WHERE id=?""",
                (member.team_id, member.name, member.role, member.phone,
                 member.email, member.updated_at, member.id)
            )

    def get_duty_member(self, member_id: str) -> Optional[DutyMember]:
        """按ID获取值班人员"""
        _ensure_duty_tables(self)
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM duty_members WHERE id = ?", (member_id,)
            ).fetchone()
            if not row:
                return None
            return DutyMember(**dict(row))

    def get_duty_member_by_name(self, team_id: str, name: str) -> Optional[DutyMember]:
        """按班组和姓名获取值班人员"""
        _ensure_duty_tables(self)
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM duty_members WHERE team_id = ? AND name = ?",
                (team_id, name)
            ).fetchone()
            if not row:
                return None
            return DutyMember(**dict(row))

    def get_duty_members_by_team(self, team_id: str) -> list[DutyMember]:
        """获取班组的所有成员"""
        _ensure_duty_tables(self)
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM duty_members WHERE team_id = ? ORDER BY name",
                (team_id,)
            ).fetchall()
            return [DutyMember(**dict(r)) for r in rows]

    def duty_member_exists(self, member_id: str) -> bool:
        """检查值班人员是否存在"""
        _ensure_duty_tables(self)
        with self._conn() as conn:
            cur = conn.execute(
                "SELECT COUNT(*) FROM duty_members WHERE id = ?", (member_id,)
            )
            return cur.fetchone()[0] > 0

    def delete_duty_member(self, member_id: str) -> None:
        """删除值班人员"""
        with self._conn() as conn:
            conn.execute("DELETE FROM duty_members WHERE id = ?", (member_id,))

    # ============ Schedule 操作 ============

    def insert_duty_schedule(self, schedule: DutySchedule) -> None:
        """插入排班记录"""
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO duty_schedules
                   (id, team_id, member_id, shift_type, schedule_date,
                    start_time, end_time, escalation_level, note, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (schedule.id, schedule.team_id, schedule.member_id,
                 schedule.shift_type, schedule.schedule_date,
                 schedule.start_time, schedule.end_time,
                 schedule.escalation_level, schedule.note,
                 schedule.created_at, schedule.updated_at)
            )

    def update_duty_schedule(self, schedule: DutySchedule) -> None:
        """更新排班记录"""
        with self._conn() as conn:
            conn.execute(
                """UPDATE duty_schedules SET
                   team_id=?, member_id=?, shift_type=?, schedule_date=?,
                   start_time=?, end_time=?, escalation_level=?, note=?, updated_at=?
                   WHERE id=?""",
                (schedule.team_id, schedule.member_id, schedule.shift_type,
                 schedule.schedule_date, schedule.start_time, schedule.end_time,
                 schedule.escalation_level, schedule.note, schedule.updated_at,
                 schedule.id)
            )

    def get_duty_schedule(self, schedule_id: str) -> Optional[DutySchedule]:
        """按ID获取排班记录"""
        _ensure_duty_tables(self)
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM duty_schedules WHERE id = ?", (schedule_id,)
            ).fetchone()
            if not row:
                return None
            return DutySchedule(**dict(row))

    def get_duty_schedules_by_date(self, team_id: str, schedule_date: str) -> list[DutySchedule]:
        """按日期获取班组排班"""
        _ensure_duty_tables(self)
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM duty_schedules
                   WHERE team_id = ? AND schedule_date = ?
                   ORDER BY start_time""",
                (team_id, schedule_date)
            ).fetchall()
            return [DutySchedule(**dict(r)) for r in rows]

    def get_duty_schedules_by_date_range(
        self, team_id: str, date_from: str, date_to: str
    ) -> list[DutySchedule]:
        """按日期范围获取班组排班"""
        _ensure_duty_tables(self)
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM duty_schedules
                   WHERE team_id = ? AND schedule_date BETWEEN ? AND ?
                   ORDER BY schedule_date, start_time""",
                (team_id, date_from, date_to)
            ).fetchall()
            return [DutySchedule(**dict(r)) for r in rows]

    def get_duty_schedules_by_member(
        self, member_id: str, date_from: str | None = None, date_to: str | None = None
    ) -> list[DutySchedule]:
        """获取人员的排班"""
        _ensure_duty_tables(self)
        conditions = ["member_id = ?"]
        params: list[Any] = [member_id]
        if date_from:
            conditions.append("schedule_date >= ?")
            params.append(date_from)
        if date_to:
            conditions.append("schedule_date <= ?")
            params.append(date_to)
        where_clause = " AND ".join(conditions)
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM duty_schedules WHERE {where_clause} ORDER BY schedule_date, start_time",
                params
            ).fetchall()
            return [DutySchedule(**dict(r)) for r in rows]

    def get_all_duty_schedules(self) -> list[DutySchedule]:
        """获取所有排班记录"""
        _ensure_duty_tables(self)
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM duty_schedules ORDER BY schedule_date DESC, start_time"
            ).fetchall()
            return [DutySchedule(**dict(r)) for r in rows]

    def get_all_duty_schedules_by_team(self, team_id: str) -> list[DutySchedule]:
        """获取班组的所有排班记录"""
        _ensure_duty_tables(self)
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM duty_schedules
                   WHERE team_id = ? ORDER BY schedule_date DESC, start_time""",
                (team_id,)
            ).fetchall()
            return [DutySchedule(**dict(r)) for r in rows]

    def find_conflicting_schedules(
        self, team_id: str, schedule_date: str,
        start_time: str, end_time: str, exclude_schedule_id: str | None = None
    ) -> list[DutySchedule]:
        """查找同一时段冲突的排班"""
        _ensure_duty_tables(self)
        conditions = [
            "team_id = ?",
            "schedule_date = ?",
            "start_time < ?",
            "end_time > ?",
        ]
        params: list[Any] = [team_id, schedule_date, end_time, start_time]
        if exclude_schedule_id:
            conditions.append("id != ?")
            params.append(exclude_schedule_id)
        where_clause = " AND ".join(conditions)
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM duty_schedules WHERE {where_clause} ORDER BY start_time",
                params
            ).fetchall()
            return [DutySchedule(**dict(r)) for r in rows]

    def duty_schedule_exists(self, schedule_id: str) -> bool:
        """检查排班记录是否存在"""
        _ensure_duty_tables(self)
        with self._conn() as conn:
            cur = conn.execute(
                "SELECT COUNT(*) FROM duty_schedules WHERE id = ?", (schedule_id,)
            )
            return cur.fetchone()[0] > 0

    def delete_duty_schedule(self, schedule_id: str) -> None:
        """删除排班记录"""
        with self._conn() as conn:
            conn.execute("DELETE FROM duty_schedules WHERE id = ?", (schedule_id,))

    # ============ Escalation Level 操作 ============

    def insert_duty_escalation_level(self, level: DutyEscalationLevel) -> None:
        """插入升级层级"""
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO duty_escalation_levels
                   (id, team_id, level, name, response_minutes, escalation_minutes, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (level.id, level.team_id, level.level, level.name,
                 level.response_minutes, level.escalation_minutes, level.created_at)
            )

    def get_duty_escalation_levels(self, team_id: str) -> list[DutyEscalationLevel]:
        """获取班组的所有升级层级"""
        _ensure_duty_tables(self)
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM duty_escalation_levels WHERE team_id = ? ORDER BY level",
                (team_id,)
            ).fetchall()
            return [DutyEscalationLevel(**dict(r)) for r in rows]

    def get_duty_escalation_level(self, team_id: str, level: int) -> Optional[DutyEscalationLevel]:
        """获取指定层级"""
        _ensure_duty_tables(self)
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM duty_escalation_levels WHERE team_id = ? AND level = ?",
                (team_id, level)
            ).fetchone()
            if not row:
                return None
            return DutyEscalationLevel(**dict(row))

    def delete_duty_escalation_levels(self, team_id: str) -> int:
        """删除班组的所有升级层级"""
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM duty_escalation_levels WHERE team_id = ?",
                (team_id,)
            )
            return cur.rowcount

    # ============ Time Window 操作 ============

    def insert_duty_time_window(self, window: DutyTimeWindow) -> None:
        """插入时间窗口"""
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO duty_time_windows
                   (id, team_id, name, start_time, end_time, days_of_week, priority, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (window.id, window.team_id, window.name, window.start_time,
                 window.end_time, window.days_of_week, window.priority, window.created_at)
            )

    def get_duty_time_windows(self, team_id: str) -> list[DutyTimeWindow]:
        """获取班组的所有时间窗口"""
        _ensure_duty_tables(self)
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM duty_time_windows WHERE team_id = ? ORDER BY priority DESC, start_time",
                (team_id,)
            ).fetchall()
            return [DutyTimeWindow(**dict(r)) for r in rows]

    def delete_duty_time_windows(self, team_id: str) -> int:
        """删除班组的所有时间窗口"""
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM duty_time_windows WHERE team_id = ?",
                (team_id,)
            )
            return cur.rowcount

    # ============ Handover 操作 ============

    def insert_duty_handover(self, handover: DutyHandover) -> None:
        """插入交班记录"""
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO duty_handovers
                   (id, team_id, from_member_id, to_member_id, operator_member_id,
                    schedule_id, handed_at, note, status, revoked_at, revoked_by,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (handover.id, handover.team_id, handover.from_member_id,
                 handover.to_member_id, handover.operator_member_id,
                 handover.schedule_id, handover.handed_at, handover.note,
                 handover.status, handover.revoked_at, handover.revoked_by,
                 handover.created_at, handover.updated_at)
            )

    def get_duty_handover(self, handover_id: str) -> Optional[DutyHandover]:
        """按ID获取交班记录"""
        _ensure_duty_tables(self)
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM duty_handovers WHERE id = ?", (handover_id,)
            ).fetchone()
            if not row:
                return None
            return DutyHandover(**dict(row))

    def get_last_duty_handover(self, team_id: str) -> Optional[DutyHandover]:
        """获取班组的最近一次生效中的交班记录"""
        _ensure_duty_tables(self)
        with self._conn() as conn:
            row = conn.execute(
                """SELECT * FROM duty_handovers
                   WHERE team_id = ? AND status = ?
                   ORDER BY handed_at DESC, rowid DESC LIMIT 1""",
                (team_id, DUTY_HANDOVER_STATUS_ACTIVE)
            ).fetchone()
            if not row:
                return None
            return DutyHandover(**dict(row))

    def get_most_recent_handover(self, team_id: str) -> Optional[DutyHandover]:
        """获取班组的最近一次交班记录（无论状态）"""
        _ensure_duty_tables(self)
        with self._conn() as conn:
            row = conn.execute(
                """SELECT * FROM duty_handovers
                   WHERE team_id = ?
                   ORDER BY handed_at DESC, rowid DESC LIMIT 1""",
                (team_id,)
            ).fetchone()
            if not row:
                return None
            return DutyHandover(**dict(row))

    def get_duty_handovers_by_schedule(self, schedule_id: str) -> list[DutyHandover]:
        """获取排班的所有交班记录"""
        _ensure_duty_tables(self)
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM duty_handovers WHERE schedule_id = ? ORDER BY handed_at DESC",
                (schedule_id,)
            ).fetchall()
            return [DutyHandover(**dict(r)) for r in rows]

    def get_duty_handovers_by_team(
        self, team_id: str, date_from: Optional[str] = None,
        date_to: Optional[str] = None, limit: int = 100
    ) -> list[DutyHandover]:
        """获取班组的交班记录"""
        _ensure_duty_tables(self)
        conditions = ["team_id = ?"]
        params: list[Any] = [team_id]

        if date_from:
            conditions.append("handed_at >= ?")
            params.append(date_from)
        if date_to:
            conditions.append("handed_at <= ?")
            params.append(date_to)

        where_clause = " AND ".join(conditions)
        params.append(limit)

        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM duty_handovers WHERE {where_clause} ORDER BY handed_at DESC LIMIT ?",
                params
            ).fetchall()
            return [DutyHandover(**dict(r)) for r in rows]

    def update_duty_handover(self, handover: DutyHandover) -> bool:
        """更新交班记录"""
        with self._conn() as conn:
            cur = conn.execute(
                """UPDATE duty_handovers SET
                   team_id=?, from_member_id=?, to_member_id=?, operator_member_id=?,
                   schedule_id=?, handed_at=?, note=?, status=?, revoked_at=?,
                   revoked_by=?, updated_at=?
                   WHERE id=?""",
                (handover.team_id, handover.from_member_id, handover.to_member_id,
                 handover.operator_member_id, handover.schedule_id, handover.handed_at,
                 handover.note, handover.status, handover.revoked_at,
                 handover.revoked_by, handover.updated_at, handover.id)
            )
            return cur.rowcount > 0

    def update_duty_handover_status(
        self, handover_id: str, status: str, revoked_by: str = "",
        revoked_at: str = ""
    ) -> bool:
        """更新交班记录状态"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        fields = ["status = ?", "updated_at = ?"]
        params = [status, now]

        if status == DUTY_HANDOVER_STATUS_REVOKED:
            fields.append("revoked_at = ?")
            params.append(revoked_at or now)
            fields.append("revoked_by = ?")
            params.append(revoked_by)

        params.append(handover_id)

        with self._conn() as conn:
            cur = conn.execute(
                f"UPDATE duty_handovers SET {', '.join(fields)} WHERE id = ?",
                params
            )
            return cur.rowcount > 0

    def duty_handover_exists(self, handover_id: str) -> bool:
        """检查交班记录是否存在"""
        _ensure_duty_tables(self)
        with self._conn() as conn:
            cur = conn.execute(
                "SELECT COUNT(*) FROM duty_handovers WHERE id = ?", (handover_id,)
            )
            return cur.fetchone()[0] > 0

    # ============ Escalation Log 操作 ============

    def insert_duty_escalation_log(self, log: DutyEscalationLog) -> None:
        """插入升级命中日志"""
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO duty_escalation_logs
                   (id, event_id, event_title, team_id, schedule_id,
                    member_id, escalation_level, event_time, hit_time,
                    status, handover_note, acknowledged_at, resolved_at,
                    escalated_to, escalated_at, note, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (log.id, log.event_id, log.event_title, log.team_id,
                 log.schedule_id, log.member_id, log.escalation_level,
                 log.event_time, log.hit_time, log.status, log.handover_note,
                 log.acknowledged_at, log.resolved_at, log.escalated_to,
                 log.escalated_at, log.note, log.created_at, log.updated_at)
            )

    def get_duty_escalation_log(self, log_id: str) -> Optional[DutyEscalationLog]:
        """按ID获取升级命中日志"""
        _ensure_duty_tables(self)
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM duty_escalation_logs WHERE id = ?", (log_id,)
            ).fetchone()
            if not row:
                return None
            return DutyEscalationLog(**dict(row))

    def get_duty_escalation_logs_by_event(self, event_id: str) -> list[DutyEscalationLog]:
        """获取事件的所有升级命中日志"""
        _ensure_duty_tables(self)
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM duty_escalation_logs WHERE event_id = ? ORDER BY hit_time DESC",
                (event_id,)
            ).fetchall()
            return [DutyEscalationLog(**dict(r)) for r in rows]

    def get_duty_escalation_logs_by_member(
        self, member_id: str, status: str | None = None, limit: int = 100
    ) -> list[DutyEscalationLog]:
        """获取人员的升级命中日志"""
        _ensure_duty_tables(self)
        conditions = ["member_id = ?"]
        params: list[Any] = [member_id]
        if status:
            conditions.append("status = ?")
            params.append(status)
        where_clause = " AND ".join(conditions)
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM duty_escalation_logs WHERE {where_clause} ORDER BY hit_time DESC LIMIT ?",
                params + [limit]
            ).fetchall()
            return [DutyEscalationLog(**dict(r)) for r in rows]

    def filter_duty_escalation_logs(
        self,
        event_ids: list[str] | None = None,
        team_ids: list[str] | None = None,
        member_ids: list[str] | None = None,
        statuses: list[str] | None = None,
        time_from: str | None = None,
        time_to: str | None = None,
    ) -> list[DutyEscalationLog]:
        """筛选升级命中日志"""
        _ensure_duty_tables(self)
        conditions: list[str] = []
        params: list[Any] = []

        if event_ids:
            placeholders = ",".join(["?"] * len(event_ids))
            conditions.append(f"event_id IN ({placeholders})")
            params.extend(event_ids)

        if team_ids:
            placeholders = ",".join(["?"] * len(team_ids))
            conditions.append(f"team_id IN ({placeholders})")
            params.extend(team_ids)

        if member_ids:
            placeholders = ",".join(["?"] * len(member_ids))
            conditions.append(f"member_id IN ({placeholders})")
            params.extend(member_ids)

        if statuses:
            placeholders = ",".join(["?"] * len(statuses))
            conditions.append(f"status IN ({placeholders})")
            params.extend(statuses)

        if time_from:
            conditions.append("hit_time >= ?")
            params.append(time_from)

        if time_to:
            conditions.append("hit_time <= ?")
            params.append(time_to)

        where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""

        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM duty_escalation_logs {where_clause} ORDER BY hit_time DESC",
                params
            ).fetchall()
            return [DutyEscalationLog(**dict(r)) for r in rows]

    def update_duty_escalation_log(self, log: DutyEscalationLog) -> bool:
        """更新升级命中日志"""
        with self._conn() as conn:
            cur = conn.execute(
                """UPDATE duty_escalation_logs SET
                   event_id=?, event_title=?, team_id=?, schedule_id=?,
                   member_id=?, escalation_level=?, event_time=?, hit_time=?,
                   status=?, handover_note=?, acknowledged_at=?, resolved_at=?,
                   escalated_to=?, escalated_at=?, note=?, updated_at=?
                   WHERE id=?""",
                (log.event_id, log.event_title, log.team_id, log.schedule_id,
                 log.member_id, log.escalation_level, log.event_time, log.hit_time,
                 log.status, log.handover_note, log.acknowledged_at, log.resolved_at,
                 log.escalated_to, log.escalated_at, log.note, log.updated_at,
                 log.id)
            )
            return cur.rowcount > 0

    def update_duty_escalation_log_status(
        self, log_id: str, status: str, note: str = ""
    ) -> bool:
        """更新升级命中日志状态"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        fields: list[str] = ["status = ?", "updated_at = ?"]
        params: list[Any] = [status, now]

        if status == DUTY_ESCALATION_STATUS_ACKNOWLEDGED:
            fields.append("acknowledged_at = ?")
            params.append(now)
        elif status == DUTY_ESCALATION_STATUS_RESOLVED:
            fields.append("resolved_at = ?")
            params.append(now)
        elif status == DUTY_ESCALATION_STATUS_CLOSED:
            fields.append("resolved_at = ?")
            params.append(now)

        if note:
            fields.append("note = ?")
            params.append(note)

        params.append(log_id)

        with self._conn() as conn:
            cur = conn.execute(
                f"UPDATE duty_escalation_logs SET {', '.join(fields)} WHERE id = ?",
                params
            )
            return cur.rowcount > 0

    def duty_escalation_log_exists(self, log_id: str) -> bool:
        """检查升级命中日志是否存在"""
        _ensure_duty_tables(self)
        with self._conn() as conn:
            cur = conn.execute(
                "SELECT COUNT(*) FROM duty_escalation_logs WHERE id = ?", (log_id,)
            )
            return cur.fetchone()[0] > 0

    db_class.insert_duty_team = insert_duty_team
    db_class.update_duty_team = update_duty_team
    db_class.get_duty_team = get_duty_team
    db_class.get_duty_team_by_name = get_duty_team_by_name
    db_class.get_all_duty_teams = get_all_duty_teams
    db_class.duty_team_exists = duty_team_exists
    db_class.duty_team_name_exists = duty_team_name_exists
    db_class.delete_duty_team = delete_duty_team

    db_class.insert_duty_member = insert_duty_member
    db_class.update_duty_member = update_duty_member
    db_class.get_duty_member = get_duty_member
    db_class.get_duty_member_by_name = get_duty_member_by_name
    db_class.get_duty_members_by_team = get_duty_members_by_team
    db_class.duty_member_exists = duty_member_exists
    db_class.delete_duty_member = delete_duty_member

    db_class.insert_duty_schedule = insert_duty_schedule
    db_class.update_duty_schedule = update_duty_schedule
    db_class.get_duty_schedule = get_duty_schedule
    db_class.get_duty_schedules_by_date = get_duty_schedules_by_date
    db_class.get_duty_schedules_by_date_range = get_duty_schedules_by_date_range
    db_class.get_duty_schedules_by_member = get_duty_schedules_by_member
    db_class.get_all_duty_schedules = get_all_duty_schedules
    db_class.get_all_duty_schedules_by_team = get_all_duty_schedules_by_team
    db_class.find_conflicting_schedules = find_conflicting_schedules
    db_class.duty_schedule_exists = duty_schedule_exists
    db_class.delete_duty_schedule = delete_duty_schedule

    db_class.insert_duty_escalation_level = insert_duty_escalation_level
    db_class.get_duty_escalation_levels = get_duty_escalation_levels
    db_class.get_duty_escalation_level = get_duty_escalation_level
    db_class.delete_duty_escalation_levels = delete_duty_escalation_levels

    db_class.insert_duty_time_window = insert_duty_time_window
    db_class.get_duty_time_windows = get_duty_time_windows
    db_class.delete_duty_time_windows = delete_duty_time_windows

    db_class.insert_duty_handover = insert_duty_handover
    db_class.get_duty_handover = get_duty_handover
    db_class.get_last_duty_handover = get_last_duty_handover
    db_class.get_most_recent_handover = get_most_recent_handover
    db_class.get_duty_handovers_by_schedule = get_duty_handovers_by_schedule
    db_class.get_duty_handovers_by_team = get_duty_handovers_by_team
    db_class.update_duty_handover = update_duty_handover
    db_class.update_duty_handover_status = update_duty_handover_status
    db_class.duty_handover_exists = duty_handover_exists

    db_class.insert_duty_escalation_log = insert_duty_escalation_log
    db_class.get_duty_escalation_log = get_duty_escalation_log
    db_class.get_duty_escalation_logs_by_event = get_duty_escalation_logs_by_event
    db_class.get_duty_escalation_logs_by_member = get_duty_escalation_logs_by_member
    db_class.filter_duty_escalation_logs = filter_duty_escalation_logs
    db_class.update_duty_escalation_log = update_duty_escalation_log
    db_class.update_duty_escalation_log_status = update_duty_escalation_log_status
    db_class.duty_escalation_log_exists = duty_escalation_log_exists

    return db_class


Database = _add_duty_methods(Database)


# ==================== 值班对账快照模块 ====================

SNAPSHOT_STATUS_ACTIVE = "active"
SNAPSHOT_STATUS_DELETED = "deleted"
SNAPSHOT_STATUS_IMPORTED = "imported"
SNAPSHOT_STATUS_ROLLED_BACK = "rolled_back"

SNAPSHOT_OP_GENERATE = "generate"
SNAPSHOT_OP_EXPORT = "export"
SNAPSHOT_OP_IMPORT = "import"
SNAPSHOT_OP_ROLLBACK = "rollback"
SNAPSHOT_OP_DELETE = "delete"
SNAPSHOT_OP_DIFF = "diff"

VALID_SNAPSHOT_OPERATIONS = {
    SNAPSHOT_OP_GENERATE, SNAPSHOT_OP_EXPORT, SNAPSHOT_OP_IMPORT,
    SNAPSHOT_OP_ROLLBACK, SNAPSHOT_OP_DELETE, SNAPSHOT_OP_DIFF,
}

SNAPSHOT_IMPORT_STATUS_SUCCESS = "success"
SNAPSHOT_IMPORT_STATUS_PARTIAL = "partial"
SNAPSHOT_IMPORT_STATUS_FAILED = "failed"


@dataclass
class DutySnapshot:
    """值班对账快照"""
    id: str
    team_id: str
    team_name: str
    snapshot_date: str
    snapshot_point: str
    operator: str
    status: str = SNAPSHOT_STATUS_ACTIVE
    note: str = ""
    source: str = "manual"
    checksum: str = ""
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.id,
            "team_id": self.team_id,
            "team_name": self.team_name,
            "snapshot_date": self.snapshot_date,
            "snapshot_point": self.snapshot_point,
            "operator": self.operator,
            "status": self.status,
            "note": self.note,
            "source": self.source,
            "checksum": self.checksum,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def to_export_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.id,
            "team_id": self.team_id,
            "team_name": self.team_name,
            "snapshot_date": self.snapshot_date,
            "snapshot_point": self.snapshot_point,
            "operator": self.operator,
            "status": self.status,
            "note": self.note,
            "source": self.source,
            "checksum": self.checksum,
            "created_at": self.created_at,
        }


@dataclass
class DutySnapshotContent:
    """快照内容（JSON序列化存储）"""
    snapshot_id: str
    team_info: dict[str, Any] = field(default_factory=dict)
    members: list[dict[str, Any]] = field(default_factory=list)
    schedules: list[dict[str, Any]] = field(default_factory=list)
    handovers: list[dict[str, Any]] = field(default_factory=list)
    escalation_logs: list[dict[str, Any]] = field(default_factory=list)
    escalation_levels: list[dict[str, Any]] = field(default_factory=list)
    time_windows: list[dict[str, Any]] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "team_info": self.team_info,
            "members": self.members,
            "schedules": self.schedules,
            "handovers": self.handovers,
            "escalation_logs": self.escalation_logs,
            "escalation_levels": self.escalation_levels,
            "time_windows": self.time_windows,
            "meta": self.meta,
        }


@dataclass
class DutySnapshotDiff:
    """两份快照的差异结果"""
    id: str
    snapshot_a_id: str
    snapshot_b_id: str
    team_id: str
    operator: str
    diff_summary_json: str = "{}"
    diff_detail_json: str = "{}"
    has_conflicts: bool = False
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        import json
        return {
            "diff_id": self.id,
            "snapshot_a_id": self.snapshot_a_id,
            "snapshot_b_id": self.snapshot_b_id,
            "team_id": self.team_id,
            "operator": self.operator,
            "summary": json.loads(self.diff_summary_json) if self.diff_summary_json else {},
            "detail": json.loads(self.diff_detail_json) if self.diff_detail_json else {},
            "has_conflicts": self.has_conflicts,
            "created_at": self.created_at,
        }


@dataclass
class DutySnapshotLog:
    """快照操作日志"""
    id: str
    operation: str
    operator: str
    team_id: str = ""
    snapshot_id: str = ""
    diff_id: str = ""
    status: str = ""
    detail: str = ""
    error_message: str = ""
    import_file: str = ""
    export_file: str = ""
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "log_id": self.id,
            "operation": self.operation,
            "operator": self.operator,
            "team_id": self.team_id,
            "snapshot_id": self.snapshot_id,
            "diff_id": self.diff_id,
            "status": self.status,
            "detail": self.detail,
            "error_message": self.error_message,
            "import_file": self.import_file,
            "export_file": self.export_file,
            "created_at": self.created_at,
        }


def _init_duty_snapshot_tables(conn: sqlite3.Connection) -> None:
    """初始化值班对账快照相关表"""
    statements = [
        """CREATE TABLE IF NOT EXISTS duty_snapshots (
            id TEXT PRIMARY KEY,
            team_id TEXT NOT NULL,
            team_name TEXT NOT NULL,
            snapshot_date TEXT NOT NULL,
            snapshot_point TEXT NOT NULL,
            operator TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            note TEXT DEFAULT '',
            source TEXT DEFAULT 'manual',
            checksum TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_duty_snapshot_team_date ON duty_snapshots(team_id, snapshot_date)",
        "CREATE INDEX IF NOT EXISTS idx_duty_snapshot_status ON duty_snapshots(status)",
        "CREATE INDEX IF NOT EXISTS idx_duty_snapshot_created ON duty_snapshots(created_at DESC)",
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_duty_snapshot_unique
            ON duty_snapshots(team_id, snapshot_date, snapshot_point)
            WHERE status NOT IN ('deleted', 'rolled_back')""",
        """CREATE TABLE IF NOT EXISTS duty_snapshot_contents (
            snapshot_id TEXT PRIMARY KEY,
            team_info_json TEXT DEFAULT '{}',
            members_json TEXT DEFAULT '[]',
            schedules_json TEXT DEFAULT '[]',
            handovers_json TEXT DEFAULT '[]',
            escalation_logs_json TEXT DEFAULT '[]',
            escalation_levels_json TEXT DEFAULT '[]',
            time_windows_json TEXT DEFAULT '[]',
            meta_json TEXT DEFAULT '{}',
            created_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS duty_snapshot_diffs (
            id TEXT PRIMARY KEY,
            snapshot_a_id TEXT NOT NULL,
            snapshot_b_id TEXT NOT NULL,
            team_id TEXT NOT NULL,
            operator TEXT NOT NULL,
            diff_summary_json TEXT DEFAULT '{}',
            diff_detail_json TEXT DEFAULT '{}',
            has_conflicts INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_duty_snapshot_diff_team ON duty_snapshot_diffs(team_id)",
        "CREATE INDEX IF NOT EXISTS idx_duty_snapshot_diff_created ON duty_snapshot_diffs(created_at DESC)",
        """CREATE TABLE IF NOT EXISTS duty_snapshot_logs (
            id TEXT PRIMARY KEY,
            operation TEXT NOT NULL,
            operator TEXT NOT NULL,
            team_id TEXT DEFAULT '',
            snapshot_id TEXT DEFAULT '',
            diff_id TEXT DEFAULT '',
            status TEXT DEFAULT '',
            detail TEXT DEFAULT '',
            error_message TEXT DEFAULT '',
            import_file TEXT DEFAULT '',
            export_file TEXT DEFAULT '',
            created_at TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_duty_snapshot_log_op ON duty_snapshot_logs(operation)",
        "CREATE INDEX IF NOT EXISTS idx_duty_snapshot_log_created ON duty_snapshot_logs(created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_duty_snapshot_log_team ON duty_snapshot_logs(team_id)",
    ]
    for stmt in statements:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass


def _ensure_snapshot_tables(db: "Database") -> None:
    """确保快照表存在"""
    with db._conn() as conn:
        _init_duty_snapshot_tables(conn)


def _add_snapshot_tables_to_init(db_class):
    """将快照表初始化添加到 Database._init_db 中"""
    original_init = db_class._init_db

    def new_init_db(self):
        original_init(self)
        with self._conn() as conn:
            _init_duty_snapshot_tables(conn)

    db_class._init_db = new_init_db
    return db_class


Database = _add_snapshot_tables_to_init(Database)


def _generate_snapshot_id(prefix: str) -> str:
    """生成快照相关ID"""
    return prefix + uuid.uuid4().hex[:12].upper()


def _add_snapshot_methods(db_class):
    """将快照相关方法添加到 Database 类中"""
    import json as _json

    # ============ Snapshot 操作 ============

    def insert_duty_snapshot(self, snapshot: DutySnapshot,
                             content: DutySnapshotContent | None = None) -> None:
        """插入快照及其内容"""
        _ensure_snapshot_tables(self)
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO duty_snapshots
                   (id, team_id, team_name, snapshot_date, snapshot_point,
                    operator, status, note, source, checksum, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (snapshot.id, snapshot.team_id, snapshot.team_name,
                 snapshot.snapshot_date, snapshot.snapshot_point,
                 snapshot.operator, snapshot.status, snapshot.note,
                 snapshot.source, snapshot.checksum,
                 snapshot.created_at, snapshot.updated_at)
            )
            if content is not None:
                conn.execute(
                    """INSERT INTO duty_snapshot_contents
                       (snapshot_id, team_info_json, members_json, schedules_json,
                        handovers_json, escalation_logs_json, escalation_levels_json,
                        time_windows_json, meta_json, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (content.snapshot_id,
                     _json.dumps(content.team_info, ensure_ascii=False),
                     _json.dumps(content.members, ensure_ascii=False),
                     _json.dumps(content.schedules, ensure_ascii=False),
                     _json.dumps(content.handovers, ensure_ascii=False),
                     _json.dumps(content.escalation_logs, ensure_ascii=False),
                     _json.dumps(content.escalation_levels, ensure_ascii=False),
                     _json.dumps(content.time_windows, ensure_ascii=False),
                     _json.dumps(content.meta, ensure_ascii=False),
                     snapshot.created_at)
                )

    def update_duty_snapshot(self, snapshot: DutySnapshot) -> None:
        """更新快照元数据"""
        _ensure_snapshot_tables(self)
        with self._conn() as conn:
            conn.execute(
                """UPDATE duty_snapshots SET
                   team_id=?, team_name=?, snapshot_date=?, snapshot_point=?,
                   operator=?, status=?, note=?, source=?, checksum=?, updated_at=?
                   WHERE id=?""",
                (snapshot.team_id, snapshot.team_name, snapshot.snapshot_date,
                 snapshot.snapshot_point, snapshot.operator, snapshot.status,
                 snapshot.note, snapshot.source, snapshot.checksum,
                 snapshot.updated_at, snapshot.id)
            )

    def get_duty_snapshot(self, snapshot_id: str) -> Optional[DutySnapshot]:
        """按ID获取快照"""
        _ensure_snapshot_tables(self)
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM duty_snapshots WHERE id = ?", (snapshot_id,)
            ).fetchone()
            if not row:
                return None
            return DutySnapshot(**dict(row))

    def get_duty_snapshot_content(self, snapshot_id: str) -> Optional[DutySnapshotContent]:
        """获取快照内容"""
        _ensure_snapshot_tables(self)
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM duty_snapshot_contents WHERE snapshot_id = ?",
                (snapshot_id,)
            ).fetchone()
            if not row:
                return None
            d = dict(row)
            return DutySnapshotContent(
                snapshot_id=d["snapshot_id"],
                team_info=_json.loads(d["team_info_json"]) if d["team_info_json"] else {},
                members=_json.loads(d["members_json"]) if d["members_json"] else [],
                schedules=_json.loads(d["schedules_json"]) if d["schedules_json"] else [],
                handovers=_json.loads(d["handovers_json"]) if d["handovers_json"] else [],
                escalation_logs=_json.loads(d["escalation_logs_json"]) if d["escalation_logs_json"] else [],
                escalation_levels=_json.loads(d["escalation_levels_json"]) if d["escalation_levels_json"] else [],
                time_windows=_json.loads(d["time_windows_json"]) if d["time_windows_json"] else [],
                meta=_json.loads(d["meta_json"]) if d["meta_json"] else {},
            )

    def filter_duty_snapshots(self, team_id: str | None = None,
                               snapshot_date: str | None = None,
                               date_from: str | None = None,
                               date_to: str | None = None,
                               operator: str | None = None,
                               status: str | None = None,
                               limit: int | None = None) -> list[DutySnapshot]:
        """按条件过滤快照"""
        _ensure_snapshot_tables(self)
        conditions: list[str] = []
        params: list[Any] = []

        if team_id:
            conditions.append("team_id = ?")
            params.append(team_id)
        if snapshot_date:
            conditions.append("snapshot_date = ?")
            params.append(snapshot_date)
        if date_from:
            conditions.append("snapshot_date >= ?")
            params.append(date_from)
        if date_to:
            conditions.append("snapshot_date <= ?")
            params.append(date_to)
        if operator:
            conditions.append("operator = ?")
            params.append(operator)
        if status:
            conditions.append("status = ?")
            params.append(status)

        where_clause = ""
        if conditions:
            where_clause = "WHERE " + " AND ".join(conditions)

        sql = f"""SELECT * FROM duty_snapshots {where_clause}
                  ORDER BY created_at DESC"""
        if limit:
            sql += f" LIMIT {int(limit)}"

        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [DutySnapshot(**dict(r)) for r in rows]

    def get_snapshots_by_team(self, team_id: str,
                               limit: int | None = None) -> list[DutySnapshot]:
        """获取班组的所有快照"""
        return self.filter_duty_snapshots(team_id=team_id, limit=limit)

    def get_snapshots_by_date(self, snapshot_date: str,
                               team_id: str | None = None,
                               limit: int | None = None) -> list[DutySnapshot]:
        """按日期获取快照"""
        return self.filter_duty_snapshots(
            team_id=team_id, snapshot_date=snapshot_date, limit=limit
        )

    def duty_snapshot_exists(self, snapshot_id: str) -> bool:
        """检查快照是否存在"""
        _ensure_snapshot_tables(self)
        with self._conn() as conn:
            cur = conn.execute(
                "SELECT COUNT(*) FROM duty_snapshots WHERE id = ?", (snapshot_id,)
            )
            return cur.fetchone()[0] > 0

    def duty_snapshot_unique_exists(self, team_id: str, snapshot_date: str,
                                     snapshot_point: str) -> bool:
        """检查唯一约束的快照是否存在"""
        _ensure_snapshot_tables(self)
        with self._conn() as conn:
            cur = conn.execute(
                """SELECT COUNT(*) FROM duty_snapshots
                   WHERE team_id = ? AND snapshot_date = ? AND snapshot_point = ?
                   AND status != 'deleted'""",
                (team_id, snapshot_date, snapshot_point)
            )
            return cur.fetchone()[0] > 0

    def count_team_snapshots(self, team_id: str, status: str | None = None) -> int:
        """统计班组的快照数量"""
        _ensure_snapshot_tables(self)
        conditions = ["team_id = ?"]
        params: list[Any] = [team_id]
        if status:
            conditions.append("status = ?")
            params.append(status)
        where = " AND ".join(conditions)
        with self._conn() as conn:
            cur = conn.execute(
                f"SELECT COUNT(*) FROM duty_snapshots WHERE {where}", params
            )
            return cur.fetchone()[0]

    def delete_oldest_snapshots(self, team_id: str, keep_count: int) -> int:
        """删除超出保留份数的最旧快照（软删除）"""
        _ensure_snapshot_tables(self)
        if keep_count <= 0:
            return 0
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT id FROM duty_snapshots
                   WHERE team_id = ? AND status = 'active'
                   ORDER BY created_at DESC""",
                (team_id,)
            ).fetchall()
            if len(rows) <= keep_count:
                return 0
            to_delete = [r[0] for r in rows[keep_count:]]
            if not to_delete:
                return 0
            placeholders = ",".join(["?"] * len(to_delete))
            conn.execute(
                f"""UPDATE duty_snapshots SET status = 'deleted', updated_at = ?
                    WHERE id IN ({placeholders})""",
                [now] + to_delete
            )
            return len(to_delete)

    def delete_duty_snapshot(self, snapshot_id: str, operator: str = "") -> bool:
        """软删除快照"""
        _ensure_snapshot_tables(self)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if not operator:
            snap = self.get_duty_snapshot(snapshot_id)
            if snap:
                operator = snap.operator
        with self._conn() as conn:
            cur = conn.execute(
                """UPDATE duty_snapshots SET status = 'deleted', operator = ?, updated_at = ?
                   WHERE id = ? AND status != 'deleted'""",
                (operator, now, snapshot_id)
            )
            return cur.rowcount > 0

    # ============ Snapshot Diff 操作 ============

    def insert_duty_snapshot_diff(self, diff: DutySnapshotDiff) -> None:
        """插入快照差异记录"""
        _ensure_snapshot_tables(self)
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO duty_snapshot_diffs
                   (id, snapshot_a_id, snapshot_b_id, team_id, operator,
                    diff_summary_json, diff_detail_json, has_conflicts, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (diff.id, diff.snapshot_a_id, diff.snapshot_b_id, diff.team_id,
                 diff.operator, diff.diff_summary_json, diff.diff_detail_json,
                 1 if diff.has_conflicts else 0, diff.created_at)
            )

    def get_duty_snapshot_diff(self, diff_id: str) -> Optional[DutySnapshotDiff]:
        """按ID获取差异记录"""
        _ensure_snapshot_tables(self)
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM duty_snapshot_diffs WHERE id = ?", (diff_id,)
            ).fetchone()
            if not row:
                return None
            d = dict(row)
            return DutySnapshotDiff(
                id=d["id"],
                snapshot_a_id=d["snapshot_a_id"],
                snapshot_b_id=d["snapshot_b_id"],
                team_id=d["team_id"],
                operator=d["operator"],
                diff_summary_json=d["diff_summary_json"],
                diff_detail_json=d["diff_detail_json"],
                has_conflicts=bool(d["has_conflicts"]),
                created_at=d["created_at"],
            )

    def list_snapshot_diffs(self, team_id: str | None = None,
                             snapshot_id: str | None = None,
                             limit: int = 20) -> list[DutySnapshotDiff]:
        """列出差异记录"""
        _ensure_snapshot_tables(self)
        conditions: list[str] = []
        params: list[Any] = []
        if team_id:
            conditions.append("team_id = ?")
            params.append(team_id)
        if snapshot_id:
            conditions.append("(snapshot_a_id = ? OR snapshot_b_id = ?)")
            params.extend([snapshot_id, snapshot_id])
        where = ""
        if conditions:
            where = "WHERE " + " AND ".join(conditions)
        sql = f"""SELECT * FROM duty_snapshot_diffs {where}
                  ORDER BY created_at DESC LIMIT ?"""
        params.append(limit)
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
            result: list[DutySnapshotDiff] = []
            for r in rows:
                d = dict(r)
                result.append(DutySnapshotDiff(
                    id=d["id"],
                    snapshot_a_id=d["snapshot_a_id"],
                    snapshot_b_id=d["snapshot_b_id"],
                    team_id=d["team_id"],
                    operator=d["operator"],
                    diff_summary_json=d["diff_summary_json"],
                    diff_detail_json=d["diff_detail_json"],
                    has_conflicts=bool(d["has_conflicts"]),
                    created_at=d["created_at"],
                ))
            return result

    # ============ Snapshot Log 操作 ============

    def insert_duty_snapshot_log(self, log: DutySnapshotLog) -> None:
        """插入操作日志"""
        _ensure_snapshot_tables(self)
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO duty_snapshot_logs
                   (id, operation, operator, team_id, snapshot_id, diff_id,
                    status, detail, error_message, import_file, export_file, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (log.id, log.operation, log.operator, log.team_id, log.snapshot_id,
                 log.diff_id, log.status, log.detail, log.error_message,
                 log.import_file, log.export_file, log.created_at)
            )

    def list_snapshot_logs(self, team_id: str | None = None,
                            operation: str | None = None,
                            operator: str | None = None,
                            limit: int = 50) -> list[DutySnapshotLog]:
        """列出操作日志"""
        _ensure_snapshot_tables(self)
        conditions: list[str] = []
        params: list[Any] = []
        if team_id:
            conditions.append("team_id = ?")
            params.append(team_id)
        if operation:
            conditions.append("operation = ?")
            params.append(operation)
        if operator:
            conditions.append("operator = ?")
            params.append(operator)
        where = ""
        if conditions:
            where = "WHERE " + " AND ".join(conditions)
        sql = f"""SELECT * FROM duty_snapshot_logs {where}
                  ORDER BY created_at DESC LIMIT ?"""
        params.append(limit)
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [DutySnapshotLog(**dict(r)) for r in rows]

    def get_snapshot_log(self, log_id: str) -> Optional[DutySnapshotLog]:
        """按ID获取操作日志"""
        _ensure_snapshot_tables(self)
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM duty_snapshot_logs WHERE id = ?", (log_id,)
            ).fetchone()
            if not row:
                return None
            return DutySnapshotLog(**dict(row))

    def get_last_failed_import_log(self, team_id: str | None = None) -> Optional[DutySnapshotLog]:
        """获取最近一次失败或部分成功的导入日志（用于回滚）"""
        _ensure_snapshot_tables(self)
        conditions = [
            "operation = ?",
            "status IN (?, ?)",
        ]
        params: list[Any] = [
            SNAPSHOT_OP_IMPORT,
            SNAPSHOT_IMPORT_STATUS_FAILED,
            SNAPSHOT_IMPORT_STATUS_PARTIAL,
        ]
        if team_id:
            conditions.append("team_id = ?")
            params.append(team_id)
        where = "WHERE " + " AND ".join(conditions)
        with self._conn() as conn:
            row = conn.execute(
                f"""SELECT * FROM duty_snapshot_logs {where}
                    ORDER BY created_at DESC LIMIT 1""",
                params
            ).fetchone()
            if not row:
                return None
            return DutySnapshotLog(**dict(row))

    def cleanup_old_snapshot_logs(self, days: int) -> int:
        """清理N天前的操作日志"""
        if days <= 0:
            return 0
        from datetime import timedelta as _td
        cutoff = (datetime.now() - _td(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM duty_snapshot_logs WHERE created_at < ?",
                (cutoff,)
            )
            return cur.rowcount

    # ============ 绑定到类 ============

    db_class.insert_duty_snapshot = insert_duty_snapshot
    db_class.update_duty_snapshot = update_duty_snapshot
    db_class.get_duty_snapshot = get_duty_snapshot
    db_class.get_duty_snapshot_content = get_duty_snapshot_content
    db_class.filter_duty_snapshots = filter_duty_snapshots
    db_class.get_snapshots_by_team = get_snapshots_by_team
    db_class.get_snapshots_by_date = get_snapshots_by_date
    db_class.duty_snapshot_exists = duty_snapshot_exists
    db_class.duty_snapshot_unique_exists = duty_snapshot_unique_exists
    db_class.count_team_snapshots = count_team_snapshots
    db_class.delete_oldest_snapshots = delete_oldest_snapshots
    db_class.delete_duty_snapshot = delete_duty_snapshot

    db_class.insert_duty_snapshot_diff = insert_duty_snapshot_diff
    db_class.get_duty_snapshot_diff = get_duty_snapshot_diff
    db_class.list_snapshot_diffs = list_snapshot_diffs

    db_class.insert_duty_snapshot_log = insert_duty_snapshot_log
    db_class.list_snapshot_logs = list_snapshot_logs
    db_class.get_snapshot_log = get_snapshot_log
    db_class.get_last_failed_import_log = get_last_failed_import_log
    db_class.cleanup_old_snapshot_logs = cleanup_old_snapshot_logs

    return db_class


Database = _add_snapshot_methods(Database)
