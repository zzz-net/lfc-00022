"""测试：批量操作功能完整性"""
from __future__ import annotations

import csv
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta

from inspection_cli.annotation import AnnotationManager
from inspection_cli.batch import (
    BatchFilter, BatchOperationError, BatchOperationManager, BatchUpdate,
    CONFLICT_STRATEGY_ABORT, CONFLICT_STRATEGY_FORCE, CONFLICT_STRATEGY_SKIP,
)
from inspection_cli.config import AppConfig
from inspection_cli.database import (
    BATCH_STATUS_COMPLETED, ITEM_STATUS_CONFLICT, ITEM_STATUS_SKIPPED,
    ITEM_STATUS_SUCCESS, Database, Event,
)
from inspection_cli.exporter import Exporter
from inspection_cli.importer import RecordImporter
from inspection_cli.merger import EventMerger


class _TestBase(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp_dir, "test.db")
        self.db = Database(self.db_path)
        self.config = AppConfig(db_path=self.db_path)
        self.manager = AnnotationManager(self.db)
        self.batch_manager = BatchOperationManager(self.db, self.config)
        self.exporter = Exporter(self.db, self.config)
        self.importer = RecordImporter(self.db, self.config)
        self.merger = EventMerger(self.db, self.config)

    def _insert_event(self, event_id: str, device_id: str = "DEV-A001",
                      status: str = "unconfirmed", handler: str = "",
                      note: str = "", version: int = 1) -> str:
        event = Event(
            id=event_id,
            device_id=device_id,
            first_seen="2026-06-15 08:30:00",
            last_seen="2026-06-15 09:10:00",
            issue_type="temperature",
            severity="critical",
            status=status,
            handler=handler,
            note=note,
            version=version,
        )
        self.db.insert_event(event)
        return event_id

    def _insert_multiple_events(self, count: int = 5,
                                device_ids: list[str] | None = None,
                                statuses: list[str] | None = None) -> list[str]:
        if device_ids is None:
            device_ids = ["DEV-A001", "DEV-A002", "DEV-B001"]
        if statuses is None:
            statuses = ["unconfirmed"] * 5
        event_ids = []
        for i in range(count):
            eid = f"EVT-TEST{i:03d}"
            self._insert_event(
                event_id=eid,
                device_id=device_ids[i % len(device_ids)],
                status=statuses[i % len(statuses)],
                handler=f"User{i % 3}",
                note=f"Note {i}",
            )
            event_ids.append(eid)
        return event_ids

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


class TestBatchFilterAndPreview(_TestBase):
    """测试批量筛选和预览功能"""

    def test_filter_by_event_ids(self):
        eids = self._insert_multiple_events(5)
        batch_filter = BatchFilter(event_ids=[eids[0], eids[2]])
        events = self.batch_manager.preview(batch_filter)
        self.assertEqual(len(events), 2)
        self.assertEqual({e.id for e in events}, {eids[0], eids[2]})

    def test_filter_by_device_ids(self):
        self._insert_multiple_events(5)
        batch_filter = BatchFilter(device_ids=["DEV-A001"])
        events = self.batch_manager.preview(batch_filter)
        self.assertEqual(len(events), 2)
        for e in events:
            self.assertEqual(e.device_id, "DEV-A001")

    def test_filter_by_statuses(self):
        self._insert_multiple_events(5, statuses=[
            "unconfirmed", "unconfirmed", "confirmed",
            "false_positive", "closed"
        ])
        batch_filter = BatchFilter(statuses=["unconfirmed", "confirmed"])
        events = self.batch_manager.preview(batch_filter)
        self.assertEqual(len(events), 3)
        for e in events:
            self.assertIn(e.status, ["unconfirmed", "confirmed"])

    def test_filter_by_time_window(self):
        self._insert_multiple_events(3)
        batch_filter = BatchFilter(
            time_from="2026-06-15 08:00:00",
            time_to="2026-06-15 10:00:00"
        )
        events = self.batch_manager.preview(batch_filter)
        self.assertEqual(len(events), 3)

    def test_filter_combined(self):
        eids = self._insert_multiple_events(5, statuses=[
            "unconfirmed", "confirmed", "unconfirmed",
            "false_positive", "closed"
        ])
        batch_filter = BatchFilter(
            device_ids=["DEV-A001"],
            statuses=["unconfirmed"]
        )
        events = self.batch_manager.preview(batch_filter)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].id, eids[0])

    def test_filter_invalid_status(self):
        batch_filter = BatchFilter(statuses=["invalid_status"])
        with self.assertRaises(BatchOperationError) as ctx:
            self.batch_manager.preview(batch_filter)
        self.assertIn("无效的状态筛选值", str(ctx.exception))

    def test_preview_formatting(self):
        eids = self._insert_multiple_events(3)
        batch_filter = BatchFilter(event_ids=eids)
        batch_update = BatchUpdate(status="confirmed", handler="BatchUser")
        events = self.batch_manager.preview(batch_filter)
        preview = self.batch_manager.format_preview(events, batch_filter, batch_update)
        formatted = preview.formatted()
        self.assertIn("共 3 个事件将被修改", formatted)
        self.assertIn("状态 → 已确认", formatted)
        self.assertIn("处理人 → BatchUser", formatted)
        for eid in eids:
            self.assertIn(eid, formatted)


