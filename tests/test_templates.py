"""测试：批量任务模板功能完整性"""
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
from inspection_cli.config import AppConfig, ValidationRules
from inspection_cli.database import (
    BATCH_STATUS_COMPLETED, BATCH_STATUS_PARTIAL, ITEM_STATUS_CONFLICT,
    ITEM_STATUS_SKIPPED, ITEM_STATUS_SUCCESS, Database, Event,
)
from inspection_cli.exporter import Exporter
from inspection_cli.importer import RecordImporter
from inspection_cli.merger import EventMerger
from inspection_cli.templates import (
    TemplateError, TemplateExportResult, TemplateImportError,
    TemplateImportItemResult, TemplateImportResult, TemplateManager,
    TemplateValidationIssue, TemplateValidationResult, TEMPLATE_EXPORT_VERSION,
)


class _TestBase(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp_dir, "test.db")
        self.db = Database(self.db_path)
        self.config = AppConfig(db_path=self.db_path)
        self.manager = AnnotationManager(self.db)
        self.batch_manager = BatchOperationManager(self.db, self.config)
        self.template_manager = TemplateManager(self.db, self.config)
        self.exporter = Exporter(self.db, self.config)
        self.importer = RecordImporter(self.db, self.config)
        self.merger = EventMerger(self.db, self.config)

    def _insert_event(self, event_id: str, device_id: str = "DEV-A001",
                      status: str = "unconfirmed", handler: str = "",
                      note: str = "", version: int = 1,
                      first_seen: str = "2026-06-15 08:30:00",
                      last_seen: str = "2026-06-15 09:10:00") -> str:
        event = Event(
            id=event_id,
            device_id=device_id,
            first_seen=first_seen,
            last_seen=last_seen,
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

    def _create_sample_csv(self, path: str, rows: list[dict]) -> None:
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)


class TestTemplateCRUD(_TestBase):
    """测试模板 CRUD 操作"""

    def test_save_template_basic(self):
        """基本保存模板"""
        tpl = self.template_manager.save_template(
            name="close-unconfirmed",
            description="关闭所有待确认事件",
            batch_filter=BatchFilter(statuses=["unconfirmed"]),
            batch_update=BatchUpdate(status="closed", handler="Admin"),
            conflict_strategy="skip",
        )
        self.assertEqual(tpl.name, "close-unconfirmed")
        self.assertEqual(tpl.conflict_strategy, "skip")

        loaded = self.template_manager.get_template("close-unconfirmed")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.description, "关闭所有待确认事件")

    def test_save_template_duplicate_name(self):
        """重复名称保存应报错"""
        self.template_manager.save_template(
            name="tpl1",
            description="第一个模板",
            batch_filter=BatchFilter(statuses=["unconfirmed"]),
            batch_update=BatchUpdate(status="confirmed", handler="H"),
        )

        with self.assertRaises(TemplateError) as ctx:
            self.template_manager.save_template(
                name="tpl1",
                description="另一个",
                batch_filter=BatchFilter(device_ids=["DEV-A001"]),
                batch_update=BatchUpdate(handler="New"),
            )
        self.assertIn("已存在", str(ctx.exception))
        self.assertIn("--overwrite", str(ctx.exception))

    def test_save_template_overwrite(self):
        """使用 overwrite 覆盖同名模板"""
        self.template_manager.save_template(
            name="tpl-overwrite",
            description="原始版本",
            batch_filter=BatchFilter(statuses=["unconfirmed"]),
            batch_update=BatchUpdate(status="confirmed", handler="H1"),
            conflict_strategy="skip",
        )

        tpl2 = self.template_manager.save_template(
            name="tpl-overwrite",
            description="更新版本",
            batch_filter=BatchFilter(statuses=["confirmed"]),
            batch_update=BatchUpdate(status="closed", handler="H2"),
            conflict_strategy="force",
            overwrite=True,
        )
        self.assertEqual(tpl2.description, "更新版本")
        self.assertEqual(tpl2.conflict_strategy, "force")

    def test_save_template_empty_name(self):
        """空名称应报错"""
        with self.assertRaises(TemplateError) as ctx:
            self.template_manager.save_template(
                name="  ",
                description="空名称测试",
                batch_filter=BatchFilter(),
                batch_update=BatchUpdate(status="confirmed"),
            )
        self.assertIn("不能为空", str(ctx.exception))

    def test_save_template_no_update(self):
        """没有任何更新内容应报错"""
        with self.assertRaises(TemplateError) as ctx:
            self.template_manager.save_template(
                name="no-update",
                description="没有更新",
                batch_filter=BatchFilter(statuses=["unconfirmed"]),
                batch_update=BatchUpdate(),
            )
        self.assertIn("没有指定任何更新内容", str(ctx.exception))

    def test_save_template_invalid_conflict_strategy(self):
        """无效冲突策略应报错"""
        with self.assertRaises(TemplateError) as ctx:
            self.template_manager.save_template(
                name="bad-strategy",
                description="测试",
                batch_filter=BatchFilter(),
                batch_update=BatchUpdate(status="confirmed"),
                conflict_strategy="invalid",
            )
        self.assertIn("无效的冲突策略", str(ctx.exception))

    def test_list_templates_empty(self):
        """空模板列表"""
        lst = self.template_manager.list_templates()
        self.assertEqual(len(lst), 0)
        formatted = self.template_manager.format_template_list(lst)
        self.assertIn("暂无模板", formatted)

    def test_list_templates_multiple(self):
        """多个模板列表"""
        for i in range(3):
            self.template_manager.save_template(
                name=f"tpl-{i}",
                description=f"模板{i}",
                batch_filter=BatchFilter(statuses=["unconfirmed"]),
                batch_update=BatchUpdate(status="confirmed", handler=f"H{i}"),
            )
        lst = self.template_manager.list_templates()
        self.assertEqual(len(lst), 3)
        formatted = self.template_manager.format_template_list(lst)
        for i in range(3):
            self.assertIn(f"tpl-{i}", formatted)

    def test_get_template_nonexistent(self):
        """获取不存在的模板"""
        result = self.template_manager.get_template("nonexistent")
        self.assertIsNone(result)

        with self.assertRaises(TemplateError) as ctx:
            self.template_manager.get_template_or_error("nonexistent")
        self.assertIn("模板不存在", str(ctx.exception))
        self.assertIn("template-list", str(ctx.exception))

    def test_copy_template(self):
        """复制模板"""
        self.template_manager.save_template(
            name="source-tpl",
            description="源模板描述",
            batch_filter=BatchFilter(
                statuses=["unconfirmed"],
                device_ids=["DEV-A001", "DEV-A002"],
            ),
            batch_update=BatchUpdate(
                status="confirmed",
                handler="SourceHandler",
                note="源备注",
            ),
            conflict_strategy="abort",
        )

        new_tpl = self.template_manager.copy_template(
            source_name="source-tpl",
            target_name="copied-tpl",
        )
        self.assertEqual(new_tpl.name, "copied-tpl")
        self.assertIn("副本", new_tpl.description)

        src = self.template_manager.get_template("source-tpl")
        self.assertEqual(src.filters, new_tpl.filters)
        self.assertEqual(src.updates, new_tpl.updates)
        self.assertEqual(src.conflict_strategy, new_tpl.conflict_strategy)

    def test_copy_template_custom_description(self):
        """复制模板时自定义描述"""
        self.template_manager.save_template(
            name="src",
            description="原始",
            batch_filter=BatchFilter(),
            batch_update=BatchUpdate(status="closed"),
        )

        new_tpl = self.template_manager.copy_template(
            source_name="src",
            target_name="dst",
            new_description="我的自定义副本",
        )
        self.assertEqual(new_tpl.description, "我的自定义副本")

    def test_copy_template_source_not_found(self):
        """复制不存在的源模板"""
        with self.assertRaises(TemplateError) as ctx:
            self.template_manager.copy_template("no-source", "target")
        self.assertIn("模板不存在", str(ctx.exception))

    def test_copy_template_target_exists(self):
        """复制到已存在的目标名称"""
        for n in ["src", "target"]:
            self.template_manager.save_template(
                name=n, description=f"{n}的描述",
                batch_filter=BatchFilter(),
                batch_update=BatchUpdate(status="confirmed"),
            )
        with self.assertRaises(TemplateError) as ctx:
            self.template_manager.copy_template("src", "target")
        self.assertIn("已存在", str(ctx.exception))

    def test_copy_template_empty_target_name(self):
        """复制时目标名称为空"""
        self.template_manager.save_template(
            name="src", description="test",
            batch_filter=BatchFilter(),
            batch_update=BatchUpdate(status="confirmed"),
        )
        with self.assertRaises(TemplateError) as ctx:
            self.template_manager.copy_template("src", "   ")
        self.assertIn("不能为空", str(ctx.exception))

    def test_delete_template(self):
        """删除模板"""
        self.template_manager.save_template(
            name="to-delete", description="test",
            batch_filter=BatchFilter(),
            batch_update=BatchUpdate(status="confirmed"),
        )
        self.assertIsNotNone(self.template_manager.get_template("to-delete"))

        deleted = self.template_manager.delete_template("to-delete")
        self.assertTrue(deleted)
        self.assertIsNone(self.template_manager.get_template("to-delete"))

    def test_delete_template_nonexistent(self):
        """删除不存在的模板"""
        deleted = self.template_manager.delete_template("no-such-template")
        self.assertFalse(deleted)


