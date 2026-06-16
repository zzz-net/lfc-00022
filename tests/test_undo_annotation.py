"""测试：撤销标注链路完整性"""
from __future__ import annotations

import csv
import json
import os
import tempfile
import unittest

from inspection_cli.annotation import AnnotationError, AnnotationManager
from inspection_cli.config import AppConfig
from inspection_cli.database import Database, Event
from inspection_cli.exporter import Exporter


class _TestBase(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp_dir, "test.db")
        self.db = Database(self.db_path)
        self.config = AppConfig(db_path=self.db_path)
        self.manager = AnnotationManager(self.db)
        self.exporter = Exporter(self.db, self.config)

    def _insert_event(self, event_id: str = "EVT-TEST001") -> str:
        event = Event(
            id=event_id,
            device_id="DEV-A001",
            first_seen="2026-06-15 08:30:00",
            last_seen="2026-06-15 09:10:00",
            issue_type="temperature",
            severity="critical",
            status="unconfirmed",
        )
        self.db.insert_event(event)
        return event_id

    def _export_csv_rows(self) -> list[dict]:
        path = os.path.join(self.tmp_dir, "out.csv")
        self.exporter.export_events(path, fmt="csv")
        with open(path, encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            return list(reader)

    def _export_json_events(self) -> list[dict]:
        path = os.path.join(self.tmp_dir, "out.json")
        self.exporter.export_events(path, fmt="json")
        with open(path, encoding="utf-8") as f:
            return json.load(f)["events"]


class TestUndoRestoresFullSnapshot(_TestBase):
    """复现: Alice/first-note → Bob/second-note → undo → 导出应看到 Alice/first-note"""

    def test_double_annotate_then_undo_csv(self):
        eid = self._insert_event()

        self.manager.annotate(eid, "false_positive", "Alice", "first-note")
        self.manager.annotate(eid, "closed", "Bob", "second-note")
        self.manager.undo(eid)

        rows = self._export_csv_rows()
        row = rows[0]
        self.assertEqual(row["status"], "false_positive")
        self.assertEqual(row["handler"], "Alice")
        self.assertEqual(row["note"], "first-note")

    def test_double_annotate_then_undo_json(self):
        eid = self._insert_event()

        self.manager.annotate(eid, "false_positive", "Alice", "first-note")
        self.manager.annotate(eid, "closed", "Bob", "second-note")
        self.manager.undo(eid)

        events = self._export_json_events()
        ev = events[0]
        self.assertEqual(ev["status"], "false_positive")
        self.assertEqual(ev["handler"], "Alice")
        self.assertEqual(ev["note"], "first-note")

    def test_double_annotate_then_undo_event_object(self):
        eid = self._insert_event()

        self.manager.annotate(eid, "false_positive", "Alice", "first-note")
        self.manager.annotate(eid, "closed", "Bob", "second-note")
        self.manager.undo(eid)

        event = self.db.get_event(eid)
        self.assertEqual(event.status, "false_positive")
        self.assertEqual(event.handler, "Alice")
        self.assertEqual(event.note, "first-note")

    def test_single_annotate_then_undo_clears_handler_note(self):
        eid = self._insert_event()

        self.manager.annotate(eid, "confirmed", "Alice", "first-note")
        self.manager.undo(eid)

        event = self.db.get_event(eid)
        self.assertEqual(event.status, "unconfirmed")
        self.assertEqual(event.handler, "")
        self.assertEqual(event.note, "")

        rows = self._export_csv_rows()
        self.assertEqual(rows[0]["handler"], "")
        self.assertEqual(rows[0]["note"], "")

    def test_triple_annotate_then_undo_once(self):
        eid = self._insert_event()

        self.manager.annotate(eid, "false_positive", "Alice", "first")
        self.manager.annotate(eid, "confirmed", "Bob", "second")
        self.manager.annotate(eid, "closed", "Carol", "third")
        self.manager.undo(eid)

        event = self.db.get_event(eid)
        self.assertEqual(event.status, "confirmed")
        self.assertEqual(event.handler, "Bob")
        self.assertEqual(event.note, "second")

    def test_triple_annotate_then_undo_twice(self):
        eid = self._insert_event()

        self.manager.annotate(eid, "false_positive", "Alice", "first")
        self.manager.annotate(eid, "confirmed", "Bob", "second")
        self.manager.annotate(eid, "closed", "Carol", "third")

        self.manager.undo(eid)
        event = self.db.get_event(eid)
        self.assertEqual(event.status, "confirmed")
        self.assertEqual(event.handler, "Bob")
        self.assertEqual(event.note, "second")

        self.manager.undo(eid)
        event = self.db.get_event(eid)
        self.assertEqual(event.status, "false_positive")
        self.assertEqual(event.handler, "Alice")
        self.assertEqual(event.note, "first")


class TestUndoAcrossRestart(_TestBase):
    """回归: 跨重启后再次导出仍正确"""

    def test_persistence_after_reopen(self):
        eid = self._insert_event()

        self.manager.annotate(eid, "false_positive", "Alice", "first-note")
        self.manager.annotate(eid, "closed", "Bob", "second-note")
        self.manager.undo(eid)

        rows_before = self._export_csv_rows()

        self.db = Database(self.db_path)
        self.manager = AnnotationManager(self.db)
        self.exporter = Exporter(self.db, self.config)

        event = self.db.get_event(eid)
        self.assertEqual(event.status, "false_positive")
        self.assertEqual(event.handler, "Alice")
        self.assertEqual(event.note, "first-note")

        rows_after = self._export_csv_rows()
        self.assertEqual(rows_before, rows_after)

    def test_annotation_history_intact_after_reopen(self):
        eid = self._insert_event()

        self.manager.annotate(eid, "false_positive", "Alice", "first-note")
        self.manager.annotate(eid, "closed", "Bob", "second-note")
        self.manager.undo(eid)

        self.db = Database(self.db_path)
        annotations = self.db.get_annotations_for_event(eid)
        self.assertEqual(len(annotations), 1)
        self.assertEqual(annotations[0].handler, "Alice")
        self.assertEqual(annotations[0].note, "first-note")
        self.assertEqual(annotations[0].new_status, "false_positive")


class TestUndoWithoutHistory(_TestBase):
    """确认: 没有标注历史时 undo 的错误提示不被改坏"""

    def test_undo_unannotated_event(self):
        eid = self._insert_event()

        with self.assertRaises(AnnotationError) as ctx:
            self.manager.undo(eid)

        msg = str(ctx.exception)
        self.assertIn("没有标注历史", msg)
        self.assertIn("无法撤销", msg)
        self.assertIn("尚未进行过任何标注操作", msg)

    def test_undo_after_single_annotate_then_undo(self):
        eid = self._insert_event()

        self.manager.annotate(eid, "confirmed", "Alice", "note")
        self.manager.undo(eid)

        with self.assertRaises(AnnotationError) as ctx:
            self.manager.undo(eid)

        msg = str(ctx.exception)
        self.assertIn("没有标注历史", msg)
        self.assertIn("尚未进行过任何标注操作", msg)

    def test_undo_nonexistent_event(self):
        with self.assertRaises(AnnotationError) as ctx:
            self.manager.undo("EVT-NONEXIST")

        self.assertIn("事件不存在", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
