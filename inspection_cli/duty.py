"""值班排班管理：班组、值班人、排班、冲突检测"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional

from .config import AppConfig, DutyConfig
from .database import (
    Database, DutyTeam, DutyMember, DutySchedule,
    DutyEscalationLevel, DutyTimeWindow, DutyHandover, DutyEscalationLog,
    VALID_DUTY_ROLES, VALID_DUTY_SHIFTS, DUTY_SHIFT_TIME_RANGES,
    DUTY_HANDOVER_STATUS_ACTIVE, DUTY_HANDOVER_STATUS_REVOKED,
    DUTY_ESCALATION_STATUS_PENDING,
    _generate_duty_id,
)


class DutyError(Exception):
    """值班排班操作错误"""
    pass


class DutyConflictError(DutyError):
    """值班排班冲突错误"""
    pass


class DutyPermissionError(DutyError):
    """值班排班权限错误"""
    pass


@dataclass
class DutyScheduleResult:
    """排班操作结果"""
    schedule: DutySchedule
    is_new: bool = False
    conflicts: list[DutySchedule] = field(default_factory=list)

    def formatted(self) -> str:
        from .database import DUTY_SHIFT_TIME_RANGES
        shift_label = DUTY_SHIFT_TIME_RANGES.get(
            self.schedule.shift_type,
            (self.schedule.start_time, self.schedule.end_time)
        )
        lines = [
            f"排班{'创建' if self.is_new else '更新'}成功: {self.schedule.id}",
            f"  日期: {self.schedule.schedule_date}",
            f"  班次: {self.schedule.shift_type} ({self.schedule.start_time}-{self.schedule.end_time})",
            f"  升级层级: {self.schedule.escalation_level}",
        ]
        if self.schedule.note:
            lines.append(f"  备注: {self.schedule.note}")
        if self.conflicts:
            lines.append(f"  冲突排班: {len(self.conflicts)} 个")
            for c in self.conflicts[:3]:
                lines.append(f"    - {c.schedule_date} {c.start_time}-{c.end_time}")
        return "\n".join(lines)


@dataclass
class DutyTodayResult:
    """当天值班查询结果"""
    team_id: str
    team_name: str
    schedule_date: str
    schedules: list[DutySchedule]
    members: dict[str, DutyMember] = field(default_factory=dict)
    current_duty: Optional[DutySchedule] = None
    current_member: Optional[DutyMember] = None

    def formatted(self) -> str:
        if not self.schedules:
            return f"{self.team_name} ({self.team_id}) {self.schedule_date} 无排班"

        from .database import DUTY_SHIFT_TIME_RANGES
        lines = [
            f"===== {self.team_name} {self.schedule_date} 值班表 =====",
            f"共 {len(self.schedules)} 个排班:",
            "",
        ]

        header = (
            f"{'班次':<12} {'时间':<20} {'值班人':<16} {'角色':<10} {'层级':<6}"
        )
        lines.append(header)
        lines.append("-" * len(header))

        for s in self.schedules:
            member = self.members.get(s.member_id)
            member_name = member.name if member else "(未知)"
            member_role = member.role if member else "-"
            time_range = f"{s.start_time}-{s.end_time}"
            lines.append(
                f"{s.shift_type:<12} {time_range:<20} {member_name:<16} {member_role:<10} {s.escalation_level:<6}"
            )

        if self.current_duty and self.current_member:
            lines.append("")
            lines.append(f"当前值班 ({datetime.now().strftime('%H:%M:%S')}):")
            lines.append(
                f"  {self.current_member.name} ({self.current_member.role}) "
                f"- {self.current_duty.shift_type} {self.current_duty.start_time}-{self.current_duty.end_time}"
            )

        return "\n".join(lines)


@dataclass
class DutyTeamResult:
    """班组操作结果"""
    team: DutyTeam
    is_new: bool = False

    def formatted(self) -> str:
        lines = [
            f"班组{'创建' if self.is_new else '更新'}成功: {self.team.id}",
            f"  名称: {self.team.name}",
        ]
        if self.team.description:
            lines.append(f"  描述: {self.team.description}")
        return "\n".join(lines)


@dataclass
class DutyMemberResult:
    """人员操作结果"""
    member: DutyMember
    is_new: bool = False

    def formatted(self) -> str:
        from .config import DutyConfig
        role_labels = DutyConfig().role_labels()
        lines = [
            f"人员{'创建' if self.is_new else '更新'}成功: {self.member.id}",
            f"  姓名: {self.member.name}",
            f"  角色: {role_labels.get(self.member.role, self.member.role)}",
        ]
        if self.member.phone:
            lines.append(f"  电话: {self.member.phone}")
        if self.member.email:
            lines.append(f"  邮箱: {self.member.email}")
        return "\n".join(lines)


class DutyManager:
    """值班排班管理器"""

    def __init__(self, db: Database, config: AppConfig):
        self.db = db
        self.config = config
        self.duty_cfg: DutyConfig = config.duty

    def _get_valid_roles(self) -> list[str]:
        """获取有效的角色列表"""
        if self.duty_cfg.valid_roles:
            return self.duty_cfg.valid_roles
        return list(VALID_DUTY_ROLES)

    def _get_valid_shifts(self) -> list[str]:
        """获取有效的班次列表"""
        if self.duty_cfg.valid_shifts:
            return self.duty_cfg.valid_shifts
        return list(VALID_DUTY_SHIFTS)

    def _validate_role(self, role: str) -> None:
        """验证角色是否有效"""
        valid = self._get_valid_roles()
        if role not in valid:
            raise DutyError(
                f"无效的角色: {role}，允许值: {', '.join(valid)}"
            )

    def _validate_shift(self, shift_type: str) -> None:
        """验证班次是否有效"""
        valid = self._get_valid_shifts()
        if shift_type not in valid:
            raise DutyError(
                f"无效的班次: {shift_type}，允许值: {', '.join(valid)}"
            )

    def _validate_team(self, team_id: str) -> None:
        """验证班组是否存在且在可用列表中"""
        if not self.db.duty_team_exists(team_id):
            raise DutyError(f"班组不存在: {team_id}")

        team = self.db.get_duty_team(team_id)
        if team and self.duty_cfg.available_teams:
            if team.name not in self.duty_cfg.available_teams:
                raise DutyError(
                    f"班组 '{team.name}' 不在可用班组列表中，"
                    f"允许值: {', '.join(self.duty_cfg.available_teams)}"
                )

    def _validate_escalation_level(self, team_id: str, level: int) -> None:
        """验证升级层级是否有效"""
        if level < 1 or level > self.duty_cfg.max_escalation_levels:
            raise DutyError(
                f"无效的升级层级: {level}，允许范围: 1-{self.duty_cfg.max_escalation_levels}"
            )

    def _validate_time_format(self, time_str: str) -> None:
        """验证时间格式 (HH:MM)"""
        try:
            datetime.strptime(time_str, "%H:%M")
        except ValueError:
            raise DutyError(f"时间格式错误: {time_str}，应为 HH:MM 格式")

    def _validate_date_format(self, date_str: str) -> None:
        """验证日期格式 (YYYY-MM-DD)"""
        try:
            datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            raise DutyError(f"日期格式错误: {date_str}，应为 YYYY-MM-DD 格式")

    def _get_shift_time_range(self, shift_type: str) -> tuple[str, str]:
        """获取班次的时间范围"""
        if shift_type in DUTY_SHIFT_TIME_RANGES:
            return DUTY_SHIFT_TIME_RANGES[shift_type]
        raise DutyError(f"未知班次类型: {shift_type}")

    # ============ Team 操作 ============

    def create_team(self, name: str, description: str = "") -> DutyTeamResult:
        """创建班组

        Args:
            name: 班组名称
            description: 班组描述

        Returns:
            DutyTeamResult

        Raises:
            DutyError: 参数错误
            DutyConflictError: 班组名称已存在
        """
        if not name or not name.strip():
            raise DutyError("班组名称不能为空")

        if self.duty_cfg.available_teams and name not in self.duty_cfg.available_teams:
            raise DutyError(
                f"班组名称 '{name}' 不在可用班组列表中，"
                f"允许值: {', '.join(self.duty_cfg.available_teams)}"
            )

        if self.db.duty_team_name_exists(name.strip()):
            raise DutyConflictError(f"班组名称已存在: {name}")

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        team_id = _generate_duty_id("TEAM-")

        team = DutyTeam(
            id=team_id,
            name=name.strip(),
            description=description.strip(),
            created_at=now,
            updated_at=now,
        )

        self.db.insert_duty_team(team)

        return DutyTeamResult(team=team, is_new=True)

    def update_team(self, team_id: str, name: str | None = None,
                    description: str | None = None) -> DutyTeamResult:
        """更新班组

        Args:
            team_id: 班组ID
            name: 新名称（None表示不修改）
            description: 新描述（None表示不修改）

        Returns:
            DutyTeamResult

        Raises:
            DutyError: 班组不存在或参数错误
            DutyConflictError: 班组名称已存在
        """
        team = self.db.get_duty_team(team_id)
        if team is None:
            raise DutyError(f"班组不存在: {team_id}")

        if name is not None:
            name = name.strip()
            if not name:
                raise DutyError("班组名称不能为空")
            if self.duty_cfg.available_teams and name not in self.duty_cfg.available_teams:
                raise DutyError(
                    f"班组名称 '{name}' 不在可用班组列表中，"
                    f"允许值: {', '.join(self.duty_cfg.available_teams)}"
                )
            if name != team.name and self.db.duty_team_name_exists(name):
                raise DutyConflictError(f"班组名称已存在: {name}")
            team.name = name

        if description is not None:
            team.description = description.strip()

        team.updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.db.update_duty_team(team)

        return DutyTeamResult(team=team, is_new=False)

    def get_team(self, team_id: str) -> DutyTeam:
        """获取班组

        Args:
            team_id: 班组ID

        Returns:
            DutyTeam

        Raises:
            DutyError: 班组不存在
        """
        team = self.db.get_duty_team(team_id)
        if team is None:
            raise DutyError(f"班组不存在: {team_id}")
        return team

    def list_teams(self) -> list[DutyTeam]:
        """列出所有班组

        Returns:
            班组列表
        """
        return self.db.get_all_duty_teams()

    def list_teams_formatted(self) -> str:
        """格式化列出所有班组"""
        teams = self.list_teams()
        if not teams:
            return "暂无班组。"

        lines = [f"共 {len(teams)} 个班组:"]
        lines.append("")

        header = f"{'班组ID':<20} {'名称':<16} {'描述':<30} {'创建时间':<20}"
        lines.append(header)
        lines.append("-" * len(header))

        for t in teams:
            desc = t.description[:28] + ".." if len(t.description) > 30 else t.description or "-"
            lines.append(
                f"{t.id:<20} {t.name:<16} {desc:<30} {t.created_at:<20}"
            )

        return "\n".join(lines)

    # ============ Member 操作 ============

    def add_member(self, team_id: str, name: str, role: str,
                   phone: str = "", email: str = "") -> DutyMemberResult:
        """添加值班人员

        Args:
            team_id: 班组ID
            name: 姓名
            role: 角色
            phone: 电话
            email: 邮箱

        Returns:
            DutyMemberResult

        Raises:
            DutyError: 参数错误
            DutyConflictError: 人员已在班组中
        """
        self._validate_team(team_id)

        if not name or not name.strip():
            raise DutyError("人员姓名不能为空")

        self._validate_role(role)

        existing = self.db.get_duty_member_by_name(team_id, name.strip())
        if existing is not None:
            raise DutyConflictError(f"人员 '{name}' 已在班组中")

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        member_id = _generate_duty_id("MEM-")

        member = DutyMember(
            id=member_id,
            team_id=team_id,
            name=name.strip(),
            role=role.strip(),
            phone=phone.strip(),
            email=email.strip(),
            created_at=now,
            updated_at=now,
        )

        self.db.insert_duty_member(member)

        return DutyMemberResult(member=member, is_new=True)

    def update_member(self, member_id: str, team_id: str | None = None,
                      name: str | None = None, role: str | None = None,
                      phone: str | None = None, email: str | None = None) -> DutyMemberResult:
        """更新值班人员

        Args:
            member_id: 人员ID
            team_id: 新班组ID（None表示不修改）
            name: 新姓名（None表示不修改）
            role: 新角色（None表示不修改）
            phone: 新电话（None表示不修改）
            email: 新邮箱（None表示不修改）

        Returns:
            DutyMemberResult

        Raises:
            DutyError: 人员不存在或参数错误
        """
        member = self.db.get_duty_member(member_id)
        if member is None:
            raise DutyError(f"人员不存在: {member_id}")

        if team_id is not None:
            self._validate_team(team_id)
            member.team_id = team_id

        if name is not None:
            name = name.strip()
            if not name:
                raise DutyError("人员姓名不能为空")
            if name != member.name:
                existing = self.db.get_duty_member_by_name(member.team_id, name)
                if existing is not None and existing.id != member_id:
                    raise DutyConflictError(f"人员 '{name}' 已在班组中")
            member.name = name

        if role is not None:
            self._validate_role(role)
            member.role = role.strip()

        if phone is not None:
            member.phone = phone.strip()

        if email is not None:
            member.email = email.strip()

        member.updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.db.update_duty_member(member)

        return DutyMemberResult(member=member, is_new=False)

    def get_member(self, member_id: str) -> DutyMember:
        """获取值班人员

        Args:
            member_id: 人员ID

        Returns:
            DutyMember

        Raises:
            DutyError: 人员不存在
        """
        member = self.db.get_duty_member(member_id)
        if member is None:
            raise DutyError(f"人员不存在: {member_id}")
        return member

    def list_members(self, team_id: str) -> list[DutyMember]:
        """列出班组的所有成员

        Args:
            team_id: 班组ID

        Returns:
            成员列表
        """
        self._validate_team(team_id)
        return self.db.get_duty_members_by_team(team_id)

    def list_members_formatted(self, team_id: str) -> str:
        """格式化列出班组的所有成员"""
        from .config import DutyConfig
        role_labels = DutyConfig().role_labels()
        members = self.list_members(team_id)

        team = self.db.get_duty_team(team_id)
        team_name = team.name if team else team_id

        if not members:
            return f"班组 {team_name} 暂无成员。"

        lines = [f"班组 {team_name} 共 {len(members)} 名成员:"]
        lines.append("")

        header = f"{'人员ID':<20} {'姓名':<12} {'角色':<10} {'电话':<16} {'邮箱':<24}"
        lines.append(header)
        lines.append("-" * len(header))

        for m in members:
            role = role_labels.get(m.role, m.role)
            phone = m.phone or "-"
            email = m.email or "-"
            lines.append(
                f"{m.id:<20} {m.name:<12} {role:<10} {phone:<16} {email:<24}"
            )

        return "\n".join(lines)

    # ============ Schedule 操作 ============

    def add_or_update_schedule(
        self,
        team_id: str,
        member_name: str,
        schedule_date: str,
        shift_type: str,
        start_time: str | None = None,
        end_time: str | None = None,
        escalation_level: int = 1,
        note: str = "",
        overwrite: bool = False,
    ) -> DutyScheduleResult:
        """新增或修改排班

        Args:
            team_id: 班组ID
            member_name: 值班人姓名
            schedule_date: 排班日期 (YYYY-MM-DD)
            shift_type: 班次类型
            start_time: 开始时间 (HH:MM)，custom班次必填
            end_time: 结束时间 (HH:MM)，custom班次必填
            escalation_level: 升级层级
            note: 备注
            overwrite: 是否覆盖已有冲突排班

        Returns:
            DutyScheduleResult

        Raises:
            DutyError: 参数错误
            DutyConflictError: 存在时间冲突且overwrite=False
        """
        self._validate_team(team_id)
        self._validate_date_format(schedule_date)
        self._validate_shift(shift_type)
        self._validate_escalation_level(team_id, escalation_level)

        if shift_type == "custom":
            if not start_time or not end_time:
                raise DutyError("自定义班次必须指定开始和结束时间")
            self._validate_time_format(start_time)
            self._validate_time_format(end_time)
        else:
            start_time, end_time = self._get_shift_time_range(shift_type)

        member = self.db.get_duty_member_by_name(team_id, member_name.strip())
        if member is None:
            raise DutyError(f"人员 '{member_name}' 不在班组中")

        conflicts = self.db.find_conflicting_schedules(
            team_id, schedule_date, start_time, end_time
        )

        if conflicts and not overwrite:
            conflict_info = "; ".join([
                f"{c.start_time}-{c.end_time}" for c in conflicts
            ])
            raise DutyConflictError(
                f"同一时段存在冲突排班: {conflict_info}。使用 --overwrite 覆盖。"
            )

        existing_schedule = None
        for c in conflicts:
            if c.member_id == member.id and c.start_time == start_time and c.end_time == end_time:
                existing_schedule = c
                break

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if existing_schedule:
            existing_schedule.escalation_level = escalation_level
            existing_schedule.note = note
            existing_schedule.updated_at = now
            self.db.update_duty_schedule(existing_schedule)
            return DutyScheduleResult(
                schedule=existing_schedule,
                is_new=False,
                conflicts=conflicts if overwrite else [],
            )

        if overwrite:
            for c in conflicts:
                self.db.delete_duty_schedule(c.id)

        schedule_id = _generate_duty_id("SCHED-")
        schedule = DutySchedule(
            id=schedule_id,
            team_id=team_id,
            member_id=member.id,
            shift_type=shift_type,
            schedule_date=schedule_date,
            start_time=start_time,
            end_time=end_time,
            escalation_level=escalation_level,
            note=note.strip(),
            created_at=now,
            updated_at=now,
        )

        self.db.insert_duty_schedule(schedule)

        return DutyScheduleResult(
            schedule=schedule,
            is_new=True,
            conflicts=conflicts if overwrite else [],
        )

    def get_schedule(self, schedule_id: str) -> DutySchedule:
        """获取排班记录

        Args:
            schedule_id: 排班ID

        Returns:
            DutySchedule

        Raises:
            DutyError: 排班不存在
        """
        schedule = self.db.get_duty_schedule(schedule_id)
        if schedule is None:
            raise DutyError(f"排班不存在: {schedule_id}")
        return schedule

    def get_today_schedule(self, team_id: str) -> DutyTodayResult:
        """获取当天值班安排

        Args:
            team_id: 班组ID

        Returns:
            DutyTodayResult
        """
        self._validate_team(team_id)

        team = self.db.get_duty_team(team_id)
        team_name = team.name if team else team_id
        today = datetime.now().strftime("%Y-%m-%d")

        schedules = self.db.get_duty_schedules_by_date(team_id, today)
        members = {}
        for s in schedules:
            member = self.db.get_duty_member(s.member_id)
            if member:
                members[s.member_id] = member

        current_duty = None
        current_member = None
        now_time = datetime.now().strftime("%H:%M")
        for s in schedules:
            if s.start_time <= now_time < s.end_time:
                current_duty = s
                current_member = members.get(s.member_id)
                break

        return DutyTodayResult(
            team_id=team_id,
            team_name=team_name,
            schedule_date=today,
            schedules=schedules,
            members=members,
            current_duty=current_duty,
            current_member=current_member,
        )

    def list_schedules(self, team_id: str, date_from: str | None = None,
                       date_to: str | None = None) -> list[DutySchedule]:
        """列出排班记录

        Args:
            team_id: 班组ID
            date_from: 开始日期
            date_to: 结束日期

        Returns:
            排班列表
        """
        self._validate_team(team_id)

        if date_from and date_to:
            self._validate_date_format(date_from)
            self._validate_date_format(date_to)
            return self.db.get_duty_schedules_by_date_range(team_id, date_from, date_to)
        elif date_from:
            self._validate_date_format(date_from)
            return self.db.get_duty_schedules_by_date_range(
                team_id, date_from, "9999-12-31"
            )
        else:
            return self.db.get_all_duty_schedules()

    def list_schedules_formatted(self, team_id: str, date_from: str | None = None,
                                 date_to: str | None = None) -> str:
        """格式化列出排班记录"""
        from .config import DutyConfig
        shift_labels = DutyConfig().shift_labels()
        schedules = self.list_schedules(team_id, date_from, date_to)

        team = self.db.get_duty_team(team_id)
        team_name = team.name if team else team_id

        if not schedules:
            return f"班组 {team_name} 暂无排班记录。"

        members = {}
        for s in schedules:
            if s.member_id not in members:
                member = self.db.get_duty_member(s.member_id)
                if member:
                    members[s.member_id] = member

        lines = [f"班组 {team_name} 共 {len(schedules)} 条排班记录:"]
        lines.append("")

        header = (
            f"{'排班ID':<20} {'日期':<12} {'班次':<12} {'时间':<20} "
            f"{'值班人':<12} {'层级':<6} {'备注':<20}"
        )
        lines.append(header)
        lines.append("-" * len(header))

        for s in schedules:
            member = members.get(s.member_id)
            member_name = member.name if member else "(未知)"
            shift_label = shift_labels.get(s.shift_type, s.shift_type).split()[0]
            time_range = f"{s.start_time}-{s.end_time}"
            note = s.note[:18] + ".." if len(s.note) > 20 else s.note or "-"
            lines.append(
                f"{s.id:<20} {s.schedule_date:<12} {shift_label:<12} "
                f"{time_range:<20} {member_name:<12} {s.escalation_level:<6} {note:<20}"
            )

        return "\n".join(lines)

    def delete_schedule(self, schedule_id: str) -> bool:
        """删除排班记录

        Args:
            schedule_id: 排班ID

        Returns:
            是否删除成功

        Raises:
            DutyError: 排班不存在
        """
        if not self.db.duty_schedule_exists(schedule_id):
            raise DutyError(f"排班不存在: {schedule_id}")
        self.db.delete_duty_schedule(schedule_id)
        return True

    # ============ Escalation Levels 操作 ============

    def set_escalation_levels(self, team_id: str, levels: list[dict[str, Any]]) -> list[DutyEscalationLevel]:
        """设置班组的升级层级

        Args:
            team_id: 班组ID
            levels: 层级列表，每个元素包含 level, name, response_minutes, escalation_minutes

        Returns:
            升级层级列表
        """
        self._validate_team(team_id)

        if len(levels) > self.duty_cfg.max_escalation_levels:
            raise DutyError(
                f"升级层级数量超过限制，最多 {self.duty_cfg.max_escalation_levels} 级"
            )

        seen_levels = set()
        for lvl in levels:
            level = lvl.get("level")
            if not isinstance(level, int) or level < 1:
                raise DutyError(f"无效的层级编号: {level}，应为正整数")
            if level in seen_levels:
                raise DutyError(f"层级编号重复: {level}")
            seen_levels.add(level)
            self._validate_escalation_level(team_id, level)

        self.db.delete_duty_escalation_levels(team_id)

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        result = []
        for lvl in sorted(levels, key=lambda x: x["level"]):
            level_id = _generate_duty_id("ESC-")
            level = DutyEscalationLevel(
                id=level_id,
                team_id=team_id,
                level=lvl["level"],
                name=lvl["name"],
                response_minutes=lvl.get("response_minutes", 30),
                escalation_minutes=lvl.get("escalation_minutes", 60),
                created_at=now,
            )
            self.db.insert_duty_escalation_level(level)
            result.append(level)

        return result

    def get_escalation_levels(self, team_id: str) -> list[DutyEscalationLevel]:
        """获取班组的升级层级

        Args:
            team_id: 班组ID

        Returns:
            升级层级列表
        """
        self._validate_team(team_id)
        return self.db.get_duty_escalation_levels(team_id)

    # ============ Time Windows 操作 ============

    def set_time_windows(self, team_id: str, windows: list[dict[str, Any]]) -> list[DutyTimeWindow]:
        """设置班组的时间窗口

        Args:
            team_id: 班组ID
            windows: 时间窗口列表，每个元素包含 name, start_time, end_time, days_of_week, priority

        Returns:
            时间窗口列表
        """
        self._validate_team(team_id)

        self.db.delete_duty_time_windows(team_id)

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        result = []
        for w in windows:
            self._validate_time_format(w["start_time"])
            self._validate_time_format(w["end_time"])

            window_id = _generate_duty_id("WIN-")
            window = DutyTimeWindow(
                id=window_id,
                team_id=team_id,
                name=w["name"],
                start_time=w["start_time"],
                end_time=w["end_time"],
                days_of_week=w.get("days_of_week", ""),
                priority=w.get("priority", 1),
                created_at=now,
            )
            self.db.insert_duty_time_window(window)
            result.append(window)

        return result

    def get_time_windows(self, team_id: str) -> list[DutyTimeWindow]:
        """获取班组的时间窗口

        Args:
            team_id: 班组ID

        Returns:
            时间窗口列表
        """
        self._validate_team(team_id)
        return self.db.get_duty_time_windows(team_id)

    # ============ 权限检查 ============

    def check_handover_permission(self, operator_role: str) -> bool:
        """检查是否有交班权限

        Args:
            operator_role: 操作人角色

        Returns:
            是否有权限
        """
        if not self.duty_cfg.handover_allowed_roles:
            return True
        return operator_role in self.duty_cfg.handover_allowed_roles

    def validate_handover_permission(self, operator_role: str) -> None:
        """验证交班权限，无权限则抛出异常

        Args:
            operator_role: 操作人角色

        Raises:
            DutyPermissionError: 无权限
        """
        if not self.check_handover_permission(operator_role):
            raise DutyPermissionError(
                f"角色 '{operator_role}' 无交班权限，"
                f"允许角色: {', '.join(self.duty_cfg.handover_allowed_roles)}"
            )
