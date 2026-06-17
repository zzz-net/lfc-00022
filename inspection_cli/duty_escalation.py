"""值班升级匹配引擎：根据时间窗口、升级顺序命中责任人"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional

from .config import AppConfig
from .database import (
    Database, DutySchedule, DutyMember, DutyEscalationLevel,
    DutyTimeWindow, DutyEscalationLog,
    DUTY_ESCALATION_STATUS_PENDING, DUTY_ESCALATION_STATUS_ESCALATED,
    DUTY_ESCALATION_STATUS_RESOLVED, DUTY_ESCALATION_STATUS_CLOSED,
    _generate_duty_id,
)
from .duty import DutyError, DutyManager


@dataclass
class DutyEscalationMatchResult:
    """升级命中结果"""
    success: bool
    event_id: str
    team_id: str
    schedule: Optional[DutySchedule] = None
    member: Optional[DutyMember] = None
    escalation_level: Optional[DutyEscalationLevel] = None
    time_window: Optional[DutyTimeWindow] = None
    message: str = ""
    log_id: Optional[str] = None

    def formatted(self) -> str:
        if not self.success:
            return f"升级命中失败 [{self.event_id}]: {self.message}"

        lines = [
            f"===== 升级命中结果 [{self.event_id}] =====",
            f"状态: 成功",
            f"日志ID: {self.log_id}",
        ]

        if self.time_window:
            lines.append(
                f"时间窗口: {self.time_window.name} "
                f"({self.time_window.start_time}-{self.time_window.end_time}) "
                f"优先级: {self.time_window.priority}"
            )

        if self.escalation_level:
            lines.append(
                f"升级层级: L{self.escalation_level.level} "
                f"{self.escalation_level.name}"
            )
            lines.append(
                f"响应时限: {self.escalation_level.response_minutes}分钟"
            )
            lines.append(
                f"升级时限: {self.escalation_level.escalation_minutes}分钟"
            )

        if self.schedule and self.member:
            lines.append("")
            lines.append("===== 当前责任人 =====")
            lines.append(f"姓名: {self.member.name}")
            lines.append(f"角色: {self.member.role}")
            if self.member.phone:
                lines.append(f"电话: {self.member.phone}")
            if self.member.email:
                lines.append(f"邮箱: {self.member.email}")
            lines.append(
                f"排班: {self.schedule.schedule_date} "
                f"{self.schedule.start_time}-{self.schedule.end_time} "
                f"({self.schedule.shift_type})"
            )
            if self.schedule.note:
                lines.append(f"备注: {self.schedule.note}")

        return "\n".join(lines)


@dataclass
class DutyEscalationLogResult:
    """升级日志查询结果"""
    log: DutyEscalationLog
    schedule: Optional[DutySchedule] = None
    member: Optional[DutyMember] = None
    escalation_level: Optional[DutyEscalationLevel] = None

    def formatted(self) -> str:
        status_map = {
            DUTY_ESCALATION_STATUS_PENDING: "待处理",
            DUTY_ESCALATION_STATUS_ESCALATED: "已升级",
            DUTY_ESCALATION_STATUS_RESOLVED: "已解决",
            DUTY_ESCALATION_STATUS_CLOSED: "已关闭",
        }
        status_label = status_map.get(self.log.status, self.log.status)

        lines = [
            f"===== 升级日志 [{self.log.id}] =====",
            f"事件ID: {self.log.event_id}",
            f"事件标题: {self.log.event_title}",
            f"状态: {status_label}",
            f"发生时间: {self.log.event_time}",
            f"命中时间: {self.log.hit_time}",
        ]

        if self.escalation_level:
            lines.append(
                f"升级层级: L{self.escalation_level.level} {self.escalation_level.name}"
            )

        if self.member:
            lines.append(f"责任人员: {self.member.name} ({self.member.role})")
            if self.member.phone:
                lines.append(f"联系电话: {self.member.phone}")

        if self.log.handover_note:
            lines.append(f"交接备注: {self.log.handover_note}")

        if self.log.acknowledged_at:
            lines.append(f"确认时间: {self.log.acknowledged_at}")
        if self.log.resolved_at:
            lines.append(f"解决时间: {self.log.resolved_at}")

        if self.log.escalated_to:
            lines.append(f"已升级至: {self.log.escalated_to}")

        return "\n".join(lines)


@dataclass
class DutyEscalationBatchResult:
    """批量升级处理结果"""
    total: int = 0
    matched: int = 0
    failed: int = 0
    matches: list[DutyEscalationMatchResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def formatted(self) -> str:
        lines = [
            f"===== 批量升级处理结果 =====",
            f"总计: {self.total} 条",
            f"成功: {self.matched} 条",
            f"失败: {self.failed} 条",
        ]

        if self.matches:
            lines.append("")
            lines.append("成功匹配:")
            for m in self.matches[:10]:
                status = "✓" if m.success else "✗"
                info = f"{m.event_id} -> {m.member.name}" if m.member else m.event_id
                lines.append(f"  {status} {info}")
            if len(self.matches) > 10:
                lines.append(f"  ... 还有 {len(self.matches) - 10} 条")

        if self.errors:
            lines.append("")
            lines.append("错误信息:")
            for e in self.errors[:10]:
                lines.append(f"  ✗ {e}")
            if len(self.errors) > 10:
                lines.append(f"  ... 还有 {len(self.errors) - 10} 条")

        return "\n".join(lines)


class DutyEscalationEngine:
    """值班升级匹配引擎"""

    def __init__(self, db: Database, config: AppConfig, duty_manager: DutyManager):
        self.db = db
        self.config = config
        self.duty_cfg = config.duty
        self.duty_manager = duty_manager

    def _match_time_window(self, team_id: str, check_time: datetime) -> Optional[DutyTimeWindow]:
        """匹配时间窗口

        Args:
            team_id: 班组ID
            check_time: 检查时间

        Returns:
            匹配到的时间窗口，None表示匹配默认窗口
        """
        windows = self.db.get_duty_time_windows(team_id)
        if not windows:
            return None

        check_time_str = check_time.strftime("%H:%M")
        check_weekday = str(check_time.weekday())

        matching_windows = []
        for w in windows:
            if w.start_time <= check_time_str < w.end_time:
                if not w.days_of_week or check_weekday in w.days_of_week.split(","):
                    matching_windows.append(w)

        if not matching_windows:
            return None

        matching_windows.sort(key=lambda x: x.priority, reverse=True)
        return matching_windows[0]

    def _get_applicable_schedules(
        self,
        team_id: str,
        check_time: datetime,
        min_level: int = 1,
    ) -> list[tuple[DutySchedule, DutyMember, int]]:
        """获取适用的排班列表

        Args:
            team_id: 班组ID
            check_time: 检查时间
            min_level: 最小升级层级

        Returns:
            排班列表，每个元素为 (排班, 人员, 实际层级) 按层级升序排列
        """
        date_str = check_time.strftime("%Y-%m-%d")
        time_str = check_time.strftime("%H:%M")

        schedules = self.db.get_duty_schedules_by_date(team_id, date_str)

        applicable = []
        members_cache = {}

        for s in schedules:
            if s.start_time <= time_str < s.end_time:
                actual_level = max(s.escalation_level, min_level)
                if actual_level > self.duty_cfg.max_escalation_levels:
                    continue

                if s.member_id not in members_cache:
                    member = self.db.get_duty_member(s.member_id)
                    if member:
                        members_cache[s.member_id] = member
                    else:
                        continue

                member = members_cache[s.member_id]
                applicable.append((s, member, actual_level))

        applicable.sort(key=lambda x: x[2])
        return applicable

    def match_event(
        self,
        team_id: str,
        event_id: str,
        event_title: str,
        event_time: Optional[str] = None,
        min_level: int = 1,
        handover_note: str = "",
    ) -> DutyEscalationMatchResult:
        """根据事件匹配责任人

        Args:
            team_id: 班组ID
            event_id: 事件ID
            event_title: 事件标题
            event_time: 事件发生时间（None表示当前时间）
            min_level: 最小升级层级
            handover_note: 交接备注

        Returns:
            DutyEscalationMatchResult
        """
        try:
            self.duty_manager._validate_team(team_id)
        except DutyError as e:
            return DutyEscalationMatchResult(
                success=False,
                event_id=event_id,
                team_id=team_id,
                message=str(e),
            )

        if event_time:
            try:
                check_time = datetime.strptime(event_time, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return DutyEscalationMatchResult(
                    success=False,
                    event_id=event_id,
                    team_id=team_id,
                    message=f"事件时间格式错误: {event_time}，应为 YYYY-MM-DD HH:MM:SS",
                )
        else:
            check_time = datetime.now()

        time_window = self._match_time_window(team_id, check_time)
        applicable = self._get_applicable_schedules(team_id, check_time, min_level)

        if not applicable:
            return DutyEscalationMatchResult(
                success=False,
                event_id=event_id,
                team_id=team_id,
                time_window=time_window,
                message=f"在 {check_time.strftime('%Y-%m-%d %H:%M:%S')} 没有可用的值班人员",
            )

        schedule, member, actual_level = applicable[0]

        escalation_level = None
        levels = self.db.get_duty_escalation_levels(team_id)
        for lvl in levels:
            if lvl.level == actual_level:
                escalation_level = lvl
                break

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_id = _generate_duty_id("LOG-")

        log = DutyEscalationLog(
            id=log_id,
            event_id=event_id,
            event_title=event_title,
            team_id=team_id,
            schedule_id=schedule.id,
            member_id=member.id,
            escalation_level=actual_level,
            event_time=event_time or now,
            hit_time=now,
            status=DUTY_ESCALATION_STATUS_PENDING,
            handover_note=handover_note.strip(),
            acknowledged_at=None,
            resolved_at=None,
            escalated_to=None,
            created_at=now,
            updated_at=now,
        )

        self.db.insert_duty_escalation_log(log)

        return DutyEscalationMatchResult(
            success=True,
            event_id=event_id,
            team_id=team_id,
            schedule=schedule,
            member=member,
            escalation_level=escalation_level,
            time_window=time_window,
            message="匹配成功",
            log_id=log_id,
        )

    def batch_match_events(
        self,
        team_id: str,
        events: list[dict[str, Any]],
    ) -> DutyEscalationBatchResult:
        """批量匹配事件

        Args:
            team_id: 班组ID
            events: 事件列表，每个元素包含 event_id, event_title, event_time(可选), min_level(可选)

        Returns:
            DutyEscalationBatchResult
        """
        result = DutyEscalationBatchResult(total=len(events))

        for event in events:
            try:
                match = self.match_event(
                    team_id=team_id,
                    event_id=event["event_id"],
                    event_title=event.get("event_title", ""),
                    event_time=event.get("event_time"),
                    min_level=event.get("min_level", 1),
                    handover_note=event.get("handover_note", ""),
                )
                if match.success:
                    result.matched += 1
                    result.matches.append(match)
                else:
                    result.failed += 1
                    result.errors.append(f"{event['event_id']}: {match.message}")
            except Exception as e:
                result.failed += 1
                result.errors.append(f"{event.get('event_id', 'unknown')}: {str(e)}")

        return result

    def get_escalation_log(self, log_id: str) -> DutyEscalationLogResult:
        """获取单条升级命中日志详情

        Args:
            log_id: 日志ID

        Returns:
            DutyEscalationLogResult

        Raises:
            DutyError: 日志不存在
        """
        log = self.db.get_duty_escalation_log(log_id)
        if log is None:
            raise DutyError(f"升级日志不存在: {log_id}")

        schedule = None
        member = None
        escalation_level = None

        if log.schedule_id:
            schedule = self.db.get_duty_schedule(log.schedule_id)
        if log.member_id:
            member = self.db.get_duty_member(log.member_id)

        levels = self.db.get_duty_escalation_levels(log.team_id)
        for lvl in levels:
            if lvl.level == log.escalation_level:
                escalation_level = lvl
                break

        return DutyEscalationLogResult(
            log=log,
            schedule=schedule,
            member=member,
            escalation_level=escalation_level,
        )

    def list_escalation_logs(
        self,
        team_id: str | None = None,
        status: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        limit: int = 50,
    ) -> list[DutyEscalationLogResult]:
        """按筛选条件列出升级日志

        Args:
            team_id: 班组ID（可选）
            status: 状态（可选）
            date_from: 开始日期（可选）
            date_to: 结束日期（可选）
            limit: 返回数量限制

        Returns:
            日志结果列表
        """
        team_ids = [team_id] if team_id else None
        statuses = [status] if status else None
        logs = self.db.filter_duty_escalation_logs(
            team_ids=team_ids,
            statuses=statuses,
            time_from=date_from,
            time_to=date_to,
        )
        if limit and len(logs) > limit:
            logs = logs[:limit]

        result = []
        for log in logs:
            schedule = None
            member = None
            escalation_level = None

            if log.schedule_id:
                schedule = self.db.get_duty_schedule(log.schedule_id)
            if log.member_id:
                member = self.db.get_duty_member(log.member_id)

            levels = self.db.get_duty_escalation_levels(log.team_id)
            for lvl in levels:
                if lvl.level == log.escalation_level:
                    escalation_level = lvl
                    break

            result.append(
                DutyEscalationLogResult(
                    log=log,
                    schedule=schedule,
                    member=member,
                    escalation_level=escalation_level,
                )
            )

        return result

    def list_escalation_logs_formatted(
        self,
        team_id: str | None = None,
        status: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        limit: int = 50,
    ) -> str:
        """格式化列出升级日志"""
        logs = self.list_escalation_logs(team_id, status, date_from, date_to, limit)

        if not logs:
            return "暂无升级命中日志。"

        status_map = {
            DUTY_ESCALATION_STATUS_PENDING: "待处理",
            DUTY_ESCALATION_STATUS_ESCALATED: "已升级",
            DUTY_ESCALATION_STATUS_RESOLVED: "已解决",
            DUTY_ESCALATION_STATUS_CLOSED: "已关闭",
        }

        lines = [f"共 {len(logs)} 条升级命中日志:"]
        lines.append("")

        header = (
            f"{'日志ID':<20} {'事件ID':<16} {'状态':<8} {'责任人':<10} "
            f"{'层级':<6} {'命中时间':<20}"
        )
        lines.append(header)
        lines.append("-" * len(header))

        for r in logs:
            status_label = status_map.get(r.log.status, r.log.status)
            member_name = r.member.name if r.member else "(未知)"
            level_label = f"L{r.log.escalation_level}"
            lines.append(
                f"{r.log.id:<20} {r.log.event_id:<16} {status_label:<8} "
                f"{member_name:<10} {level_label:<6} {r.log.hit_time:<20}"
            )

        return "\n".join(lines)

    def acknowledge_log(self, log_id: str, operator: str) -> bool:
        """确认升级日志

        Args:
            log_id: 日志ID
            operator: 操作人

        Returns:
            是否成功
        """
        log = self.db.get_duty_escalation_log(log_id)
        if log is None:
            raise DutyError(f"升级日志不存在: {log_id}")

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log.acknowledged_at = now
        log.status = DUTY_ESCALATION_STATUS_ESCALATED
        log.updated_at = now

        if log.handover_note:
            log.handover_note = f"{log.handover_note}; 确认人: {operator}"
        else:
            log.handover_note = f"确认人: {operator}"

        self.db.update_duty_escalation_log(log)
        return True

    def resolve_log(self, log_id: str, operator: str, resolution_note: str = "") -> bool:
        """解决升级日志

        Args:
            log_id: 日志ID
            operator: 操作人
            resolution_note: 解决备注

        Returns:
            是否成功
        """
        log = self.db.get_duty_escalation_log(log_id)
        if log is None:
            raise DutyError(f"升级日志不存在: {log_id}")

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log.resolved_at = now
        log.status = DUTY_ESCALATION_STATUS_RESOLVED
        log.updated_at = now

        if log.handover_note:
            if resolution_note:
                log.handover_note = f"{log.handover_note}; 解决人: {operator}; {resolution_note}"
            else:
                log.handover_note = f"{log.handover_note}; 解决人: {operator}"
        else:
            if resolution_note:
                log.handover_note = f"解决人: {operator}; {resolution_note}"
            else:
                log.handover_note = f"解决人: {operator}"

        self.db.update_duty_escalation_log(log)
        return True

    def escalate_log(self, log_id: str, operator: str, escalated_to: str) -> bool:
        """升级日志到更高层级

        Args:
            log_id: 日志ID
            operator: 操作人
            escalated_to: 升级目标

        Returns:
            是否成功
        """
        log = self.db.get_duty_escalation_log(log_id)
        if log is None:
            raise DutyError(f"升级日志不存在: {log_id}")

        if log.escalation_level >= self.duty_cfg.max_escalation_levels:
            raise DutyError(
                f"已达到最大升级层级 {self.duty_cfg.max_escalation_levels}"
            )

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log.escalation_level += 1
        log.escalated_to = escalated_to
        log.status = DUTY_ESCALATION_STATUS_ESCALATED
        log.updated_at = now

        if log.handover_note:
            log.handover_note = (
                f"{log.handover_note}; 升级操作人: {operator}; 升级至: {escalated_to}"
            )
        else:
            log.handover_note = f"升级操作人: {operator}; 升级至: {escalated_to}"

        self.db.update_duty_escalation_log(log)
        return True