class TestTemplatePersistenceAndValidation(_TestBase):
    """测试模板持久化、跨重启和兼容性验证"""

    def test_template_persistence_across_restart(self):
        """模板保存后跨重启（重新实例化 Database）仍然存在"""
        tpl = self.template_manager.save_template(
            name="persistent-tpl",
            description="跨持久化测试",
            batch_filter=BatchFilter(
                statuses=["unconfirmed", "false_positive"],
                device_ids=["DEV-X001", "DEV-X002"],
                time_from="2026-06-15 00:00:00",
                time_to="2026-06-20 23:59:59",
            ),
            batch_update=BatchUpdate(
                status="closed",
                handler="PersistenceUser",
                note="持久化备注",
            ),
            conflict_strategy="abort",
        )

        bf_before, bu_before, cs_before = self.template_manager.template_to_objects(tpl)

        self.db = Database(self.db_path)
        self.template_manager = TemplateManager(self.db, self.config)

        loaded = self.template_manager.get_template("persistent-tpl")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.name, "persistent-tpl")
        self.assertEqual(loaded.description, "跨持久化测试")
        self.assertEqual(loaded.created_at, tpl.created_at)
        self.assertEqual(loaded.updated_at, tpl.updated_at)

        bf_after, bu_after, cs_after = self.template_manager.template_to_objects(loaded)

        self.assertEqual(bf_before.event_ids, bf_after.event_ids)
        self.assertEqual(bf_before.device_ids, bf_after.device_ids)
        self.assertEqual(bf_before.statuses, bf_after.statuses)
        self.assertEqual(bf_before.time_from, bf_after.time_from)
        self.assertEqual(bf_before.time_to, bf_after.time_to)

        self.assertEqual(bu_before.status, bu_after.status)
        self.assertEqual(bu_before.handler, bu_after.handler)
        self.assertEqual(bu_before.note, bu_after.note)

        self.assertEqual(cs_before, cs_after)

    def test_template_validate_compatible(self):
        """完全兼容的模板验证"""
        tpl = self.template_manager.save_template(
            name="compatible",
            description="兼容测试",
            batch_filter=BatchFilter(
                statuses=["unconfirmed", "confirmed"],
                device_ids=["DEV-ABC123"],
                time_from="2026-06-15 08:00:00",
                time_to="2026-06-15 18:00:00",
            ),
            batch_update=BatchUpdate(status="closed", handler="ValidUser"),
        )
        validation = self.template_manager.validate_template(tpl)
        self.assertTrue(validation.is_valid)
        self.assertFalse(validation.has_warnings)

    def test_template_validate_invalid_filter_status(self):
        """筛选状态与当前配置冲突时应报错"""
        tpl = self.template_manager.save_template(
            name="bad-status-filter",
            description="测试",
            batch_filter=BatchFilter(statuses=["unconfirmed", "invalid_old_status"]),
            batch_update=BatchUpdate(status="confirmed"),
        )
        validation = self.template_manager.validate_template(tpl)
        self.assertFalse(validation.is_valid)
        errors = validation.errors
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].field, "filters.statuses")
        self.assertIn("invalid_old_status", errors[0].message)

    def test_template_validate_invalid_target_status(self):
        """目标状态与当前配置冲突时应报错"""
        tpl = self.template_manager.save_template(
            name="bad-status-target",
            description="测试",
            batch_filter=BatchFilter(statuses=["unconfirmed"]),
            batch_update=BatchUpdate(status="archived", handler="H"),
        )
        validation = self.template_manager.validate_template(tpl)
        self.assertFalse(validation.is_valid)
        errors = validation.errors
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].field, "updates.status")
        self.assertIn("archived", errors[0].message)

    def test_template_validate_invalid_time_format(self):
        """时间格式与当前配置冲突时应报错"""
        alt_config = AppConfig(db_path=self.db_path)
        alt_config.validation.time_formats = ["%d/%m/%Y %H:%M"]
        alt_tm = TemplateManager(self.db, alt_config)

        tpl = alt_tm.save_template(
            name="time-format-test",
            description="使用不同时间格式",
            batch_filter=BatchFilter(
                statuses=["unconfirmed"],
                time_from="15/06/2026 08:30",
                time_to="20/06/2026 18:00",
            ),
            batch_update=BatchUpdate(status="confirmed"),
        )

        validation = self.template_manager.validate_template(tpl)
        self.assertFalse(validation.is_valid)
        error_fields = {e.field for e in validation.errors}
        self.assertIn("filters.time_from", error_fields)
        self.assertIn("filters.time_to", error_fields)

    def test_template_validate_empty_handler(self):
        """处理人为空字符串应报错"""
        tpl = self.template_manager.save_template(
            name="empty-handler",
            description="测试",
            batch_filter=BatchFilter(statuses=["unconfirmed"]),
            batch_update=BatchUpdate(handler="  ", note="n"),
        )
        validation = self.template_manager.validate_template(tpl)
        self.assertFalse(validation.is_valid)
        self.assertEqual(validation.errors[0].field, "updates.handler")

    def test_template_validate_device_id_pattern_warning(self):
        """设备编号不符合模式应给出警告（不阻止）"""
        tpl = self.template_manager.save_template(
            name="device-warning",
            description="测试",
            batch_filter=BatchFilter(device_ids=["DEV-X", "BAD-FORMAT-123"]),
            batch_update=BatchUpdate(status="confirmed"),
        )
        validation = self.template_manager.validate_template(tpl)
        self.assertTrue(validation.is_valid)
        self.assertTrue(validation.has_warnings)
        warning_fields = {w.field for w in validation.warnings}
        self.assertIn("filters.device_ids", warning_fields)