class TestBatchExecuteBasic(_TestBase):
    """测试批量执行基本功能"""

    def test_batch_update_status(self):
        eids = self._insert_multiple_events(3)
        batch_filter = BatchFilter(event_ids=eids)
        batch_update = BatchUpdate(status="confirmed")

        result = self.batch_manager.execute(
            batch_filter, batch_update, operator="TestUser"
        )

        self.assertEqual(result.total_count, 3)
        self.assertEqual(result.success_count, 3)
        self.assertEqual(result.batch_id.startswith("BATCH-"), True)

        for eid in eids:
            ev = self.db.get_event(eid)
            self.assertEqual(ev.status, "confirmed")
            self.assertEqual(ev.version, 2)

    def test_batch_update_handler_and_note(self):
        eids = self._insert_multiple_events(2)
        batch_filter = BatchFilter(event_ids=eids)
        batch_update = BatchUpdate(handler="NewHandler", note="New note")

        result = self.batch_manager.execute(
            batch_filter, batch_update, operator="TestUser"
        )

        self.assertEqual(result.success_count, 2)
        for eid in eids:
            ev = self.db.get_event(eid)
            self.assertEqual(ev.handler, "NewHandler")
            self.assertEqual(ev.note, "New note")

    def test_batch_skip_same_values(self):
        eid = self._insert_event("EVT-001", status="confirmed")
        batch_filter = BatchFilter(event_ids=[eid])
        batch_update = BatchUpdate(status="confirmed")

        result = self.batch_manager.execute(
            batch_filter, batch_update, operator="TestUser"
        )

        self.assertEqual(result.success_count, 0)
        self.assertEqual(result.skipped_count, 1)
        ev = self.db.get_event(eid)
        self.assertEqual(ev.version, 1)

    def test_batch_partial_update(self):
        eids = self._insert_multiple_events(3, statuses=[
            "unconfirmed", "unconfirmed", "confirmed"
        ])
        self._insert_event("EVT-SKIP", status="confirmed")

        batch_filter = BatchFilter(statuses=["unconfirmed"])
        batch_update = BatchUpdate(status="closed", handler="Closer")

        result = self.batch_manager.execute(
            batch_filter, batch_update, operator="TestUser"
        )

        self.assertEqual(result.total_count, 2)
        self.assertEqual(result.success_count, 2)

        for eid in eids[:2]:
            ev = self.db.get_event(eid)
            self.assertEqual(ev.status, "closed")
            self.assertEqual(ev.handler, "Closer")

        skip_ev = self.db.get_event("EVT-SKIP")
        self.assertEqual(skip_ev.status, "confirmed")
        self.assertEqual(skip_ev.version, 1)

    def test_batch_logs_recorded(self):
        eids = self._insert_multiple_events(2)
        batch_filter = BatchFilter(event_ids=eids)
        batch_update = BatchUpdate(status="confirmed")

        result = self.batch_manager.execute(
            batch_filter, batch_update, operator="LogTest"
        )

        batch = self.db.get_batch_operation(result.batch_id)
        self.assertIsNotNone(batch)
        self.assertEqual(batch.operation_type, "annotate")
        self.assertEqual(batch.operator, "LogTest")
        self.assertEqual(batch.total_count, 2)
        self.assertEqual(batch.success_count, 2)
        self.assertEqual(batch.status, BATCH_STATUS_COMPLETED)

        items = self.db.get_batch_operation_items(result.batch_id)
        self.assertEqual(len(items), 2)
        for item in items:
            self.assertEqual(item.status, ITEM_STATUS_SUCCESS)
            self.assertEqual(item.old_version, 1)
            self.assertEqual(item.new_version, 2)
            self.assertEqual(item.new_status, "confirmed")

    def test_annotation_history_created(self):
        eid = self._insert_event("EVT-001", status="unconfirmed")
        batch_filter = BatchFilter(event_ids=[eid])
        batch_update = BatchUpdate(status="confirmed", handler="Annotator")

        self.batch_manager.execute(
            batch_filter, batch_update, operator="TestUser"
        )

        annotations = self.db.get_annotations_for_event(eid)
        self.assertEqual(len(annotations), 1)
        self.assertEqual(annotations[0].old_status, "unconfirmed")
        self.assertEqual(annotations[0].new_status, "confirmed")
        self.assertEqual(annotations[0].handler, "Annotator")


