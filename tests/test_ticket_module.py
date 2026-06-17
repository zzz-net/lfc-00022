"""工单模块测试

覆盖：
  1. 工单基本操作（创建、列表、详情）
  2. 工单流转（领取、转派、完成、撤回）
  3. 跨重启持久化
  4. 导入导出（CSV/JSON）
  5. 冲突检测（重复开单、已关闭事件再派单、批量关闭冲突）
  6. 撤回链路
  7. 配置约束（优先级、可转派人员）
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest

from inspection_cli.config import AppConfig, ConfigError
from inspection_cli.database import Database, Event
from inspection_cli.ticket import (
    TicketManager, TicketError, TicketConflictError,
    TicketCreateResult, TicketOperationResult,
)
from inspection_cli.ticket_io import TicketIOManager
from inspection_cli.batch import BatchFilter, BatchUpdate, BatchOperationManager


class TestTicketBase(unittest.TestCase):
    """测试基类"""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp_dir, "test_ticket.db")
        self.config = AppConfig(db_path=self.db_path)
        self.db = Database(self.db_path)
        self.ticket_manager = TicketManager(self.db, self.config)
        self.ticket_io_manager = TicketIOManager(self.db, self.config, self.ticket_manager)
        self.batch_manager = BatchOperationManager(self.db, self.config)

    def _insert_event(self, event_id: str, device_id: str = "DEV-A001",
                      status: str = "unconfirmed") -> str:
        event = Event(
            id=event_id,
            device_id=device_id,
            first_seen="2026-06-15 08:30:00",
            last_seen="2026-06-15 09:10:00",
            issue_type="temperature",
            severity="critical",
            status=status,
        )
        self.db.insert_event(event)
        return event_id

    def _reopen_db(self):
        """模拟重启 - 重新创建 Database 和 TicketManager"""
        self.db = Database(self.db_path)
        self.ticket_manager = TicketManager(self.db, self.config)
        self.ticket_io_manager = TicketIOManager(self.db, self.config, self.ticket_manager)
        self.batch_manager = BatchOperationManager(self.db, self.config)


class TestTicketBasicOperations(TestTicketBase):
    """测试工单基本操作"""

    def test_create_ticket_without_events(self):
        """测试创建不关联事件的工单"""
        result = self.ticket_manager.create_ticket(
            title="测试工单",
            creator="admin",
            description="测试描述",
            priority="high",
        )
        self.assertIsInstance(result, TicketCreateResult)
        self.assertEqual(result.ticket.title, "测试工单")
        self.assertEqual(result.ticket.creator, "admin")
        self.assertEqual(result.ticket.priority, "high")
        self.assertEqual(result.ticket.status, "open")
        self.assertEqual(len(result.event_ids), 0)

        logs = self.db.get_ticket_logs(result.ticket.id)
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0].operation, "create")

    def test_create_ticket_with_event(self):
        """测试创建关联事件的工单"""
        self._insert_event("EVT-001")
        result = self.ticket_manager.create_ticket(
            title="处理温度告警",
            creator="admin",
            event_ids=["EVT-001"],
            priority="high",
            assignee="engineer1",
        )
        self.assertEqual(len(result.event_ids), 1)
        self.assertEqual(result.ticket.status, "assigned")
        self.assertEqual(result.ticket.assignee, "engineer1")

        event_ids = self.db.get_ticket_event_ids(result.ticket.id)
        self.assertEqual(event_ids, ["EVT-001"])

    def test_create_ticket_empty_title(self):
        """测试创建工单 - 标题为空"""
        with self.assertRaises(TicketError) as ctx:
            self.ticket_manager.create_ticket(
                title="  ",
                creator="admin",
            )
        self.assertIn("标题不能为空", str(ctx.exception))

    def test_create_ticket_empty_creator(self):
        """测试创建工单 - 创建人为空"""
        with self.assertRaises(TicketError) as ctx:
            self.ticket_manager.create_ticket(
                title="测试",
                creator="",
            )
        self.assertIn("创建人不能为空", str(ctx.exception))

    def test_create_ticket_invalid_priority(self):
        """测试创建工单 - 无效优先级"""
        with self.assertRaises(TicketError) as ctx:
            self.ticket_manager.create_ticket(
                title="测试",
                creator="admin",
                priority="invalid",
            )
        self.assertIn("无效的优先级", str(ctx.exception))

    def test_create_ticket_nonexistent_event(self):
        """测试创建工单 - 事件不存在"""
        with self.assertRaises(TicketError) as ctx:
            self.ticket_manager.create_ticket(
                title="测试",
                creator="admin",
                event_ids=["NONEXISTENT"],
            )
        self.assertIn("事件不存在", str(ctx.exception))

    def test_list_tickets_empty(self):
        """测试空工单列表"""
        result = self.ticket_manager.list_tickets()
        self.assertEqual(len(result.tickets), 0)
        self.assertIn("没有工单", result.formatted())

    def test_list_tickets_multiple(self):
        """测试工单列表"""
        for i in range(3):
            self.ticket_manager.create_ticket(
                title=f"工单-{i}",
                creator="admin",
                priority="medium",
            )
        result = self.ticket_manager.list_tickets()
        self.assertEqual(len(result.tickets), 3)

    def test_list_tickets_filter_by_status(self):
        """测试按状态筛选工单"""
        r1 = self.ticket_manager.create_ticket(title="工单1", creator="admin")
        r2 = self.ticket_manager.create_ticket(title="工单2", creator="admin")
        self.ticket_manager.complete_ticket(r1.ticket.id, operator="admin")

        result = self.ticket_manager.list_tickets(statuses=["open"])
        self.assertEqual(len(result.tickets), 1)
        self.assertEqual(result.tickets[0].id, r2.ticket.id)

        result = self.ticket_manager.list_tickets(statuses=["completed"])
        self.assertEqual(len(result.tickets), 1)
        self.assertEqual(result.tickets[0].id, r1.ticket.id)

    def test_list_tickets_filter_by_priority(self):
        """测试按优先级筛选工单"""
        self.ticket_manager.create_ticket(title="高优先级", creator="admin", priority="high")
        self.ticket_manager.create_ticket(title="低优先级", creator="admin", priority="low")

        result = self.ticket_manager.list_tickets(priorities=["high"])
        self.assertEqual(len(result.tickets), 1)
        self.assertEqual(result.tickets[0].title, "高优先级")

    def test_get_ticket_detail(self):
        """测试获取工单详情"""
        self._insert_event("EVT-001")
        r = self.ticket_manager.create_ticket(
            title="测试工单详情",
            creator="admin",
            event_ids=["EVT-001"],
            description="测试描述",
            priority="critical",
        )
        detail = self.ticket_manager.get_ticket_detail(r.ticket.id)
        self.assertEqual(detail.ticket.title, "测试工单详情")
        self.assertEqual(detail.ticket.description, "测试描述")
        self.assertEqual(len(detail.event_ids), 1)
        self.assertEqual(detail.event_ids[0], "EVT-001")
        self.assertGreaterEqual(len(detail.logs), 1)

    def test_get_ticket_detail_nonexistent(self):
        """测试获取不存在的工单详情"""
        with self.assertRaises(TicketError) as ctx:
            self.ticket_manager.get_ticket_detail("NONEXISTENT")
        self.assertIn("工单不存在", str(ctx.exception))

    def test_list_priorities(self):
        """测试列出优先级"""
        result = self.ticket_manager.list_priorities()
        self.assertIn("可用优先级", result)
        self.assertIn("low", result)
        self.assertIn("medium", result)
        self.assertIn("high", result)
        self.assertIn("critical", result)

    def test_list_assignable_users_unconfigured(self):
        """测试列出可分配人员 - 未配置"""
        result = self.ticket_manager.list_assignable_users()
        self.assertIn("未配置", result)


class TestTicketWorkflow(TestTicketBase):
    """测试工单流转"""

    def test_claim_ticket(self):
        """测试领取工单"""
        r = self.ticket_manager.create_ticket(title="测试领取", creator="admin")
        self.assertEqual(r.ticket.status, "open")

        result = self.ticket_manager.claim_ticket(
            ticket_id=r.ticket.id,
            operator="engineer1",
            note="我来处理",
        )
        self.assertIsInstance(result, TicketOperationResult)
        self.assertEqual(result.operation, "claim")
        self.assertEqual(result.new_status, "in_progress")
        self.assertEqual(result.new_assignee, "engineer1")

        ticket = self.db.get_ticket(r.ticket.id)
        self.assertEqual(ticket.status, "in_progress")
        self.assertEqual(ticket.assignee, "engineer1")

    def test_claim_ticket_already_assigned(self):
        """测试领取已分配的工单"""
        r = self.ticket_manager.create_ticket(
            title="测试领取", creator="admin", assignee="engineer1"
        )
        self.assertEqual(r.ticket.status, "assigned")

        result = self.ticket_manager.claim_ticket(
            ticket_id=r.ticket.id,
            operator="engineer2",
        )
        self.assertEqual(result.new_status, "in_progress")
        self.assertEqual(result.new_assignee, "engineer2")

    def test_claim_ticket_completed(self):
        """测试领取已完成的工单 - 应失败"""
        r = self.ticket_manager.create_ticket(title="测试", creator="admin")
        self.ticket_manager.complete_ticket(r.ticket.id, operator="admin")

        with self.assertRaises(TicketError) as ctx:
            self.ticket_manager.claim_ticket(r.ticket.id, operator="engineer1")
        self.assertIn("无法领取", str(ctx.exception))

    def test_claim_ticket_revoked(self):
        """测试领取已撤回的工单 - 应失败"""
        r = self.ticket_manager.create_ticket(title="测试", creator="admin")
        self.ticket_manager.revoke_ticket(r.ticket.id, operator="admin")

        with self.assertRaises(TicketError) as ctx:
            self.ticket_manager.claim_ticket(r.ticket.id, operator="engineer1")
        self.assertIn("无法领取", str(ctx.exception))

    def test_claim_ticket_empty_operator(self):
        """测试领取工单 - 操作人为空"""
        r = self.ticket_manager.create_ticket(title="测试", creator="admin")
        with self.assertRaises(TicketError) as ctx:
            self.ticket_manager.claim_ticket(r.ticket.id, operator="")
        self.assertIn("操作人不能为空", str(ctx.exception))

    def test_assign_ticket(self):
        """测试转派工单"""
        r = self.ticket_manager.create_ticket(title="测试转派", creator="admin")

        result = self.ticket_manager.assign_ticket(
            ticket_id=r.ticket.id,
            new_assignee="engineer1",
            operator="admin",
            note="转派给你",
        )
        self.assertEqual(result.operation, "assign")
        self.assertEqual(result.new_assignee, "engineer1")
        self.assertEqual(result.new_status, "assigned")

    def test_assign_ticket_in_progress(self):
        """测试转派处理中的工单"""
        r = self.ticket_manager.create_ticket(title="测试", creator="admin")
        self.ticket_manager.claim_ticket(r.ticket.id, operator="engineer1")

        result = self.ticket_manager.assign_ticket(
            ticket_id=r.ticket.id,
            new_assignee="engineer2",
            operator="admin",
        )
        self.assertEqual(result.new_assignee, "engineer2")
        ticket = self.db.get_ticket(r.ticket.id)
        self.assertEqual(ticket.status, "in_progress")

    def test_assign_ticket_completed(self):
        """测试转派已完成的工单 - 应失败"""
        r = self.ticket_manager.create_ticket(title="测试", creator="admin")
        self.ticket_manager.complete_ticket(r.ticket.id, operator="admin")

        with self.assertRaises(TicketError) as ctx:
            self.ticket_manager.assign_ticket(
                r.ticket.id, new_assignee="engineer1", operator="admin"
            )
        self.assertIn("无法转派", str(ctx.exception))

    def test_assign_ticket_same_assignee(self):
        """测试转派给同一人 - 应失败"""
        r = self.ticket_manager.create_ticket(
            title="测试", creator="admin", assignee="engineer1"
        )
        with self.assertRaises(TicketError) as ctx:
            self.ticket_manager.assign_ticket(
                r.ticket.id, new_assignee="engineer1", operator="admin"
            )
        self.assertIn("已经是", str(ctx.exception))

    def test_complete_ticket(self):
        """测试完成工单"""
        r = self.ticket_manager.create_ticket(title="测试完成", creator="admin")
        self.ticket_manager.claim_ticket(r.ticket.id, operator="engineer1")

        result = self.ticket_manager.complete_ticket(
            ticket_id=r.ticket.id,
            operator="engineer1",
            note="已修复",
        )
        self.assertEqual(result.operation, "complete")
        self.assertEqual(result.new_status, "completed")

        ticket = self.db.get_ticket(r.ticket.id)
        self.assertEqual(ticket.status, "completed")
        self.assertNotEqual(ticket.completed_at, "")

    def test_complete_ticket_already_completed(self):
        """测试重复完成工单 - 应失败"""
        r = self.ticket_manager.create_ticket(title="测试", creator="admin")
        self.ticket_manager.complete_ticket(r.ticket.id, operator="admin")

        with self.assertRaises(TicketError) as ctx:
            self.ticket_manager.complete_ticket(r.ticket.id, operator="admin")
        self.assertIn("已经完成", str(ctx.exception))

    def test_complete_ticket_revoked(self):
        """测试完成已撤回的工单 - 应失败"""
        r = self.ticket_manager.create_ticket(title="测试", creator="admin")
        self.ticket_manager.revoke_ticket(r.ticket.id, operator="admin")

        with self.assertRaises(TicketError) as ctx:
            self.ticket_manager.complete_ticket(r.ticket.id, operator="admin")
        self.assertIn("已撤回", str(ctx.exception))

    def test_revoke_ticket(self):
        """测试撤回工单"""
        r = self.ticket_manager.create_ticket(title="测试撤回", creator="admin")
        self.ticket_manager.claim_ticket(r.ticket.id, operator="engineer1")

        result = self.ticket_manager.revoke_ticket(
            ticket_id=r.ticket.id,
            operator="admin",
            note="需求变更",
        )
        self.assertEqual(result.operation, "revoke")
        self.assertEqual(result.new_status, "revoked")

        ticket = self.db.get_ticket(r.ticket.id)
        self.assertEqual(ticket.status, "revoked")

    def test_revoke_ticket_already_revoked(self):
        """测试重复撤回工单 - 应失败"""
        r = self.ticket_manager.create_ticket(title="测试", creator="admin")
        self.ticket_manager.revoke_ticket(r.ticket.id, operator="admin", note="测试撤回")

        with self.assertRaises(TicketError) as ctx:
            self.ticket_manager.revoke_ticket(r.ticket.id, operator="admin")
        self.assertIn("已经撤回", str(ctx.exception))

    def test_revoke_ticket_completed(self):
        """测试撤回已完成的工单 - 应失败"""
        r = self.ticket_manager.create_ticket(title="测试", creator="admin")
        self.ticket_manager.complete_ticket(r.ticket.id, operator="admin")

        with self.assertRaises(TicketError) as ctx:
            self.ticket_manager.revoke_ticket(r.ticket.id, operator="admin")
        self.assertIn("已完成", str(ctx.exception))

    def test_workflow_full_chain(self):
        """测试完整工作流: 创建 -> 领取 -> 转派 -> 完成"""
        r = self.ticket_manager.create_ticket(
            title="完整流程测试", creator="admin", priority="high"
        )
        self.assertEqual(r.ticket.status, "open")

        r_claim = self.ticket_manager.claim_ticket(r.ticket.id, "engineer1")
        self.assertEqual(r_claim.new_status, "in_progress")

        r_assign = self.ticket_manager.assign_ticket(
            r.ticket.id, "engineer2", "engineer1"
        )
        self.assertEqual(r_assign.new_assignee, "engineer2")

        r_complete = self.ticket_manager.complete_ticket(r.ticket.id, "engineer2")
        self.assertEqual(r_complete.new_status, "completed")

        logs = self.db.get_ticket_logs(r.ticket.id)
        self.assertEqual(len(logs), 4)
        operations = [log.operation for log in logs]
        self.assertEqual(operations, ["create", "claim", "assign", "complete"])


class TestTicketPersistence(TestTicketBase):
    """测试工单跨重启持久化"""

    def test_ticket_persists_across_restart(self):
        """测试工单数据跨重启持久化"""
        r = self.ticket_manager.create_ticket(
            title="持久化测试",
            creator="admin",
            description="测试数据是否持久化",
            priority="high",
        )
        ticket_id = r.ticket.id

        self._reopen_db()

        ticket = self.db.get_ticket(ticket_id)
        self.assertIsNotNone(ticket)
        self.assertEqual(ticket.title, "持久化测试")
        self.assertEqual(ticket.priority, "high")
        self.assertEqual(ticket.status, "open")

    def test_ticket_logs_persist_across_restart(self):
        """测试工单日志跨重启持久化"""
        r = self.ticket_manager.create_ticket(title="测试", creator="admin")
        self.ticket_manager.claim_ticket(r.ticket.id, "engineer1")
        self.ticket_manager.complete_ticket(r.ticket.id, "engineer1")

        self._reopen_db()

        logs = self.db.get_ticket_logs(r.ticket.id)
        self.assertEqual(len(logs), 3)
        self.assertEqual(logs[0].operation, "create")
        self.assertEqual(logs[1].operation, "claim")
        self.assertEqual(logs[2].operation, "complete")

    def test_ticket_events_persist_across_restart(self):
        """测试工单-事件关联跨重启持久化"""
        self._insert_event("EVT-001")
        self._insert_event("EVT-002")
        r = self.ticket_manager.create_ticket(
            title="关联测试", creator="admin", event_ids=["EVT-001", "EVT-002"]
        )

        self._reopen_db()

        event_ids = self.db.get_ticket_event_ids(r.ticket.id)
        self.assertEqual(len(event_ids), 2)
        self.assertIn("EVT-001", event_ids)
        self.assertIn("EVT-002", event_ids)

    def test_workflow_persistence_full_chain(self):
        """测试完整工作流的持久化"""
        self._insert_event("EVT-001")
        r = self.ticket_manager.create_ticket(
            title="完整持久化测试", creator="admin", event_ids=["EVT-001"]
        )
        self.ticket_manager.claim_ticket(r.ticket.id, "engineer1")
        self.ticket_manager.assign_ticket(r.ticket.id, "engineer2", "admin")
        self.ticket_manager.complete_ticket(r.ticket.id, "engineer2", note="已修复")

        ticket_id = r.ticket.id
        self._reopen_db()

        detail = self.ticket_manager.get_ticket_detail(ticket_id)
        self.assertEqual(detail.ticket.status, "completed")
        self.assertEqual(detail.ticket.assignee, "engineer2")
        self.assertEqual(len(detail.event_ids), 1)
        self.assertEqual(len(detail.logs), 4)


class TestTicketConflicts(TestTicketBase):
    """测试工单冲突检测"""

    def test_duplicate_open_ticket_conflict(self):
        """测试同一事件重复开单 - 默认禁止"""
        self._insert_event("EVT-001")
        self.ticket_manager.create_ticket(
            title="第一个工单", creator="admin", event_ids=["EVT-001"]
        )

        with self.assertRaises(TicketConflictError) as ctx:
            self.ticket_manager.create_ticket(
                title="第二个工单", creator="admin", event_ids=["EVT-001"]
            )
        self.assertIn("未完成工单", str(ctx.exception))

    def test_duplicate_ticket_allowed_with_config(self):
        """测试配置允许重复开单时可以创建"""
        config = AppConfig(db_path=self.db_path)
        config.ticket.allow_duplicate_open_ticket = True
        ticket_manager = TicketManager(self.db, config)

        self._insert_event("EVT-001")
        r1 = ticket_manager.create_ticket(
            title="第一个工单", creator="admin", event_ids=["EVT-001"]
        )
        r2 = ticket_manager.create_ticket(
            title="第二个工单", creator="admin", event_ids=["EVT-001"]
        )

        self.assertNotEqual(r1.ticket.id, r2.ticket.id)
        self.assertEqual(r2.event_ids, ["EVT-001"])

    def test_closed_event_ticket_conflict(self):
        """测试已关闭事件创建工单 - 默认禁止"""
        self._insert_event("EVT-001", status="closed")

        with self.assertRaises(TicketConflictError) as ctx:
            self.ticket_manager.create_ticket(
                title="测试关闭事件", creator="admin", event_ids=["EVT-001"]
            )
        self.assertIn("已关闭", str(ctx.exception))

    def test_closed_event_ticket_allowed_with_config(self):
        """测试配置允许时可以为已关闭事件创建工单"""
        config = AppConfig(db_path=self.db_path)
        config.ticket.allow_closed_event_ticket = True
        ticket_manager = TicketManager(self.db, config)

        self._insert_event("EVT-001", status="closed")
        r = ticket_manager.create_ticket(
            title="测试", creator="admin", event_ids=["EVT-001"]
        )
        self.assertEqual(len(r.event_ids), 1)

    def test_batch_close_with_open_ticket_conflict_skip(self):
        """测试批量关闭事件时遇到未完成工单 - skip策略"""
        self._insert_event("EVT-001")
        self._insert_event("EVT-002")
        self.ticket_manager.create_ticket(
            title="关联工单", creator="admin", event_ids=["EVT-001"]
        )

        result = self.batch_manager.execute(
            BatchFilter(event_ids=["EVT-001", "EVT-002"]),
            BatchUpdate(status="closed"),
            operator="admin",
            conflict_strategy="skip",
        )
        self.assertEqual(result.total_count, 2)
        self.assertEqual(result.success_count, 1)
        self.assertEqual(result.conflict_count, 1)

        conflict_items = [item for item in result.items if item.status == "conflict"]
        self.assertEqual(len(conflict_items), 1)
        self.assertEqual(conflict_items[0].event_id, "EVT-001")
        self.assertIn("未完成工单", conflict_items[0].reason)

    def test_batch_close_with_open_ticket_conflict_abort(self):
        """测试批量关闭事件时遇到未完成工单 - abort策略"""
        self._insert_event("EVT-001")
        self._insert_event("EVT-002")
        self.ticket_manager.create_ticket(
            title="关联工单", creator="admin", event_ids=["EVT-001"]
        )

        from inspection_cli.batch import BatchOperationError
        with self.assertRaises(BatchOperationError):
            self.batch_manager.execute(
                BatchFilter(event_ids=["EVT-001", "EVT-002"]),
                BatchUpdate(status="closed"),
                operator="admin",
                conflict_strategy="abort",
            )

    def test_batch_close_without_tickets_succeeds(self):
        """测试批量关闭无关联工单的事件 - 全部成功"""
        self._insert_event("EVT-001")
        self._insert_event("EVT-002")

        result = self.batch_manager.execute(
            BatchFilter(event_ids=["EVT-001", "EVT-002"]),
            BatchUpdate(status="closed"),
            operator="admin",
            conflict_strategy="skip",
        )
        self.assertEqual(result.total_count, 2)
        self.assertEqual(result.success_count, 2)
        self.assertEqual(result.conflict_count, 0)

    def test_completed_ticket_allows_new_ticket(self):
        """测试已完成的工单不影响创建新工单"""
        self._insert_event("EVT-001")
        r = self.ticket_manager.create_ticket(
            title="第一个工单", creator="admin", event_ids=["EVT-001"]
        )
        self.ticket_manager.complete_ticket(r.ticket.id, "admin")

        r2 = self.ticket_manager.create_ticket(
            title="第二个工单", creator="admin", event_ids=["EVT-001"]
        )
        self.assertEqual(len(r2.event_ids), 1)

    def test_revoked_ticket_allows_new_ticket(self):
        """测试已撤回的工单不影响创建新工单"""
        self._insert_event("EVT-001")
        r = self.ticket_manager.create_ticket(
            title="第一个工单", creator="admin", event_ids=["EVT-001"]
        )
        self.ticket_manager.revoke_ticket(r.ticket.id, "admin")

        r2 = self.ticket_manager.create_ticket(
            title="第二个工单", creator="admin", event_ids=["EVT-001"]
        )
        self.assertEqual(len(r2.event_ids), 1)

    def test_check_events_for_close(self):
        """测试检查事件关闭冲突"""
        self.config.ticket.allow_duplicate_open_ticket = True
        self._insert_event("EVT-001")
        self._insert_event("EVT-002")
        self._insert_event("EVT-003")
        self.ticket_manager.create_ticket(
            title="工单1", creator="admin", event_ids=["EVT-001"]
        )
        self.ticket_manager.create_ticket(
            title="工单2", creator="admin", event_ids=["EVT-001"]
        )

        result = self.ticket_manager.check_events_for_close(
            ["EVT-001", "EVT-002", "EVT-003"]
        )
        self.assertEqual(result["total_events"], 3)
        self.assertEqual(result["conflict_event_count"], 1)
        self.assertIn("EVT-001", result["conflict_events"])
        self.assertEqual(len(result["conflict_events"]["EVT-001"]), 2)


class TestTicketImportExport(TestTicketBase):
    """测试工单导入导出"""

    def test_export_json(self):
        """测试导出 JSON"""
        self._insert_event("EVT-001")
        self.ticket_manager.create_ticket(
            title="导出测试", creator="admin", event_ids=["EVT-001"], priority="high"
        )

        out_path = os.path.join(self.tmp_dir, "tickets.json")
        result = self.ticket_io_manager.export_tickets(out_path, fmt="json",
                                                        include_events=True)
        self.assertEqual(result.ticket_count, 1)
        self.assertTrue(os.path.exists(out_path))

        with open(out_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["ticket_count"], 1)
        self.assertEqual(len(data["tickets"]), 1)
        self.assertEqual(data["tickets"][0]["title"], "导出测试")
        self.assertEqual(data["tickets"][0]["priority"], "high")

    def test_export_csv(self):
        """测试导出 CSV"""
        self.ticket_manager.create_ticket(title="CSV测试", creator="admin")

        out_path = os.path.join(self.tmp_dir, "tickets.csv")
        result = self.ticket_io_manager.export_tickets(out_path, fmt="csv")
        self.assertEqual(result.ticket_count, 1)
        self.assertTrue(os.path.exists(out_path))

        import csv
        with open(out_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "CSV测试")

    def test_export_with_logs(self):
        """测试导出包含日志"""
        r = self.ticket_manager.create_ticket(title="日志测试", creator="admin")
        self.ticket_manager.claim_ticket(r.ticket.id, "engineer1")

        out_path = os.path.join(self.tmp_dir, "tickets.json")
        self.ticket_io_manager.export_tickets(out_path, fmt="json", include_logs=True)

        with open(out_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertIn("logs", data["tickets"][0])
        self.assertEqual(len(data["tickets"][0]["logs"]), 2)

    def test_import_json_new(self):
        """测试导入 JSON - 新工单"""
        ticket_data = {
            "tickets": [
                {
                    "ticket_id": "TKT-IMPORT-001",
                    "title": "导入测试",
                    "description": "测试导入",
                    "priority": "high",
                    "status": "open",
                    "assignee": "",
                    "creator": "import",
                }
            ]
        }
        in_path = os.path.join(self.tmp_dir, "import.json")
        with open(in_path, "w", encoding="utf-8") as f:
            json.dump(ticket_data, f, ensure_ascii=False)

        result = self.ticket_io_manager.import_tickets(in_path, operator="test-user")
        self.assertEqual(result.total_count, 1)
        self.assertEqual(result.success_count, 1)

        ticket = self.db.get_ticket("TKT-IMPORT-001")
        self.assertIsNotNone(ticket)
        self.assertEqual(ticket.title, "导入测试")

    def test_import_conflict_skip(self):
        """测试导入冲突 - skip策略"""
        r = self.ticket_manager.create_ticket(title="原工单", creator="admin")

        ticket_data = {
            "tickets": [
                {
                    "ticket_id": r.ticket.id,
                    "title": "更新后的工单",
                    "priority": "high",
                    "status": "open",
                    "creator": "import",
                }
            ]
        }
        in_path = os.path.join(self.tmp_dir, "import.json")
        with open(in_path, "w", encoding="utf-8") as f:
            json.dump(ticket_data, f, ensure_ascii=False)

        result = self.ticket_io_manager.import_tickets(in_path, conflict_strategy="skip")
        self.assertEqual(result.conflict_count, 1)

        ticket = self.db.get_ticket(r.ticket.id)
        self.assertEqual(ticket.title, "原工单")

    def test_import_conflict_force(self):
        """测试导入冲突 - force策略"""
        r = self.ticket_manager.create_ticket(title="原工单", creator="admin")

        ticket_data = {
            "tickets": [
                {
                    "ticket_id": r.ticket.id,
                    "title": "更新后的工单",
                    "priority": "high",
                    "status": "open",
                    "creator": "import",
                }
            ]
        }
        in_path = os.path.join(self.tmp_dir, "import.json")
        with open(in_path, "w", encoding="utf-8") as f:
            json.dump(ticket_data, f, ensure_ascii=False)

        result = self.ticket_io_manager.import_tickets(in_path, conflict_strategy="force")
        self.assertEqual(result.success_count, 1)

        ticket = self.db.get_ticket(r.ticket.id)
        self.assertEqual(ticket.title, "更新后的工单")

    def test_export_then_import_roundtrip(self):
        """测试导出再导入的往返一致性"""
        self._insert_event("EVT-001")
        self._insert_event("EVT-002")
        r = self.ticket_manager.create_ticket(
            title="往返测试", creator="admin", priority="high",
            event_ids=["EVT-001", "EVT-002"], description="测试描述"
        )
        self.ticket_manager.claim_ticket(r.ticket.id, "engineer1")
        original_id = r.ticket.id

        out_path = os.path.join(self.tmp_dir, "tickets_export.json")
        self.ticket_io_manager.export_tickets(
            out_path, fmt="json", include_logs=True, include_events=True
        )

        new_db_path = os.path.join(self.tmp_dir, "new_test.db")
        new_config = AppConfig(db_path=new_db_path)
        new_db = Database(new_db_path)
        new_db.insert_event(Event(
            id="EVT-001", device_id="DEV-A001",
            first_seen="2026-06-15 08:30:00", last_seen="2026-06-15 09:10:00",
            issue_type="temperature", severity="critical",
        ))
        new_db.insert_event(Event(
            id="EVT-002", device_id="DEV-A001",
            first_seen="2026-06-15 09:00:00", last_seen="2026-06-15 09:30:00",
            issue_type="pressure", severity="warning",
        ))
        new_ticket_manager = TicketManager(new_db, new_config)
        new_ticket_io = TicketIOManager(new_db, new_config, new_ticket_manager)

        result = new_ticket_io.import_tickets(out_path, operator="import")
        self.assertEqual(result.success_count, 1)

        imported_ticket = new_db.get_ticket(original_id)
        self.assertIsNotNone(imported_ticket)
        self.assertEqual(imported_ticket.title, "往返测试")
        self.assertEqual(imported_ticket.priority, "high")
        self.assertEqual(imported_ticket.description, "测试描述")

        event_ids = new_db.get_ticket_event_ids(original_id)
        self.assertEqual(len(event_ids), 2)

    def test_import_invalid_priority_fallback(self):
        """测试导入无效优先级时回退到默认"""
        ticket_data = {
            "tickets": [
                {
                    "ticket_id": "TKT-BAD-PRIO",
                    "title": "无效优先级",
                    "priority": "invalid_priority",
                    "status": "open",
                    "creator": "import",
                }
            ]
        }
        in_path = os.path.join(self.tmp_dir, "bad_prio.json")
        with open(in_path, "w", encoding="utf-8") as f:
            json.dump(ticket_data, f)

        result = self.ticket_io_manager.import_tickets(in_path)
        self.assertEqual(result.success_count, 1)

        ticket = self.db.get_ticket("TKT-BAD-PRIO")
        self.assertEqual(ticket.priority, "medium")


class TestTicketConfigConstraints(TestTicketBase):
    """测试工单配置约束"""

    def test_assignable_users_restriction(self):
        """测试可分配人员约束"""
        config = AppConfig(db_path=self.db_path)
        config.ticket.assignable_users = ["alice", "bob", "charlie"]
        ticket_manager = TicketManager(self.db, config)

        r = ticket_manager.create_ticket(title="测试", creator="admin")

        with self.assertRaises(TicketError) as ctx:
            ticket_manager.assign_ticket(r.ticket.id, "dave", "admin")
        self.assertIn("不在可分配人员列表中", str(ctx.exception))

        result = ticket_manager.assign_ticket(r.ticket.id, "alice", "admin")
        self.assertEqual(result.new_assignee, "alice")

    def test_claim_with_assignable_users(self):
        """测试领取时也受可分配人员约束"""
        config = AppConfig(db_path=self.db_path)
        config.ticket.assignable_users = ["alice", "bob"]
        ticket_manager = TicketManager(self.db, config)

        r = ticket_manager.create_ticket(title="测试", creator="admin")

        with self.assertRaises(TicketError) as ctx:
            ticket_manager.claim_ticket(r.ticket.id, "charlie")
        self.assertIn("不在可分配人员列表中", str(ctx.exception))

    def test_create_with_assignee_validation(self):
        """测试创建工单时验证负责人"""
        config = AppConfig(db_path=self.db_path)
        config.ticket.assignable_users = ["alice"]
        ticket_manager = TicketManager(self.db, config)

        with self.assertRaises(TicketError) as ctx:
            ticket_manager.create_ticket(
                title="测试", creator="admin", assignee="bob"
            )
        self.assertIn("不在可分配人员列表中", str(ctx.exception))

    def test_config_invalid_default_priority(self):
        """测试配置中默认优先级不在有效列表中"""
        from io import StringIO
        import yaml

        bad_config = {
            "ticket": {
                "valid_priorities": ["low", "high"],
                "default_priority": "medium",
            }
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(bad_config, f)
            bad_config_path = f.name

        try:
            with self.assertRaises(ConfigError) as ctx:
                AppConfig.load(bad_config_path)
            self.assertIn("不在 valid_priorities 列表中", str(ctx.exception))
        finally:
            os.unlink(bad_config_path)

    def test_config_empty_valid_priorities(self):
        """测试配置中空的有效优先级列表"""
        import yaml

        bad_config = {
            "ticket": {
                "valid_priorities": [],
            }
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(bad_config, f)
            bad_config_path = f.name

        try:
            with self.assertRaises(ConfigError) as ctx:
                AppConfig.load(bad_config_path)
            self.assertIn("不能为空列表", str(ctx.exception))
        finally:
            os.unlink(bad_config_path)

    def test_bad_config_does_not_delete_existing_tickets(self):
        """测试配置错误不影响已有的工单数据"""
        r = self.ticket_manager.create_ticket(title="重要工单", creator="admin")
        ticket_id = r.ticket.id

        import yaml
        bad_config = {
            "ticket": {
                "valid_priorities": "not_a_list",
            }
        }
        bad_config_path = os.path.join(self.tmp_dir, "bad_config.yaml")
        with open(bad_config_path, "w", encoding="utf-8") as f:
            yaml.dump(bad_config, f)

        try:
            AppConfig.load(bad_config_path)
        except ConfigError:
            pass

        ticket = self.db.get_ticket(ticket_id)
        self.assertIsNotNone(ticket)
        self.assertEqual(ticket.title, "重要工单")


class TestTicketRevokeChain(TestTicketBase):
    """测试工单撤回链路"""

    def test_revoke_cannot_be_completed(self):
        """测试已撤回工单不能完成"""
        r = self.ticket_manager.create_ticket(title="测试", creator="admin")
        self.ticket_manager.revoke_ticket(r.ticket.id, "admin", note="撤回测试")

        with self.assertRaises(TicketError) as ctx:
            self.ticket_manager.complete_ticket(r.ticket.id, "admin")
        self.assertIn("已撤回", str(ctx.exception))

    def test_revoke_cannot_be_claimed(self):
        """测试已撤回工单不能领取"""
        r = self.ticket_manager.create_ticket(title="测试", creator="admin")
        self.ticket_manager.revoke_ticket(r.ticket.id, "admin")

        with self.assertRaises(TicketError) as ctx:
            self.ticket_manager.claim_ticket(r.ticket.id, "engineer1")
        self.assertIn("无法领取", str(ctx.exception))

    def test_revoke_cannot_be_assigned(self):
        """测试已撤回工单不能转派"""
        r = self.ticket_manager.create_ticket(title="测试", creator="admin")
        self.ticket_manager.revoke_ticket(r.ticket.id, "admin")

        with self.assertRaises(TicketError) as ctx:
            self.ticket_manager.assign_ticket(r.ticket.id, "engineer1", "admin")
        self.assertIn("无法转派", str(ctx.exception))

    def test_revoke_log_recorded(self):
        """测试撤回操作被记录到日志"""
        r = self.ticket_manager.create_ticket(title="测试", creator="admin")
        self.ticket_manager.revoke_ticket(r.ticket.id, "admin", note="需求变更")

        logs = self.db.get_ticket_logs(r.ticket.id)
        revoke_log = logs[-1]
        self.assertEqual(revoke_log.operation, "revoke")
        self.assertEqual(revoke_log.operator, "admin")
        self.assertEqual(revoke_log.note, "需求变更")
        self.assertEqual(revoke_log.new_status, "revoked")

    def test_claim_then_revoke(self):
        """测试领取后再撤回的完整链路"""
        r = self.ticket_manager.create_ticket(title="测试链路", creator="admin")
        self.ticket_manager.claim_ticket(r.ticket.id, "engineer1", note="我来处理")
        self.ticket_manager.assign_ticket(r.ticket.id, "engineer2", "engineer1", note="转派")
        self.ticket_manager.revoke_ticket(r.ticket.id, "admin", note="项目取消")

        logs = self.db.get_ticket_logs(r.ticket.id)
        self.assertEqual(len(logs), 4)

        statuses = [log.new_status for log in logs]
        self.assertEqual(statuses, ["open", "in_progress", "in_progress", "revoked"])

        operations = [log.operation for log in logs]
        self.assertEqual(operations, ["create", "claim", "assign", "revoke"])

    def test_revoked_shows_in_list(self):
        """测试已撤回工单在列表中可见"""
        r = self.ticket_manager.create_ticket(title="已撤回工单", creator="admin")
        self.ticket_manager.revoke_ticket(r.ticket.id, "admin")

        all_tickets = self.ticket_manager.list_tickets()
        self.assertEqual(len(all_tickets.tickets), 1)

        revoked = self.ticket_manager.list_tickets(statuses=["revoked"])
        self.assertEqual(len(revoked.tickets), 1)

        open_tickets = self.ticket_manager.list_tickets(statuses=["open"])
        self.assertEqual(len(open_tickets.tickets), 0)


if __name__ == "__main__":
    unittest.main()
