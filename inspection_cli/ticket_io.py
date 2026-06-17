"""工单导入导出：CSV/JSON 格式，冲突处理"""
from __future__ import annotations

import csv
import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from .config import AppConfig
from .database import (
    Database, Ticket, TicketLog,
    TICKET_STATUS_OPEN, TICKET_STATUS_ASSIGNED,
    TICKET_STATUS_IN_PROGRESS, TICKET_STATUS_COMPLETED,
    TICKET_STATUS_REVOKED, VALID_TICKET_STATUSES,
    TICKET_LOG_OP_IMPORT, TICKET_STATUS_LABELS,
    TICKET_IMPORT_CONFLICT_SKIP, TICKET_IMPORT_CONFLICT_ABORT,
    TICKET_IMPORT_CONFLICT_FORCE, VALID_TICKET_IMPORT_CONFLICT_STRATEGIES,
    DEFAULT_TICKET_PRIORITIES,
)
from .ticket import TicketManager, TicketError


@dataclass
class TicketExportResult:
    """工单导出结果"""
    file_path: str
    ticket_count: int
    format: str

    def formatted(self) -> str:
        return f"已导出 {self.ticket_count} 个工单到 {self.file_path} ({self.format.upper()})"


@dataclass
class TicketImportResult:
    """工单导入结果"""
    file_path: str
    total_count: int = 0
    success_count: int = 0
    skipped_count: int = 0
    conflict_count: int = 0
    error_count: int = 0
    conflict_strategy: str = TICKET_IMPORT_CONFLICT_SKIP
    items: list[dict[str, Any]] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return self.error_count > 0

    def formatted(self) -> str:
        lines = [f"工单导入完成: {self.file_path}"]
        lines.append(f"总计: {self.total_count}")
        lines.append(f"成功: {self.success_count}")
        if self.skipped_count:
            lines.append(f"跳过: {self.skipped_count}")
        if self.conflict_count:
            lines.append(f"冲突: {self.conflict_count}")
        if self.error_count:
            lines.append(f"错误: {self.error_count}")
        lines.append(f"冲突策略: {self.conflict_strategy}")
        return "\n".join(lines)


