"""值班排班模块测试

覆盖：
  1. 基本操作（班组、人员、排班 CRUD）
  2. 跨重启持久化（排班数据、交班历史、升级日志）
  3. 配置约束（可用班组、最大升级层级、交班权限角色）
  4. 导入导出（CSV/JSON 往返、自动创建班组和人员）
  5. 冲突处理（同一时段冲突排班、重复导入、已撤销记录再次回滚）
  6. 权限控制（无权限交班）
  7. 撤销链路（交班 -> 撤销 -> 再次交班）
  8. 升级匹配（时间窗口、升级顺序、责任人命中）
"""
from __future__ import annotations

import csv
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta

from inspection_cli.config import AppConfig, DutyConfig
from inspection_cli.database import (
    Database,
    DUTY_HANDOVER_STATUS_ACTIVE, DUTY_HANDOVER_STATUS_REVOKED,
    DUTY_ESCALATION_STATUS_PENDING, DUTY_ESCALATION_STATUS_ESCALATED,
    DUTY_ESCALATION_STATUS_RESOLVED,
)
from inspection_cli.duty import (
    DutyManager, DutyError, DutyConflictError, DutyPermissionError,
)
from inspection_cli.duty_escalation import DutyEscalationEngine
from inspection_cli.duty_handover import DutyHandoverManager
from inspection_cli.duty_io import (
    DutyIOManager,
    DUTY_IMPORT_CONFLICT_SKIP, DUTY_IMPORT_CONFLICT_ABORT,
    DUTY_IMPORT_CONFLICT_FORCE,
)


class TestDutyBase(unittest.TestCase):
    """测试基类"""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp_dir, "test_duty.db")
        self.config = AppConfig(
            db_path=self.db_path,
            duty=DutyConfig(
                available_teams=["运维一班", "运维二班"],
                max_escalation_levels=3,
                handover_allowed_roles=["leader", "manager"],
                default_rollback_window_hours=24,
                log_retention_days=90,
            ),
        )
        self.db = Database(self.db_path)
        self.duty_manager = DutyManager(self.db, self.config)
        self.duty_escalation_engine = DutyEscalationEngine(
            self.db, self.config, self.duty_manager
        )
        self.duty_handover_manager = DutyHandoverManager(
            self.db, self.config, self.duty_manager
        )
        self.duty_io_manager = DutyIOManager(
            self.db, self.config, self.duty_manager
        )

    def _reopen_db(self):
        """模拟重启 - 重新创建所有管理器"""
        self.db = Database(self.db_path)
        self.duty_manager = DutyManager(self.db, self.config)
        self.duty_escalation_engine = DutyEscalationEngine(
            self.db, self.config, self.duty_manager
        )
        self.duty_handover_manager = DutyHandoverManager(
            self.db, self.config, self.duty_manager
        )
        self.duty_io_manager = DutyIOManager(
            self.db, self.config, self.duty_manager
        )

    def _create_team_and_members(self):
        """创建测试班组和人员"""
        team_result = self.duty_manager.create_team(
            name="运维一班",
            description="运维第一班组"
        )
        team_id = team_result.team.id

        self.duty_manager.add_member(
            team_id=team_id,
            name="张工",
            role="leader",
            phone="13800138001",
            email="zhang@example.com"
        )
        self.duty_manager.add_member(
            team_id=team_id,
            name="李工",
            role="engineer",
            phone="13800138002",
            email="li@example.com"
        )
        self.duty_manager.add_member(
            team_id=team_id,
            name="王工",
            role="operator",
            phone="13800138003",
            email="wang@example.com"
        )

        return team_id

    def _create_schedule(self, team_id: str, member_name: str,
                         schedule_date: str, shift_type: str,
                         escalation_level: int = 1) -> str:
        """创建测试排班"""
        result = self.duty_manager.add_or_update_schedule(
            team_id=team_id,
            member_name=member_name,
            schedule_date=schedule_date,
            shift_type=shift_type,
            escalation_level=escalation_level,
        )
        return result.schedule.id