class TestBatchVersionConflict(_TestBase):
    """测试版本冲突检测和处理策略"""

    def test_conflict_detection_skip_strategy(self):
        eid = self._insert_event("EVT-001", status="unconfirmed", version=1)

        batch_filter = BatchFilter(event_ids=[eid])
        batch_update = BatchUpdate(status="confirmed")
        preview_events = self.batch_manager.preview(batch_filter)

        ev = self.db.get_event(eid)
        ev.status = "false_positive"
        ev.version = 2
        self.db.update_event(ev)

        result = self.batch_manager.execute(
            batch_filter, batch_update, operator="TestUser",
            conflict_strategy=CONFLICT_STRATEGY_SKIP,
            preview_events=preview_events
        )

        self.assertEqual(result.conflict_count, 1)
        self.assertEqual(result.success_count, 0)

        ev_final = self.db.get_event(eid)
        self.assertEqual(ev_final.status, "false_positive")
        self.assertEqual(ev_final.version, 2)

        items = self.db.get_batch_operation_items(result.batch_id)
        self.assertEqual(items[0].status, ITEM_STATUS_CONFLICT)
        self.assertIn("版本冲突", items[0].reason)

    def test_conflict_detection_abort_strategy(self):
        eids = self._insert_multiple_events(3)
        batch_filter = BatchFilter(event_ids=eids)
        batch_update = BatchUpdate(status="confirmed")
        preview_events = self.batch_manager.preview(batch_filter)

        ev = self.db.get_event(eids[1])
        ev.version = 5
        self.db.update_event(ev)

        with self.assertRaises(BatchOperationError) as ctx:
            self.batch_manager.execute(
                batch_filter, batch_update, operator="TestUser",
                conflict_strategy=CONFLICT_STRATEGY_ABORT,
                preview_events=preview_events
            )

        self.assertIn("已中止批量操作", str(ctx.exception))

        ev0 = self.db.get_event(eids[0])
        self.assertEqual(ev0.status, "confirmed")
        self.assertEqual(ev0.version, 2)

        ev1 = self.db.get_event(eids[1])
        self.assertEqual(ev1.version, 5)

        ev2 = self.db.get_event(eids[2])
        self.assertEqual(ev2.version, 1)

    def test_conflict_force_strategy(self):
        eid = self._insert_event("EVT-001", status="unconfirmed", version=1)

        batch_filter = BatchFilter(event_ids=[eid])
        batch_update = BatchUpdate(status="confirmed", handler="ForceUser")
        preview_events = self.batch_manager.preview(batch_filter)

        ev = self.db.get_event(eid)
        ev.version = 10
        self.db.update_event(ev)

        result = self.batch_manager.execute(
            batch_filter, batch_update, operator="TestUser",
            conflict_strategy=CONFLICT_STRATEGY_FORCE,
            preview_events=preview_events
        )

        self.assertEqual(result.success_count, 1)
        self.assertEqual(result.conflict_count, 0)

        ev_final = self.db.get_event(eid)
        self.assertEqual(ev_final.status, "confirmed")
        self.assertEqual(ev_final.handler, "ForceUser")


