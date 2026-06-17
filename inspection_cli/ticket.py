"""工单管理：创建、领取、转派、完成、撤回、冲突检测"""
from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from .config import AppConfig, TicketConfig
from .database import (
    Database, Event, Ticket, TicketLog,
    TICKET_STATUS_OPEN, TICKET_STATUS_ASSIGNED,
    TICKET_STATUS_IN_PROGRESS, TICKET_STATUS_COMPLETED,
    TICKET_STATUS_REVOKED, VALID_TICKET_STATUSES,
    TICKET_LOG_OP_CREATE, TICKET_LOG_OP_ASSIGN,
    TICKET_LOG_OP_CLAIM, TICKET_LOG_OP_COMPLETE,
    TICKET_LOG_OP_REVOKE, TICKET_LOG_OP_UPDATE,
    TICKET_STATUS_LABELS, TICKET_PRIORITY_LABELS,
    DEFAULT_TICKET_PRIORITIES,
)


class TicketError(Exception):
    """工单操作错误"""
    pass


class TicketConflictError(TicketError):
    """工单冲突错误"""
    pass


@dataclass
class TicketCreateResult:
    """工单创建结果"""
    ticket: Ticket
    event_ids: list[str]
    conflict_events: list[str] = field(default_factory=list)
    closed_events: list[str] = field(default_factory=list)

    def formatted(self) -> str:
        lines = [
            f"工单创建成功: {self.ticket.id}",
            f"  标题: {self.ticket.title}",
            f"  优先级: {TICKET_PRIORITY_LABELS.get(self.ticket.priority, self.ticket.priority)}",
            f"  状态: {TICKET_STATUS_LABELS.get(self.ticket.status, self.ticket.status)}",
            f"  负责人: {self.ticket.assignee or '(未分配)'}",
            f"  创建人: {self.ticket.creator}",
            f"  关联事件数: {len(self.event_ids)}",
        ]
        if self.ticket.due_time:
            lines.append(f"  截止时间: {self.ticket.due_time}")
        if self.conflict_events:
            lines.append(f"  冲突事件(已存在未完成工单): {len(self.conflict_events)} 个")
        if self.closed_events:
            lines.append(f"  已关闭事件(跳过): {len(self.closed_events)} 个")
        return "\n".join(lines)


@dataclass
class TicketOperationResult:
    """工单操作结果"""
    ticket_id: str
    operation: str
    old_status: str
    new_status: str
    old_assignee: str
    new_assignee: str
    operator: str
    note: str = ""

    def formatted(self) -> str:
        op_labels = {
            TICKET_LOG_OP_CREATE: "创建",
            TICKET_LOG_OP_ASSIGN: "转派",
            TICKET_LOG_OP_CLAIM: "领取",
            TICKET_LOG_OP_COMPLETE: "完成",
            TICKET_LOG_OP_REVOKE: "撤回",
            TICKET_LOG_OP_UPDATE: "更新",
        }
        lines = [
            f"工单 {self.ticket_id} {op_labels.get(self.operation, self.operation)}成功",
            f"  状态: {TICKET_STATUS_LABELS.get(self.old_status, self.old_status)} "
            f"→ {TICKET_STATUS_LABELS.get(self.new_status, self.new_status)}",
        ]
        if self.old_assignee != self.new_assignee:
            lines.append(f"  负责人: {self.old_assignee or '(未分配)'} → {self.new_assignee or '(未分配)'}")
        lines.append(f"  操作人: {self.operator}")
        if self.note:
            lines.append(f"  备注: {self.note}")
        return "\n".join(lines)


@dataclass
class TicketListResult:
    """工单列表结果"""
    tickets: list[Ticket]
    filters: dict[str, Any] = field(default_factory=dict)

    def formatted(self) -> str:
        if not self.tickets:
            return "没有工单。"

        lines = [f"共 {len(self.tickets)} 个工单:"]
        lines.append("")

        header = (
            f"{'工单ID':<20} {'状态':<8} {'优先级':<6} {'标题':<24} "
            f"{'负责人':<12} {'创建人':<12} {'创建时间':<20}"
        )
        lines.append(header)
        lines.append("-" * len(header))

        for t in self.tickets:
            title = t.title[:22] + ".." if len(t.title) > 24 else t.title
            lines.append(
                f"{t.id:<20} "
                f"{TICKET_STATUS_LABELS.get(t.status, t.status):<8} "
                f"{TICKET_PRIORITY_LABELS.get(t.priority, t.priority):<6} "
                f"{title:<24} "
                f"{(t.assignee or '-'):<12} "
                f"{t.creator:<12} "
                f"{t.created_at:<20}"
            )
        return "\n".join(lines)