class TestDutyBasicOperations(TestDutyBase):
    """测试基本操作"""

    def test_create_team(self):
        """测试创建班组"""
        result = self.duty_manager.create_team(
            name="运维一班",
            description="运维第一班组"
        )
        self.assertTrue(result.is_new)
        self.assertEqual(result.team.name, "运维一班")
        self.assertEqual(result.team.description, "运维第一班组")

        team = self.duty_manager.get_team(result.team.id)
        self.assertEqual(team.name, "运维一班")

    def test_create_duplicate_team(self):
        """测试创建重复班组名称"""
        self.duty_manager.create_team(name="运维一班")

        with self.assertRaises(DutyConflictError):
            self.duty_manager.create_team(name="运维一班")

    def test_create_team_not_in_available(self):
        """测试创建不在可用列表中的班组"""
        with self.assertRaises(DutyError):
            self.duty_manager.create_team(name="运维三班")

    def test_add_member(self):
        """测试添加人员"""
        team_id = self._create_team_and_members()

        members = self.duty_manager.list_members(team_id)
        self.assertEqual(len(members), 3)
        names = [m.name for m in members]
        self.assertIn("张工", names)
        self.assertIn("李工", names)
        self.assertIn("王工", names)

    def test_add_duplicate_member(self):
        """测试添加重复人员"""
        team_id = self._create_team_and_members()

        with self.assertRaises(DutyConflictError):
            self.duty_manager.add_member(
                team_id=team_id,
                name="张工",
                role="engineer"
            )

    def test_add_invalid_role(self):
        """测试添加无效角色"""
        team_id = self._create_team_and_members()

        with self.assertRaises(DutyError):
            self.duty_manager.add_member(
                team_id=team_id,
                name="赵工",
                role="invalid_role"
            )

    def test_create_schedule_morning(self):
        """测试创建早班排班"""
        team_id = self._create_team_and_members()
        today = datetime.now().strftime("%Y-%m-%d")

        schedule_id = self._create_schedule(
            team_id=team_id,
            member_name="张工",
            schedule_date=today,
            shift_type="morning",
            escalation_level=1
        )

        schedule = self.duty_manager.get_schedule(schedule_id)
        self.assertEqual(schedule.member_id, self.db.get_duty_member_by_name(team_id, "张工").id)
        self.assertEqual(schedule.start_time, "08:00")
        self.assertEqual(schedule.end_time, "16:00")
        self.assertEqual(schedule.escalation_level, 1)

    def test_create_schedule_custom(self):
        """测试创建自定义班次排班"""
        team_id = self._create_team_and_members()
        today = datetime.now().strftime("%Y-%m-%d")

        result = self.duty_manager.add_or_update_schedule(
            team_id=team_id,
            member_name="李工",
            schedule_date=today,
            shift_type="custom",
            start_time="10:00",
            end_time="14:00",
            escalation_level=2,
        )

        schedule = self.duty_manager.get_schedule(result.schedule.id)
        self.assertEqual(schedule.start_time, "10:00")
        self.assertEqual(schedule.end_time, "14:00")
        self.assertEqual(schedule.escalation_level, 2)

    def test_schedule_conflict_detection(self):
        """测试排班冲突检测"""
        team_id = self._create_team_and_members()
        today = datetime.now().strftime("%Y-%m-%d")

        self._create_schedule(
            team_id=team_id,
            member_name="张工",
            schedule_date=today,
            shift_type="morning",
        )

        with self.assertRaises(DutyConflictError):
            self._create_schedule(
                team_id=team_id,
                member_name="李工",
                schedule_date=today,
                shift_type="morning",
            )

    def test_schedule_overwrite_conflict(self):
        """测试覆盖冲突排班"""
        team_id = self._create_team_and_members()
        today = datetime.now().strftime("%Y-%m-%d")

        self._create_schedule(
            team_id=team_id,
            member_name="张工",
            schedule_date=today,
            shift_type="morning",
        )

        result = self.duty_manager.add_or_update_schedule(
            team_id=team_id,
            member_name="李工",
            schedule_date=today,
            shift_type="morning",
            overwrite=True,
        )
        self.assertEqual(result.schedule.member_id,
                         self.db.get_duty_member_by_name(team_id, "李工").id)
        self.assertEqual(len(result.conflicts), 1)

    def test_invalid_escalation_level(self):
        """测试无效的升级层级"""
        team_id = self._create_team_and_members()
        today = datetime.now().strftime("%Y-%m-%d")

        with self.assertRaises(DutyError):
            self._create_schedule(
                team_id=team_id,
                member_name="张工",
                schedule_date=today,
                shift_type="morning",
                escalation_level=5,
            )

    def test_delete_schedule(self):
        """测试删除排班"""
        team_id = self._create_team_and_members()
        today = datetime.now().strftime("%Y-%m-%d")

        schedule_id = self._create_schedule(
            team_id=team_id,
            member_name="张工",
            schedule_date=today,
            shift_type="morning",
        )

        self.assertTrue(self.duty_manager.delete_schedule(schedule_id))

        with self.assertRaises(DutyError):
            self.duty_manager.get_schedule(schedule_id)

    def test_set_escalation_levels(self):
        """测试设置升级层级"""
        team_id = self._create_team_and_members()

        levels = [
            {"level": 1, "name": "L1一线", "response_minutes": 30, "escalation_minutes": 60},
            {"level": 2, "name": "L2主管", "response_minutes": 15, "escalation_minutes": 30},
            {"level": 3, "name": "L3经理", "response_minutes": 5, "escalation_minutes": 15},
        ]

        result = self.duty_manager.set_escalation_levels(team_id, levels)
        self.assertEqual(len(result), 3)

        levels_from_db = self.duty_manager.get_escalation_levels(team_id)
        self.assertEqual(len(levels_from_db), 3)
        self.assertEqual(levels_from_db[0].name, "L1一线")
        self.assertEqual(levels_from_db[1].name, "L2主管")

    def test_set_time_windows(self):
        """测试设置时间窗口"""
        team_id = self._create_team_and_members()

        windows = [
            {"name": "工作时间", "start_time": "09:00", "end_time": "18:00",
             "days_of_week": "0,1,2,3,4", "priority": 1},
            {"name": "非工作时间", "start_time": "18:00", "end_time": "09:00",
             "days_of_week": "0,1,2,3,4,5,6", "priority": 2},
        ]

        result = self.duty_manager.set_time_windows(team_id, windows)
        self.assertEqual(len(result), 2)

        windows_from_db = self.duty_manager.get_time_windows(team_id)
        self.assertEqual(len(windows_from_db), 2)