class TestBatchUndoAndRestart(_TestBase):
    """测试批量修改后撤销和跨重启恢复"""

    def test_batch_annotate_then_undo(self):
        eids = self._insert_multiple_events(3)
        batch_filter = BatchFilter(event_ids=eids)
        batch_update = BatchUpdate(
            status="false_positive", handler="Alice", note="batch-note"
        )

        self.batch_manager.execute(
            batch_filter, batch_update, operator="Operator"
        )

        for eid in eids:
            self.manager.undo(eid)
            ev = self.db.get_event(eid)
            self.assertEqual(ev.status, "unconfirmed")
            self.assertEqual(ev.handler, "")
            self.assertEqual(ev.note, "")

    def test_multiple_batch_then_undo(self):
        eid = self._insert_event("EVT-001")

        self.batch_manager.execute(
            BatchFilter(event_ids=[eid]),
            BatchUpdate(status="false_positive", handler="Alice", note="first"),
            operator="Op1"
        )

        self.batch_manager.execute(
            BatchFilter(event_ids=[eid]),
            BatchUpdate(status="closed", handler="Bob", note="second"),
            operator="Op2"
        )

        self.manager.undo(eid)
        ev = self.db.get_event(eid)
        self.assertEqual(ev.status, "false_positive")
        self.assertEqual(ev.handler, "Alice")
        self.assertEqual(ev.note, "first")

        self.manager.undo(eid)
        ev = self.db.get_event(eid)
        self.assertEqual(ev.status, "unconfirmed")
        self.assertEqual(ev.handler, "")
        self.assertEqual(ev.note, "")

    def test_persistence_across_restart(self):
        eids = self._insert_multiple_events(3)

        result = self.batch_manager.execute(
            BatchFilter(event_ids=eids),
            BatchUpdate(status="confirmed", handler="BeforeRestart"),
            operator="Op"
        )

        csv_before = self._export_csv_rows()
        json_before = self._export_json_events()

        self.db = Database(self.db_path)
        self.batch_manager = BatchOperationManager(self.db, self.config)
        self.manager = AnnotationManager(self.db)
        self.exporter = Exporter(self.db, self.config)

        batch = self.db.get_batch_operation(result.batch_id)
        self.assertIsNotNone(batch)
        self.assertEqual(batch.success_count, 3)

        for eid in eids:
            ev = self.db.get_event(eid)
            self.assertEqual(ev.status, "confirmed")
            self.assertEqual(ev.handler, "BeforeRestart")
            self.assertEqual(ev.version, 2)

        csv_after = self._export_csv_rows()
        json_after = self._export_json_events()

        self.assertEqual(csv_before, csv_after)
        self.assertEqual(json_before, json_after)

    def test_batch_logs_persist_across_restart(self):
        eids = self._insert_multiple_events(2)
        result = self.batch_manager.execute(
            BatchFilter(event_ids=eids),
            BatchUpdate(status="confirmed"),
            operator="LogTest"
        )

        self.db = Database(self.db_path)
        self.batch_manager = BatchOperationManager(self.db, self.config)

        logs = self.batch_manager.get_batch_logs(10)
        self.assertIn(result.batch_id, logs)
        self.assertIn("LogTest", logs)

        detail = self.batch_manager.get_batch_detail(result.batch_id)
        self.assertIn(result.batch_id, detail)
        self.assertIn("总计: 2", detail)


