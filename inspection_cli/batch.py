"""批量操作管理器：筛选、预览、版本冲突检测、批量更新和日志记录"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from .annotation import AnnotationManager, STATUS_LABELS
from .config import AppConfig
from .database import (
    BATCH_STATUS_COMPLETED, BATCH_STATUS_PARTIAL, BATCH_STATUS_PENDING,
    BatchOperation, BatchOperationItem, CONFLICT_STRATEGY_ABORT,
    CONFLICT_STRATEGY_FORCE, CONFLICT_STRATEGY_SKIP, Database, Event,
    ITEM_STATUS_CONFLICT, ITEM_STATUS_ERROR, ITEM_STATUS_SKIPPED,
    ITEM_STATUS_SUCCESS, VALID_CONFLICT_STRATEGIES, VALID_STATUSES,
)


class BatchOperationError(Exception):
    """批量操作错误"""
    pass


@dataclass
class BatchFilter:
    """批量操作筛选条件"""
    event_ids: Optional[list[str]] = None
    device_ids: Optional[list[str]] = None
    statuses: Optional[list[str]] = None
    time_from: Optional[str] = None
    time_to: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_ids": self.event_ids,
            "device_ids": self.device_ids,
            "statuses": self.statuses,
            "time_from": self.time_from,
            "time_to": self.time_to,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    def describe(self) -> str:
        parts = []
        if self.event_ids:
            parts.append(f"事件ID: {', '.join(self.event_ids)}")
        if self.device_ids:
            parts.append(f"设备: {', '.join(self.device_ids)}")
        if self.statuses:
            parts.append(f"状态: {', '.join(self.statuses)}")
        if self.time_from:
            parts.append(f"起始时间: {self.time_from}")
        if self.time_to:
            parts.append(f"结束时间: {self.time_to}")
        return "; ".join(parts) if parts else "无筛选条件（所有事件）"


@dataclass
class BatchUpdate:
    """批量更新内容"""
    status: Optional[str] = None
    handler: Optional[str] = None
    note: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "handler": self.handler,
            "note": self.note,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    def describe(self) -> str:
        parts = []
        if self.status:
            parts.append(f"状态 → {STATUS_LABELS.get(self.status, self.status)}")
        if self.handler:
            parts.append(f"处理人 → {self.handler}")
        if self.note is not None:
            parts.append(f"备注 → {self.note or '(空)'}")
        return "; ".join(parts) if parts else "无更新内容"


@dataclass
class BatchPreviewResult:
    """批量操作预览结果"""
    events: list[Event]
    filter: BatchFilter
    update: BatchUpdate
    preview_fields: list[str]

    def formatted(self) -> str:
        if not self.events:
            return "没有符合条件的事件。"

        lines = [f"共 {len(self.events)} 个事件将被修改:"]
        lines.append("")
        lines.append(f"筛选条件: {self.filter.describe()}")
        lines.append(f"更新内容: {self.update.describe()}")
        lines.append("")

        field_widths = {
            "event_id": 22, "status": 12, "device_id": 14,
            "first_seen": 20, "last_seen": 20, "severity": 10,
            "issue_type": 14, "handler": 12, "note": 20, "version": 8,
        }

        header = "  ".join(
            f"{f:<{field_widths.get(f, 15)}}" for f in self.preview_fields
        )
        lines.append(header)
        lines.append("-" * len(header))

        for ev in self.events:
            ev_dict = ev.to_dict()
            ev_dict["status"] = STATUS_LABELS.get(ev.status, ev.status)
            row = "  ".join(
                f"{str(ev_dict.get(f, '')):<{field_widths.get(f, 15)}}"
                for f in self.preview_fields
            )
            lines.append(row)

        return "\n".join(lines)


@dataclass
class BatchExecuteResult:
    """批量操作执行结果"""
    batch_id: str
    total_count: int
    success_count: int = 0
    skipped_count: int = 0
    conflict_count: int = 0
    error_count: int = 0
    items: list[BatchOperationItem] = field(default_factory=list)

    def formatted(self) -> str:
        lines = [f"批量操作 {self.batch_id} 完成"]
        lines.append(f"总计: {self.total_count}")
        lines.append(f"成功: {self.success_count}")
        if self.skipped_count:
            lines.append(f"跳过: {self.skipped_count}")
        if self.conflict_count:
            lines.append(f"冲突: {self.conflict_count}")
        if self.error_count:
            lines.append(f"错误: {self.error_count}")
        lines.append("")

        if self.conflict_count or self.skipped_count or self.error_count:
            lines.append("详情:")
            for item in self.items:
                if item.status != ITEM_STATUS_SUCCESS:
                    status_label = {
                        ITEM_STATUS_SKIPPED: "跳过",
                        ITEM_STATUS_CONFLICT: "冲突",
                        ITEM_STATUS_ERROR: "错误",
                    }.get(item.status, item.status)
                    lines.append(f"  [{status_label}] {item.event_id}: {item.reason}")

        return "\n".join(lines)


class BatchOperationManager:
    """批量操作管理器"""

    def __init__(self, db: Database, config: AppConfig):
        self.db = db
        self.config = config
        self.batch_cfg = config.batch
        self.annotation_manager = AnnotationManager(db)

    def preview(self, batch_filter: BatchFilter) -> list[Event]:
        """预览符合筛选条件的事件"""
        self._validate_filter(batch_filter)
        return self.db.filter_events(
            event_ids=batch_filter.event_ids,
            device_ids=batch_filter.device_ids,
            statuses=batch_filter.statuses,
            time_from=batch_filter.time_from,
            time_to=batch_filter.time_to,
        )

    def format_preview(self, events: list[Event], batch_filter: BatchFilter,
                       batch_update: BatchUpdate) -> BatchPreviewResult:
        """格式化预览结果"""
        return BatchPreviewResult(
            events=events,
            filter=batch_filter,
            update=batch_update,
            preview_fields=self.batch_cfg.preview_fields,
        )

    def execute(self, batch_filter: BatchFilter, batch_update: BatchUpdate,
                operator: str, conflict_strategy: Optional[str] = None,
                preview_events: Optional[list[Event]] = None) -> BatchExecuteResult:
        """执行批量更新"""
        self._validate_filter(batch_filter)
        self._validate_update(batch_update)
        self._validate_operator(operator)

        if conflict_strategy is None:
            conflict_strategy = self.batch_cfg.conflict_strategy
        if conflict_strategy not in VALID_CONFLICT_STRATEGIES:
            raise BatchOperationError(
                f"无效的冲突策略: {conflict_strategy}，"
                f"允许值: {', '.join(sorted(VALID_CONFLICT_STRATEGIES))}"
            )

        if preview_events is None:
            events = self.preview(batch_filter)
        else:
            events = preview_events

        if not events:
            raise BatchOperationError("没有符合条件的事件，无法执行批量操作。")

        total_count = len(events)
        batch_id = self.db.create_batch_operation(
            operation_type="annotate",
            operator=operator,
            filters=batch_filter.to_json(),
            updates=batch_update.to_json(),
            total_count=total_count,
            conflict_strategy=conflict_strategy,
        )

        result = BatchExecuteResult(batch_id=batch_id, total_count=total_count)

        for ev in events:
            item = self._process_event(
                ev, batch_update, batch_id, conflict_strategy
            )
            result.items.append(item)

            if item.status == ITEM_STATUS_SUCCESS:
                result.success_count += 1
            elif item.status == ITEM_STATUS_SKIPPED:
                result.skipped_count += 1
            elif item.status == ITEM_STATUS_CONFLICT:
                result.conflict_count += 1
                if conflict_strategy == CONFLICT_STRATEGY_ABORT:
                    self.db.update_batch_operation_counts(
                        batch_id, result.success_count, result.skipped_count,
                        result.conflict_count, result.error_count
                    )
                    self.db.complete_batch_operation(batch_id)
                    raise BatchOperationError(
                        f"检测到版本冲突（事件 {ev.id}），已中止批量操作。"
                        f"已成功处理 {result.success_count} 个事件。"
                    )
            elif item.status == ITEM_STATUS_ERROR:
                result.error_count += 1

        self.db.update_batch_operation_counts(
            batch_id, result.success_count, result.skipped_count,
            result.conflict_count, result.error_count
        )
        self.db.complete_batch_operation(batch_id)

        return result

    def _process_event(self, ev: Event, batch_update: BatchUpdate,
                       batch_id: str, conflict_strategy: str) -> BatchOperationItem:
        """处理单个事件的更新"""
        item_id = str(uuid.uuid4())
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        old_version = ev.version
        old_status = ev.status
        old_handler = ev.handler
        old_note = ev.note

        new_status = batch_update.status if batch_update.status else old_status
        new_handler = batch_update.handler if batch_update.handler else old_handler
        new_note = batch_update.note if batch_update.note is not None else old_note

        def _save_and_return(item: BatchOperationItem) -> BatchOperationItem:
            self.db.add_batch_operation_item(item)
            return item

        if batch_update.status and batch_update.status not in VALID_STATUSES:
            return _save_and_return(BatchOperationItem(
                id=item_id, batch_id=batch_id, event_id=ev.id,
                old_version=old_version, new_version=old_version,
                old_status=old_status, new_status=old_status,
                old_handler=old_handler, new_handler=old_handler,
                old_note=old_note, new_note=old_note,
                status=ITEM_STATUS_ERROR,
                reason=f"无效的状态值: {batch_update.status}",
                processed_at=now,
            ))

        if (old_status == new_status and
                old_handler == new_handler and
                old_note == new_note):
            return _save_and_return(BatchOperationItem(
                id=item_id, batch_id=batch_id, event_id=ev.id,
                old_version=old_version, new_version=old_version,
                old_status=old_status, new_status=new_status,
                old_handler=old_handler, new_handler=new_handler,
                old_note=old_note, new_note=new_note,
                status=ITEM_STATUS_SKIPPED,
                reason="无需更新：目标值与当前值相同",
                processed_at=now,
            ))

        current_ev = self.db.get_event(ev.id)
        if current_ev is None:
            return _save_and_return(BatchOperationItem(
                id=item_id, batch_id=batch_id, event_id=ev.id,
                old_version=old_version, new_version=old_version,
                old_status=old_status, new_status=new_status,
                old_handler=old_handler, new_handler=new_handler,
                old_note=old_note, new_note=new_note,
                status=ITEM_STATUS_CONFLICT,
                reason="事件已不存在（可能已被删除或重新归并）",
                processed_at=now,
            ))

        if current_ev.version != old_version:
            if conflict_strategy == CONFLICT_STRATEGY_SKIP:
                return _save_and_return(BatchOperationItem(
                    id=item_id, batch_id=batch_id, event_id=ev.id,
                    old_version=old_version, new_version=current_ev.version,
                    old_status=old_status, new_status=old_status,
                    old_handler=old_handler, new_handler=old_handler,
                    old_note=old_note, new_note=old_note,
                    status=ITEM_STATUS_CONFLICT,
                    reason=f"版本冲突：预览时版本={old_version}，当前版本={current_ev.version}，"
                           f"可能已被其他操作修改",
                    processed_at=now,
                ))
            elif conflict_strategy == CONFLICT_STRATEGY_FORCE:
                pass
            else:
                return _save_and_return(BatchOperationItem(
                    id=item_id, batch_id=batch_id, event_id=ev.id,
                    old_version=old_version, new_version=current_ev.version,
                    old_status=old_status, new_status=old_status,
                    old_handler=old_handler, new_handler=old_handler,
                    old_note=old_note, new_note=old_note,
                    status=ITEM_STATUS_CONFLICT,
                    reason=f"版本冲突：预览时版本={old_version}，当前版本={current_ev.version}",
                    processed_at=now,
                ))

        update_ev = Event(
            id=current_ev.id,
            device_id=current_ev.device_id,
            first_seen=current_ev.first_seen,
            last_seen=current_ev.last_seen,
            issue_type=current_ev.issue_type,
            severity=current_ev.severity,
            status=new_status,
            handler=new_handler,
            note=new_note,
            record_count=current_ev.record_count,
            record_ids=current_ev.record_ids,
            version=current_ev.version,
        )

        expected_version = current_ev.version
        if conflict_strategy == CONFLICT_STRATEGY_FORCE:
            self.db.update_event(update_ev)
            new_version = update_ev.version
        else:
            success = self.db.update_event_with_version(update_ev, expected_version)
            if not success:
                current_ev2 = self.db.get_event(ev.id)
                actual_version = current_ev2.version if current_ev2 else -1
                return BatchOperationItem(
                    id=item_id, batch_id=batch_id, event_id=ev.id,
                    old_version=expected_version, new_version=actual_version,
                    old_status=old_status, new_status=old_status,
                    old_handler=old_handler, new_handler=old_handler,
                    old_note=old_note, new_note=old_note,
                    status=ITEM_STATUS_CONFLICT,
                    reason=f"版本冲突：预期版本={expected_version}，更新时检测到版本={actual_version}",
                    processed_at=now,
                )
            new_version = update_ev.version

        if batch_update.status and old_status != new_status:
            try:
                self.db.add_annotation(
                    event_id=ev.id,
                    old_status=old_status,
                    new_status=new_status,
                    handler=new_handler,
                    note=new_note,
                )
            except Exception as e:
                return BatchOperationItem(
                    id=item_id, batch_id=batch_id, event_id=ev.id,
                    old_version=expected_version, new_version=new_version,
                    old_status=old_status, new_status=new_status,
                    old_handler=old_handler, new_handler=new_handler,
                    old_note=old_note, new_note=new_note,
                    status=ITEM_STATUS_ERROR,
                    reason=f"记录标注历史失败: {e}",
                    processed_at=now,
                )

        self.db.add_batch_operation_item(BatchOperationItem(
            id=item_id, batch_id=batch_id, event_id=ev.id,
            old_version=old_version, new_version=new_version,
            old_status=old_status, new_status=new_status,
            old_handler=old_handler, new_handler=new_handler,
            old_note=old_note, new_note=new_note,
            status=ITEM_STATUS_SUCCESS,
            reason="",
            processed_at=now,
        ))

        return BatchOperationItem(
            id=item_id, batch_id=batch_id, event_id=ev.id,
            old_version=old_version, new_version=new_version,
            old_status=old_status, new_status=new_status,
            old_handler=old_handler, new_handler=new_handler,
            old_note=old_note, new_note=new_note,
            status=ITEM_STATUS_SUCCESS,
            reason="",
            processed_at=now,
        )

    def get_batch_logs(self, limit: int = 20) -> str:
        """获取批量操作日志"""
        batches = self.db.get_recent_batch_operations(limit)
        if not batches:
            return "暂无批量操作记录。"

        lines = [f"最近 {len(batches)} 条批量操作记录:"]
        lines.append("")

        header = f"{'批量ID':<22} {'类型':<10} {'状态':<12} {'操作人':<12} {'总数':<6} {'成功':<6} {'跳过':<6} {'冲突':<6} {'错误':<6} {'创建时间':<20}"
        lines.append(header)
        lines.append("-" * len(header))

        status_labels = {
            BATCH_STATUS_PENDING: "处理中",
            BATCH_STATUS_COMPLETED: "已完成",
            BATCH_STATUS_PARTIAL: "部分完成",
        }

        for batch in batches:
            status = status_labels.get(batch.status, batch.status)
            lines.append(
                f"{batch.id:<22} {batch.operation_type:<10} {status:<12} "
                f"{batch.operator:<12} {batch.total_count:<6} "
                f"{batch.success_count:<6} {batch.skipped_count:<6} "
                f"{batch.conflict_count:<6} {batch.error_count:<6} "
                f"{batch.created_at:<20}"
            )

        return "\n".join(lines)

    def get_batch_detail(self, batch_id: str) -> str:
        """获取批量操作详情"""
        batch = self.db.get_batch_operation(batch_id)
        if not batch:
            return f"批量操作不存在: {batch_id}"

        items = self.db.get_batch_operation_items(batch_id)

        status_labels = {
            BATCH_STATUS_PENDING: "处理中",
            BATCH_STATUS_COMPLETED: "已完成",
            BATCH_STATUS_PARTIAL: "部分完成",
        }

        item_status_labels = {
            ITEM_STATUS_SUCCESS: "成功",
            ITEM_STATUS_SKIPPED: "跳过",
            ITEM_STATUS_CONFLICT: "冲突",
            ITEM_STATUS_ERROR: "错误",
        }

        lines = [f"批量操作详情: {batch_id}"]
        lines.append("=" * 60)
        lines.append(f"类型: {batch.operation_type}")
        lines.append(f"状态: {status_labels.get(batch.status, batch.status)}")
        lines.append(f"操作人: {batch.operator}")
        lines.append(f"冲突策略: {batch.conflict_strategy}")
        lines.append(f"创建时间: {batch.created_at}")
        if batch.completed_at:
            lines.append(f"完成时间: {batch.completed_at}")
        lines.append("")
        lines.append(f"筛选条件: {json.loads(batch.filters)}")
        lines.append(f"更新内容: {json.loads(batch.updates)}")
        lines.append("")
        lines.append(
            f"总计: {batch.total_count} | 成功: {batch.success_count} | "
            f"跳过: {batch.skipped_count} | 冲突: {batch.conflict_count} | "
            f"错误: {batch.error_count}"
        )
        lines.append("")
        lines.append("各事件处理详情:")
        lines.append("-" * 60)

        for item in items:
            status = item_status_labels.get(item.status, item.status)
            line = f"[{status}] {item.event_id} "
            line += f"(v{item.old_version} → v{item.new_version}) "
            if item.old_status != item.new_status:
                line += f"状态: {item.old_status} → {item.new_status} "
            if item.old_handler != item.new_handler:
                line += f"处理人: {item.old_handler} → {item.new_handler} "
            if item.reason:
                line += f"原因: {item.reason}"
            lines.append(line)

        return "\n".join(lines)

    def cleanup_old_logs(self, days: Optional[int] = None) -> int:
        """清理旧的批量操作日志"""
        if days is None:
            days = self.batch_cfg.log_retention_days
        return self.db.cleanup_old_batch_operations(days)

    def _validate_filter(self, batch_filter: BatchFilter) -> None:
        """验证筛选条件"""
        if batch_filter.statuses:
            for s in batch_filter.statuses:
                if s not in VALID_STATUSES:
                    raise BatchOperationError(
                        f"无效的状态筛选值: {s}，"
                        f"允许值: {', '.join(sorted(VALID_STATUSES))}"
                    )

        if batch_filter.time_from:
            self._validate_time_format(batch_filter.time_from)
        if batch_filter.time_to:
            self._validate_time_format(batch_filter.time_to)

    def _validate_update(self, batch_update: BatchUpdate) -> None:
        """验证更新内容"""
        if (batch_update.status is None and
                batch_update.handler is None and
                batch_update.note is None):
            raise BatchOperationError("没有指定任何更新内容。")

        if batch_update.status is not None:
            if batch_update.status not in VALID_STATUSES:
                raise BatchOperationError(
                    f"无效的目标状态: {batch_update.status}，"
                    f"允许值: {', '.join(sorted(VALID_STATUSES))}"
                )

        if batch_update.handler is not None and not batch_update.handler.strip():
            raise BatchOperationError("处理人不能为空字符串。")

    def _validate_operator(self, operator: str) -> None:
        """验证操作人"""
        if not operator or not operator.strip():
            raise BatchOperationError("操作人不能为空。")

    def _validate_time_format(self, time_str: str) -> None:
        """验证时间格式"""
        formats = [
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%d",
        ]
        for fmt in formats:
            try:
                datetime.strptime(time_str, fmt)
                return
            except ValueError:
                continue
        raise BatchOperationError(
            f"无效的时间格式: {time_str}，支持格式: "
            f"YYYY-MM-DD HH:MM:SS, YYYY-MM-DD HH:MM, YYYY-MM-DD"
        )
