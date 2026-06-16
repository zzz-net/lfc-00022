"""标注管理：事件状态标注与撤销"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .database import Database, Event, VALID_STATUSES


class AnnotationError(Exception):
    """标注操作错误"""
    pass


VALID_TARGET_STATUSES = {"confirmed", "false_positive", "closed"}
STATUS_LABELS = {
    "unconfirmed": "待确认",
    "confirmed": "已确认",
    "false_positive": "误报",
    "closed": "已关闭",
}


@dataclass
class AnnotationResult:
    """标注操作结果"""
    event_id: str
    old_status: str
    new_status: str
    handler: str
    note: str = ""

    def formatted(self) -> str:
        return (
            f"事件 {self.event_id} 标注成功\n"
            f"  状态: {STATUS_LABELS.get(self.old_status, self.old_status)} "
            f"→ {STATUS_LABELS.get(self.new_status, self.new_status)}\n"
            f"  处理人: {self.handler}"
            + (f"\n  备注: {self.note}" if self.note else "")
        )


@dataclass
class UndoResult:
    """撤销操作结果"""
    event_id: str
    restored_status: str
    removed_annotation_id: str

    def formatted(self) -> str:
        return (
            f"事件 {self.event_id} 撤销成功\n"
            f"  已恢复状态: {STATUS_LABELS.get(self.restored_status, self.restored_status)}\n"
            f"  删除的标注 ID: {self.removed_annotation_id}"
        )


class AnnotationManager:
    """标注管理器"""

    def __init__(self, db: Database):
        self.db = db

    def annotate(self, event_id: str, status: str, handler: str,
                 note: str = "") -> AnnotationResult:
        """标注事件状态

        Args:
            event_id: 事件 ID
            status: 目标状态 (confirmed/false_positive/closed)
            handler: 处理人
            note: 备注
        """
        if status not in VALID_TARGET_STATUSES:
            raise AnnotationError(
                f"无效的目标状态: {status}，允许值: {', '.join(sorted(VALID_TARGET_STATUSES))}"
            )

        if not handler or not handler.strip():
            raise AnnotationError("处理人不能为空")

        event: Optional[Event] = self.db.get_event(event_id)
        if event is None:
            raise AnnotationError(f"事件不存在: {event_id}")

        old_status = event.status
        if old_status == status:
            raise AnnotationError(
                f"事件 {event_id} 已经是 {STATUS_LABELS.get(status, status)} 状态"
            )

        event.status = status
        event.handler = handler.strip()
        event.note = note.strip()
        self.db.update_event(event)

        self.db.add_annotation(
            event_id=event_id,
            old_status=old_status,
            new_status=status,
            handler=handler.strip(),
            note=note.strip(),
        )

        return AnnotationResult(
            event_id=event_id,
            old_status=old_status,
            new_status=status,
            handler=handler.strip(),
            note=note.strip(),
        )

    def undo(self, event_id: str) -> UndoResult:
        """撤销最后一次标注

        Args:
            event_id: 事件 ID
        """
        event: Optional[Event] = self.db.get_event(event_id)
        if event is None:
            raise AnnotationError(f"事件不存在: {event_id}")

        if self.db.get_annotation_count(event_id) == 0:
            raise AnnotationError(
                f"事件 {event_id} 没有标注历史，无法撤销。"
                f"当前状态为 {STATUS_LABELS.get(event.status, event.status)}，"
                f"尚未进行过任何标注操作。"
            )

        annotations = self.db.get_annotations_for_event(event_id)
        last_ann = annotations[-1]

        restored = last_ann.old_status
        if restored not in VALID_STATUSES:
            restored = "unconfirmed"

        event.status = restored
        if len(annotations) >= 2:
            prev_ann = annotations[-2]
            event.handler = prev_ann.handler
            event.note = prev_ann.note
        else:
            event.handler = ""
            event.note = ""
        self.db.update_event(event)

        self.db.delete_annotation(last_ann.id)

        return UndoResult(
            event_id=event_id,
            restored_status=restored,
            removed_annotation_id=last_ann.id,
        )

    def list_statuses(self) -> str:
        """列出可用状态"""
        lines = ["可用状态:"]
        for key, label in STATUS_LABELS.items():
            lines.append(f"  - {key}: {label}")
        return "\n".join(lines)