class TestTemplateExecuteWithReimportAndConsistency(_TestBase):
    """测试重复导入 merge 后套用模板，以及执行结果一致性"""

    def test_reimport_merge_then_apply_template(self):
        """重复导入并 merge 后，继续套用模板，version 不回退"""
        sample_data = [
            {
                "id": "REC301", "device_id": "DEV-Z001",
                "event_time": "2026-06-15 08:30:00",
                "issue_type": "temperature", "severity": "warning",
                "description": "T1",
            },
            {
                "id": "REC302", "device_id": "DEV-Z001",
                "event_time": "2026-06-15 08:45:00",
                "issue_type": "temperature", "severity": "critical",
                "description": "T2",
            },
            {
                "id": "REC303", "device_id": "DEV-Z002",
                "event_time": "2026-06-15 09:00:00",
                "issue_type": "pressure", "severity": "warning",
                "description": "P1",
            },
            {
                "id": "REC304", "device_id": "DEV-Z003",
                "event_time": "2026-06-15 10:00:00",
                "issue_type": "connectivity", "severity": "critical",
                "description": "C1",
            },
        ]
        csv_path = os.path.join(self.tmp_dir, "sample_reimport.csv")
        self._create_sample_csv(csv_path, sample_data)

        self.importer.import_file(csv_path)
        self.merger.merge(preserve_annotations=False)

        events_v1 = self.db.get_all_events()
        eids_v1 = [e.id for e in events_v1]
        self.assertGreaterEqual(len(eids_v1), 2)

        self.template_manager.save_template(
            name="confirm-all-unconfirmed",
            description="确认所有待确认事件",
            batch_filter=BatchFilter(statuses=["unconfirmed"]),
            batch_update=BatchUpdate(
                status="confirmed",
                handler="TemplateRunner",
                note="由模板批量确认",
            ),
            conflict_strategy="skip",
        )

        tpl = self.template_manager.get_template_or_error("confirm-all-unconfirmed")
        validation = self.template_manager.validate_template(tpl)
        self.assertTrue(validation.is_valid)

        bf, bu, cs = self.template_manager.template_to_objects(tpl)
        result1 = self.batch_manager.execute(
            bf, bu, operator="Op1", conflict_strategy=cs,
        )
        self.assertEqual(result1.success_count, len(eids_v1))

        versions_after_tpl = {eid: self.db.get_event(eid).version for eid in eids_v1}
        for v in versions_after_tpl.values():
            self.assertEqual(v, 2)

        self.importer.import_file(csv_path)
        self.merger.merge(preserve_annotations=True)

        events_after_merge = self.db.get_all_events()
        self.assertEqual({e.id for e in events_after_merge}, set(eids_v1))
        for e in events_after_merge:
            self.assertEqual(e.version, 2, f"{e.id} merge 后 version 应保持 2")
            self.assertEqual(e.status, "confirmed")
            self.assertEqual(e.handler, "TemplateRunner")

        target_eid = eids_v1[0]
        self.manager.annotate(target_eid, "closed", "SingleOp", "单条关闭")
        self.assertEqual(self.db.get_event(target_eid).version, 3)

        self.importer.import_file(csv_path)
        self.merger.merge(preserve_annotations=True)

        ev_final = self.db.get_event(target_eid)
        self.assertEqual(ev_final.version, 3, "重复 merge 后 version 不应回退")
        self.assertEqual(ev_final.status, "closed")
        self.assertEqual(ev_final.handler, "SingleOp")

        for eid in eids_v1[1:]:
            ev = self.db.get_event(eid)
            self.assertEqual(ev.version, 2)
            self.assertEqual(ev.status, "confirmed")

    def test_template_batch_result_detail_logs_export_consistency(self):
        """模板执行后，batch-detail、batch-logs、导出 CSV/JSON 数据一致"""
        sample_data = [
            {"id": "REC401", "device_id": "DEV-Y001",
             "event_time": "2026-06-15 08:30:00",
             "issue_type": "vibration", "severity": "warning",
             "description": "V1"},
            {"id": "REC402", "device_id": "DEV-Y001",
             "event_time": "2026-06-15 08:50:00",
             "issue_type": "vibration", "severity": "critical",
             "description": "V2"},
            {"id": "REC403", "device_id": "DEV-Y002",
             "event_time": "2026-06-15 09:00:00",
             "issue_type": "current", "severity": "warning",
             "description": "C1"},
            {"id": "REC404", "device_id": "DEV-Y003",
             "event_time": "2026-06-15 10:00:00",
             "issue_type": "voltage", "severity": "critical",
             "description": "V3"},
            {"id": "REC405", "device_id": "DEV-Y004",
             "event_time": "2026-06-15 11:00:00",
             "issue_type": "temperature", "severity": "info",
             "description": "T1"},
        ]
        csv_path = os.path.join(self.tmp_dir, "consistency.csv")
        self._create_sample_csv(csv_path, sample_data)

        self.importer.import_file(csv_path)
        self.merger.merge(preserve_annotations=False)

        events = self.db.get_all_events()
        eids = [e.id for e in events]
        self.assertEqual(len(eids), 4)

        self.manager.annotate(eids[1], "false_positive", "PriorUser", "提前标注")
        self.manager.annotate(eids[3], "closed", "PriorUser", "提前关闭")

        self.template_manager.save_template(
            name="close-unconfirmed-critical",
            description="关闭所有待确认的告警",
            batch_filter=BatchFilter(
                statuses=["unconfirmed"],
            ),
            batch_update=BatchUpdate(
                status="closed",
                handler="BatchTpl",
                note="模板批量关闭",
            ),
            conflict_strategy="skip",
        )

        tpl = self.template_manager.get_template_or_error("close-unconfirmed-critical")
        bf, bu, cs = self.template_manager.template_to_objects(tpl)

        preview = self.batch_manager.preview(bf)
        expected_success = len(preview)

        result = self.batch_manager.execute(
            bf, bu, operator="BatchOp", conflict_strategy=cs,
            preview_events=preview,
        )

        self.assertEqual(result.total_count, expected_success)
        self.assertGreater(result.success_count, 0)

        batch = self.db.get_batch_operation(result.batch_id)
        self.assertIsNotNone(batch)
        self.assertEqual(batch.total_count, result.total_count)
        self.assertEqual(batch.success_count, result.success_count)
        self.assertEqual(batch.conflict_count, result.conflict_count)
        self.assertEqual(batch.skipped_count, result.skipped_count)
        self.assertEqual(batch.error_count, result.error_count)

        detail = self.batch_manager.get_batch_detail(result.batch_id)
        self.assertIn(result.batch_id, detail)
        self.assertIn(
            f"总计: {result.total_count} | 成功: {result.success_count} | "
            f"跳过: {result.skipped_count} | 冲突: {result.conflict_count} | "
            f"错误: {result.error_count}",
            detail,
        )

        logs = self.batch_manager.get_batch_logs(10)
        self.assertIn(result.batch_id, logs)

        csv_rows = self._export_csv_rows()
        csv_by_id = {r["event_id"]: r for r in csv_rows}

        json_events = self._export_json_events()
        json_by_id = {e["event_id"]: e for e in json_events}

        items = self.db.get_batch_operation_items(result.batch_id)
        self.assertEqual(len(items), result.total_count)

        success_items = [x for x in items if x.status == ITEM_STATUS_SUCCESS]
        self.assertEqual(len(success_items), result.success_count)

        for item in success_items:
            self.assertEqual(item.new_status, "closed")
            self.assertEqual(item.new_handler, "BatchTpl")
            self.assertEqual(item.new_note, "模板批量关闭")
            self.assertEqual(item.new_version, item.old_version + 1)

            ev = self.db.get_event(item.event_id)
            self.assertEqual(ev.status, "closed")
            self.assertEqual(ev.handler, "BatchTpl")
            self.assertEqual(ev.note, "模板批量关闭")
            self.assertEqual(ev.version, item.new_version)

            csv_row = csv_by_id.get(item.event_id)
            self.assertIsNotNone(csv_row)
            self.assertEqual(csv_row["status"], "closed")
            self.assertEqual(csv_row["handler"], "BatchTpl")
            self.assertEqual(csv_row["note"], "模板批量关闭")
            self.assertEqual(int(csv_row["version"]), item.new_version)

            json_ev = json_by_id.get(item.event_id)
            self.assertIsNotNone(json_ev)
            self.assertEqual(json_ev["status"], "closed")
            self.assertEqual(json_ev["handler"], "BatchTpl")
            self.assertEqual(json_ev["note"], "模板批量关闭")
            self.assertEqual(json_ev["version"], item.new_version)

        for eid in [eids[1], eids[3]]:
            ev = self.db.get_event(eid)
            csv_row = csv_by_id[eid]
            json_ev = json_by_id[eid]
            self.assertEqual(ev.status, csv_row["status"])
            self.assertEqual(ev.handler, csv_row["handler"])
            self.assertEqual(ev.note, csv_row["note"])
            self.assertEqual(ev.version, int(csv_row["version"]))
            self.assertEqual(ev.status, json_ev["status"])
            self.assertEqual(ev.handler, json_ev["handler"])
            self.assertEqual(ev.note, json_ev["note"])
            self.assertEqual(ev.version, json_ev["version"])

    def test_template_with_conflicts_and_detail_match(self):
        """模板执行含冲突场景，batch-detail 和结果计数一致"""
        eids = self._insert_multiple_events(5, statuses=["unconfirmed"] * 5)

        self.template_manager.save_template(
            name="conflict-tpl",
            description="冲突测试模板",
            batch_filter=BatchFilter(event_ids=eids),
            batch_update=BatchUpdate(
                status="confirmed",
                handler="TplUser",
                note="冲突测试",
            ),
            conflict_strategy="skip",
        )

        tpl = self.template_manager.get_template_or_error("conflict-tpl")
        bf, bu, cs = self.template_manager.template_to_objects(tpl)

        preview_events = self.batch_manager.preview(bf)

        self.manager.annotate(eids[1], "closed", "Interloper1", "抢先改1")
        self.manager.annotate(eids[3], "false_positive", "Interloper2", "抢先改2")

        result = self.batch_manager.execute(
            bf, bu, operator="Runner", conflict_strategy=cs,
            preview_events=preview_events,
        )

        self.assertEqual(result.success_count, 3)
        self.assertEqual(result.conflict_count, 2)
        self.assertEqual(result.total_count, 5)

        batch = self.db.get_batch_operation(result.batch_id)
        self.assertEqual(batch.success_count, 3)
        self.assertEqual(batch.conflict_count, 2)
        self.assertEqual(batch.status, BATCH_STATUS_PARTIAL)

        detail = self.batch_manager.get_batch_detail(result.batch_id)
        self.assertIn("总计: 5 | 成功: 3 | 跳过: 0 | 冲突: 2 | 错误: 0", detail)
        self.assertIn(eids[1], detail)
        self.assertIn(eids[3], detail)
        self.assertIn("预览时版本=1，当前版本=2", detail)

        items = self.db.get_batch_operation_items(result.batch_id)
        conflict_items = [x for x in items if x.status == ITEM_STATUS_CONFLICT]
        success_items = [x for x in items if x.status == ITEM_STATUS_SUCCESS]
        self.assertEqual(len(conflict_items), 2)
        self.assertEqual(len(success_items), 3)

        for c_item in conflict_items:
            ev = self.db.get_event(c_item.event_id)
            self.assertNotEqual(ev.status, "confirmed")
            self.assertNotEqual(ev.handler, "TplUser")

        for s_item in success_items:
            ev = self.db.get_event(s_item.event_id)
            self.assertEqual(ev.status, "confirmed")
            self.assertEqual(ev.handler, "TplUser")
            self.assertEqual(ev.note, "冲突测试")

        csv_by_id = {r["event_id"]: r for r in self._export_csv_rows()}
        json_by_id = {e["event_id"]: e for e in self._export_json_events()}

        for eid in eids:
            ev = self.db.get_event(eid)
            csv_r = csv_by_id[eid]
            json_e = json_by_id[eid]
            self.assertEqual(ev.status, csv_r["status"])
            self.assertEqual(ev.status, json_e["status"])
            self.assertEqual(ev.version, int(csv_r["version"]))
            self.assertEqual(ev.version, json_e["version"])


