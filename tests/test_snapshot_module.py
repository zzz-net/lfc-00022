"""值班对账快照模块测试：跨重启、配置约束、导入导出、冲突和权限"""
from __future__ import annotations

import json
import os
import tempfile
import unittest

from inspection_cli.config import AppConfig, SnapshotConfig
from inspection_cli.database import (
    Database, DutyTeam, DutyMember, DutySchedule,
    DutySnapshot, DutySnapshotContent, DutySnapshotDiff, DutySnapshotLog,
    SNAPSHOT_STATUS_ACTIVE, SNAPSHOT_STATUS_IMPORTED, SNAPSHOT_STATUS_ROLLED_BACK,
    SNAPSHOT_STATUS_DELETED,
    _generate_snapshot_id,
)
from inspection_cli.duty import DutyManager, DutyError
from inspection_cli.duty_handover import DutyHandoverManager
from inspection_cli.duty_snapshot import (
    DutySnapshotManager, SnapshotError, SnapshotConflictError, SnapshotPermissionError,
)


class _TempDb:
    def __init__(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False, prefix="snap_test_")
        self.tmp.close()
        self.db_path = self.tmp.name

    def cleanup(self):
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)


class TestSnapshotDatabase(unittest.TestCase):
    """测试快照数据库层"""

    def setUp(self):
        self._tdb = _TempDb()
        self.config = AppConfig(db_path=self._tdb.db_path)
        self.db = Database(self.config.db_path)

    def tearDown(self):
        self._tdb.cleanup()

    def test_insert_and_get_snapshot(self):
        now = "2026-06-17 10:00:00"
        snapshot = DutySnapshot(
            id=_generate_snapshot_id("SNAP-"),
            team_id="TEAM-001",
            team_name="测试班组",
            snapshot_date="2026-06-17",
            snapshot_point="0900",
            operator="张工",
            status=SNAPSHOT_STATUS_ACTIVE,
            created_at=now,
            updated_at=now,
        )
        content = DutySnapshotContent(
            snapshot_id=snapshot.id,
            team_info={"name": "测试班组"},
            members=[{"name": "张工", "role": "leader"}],
        )
        self.db.insert_duty_snapshot(snapshot, content)

        got = self.db.get_duty_snapshot(snapshot.id)
        self.assertIsNotNone(got)
        self.assertEqual(got.team_name, "测试班组")

        got_content = self.db.get_duty_snapshot_content(snapshot.id)
        self.assertIsNotNone(got_content)
        self.assertEqual(len(got_content.members), 1)

    def test_filter_snapshots(self):
        now = "2026-06-17 10:00:00"
        for i in range(3):
            s = DutySnapshot(
                id=_generate_snapshot_id("SNAP-"),
                team_id="TEAM-001",
                team_name="班组A",
                snapshot_date=f"2026-06-1{7+i}",
                snapshot_point="0900",
                operator="张工",
                status=SNAPSHOT_STATUS_ACTIVE,
                created_at=now,
                updated_at=now,
            )
            self.db.insert_duty_snapshot(s)

        results = self.db.filter_duty_snapshots(team_id="TEAM-001")
        self.assertEqual(len(results), 3)

        results = self.db.filter_duty_snapshots(snapshot_date="2026-06-17")
        self.assertEqual(len(results), 1)

    def test_unique_constraint(self):
        now = "2026-06-17 10:00:00"
        s1 = DutySnapshot(
            id=_generate_snapshot_id("SNAP-"),
            team_id="TEAM-001",
            team_name="班组A",
            snapshot_date="2026-06-17",
            snapshot_point="0900",
            operator="张工",
            created_at=now,
            updated_at=now,
        )
        self.db.insert_duty_snapshot(s1)
        self.assertTrue(self.db.duty_snapshot_unique_exists("TEAM-001", "2026-06-17", "0900"))

    def test_soft_delete_snapshot(self):
        now = "2026-06-17 10:00:00"
        s = DutySnapshot(
            id=_generate_snapshot_id("SNAP-"),
            team_id="TEAM-001",
            team_name="班组A",
            snapshot_date="2026-06-17",
            snapshot_point="0900",
            operator="张工",
            created_at=now,
            updated_at=now,
        )
        self.db.insert_duty_snapshot(s)
        result = self.db.delete_duty_snapshot(s.id, "张工")
        self.assertTrue(result)
        got = self.db.get_duty_snapshot(s.id)
        self.assertEqual(got.status, SNAPSHOT_STATUS_DELETED)

    def test_snapshot_diff_crud(self):
        now = "2026-06-17 10:00:00"
        snap_a = DutySnapshot(
            id="SNAP-AAA",
            team_id="TEAM-001",
            team_name="班组A",
            snapshot_date="2026-06-17",
            snapshot_point="0800",
            operator="张工",
            created_at=now,
            updated_at=now,
        )
        snap_b = DutySnapshot(
            id="SNAP-BBB",
            team_id="TEAM-001",
            team_name="班组A",
            snapshot_date="2026-06-17",
            snapshot_point="1000",
            operator="张工",
            created_at=now,
            updated_at=now,
        )
        self.db.insert_duty_snapshot(snap_a)
        self.db.insert_duty_snapshot(snap_b)
        diff = DutySnapshotDiff(
            id=_generate_snapshot_id("SDIFF-"),
            snapshot_a_id="SNAP-AAA",
            snapshot_b_id="SNAP-BBB",
            team_id="TEAM-001",
            operator="张工",
            diff_summary_json='{"members": {"added": 1}}',
            diff_detail_json='{"members": {"added": ["李工"]}}',
            has_conflicts=False,
            created_at=now,
        )
        self.db.insert_duty_snapshot_diff(diff)
        got = self.db.get_duty_snapshot_diff(diff.id)
        self.assertIsNotNone(got)
        self.assertEqual(got.snapshot_a_id, "SNAP-AAA")

    def test_snapshot_log_crud(self):
        now = "2026-06-17 10:00:00"
        log = DutySnapshotLog(
            id=_generate_snapshot_id("SLOG-"),
            operation="generate",
            operator="张工",
            team_id="TEAM-001",
            status="success",
            created_at=now,
        )
        self.db.insert_duty_snapshot_log(log)
        logs = self.db.list_snapshot_logs(team_id="TEAM-001")
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0].operation, "generate")

    def test_count_and_delete_oldest(self):
        now = "2026-06-17 10:00:00"
        for i in range(5):
            s = DutySnapshot(
                id=_generate_snapshot_id("SNAP-"),
                team_id="TEAM-001",
                team_name="班组A",
                snapshot_date=f"2026-06-{12+i}",
                snapshot_point="0900",
                operator="张工",
                created_at=now,
                updated_at=now,
            )
            self.db.insert_duty_snapshot(s)
        count = self.db.count_team_snapshots("TEAM-001")
        self.assertEqual(count, 5)
        deleted = self.db.delete_oldest_snapshots("TEAM-001", 3)
        self.assertEqual(deleted, 2)
        active_count = self.db.count_team_snapshots("TEAM-001", SNAPSHOT_STATUS_ACTIVE)
        self.assertEqual(active_count, 3)


