"""值班排班导入导出：CSV/JSON 格式，冲突处理"""
from __future__ import annotations

import csv
import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from .config import AppConfig
from .database import (
    Database, DutySchedule, DutyTeam, DutyMember,
    DUTY_SHIFT_TIME_RANGES,
)
from .duty import DutyError, DutyConflictError, DutyManager


DUTY_IMPORT_CONFLICT_SKIP = "skip"
DUTY_IMPORT_CONFLICT_ABORT = "abort"
DUTY_IMPORT_CONFLICT_FORCE = "force"

VALID_DUTY_IMPORT_CONFLICT_STRATEGIES = {
    DUTY_IMPORT_CONFLICT_SKIP,
    DUTY_IMPORT_CONFLICT_ABORT,
    DUTY_IMPORT_CONFLICT_FORCE,
}


@dataclass
class DutyExportResult:
    """排班导出结果"""
    file_path: str
    schedule_count: int
    format: str
    included_teams: list[str] = field(default_factory=list)

    def formatted(self) -> str:
        lines = [
            f"已导出 {self.schedule_count} 条排班到 {self.file_path} ({self.format.upper()})"
        ]
        if self.included_teams:
            lines.append(f"包含班组: {', '.join(self.included_teams)}")
        return "\n".join(lines)


@dataclass
class DutyImportResult:
    """排班导入结果"""
    file_path: str
    total_count: int = 0
    success_count: int = 0
    skipped_count: int = 0
    conflict_count: int = 0
    error_count: int = 0
    conflict_strategy: str = DUTY_IMPORT_CONFLICT_SKIP
    items: list[dict[str, Any]] = field(default_factory=list)
    conflicts_detail: list[str] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return self.error_count > 0

    def formatted(self) -> str:
        lines = [f"排班导入完成: {self.file_path}"]
        lines.append(f"总计: {self.total_count}")
        lines.append(f"成功: {self.success_count}")
        if self.skipped_count:
            lines.append(f"跳过: {self.skipped_count}")
        if self.conflict_count:
            lines.append(f"冲突: {self.conflict_count}")
        if self.error_count:
            lines.append(f"错误: {self.error_count}")
        lines.append(f"冲突策略: {self.conflict_strategy}")

        if self.conflicts_detail:
            lines.append("")
            lines.append("冲突详情:")
            for c in self.conflicts_detail[:10]:
                lines.append(f"  - {c}")
            if len(self.conflicts_detail) > 10:
                lines.append(f"  ... 还有 {len(self.conflicts_detail) - 10} 条")

        return "\n".join(lines)


