"""持久化层：SQLite 数据库操作"""
from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterator, Optional


VALID_STATUSES = {"unconfirmed", "confirmed", "false_positive", "closed"}


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
                    record_count INTEGER DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS idx_events_device
                    ON events(device_id);
                CREATE INDEX IF NOT EXISTS idx_events_status
                    ON events(status);

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
                    severity, status, handler, note, record_count)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (event.id, event.device_id, event.first_seen, event.last_seen,
                 event.issue_type, event.severity, event.status,
                 event.handler, event.note, event.record_count)
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
                   severity=?, status=?, handler=?, note=?, record_count=?
                   WHERE id=?""",
                (event.device_id, event.first_seen, event.last_seen,
                 event.issue_type, event.severity, event.status,
                 event.handler, event.note, event.record_count, event.id)
            )

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
                   ORDER BY annotate_time DESC, id DESC LIMIT 1""",
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

    def get_annotation_count(self, event_id: str) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                "SELECT COUNT(*) FROM annotations WHERE event_id = ?", (event_id,)
            )
            return cur.fetchone()[0]