class TestDutyCrossRestartPersistence(TestDutyBase):
    """测试跨重启持久化"""

    def test_team_persistence(self):
        """测试班组数据跨重启"""
        result = self.duty_manager.create_team(
            name="运维一班",
            description="测试班组"
        )
        team_id = result.team.id

        self._reopen_db()

        team = self.duty_manager.get_team(team_id)
        self.assertEqual(team.name, "运维一班")
        self.assertEqual(team.description, "测试班组")

    def test_member_persistence(self):
        """测试人员数据跨重启"""
        team_id = self._create_team_and_members()

        self._reopen_db()

        members = self.duty_manager.list_members(team_id)
        self.assertEqual(len(members), 3)

    def test_schedule_persistence(self):
        """测试排班数据跨重启"""
        team_id = self._create_team_and_members()
        today = datetime.now().strftime("%Y-%m-%d")
        schedule_id = self._create_schedule(
            team_id=team_id,
            member_name="张工",
            schedule_date=today,
            shift_type="morning",
        )

        self._reopen_db()

        schedule = self.duty_manager.get_schedule(schedule_id)
        self.assertEqual(schedule.schedule_date, today)
        self.assertEqual(schedule.shift_type, "morning")

    def test_handover_persistence(self):
        """测试交班历史跨重启"""
        team_id = self._create_team_and_members()
        today = datetime.now().strftime("%Y-%m-%d")
        self._create_schedule(
            team_id=team_id,
            member_name="张工",
            schedule_date=today,
            shift_type="day",
        )

        handover_result = self.duty_handover_manager.perform_handover(
            team_id=team_id,
            operator_member_name="张工",
            to_member_name="李工",
            note="临时有事，交接给李工"
        )
        handover_id = handover_result.handover.id

        self._reopen_db()

        handover = self.duty_handover_manager.get_handover(handover_id)
        self.assertEqual(handover.handover.status, DUTY_HANDOVER_STATUS_ACTIVE)
        self.assertEqual(handover.handover.note, "临时有事，交接给李工")

    def test_escalation_log_persistence(self):
        """测试升级日志跨重启"""
        team_id = self._create_team_and_members()
        today = datetime.now().strftime("%Y-%m-%d")
        self._create_schedule(
            team_id=team_id,
            member_name="张工",
            schedule_date=today,
            shift_type="day",
        )

        match_result = self.duty_escalation_engine.match_event(
            team_id=team_id,
            event_id="EVT-TEST-001",
            event_title="测试告警",
            event_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            handover_note="请优先处理"
        )
        self.assertTrue(match_result.success)
        log_id = match_result.log_id

        self._reopen_db()

        log_result = self.duty_escalation_engine.get_escalation_log(log_id)
        self.assertEqual(log_result.log.event_id, "EVT-TEST-001")
        self.assertEqual(log_result.log.status, DUTY_ESCALATION_STATUS_PENDING)

    def test_undo_chain_persistence(self):
        """测试撤销链路跨重启"""
        team_id = self._create_team_and_members()
        today = datetime.now().strftime("%Y-%m-%d")
        self._create_schedule(
            team_id=team_id,
            member_name="张工",
            schedule_date=today,
            shift_type="day",
        )

        self.duty_handover_manager.perform_handover(
            team_id=team_id,
            operator_member_name="张工",
            to_member_name="李工",
            note="第一次交班"
        )

        undo_result = self.duty_handover_manager.undo_last_handover(
            team_id=team_id,
            operator_member_name="张工"
        )
        self.assertTrue(undo_result.is_revoked)
        handover_id = undo_result.handover.id

        self._reopen_db()

        handover = self.duty_handover_manager.get_handover(handover_id)
        self.assertEqual(handover.handover.status, DUTY_HANDOVER_STATUS_REVOKED)
        self.assertIsNotNone(handover.handover.revoked_at)