class DutyIOManager:
    """值班排班导入导出管理器"""

    def __init__(self, db: Database, config: AppConfig, duty_manager: DutyManager):
        self.db = db
        self.config = config
        self.duty_cfg = config.duty
        self.duty_manager = duty_manager

    def export_schedules(self, output_path: str,
                         team_id: Optional[str] = None,
                         date_from: Optional[str] = None,
                         date_to: Optional[str] = None,
                         fmt: Optional[str] = None,
                         include_members: bool = True,
                         include_teams: bool = True) -> DutyExportResult:
        """导出排班列表

        Args:
            output_path: 输出文件路径
            team_id: 班组ID（None表示全部）
            date_from: 开始日期（None表示不限）
            date_to: 结束日期（None表示不限）
            fmt: 格式 (csv/json)，为 None 时根据后缀推断
            include_members: 是否包含人员信息
            include_teams: 是否包含班组信息

        Returns:
            DutyExportResult
        """
        if fmt is None:
            ext = os.path.splitext(output_path)[1].lower().lstrip(".")
            fmt = ext if ext in ("csv", "json") else "json"

        if fmt not in ("csv", "json"):
            raise DutyError(f"不支持的导出格式: {fmt}")

        schedules = self._collect_schedules(team_id, date_from, date_to)

        included_teams: list[str] = []
        if team_id:
            team = self.db.get_duty_team(team_id)
            if team:
                included_teams.append(team.name)
        else:
            teams = self.db.get_all_duty_teams()
            included_teams = [t.name for t in teams]

        if fmt == "csv":
            self._export_csv(schedules, output_path)
        else:
            self._export_json(schedules, output_path, include_members, include_teams)

        return DutyExportResult(
            file_path=os.path.abspath(output_path),
            schedule_count=len(schedules),
            format=fmt,
            included_teams=included_teams,
        )

    def _collect_schedules(self, team_id: Optional[str],
                           date_from: Optional[str],
                           date_to: Optional[str]) -> list[DutySchedule]:
        """收集排班数据"""
        if team_id:
            if date_from and date_to:
                return self.db.get_duty_schedules_by_date_range(team_id, date_from, date_to)
            else:
                return self.db.get_all_duty_schedules_by_team(team_id)
        else:
            schedules: list[DutySchedule] = []
            teams = self.db.get_all_duty_teams()
            for t in teams:
                if date_from and date_to:
                    scheds = self.db.get_duty_schedules_by_date_range(t.id, date_from, date_to)
                else:
                    scheds = self.db.get_all_duty_schedules_by_team(t.id)
                schedules.extend(scheds)
            return schedules

    def _export_csv(self, schedules: list[DutySchedule], output_path: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(output_path)) or ".", exist_ok=True)

        fieldnames = [
            "team_id", "team_name", "member_name", "member_role",
            "schedule_date", "shift_type", "start_time", "end_time",
            "escalation_level", "note", "schedule_id",
            "created_at", "updated_at",
        ]

        with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for s in schedules:
                writer.writerow(self._schedule_to_csv_row(s))

    def _schedule_to_csv_row(self, schedule: DutySchedule) -> dict[str, Any]:
        team = self.db.get_duty_team(schedule.team_id)
        member = self.db.get_duty_member(schedule.member_id)

        return {
            "team_id": schedule.team_id,
            "team_name": team.name if team else "",
            "member_name": member.name if member else "",
            "member_role": member.role if member else "",
            "schedule_date": schedule.schedule_date,
            "shift_type": schedule.shift_type,
            "start_time": schedule.start_time,
            "end_time": schedule.end_time,
            "escalation_level": schedule.escalation_level,
            "note": schedule.note,
            "schedule_id": schedule.id,
            "created_at": schedule.created_at,
            "updated_at": schedule.updated_at,
        }

    def _export_json(self, schedules: list[DutySchedule], output_path: str,
                     include_members: bool, include_teams: bool) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(output_path)) or ".", exist_ok=True)

        teams_data: list[dict[str, Any]] = []
        members_data: list[dict[str, Any]] = []
        seen_team_ids: set[str] = set()
        seen_member_ids: set[str] = set()

        schedules_data: list[dict[str, Any]] = []
        for s in schedules:
            if include_teams and s.team_id not in seen_team_ids:
                team = self.db.get_duty_team(s.team_id)
                if team:
                    teams_data.append(team.to_dict())
                    seen_team_ids.add(s.team_id)

            if include_members and s.member_id not in seen_member_ids:
                member = self.db.get_duty_member(s.member_id)
                if member:
                    members_data.append(member.to_dict())
                    seen_member_ids.add(s.member_id)

            row = self._schedule_to_csv_row(s)
            row["id"] = s.id
            schedules_data.append(row)

        output: dict[str, Any] = {
            "version": "1.0",
            "export_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "schedule_count": len(schedules_data),
            "schedules": schedules_data,
        }

        if include_teams:
            output["teams"] = teams_data
            output["team_count"] = len(teams_data)

        if include_members:
            output["members"] = members_data
            output["member_count"] = len(members_data)

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False, indent=2)

    def import_schedules(self, file_path: str,
                         conflict_strategy: str = DUTY_IMPORT_CONFLICT_SKIP,
                         auto_create_teams: bool = False,
                         auto_create_members: bool = False,
                         operator: str = "") -> DutyImportResult:
        """从文件导入排班

        Args:
            file_path: 输入文件路径
            conflict_strategy: 冲突处理策略 (skip/abort/force)
            auto_create_teams: 是否自动创建不存在的班组
            auto_create_members: 是否自动创建不存在的人员
            operator: 操作人

        Returns:
            DutyImportResult
        """
        if conflict_strategy not in VALID_DUTY_IMPORT_CONFLICT_STRATEGIES:
            raise DutyError(
                f"无效的冲突策略: {conflict_strategy}，"
                f"允许值: {', '.join(sorted(VALID_DUTY_IMPORT_CONFLICT_STRATEGIES))}"
            )

        if not os.path.exists(file_path):
            raise DutyError(f"文件不存在: {file_path}")

        ext = os.path.splitext(file_path)[1].lower().lstrip(".")
        fmt = ext if ext in ("csv", "json") else "json"

        result = DutyImportResult(
            file_path=os.path.abspath(file_path),
            conflict_strategy=conflict_strategy,
        )

        try:
            if fmt == "csv":
                schedule_dicts = self._parse_csv(file_path)
            else:
                schedule_dicts = self._parse_json(file_path)
        except Exception as e:
            raise DutyError(f"解析文件失败: {e}") from e

        result.total_count = len(schedule_dicts)

        for idx, s_dict in enumerate(schedule_dicts):
            item_result = self._import_single_schedule(
                s_dict, conflict_strategy, auto_create_teams, auto_create_members, operator, idx
            )
            result.items.append(item_result)

            if item_result["status"] == "success":
                result.success_count += 1
            elif item_result["status"] == "skipped":
                result.skipped_count += 1
            elif item_result["status"] == "conflict":
                result.conflict_count += 1
                result.conflicts_detail.append(item_result.get("reason", ""))
                if conflict_strategy == DUTY_IMPORT_CONFLICT_ABORT:
                    break
            elif item_result["status"] == "error":
                result.error_count += 1
                if conflict_strategy == DUTY_IMPORT_CONFLICT_ABORT:
                    break

        return result

    def _parse_csv(self, file_path: str) -> list[dict[str, Any]]:
        """解析 CSV 文件"""
        schedules: list[dict[str, Any]] = []
        with open(file_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                schedules.append(self._normalize_schedule_dict(row))
        return schedules

    def _parse_json(self, file_path: str) -> list[dict[str, Any]]:
        """解析 JSON 文件"""
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, dict):
            if "teams" in data and isinstance(data["teams"], list):
                for t_dict in data["teams"]:
                    self._ensure_team_from_dict(t_dict)

            if "members" in data and isinstance(data["members"], list):
                for m_dict in data["members"]:
                    self._ensure_member_from_dict(m_dict)

            if "schedules" in data:
                schedule_list = data["schedules"]
            elif isinstance(data, dict) and not any(k in data for k in ["schedules", "teams", "members"]):
                schedule_list = [data]
            else:
                raise ValueError("JSON 格式不正确，需要包含 schedules 数组")
        elif isinstance(data, list):
            schedule_list = data
        else:
            raise ValueError("JSON 格式不正确")

        return [self._normalize_schedule_dict(s) for s in schedule_list]

    def _ensure_team_from_dict(self, t_dict: dict[str, Any]) -> None:
        """从字典确保班组存在"""
        name = t_dict.get("name", "").strip()
        if not name:
            return

        existing = self.db.duty_team_name_exists(name)
        if not existing:
            try:
                self.duty_manager.create_team(
                    name=name,
                    description=t_dict.get("description", "")
                )
            except DutyError:
                pass

    def _ensure_member_from_dict(self, m_dict: dict[str, Any]) -> None:
        """从字典确保人员存在"""
        team_id = m_dict.get("team_id", "").strip()
        name = m_dict.get("name", "").strip()
        role = m_dict.get("role", "operator").strip()

        if not team_id or not name:
            return

        if not self.db.duty_team_exists(team_id):
            return

        existing = self.db.get_duty_member_by_name(team_id, name)
        if not existing:
            try:
                self.duty_manager.add_member(
                    team_id=team_id,
                    name=name,
                    role=role,
                    phone=m_dict.get("phone", ""),
                    email=m_dict.get("email", "")
                )
            except DutyError:
                pass

    def _normalize_schedule_dict(self, s_dict: dict[str, Any]) -> dict[str, Any]:
        """标准化排班字典"""
        normalized: dict[str, Any] = {}

        team_keys = ["team_id", "team"]
        for key in team_keys:
            if key in s_dict and s_dict[key]:
                normalized["team_id"] = str(s_dict[key])
                break

        team_name_keys = ["team_name", "班组", "班组名称"]
        for key in team_name_keys:
            if key in s_dict and s_dict[key]:
                normalized["team_name"] = str(s_dict[key])
                break

        member_keys = ["member_name", "姓名", "值班人", "member"]
        for key in member_keys:
            if key in s_dict and s_dict[key]:
                normalized["member_name"] = str(s_dict[key])
                break

        role_keys = ["member_role", "角色", "role"]
        for key in role_keys:
            if key in s_dict and s_dict[key]:
                normalized["member_role"] = str(s_dict[key])
                break

        date_keys = ["schedule_date", "日期", "date"]
        for key in date_keys:
            if key in s_dict and s_dict[key]:
                normalized["schedule_date"] = str(s_dict[key])
                break

        shift_keys = ["shift_type", "班次", "shift"]
        for key in shift_keys:
            if key in s_dict and s_dict[key]:
                normalized["shift_type"] = str(s_dict[key])
                break

        start_keys = ["start_time", "开始时间", "start"]
        for key in start_keys:
            if key in s_dict and s_dict[key]:
                normalized["start_time"] = str(s_dict[key])
                break

        end_keys = ["end_time", "结束时间", "end"]
        for key in end_keys:
            if key in s_dict and s_dict[key]:
                normalized["end_time"] = str(s_dict[key])
                break

        level_keys = ["escalation_level", "层级", "level"]
        for key in level_keys:
            if key in s_dict and s_dict[key]:
                try:
                    normalized["escalation_level"] = int(s_dict[key])
                except (ValueError, TypeError):
                    normalized["escalation_level"] = 1
                break

        note_keys = ["note", "备注", "说明"]
        for key in note_keys:
            if key in s_dict and s_dict[key]:
                normalized["note"] = str(s_dict[key])
                break

        id_keys = ["schedule_id", "id"]
        for key in id_keys:
            if key in s_dict and s_dict[key]:
                normalized["schedule_id"] = str(s_dict[key])
                break

        return normalized

    def _import_single_schedule(self, s_dict: dict[str, Any],
                                conflict_strategy: str,
                                auto_create_teams: bool,
                                auto_create_members: bool,
                                operator: str,
                                index: int) -> dict[str, Any]:
        """导入单个排班"""
        item_result = {
            "index": index,
            "schedule_id": s_dict.get("schedule_id", f"import-{index}"),
            "status": "pending",
            "reason": "",
        }

        try:
            team_id = s_dict.get("team_id", "")
            team_name = s_dict.get("team_name", "")

            team = None
            if team_id:
                try:
                    self.duty_manager._validate_team(team_id)
                    team = self.db.get_duty_team(team_id)
                except DutyError:
                    team = None
                    if team_name:
                        teams = self.db.get_all_duty_teams()
                        for t in teams:
                            if t.name == team_name:
                                team = t
                                team_id = t.id
                                break

            if team is None and team_name:
                teams = self.db.get_all_duty_teams()
                for t in teams:
                    if t.name == team_name:
                        team = t
                        team_id = t.id
                        break

            if team is None:
                if auto_create_teams and team_name:
                    team_result = self.duty_manager.create_team(
                        name=team_name,
                        description="自动创建"
                    )
                    team_id = team_result.team.id
                else:
                    item_result["status"] = "error"
                    if team_name:
                        item_result["reason"] = f"班组 '{team_name}' 不存在，使用 --auto-create-teams 自动创建"
                    else:
                        item_result["reason"] = "缺少班组信息"
                    return item_result

            try:
                self.duty_manager._validate_team(team_id)
            except DutyError as e:
                item_result["status"] = "error"
                item_result["reason"] = f"班组无效: {e}"
                return item_result

            member_name = s_dict.get("member_name", "").strip()
            if not member_name:
                item_result["status"] = "error"
                item_result["reason"] = "缺少值班人姓名"
                return item_result

            member_role = s_dict.get("member_role", "operator").strip()
            member = self.db.get_duty_member_by_name(team_id, member_name)
            if member is None:
                if auto_create_members:
                    member_result = self.duty_manager.add_member(
                        team_id=team_id,
                        name=member_name,
                        role=member_role,
                        phone="",
                        email=""
                    )
                else:
                    item_result["status"] = "error"
                    item_result["reason"] = (
                        f"人员 '{member_name}' 不在班组中，"
                        f"使用 --auto-create-members 自动创建"
                    )
                    return item_result

            schedule_date = s_dict.get("schedule_date", "").strip()
            if not schedule_date:
                item_result["status"] = "error"
                item_result["reason"] = "缺少排班日期"
                return item_result

            shift_type = s_dict.get("shift_type", "custom").strip()
            start_time = s_dict.get("start_time", "").strip()
            end_time = s_dict.get("end_time", "").strip()
            escalation_level = s_dict.get("escalation_level", 1)
            note = s_dict.get("note", "").strip()

            try:
                schedule_result = self.duty_manager.add_or_update_schedule(
                    team_id=team_id,
                    member_name=member_name,
                    schedule_date=schedule_date,
                    shift_type=shift_type,
                    start_time=start_time,
                    end_time=end_time,
                    escalation_level=escalation_level,
                    note=note,
                    overwrite=(conflict_strategy == DUTY_IMPORT_CONFLICT_FORCE),
                )
                item_result["status"] = "success"
                item_result["schedule_id"] = schedule_result.schedule.id
                if schedule_result.conflicts and conflict_strategy == DUTY_IMPORT_CONFLICT_FORCE:
                    item_result["reason"] = f"已覆盖 {len(schedule_result.conflicts)} 个冲突排班"
                elif schedule_result.is_new:
                    item_result["reason"] = "导入成功"
                else:
                    item_result["reason"] = "更新成功"

            except DutyConflictError as e:
                if conflict_strategy == DUTY_IMPORT_CONFLICT_SKIP:
                    item_result["status"] = "conflict"
                    item_result["reason"] = str(e)
                elif conflict_strategy == DUTY_IMPORT_CONFLICT_ABORT:
                    item_result["status"] = "conflict"
                    item_result["reason"] = str(e)
                else:
                    item_result["status"] = "conflict"
                    item_result["reason"] = str(e)

        except Exception as e:
            item_result["status"] = "error"
            item_result["reason"] = str(e)

        return item_result