@dataclass
class TicketDetailResult:
    """工单详情结果"""
    ticket: Ticket
    event_ids: list[str]
    logs: list[TicketLog]

    def formatted(self) -> str:
        t = self.ticket
        lines = [
            f"===== 工单详情 =====",
            f"工单ID: {t.id}",
            f"标题: {t.title}",
            f"描述: {t.description or '(无)'}",
            f"状态: {TICKET_STATUS_LABELS.get(t.status, t.status)}",
            f"优先级: {TICKET_PRIORITY_LABELS.get(t.priority, t.priority)}",
            f"负责人: {t.assignee or '(未分配)'}",
            f"创建人: {t.creator}",
            f"创建时间: {t.created_at}",
            f"更新时间: {t.updated_at}",
        ]
        if t.due_time:
            lines.append(f"截止时间: {t.due_time}")
        if t.completed_at:
            lines.append(f"完成时间: {t.completed_at}")
        if t.steps:
            lines.append(f"处理步骤: {t.steps}")
        if t.note:
            lines.append(f"备注: {t.note}")
        lines.append(f"版本: {t.version}")

        lines.append("")
        lines.append(f"关联事件 ({len(self.event_ids)} 个):")
        if self.event_ids:
            for eid in self.event_ids[:10]:
                lines.append(f"  - {eid}")
            if len(self.event_ids) > 10:
                lines.append(f"  ... 还有 {len(self.event_ids) - 10} 个")
        else:
            lines.append("  (无关联事件)")

        lines.append("")
        lines.append(f"流转日志 ({len(self.logs)} 条):")
        if self.logs:
            for log in self.logs:
                op_labels = {
                    TICKET_LOG_OP_CREATE: "创建",
                    TICKET_LOG_OP_ASSIGN: "转派",
                    TICKET_LOG_OP_CLAIM: "领取",
                    TICKET_LOG_OP_COMPLETE: "完成",
                    TICKET_LOG_OP_REVOKE: "撤回",
                    TICKET_LOG_OP_UPDATE: "更新",
                }
                lines.append(
                    f"  [{log.operated_at}] {op_labels.get(log.operation, log.operation)} "
                    f"by {log.operator}"
                    f" | {TICKET_STATUS_LABELS.get(log.old_status, log.old_status)}"
                    f" → {TICKET_STATUS_LABELS.get(log.new_status, log.new_status)}"
                )
                if log.note:
                    lines.append(f"    备注: {log.note}")
        else:
            lines.append("  (无日志)")

        return "\n".join(lines)