class TestDutyConfigConstraints(TestDutyBase):
    """测试配置约束"""

    def test_available_teams_constraint(self):
        """测试可用班组约束"""
        with self.assertRaises(DutyError):
            self.duty_manager.create_team(name="运维三班")

    def test_max_escalation_levels_constraint(self):
        """测试最大升级层级约束"""
        team_id = self._create_team_and_members()

        levels = [
            {"level": 1, "name": "L1"},
            {"level": 2, "name": "L2"},
            {"level": 3, "name": "L3"},
            {"level": 4, "name": "L4"},
        ]

        with self.assertRaises(DutyError):
            self.duty_manager.set_escalation_levels(team_id, levels)

    def test_handover_permission_constraint(self):
        """测试交班权限约束"""
        team_id = self._create_team_and_members()
        today = datetime.now().strftime("%Y-%m-%d")
        self._create_schedule(
            team_id=team_id,
            member_name="李工",
            schedule_date=today,
            shift_type="day",
        )

        with self.assertRaises(DutyPermissionError):
            self.duty_handover_manager.perform_handover(
                team_id=team_id,
                operator_member_name="王工",
                to_member_name="张工"
            )

    def test_rollback_window_constraint(self):
        """测试回滚窗口约束"""
        team_id = self._create_team_and_members()
        today = datetime.now().strftime("%Y-%m-%d")
        self._create_schedule(
            team_id=team_id,
            member_name="张工",
            schedule_date=today,
            shift_type="day",
        )

        handover = self.duty_handover_manager.perform_handover(
            team_id=team_id,
            operator_member_name="张工",
            to_member_name="李工"
        )

        old_time = (datetime.now() - timedelta(hours=48)).strftime("%Y-%m-%d %H:%M:%S")
        with self.db._conn() as conn:
            conn.execute(
                "UPDATE duty_handovers SET handed_at = ? WHERE id = ?",
                (old_time, handover.handover.id)
            )

        with self.assertRaises(DutyConflictError):
            self.duty_handover_manager.undo_last_handover(
                team_id=team_id,
                operator_member_name="张工"
            )

    def test_valid_roles_constraint(self):
        """测试有效角色约束"""
        team_id = self._create_team_and_members()

        with self.assertRaises(DutyError):
            self.duty_manager.add_member(
                team_id=team_id,
                name="赵工",
                role="superadmin"
            )

    def test_custom_config_valid_roles(self):
        """测试自定义配置的有效角色"""
        custom_config = AppConfig(
            db_path=self.db_path,
            duty=DutyConfig(
                available_teams=["测试班组"],
                valid_roles=["admin", "user"],
            ),
        )
        custom_manager = DutyManager(self.db, custom_config)

        team_result = custom_manager.create_team(name="测试班组")
        team_id = team_result.team.id

        with self.assertRaises(DutyError):
            custom_manager.add_member(
                team_id=team_id,
                name="test",
                role="engineer"
            )

        result = custom_manager.add_member(
            team_id=team_id,
            name="test",
            role="admin"
        )
        self.assertEqual(result.member.role, "admin")