class TestBatchReimportAndExport(_TestBase):
    """测试重复导入后批量导出一致性"""

    def _create_sample_csv(self, path: str, rows: list[dict]) -> None:
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)

    def test_reimport_batch_update_export_consistency(self):
        sample_data = [
            {
                "id": "REC001", "device_id": "DEV-A001",
                "event_time": "2026-06-15 08:30:00",
                "issue_type": "temperature", "severity": "warning",
                "description": "Temp high"
            },
            {
                "id": "REC002", "device_id": "DEV-A001",
                "event_time": "2026-06-15 08:45:00",
                "issue_type": "temperature", "severity": "critical",
                "description": "Temp very high"
            },
            {
                "id": "REC003", "device_id": "DEV-B001",
                "event_time": "2026-06-15 09:00:00",
                "issue_type": "pressure", "severity": "warning",
                "description": "Pressure low"
            },
        ]

        csv_path = os.path.join(self.tmp_dir, "sample.csv")
        self._create_sample_csv(csv_path, sample_data)

        self.importer.import_file(csv_path)
        self.merger.merge(preserve_annotations=True)

        events_before = self.db.get_all_events()
        eids = [e.id for e in events_before]

        self.batch_manager.execute(
            BatchFilter(event_ids=eids),
            BatchUpdate(status="confirmed", handler="BatchHandler", note="批量处理"),
            operator="Op"
        )

        csv_export1 = self._export_csv_rows()
        json_export1 = self._export_json_events()

        self.importer.import_file(csv_path)
        self.merger.merge(preserve_annotations=True)

        csv_export2 = self._export_csv_rows()
        json_export2 = self._export_json_events()

        self.assertEqual(len(csv_export1), len(csv_export2))
        for i in range(len(csv_export1)):
            self.assertEqual(csv_export1[i]["event_id"], csv_export2[i]["event_id"])
            self.assertEqual(csv_export1[i]["status"], csv_export2[i]["status"])
            self.assertEqual(csv_export1[i]["handler"], csv_export2[i]["handler"])
            self.assertEqual(csv_export1[i]["note"], csv_export2[i]["note"])

        self.assertEqual(len(json_export1), len(json_export2))
        for i in range(len(json_export1)):
            self.assertEqual(json_export1[i]["event_id"], json_export2[i]["event_id"])
            self.assertEqual(json_export1[i]["status"], json_export2[i]["status"])
            self.assertEqual(json_export1[i]["handler"], json_export2[i]["handler"])
            self.assertEqual(json_export1[i]["note"], json_export2[i]["note"])

    def test_batch_update_then_export_versions(self):
        eids = self._insert_multiple_events(3)

        self.batch_manager.execute(
            BatchFilter(event_ids=[eids[0]]),
            BatchUpdate(status="confirmed", handler="H1"),
            operator="Op1"
        )

        self.batch_manager.execute(
            BatchFilter(event_ids=[eids[0], eids[1]]),
            BatchUpdate(status="closed", handler="H2", note="Note"),
            operator="Op2"
        )

        csv_rows = self._export_csv_rows()
        json_events = self._export_json_events()

        versions = {row["event_id"]: int(row["version"]) for row in csv_rows}
        self.assertEqual(versions[eids[0]], 3)
        self.assertEqual(versions[eids[1]], 2)
        self.assertEqual(versions[eids[2]], 1)

        for ev in json_events:
            self.assertEqual(ev["version"], versions[ev["event_id"]])


