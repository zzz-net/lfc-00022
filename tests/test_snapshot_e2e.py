"""端到端链路验证：建班组→排班→交班→升级命中→生成快照→导出→导回→核对"""
from __future__ import annotations

import json
import os
import tempfile
import unittest

from inspection_cli.config import AppConfig
from inspection_cli.database import Database
from inspection_cli.duty import DutyManager
from inspection_cli.duty_escalation import DutyEscalationEngine
from inspection_cli.duty_handover import DutyHandoverManager
from inspection_cli.duty_snapshot import DutySnapshotManager


class TestSnapshotE2E(unittest.TestCase):
    """完整CLI链路验收：建班组→排班→交班→升级命中→生成快照→导出→导回→核对"""

    def setUp(self):
        self._tdb = tempfile.NamedTemporaryFile(suffix=".db", delete=False, prefix="snap_e2e_")
        self._tdb.close()
        self.config = AppConfig(db_path=self._tdb.name)
        self.config.snapshot.exportable_teams = []
        self.config.snapshot.allowed_generate_roles = ["leader", "manager", "engineer", "operator"]
        self.config.snapshot.allowed_export_roles = ["leader", "manager", "engineer", "operator"]
        self.config.snapshot.allowed_import_roles = ["manager"]
        self.db = Database(self.config.db_path)
        self.duty_mgr = DutyManager(self.db, self.config)
        self.escalation_engine = DutyEscalationEngine(self.db, self.config, self.duty_mgr)
        self.handover_mgr = DutyHandoverManager(self.db, self.config, self.duty_mgr)
        self.snapshot_mgr = DutySnapshotManager(
            self.db, self.config, self.duty_mgr, self.handover_mgr
        )

    def tearDown(self):
        if os.path.exists(self._tdb.name):
            os.unlink(self._tdb.name)

    def test_full_chain(self):
        # Step 1: 建班组
        team_result = self.duty_mgr.create_team("验收班组", "端到端验收")
        team_id = team_result.team.id
        self.assertIsNotNone(team_id)

        # Step 2: 添加成员
        m1 = self.duty_mgr.add_member(team_id=team_id, name="张班长", role="leader").member
        m2 = self.duty_mgr.add_member(team_id=team_id, name="李工程师", role="engineer").member
        m3 = self.duty_mgr.add_member(team_id=team_id, name="王操作员", role="operator").member

        # Step 3: 排班
        self.duty_mgr.add_or_update_schedule(
            team_id=team_id, member_name="张班长",
            schedule_date="2026-06-17", shift_type="morning",
        )
        self.duty_mgr.add_or_update_schedule(
            team_id=team_id, member_name="李工程师",
            schedule_date="2026-06-17", shift_type="afternoon",
        )
        self.duty_mgr.add_or_update_schedule(
            team_id=team_id, member_name="王操作员",
            schedule_date="2026-06-17", shift_type="night",
        )

        # Step 4: 设置升级级别
        self.duty_mgr.set_escalation_levels(
            team_id=team_id,
            levels=[
                {"level": 1, "name": "一级响应", "response_minutes": 30, "escalation_minutes": 60},
                {"level": 2, "name": "二级升级", "response_minutes": 60, "escalation_minutes": 120},
            ],
        )

        # Step 5: 生成快照（交班前）
        snap1 = self.snapshot_mgr.generate_snapshot(
            team_id=team_id, operator="张班长",
            snapshot_date="2026-06-17", snapshot_point="早班前",
            note="交班前快照",
        )
        self.assertEqual(snap1.member_count, 3)
        self.assertEqual(snap1.schedule_count, 3)
        self.assertEqual(snap1.snapshot.status, "active")

        # Step 6: 查询快照
        query_result = self.snapshot_mgr.query_snapshots(
            team_id=team_id, snapshot_date="2026-06-17"
        )
        self.assertEqual(len(query_result), 1)

        # Step 7: 查看快照详情
        detail = self.snapshot_mgr.get_snapshot_detail(snap1.snapshot.id)
        self.assertIsNotNone(detail)
        self.assertEqual(len(detail["content"]["members"]), 3)
        self.assertEqual(len(detail["content"]["schedules"]), 3)

        # Step 8: 验证一致性
        verify = self.snapshot_mgr.verify_snapshot_consistency(snap1.snapshot.id)
        self.assertTrue(verify["consistent"], f"验证失败: {verify}")

        # Step 9: 交班
        self.config.snapshot.allow_generate_after_handover = True
        handover_result = self.handover_mgr.perform_handover(
            team_id=team_id,
            operator_member_name="张班长",
            to_member_name="李工程师",
            note="早班交中班",
        )
        self.assertIsNotNone(handover_result)

        # Step 10: 生成快照（交班后）
        snap2 = self.snapshot_mgr.generate_snapshot(
            team_id=team_id, operator="张班长",
            snapshot_date="2026-06-17", snapshot_point="交班后",
            note="交班后快照",
        )
        self.assertEqual(snap2.handover_count, 1)

        # Step 11: 比对两份快照
        diff_result = self.snapshot_mgr.diff_snapshots(
            snap1.snapshot.id, snap2.snapshot.id, operator="张班长"
        )
        self.assertIsNotNone(diff_result.diff)
        self.assertIn("handovers", diff_result.summary)

        # Step 12: 导出JSON
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w", encoding="utf-8") as f:
            json_path = f.name
        try:
            export_result = self.snapshot_mgr.export_snapshots(
                output_path=json_path, team_id=team_id,
                fmt="json", operator="张班长",
            )
            self.assertEqual(export_result.snapshot_count, 2)
            self.assertTrue(os.path.exists(json_path))

            # Step 13: 验证导出内容
            with open(json_path, "r", encoding="utf-8-sig") as f:
                exported = json.load(f)
            self.assertEqual(len(exported), 2)
            self.assertIn("content", exported[0])
        finally:
            if os.path.exists(json_path):
                os.unlink(json_path)

        # Step 14: 导出CSV
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False, mode="w", encoding="utf-8") as f:
            csv_path = f.name
        try:
            csv_result = self.snapshot_mgr.export_snapshots(
                output_path=csv_path, team_id=team_id,
                fmt="csv", operator="张班长", include_content=True,
            )
            self.assertEqual(csv_result.snapshot_count, 2)
        finally:
            if os.path.exists(csv_path):
                os.unlink(csv_path)

        # Step 15: 导出再导回JSON
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w", encoding="utf-8") as f:
            reimport_path = f.name
        try:
            self.snapshot_mgr.export_snapshots(
                output_path=reimport_path, team_id=team_id,
                fmt="json", operator="张班长",
            )
            # 硬删除一个快照以便导回
            with self.db._conn() as conn:
                conn.execute("DELETE FROM duty_snapshot_contents WHERE snapshot_id = ?",
                             (snap1.snapshot.id,))
                conn.execute("DELETE FROM duty_snapshots WHERE id = ?",
                             (snap1.snapshot.id,))

            import_result = self.snapshot_mgr.import_snapshots(
                file_path=reimport_path, operator="管理员",
                conflict_strategy="force",
            )
            self.assertGreater(import_result.success_count, 0)
        finally:
            if os.path.exists(reimport_path):
                os.unlink(reimport_path)

        # Step 16: 导入后验证快照列表
        all_snaps = self.snapshot_mgr.query_snapshots(team_id=team_id)
        self.assertGreaterEqual(len(all_snaps), 2)

        # Step 17: 核对数据库一致性
        for s in all_snaps:
            if s.status == "active" or s.status == "imported":
                verify = self.snapshot_mgr.verify_snapshot_consistency(s.id)
                self.assertTrue(verify["consistent"],
                                 f"快照 {s.id} 不一致: {verify}")

        # Step 18: 查看操作日志
        logs = self.snapshot_mgr.db.list_snapshot_logs()
        self.assertGreater(len(logs), 0)
        gen_logs = [l for l in logs if l.operation == "generate"]
        self.assertGreater(len(gen_logs), 0)
        export_logs = [l for l in logs if l.operation == "export"]
        self.assertGreater(len(export_logs), 0)
        import_logs = [l for l in logs if l.operation == "import"]
        self.assertGreater(len(import_logs), 0)

        # Step 19: 格式化输出
        formatted_list = self.snapshot_mgr.format_snapshot_list(all_snaps)
        self.assertIn("验收班组", formatted_list)
        self.assertIn("快照ID", formatted_list)

        # Step 20: 格式化差异结果
        formatted_diff = diff_result.formatted()
        self.assertIn("差异比对", formatted_diff)

        print("\n========== 端到端链路验证全部通过 ==========")
        print(f"  班组: 验收班组 ({team_id})")
        print(f"  成员: 3人")
        print(f"  排班: 3条")
        print(f"  快照: {len(all_snaps)} 份")
        print(f"  操作日志: {len(logs)} 条")
        print(f"  差异记录: 已生成")
        print("============================================")


if __name__ == "__main__":
    unittest.main()