class TestDutyImportExport(TestDutyBase):
    """测试导入导出"""

    def test_export_csv(self):
        """测试导出 CSV"""
        team_id = self._create_team_and_members()
        today = datetime.now().strftime("%Y-%m-%d")
        self._create_schedule(team_id, "张工", today, "morning")
        self._create_schedule(team_id, "李工", today, "afternoon")

        export_path = os.path.join(self.tmp_dir, "schedules.csv")
        result = self.duty_io_manager.export_schedules(
            output_path=export_path,
            team_id=team_id,
            fmt="csv"
        )

        self.assertEqual(result.schedule_count, 2)
        self.assertTrue(os.path.exists(export_path))

        with open(export_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            self.assertEqual(len(rows), 2)
            self.assertIn(rows[0]["member_name"], ["张工", "李工"])
            self.assertEqual(rows[0]["schedule_date"], today)

    def test_export_json(self):
        """测试导出 JSON"""
        team_id = self._create_team_and_members()
        today = datetime.now().strftime("%Y-%m-%d")
        self._create_schedule(team_id, "张工", today, "morning")

        export_path = os.path.join(self.tmp_dir, "schedules.json")
        result = self.duty_io_manager.export_schedules(
            output_path=export_path,
            team_id=team_id,
            fmt="json"
        )

        self.assertEqual(result.schedule_count, 1)
        self.assertTrue(os.path.exists(export_path))

        with open(export_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            self.assertEqual(data["schedule_count"], 1)
            self.assertEqual(len(data["schedules"]), 1)
            self.assertEqual(data["schedules"][0]["member_name"], "张工")
            self.assertIn("teams", data)
            self.assertIn("members", data)

    def test_import_csv_skip_conflict(self):
        """测试导入 CSV - 跳过冲突"""
        team_id = self._create_team_and_members()
        today = datetime.now().strftime("%Y-%m-%d")
        self._create_schedule(team_id, "张工", today, "morning")

        csv_content = (
            "team_id,team_name,member_name,member_role,schedule_date,"
            "shift_type,start_time,end_time,escalation_level,note\n"
            f"{team_id},运维一班,张工,leader,{today},morning,08:00,16:00,1,\n"
            f"{team_id},运维一班,李工,engineer,{today},morning,08:00,16:00,1,\n"
        )

        import_path = os.path.join(self.tmp_dir, "import.csv")
        with open(import_path, "w", encoding="utf-8-sig") as f:
            f.write(csv_content)

        result = self.duty_io_manager.import_schedules(
            file_path=import_path,
            conflict_strategy=DUTY_IMPORT_CONFLICT_SKIP,
        )

        self.assertEqual(result.total_count, 2)
        self.assertEqual(result.success_count, 0)
        self.assertEqual(result.conflict_count, 2)

    def test_import_csv_force_conflict(self):
        """测试导入 CSV - 强制覆盖冲突"""
        team_id = self._create_team_and_members()
        today = datetime.now().strftime("%Y-%m-%d")
        self._create_schedule(team_id, "张工", today, "morning")

        csv_content = (
            "team_id,team_name,member_name,member_role,schedule_date,"
            "shift_type,start_time,end_time,escalation_level,note\n"
            f"{team_id},运维一班,李工,engineer,{today},morning,08:00,16:00,1,覆盖测试\n"
        )

        import_path = os.path.join(self.tmp_dir, "import.csv")
        with open(import_path, "w", encoding="utf-8-sig") as f:
            f.write(csv_content)

        result = self.duty_io_manager.import_schedules(
            file_path=import_path,
            conflict_strategy=DUTY_IMPORT_CONFLICT_FORCE,
        )

        self.assertEqual(result.total_count, 1)
        self.assertEqual(result.success_count, 1)
        self.assertEqual(result.conflict_count, 0)

        today_result = self.duty_manager.get_today_schedule(team_id)
        current = today_result.current_duty
        # 可能不在day班次时间，检查所有排班
        schedules = today_result.schedules
        self.assertEqual(len(schedules), 1)
        self.assertEqual(schedules[0].member_id,
                         self.db.get_duty_member_by_name(team_id, "李工").id)
        self.assertEqual(schedules[0].note, "覆盖测试")

    def test_export_import_roundtrip_json(self):
        """测试 JSON 导出再导入的往返"""
        team_id = self._create_team_and_members()
        today = datetime.now().strftime("%Y-%m-%d")
        self._create_schedule(team_id, "张工", today, "morning")
        self._create_schedule(team_id, "李工", today, "afternoon")

        export_path = os.path.join(self.tmp_dir, "export.json")
        self.duty_io_manager.export_schedules(
            output_path=export_path,
            team_id=team_id,
            fmt="json"
        )

        new_db_path = os.path.join(self.tmp_dir, "new_test.db")
        new_config = AppConfig(db_path=new_db_path, duty=self.config.duty)
        new_db = Database(new_db_path)
        new_duty_manager = DutyManager(new_db, new_config)
        new_io_manager = DutyIOManager(new_db, new_config, new_duty_manager)

        import_result = new_io_manager.import_schedules(
            file_path=export_path,
            conflict_strategy=DUTY_IMPORT_CONFLICT_SKIP,
            auto_create_teams=True,
            auto_create_members=True,
        )

        self.assertEqual(import_result.success_count, 2)
        self.assertEqual(import_result.total_count, 2)

        new_team = new_db.get_all_duty_teams()[0]
        schedules = new_db.get_duty_schedules_by_date(new_team.id, today)
        self.assertEqual(len(schedules), 2)

    def test_import_auto_create_teams_and_members(self):
        """测试导入时自动创建班组和人员"""
        import_content = {
            "version": "1.0",
            "teams": [{"id": "TEAM-001", "name": "运维一班", "description": "导入班组"}],
            "members": [
                {"id": "MEM-001", "team_id": "TEAM-001", "name": "赵工", "role": "engineer"},
            ],
            "schedules": [
                {
                    "team_id": "TEAM-001",
                    "team_name": "运维一班",
                    "member_name": "赵工",
                    "member_role": "engineer",
                    "schedule_date": datetime.now().strftime("%Y-%m-%d"),
                    "shift_type": "morning",
                    "escalation_level": 1,
                }
            ],
        }

        import_path = os.path.join(self.tmp_dir, "auto_import.json")
        with open(import_path, "w", encoding="utf-8") as f:
            json.dump(import_content, f)

        result = self.duty_io_manager.import_schedules(
            file_path=import_path,
            conflict_strategy=DUTY_IMPORT_CONFLICT_SKIP,
            auto_create_teams=True,
            auto_create_members=True,
        )

        self.assertEqual(result.success_count, 1)

        teams = self.duty_manager.list_teams()
        self.assertEqual(len(teams), 1)
        self.assertEqual(teams[0].name, "运维一班")

        members = self.duty_manager.list_members(teams[0].id)
        self.assertEqual(len(members), 1)
        self.assertEqual(members[0].name, "赵工")

    def test_import_abort_on_error(self):
        """测试导入出错时中止"""
        team_id = self._create_team_and_members()
        today = datetime.now().strftime("%Y-%m-%d")

        csv_content = (
            "team_id,team_name,member_name,member_role,schedule_date,"
            "shift_type,start_time,end_time,escalation_level,note\n"
            f"{team_id},运维一班,张工,leader,{today},morning,08:00,16:00,1,\n"
            f"{team_id},运维一班,不存在的人,leader,{today},morning,08:00,16:00,1,\n"
        )

        import_path = os.path.join(self.tmp_dir, "import.csv")
        with open(import_path, "w", encoding="utf-8-sig") as f:
            f.write(csv_content)

        result = self.duty_io_manager.import_schedules(
            file_path=import_path,
            conflict_strategy=DUTY_IMPORT_CONFLICT_ABORT,
        )

        self.assertEqual(result.total_count, 2)
        self.assertEqual(result.error_count, 1)


class TestDutyConflictHandling(TestDutyBase):
    """测试冲突处理"""

    def test_time_slot_conflict(self):
        """测试同一时段冲突排班"""
        team_id = self._create_team_and_members()
        today = datetime.now().strftime("%Y-%m-%d")

        self._create_schedule(team_id, "张工", today, "morning")

        with self.assertRaises(DutyConflictError):
            self._create_schedule(team_id, "李工", today, "morning")

    def test_partial_time_overlap_conflict(self):
        """测试部分时间重叠冲突"""
        team_id = self._create_team_and_members()
        today = datetime.now().strftime("%Y-%m-%d")

        self.duty_manager.add_or_update_schedule(
            team_id=team_id,
            member_name="张工",
            schedule_date=today,
            shift_type="custom",
            start_time="08:00",
            end_time="12:00",
        )

        with self.assertRaises(DutyConflictError):
            self.duty_manager.add_or_update_schedule(
                team_id=team_id,
                member_name="李工",
                schedule_date=today,
                shift_type="custom",
                start_time="10:00",
                end_time="14:00",
            )

    def test_duplicate_import_skip(self):
        """测试重复导入 - 跳过"""
        team_id = self._create_team_and_members()
        today = datetime.now().strftime("%Y-%m-%d")
        self._create_schedule(team_id, "张工", today, "morning")

        csv_content = (
            "team_id,team_name,member_name,member_role,schedule_date,"
            "shift_type,start_time,end_time,escalation_level,note\n"
            f"{team_id},运维一班,张工,leader,{today},morning,08:00,16:00,1,\n"
        )

        import_path = os.path.join(self.tmp_dir, "dup_import.csv")
        with open(import_path, "w", encoding="utf-8-sig") as f:
            f.write(csv_content)

        result = self.duty_io_manager.import_schedules(
            file_path=import_path,
            conflict_strategy=DUTY_IMPORT_CONFLICT_SKIP,
        )

        self.assertEqual(result.conflict_count, 1)
        self.assertEqual(result.success_count, 0)

    def test_already_revoked_handover_undo(self):
        """测试已撤销记录再次回滚"""
        team_id = self._create_team_and_members()
        today = datetime.now().strftime("%Y-%m-%d")
        self._create_schedule(team_id, "张工", today, "day")

        self.duty_handover_manager.perform_handover(
            team_id=team_id,
            operator_member_name="张工",
            to_member_name="李工"
        )

        self.duty_handover_manager.undo_last_handover(
            team_id=team_id,
            operator_member_name="张工"
        )

        with self.assertRaises(DutyConflictError):
            self.duty_handover_manager.undo_last_handover(
                team_id=team_id,
                operator_member_name="张工"
            )

    def test_export_reimport_conflict_force(self):
        """测试导出后再导回 - 强制覆盖"""
        team_id = self._create_team_and_members()
        today = datetime.now().strftime("%Y-%m-%d")
        self._create_schedule(team_id, "张工", today, "morning")

        export_path = os.path.join(self.tmp_dir, "reimport.json")
        self.duty_io_manager.export_schedules(
            output_path=export_path,
            team_id=team_id,
            fmt="json"
        )

        with open(export_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        data["schedules"][0]["member_name"] = "李工"
        data["schedules"][0]["note"] = "修改后重新导入"

        with open(export_path, "w", encoding="utf-8") as f:
            json.dump(data, f)

        result = self.duty_io_manager.import_schedules(
            file_path=export_path,
            conflict_strategy=DUTY_IMPORT_CONFLICT_FORCE,
        )

        self.assertEqual(result.success_count, 1)

        schedule = self.db.get_duty_schedule(schedule_id=data["schedules"][0]["id"])
        if schedule:
            self.assertEqual(schedule.note, "修改后重新导入")


class TestDutyPermissionControl(TestDutyBase):
    """测试权限控制"""

    def test_operator_cannot_handover(self):
        """测试 operator 角色无法交班"""
        team_id = self._create_team_and_members()
        today = datetime.now().strftime("%Y-%m-%d")
        self._create_schedule(team_id, "李工", today, "day")

        with self.assertRaises(DutyPermissionError):
            self.duty_handover_manager.perform_handover(
                team_id=team_id,
                operator_member_name="王工",
                to_member_name="张工"
            )

    def test_operator_cannot_undo(self):
        """测试 operator 角色无法撤销交班"""
        team_id = self._create_team_and_members()
        today = datetime.now().strftime("%Y-%m-%d")
        self._create_schedule(team_id, "张工", today, "day")

        self.duty_handover_manager.perform_handover(
            team_id=team_id,
            operator_member_name="张工",
            to_member_name="李工"
        )

        with self.assertRaises(DutyPermissionError):
            self.duty_handover_manager.undo_last_handover(
                team_id=team_id,
                operator_member_name="王工"
            )

    def test_leader_can_handover(self):
        """测试 leader 角色可以交班"""
        team_id = self._create_team_and_members()
        today = datetime.now().strftime("%Y-%m-%d")
        self._create_schedule(team_id, "张工", today, "day")

        result = self.duty_handover_manager.perform_handover(
            team_id=team_id,
            operator_member_name="张工",
            to_member_name="李工"
        )

        self.assertEqual(result.handover.status, DUTY_HANDOVER_STATUS_ACTIVE)

    def test_custom_permission_roles(self):
        """测试自定义权限角色"""
        custom_config = AppConfig(
            db_path=self.db_path,
            duty=DutyConfig(
                available_teams=["测试班组"],
                handover_allowed_roles=["admin"],
                valid_roles=["leader", "engineer", "operator", "manager", "admin"],
            ),
        )
        custom_manager = DutyManager(self.db, custom_config)
        custom_handover_manager = DutyHandoverManager(
            self.db, custom_config, custom_manager
        )

        custom_manager.create_team(name="测试班组")
        team_id = custom_manager.list_teams()[0].id

        custom_manager.add_member(team_id, "admin_user", "admin")
        custom_manager.add_member(team_id, "leader_user", "leader")
        custom_manager.add_member(team_id, "engineer_user", "engineer")

        today = datetime.now().strftime("%Y-%m-%d")
        custom_manager.add_or_update_schedule(
            team_id=team_id,
            member_name="leader_user",
            schedule_date=today,
            shift_type="day",
        )

        with self.assertRaises(DutyPermissionError):
            custom_handover_manager.perform_handover(
                team_id=team_id,
                operator_member_name="leader_user",
                to_member_name="engineer_user"
            )

        result = custom_handover_manager.perform_handover(
            team_id=team_id,
            operator_member_name="admin_user",
            to_member_name="engineer_user"
        )
        self.assertEqual(result.handover.status, DUTY_HANDOVER_STATUS_ACTIVE)


class TestDutyUndoChain(TestDutyBase):
    """测试撤销链路"""

    def test_handover_undo_chain(self):
        """测试完整的交班-撤销-再交班链路"""
        team_id = self._create_team_and_members()
        today = datetime.now().strftime("%Y-%m-%d")
        self._create_schedule(team_id, "张工", today, "day")

        handover1 = self.duty_handover_manager.perform_handover(
            team_id=team_id,
            operator_member_name="张工",
            to_member_name="李工",
            note="第一次交班"
        )
        self.assertEqual(handover1.handover.status, DUTY_HANDOVER_STATUS_ACTIVE)

        undo1 = self.duty_handover_manager.undo_last_handover(
            team_id=team_id,
            operator_member_name="张工"
        )
        self.assertTrue(undo1.is_revoked)
        self.assertEqual(undo1.handover.status, DUTY_HANDOVER_STATUS_REVOKED)

        handover2 = self.duty_handover_manager.perform_handover(
            team_id=team_id,
            operator_member_name="张工",
            to_member_name="王工",
            note="第二次交班"
        )
        self.assertEqual(handover2.handover.status, DUTY_HANDOVER_STATUS_ACTIVE)

        history = self.duty_handover_manager.list_handover_history(team_id)
        self.assertEqual(len(history), 2)

        statuses = [h.handover.status for h in history]
        self.assertIn(DUTY_HANDOVER_STATUS_REVOKED, statuses)
        self.assertIn(DUTY_HANDOVER_STATUS_ACTIVE, statuses)

    def test_note_preserved_in_undo(self):
        """测试撤销时备注保留"""
        team_id = self._create_team_and_members()
        today = datetime.now().strftime("%Y-%m-%d")
        self._create_schedule(team_id, "张工", today, "day")

        self.duty_handover_manager.perform_handover(
            team_id=team_id,
            operator_member_name="张工",
            to_member_name="李工",
            note="重要事项：处理服务器告警"
        )

        undo_result = self.duty_handover_manager.undo_last_handover(
            team_id=team_id,
            operator_member_name="张工"
        )

        self.assertEqual(undo_result.handover.note, "重要事项：处理服务器告警")
        self.assertIsNotNone(undo_result.handover.revoked_at)
        self.assertIsNotNone(undo_result.handover.revoked_by)

    def test_multiple_handover_undo_only_last(self):
        """测试多次交班后只能撤销最后一次"""
        team_id = self._create_team_and_members()
        today = datetime.now().strftime("%Y-%m-%d")
        self._create_schedule(team_id, "张工", today, "day")

        handover1 = self.duty_handover_manager.perform_handover(
            team_id=team_id,
            operator_member_name="张工",
            to_member_name="李工",
            note="第一次"
        )

        handover2 = self.duty_handover_manager.perform_handover(
            team_id=team_id,
            operator_member_name="张工",
            to_member_name="王工",
            note="第二次"
        )

        undo_result = self.duty_handover_manager.undo_last_handover(
            team_id=team_id,
            operator_member_name="张工"
        )

        self.assertEqual(undo_result.handover.id, handover2.handover.id)
        self.assertEqual(undo_result.handover.note, "第二次")

        handover1_updated = self.duty_handover_manager.get_handover(handover1.handover.id)
        self.assertEqual(handover1_updated.handover.status, DUTY_HANDOVER_STATUS_REVOKED)


class TestDutyEscalationMatching(TestDutyBase):
    """测试升级匹配"""

    def test_match_event_success(self):
        """测试成功匹配事件责任人"""
        team_id = self._create_team_and_members()
        today = datetime.now().strftime("%Y-%m-%d")
        self._create_schedule(team_id, "张工", today, "day")

        result = self.duty_escalation_engine.match_event(
            team_id=team_id,
            event_id="EVT-001",
            event_title="服务器CPU告警",
            handover_note="请尽快处理"
        )

        self.assertTrue(result.success)
        self.assertIsNotNone(result.log_id)
        self.assertEqual(result.member.name, "张工")
        self.assertEqual(result.schedule.shift_type, "day")

    def test_match_event_no_schedule(self):
        """测试无排班时匹配失败"""
        team_id = self._create_team_and_members()

        result = self.duty_escalation_engine.match_event(
            team_id=team_id,
            event_id="EVT-002",
            event_title="测试告警"
        )

        self.assertFalse(result.success)
        self.assertIn("没有可用的值班人员", result.message)

    def test_match_with_time_window(self):
        """测试带时间窗口的匹配"""
        team_id = self._create_team_and_members()

        windows = [
            {"name": "工作时间", "start_time": "09:00", "end_time": "18:00",
             "days_of_week": "0,1,2,3,4", "priority": 1},
        ]
        self.duty_manager.set_time_windows(team_id, windows)

        today = datetime.now().strftime("%Y-%m-%d")
        self._create_schedule(team_id, "张工", today, "day")

        result = self.duty_escalation_engine.match_event(
            team_id=team_id,
            event_id="EVT-003",
            event_title="数据库连接异常",
        )

        self.assertTrue(result.success)
        self.assertIsNotNone(result.time_window)
        self.assertEqual(result.time_window.name, "工作时间")

    def test_match_with_escalation_levels(self):
        """测试带升级层级的匹配"""
        team_id = self._create_team_and_members()

        levels = [
            {"level": 1, "name": "L1一线", "response_minutes": 30, "escalation_minutes": 60},
            {"level": 2, "name": "L2主管", "response_minutes": 15, "escalation_minutes": 30},
        ]
        self.duty_manager.set_escalation_levels(team_id, levels)

        today = datetime.now().strftime("%Y-%m-%d")
        self._create_schedule(team_id, "张工", today, "day", escalation_level=1)

        result = self.duty_escalation_engine.match_event(
            team_id=team_id,
            event_id="EVT-004",
            event_title="高危漏洞告警",
            min_level=1,
        )

        self.assertTrue(result.success)
        self.assertIsNotNone(result.escalation_level)
        self.assertEqual(result.escalation_level.level, 1)
        self.assertEqual(result.escalation_level.name, "L1一线")

    def test_get_escalation_log_detail(self):
        """测试获取升级日志详情"""
        team_id = self._create_team_and_members()
        today = datetime.now().strftime("%Y-%m-%d")
        self._create_schedule(team_id, "张工", today, "day")

        match_result = self.duty_escalation_engine.match_event(
            team_id=team_id,
            event_id="EVT-DETAIL",
            event_title="详情测试告警",
            handover_note="测试备注"
        )

        log_result = self.duty_escalation_engine.get_escalation_log(match_result.log_id)
        self.assertEqual(log_result.log.event_id, "EVT-DETAIL")
        self.assertEqual(log_result.log.status, DUTY_ESCALATION_STATUS_PENDING)
        self.assertEqual(log_result.member.name, "张工")
        self.assertEqual(log_result.log.handover_note, "测试备注")

    def test_acknowledge_and_resolve_log(self):
        """测试确认和解决升级日志"""
        team_id = self._create_team_and_members()
        today = datetime.now().strftime("%Y-%m-%d")
        self._create_schedule(team_id, "张工", today, "day")

        match_result = self.duty_escalation_engine.match_event(
            team_id=team_id,
            event_id="EVT-ACK",
            event_title="确认测试",
        )
        log_id = match_result.log_id

        self.duty_escalation_engine.acknowledge_log(log_id, "operator1")
        log_result = self.duty_escalation_engine.get_escalation_log(log_id)
        self.assertEqual(log_result.log.status, DUTY_ESCALATION_STATUS_ESCALATED)
        self.assertIsNotNone(log_result.log.acknowledged_at)

        self.duty_escalation_engine.resolve_log(log_id, "operator1", "已修复")
        log_result = self.duty_escalation_engine.get_escalation_log(log_id)
        self.assertEqual(log_result.log.status, DUTY_ESCALATION_STATUS_RESOLVED)
        self.assertIsNotNone(log_result.log.resolved_at)
        self.assertIn("已修复", log_result.log.handover_note)

    def test_list_logs_with_filter(self):
        """测试按筛选条件列出升级日志"""
        team_id = self._create_team_and_members()
        today = datetime.now().strftime("%Y-%m-%d")
        self._create_schedule(team_id, "张工", today, "day")

        for i in range(5):
            self.duty_escalation_engine.match_event(
                team_id=team_id,
                event_id=f"EVT-FILTER-{i}",
                event_title=f"测试告警 {i}",
            )

        all_logs = self.duty_escalation_engine.list_escalation_logs(
            team_id=team_id,
            status=DUTY_ESCALATION_STATUS_PENDING,
        )
        self.assertEqual(len(all_logs), 5)

        first_log_id = all_logs[0].log.id
        self.duty_escalation_engine.resolve_log(first_log_id, "admin", "解决")

        pending_logs = self.duty_escalation_engine.list_escalation_logs(
            team_id=team_id,
            status=DUTY_ESCALATION_STATUS_PENDING,
        )
        self.assertEqual(len(pending_logs), 4)

        resolved_logs = self.duty_escalation_engine.list_escalation_logs(
            team_id=team_id,
            status=DUTY_ESCALATION_STATUS_RESOLVED,
        )
        self.assertEqual(len(resolved_logs), 1)

    def test_batch_match_events(self):
        """测试批量匹配事件"""
        team_id = self._create_team_and_members()
        today = datetime.now().strftime("%Y-%m-%d")
        self._create_schedule(team_id, "张工", today, "day")

        events = [
            {"event_id": "EVT-BATCH-1", "event_title": "批量事件1"},
            {"event_id": "EVT-BATCH-2", "event_title": "批量事件2"},
            {"event_id": "EVT-BATCH-3", "event_title": "批量事件3"},
        ]

        result = self.duty_escalation_engine.batch_match_events(team_id, events)

        self.assertEqual(result.total, 3)
        self.assertEqual(result.matched, 3)
        self.assertEqual(result.failed, 0)

        for m in result.matches:
            self.assertTrue(m.success)
            self.assertEqual(m.member.name, "张工")


if __name__ == "__main__":
    unittest.main()