class TestBatchValidation(_TestBase):
    """测试批量操作的参数验证"""

    def test_no_update_content(self):
        self._insert_event("EVT-001")
        with self.assertRaises(BatchOperationError) as ctx:
            self.batch_manager.execute(
                BatchFilter(event_ids=["EVT-001"]),
                BatchUpdate(),
                operator="Test"
            )
        self.assertIn("没有指定任何更新内容", str(ctx.exception))

    def test_invalid_status_update(self):
        self._insert_event("EVT-001")
        with self.assertRaises(BatchOperationError) as ctx:
            self.batch_manager.execute(
                BatchFilter(event_ids=["EVT-001"]),
                BatchUpdate(status="invalid"),
                operator="Test"
            )
        self.assertIn("无效的目标状态", str(ctx.exception))

    def test_empty_operator(self):
        self._insert_event("EVT-001")
        with self.assertRaises(BatchOperationError) as ctx:
            self.batch_manager.execute(
                BatchFilter(event_ids=["EVT-001"]),
                BatchUpdate(status="confirmed"),
                operator=""
            )
        self.assertIn("操作人不能为空", str(ctx.exception))

    def test_empty_handler_update(self):
        self._insert_event("EVT-001")
        with self.assertRaises(BatchOperationError) as ctx:
            self.batch_manager.execute(
                BatchFilter(event_ids=["EVT-001"]),
                BatchUpdate(handler="  "),
                operator="Test"
            )
        self.assertIn("处理人不能为空", str(ctx.exception))

    def test_invalid_time_format(self):
        with self.assertRaises(BatchOperationError) as ctx:
            self.batch_manager.preview(
                BatchFilter(time_from="2026/06/15")
            )
        self.assertIn("无效的时间格式", str(ctx.exception))

    def test_invalid_conflict_strategy(self):
        self._insert_event("EVT-001")
        with self.assertRaises(BatchOperationError) as ctx:
            self.batch_manager.execute(
                BatchFilter(event_ids=["EVT-001"]),
                BatchUpdate(status="confirmed"),
                operator="Test",
                conflict_strategy="invalid"
            )
        self.assertIn("无效的冲突策略", str(ctx.exception))

    def test_no_events_matched(self):
        with self.assertRaises(BatchOperationError) as ctx:
            self.batch_manager.execute(
                BatchFilter(event_ids=["NONEXISTENT"]),
                BatchUpdate(status="confirmed"),
                operator="Test"
            )
        self.assertIn("没有符合条件的事件", str(ctx.exception))


class TestBatchLogManagement(_TestBase):
    """测试批量操作日志管理"""

    def test_get_batch_logs(self):
        for i in range(5):
            eid = f"EVT-{i:03d}"
            self._insert_event(eid)
            self.batch_manager.execute(
                BatchFilter(event_ids=[eid]),
                BatchUpdate(status="confirmed"),
                operator=f"Op{i}"
            )

        logs = self.batch_manager.get_batch_logs(10)
        self.assertIn("最近 5 条批量操作记录", logs)
        for i in range(5):
            self.assertIn(f"Op{i}", logs)

    def test_get_batch_detail(self):
        eids = self._insert_multiple_events(3)
        result = self.batch_manager.execute(
            BatchFilter(event_ids=eids),
            BatchUpdate(status="confirmed", handler="DetailTest"),
            operator="Op"
        )

        detail = self.batch_manager.get_batch_detail(result.batch_id)
        self.assertIn(result.batch_id, detail)
        self.assertIn("DetailTest", detail)
        self.assertIn("总计: 3 | 成功: 3", detail)
        for eid in eids:
            self.assertIn(eid, detail)

    def test_cleanup_old_logs(self):
        eid = self._insert_event("EVT-001")
        result = self.batch_manager.execute(
            BatchFilter(event_ids=[eid]),
            BatchUpdate(status="confirmed"),
            operator="Op"
        )

        batch = self.db.get_batch_operation(result.batch_id)
        self.assertIsNotNone(batch)

        old_time = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
        with self.db._conn() as conn:
            conn.execute(
                "UPDATE batch_operations SET created_at = ? WHERE id = ?",
                (old_time, result.batch_id)
            )

        deleted = self.batch_manager.cleanup_old_logs(days=1)
        self.assertEqual(deleted, 1)

        batch = self.db.get_batch_operation(result.batch_id)
        self.assertIsNone(batch)

        items = self.db.get_batch_operation_items(result.batch_id)
        self.assertEqual(len(items), 0)

    def test_nonexistent_batch_detail(self):
        detail = self.batch_manager.get_batch_detail("BATCH-NONEXISTENT")
        self.assertIn("批量操作不存在", detail)

    def test_empty_logs(self):
        logs = self.batch_manager.get_batch_logs()
        self.assertIn("暂无批量操作记录", logs)


if __name__ == "__main__":
    unittest.main()