class TestTemplateFormatting(_TestBase):
    """测试模板列表和详情格式化输出"""

    def test_format_detail_all_fields(self):
        """模板详情包含所有筛选和更新字段"""
        self.template_manager.save_template(
            name="full-detail-tpl",
            description="完整字段测试",
            batch_filter=BatchFilter(
                event_ids=["EVT-001", "EVT-002"],
                device_ids=["DEV-A001"],
                statuses=["unconfirmed", "confirmed"],
                time_from="2026-06-01 00:00:00",
                time_to="2026-06-30 23:59:59",
            ),
            batch_update=BatchUpdate(
                status="closed",
                handler="Admin",
                note="季度末关闭",
            ),
            conflict_strategy="abort",
        )
        tpl = self.template_manager.get_template("full-detail-tpl")
        detail = self.template_manager.format_template_detail(tpl)
        self.assertIn("full-detail-tpl", detail)
        self.assertIn("完整字段测试", detail)
        self.assertIn("abort", detail)
        self.assertIn("事件ID: EVT-001, EVT-002", detail)
        self.assertIn("设备编号: DEV-A001", detail)
        self.assertIn("状态: unconfirmed, confirmed", detail)
        self.assertIn("起始时间: 2026-06-01 00:00:00", detail)
        self.assertIn("结束时间: 2026-06-30 23:59:59", detail)
        self.assertIn("状态: closed", detail)
        self.assertIn("处理人: Admin", detail)
        self.assertIn("备注: 季度末关闭", detail)

    def test_format_detail_optional_fields_unset(self):
        """未设置的可选字段显示为 (未设置) 或 (不修改)"""
        self.template_manager.save_template(
            name="partial-tpl",
            description="",
            batch_filter=BatchFilter(statuses=["unconfirmed"]),
            batch_update=BatchUpdate(note=""),
        )
        tpl = self.template_manager.get_template("partial-tpl")
        detail = self.template_manager.format_template_detail(tpl)
        self.assertIn("描述: (无)", detail)
        self.assertIn("事件ID: (未设置)", detail)
        self.assertIn("设备编号: (未设置)", detail)
        self.assertIn("起始时间: (未设置)", detail)
        self.assertIn("结束时间: (未设置)", detail)
        self.assertIn("状态: (不修改)", detail)
        self.assertIn("处理人: (不修改)", detail)
        self.assertIn("备注: (清空)", detail)

    def test_describe_method(self):
        """describe 方法输出包含核心信息"""
        self.template_manager.save_template(
            name="describe-test",
            description="d",
            batch_filter=BatchFilter(
                device_ids=["DEV-A001"],
                statuses=["unconfirmed"],
            ),
            batch_update=BatchUpdate(
                status="confirmed",
                handler="H",
            ),
            conflict_strategy="force",
        )
        tpl = self.template_manager.get_template("describe-test")
        desc = tpl.describe()
        self.assertIn("DEV-A001", desc)
        self.assertIn("unconfirmed", desc)
        self.assertIn("状态→confirmed", desc)
        self.assertIn("处理人→H", desc)
        self.assertIn("force", desc)