class TicketIOManager:
    """工单导入导出管理器"""

    def __init__(self, db: Database, config: AppConfig, ticket_manager: TicketManager):
        self.db = db
        self.config = config
        self.ticket_manager = ticket_manager

    def export_tickets(self, output_path: str,
                       fmt: Optional[str] = None,
                       include_logs: bool = False,
                       include_events: bool = False) -> TicketExportResult:
        """导出工单列表

        Args:
            output_path: 输出文件路径
            fmt: 格式 (csv/json)，为 None 时根据后缀推断
            include_logs: 是否包含流转日志
            include_events: 是否包含关联事件

        Returns:
            TicketExportResult
        """
        if fmt is None:
            ext = os.path.splitext(output_path)[1].lower().lstrip(".")
            fmt = ext if ext in ("csv", "json") else "json"

        if fmt not in ("csv", "json"):
            raise TicketError(f"不支持的导出格式: {fmt}")

        tickets = self.db.get_all_tickets()

        if fmt == "csv":
            self._export_csv(tickets, output_path)
        else:
            self._export_json(tickets, output_path, include_logs, include_events)

        return TicketExportResult(
            file_path=os.path.abspath(output_path),
            ticket_count=len(tickets),
            format=fmt,
        )

    def _export_csv(self, tickets: list[Ticket], output_path: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(output_path)) or ".", exist_ok=True)

        fieldnames = [
            "ticket_id", "title", "description", "priority", "status",
            "assignee", "creator", "due_time", "steps", "note",
            "created_at", "updated_at", "completed_at", "version",
        ]

        with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for t in tickets:
                writer.writerow(t.to_dict())

    def _export_json(self, tickets: list[Ticket], output_path: str,
                     include_logs: bool = False,
                     include_events: bool = False) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(output_path)) or ".", exist_ok=True)

        ticket_list: list[dict[str, Any]] = []
        for t in tickets:
            t_dict = t.to_dict()
            if include_logs:
                logs = self.db.get_ticket_logs(t.id)
                t_dict["logs"] = [
                    {
                        "id": log.id,
                        "operation": log.operation,
                        "operator": log.operator,
                        "old_status": log.old_status,
                        "new_status": log.new_status,
                        "old_assignee": log.old_assignee,
                        "new_assignee": log.new_assignee,
                        "note": log.note,
                        "operated_at": log.operated_at,
                    }
                    for log in logs
                ]
            if include_events:
                event_ids = self.db.get_ticket_event_ids(t.id)
                t_dict["event_ids"] = event_ids
            ticket_list.append(t_dict)

        output = {
            "version": "1.0",
            "ticket_count": len(tickets),
            "export_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "tickets": ticket_list,
        }

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False, indent=2)

    def import_tickets(self, file_path: str,
                       conflict_strategy: str = TICKET_IMPORT_CONFLICT_SKIP,
                       operator: str = "") -> TicketImportResult:
        """从文件导入工单

        Args:
            file_path: 输入文件路径
            conflict_strategy: 冲突处理策略 (skip/abort/force)
            operator: 操作人

        Returns:
            TicketImportResult
        """
        if conflict_strategy not in VALID_TICKET_IMPORT_CONFLICT_STRATEGIES:
            raise TicketError(
                f"无效的冲突策略: {conflict_strategy}，"
                f"允许值: {', '.join(sorted(VALID_TICKET_IMPORT_CONFLICT_STRATEGIES))}"
            )

        if not os.path.exists(file_path):
            raise TicketError(f"文件不存在: {file_path}")

        ext = os.path.splitext(file_path)[1].lower().lstrip(".")
        fmt = ext if ext in ("csv", "json") else "json"

        result = TicketImportResult(
            file_path=os.path.abspath(file_path),
            conflict_strategy=conflict_strategy,
        )

        try:
            if fmt == "csv":
                ticket_dicts = self._parse_csv(file_path)
            else:
                ticket_dicts = self._parse_json(file_path)
        except Exception as e:
            raise TicketError(f"解析文件失败: {e}") from e

        result.total_count = len(ticket_dicts)

        for idx, t_dict in enumerate(ticket_dicts):
            item_result = self._import_single_ticket(
                t_dict, conflict_strategy, operator, idx
            )
            result.items.append(item_result)

            if item_result["status"] == "success":
                result.success_count += 1
            elif item_result["status"] == "skipped":
                result.skipped_count += 1
            elif item_result["status"] == "conflict":
                result.conflict_count += 1
                if conflict_strategy == TICKET_IMPORT_CONFLICT_ABORT:
                    break
            elif item_result["status"] == "error":
                result.error_count += 1
                if conflict_strategy == TICKET_IMPORT_CONFLICT_ABORT:
                    break

        return result

    def _parse_csv(self, file_path: str) -> list[dict[str, Any]]:
        """解析 CSV 文件"""
        tickets: list[dict[str, Any]] = []
        with open(file_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                tickets.append(self._normalize_ticket_dict(row))
        return tickets

    def _parse_json(self, file_path: str) -> list[dict[str, Any]]:
        """解析 JSON 文件"""
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, dict) and "tickets" in data:
            ticket_list = data["tickets"]
        elif isinstance(data, list):
            ticket_list = data
        else:
            raise ValueError("JSON 格式不正确，需要包含 tickets 数组或直接是数组")

        return [self._normalize_ticket_dict(t) for t in ticket_list]

    def _normalize_ticket_dict(self, t_dict: dict[str, Any]) -> dict[str, Any]:
        """标准化工单字典"""
        normalized: dict[str, Any] = {}

        id_keys = ["ticket_id", "id"]
        for key in id_keys:
            if key in t_dict and t_dict[key]:
                normalized["ticket_id"] = str(t_dict[key])
                break

        title_keys = ["title", "名称", "标题"]
        for key in title_keys:
            if key in t_dict:
                normalized["title"] = str(t_dict.get(key, ""))
                break

        normalized["description"] = str(t_dict.get("description", ""))
        normalized["priority"] = str(t_dict.get("priority", "medium")).lower()
        normalized["status"] = str(t_dict.get("status", "open")).lower()
        normalized["assignee"] = str(t_dict.get("assignee", ""))
        normalized["creator"] = str(t_dict.get("creator", "import"))
        normalized["due_time"] = str(t_dict.get("due_time", ""))
        normalized["steps"] = str(t_dict.get("steps", ""))
        normalized["note"] = str(t_dict.get("note", ""))
        normalized["created_at"] = str(t_dict.get("created_at", ""))
        normalized["updated_at"] = str(t_dict.get("updated_at", ""))
        normalized["completed_at"] = str(t_dict.get("completed_at", ""))
        normalized["version"] = int(t_dict.get("version", 1) or 1)

        if "event_ids" in t_dict:
            normalized["event_ids"] = t_dict["event_ids"]
        if "logs" in t_dict:
            normalized["logs"] = t_dict["logs"]

        return normalized

    def _import_single_ticket(self, t_dict: dict[str, Any],
                              conflict_strategy: str,
                              operator: str,
                              index: int) -> dict[str, Any]:
        """导入单个工单"""
        item_result = {
            "index": index,
            "ticket_id": t_dict.get("ticket_id", f"import-{index}"),
            "status": "pending",
            "reason": "",
        }

        try:
            title = t_dict.get("title", "").strip()
            if not title:
                item_result["status"] = "error"
                item_result["reason"] = "标题不能为空"
                return item_result

            priority = t_dict.get("priority", "medium")
            valid_priorities = self.ticket_manager._get_valid_priorities()
            if priority not in valid_priorities:
                priority = self.ticket_manager.ticket_cfg.default_priority
                t_dict["priority"] = priority

            status = t_dict.get("status", "open")
            if status not in VALID_TICKET_STATUSES:
                status = TICKET_STATUS_OPEN
                t_dict["status"] = status

            assignee = t_dict.get("assignee", "")
            if assignee:
                try:
                    self.ticket_manager._validate_assignee(assignee)
                except TicketError as e:
                    item_result["status"] = "error"
                    item_result["reason"] = f"负责人无效: {e}"
                    return item_result

            ticket_id = t_dict.get("ticket_id", "")
            existing = None
            if ticket_id:
                existing = self.db.get_ticket(ticket_id)

            if existing:
                if conflict_strategy == TICKET_IMPORT_CONFLICT_SKIP:
                    item_result["status"] = "conflict"
                    item_result["reason"] = "工单ID已存在，跳过"
                    return item_result
                elif conflict_strategy == TICKET_IMPORT_CONFLICT_FORCE:
                    self._update_existing_ticket(existing, t_dict, operator)
                    item_result["status"] = "success"
                    item_result["reason"] = "已覆盖"
                    return item_result
                else:
                    item_result["status"] = "conflict"
                    item_result["reason"] = "工单ID已存在"
                    return item_result

            self._create_imported_ticket(t_dict, operator)
            item_result["status"] = "success"
            item_result["reason"] = "导入成功"

        except Exception as e:
            item_result["status"] = "error"
            item_result["reason"] = str(e)

        return item_result

    def _create_imported_ticket(self, t_dict: dict[str, Any], operator: str) -> None:
        """创建导入的工单"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        created_at = t_dict.get("created_at") or now
        updated_at = t_dict.get("updated_at") or now
        completed_at = t_dict.get("completed_at", "")

        ticket_id = t_dict.get("ticket_id") or self._generate_ticket_id()

        ticket = Ticket(
            id=ticket_id,
            title=t_dict.get("title", ""),
            description=t_dict.get("description", ""),
            priority=t_dict.get("priority", "medium"),
            status=t_dict.get("status", TICKET_STATUS_OPEN),
            assignee=t_dict.get("assignee", ""),
            creator=t_dict.get("creator", operator or "import"),
            due_time=t_dict.get("due_time", ""),
            steps=t_dict.get("steps", ""),
            note=t_dict.get("note", ""),
            created_at=created_at,
            updated_at=updated_at,
            completed_at=completed_at,
            version=int(t_dict.get("version", 1) or 1),
        )

        event_ids = t_dict.get("event_ids", [])
        if event_ids:
            valid_event_ids = [eid for eid in event_ids if self.db.event_exists(eid)]
        else:
            valid_event_ids = []

        self.db.insert_ticket(ticket, event_ids=valid_event_ids)

        self.db.add_ticket_log(
            ticket_id=ticket_id,
            operation=TICKET_LOG_OP_IMPORT,
            operator=operator or "import",
            old_status="",
            new_status=ticket.status,
            old_assignee="",
            new_assignee=ticket.assignee,
            note=f"导入工单，关联 {len(valid_event_ids)} 个事件",
        )

    def _update_existing_ticket(self, existing: Ticket, t_dict: dict[str, Any],
                                operator: str) -> None:
        """更新已存在的工单（force 模式）"""
        old_status = existing.status
        old_assignee = existing.assignee

        existing.title = t_dict.get("title", existing.title)
        existing.description = t_dict.get("description", existing.description)
        existing.priority = t_dict.get("priority", existing.priority)
        existing.status = t_dict.get("status", existing.status)
        existing.assignee = t_dict.get("assignee", existing.assignee)
        existing.due_time = t_dict.get("due_time", existing.due_time)
        existing.steps = t_dict.get("steps", existing.steps)
        existing.note = t_dict.get("note", existing.note)
        existing.updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        existing.version += 1

        self.db.update_ticket(existing)

        event_ids = t_dict.get("event_ids", [])
        if event_ids:
            for eid in event_ids:
                if self.db.event_exists(eid):
                    self.db.add_ticket_event(existing.id, eid)

        self.db.add_ticket_log(
            ticket_id=existing.id,
            operation=TICKET_LOG_OP_IMPORT,
            operator=operator or "import",
            old_status=old_status,
            new_status=existing.status,
            old_assignee=old_assignee,
            new_assignee=existing.assignee,
            note="导入覆盖",
        )

    def _generate_ticket_id(self) -> str:
        """生成工单ID"""
        return "TKT-" + uuid.uuid4().hex[:12].upper()