class TicketManager:
    """工单管理器"""

    def __init__(self, db: Database, config: AppConfig):
        self.db = db
        self.config = config
        self.ticket_cfg: TicketConfig = config.ticket

    def _get_valid_priorities(self) -> list[str]:
        """获取有效的优先级列表"""
        if self.ticket_cfg.valid_priorities:
            return self.ticket_cfg.valid_priorities
        return DEFAULT_TICKET_PRIORITIES

    def _validate_priority(self, priority: str) -> None:
        """验证优先级是否有效"""
        valid = self._get_valid_priorities()
        if priority not in valid:
            raise TicketError(
                f"无效的优先级: {priority}，允许值: {', '.join(valid)}"
            )

    def _validate_assignee(self, assignee: str) -> None:
        """验证负责人是否在可转派人员列表中（如果配置了）"""
        if not assignee:
            return
        if self.ticket_cfg.assignable_users:
            if assignee not in self.ticket_cfg.assignable_users:
                raise TicketError(
                    f"用户 '{assignee}' 不在可分配人员列表中，"
                    f"允许值: {', '.join(self.ticket_cfg.assignable_users)}"
                )

    def _generate_ticket_id(self) -> str:
        """生成工单ID"""
        return "TKT-" + uuid.uuid4().hex[:12].upper()

    def create_ticket(self,
                      title: str,
                      creator: str,
                      event_ids: list[str] | None = None,
                      description: str = "",
                      priority: str | None = None,
                      assignee: str = "",
                      due_time: str = "",
                      steps: str = "",
                      note: str = "") -> TicketCreateResult:
        """创建工单

        Args:
            title: 工单标题
            creator: 创建人
            event_ids: 关联的事件ID列表
            description: 描述
            priority: 优先级
            assignee: 负责人
            due_time: 截止时间
            steps: 处理步骤
            note: 备注

        Returns:
            TicketCreateResult
        """
        event_ids = event_ids or []
        priority = priority or self.ticket_cfg.default_priority

        if not title or not title.strip():
            raise TicketError("工单标题不能为空")

        if not creator or not creator.strip():
            raise TicketError("创建人不能为空")

        self._validate_priority(priority)
        if assignee:
            self._validate_assignee(assignee)

        valid_event_ids: list[str] = []
        conflict_events: list[str] = []
        closed_events: list[str] = []

        for eid in event_ids:
            event = self.db.get_event(eid)
            if event is None:
                raise TicketError(f"事件不存在: {eid}")

            if event.status == "closed":
                if not self.ticket_cfg.allow_closed_event_ticket:
                    closed_events.append(eid)
                    continue
                valid_event_ids.append(eid)
            else:
                open_tickets = self.db.get_open_tickets_for_event(eid)
                if open_tickets and not self.ticket_cfg.allow_duplicate_open_ticket:
                    conflict_events.append(eid)
                    continue
                valid_event_ids.append(eid)

        if not valid_event_ids and event_ids:
            if conflict_events:
                raise TicketConflictError(
                    f"所有 {len(event_ids)} 个事件都存在未完成工单，无法创建新工单。"
                    f"可设置 allow_duplicate_open_ticket=true 允许重复开单。"
                )
            if closed_events:
                raise TicketConflictError(
                    f"所有 {len(event_ids)} 个事件都已关闭，无法创建工单。"
                    f"可设置 allow_closed_event_ticket=true 允许为已关闭事件开单。"
                )

        ticket_id = self._generate_ticket_id()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        status = TICKET_STATUS_ASSIGNED if assignee else TICKET_STATUS_OPEN

        ticket = Ticket(
            id=ticket_id,
            title=title.strip(),
            description=description.strip(),
            priority=priority,
            status=status,
            assignee=assignee.strip(),
            creator=creator.strip(),
            due_time=due_time.strip(),
            steps=steps.strip(),
            note=note.strip(),
            created_at=now,
            updated_at=now,
            version=1,
        )

        self.db.insert_ticket(ticket, event_ids=valid_event_ids)

        self.db.add_ticket_log(
            ticket_id=ticket_id,
            operation=TICKET_LOG_OP_CREATE,
            operator=creator.strip(),
            old_status="",
            new_status=status,
            old_assignee="",
            new_assignee=assignee.strip(),
            note=f"创建工单，关联 {len(valid_event_ids)} 个事件",
        )

        return TicketCreateResult(
            ticket=ticket,
            event_ids=valid_event_ids,
            conflict_events=conflict_events,
            closed_events=closed_events,
        )

    def claim_ticket(self, ticket_id: str, operator: str,
                     note: str = "") -> TicketOperationResult:
        """领取工单

        Args:
            ticket_id: 工单ID
            operator: 领取人
            note: 备注

        Returns:
            TicketOperationResult
        """
        if not operator or not operator.strip():
            raise TicketError("操作人不能为空")

        self._validate_assignee(operator.strip())

        ticket = self.db.get_ticket(ticket_id)
        if ticket is None:
            raise TicketError(f"工单不存在: {ticket_id}")

        if ticket.status in (TICKET_STATUS_COMPLETED, TICKET_STATUS_REVOKED):
            raise TicketError(
                f"工单处于 {TICKET_STATUS_LABELS.get(ticket.status, ticket.status)} 状态，无法领取"
            )

        if ticket.status == TICKET_STATUS_IN_PROGRESS and ticket.assignee == operator.strip():
            raise TicketError(f"您已经是该工单的负责人，无需重复领取")

        old_status = ticket.status
        old_assignee = ticket.assignee
        new_status = TICKET_STATUS_IN_PROGRESS
        new_assignee = operator.strip()

        ticket.status = new_status
        ticket.assignee = new_assignee
        ticket.updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ticket.version += 1
        self.db.update_ticket(ticket)

        self.db.add_ticket_log(
            ticket_id=ticket_id,
            operation=TICKET_LOG_OP_CLAIM,
            operator=operator.strip(),
            old_status=old_status,
            new_status=new_status,
            old_assignee=old_assignee,
            new_assignee=new_assignee,
            note=note.strip(),
        )

        return TicketOperationResult(
            ticket_id=ticket_id,
            operation=TICKET_LOG_OP_CLAIM,
            old_status=old_status,
            new_status=new_status,
            old_assignee=old_assignee,
            new_assignee=new_assignee,
            operator=operator.strip(),
            note=note.strip(),
        )

    def assign_ticket(self, ticket_id: str, new_assignee: str,
                      operator: str, note: str = "") -> TicketOperationResult:
        """转派工单

        Args:
            ticket_id: 工单ID
            new_assignee: 新负责人
            operator: 操作人
            note: 备注

        Returns:
            TicketOperationResult
        """
        if not new_assignee or not new_assignee.strip():
            raise TicketError("新负责人不能为空")

        if not operator or not operator.strip():
            raise TicketError("操作人不能为空")

        self._validate_assignee(new_assignee.strip())

        ticket = self.db.get_ticket(ticket_id)
        if ticket is None:
            raise TicketError(f"工单不存在: {ticket_id}")

        if ticket.status in (TICKET_STATUS_COMPLETED, TICKET_STATUS_REVOKED):
            raise TicketError(
                f"工单处于 {TICKET_STATUS_LABELS.get(ticket.status, ticket.status)} 状态，无法转派"
            )

        if ticket.assignee == new_assignee.strip():
            raise TicketError(f"工单负责人已经是 {new_assignee}")

        old_status = ticket.status
        old_assignee = ticket.assignee
        new_status = TICKET_STATUS_ASSIGNED if ticket.status == TICKET_STATUS_OPEN else ticket.status

        ticket.assignee = new_assignee.strip()
        ticket.status = new_status
        ticket.updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ticket.version += 1
        self.db.update_ticket(ticket)

        self.db.add_ticket_log(
            ticket_id=ticket_id,
            operation=TICKET_LOG_OP_ASSIGN,
            operator=operator.strip(),
            old_status=old_status,
            new_status=new_status,
            old_assignee=old_assignee,
            new_assignee=new_assignee.strip(),
            note=note.strip(),
        )

        return TicketOperationResult(
            ticket_id=ticket_id,
            operation=TICKET_LOG_OP_ASSIGN,
            old_status=old_status,
            new_status=new_status,
            old_assignee=old_assignee,
            new_assignee=new_assignee.strip(),
            operator=operator.strip(),
            note=note.strip(),
        )

    def complete_ticket(self, ticket_id: str, operator: str,
                        note: str = "") -> TicketOperationResult:
        """完成工单

        Args:
            ticket_id: 工单ID
            operator: 操作人
            note: 备注

        Returns:
            TicketOperationResult
        """
        if not operator or not operator.strip():
            raise TicketError("操作人不能为空")

        ticket = self.db.get_ticket(ticket_id)
        if ticket is None:
            raise TicketError(f"工单不存在: {ticket_id}")

        if ticket.status == TICKET_STATUS_COMPLETED:
            raise TicketError("工单已经完成，无需重复操作")

        if ticket.status == TICKET_STATUS_REVOKED:
            raise TicketError("工单已撤回，无法完成")

        old_status = ticket.status
        old_assignee = ticket.assignee
        new_status = TICKET_STATUS_COMPLETED

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ticket.status = new_status
        ticket.completed_at = now
        ticket.updated_at = now
        ticket.version += 1
        self.db.update_ticket(ticket)

        self.db.add_ticket_log(
            ticket_id=ticket_id,
            operation=TICKET_LOG_OP_COMPLETE,
            operator=operator.strip(),
            old_status=old_status,
            new_status=new_status,
            old_assignee=old_assignee,
            new_assignee=old_assignee,
            note=note.strip(),
        )

        return TicketOperationResult(
            ticket_id=ticket_id,
            operation=TICKET_LOG_OP_COMPLETE,
            old_status=old_status,
            new_status=new_status,
            old_assignee=old_assignee,
            new_assignee=old_assignee,
            operator=operator.strip(),
            note=note.strip(),
        )

    def revoke_ticket(self, ticket_id: str, operator: str,
                      note: str = "") -> TicketOperationResult:
        """撤回工单

        Args:
            ticket_id: 工单ID
            operator: 操作人
            note: 撤回原因

        Returns:
            TicketOperationResult
        """
        if not operator or not operator.strip():
            raise TicketError("操作人不能为空")

        ticket = self.db.get_ticket(ticket_id)
        if ticket is None:
            raise TicketError(f"工单不存在: {ticket_id}")

        if ticket.status == TICKET_STATUS_REVOKED:
            raise TicketError("工单已经撤回，无需重复操作")

        if ticket.status == TICKET_STATUS_COMPLETED:
            raise TicketError("工单已完成，无法撤回，请重新打开")

        old_status = ticket.status
        old_assignee = ticket.assignee
        new_status = TICKET_STATUS_REVOKED

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ticket.status = new_status
        ticket.updated_at = now
        ticket.version += 1
        self.db.update_ticket(ticket)

        self.db.add_ticket_log(
            ticket_id=ticket_id,
            operation=TICKET_LOG_OP_REVOKE,
            operator=operator.strip(),
            old_status=old_status,
            new_status=new_status,
            old_assignee=old_assignee,
            new_assignee=old_assignee,
            note=note.strip(),
        )

        return TicketOperationResult(
            ticket_id=ticket_id,
            operation=TICKET_LOG_OP_REVOKE,
            old_status=old_status,
            new_status=new_status,
            old_assignee=old_assignee,
            new_assignee=old_assignee,
            operator=operator.strip(),
            note=note.strip(),
        )

    def get_ticket_detail(self, ticket_id: str) -> TicketDetailResult:
        """获取工单详情

        Args:
            ticket_id: 工单ID

        Returns:
            TicketDetailResult
        """
        ticket = self.db.get_ticket(ticket_id)
        if ticket is None:
            raise TicketError(f"工单不存在: {ticket_id}")

        event_ids = self.db.get_ticket_event_ids(ticket_id)
        logs = self.db.get_ticket_logs(ticket_id)

        return TicketDetailResult(
            ticket=ticket,
            event_ids=event_ids,
            logs=logs,
        )

    def list_tickets(self,
                     statuses: list[str] | None = None,
                     priorities: list[str] | None = None,
                     assignees: list[str] | None = None,
                     creators: list[str] | None = None) -> TicketListResult:
        """列出工单

        Args:
            statuses: 按状态筛选
            priorities: 按优先级筛选
            assignees: 按负责人筛选
            creators: 按创建人筛选

        Returns:
            TicketListResult
        """
        tickets = self.db.filter_tickets(
            statuses=statuses,
            priorities=priorities,
            assignees=assignees,
            creators=creators,
        )

        filters: dict[str, Any] = {}
        if statuses:
            filters["statuses"] = statuses
        if priorities:
            filters["priorities"] = priorities
        if assignees:
            filters["assignees"] = assignees
        if creators:
            filters["creators"] = creators

        return TicketListResult(tickets=tickets, filters=filters)

    def check_events_for_close(self, event_ids: list[str]) -> dict[str, Any]:
        """检查批量关闭事件时的工单冲突

        Args:
            event_ids: 要关闭的事件ID列表

        Returns:
            包含冲突信息的字典
        """
        conflict_events: dict[str, list[str]] = {}
        for eid in event_ids:
            open_tickets = self.db.get_open_tickets_for_event(eid)
            if open_tickets:
                conflict_events[eid] = [t.id for t in open_tickets]

        return {
            "total_events": len(event_ids),
            "conflict_event_count": len(conflict_events),
            "conflict_events": conflict_events,
        }

    def list_priorities(self) -> str:
        """列出可用优先级"""
        lines = ["可用优先级:"]
        valid = self._get_valid_priorities()
        labels = self.ticket_cfg.priority_labels()
        for p in valid:
            label = labels.get(p, p)
            default_mark = " (默认)" if p == self.ticket_cfg.default_priority else ""
            lines.append(f"  - {p}: {label}{default_mark}")
        return "\n".join(lines)

    def list_assignable_users(self) -> str:
        """列出可分配人员"""
        if not self.ticket_cfg.assignable_users:
            return "未配置可分配人员列表（允许任意人员）"
        lines = ["可分配人员:"]
        for u in self.ticket_cfg.assignable_users:
            lines.append(f"  - {u}")
        return "\n".join(lines)