class TestTemplateExport(_TestBase):
    """模板导出功能测试"""

    def test_export_single_template_to_dict(self):
        """导出单个模板为字典"""
        self.template_manager.save_template(
            name="export-single",
            description="单模板导出测试",
            batch_filter=BatchFilter(statuses=["unconfirmed"]),
            batch_update=BatchUpdate(status="confirmed", handler="ExportTester"),
        )
        data = self.template_manager.export_template("export-single")
        self.assertIsNotNone(data)
        self.assertEqual(data["name"], "export-single")
        self.assertEqual(data["description"], "单模板导出测试")
        self.assertEqual(data["filters"]["statuses"], ["unconfirmed"])
        self.assertEqual(data["updates"]["status"], "confirmed")
        self.assertEqual(data["updates"]["handler"], "ExportTester")
        self.assertNotIn("id", data)
        self.assertNotIn("created_at", data)
        self.assertNotIn("updated_at", data)

    def test_export_nonexistent_template_raises(self):
        """导出不存在的模板应报错"""
        with self.assertRaises(TemplateError) as ctx:
            self.template_manager.export_template("no-such-template")
        self.assertIn("不存在", str(ctx.exception))

    def test_export_multiple_templates(self):
        """批量导出多个模板"""
        names = ["tpl-exp-1", "tpl-exp-2", "tpl-exp-3"]
        for i, n in enumerate(names):
            self.template_manager.save_template(
                name=n,
                description=f"模板{i}",
                batch_filter=BatchFilter(statuses=["unconfirmed"]),
                batch_update=BatchUpdate(status="confirmed", handler=f"H{i}"),
            )

        data = self.template_manager.export_templates(names[:2])
        self.assertEqual(data["version"], TEMPLATE_EXPORT_VERSION)
        self.assertEqual(data["template_count"], 2)
        self.assertIn("exported_at", data)
        exported_names = [t["name"] for t in data["templates"]]
        self.assertEqual(sorted(exported_names), sorted(names[:2]))

    def test_export_all_templates(self):
        """不指定名称时导出全部模板"""
        for i in range(3):
            self.template_manager.save_template(
                name=f"tpl-all-{i}",
                description=f"全量导出{i}",
                batch_filter=BatchFilter(statuses=["unconfirmed"]),
                batch_update=BatchUpdate(status="confirmed"),
            )
        data = self.template_manager.export_templates()
        self.assertEqual(data["template_count"], 3)

    def test_export_empty_when_no_templates(self):
        """没有模板时导出空集合"""
        data = self.template_manager.export_templates()
        self.assertEqual(data["template_count"], 0)
        self.assertEqual(data["templates"], [])

    def test_export_templates_to_file(self):
        """导出到 JSON 文件"""
        self.template_manager.save_template(
            name="file-export",
            description="文件导出测试",
            batch_filter=BatchFilter(statuses=["unconfirmed", "confirmed"]),
            batch_update=BatchUpdate(status="closed", handler="FileExporter"),
        )
        out_path = os.path.join(self.tmp_dir, "exported.json")
        result = self.template_manager.export_templates_to_file(
            out_path, names=["file-export"], operator="TestUser",
        )
        self.assertIsInstance(result, TemplateExportResult)
        self.assertEqual(result.template_count, 1)
        self.assertEqual(result.template_names, ["file-export"])
        self.assertTrue(os.path.exists(out_path))

        with open(out_path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        self.assertEqual(loaded["version"], TEMPLATE_EXPORT_VERSION)
        self.assertEqual(loaded["template_count"], 1)
        self.assertEqual(loaded["templates"][0]["name"], "file-export")

    def test_export_result_formatted(self):
        """导出结果格式化输出"""
        self.template_manager.save_template(
            name="fmt-export",
            description="格式化测试",
            batch_filter=BatchFilter(),
            batch_update=BatchUpdate(status="confirmed"),
        )
        out_path = os.path.join(self.tmp_dir, "fmt.json")
        result = self.template_manager.export_templates_to_file(out_path)
        formatted = result.formatted()
        self.assertIn("已导出", formatted)
        self.assertIn(out_path, formatted)
        self.assertIn("1", formatted)


class TestTemplateImportBasic(_TestBase):
    """模板导入基础功能测试"""

    def _make_export_json(self, templates: list[dict]) -> str:
        data = {
            "version": TEMPLATE_EXPORT_VERSION,
            "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "template_count": len(templates),
            "templates": templates,
        }
        path = os.path.join(self.tmp_dir, "import-source.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return path

    def test_import_single_template(self):
        """导入单个模板"""
        tpl = {
            "name": "imported-tpl",
            "description": "从文件导入的模板",
            "filters": {"event_ids": None, "device_ids": None, "statuses": ["unconfirmed"],
                        "time_from": None, "time_to": None},
            "updates": {"status": "confirmed", "handler": "ImportUser", "note": "导入测试"},
            "conflict_strategy": "skip",
        }
        path = self._make_export_json([tpl])
        result = self.template_manager.import_templates_from_file(path, operator="Tester")
        self.assertIsInstance(result, TemplateImportResult)
        self.assertEqual(result.total_count, 1)
        self.assertEqual(result.success_count, 1)
        self.assertEqual(result.skipped_count, 0)

        loaded = self.template_manager.get_template("imported-tpl")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.description, "从文件导入的模板")
        self.assertEqual(loaded.conflict_strategy, "skip")

    def test_import_multiple_templates(self):
        """批量导入多个模板"""
        tpls = [
            {"name": "imp-1", "description": "批量导入1",
             "filters": {"statuses": ["unconfirmed"], "event_ids": None,
                         "device_ids": None, "time_from": None, "time_to": None},
             "updates": {"status": "confirmed", "handler": "H", "note": None},
             "conflict_strategy": "skip"},
            {"name": "imp-2", "description": "批量导入2",
             "filters": {"statuses": ["confirmed"], "event_ids": None,
                         "device_ids": None, "time_from": None, "time_to": None},
             "updates": {"status": "closed", "handler": "H", "note": None},
             "conflict_strategy": "abort"},
            {"name": "imp-3", "description": "批量导入3",
             "filters": {"statuses": ["false_positive"], "event_ids": None,
                         "device_ids": None, "time_from": None, "time_to": None},
             "updates": {"status": "closed", "handler": "H", "note": None},
             "conflict_strategy": "force"},
        ]
        path = self._make_export_json(tpls)
        result = self.template_manager.import_templates_from_file(path)
        self.assertEqual(result.total_count, 3)
        self.assertEqual(result.success_count, 3)
        for name in ["imp-1", "imp-2", "imp-3"]:
            self.assertIsNotNone(self.template_manager.get_template(name))

    def test_import_result_formatted(self):
        """导入结果格式化输出"""
        tpl = {
            "name": "fmt-imp",
            "description": "",
            "filters": {"statuses": ["unconfirmed"], "event_ids": None,
                        "device_ids": None, "time_from": None, "time_to": None},
            "updates": {"status": "confirmed", "handler": "H", "note": None},
            "conflict_strategy": "skip",
        }
        path = self._make_export_json([tpl])
        result = self.template_manager.import_templates_from_file(path)
        formatted = result.formatted()
        self.assertIn("总计", formatted)
        self.assertIn("成功", formatted)

    def test_import_invalid_version_raises(self):
        """导入版本不兼容的文件应报错"""
        data = {
            "version": "999.0",
            "exported_at": "2026-06-17 10:00:00",
            "template_count": 1,
            "templates": [{"name": "x", "description": "",
                           "filters": {}, "updates": {}}],
        }
        path = os.path.join(self.tmp_dir, "bad-version.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)

        with self.assertRaises(TemplateImportError) as ctx:
            self.template_manager.import_templates_from_file(path)
        self.assertIn("版本", str(ctx.exception))
        self.assertIn("999.0", str(ctx.exception))

    def test_import_missing_required_field_raises(self):
        """模板缺少必填字段应报错"""
        bad_tpl = {
            "description": "缺少name字段",
            "filters": {"statuses": ["unconfirmed"], "event_ids": None,
                        "device_ids": None, "time_from": None, "time_to": None},
            "updates": {"status": "confirmed", "handler": "H", "note": None},
        }
        path = self._make_export_json([bad_tpl])
        with self.assertRaises(TemplateImportError):
            self.template_manager.import_templates_from_file(path)


class TestTemplateImportConflict(_TestBase):
    """模板导入冲突策略测试"""

    def _make_export_json(self, templates: list[dict]) -> str:
        data = {
            "version": TEMPLATE_EXPORT_VERSION,
            "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "template_count": len(templates),
            "templates": templates,
        }
        path = os.path.join(self.tmp_dir, f"import-conflict-{id(self)}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return path

    def _make_tpl_dict(self, name: str, description: str = "测试模板") -> dict:
        return {
            "name": name, "description": description,
            "filters": {"statuses": ["unconfirmed"], "event_ids": None,
                        "device_ids": None, "time_from": None, "time_to": None},
            "updates": {"status": "confirmed", "handler": "H", "note": None},
            "conflict_strategy": "skip",
        }

    def test_conflict_strategy_skip(self):
        """skip 策略：同名模板跳过，不覆盖，明确输出跳过原因"""
        self.template_manager.save_template(
            name="conflict-tpl",
            description="原版本",
            batch_filter=BatchFilter(statuses=["unconfirmed"]),
            batch_update=BatchUpdate(status="confirmed", handler="Original"),
        )

        path = self._make_export_json([self._make_tpl_dict("conflict-tpl", "新版本")])
        result = self.template_manager.import_templates_from_file(
            path, conflict_strategy="skip",
        )
        self.assertEqual(result.total_count, 1)
        self.assertEqual(result.success_count, 0)
        self.assertEqual(result.skipped_count, 1)

        original = self.template_manager.get_template("conflict-tpl")
        self.assertEqual(original.description, "原版本")
        self.assertEqual(original.conflict_strategy, "skip")

        self.assertIsNotNone(result.log_id)
        self.assertEqual(len(result.items), 1)
        self.assertEqual(result.items[0].status, "skipped")
        self.assertIn("已存在", result.items[0].reason)
        self.assertFalse(result.has_errors)

    def test_conflict_strategy_overwrite(self):
        """overwrite 策略：同名模板覆盖"""
        self.template_manager.save_template(
            name="overwrite-tpl",
            description="原版本",
            batch_filter=BatchFilter(statuses=["unconfirmed"]),
            batch_update=BatchUpdate(status="confirmed", handler="Original"),
        )

        new_version = self._make_tpl_dict("overwrite-tpl", "覆盖后的版本")
        new_version["updates"]["status"] = "closed"
        new_version["updates"]["handler"] = "Overwriter"
        path = self._make_export_json([new_version])

        result = self.template_manager.import_templates_from_file(
            path, conflict_strategy="overwrite",
        )
        self.assertEqual(result.success_count, 1)
        self.assertEqual(result.overwritten_count, 1)

        loaded = self.template_manager.get_template("overwrite-tpl")
        self.assertEqual(loaded.description, "覆盖后的版本")
        bf, bu, cs = self.template_manager.template_to_objects(loaded)
        self.assertEqual(bu.status, "closed")
        self.assertEqual(bu.handler, "Overwriter")

    def test_conflict_strategy_rename(self):
        """rename 策略：同名模板自动重命名"""
        self.template_manager.save_template(
            name="rename-tpl",
            description="原版本",
            batch_filter=BatchFilter(statuses=["unconfirmed"]),
            batch_update=BatchUpdate(status="confirmed", handler="Original"),
        )

        path = self._make_export_json([self._make_tpl_dict("rename-tpl", "新版本")])
        result = self.template_manager.import_templates_from_file(
            path, conflict_strategy="rename",
        )
        self.assertEqual(result.success_count, 1)
        self.assertEqual(result.renamed_count, 1)
        self.assertEqual(len(result.items), 1)

        original = self.template_manager.get_template("rename-tpl")
        self.assertEqual(original.description, "原版本")

        new_name = result.items[0].final_name
        self.assertNotEqual(new_name, "rename-tpl")
        self.assertIn("rename-tpl", new_name)
        self.assertIn("imported", new_name)

        renamed = self.template_manager.get_template(new_name)
        self.assertIsNotNone(renamed)
        self.assertEqual(renamed.description, "新版本")

    def test_conflict_strategy_rename_multiple_conflicts(self):
        """rename 策略：多次重命名产生正确的递增后缀"""
        self.template_manager.save_template(
            name="multi-rename",
            description="原版本",
            batch_filter=BatchFilter(),
            batch_update=BatchUpdate(status="confirmed", handler="H"),
        )
        tpl_dict = self._make_tpl_dict("multi-rename", "导入版本")

        for i in range(3):
            path = self._make_export_json([tpl_dict])
            result = self.template_manager.import_templates_from_file(
                path, conflict_strategy="rename",
            )
            self.assertEqual(result.renamed_count, 1)

        all_names = [t.name for t in self.template_manager.list_templates()]
        self.assertIn("multi-rename", all_names)
        self.assertIn("multi-rename-imported-1", all_names)
        self.assertIn("multi-rename-imported-2", all_names)
        self.assertIn("multi-rename-imported-3", all_names)
        self.assertEqual(len(all_names), 4)

    def test_conflict_invalid_strategy_raises(self):
        """无效冲突策略应报错"""
        path = self._make_export_json([self._make_tpl_dict("bad-cs")])
        with self.assertRaises(TemplateImportError) as ctx:
            self.template_manager.import_templates_from_file(
                path, conflict_strategy="bogus",
            )
        self.assertIn("冲突策略", str(ctx.exception))


class TestTemplateImportCompatibility(_TestBase):
    """模板导入兼容性校验测试"""

    def _make_export_json(self, templates: list[dict]) -> str:
        data = {
            "version": TEMPLATE_EXPORT_VERSION,
            "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "template_count": len(templates),
            "templates": templates,
        }
        path = os.path.join(self.tmp_dir, f"compat-{id(self)}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return path

    def test_incompatible_status_enum_is_rejected(self):
        """状态枚举不兼容：模板使用了当前配置不存在的状态，应报错并说明原因"""
        bad_tpl = {
            "name": "bad-status-tpl",
            "description": "使用了不存在的状态",
            "filters": {"statuses": ["unconfirmed", "old_archived_status"],
                        "event_ids": None, "device_ids": None,
                        "time_from": None, "time_to": None},
            "updates": {"status": "confirmed", "handler": "H", "note": None},
            "conflict_strategy": "skip",
        }
        path = self._make_export_json([bad_tpl])
        result = self.template_manager.import_templates_from_file(path)

        self.assertEqual(result.success_count, 0)
        self.assertEqual(result.error_count, 1)
        self.assertTrue(result.has_errors)
        self.assertEqual(len(result.items), 1)
        self.assertIn("状态", result.items[0].reason)
        self.assertIn("old_archived_status", result.items[0].reason)

        loaded = self.template_manager.get_template("bad-status-tpl")
        self.assertIsNone(loaded, "不兼容模板不应被写入数据库")

    def test_incompatible_time_format_is_rejected(self):
        """时间格式不兼容：模板时间格式与当前配置不匹配，应报错并说明原因"""
        bad_tpl = {
            "name": "bad-time-tpl",
            "description": "使用了不兼容的时间格式",
            "filters": {"statuses": ["unconfirmed"], "event_ids": None,
                        "device_ids": None,
                        "time_from": "15/06/2026 08:30",
                        "time_to": "20/06/2026 18:00"},
            "updates": {"status": "confirmed", "handler": "H", "note": None},
            "conflict_strategy": "skip",
        }
        path = self._make_export_json([bad_tpl])
        result = self.template_manager.import_templates_from_file(path)

        self.assertEqual(result.success_count, 0)
        self.assertEqual(result.error_count, 1)
        self.assertTrue(result.has_errors)
        self.assertIn("时间", result.items[0].reason)
        self.assertIn("15/06/2026 08:30", result.items[0].reason)

    def test_incompatible_target_status_is_rejected(self):
        """目标状态不兼容：模板要更新到的状态不存在"""
        bad_tpl = {
            "name": "bad-target-tpl",
            "description": "目标状态不存在",
            "filters": {"statuses": ["unconfirmed"], "event_ids": None,
                        "device_ids": None,
                        "time_from": None, "time_to": None},
            "updates": {"status": "deprecated_status_xyz", "handler": "H", "note": None},
            "conflict_strategy": "skip",
        }
        path = self._make_export_json([bad_tpl])
        result = self.template_manager.import_templates_from_file(path)
        self.assertEqual(result.error_count, 1)
        self.assertIn("deprecated_status_xyz", result.items[0].reason)

    def test_empty_handler_is_rejected(self):
        """处理人为空字符串应报错"""
        bad_tpl = {
            "name": "empty-handler-tpl",
            "description": "处理人是空",
            "filters": {"statuses": ["unconfirmed"], "event_ids": None,
                        "device_ids": None, "time_from": None, "time_to": None},
            "updates": {"handler": "  ", "status": None, "note": None},
            "conflict_strategy": "skip",
        }
        path = self._make_export_json([bad_tpl])
        result = self.template_manager.import_templates_from_file(path)
        self.assertEqual(result.error_count, 1)
        self.assertIn("handler", result.items[0].reason.lower())

    def test_no_validate_skips_compatibility_check(self):
        """--no-validate 跳过兼容性检查，不兼容模板仍可导入"""
        bad_tpl = {
            "name": "no-validate-tpl",
            "description": "跳过校验",
            "filters": {"statuses": ["unconfirmed", "bad_status_123"],
                        "event_ids": None, "device_ids": None,
                        "time_from": None, "time_to": None},
            "updates": {"status": "confirmed", "handler": "H", "note": None},
            "conflict_strategy": "skip",
        }
        path = self._make_export_json([bad_tpl])
        result = self.template_manager.import_templates_from_file(
            path, validate_compatibility=False,
        )
        self.assertEqual(result.success_count, 1)
        self.assertIsNotNone(self.template_manager.get_template("no-validate-tpl"))

    def test_compatible_template_with_warnings_imports(self):
        """有警告但无错误的模板应成功导入（警告不阻止）"""
        warn_tpl = {
            "name": "warn-tpl",
            "description": "含警告的模板",
            "filters": {"statuses": ["unconfirmed"],
                        "device_ids": ["NOT-A-REAL-PATTERN-123"],
                        "event_ids": None,
                        "time_from": None, "time_to": None},
            "updates": {"status": "confirmed", "handler": "ValidHandler", "note": None},
            "conflict_strategy": "skip",
        }
        path = self._make_export_json([warn_tpl])
        result = self.template_manager.import_templates_from_file(path)
        self.assertEqual(result.success_count, 1)
        self.assertEqual(result.error_count, 0)
        self.assertIsNotNone(self.template_manager.get_template("warn-tpl"))


class TestTemplateImportRollback(_TestBase):
    """模板导入回滚机制测试"""

    def _make_export_json(self, templates: list[dict]) -> str:
        data = {
            "version": TEMPLATE_EXPORT_VERSION,
            "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "template_count": len(templates),
            "templates": templates,
        }
        path = os.path.join(self.tmp_dir, f"rollback-{id(self)}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return path

    def test_partial_error_rolls_back_all_imported_templates(self):
        """部分模板出错时，默认回滚所有已导入的模板"""
        good_tpl = {
            "name": "rollback-good-1",
            "description": "好模板1",
            "filters": {"statuses": ["unconfirmed"], "event_ids": None,
                        "device_ids": None, "time_from": None, "time_to": None},
            "updates": {"status": "confirmed", "handler": "H", "note": None},
            "conflict_strategy": "skip",
        }
        also_good = {
            "name": "rollback-good-2",
            "description": "好模板2",
            "filters": {"statuses": ["unconfirmed"], "event_ids": None,
                        "device_ids": None, "time_from": None, "time_to": None},
            "updates": {"status": "confirmed", "handler": "H", "note": None},
            "conflict_strategy": "skip",
        }
        bad_tpl = {
            "name": "rollback-bad",
            "description": "坏模板",
            "filters": {"statuses": ["nonexistent_status"], "event_ids": None,
                        "device_ids": None, "time_from": None, "time_to": None},
            "updates": {"status": "confirmed", "handler": "H", "note": None},
            "conflict_strategy": "skip",
        }
        path = self._make_export_json([good_tpl, also_good, bad_tpl])
        result = self.template_manager.import_templates_from_file(
            path, rollback_on_error=True,
        )

        self.assertTrue(result.has_errors)
        self.assertTrue(result.rolled_back)
        self.assertEqual(result.status, "rolled_back")

        for name in ["rollback-good-1", "rollback-good-2", "rollback-bad"]:
            self.assertIsNone(
                self.template_manager.get_template(name),
                f"回滚后模板 {name} 不应存在",
            )

    def test_no_rollback_preserves_successes(self):
        """--no-rollback：出错时保留已成功导入的模板"""
        good_tpl = {
            "name": "keep-good",
            "description": "保留的好模板",
            "filters": {"statuses": ["unconfirmed"], "event_ids": None,
                        "device_ids": None, "time_from": None, "time_to": None},
            "updates": {"status": "confirmed", "handler": "H", "note": None},
            "conflict_strategy": "skip",
        }
        bad_tpl = {
            "name": "keep-bad",
            "description": "坏模板",
            "filters": {"statuses": ["does_not_exist"], "event_ids": None,
                        "device_ids": None, "time_from": None, "time_to": None},
            "updates": {"status": "confirmed", "handler": "H", "note": None},
            "conflict_strategy": "skip",
        }
        path = self._make_export_json([good_tpl, bad_tpl])
        result = self.template_manager.import_templates_from_file(
            path, rollback_on_error=False,
        )

        self.assertTrue(result.has_errors)
        self.assertFalse(result.rolled_back)
        self.assertEqual(result.success_count, 1)
        self.assertEqual(result.error_count, 1)

        self.assertIsNotNone(self.template_manager.get_template("keep-good"))
        self.assertIsNone(self.template_manager.get_template("keep-bad"))

    def test_overwritten_templates_rolled_back(self):
        """回滚时，被 overwrite 的原模板应恢复"""
        self.template_manager.save_template(
            name="orig-overwrite",
            description="原始版本",
            batch_filter=BatchFilter(statuses=["unconfirmed"]),
            batch_update=BatchUpdate(status="confirmed", handler="OriginalHandler"),
        )
        orig_tpl = self.template_manager.get_template("orig-overwrite")
        orig_description = orig_tpl.description

        imported_version = {
            "name": "orig-overwrite",
            "description": "导入覆盖版本",
            "filters": {"statuses": ["unconfirmed"], "event_ids": None,
                        "device_ids": None, "time_from": None, "time_to": None},
            "updates": {"status": "closed", "handler": "OverwriteHandler", "note": None},
            "conflict_strategy": "skip",
        }
        bad_tpl = {
            "name": "rollback-trigger",
            "description": "触发回滚",
            "filters": {"statuses": ["invalid_status_xyz"], "event_ids": None,
                        "device_ids": None, "time_from": None, "time_to": None},
            "updates": {"status": "confirmed", "handler": "H", "note": None},
            "conflict_strategy": "skip",
        }
        path = self._make_export_json([imported_version, bad_tpl])
        result = self.template_manager.import_templates_from_file(
            path, conflict_strategy="overwrite", rollback_on_error=True,
        )

        self.assertTrue(result.rolled_back)
        restored = self.template_manager.get_template("orig-overwrite")
        self.assertIsNotNone(restored)
        self.assertEqual(restored.description, orig_description)
        bf, bu, cs = self.template_manager.template_to_objects(restored)
        self.assertEqual(bu.status, "confirmed")
        self.assertEqual(bu.handler, "OriginalHandler")


class TestTemplateImportPersistence(_TestBase):
    """模板导入后重启持久化测试"""

    def test_imported_template_survives_manager_restart(self):
        """导入后的模板在重启 TemplateManager（重新连接 SQLite）后仍然存在"""
        tpl = {
            "name": "survives-restart",
            "description": "重启后仍然存在的模板",
            "filters": {"statuses": ["unconfirmed", "confirmed"],
                        "device_ids": ["DEV-A001", "DEV-B002"],
                        "event_ids": None,
                        "time_from": "2026-06-15 08:00:00",
                        "time_to": "2026-06-15 18:00:00"},
            "updates": {"status": "closed", "handler": "PersistentUser",
                        "note": "持久化验证"},
            "conflict_strategy": "abort",
        }
        data = {
            "version": TEMPLATE_EXPORT_VERSION,
            "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "template_count": 1,
            "templates": [tpl],
        }
        path = os.path.join(self.tmp_dir, "persistent-import.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        result = self.template_manager.import_templates_from_file(path, operator="PersistentTester")
        self.assertEqual(result.success_count, 1)

        bf_before, bu_before, cs_before = self.template_manager.template_to_objects(
            self.template_manager.get_template("survives-restart"),
        )

        del self.template_manager
        del self.db

        new_db = Database(self.db_path)
        new_config = AppConfig(db_path=self.db_path)
        new_tm = TemplateManager(new_db, new_config)

        loaded = new_tm.get_template("survives-restart")
        self.assertIsNotNone(loaded, "重启后模板应存在")
        self.assertEqual(loaded.name, "survives-restart")
        self.assertEqual(loaded.description, "重启后仍然存在的模板")

        bf_after, bu_after, cs_after = new_tm.template_to_objects(loaded)
        self.assertEqual(bf_before.statuses, bf_after.statuses)
        self.assertEqual(bf_before.device_ids, bf_after.device_ids)
        self.assertEqual(bf_before.time_from, bf_after.time_from)
        self.assertEqual(bf_before.time_to, bf_after.time_to)
        self.assertEqual(bu_before.status, bu_after.status)
        self.assertEqual(bu_before.handler, bu_after.handler)
        self.assertEqual(bu_before.note, bu_after.note)
        self.assertEqual(cs_before, cs_after)

    def test_export_round_trip(self):
        """导出 -> 再导入的往返流程：数据完全一致"""
        self.template_manager.save_template(
            name="round-trip",
            description="往返测试",
            batch_filter=BatchFilter(
                statuses=["unconfirmed"],
                device_ids=["DEV-ABC123"],
                time_from="2026-06-01 00:00:00",
                time_to="2026-06-30 23:59:59",
            ),
            batch_update=BatchUpdate(
                status="closed",
                handler="RoundTripUser",
                note="往返测试备注",
            ),
            conflict_strategy="force",
        )
        original = self.template_manager.get_template("round-trip")
        bf_orig, bu_orig, cs_orig = self.template_manager.template_to_objects(original)

        export_path = os.path.join(self.tmp_dir, "round-trip.json")
        self.template_manager.export_templates_to_file(
            export_path, names=["round-trip"], operator="Exporter",
        )

        self.template_manager.delete_template("round-trip")
        self.assertIsNone(self.template_manager.get_template("round-trip"))

        result = self.template_manager.import_templates_from_file(
            export_path, operator="Importer",
        )
        self.assertEqual(result.success_count, 1)

        reloaded = self.template_manager.get_template("round-trip")
        self.assertIsNotNone(reloaded)
        self.assertEqual(reloaded.description, "往返测试")

        bf_new, bu_new, cs_new = self.template_manager.template_to_objects(reloaded)
        self.assertEqual(bf_orig.event_ids, bf_new.event_ids)
        self.assertEqual(bf_orig.device_ids, bf_new.device_ids)
        self.assertEqual(bf_orig.statuses, bf_new.statuses)
        self.assertEqual(bf_orig.time_from, bf_new.time_from)
        self.assertEqual(bf_orig.time_to, bf_new.time_to)
        self.assertEqual(bu_orig.status, bu_new.status)
        self.assertEqual(bu_orig.handler, bu_new.handler)
        self.assertEqual(bu_orig.note, bu_new.note)
        self.assertEqual(cs_orig, cs_new)


class TestTemplateImportLogs(_TestBase):
    """模板导入导出日志功能测试"""

    def test_export_creates_audit_log(self):
        """导出操作会创建审计日志"""
        self.template_manager.save_template(
            name="log-export-tpl",
            description="日志导出测试",
            batch_filter=BatchFilter(),
            batch_update=BatchUpdate(status="confirmed", handler="H"),
        )
        out_path = os.path.join(self.tmp_dir, "log-export.json")
        self.template_manager.export_templates_to_file(
            out_path, operator="LoggerUser",
        )

        logs_text = self.template_manager.get_template_import_logs(10)
        self.assertIn("导出", logs_text)
        self.assertIn("LoggerUser", logs_text)

    def test_import_creates_audit_log(self):
        """导入操作会创建审计日志"""
        tpl = {
            "name": "log-import-tpl",
            "description": "日志导入测试",
            "filters": {"statuses": ["unconfirmed"], "event_ids": None,
                        "device_ids": None, "time_from": None, "time_to": None},
            "updates": {"status": "confirmed", "handler": "H", "note": None},
            "conflict_strategy": "skip",
        }
        data = {
            "version": TEMPLATE_EXPORT_VERSION,
            "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "template_count": 1,
            "templates": [tpl],
        }
        path = os.path.join(self.tmp_dir, "log-import.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        result = self.template_manager.import_templates_from_file(
            path, operator="ImportLogger",
        )
        self.assertIsNotNone(result.log_id)

        logs_text = self.template_manager.get_template_import_logs(10)
        self.assertIn("导入", logs_text)
        self.assertIn("ImportLogger", logs_text)
        self.assertIn(result.log_id, logs_text)

    def test_log_detail_shows_item_results(self):
        """日志详情显示每个模板的处理结果"""
        self.template_manager.save_template(
            name="detail-conflict",
            description="已存在的模板",
            batch_filter=BatchFilter(),
            batch_update=BatchUpdate(status="confirmed", handler="H"),
        )
        tpl1 = {
            "name": "detail-new",
            "description": "新模板",
            "filters": {"statuses": ["unconfirmed"], "event_ids": None,
                        "device_ids": None, "time_from": None, "time_to": None},
            "updates": {"status": "confirmed", "handler": "H", "note": None},
            "conflict_strategy": "skip",
        }
        tpl2 = {
            "name": "detail-conflict",
            "description": "冲突模板",
            "filters": {"statuses": ["unconfirmed"], "event_ids": None,
                        "device_ids": None, "time_from": None, "time_to": None},
            "updates": {"status": "confirmed", "handler": "H", "note": None},
            "conflict_strategy": "skip",
        }
        data = {
            "version": TEMPLATE_EXPORT_VERSION,
            "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "template_count": 2,
            "templates": [tpl1, tpl2],
        }
        path = os.path.join(self.tmp_dir, "detail-import.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        result = self.template_manager.import_templates_from_file(
            path, conflict_strategy="skip", operator="DetailTester",
        )
        detail = self.template_manager.get_template_import_log_detail(result.log_id)
        self.assertIn("detail-new", detail)
        self.assertIn("detail-conflict", detail)
        self.assertIn("成功", detail)
        self.assertIn("跳过", detail)
        self.assertIn("DetailTester", detail)
        self.assertIn("skip", detail)

    def test_logs_empty_when_no_operations(self):
        """没有任何操作时日志为空"""
        text = self.template_manager.get_template_import_logs(10)
        self.assertIn("暂无", text)


if __name__ == "__main__":
    unittest.main()