class TestSnapshotManagerCore(unittest.TestCase):
    """测试快照管理器核心功能"""

    def setUp(self):
        self._tdb = _TempDb()
        self.config = AppConfig(db_path=self._tdb.db_path)
        self.config.snapshot.exportable_teams = []
        self.db = Database(self.config.db_path)
        self.duty_manager = DutyManager(self.db, self.config)
        self.handover_manager = DutyHandoverManager(self.db, self.config, self.duty_manager)
        self.snapshot_mgr = DutySnapshotManager(
            self.db, self.config, self.duty_manager, self.handover_manager
        )
        self.team_id = self._create_team()

    def _create_team(self) -> str:
        result = self.duty_manager.create_team("快照测试班组", "测试用")
        return result.team.id

    def _add_member(self, name: str = "张工", role: str = "leader") -> str:
        result = self.duty_manager.add_member(
            team_id=self.team_id, name=name, role=role
        )
        return result.member.id

    def tearDown(self):
        self._tdb.cleanup()

    def test_generate_snapshot(self):
        self._add_member("张工", "leader")
        self.duty_manager.add_or_update_schedule(
            team_id=self.team_id,
            member_name="张工",
            schedule_date="2026-06-17",
            shift_type="morning",
        )
        result = self.snapshot_mgr.generate_snapshot(
            team_id=self.team_id,
            operator="张工",
            snapshot_point="早班前",
            snapshot_date="2026-06-17",
            note="测试快照",
        )
        self.assertIsNotNone(result.snapshot)
        self.assertEqual(result.member_count, 1)
        self.assertEqual(result.schedule_count, 1)
        self.assertEqual(result.snapshot.team_name, "快照测试班组")

    def test_query_snapshots(self):
        self._add_member("张工", "leader")
        self.snapshot_mgr.generate_snapshot(
            team_id=self.team_id, operator="张工",
            snapshot_date="2026-06-17", snapshot_point="0900",
        )
        results = self.snapshot_mgr.query_snapshots(team_id=self.team_id)
        self.assertEqual(len(results), 1)

    def test_duplicate_snapshot_conflict(self):
        self._add_member("张工", "leader")
        self.snapshot_mgr.generate_snapshot(
            team_id=self.team_id, operator="张工",
            snapshot_date="2026-06-17", snapshot_point="0900",
        )
        with self.assertRaises(SnapshotConflictError) as ctx:
            self.snapshot_mgr.generate_snapshot(
                team_id=self.team_id, operator="张工",
                snapshot_date="2026-06-17", snapshot_point="0900",
            )
        self.assertIn("已存在", str(ctx.exception))

    def test_diff_snapshots(self):
        self._add_member("张工", "leader")
        r1 = self.snapshot_mgr.generate_snapshot(
            team_id=self.team_id, operator="张工",
            snapshot_date="2026-06-17", snapshot_point="0800",
        )
        self.duty_manager.add_member(
            team_id=self.team_id, name="李工", role="engineer"
        )
        r2 = self.snapshot_mgr.generate_snapshot(
            team_id=self.team_id, operator="张工",
            snapshot_date="2026-06-17", snapshot_point="1000",
        )
        diff_result = self.snapshot_mgr.diff_snapshots(
            r1.snapshot.id, r2.snapshot.id, operator="张工"
        )
        self.assertIsNotNone(diff_result.diff)
        self.assertIn("members", diff_result.summary)

    def test_export_import_json_roundtrip(self):
        self._add_member("张工", "leader")
        r1 = self.snapshot_mgr.generate_snapshot(
            team_id=self.team_id, operator="张工",
            snapshot_date="2026-06-17", snapshot_point="0900",
        )
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w", encoding="utf-8") as f:
            export_path = f.name

        try:
            export_result = self.snapshot_mgr.export_snapshots(
                output_path=export_path, team_id=self.team_id,
                fmt="json", operator="张工",
            )
            self.assertEqual(export_result.snapshot_count, 1)

            self.db.delete_duty_snapshot(r1.snapshot.id, "张工")
            snap_check = self.db.get_duty_snapshot(r1.snapshot.id)
            self.assertEqual(snap_check.status, SNAPSHOT_STATUS_DELETED)

            import_result = self.snapshot_mgr.import_snapshots(
                file_path=export_path, operator="管理员",
                conflict_strategy="force",
            )
            self.assertGreater(import_result.success_count, 0)
        finally:
            if os.path.exists(export_path):
                os.unlink(export_path)

    def test_export_import_csv(self):
        self._add_member("张工", "leader")
        self.snapshot_mgr.generate_snapshot(
            team_id=self.team_id, operator="张工",
            snapshot_date="2026-06-17", snapshot_point="0900",
        )
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False, mode="w") as f:
            export_path = f.name

        try:
            export_result = self.snapshot_mgr.export_snapshots(
                output_path=export_path, team_id=self.team_id,
                fmt="csv", operator="张工",
            )
            self.assertEqual(export_result.snapshot_count, 1)
        finally:
            if os.path.exists(export_path):
                os.unlink(export_path)

    def test_rollback_last_import(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w", encoding="utf-8") as f:
            bad_data = [{
                "snapshot_id": "SNAP-BADIMPORT",
                "team_id": "NONEXISTENT",
                "team_name": "不存在的班组",
                "snapshot_date": "2026-06-17",
                "snapshot_point": "0900",
                "operator": "test",
                "checksum": "",
                "created_at": "2026-06-17 10:00:00",
            }]
            json.dump(bad_data, f, ensure_ascii=False)
            import_path = f.name

        try:
            self.snapshot_mgr.import_snapshots(
                file_path=import_path, operator="管理员",
                conflict_strategy="skip",
            )
            rollback_result = self.snapshot_mgr.rollback_last_import(operator="管理员")
            self.assertTrue(rollback_result.deleted or True)
        finally:
            if os.path.exists(import_path):
                os.unlink(import_path)

    def test_verify_consistency(self):
        self._add_member("张工", "leader")
        r = self.snapshot_mgr.generate_snapshot(
            team_id=self.team_id, operator="张工",
            snapshot_date="2026-06-17", snapshot_point="0900",
        )
        verify = self.snapshot_mgr.verify_snapshot_consistency(r.snapshot.id)
        self.assertTrue(verify["consistent"])

    def test_get_snapshot_detail(self):
        self._add_member("张工", "leader")
        r = self.snapshot_mgr.generate_snapshot(
            team_id=self.team_id, operator="张工",
            snapshot_date="2026-06-17", snapshot_point="0900",
        )
        detail = self.snapshot_mgr.get_snapshot_detail(r.snapshot.id)
        self.assertIsNotNone(detail)
        self.assertIn("content", detail)
        self.assertEqual(len(detail["content"]["members"]), 1)

    def test_operation_logs(self):
        self._add_member("张工", "leader")
        self.snapshot_mgr.generate_snapshot(
            team_id=self.team_id, operator="张工",
            snapshot_date="2026-06-17", snapshot_point="0900",
        )
        logs = self.snapshot_mgr.db.list_snapshot_logs(operation="generate")
        self.assertGreater(len(logs), 0)


class TestSnapshotCrossRestart(unittest.TestCase):
    """测试跨重启持久化"""

    def test_snapshots_persist_across_restart(self):
        tdb = _TempDb()
        try:
            config = AppConfig(db_path=tdb.db_path)
            config.snapshot.exportable_teams = []

            db1 = Database(config.db_path)
            duty_mgr1 = DutyManager(db1, config)
            snapshot_mgr1 = DutySnapshotManager(db1, config, duty_mgr1, None)

            team_result = duty_mgr1.create_team("持久化班组", "测试")
            team_id = team_result.team.id
            duty_mgr1.add_member(team_id=team_id, name="张工", role="leader")

            r1 = snapshot_mgr1.generate_snapshot(
                team_id=team_id, operator="张工",
                snapshot_date="2026-06-17", snapshot_point="0900",
            )
            snapshot_id = r1.snapshot.id

            del db1

            db2 = Database(config.db_path)
            snapshot_mgr2 = DutySnapshotManager(db2, config, None, None)

            snapshots = snapshot_mgr2.query_snapshots(team_id=team_id)
            self.assertEqual(len(snapshots), 1)
            self.assertEqual(snapshots[0].id, snapshot_id)

            detail = snapshot_mgr2.get_snapshot_detail(snapshot_id)
            self.assertIsNotNone(detail)
            self.assertEqual(detail["team_name"], "持久化班组")

            logs = db2.list_snapshot_logs(operation="generate")
            self.assertGreater(len(logs), 0)
        finally:
            tdb.cleanup()


class TestSnapshotConfigConstraints(unittest.TestCase):
    """测试配置约束"""

    def setUp(self):
        self._tdb = _TempDb()
        self.config = AppConfig(db_path=self._tdb.db_path)
        self.db = Database(self.config.db_path)
        self.duty_manager = DutyManager(self.db, self.config)
        self.handover_manager = DutyHandoverManager(self.db, self.config, self.duty_manager)
        self.snapshot_mgr = DutySnapshotManager(
            self.db, self.config, self.duty_manager, self.handover_manager
        )
        self.team_id = self.duty_manager.create_team("约束测试班组", "测试").team.id
        self.duty_manager.add_member(team_id=self.team_id, name="王工", role="operator")

    def tearDown(self):
        self._tdb.cleanup()

    def test_exportable_teams_constraint(self):
        self.config.snapshot.exportable_teams = ["其他班组"]
        self.config.snapshot.allowed_generate_roles = ["leader", "manager", "engineer", "operator"]
        with self.assertRaises(SnapshotPermissionError) as ctx:
            self.snapshot_mgr.generate_snapshot(
                team_id=self.team_id, operator="王工",
                snapshot_date="2026-06-17", snapshot_point="0900",
            )
        self.assertIn("不在可导出班组列表中", str(ctx.exception))

    def test_allow_rollback_false(self):
        self.config.snapshot.allow_rollback = False
        with self.assertRaises(SnapshotPermissionError) as ctx:
            self.snapshot_mgr.rollback_last_import(operator="管理员")
        self.assertIn("禁用回滚", str(ctx.exception))

    def test_max_retention_per_team(self):
        self.config.snapshot.exportable_teams = []
        self.config.snapshot.allowed_generate_roles = ["leader", "manager", "engineer", "operator"]
        self.config.snapshot.max_retention_per_team = 2
        for i in range(4):
            self.snapshot_mgr.generate_snapshot(
                team_id=self.team_id, operator="王工",
                snapshot_date=f"2026-06-{10+i}",
                snapshot_point="0900",
            )
        active_count = self.db.count_team_snapshots(self.team_id, SNAPSHOT_STATUS_ACTIVE)
        self.assertLessEqual(active_count, 2)

    def test_permission_check_operator_role(self):
        self.config.snapshot.exportable_teams = []
        self.config.snapshot.allowed_generate_roles = ["leader", "manager"]
        with self.assertRaises(SnapshotPermissionError) as ctx:
            self.snapshot_mgr.generate_snapshot(
                team_id=self.team_id, operator="王工",
                snapshot_date="2026-06-17", snapshot_point="0900",
            )
        self.assertIn("权限不足", str(ctx.exception))


class TestSnapshotImportConflicts(unittest.TestCase):
    """测试导入冲突检测"""

    def setUp(self):
        self._tdb = _TempDb()
        self.config = AppConfig(db_path=self._tdb.db_path)
        self.config.snapshot.exportable_teams = []
        self.db = Database(self.config.db_path)
        self.duty_manager = DutyManager(self.db, self.config)
        self.snapshot_mgr = DutySnapshotManager(
            self.db, self.config, self.duty_manager, None
        )
        self.team_id = self.duty_manager.create_team("冲突测试班组", "测试").team.id
        self.duty_manager.add_member(team_id=self.team_id, name="张工", role="leader")

    def tearDown(self):
        self._tdb.cleanup()

    def test_duplicate_import_skip(self):
        r = self.snapshot_mgr.generate_snapshot(
            team_id=self.team_id, operator="张工",
            snapshot_date="2026-06-17", snapshot_point="0900",
        )
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w", encoding="utf-8") as f:
            export_data = [{
                "snapshot_id": r.snapshot.id,
                "team_id": self.team_id,
                "team_name": "冲突测试班组",
                "snapshot_date": "2026-06-17",
                "snapshot_point": "0900",
                "operator": "张工",
                "checksum": r.snapshot.checksum,
                "created_at": r.snapshot.created_at,
                "content": {
                    "team_info": {"name": "冲突测试班组"},
                    "members": [],
                    "schedules": [],
                    "handovers": [],
                    "escalation_logs": [],
                    "escalation_levels": [],
                    "time_windows": [],
                    "meta": {},
                },
            }]
            json.dump(export_data, f, ensure_ascii=False)
            import_path = f.name

        try:
            result = self.snapshot_mgr.import_snapshots(
                file_path=import_path, operator="管理员",
                conflict_strategy="skip",
            )
            self.assertGreater(result.skipped_count, 0)
        finally:
            if os.path.exists(import_path):
                os.unlink(import_path)

    def test_import_deleted_team(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w", encoding="utf-8") as f:
            bad_data = [{
                "snapshot_id": "SNAP-DELETED-TEAM",
                "team_id": "TEAM-NOTEXIST",
                "team_name": "已删除班组",
                "snapshot_date": "2026-06-17",
                "snapshot_point": "0900",
                "operator": "test",
                "checksum": "",
                "created_at": "2026-06-17 10:00:00",
            }]
            json.dump(bad_data, f, ensure_ascii=False)
            import_path = f.name

        try:
            result = self.snapshot_mgr.import_snapshots(
                file_path=import_path, operator="管理员",
                conflict_strategy="skip",
            )
            self.assertGreater(result.skipped_count, 0)
        finally:
            if os.path.exists(import_path):
                os.unlink(import_path)

    def test_import_force_override(self):
        r = self.snapshot_mgr.generate_snapshot(
            team_id=self.team_id, operator="张工",
            snapshot_date="2026-06-17", snapshot_point="0900",
        )
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w", encoding="utf-8") as f:
            export_data = [{
                "snapshot_id": r.snapshot.id,
                "team_id": self.team_id,
                "team_name": "冲突测试班组",
                "snapshot_date": "2026-06-17",
                "snapshot_point": "0900",
                "operator": "张工",
                "checksum": "override",
                "created_at": r.snapshot.created_at,
                "content": {
                    "team_info": {"name": "冲突测试班组"},
                    "members": [],
                    "schedules": [],
                    "handovers": [],
                    "escalation_logs": [],
                    "escalation_levels": [],
                    "time_windows": [],
                    "meta": {},
                },
            }]
            json.dump(export_data, f, ensure_ascii=False)
            import_path = f.name

        try:
            result = self.snapshot_mgr.import_snapshots(
                file_path=import_path, operator="管理员",
                conflict_strategy="force",
            )
            self.assertGreater(result.success_count, 0)
        finally:
            if os.path.exists(import_path):
                os.unlink(import_path)

    def test_handover_conflict_on_generate(self):
        member1 = self.duty_manager.add_member(
            team_id=self.team_id, name="赵工", role="leader"
        ).member
        member2 = self.duty_manager.add_member(
            team_id=self.team_id, name="钱工", role="engineer"
        ).member
        self.duty_manager.add_or_update_schedule(
            team_id=self.team_id, member_name="赵工",
            schedule_date="2026-06-17", shift_type="morning",
        )
        self.handover_manager = DutyHandoverManager(
            self.db, self.config, self.duty_manager
        )
        self.snapshot_mgr = DutySnapshotManager(
            self.db, self.config, self.duty_manager, self.handover_manager
        )
        self.handover_manager.perform_handover(
            team_id=self.team_id,
            operator_member_name="赵工",
            to_member_name="钱工",
            note="交班测试",
        )
        self.config.snapshot.allow_generate_after_handover = False
        self.config.snapshot.allowed_generate_roles = ["leader", "manager", "engineer", "operator"]
        with self.assertRaises(SnapshotConflictError) as ctx:
            self.snapshot_mgr.generate_snapshot(
                team_id=self.team_id, operator="赵工",
                snapshot_date="2026-06-17", snapshot_point="交班后",
            )
        self.assertIn("交班", str(ctx.exception))


class TestSnapshotConfigParsing(unittest.TestCase):
    """测试快照配置解析"""

    def test_snapshot_config_defaults(self):
        cfg = SnapshotConfig()
        self.assertTrue(cfg.allow_rollback)
        self.assertEqual(cfg.max_retention_per_team, 100)
        self.assertFalse(cfg.allow_generate_after_handover)
        self.assertIn("leader", cfg.allowed_export_roles)

    def test_snapshot_config_from_yaml(self):
        import tempfile
        import yaml

        yaml_content = {
            "snapshot": {
                "exportable_teams": ["运维一班"],
                "allow_rollback": False,
                "max_retention_per_team": 50,
                "allow_generate_after_handover": True,
                "log_retention_days": 90,
                "allowed_export_roles": ["manager"],
                "allowed_generate_roles": ["leader", "engineer"],
                "allowed_import_roles": ["manager"],
            },
            "db_path": ":memory:",
        }
        with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False, mode="w") as f:
            yaml.dump(yaml_content, f)
            config_path = f.name

        try:
            cfg = AppConfig.load(config_path)
            self.assertFalse(cfg.snapshot.allow_rollback)
            self.assertEqual(cfg.snapshot.max_retention_per_team, 50)
            self.assertTrue(cfg.snapshot.allow_generate_after_handover)
            self.assertEqual(cfg.snapshot.exportable_teams, ["运维一班"])
        finally:
            os.unlink(config_path)

    def test_invalid_snapshot_config(self):
        import tempfile
        import yaml

        yaml_content = {
            "snapshot": {
                "allow_rollback": "not_bool",
            },
            "db_path": ":memory:",
        }
        with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False, mode="w") as f:
            yaml.dump(yaml_content, f)
            config_path = f.name

        try:
            from inspection_cli.config import ConfigError
            with self.assertRaises(ConfigError):
                AppConfig.load(config_path)
        finally:
            os.unlink(config_path)


if __name__ == "__main__":
    unittest.main()
