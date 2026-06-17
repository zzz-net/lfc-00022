"""值班交接管理：手动交班、撤销交班、交接历史"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from .config import AppConfig
from .database import (
    Database, DutyHandover, DutyMember, DutySchedule,
    DUTY_HANDOVER_STATUS_ACTIVE, DUTY_HANDOVER_STATUS_REVOKED,
    _generate_duty_id,
)
from .duty import DutyError, DutyConflictError, DutyPermissionError, DutyManager


@dataclass
class DutyHandoverResult:
    """交班操作结果"""
    handover: DutyHandover
    from_member: Optional[DutyMember] = None
    to_member: Optional[DutyMember] = None
    is_new: bool = True
    is_revoked: bool = False

    def formatted(self) -> str:
        action = "撤销" if self.is_revoked else "执行"
        lines = [
            f"===== 交班{action}成功 =====",
            f"交班ID: {self.handover.id}",
        ]

        if self.from_member:
            lines.append(
                f"交班人: {self.from_member.name} ({self.from_member.role})"
            )
        else:
            lines.append(f"原值班人ID: {self.handover.from_member_id}")

        if self.to_member:
            lines.append(
                f"接班人: {self.to_member.name} ({self.to_member.role})"
            )
        else:
            lines.append(f"新值班人ID: {self.handover.to_member_id}")

        lines.append(f"交班时间: {self.handover.handed_at}")

        if self.handover.note:
            lines.append(f"备注: {self.handover.note}")

        if self.is_revoked:
            lines.append(f"撤销时间: {self.handover.revoked_at}")

        return "\n".join(lines)


@dataclass
class DutyHandoverHistoryResult:
    """交接历史结果"""
    handover: DutyHandover
    from_member: Optional[DutyMember] = None
    to_member: Optional[DutyMember] = None
    operator_member: Optional[DutyMember] = None

    def formatted(self) -> str:
        status_label = "已撤销" if self.handover.status == DUTY_HANDOVER_STATUS_REVOKED else "生效中"

        lines = [
            f"[{status_label}] {self.handover.handed_at}",
        ]

        from_name = self.from_member.name if self.from_member else self.handover.from_member_id
        to_name = self.to_member.name if self.to_member else self.handover.to_member_id

        lines.append(f"  {from_name} -> {to_name}")

        if self.handover.note:
            lines.append(f"  备注: {self.handover.note}")

        if self.handover.status == DUTY_HANDOVER_STATUS_REVOKED:
            lines.append(f"  撤销于: {self.handover.revoked_at}")

        return "\n".join(lines)


class DutyHandoverManager:
    """值班交接管理器"""

    def __init__(self, db: Database, config: AppConfig, duty_manager: DutyManager):
        self.db = db
        self.config = config
        self.duty_cfg = config.duty
        self.duty_manager = duty_manager

    def _get_rollback_deadline(self, handed_at_str: str) -> Optional[datetime]:
        """计算回滚截止时间

        Args:
            handed_at_str: 交班时间字符串

        Returns:
            回滚截止时间，None表示无限制
        """
        if self.duty_cfg.default_rollback_window_hours <= 0:
            return None

        try:
            handed_at = datetime.strptime(handed_at_str, "%Y-%m-%d %H:%M:%S")
            return handed_at + timedelta(hours=self.duty_cfg.default_rollback_window_hours)
        except ValueError:
            return None

    def _is_within_rollback_window(self, handover: DutyHandover) -> bool:
        """检查是否在回滚窗口内

        Args:
            handover: 交班记录

        Returns:
            是否在回滚窗口内
        """
        deadline = self._get_rollback_deadline(handover.handed_at)
        if deadline is None:
            return True
        return datetime.now() <= deadline

    def _get_current_on_duty_member(self, team_id: str) -> tuple[Optional[DutySchedule], Optional[DutyMember]]:
        """获取当前值班人员

        Args:
            team_id: 班组ID

        Returns:
            (当前排班, 当前值班人员)
        """
        today_result = self.duty_manager.get_today_schedule(team_id)
        return today_result.current_duty, today_result.current_member

    def perform_handover(
        self,
        team_id: str,
        operator_member_name: str,
        to_member_name: str,
        note: str = "",
    ) -> DutyHandoverResult:
        """执行手动交班

        Args:
            team_id: 班组ID
            operator_member_name: 操作人姓名（需要有权限）
            to_member_name: 接班人姓名
            note: 交接备注

        Returns:
            DutyHandoverResult

        Raises:
            DutyError: 参数错误
            DutyPermissionError: 无交班权限
            DutyConflictError: 目标人员不在班组中
        """
        self.duty_manager._validate_team(team_id)

        operator = self.db.get_duty_member_by_name(team_id, operator_member_name.strip())
        if operator is None:
            raise DutyError(f"操作人 '{operator_member_name}' 不在班组中")

        self.duty_manager.validate_handover_permission(operator.role)

        to_member = self.db.get_duty_member_by_name(team_id, to_member_name.strip())
        if to_member is None:
            raise DutyError(f"接班人 '{to_member_name}' 不在班组中")

        current_schedule, current_member = self._get_current_on_duty_member(team_id)

        if current_member is None:
            raise DutyError(f"当前没有在值人员，无法交班")

        if current_member.id == to_member.id:
            raise DutyConflictError(f"接班人 '{to_member_name}' 已是当前值班人")

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        handover_id = _generate_duty_id("HAND-")

        handover = DutyHandover(
            id=handover_id,
            team_id=team_id,
            from_member_id=current_member.id,
            to_member_id=to_member.id,
            operator_member_id=operator.id,
            schedule_id=current_schedule.id if current_schedule else None,
            handed_at=now,
            status=DUTY_HANDOVER_STATUS_ACTIVE,
            note=note.strip(),
            revoked_at=None,
            revoked_by=None,
            created_at=now,
            updated_at=now,
        )

        last_active = self.db.get_last_duty_handover(team_id)
        if last_active and last_active.id != handover.id:
            last_active.status = DUTY_HANDOVER_STATUS_REVOKED
            last_active.revoked_at = now
            last_active.revoked_by = operator.id
            last_active.updated_at = now
            self.db.update_duty_handover(last_active)

        self.db.insert_duty_handover(handover)

        if current_schedule:
            current_schedule.member_id = to_member.id
            current_schedule.updated_at = now
            self.db.update_duty_schedule(current_schedule)

        return DutyHandoverResult(
            handover=handover,
            from_member=current_member,
            to_member=to_member,
            is_new=True,
            is_revoked=False,
        )

    def undo_last_handover(
        self,
        team_id: str,
        operator_member_name: str,
    ) -> DutyHandoverResult:
        """撤销最近一次交班

        Args:
            team_id: 班组ID
            operator_member_name: 操作人姓名（需要有权限）

        Returns:
            DutyHandoverResult

        Raises:
            DutyError: 参数错误或没有可撤销的交班
            DutyPermissionError: 无交班权限
            DutyConflictError: 已撤销或超出回滚窗口
        """
        self.duty_manager._validate_team(team_id)

        operator = self.db.get_duty_member_by_name(team_id, operator_member_name.strip())
        if operator is None:
            raise DutyError(f"操作人 '{operator_member_name}' 不在班组中")

        self.duty_manager.validate_handover_permission(operator.role)

        last_handover = self.db.get_most_recent_handover(team_id)
        if last_handover is None:
            raise DutyError(f"没有可撤销的交班记录")

        if last_handover.status == DUTY_HANDOVER_STATUS_REVOKED:
            raise DutyConflictError(
                f"最近一次交班 {last_handover.id} 已经被撤销，无法再次撤销"
            )

        if not self._is_within_rollback_window(last_handover):
            deadline = self._get_rollback_deadline(last_handover.handed_at)
            deadline_str = deadline.strftime("%Y-%m-%d %H:%M:%S") if deadline else "无限制"
            raise DutyConflictError(
                f"已超出回滚窗口，撤销截止时间为 {deadline_str}，"
                f"当前时间已超过。如需强制回滚请联系管理员。"
            )

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        last_handover.status = DUTY_HANDOVER_STATUS_REVOKED
        last_handover.revoked_at = now
        last_handover.revoked_by = operator.id
        last_handover.updated_at = now

        self.db.update_duty_handover(last_handover)

        if last_handover.schedule_id:
            schedule = self.db.get_duty_schedule(last_handover.schedule_id)
            if schedule:
                schedule.member_id = last_handover.from_member_id
                schedule.updated_at = now
                self.db.update_duty_schedule(schedule)

        from_member = self.db.get_duty_member(last_handover.from_member_id)
        to_member = self.db.get_duty_member(last_handover.to_member_id)

        return DutyHandoverResult(
            handover=last_handover,
            from_member=from_member,
            to_member=to_member,
            is_new=False,
            is_revoked=True,
        )

    def get_handover(self, handover_id: str) -> DutyHandoverHistoryResult:
        """获取交班记录详情

        Args:
            handover_id: 交班ID

        Returns:
            DutyHandoverHistoryResult

        Raises:
            DutyError: 交班记录不存在
        """
        handover = self.db.get_duty_handover(handover_id)
        if handover is None:
            raise DutyError(f"交班记录不存在: {handover_id}")

        from_member = self.db.get_duty_member(handover.from_member_id)
        to_member = self.db.get_duty_member(handover.to_member_id)
        operator_member = None
        if handover.operator_member_id:
            operator_member = self.db.get_duty_member(handover.operator_member_id)

        return DutyHandoverHistoryResult(
            handover=handover,
            from_member=from_member,
            to_member=to_member,
            operator_member=operator_member,
        )

    def list_handover_history(
        self,
        team_id: str,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        limit: int = 50,
    ) -> list[DutyHandoverHistoryResult]:
        """列出交班历史

        Args:
            team_id: 班组ID
            date_from: 开始日期（可选）
            date_to: 结束日期（可选）
            limit: 返回数量限制

        Returns:
            交班历史列表
        """
        self.duty_manager._validate_team(team_id)

        handovers = self.db.get_duty_handovers_by_team(
            team_id, date_from, date_to, limit
        )

        result = []
        for h in handovers:
            from_member = self.db.get_duty_member(h.from_member_id)
            to_member = self.db.get_duty_member(h.to_member_id)
            operator_member = None
            if h.operator_member_id:
                operator_member = self.db.get_duty_member(h.operator_member_id)

            result.append(
                DutyHandoverHistoryResult(
                    handover=h,
                    from_member=from_member,
                    to_member=to_member,
                    operator_member=operator_member,
                )
            )

        return result

    def list_handover_history_formatted(
        self,
        team_id: str,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        limit: int = 50,
    ) -> str:
        """格式化列出交班历史"""
        history = self.list_handover_history(team_id, date_from, date_to, limit)

        if not history:
            return "暂无交接历史记录。"

        team = self.db.get_duty_team(team_id)
        team_name = team.name if team else team_id

        lines = [f"===== {team_name} 交接历史 =====", f"共 {len(history)} 条记录:"]
        lines.append("")

        for h in history:
            lines.append(h.formatted())
            lines.append("")

        return "\n".join(lines)

    def get_effective_on_duty_member(
        self,
        team_id: str,
        check_time: Optional[datetime] = None,
    ) -> tuple[Optional[DutySchedule], Optional[DutyMember], Optional[DutyHandover]]:
        """获取实际生效的值班人员（考虑交班记录）

        Args:
            team_id: 班组ID
            check_time: 检查时间（None表示当前时间）

        Returns:
            (排班, 值班人员, 最后交班记录)
        """
        if check_time is None:
            check_time = datetime.now()

        current_schedule, current_member = self._get_current_on_duty_member(team_id)

        last_handover = self.db.get_last_duty_handover(team_id)
        if (last_handover is not None
                and last_handover.status == DUTY_HANDOVER_STATUS_ACTIVE):
            try:
                handover_time = datetime.strptime(
                    last_handover.handed_at, "%Y-%m-%d %H:%M:%S"
                )
                if handover_time <= check_time:
                    if last_handover.schedule_id == (current_schedule.id if current_schedule else None):
                        to_member = self.db.get_duty_member(last_handover.to_member_id)
                        return current_schedule, to_member, last_handover
            except ValueError:
                pass

        return current_schedule, current_member, last_handover
