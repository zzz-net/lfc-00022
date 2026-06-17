"""值班对账快照管理：生成、查询、差异比对、导出导入、回滚"""
from __future__ import annotations

import csv
import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from .config import AppConfig, SnapshotConfig
from .database import (
    Database, DutySnapshot, DutySnapshotContent, DutySnapshotDiff, DutySnapshotLog,
    DutyTeam, DutyMember, DutySchedule, DutyHandover, DutyEscalationLog,
    DutyEscalationLevel, DutyTimeWindow,
    SNAPSHOT_STATUS_ACTIVE, SNAPSHOT_STATUS_DELETED, SNAPSHOT_STATUS_IMPORTED,
    SNAPSHOT_STATUS_ROLLED_BACK,
    SNAPSHOT_OP_GENERATE, SNAPSHOT_OP_EXPORT, SNAPSHOT_OP_IMPORT,
    SNAPSHOT_OP_ROLLBACK, SNAPSHOT_OP_DELETE, SNAPSHOT_OP_DIFF,
    SNAPSHOT_IMPORT_STATUS_SUCCESS, SNAPSHOT_IMPORT_STATUS_PARTIAL,
    SNAPSHOT_IMPORT_STATUS_FAILED,
    _generate_snapshot_id,
    DUTY_HANDOVER_STATUS_ACTIVE,
)


class SnapshotError(Exception):
    """快照操作错误"""
    pass


class SnapshotConflictError(SnapshotError):
    """快照冲突错误"""
    pass


class SnapshotPermissionError(SnapshotError):
    """快照权限错误"""
    pass


@dataclass
class SnapshotGenerateResult:
    """快照生成结果"""
    snapshot: DutySnapshot
    content: DutySnapshotContent
    member_count: int = 0
    schedule_count: int = 0
    handover_count: int = 0
    escalation_log_count: int = 0

    def formatted(self) -> str:
        lines = [
            f"快照生成成功: {self.snapshot.id}",
            f"  班组: {self.snapshot.team_name} ({self.snapshot.team_id})",
            f"  日期: {self.snapshot.snapshot_date}",
            f"  时点: {self.snapshot.snapshot_point}",
            f"  操作人: {self.snapshot.operator}",
            f"  校验和: {self.snapshot.checksum[:16]}...",
            "",
            f"  成员数: {self.member_count}",
            f"  排班数: {self.schedule_count}",
            f"  交班记录数: {self.handover_count}",
            f"  升级命中日志数: {self.escalation_log_count}",
        ]
        if self.snapshot.note:
            lines.append(f"  备注: {self.snapshot.note}")
        return "\n".join(lines)


@dataclass
class SnapshotDiffResult:
    """快照差异比对结果"""
    diff: DutySnapshotDiff
    summary: dict[str, Any] = field(default_factory=dict)
    detail: dict[str, Any] = field(default_factory=dict)

    def formatted(self) -> str:
        lines = [
            f"快照差异比对: {self.diff.snapshot_a_id} vs {self.diff.snapshot_b_id}",
            f"  差异ID: {self.diff.id}",
            f"  班组: {self.diff.team_id}",
            f"  存在冲突: {'是' if self.diff.has_conflicts else '否'}",
            "",
        ]
        summary = self.summary
        if not summary:
            lines.append("  无差异")
            return "\n".join(lines)

        for section, changes in summary.items():
            if isinstance(changes, dict):
                for key, val in changes.items():
                    if isinstance(val, int):
                        lines.append(f"  {section}.{key}: {val}")
                    elif isinstance(val, list):
                        for item in val:
                            lines.append(f"  {section}.{key}: {item}")
                    else:
                        lines.append(f"  {section}.{key}: {val}")
            elif isinstance(changes, int):
                lines.append(f"  {section}: {changes}")

        if self.diff.has_conflicts:
            lines.append("")
            lines.append("  ⚠ 检测到冲突，请检查差异详情")

        return "\n".join(lines)


@dataclass
class SnapshotExportResult:
    """快照导出结果"""
    file_path: str
    snapshot_count: int
    format: str
    included_teams: list[str] = field(default_factory=list)

    def formatted(self) -> str:
        lines = [
            f"已导出 {self.snapshot_count} 份快照到 {self.file_path} ({self.format.upper()})"
        ]
        if self.included_teams:
            lines.append(f"包含班组: {', '.join(self.included_teams)}")
        return "\n".join(lines)


@dataclass
class SnapshotImportResult:
    """快照导入结果"""
    file_path: str
    total_count: int = 0
    success_count: int = 0
    skipped_count: int = 0
    conflict_count: int = 0
    error_count: int = 0
    items: list[dict[str, Any]] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return self.error_count > 0

    def formatted(self) -> str:
        lines = [f"快照导入完成: {self.file_path}"]
        lines.append(f"总计: {self.total_count}")
        lines.append(f"成功: {self.success_count}")
        if self.skipped_count:
            lines.append(f"跳过: {self.skipped_count}")
        if self.conflict_count:
            lines.append(f"冲突: {self.conflict_count}")
        if self.error_count:
            lines.append(f"错误: {self.error_count}")
        return "\n".join(lines)


@dataclass
class SnapshotRollbackResult:
    """快照回滚结果"""
    snapshot_id: str
    operator: str
    deleted: bool = False

    def formatted(self) -> str:
        if self.deleted:
            return (
                f"已回滚最近一次错误导入快照: {self.snapshot_id}\n"
                f"  操作人: {self.operator}\n"
                f"  状态: 已标记为 {SNAPSHOT_STATUS_ROLLED_BACK}"
            )
        return "没有可回滚的错误导入快照"


class DutySnapshotManager:
    """值班对账快照管理器"""

    def __init__(self, db: Database, config: AppConfig,
                 duty_manager=None, duty_handover_manager=None):
        self.db = db
        self.config = config
        self.snapshot_cfg: SnapshotConfig = config.snapshot
        self.duty_manager = duty_manager
        self.duty_handover_manager = duty_handover_manager

    def _now(self) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _today(self) -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def _compute_checksum(self, content: DutySnapshotContent) -> str:
        raw = json.dumps(content.to_dict(), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _check_permission(self, operator: str, required_roles: list[str]) -> None:
        if not operator:
            raise SnapshotPermissionError("操作人不能为空")
        if not self.duty_manager:
            return
        all_members = []
        for team in self.db.get_all_duty_teams():
            all_members.extend(self.db.get_duty_members_by_team(team.id))
        operator_member = None
        for m in all_members:
            if m.name == operator:
                operator_member = m
                break
        if operator_member is None:
            return
        if operator_member.role not in required_roles:
            raise SnapshotPermissionError(
                f"权限不足！用户 '{operator}' 的角色为 '{operator_member.role}'，"
                f"不允许执行此操作。\n"
                f"提示: 仅以下角色允许: {', '.join(required_roles)}"
            )

    def _check_team_exportable(self, team_name: str) -> None:
        if self.snapshot_cfg.exportable_teams:
            if team_name not in self.snapshot_cfg.exportable_teams:
                raise SnapshotPermissionError(
                    f"班组 '{team_name}' 不在可导出班组列表中。\n"
                    f"提示: 可导出班组: {', '.join(self.snapshot_cfg.exportable_teams)}"
                )

    def _check_handover_conflict(self, team_id: str) -> None:
        if self.snapshot_cfg.allow_generate_after_handover:
            return
        if not self.duty_handover_manager:
            return
        handovers = self.db.get_duty_handovers_by_team(team_id)
        today = self._today()
        for h in handovers:
            if h.status == DUTY_HANDOVER_STATUS_ACTIVE and h.handed_at.startswith(today):
                from_member = self.db.get_duty_member(h.from_member_id)
                to_member = self.db.get_duty_member(h.to_member_id)
                from_name = from_member.name if from_member else h.from_member_id
                to_name = to_member.name if to_member else h.to_member_id
                raise SnapshotConflictError(
                    f"班组今日已有交班记录（{from_name} → {to_name}），"
                    f"不允许在交班后生成快照。\n"
                    f"提示: 在配置中设置 snapshot.allow_generate_after_handover: true "
                    f"可允许交班后生成快照。"
                )

    def _log_operation(self, operation: str, operator: str,
                        team_id: str = "", snapshot_id: str = "",
                        diff_id: str = "", status: str = "",
                        detail: str = "", error_message: str = "",
                        import_file: str = "", export_file: str = "") -> None:
        log = DutySnapshotLog(
            id=_generate_snapshot_id("SLOG-"),
            operation=operation,
            operator=operator,
            team_id=team_id,
            snapshot_id=snapshot_id,
            diff_id=diff_id,
            status=status,
            detail=detail,
            error_message=error_message,
            import_file=import_file,
            export_file=export_file,
            created_at=self._now(),
        )
        self.db.insert_duty_snapshot_log(log)

    def generate_snapshot(self, team_id: str, operator: str,
                           snapshot_point: str = "",
                           snapshot_date: str | None = None,
                           note: str = "") -> SnapshotGenerateResult:
        """生成快照

        Args:
            team_id: 班组ID
            operator: 操作人
            snapshot_point: 快照时点标识（如 "早班前"、"交班后"）
            snapshot_date: 快照日期（默认今天）
            note: 备注

        Returns:
            SnapshotGenerateResult

        Raises:
            SnapshotError: 班组不存在等
            SnapshotConflictError: 重复快照/交班后限制
            SnapshotPermissionError: 权限不足
        """
        self._check_permission(operator, self.snapshot_cfg.allowed_generate_roles)

        team = self.db.get_duty_team(team_id)
        if team is None:
            raise SnapshotError(f"班组不存在: {team_id}")

        self._check_team_exportable(team.name)

        if not snapshot_date:
            snapshot_date = self._today()

        if not snapshot_point:
            snapshot_point = datetime.now().strftime("%H%M")

        if self.db.duty_snapshot_unique_exists(team_id, snapshot_date, snapshot_point):
            raise SnapshotConflictError(
                f"快照已存在：班组 {team.name} 在 {snapshot_date} 时点 {snapshot_point} "
                f"已有活跃快照。\n"
                f"提示: 请使用不同的时点标识，或先删除已有快照。"
            )

        self._check_handover_conflict(team_id)

        members = self.db.get_duty_members_by_team(team_id)
        schedules = self.db.get_duty_schedules_by_date(team_id, snapshot_date)
        if not schedules:
            all_schedules = self.db.get_all_duty_schedules_by_team(team_id)
            schedules = [s for s in all_schedules if s.schedule_date == snapshot_date]

        handovers = self.db.get_duty_handovers_by_team(team_id)
        today_handovers = [h for h in handovers if h.handed_at.startswith(snapshot_date)]

        escalation_logs = self.db.filter_duty_escalation_logs(
            team_ids=[team_id], time_from=snapshot_date, time_to=snapshot_date + " 23:59:59"
        )

        escalation_levels = self.db.get_duty_escalation_levels(team_id)
        time_windows = self.db.get_duty_time_windows(team_id)

        content = DutySnapshotContent(
            snapshot_id="",
            team_info=team.to_dict(),
            members=[m.to_dict() for m in members],
            schedules=[s.to_dict() for s in schedules],
            handovers=[h.to_dict() for h in today_handovers],
            escalation_logs=[e.to_dict() for e in escalation_logs],
            escalation_levels=[e.to_dict() for e in escalation_levels],
            time_windows=[w.to_dict() for w in time_windows],
            meta={
                "generated_at": self._now(),
                "cli_version": "1.0",
            },
        )

        now = self._now()
        snapshot_id = _generate_snapshot_id("SNAP-")
        content.snapshot_id = snapshot_id

        checksum = self._compute_checksum(content)

        snapshot = DutySnapshot(
            id=snapshot_id,
            team_id=team_id,
            team_name=team.name,
            snapshot_date=snapshot_date,
            snapshot_point=snapshot_point,
            operator=operator,
            status=SNAPSHOT_STATUS_ACTIVE,
            note=note,
            source="manual",
            checksum=checksum,
            created_at=now,
            updated_at=now,
        )

        self.db.insert_duty_snapshot(snapshot, content)

        max_ret = self.snapshot_cfg.max_retention_per_team
        if max_ret > 0:
            deleted = self.db.delete_oldest_snapshots(team_id, max_ret)
            if deleted:
                self._log_operation(
                    SNAPSHOT_OP_DELETE, "system",
                    team_id=team_id,
                    status="auto_cleanup",
                    detail=f"自动清理 {deleted} 份超限快照",
                )

        self._log_operation(
            SNAPSHOT_OP_GENERATE, operator,
            team_id=team_id, snapshot_id=snapshot_id,
            status="success",
            detail=f"成员:{len(members)} 排班:{len(schedules)} 交班:{len(today_handovers)} 升级日志:{len(escalation_logs)}",
        )

        return SnapshotGenerateResult(
            snapshot=snapshot,
            content=content,
            member_count=len(members),
            schedule_count=len(schedules),
            handover_count=len(today_handovers),
            escalation_log_count=len(escalation_logs),
        )

    def query_snapshots(self, team_id: str | None = None,
                         snapshot_date: str | None = None,
                         date_from: str | None = None,
                         date_to: str | None = None,
                         operator: str | None = None,
                         status: str | None = None,
                         limit: int | None = None) -> list[DutySnapshot]:
        """按条件查询快照"""
        return self.db.filter_duty_snapshots(
            team_id=team_id,
            snapshot_date=snapshot_date,
            date_from=date_from,
            date_to=date_to,
            operator=operator,
            status=status,
            limit=limit,
        )

    def get_snapshot_detail(self, snapshot_id: str) -> dict[str, Any] | None:
        """获取快照详情（含内容）"""
        snapshot = self.db.get_duty_snapshot(snapshot_id)
        if snapshot is None:
            return None
        content = self.db.get_duty_snapshot_content(snapshot_id)
        result = snapshot.to_dict()
        if content:
            result["content"] = content.to_dict()
        return result

    def format_snapshot_list(self, snapshots: list[DutySnapshot]) -> str:
        """格式化快照列表"""
        if not snapshots:
            return "没有找到匹配的快照"

        header = (
            f"{'快照ID':<22} {'班组':<12} {'日期':<12} {'时点':<10} "
            f"{'操作人':<10} {'状态':<10} {'创建时间':<20}"
        )
        lines = [header, "-" * len(header)]

        for s in snapshots:
            status_label = {
                SNAPSHOT_STATUS_ACTIVE: "活跃",
                SNAPSHOT_STATUS_DELETED: "已删除",
                SNAPSHOT_STATUS_IMPORTED: "已导入",
                SNAPSHOT_STATUS_ROLLED_BACK: "已回滚",
            }.get(s.status, s.status)
            lines.append(
                f"{s.id:<22} {s.team_name:<12} {s.snapshot_date:<12} "
                f"{s.snapshot_point:<10} {s.operator:<10} {status_label:<10} "
                f"{s.created_at:<20}"
            )

        lines.append("")
        lines.append(f"共 {len(snapshots)} 份快照")
        return "\n".join(lines)

    def format_snapshot_detail(self, detail: dict[str, Any]) -> str:
        """格式化快照详情"""
        lines = [
            f"快照详情: {detail.get('snapshot_id', '')}",
            f"  班组: {detail.get('team_name', '')} ({detail.get('team_id', '')})",
            f"  日期: {detail.get('snapshot_date', '')}",
            f"  时点: {detail.get('snapshot_point', '')}",
            f"  操作人: {detail.get('operator', '')}",
            f"  状态: {detail.get('status', '')}",
            f"  来源: {detail.get('source', '')}",
            f"  校验和: {detail.get('checksum', '')}",
            f"  创建时间: {detail.get('created_at', '')}",
        ]
        if detail.get("note"):
            lines.append(f"  备注: {detail['note']}")

        content = detail.get("content", {})
        if content:
            lines.append("")
            lines.append("快照内容:")
            members = content.get("members", [])
            schedules = content.get("schedules", [])
            handovers = content.get("handovers", [])
            escalation_logs = content.get("escalation_logs", [])
            lines.append(f"  成员数: {len(members)}")
            lines.append(f"  排班数: {len(schedules)}")
            lines.append(f"  交班记录数: {len(handovers)}")
            lines.append(f"  升级命中日志数: {len(escalation_logs)}")

            if members:
                lines.append("")
                lines.append("  成员列表:")
                for m in members[:10]:
                    lines.append(
                        f"    {m.get('name', '')} ({m.get('role', '')})"
                    )
                if len(members) > 10:
                    lines.append(f"    ... 还有 {len(members) - 10} 人")

            if schedules:
                lines.append("")
                lines.append("  排班列表:")
                for s in schedules[:10]:
                    lines.append(
                        f"    {s.get('schedule_date', '')} {s.get('shift_type', '')} "
                        f"- {s.get('member_name', s.get('member_id', ''))}"
                    )
                if len(schedules) > 10:
                    lines.append(f"    ... 还有 {len(schedules) - 10} 条")

        return "\n".join(lines)

    def diff_snapshots(self, snapshot_a_id: str, snapshot_b_id: str,
                        operator: str = "") -> SnapshotDiffResult:
        """比对两份快照的差异"""
        snap_a = self.db.get_duty_snapshot(snapshot_a_id)
        snap_b = self.db.get_duty_snapshot(snapshot_b_id)

        if snap_a is None:
            raise SnapshotError(f"快照不存在: {snapshot_a_id}")
        if snap_b is None:
            raise SnapshotError(f"快照不存在: {snapshot_b_id}")
        if snap_a.team_id != snap_b.team_id:
            raise SnapshotError("只能比对同一班组的快照")

        content_a = self.db.get_duty_snapshot_content(snapshot_a_id)
        content_b = self.db.get_duty_snapshot_content(snapshot_b_id)

        if content_a is None or content_b is None:
            raise SnapshotError("快照内容缺失")

        summary, detail, has_conflicts = self._compute_diff(content_a, content_b)

        diff = DutySnapshotDiff(
            id=_generate_snapshot_id("SDIFF-"),
            snapshot_a_id=snapshot_a_id,
            snapshot_b_id=snapshot_b_id,
            team_id=snap_a.team_id,
            operator=operator,
            diff_summary_json=json.dumps(summary, ensure_ascii=False),
            diff_detail_json=json.dumps(detail, ensure_ascii=False),
            has_conflicts=has_conflicts,
            created_at=self._now(),
        )

        self.db.insert_duty_snapshot_diff(diff)

        self._log_operation(
            SNAPSHOT_OP_DIFF, operator,
            team_id=snap_a.team_id,
            snapshot_id=snapshot_a_id,
            diff_id=diff.id,
            status="success",
            detail=f"比对 {snapshot_a_id} vs {snapshot_b_id}",
        )

        return SnapshotDiffResult(
            diff=diff,
            summary=summary,
            detail=detail,
        )

    def _compute_diff(self, content_a: DutySnapshotContent,
                       content_b: DutySnapshotContent) -> tuple[dict, dict, bool]:
        """计算两份内容的差异"""
        summary: dict[str, Any] = {}
        detail: dict[str, Any] = {}
        has_conflicts = False

        a_members = {m.get("member_id", m.get("name", "")): m for m in content_a.members}
        b_members = {m.get("member_id", m.get("name", "")): m for m in content_b.members}
        added_members = set(b_members.keys()) - set(a_members.keys())
        removed_members = set(a_members.keys()) - set(b_members.keys())
        changed_members = []
        for mid in set(a_members.keys()) & set(b_members.keys()):
            if a_members[mid] != b_members[mid]:
                changed_members.append(mid)
        if added_members or removed_members or changed_members:
            summary["members"] = {
                "added": len(added_members),
                "removed": len(removed_members),
                "changed": len(changed_members),
            }
            detail["members"] = {
                "added": list(added_members),
                "removed": list(removed_members),
                "changed": changed_members,
            }

        a_scheds = {(s.get("schedule_date", ""), s.get("shift_type", ""), s.get("member_id", "")): s
                     for s in content_a.schedules}
        b_scheds = {(s.get("schedule_date", ""), s.get("shift_type", ""), s.get("member_id", "")): s
                     for s in content_b.schedules}
        added_scheds = set(b_scheds.keys()) - set(a_scheds.keys())
        removed_scheds = set(a_scheds.keys()) - set(b_scheds.keys())
        if added_scheds or removed_scheds:
            summary["schedules"] = {
                "added": len(added_scheds),
                "removed": len(removed_scheds),
            }
            detail["schedules"] = {
                "added": [f"{d} {sh} -> {m}" for d, sh, m in added_scheds],
                "removed": [f"{d} {sh} -> {m}" for d, sh, m in removed_scheds],
            }

        a_handovers = {h.get("handover_id", ""): h for h in content_a.handovers}
        b_handovers = {h.get("handover_id", ""): h for h in content_b.handovers}
        added_handovers = set(b_handovers.keys()) - set(a_handovers.keys())
        if added_handovers:
            summary["handovers"] = {"added": len(added_handovers)}
            detail["handovers"] = {"added": list(added_handovers)}

        a_logs = {e.get("log_id", ""): e for e in content_a.escalation_logs}
        b_logs = {e.get("log_id", ""): e for e in content_b.escalation_logs}
        added_logs = set(b_logs.keys()) - set(a_logs.keys())
        changed_logs = []
        for lid in set(a_logs.keys()) & set(b_logs.keys()):
            if a_logs[lid].get("status") != b_logs[lid].get("status"):
                changed_logs.append({
                    "log_id": lid,
                    "old_status": a_logs[lid].get("status"),
                    "new_status": b_logs[lid].get("status"),
                })
        if added_logs or changed_logs:
            summary["escalation_logs"] = {
                "added": len(added_logs),
                "changed": len(changed_logs),
            }
            detail["escalation_logs"] = {
                "added": list(added_logs),
                "changed": changed_logs,
            }

        if removed_members or removed_scheds:
            has_conflicts = True
            detail["conflicts"] = []
            if removed_members:
                for mid in removed_members:
                    detail["conflicts"].append(
                        f"成员被删除: {a_members[mid].get('name', mid)}"
                    )
            if removed_scheds:
                for key in removed_scheds:
                    detail["conflicts"].append(
                        f"排班被删除: {key[0]} {key[1]}"
                    )

        return summary, detail, has_conflicts

    def export_snapshots(self, output_path: str,
                          team_id: str | None = None,
                          snapshot_ids: list[str] | None = None,
                          fmt: str | None = None,
                          operator: str = "",
                          include_content: bool = True) -> SnapshotExportResult:
        """导出快照到文件"""
        self._check_permission(operator, self.snapshot_cfg.allowed_export_roles)

        if fmt is None:
            ext = os.path.splitext(output_path)[1].lower().lstrip(".")
            fmt = ext if ext in ("csv", "json") else "json"

        if fmt not in ("csv", "json"):
            raise SnapshotError(f"不支持的导出格式: {fmt}")

        if snapshot_ids:
            snapshots = []
            for sid in snapshot_ids:
                s = self.db.get_duty_snapshot(sid)
                if s:
                    snapshots.append(s)
        elif team_id:
            snapshots = self.db.get_snapshots_by_team(team_id)
            snapshots = [s for s in snapshots if s.status != SNAPSHOT_STATUS_DELETED]
        else:
            snapshots = self.db.filter_duty_snapshots(status=SNAPSHOT_STATUS_ACTIVE)

        included_teams: list[str] = []
        for s in snapshots:
            if s.team_name not in included_teams:
                self._check_team_exportable(s.team_name)
                included_teams.append(s.team_name)

        if fmt == "json":
            self._export_json(snapshots, output_path, include_content)
        else:
            self._export_csv(snapshots, output_path, include_content)

        self._log_operation(
            SNAPSHOT_OP_EXPORT, operator,
            team_id=team_id or "",
            status="success",
            detail=f"导出 {len(snapshots)} 份快照",
            export_file=os.path.abspath(output_path),
        )

        return SnapshotExportResult(
            file_path=os.path.abspath(output_path),
            snapshot_count=len(snapshots),
            format=fmt,
            included_teams=included_teams,
        )

    def _export_json(self, snapshots: list[DutySnapshot],
                      output_path: str, include_content: bool) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(output_path)) or ".", exist_ok=True)
        export_data: list[dict[str, Any]] = []
        for s in snapshots:
            entry = s.to_export_dict()
            if include_content:
                content = self.db.get_duty_snapshot_content(s.id)
                if content:
                    entry["content"] = content.to_dict()
            export_data.append(entry)

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(export_data, f, ensure_ascii=False, indent=2)

    def _export_csv(self, snapshots: list[DutySnapshot],
                     output_path: str, include_content: bool) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(output_path)) or ".", exist_ok=True)
        fieldnames = [
            "snapshot_id", "team_id", "team_name", "snapshot_date",
            "snapshot_point", "operator", "status", "note", "source",
            "checksum", "created_at",
        ]
        if include_content:
            fieldnames.extend([
                "member_count", "schedule_count", "handover_count",
                "escalation_log_count",
            ])

        with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for s in snapshots:
                row = s.to_export_dict()
                if include_content:
                    content = self.db.get_duty_snapshot_content(s.id)
                    row["member_count"] = len(content.members) if content else 0
                    row["schedule_count"] = len(content.schedules) if content else 0
                    row["handover_count"] = len(content.handovers) if content else 0
                    row["escalation_log_count"] = len(content.escalation_logs) if content else 0
                writer.writerow(row)

    def import_snapshots(self, file_path: str, operator: str = "",
                          conflict_strategy: str = "skip") -> SnapshotImportResult:
        """从文件导入快照"""
        self._check_permission(operator, self.snapshot_cfg.allowed_import_roles)

        if not os.path.exists(file_path):
            raise SnapshotError(f"文件不存在: {file_path}")

        ext = os.path.splitext(file_path)[1].lower().lstrip(".")
        if ext == "csv":
            data_list = self._read_csv(file_path)
        elif ext == "json":
            data_list = self._read_json(file_path)
        else:
            raise SnapshotError(f"不支持的文件格式: {ext}")

        result = SnapshotImportResult(file_path=os.path.abspath(file_path))
        result.total_count = len(data_list)

        for data in data_list:
            try:
                snapshot, content = self._import_single(
                    data, operator, conflict_strategy
                )
                result.success_count += 1
                result.items.append({
                    "snapshot_id": snapshot.id,
                    "team_name": snapshot.team_name,
                    "status": "success",
                    "reason": "导入成功",
                })
            except SnapshotConflictError as e:
                if conflict_strategy == "skip":
                    result.skipped_count += 1
                    result.items.append({
                        "snapshot_id": data.get("snapshot_id", ""),
                        "team_name": data.get("team_name", ""),
                        "status": "skipped",
                        "reason": str(e),
                    })
                elif conflict_strategy == "abort":
                    result.error_count += 1
                    result.items.append({
                        "snapshot_id": data.get("snapshot_id", ""),
                        "team_name": data.get("team_name", ""),
                        "status": "error",
                        "reason": str(e),
                    })
                    break
                else:
                    try:
                        existing = self.db.get_duty_snapshot(data.get("snapshot_id", ""))
                        if existing:
                            existing.status = SNAPSHOT_STATUS_DELETED
                            existing.updated_at = self._now()
                            self.db.update_duty_snapshot(existing)
                        snapshot, content = self._import_single(
                            data, operator, "force"
                        )
                        result.success_count += 1
                        result.items.append({
                            "snapshot_id": snapshot.id,
                            "team_name": snapshot.team_name,
                            "status": "success",
                            "reason": "覆盖导入",
                        })
                    except Exception as ex:
                        result.error_count += 1
                        result.items.append({
                            "snapshot_id": data.get("snapshot_id", ""),
                            "team_name": data.get("team_name", ""),
                            "status": "error",
                            "reason": str(ex),
                        })
            except SnapshotError as e:
                result.error_count += 1
                result.items.append({
                    "snapshot_id": data.get("snapshot_id", ""),
                    "team_name": data.get("team_name", ""),
                    "status": "error",
                    "reason": str(e),
                })

        import_status = SNAPSHOT_IMPORT_STATUS_SUCCESS
        if result.error_count > 0:
            import_status = SNAPSHOT_IMPORT_STATUS_FAILED if result.success_count == 0 else SNAPSHOT_IMPORT_STATUS_PARTIAL

        self._log_operation(
            SNAPSHOT_OP_IMPORT, operator,
            status=import_status,
            detail=f"总计:{result.total_count} 成功:{result.success_count} "
                   f"跳过:{result.skipped_count} 冲突:{result.conflict_count} 错误:{result.error_count}",
            import_file=os.path.abspath(file_path),
        )

        return result

    def _read_json(self, file_path: str) -> list[dict[str, Any]]:
        with open(file_path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return [data]
        return data if isinstance(data, list) else []

    def _read_csv(self, file_path: str) -> list[dict[str, Any]]:
        with open(file_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            return list(reader)

    def _import_single(self, data: dict[str, Any], operator: str,
                        conflict_strategy: str) -> tuple[DutySnapshot, DutySnapshotContent]:
        """导入单个快照"""
        snapshot_id = data.get("snapshot_id", "")
        team_id = data.get("team_id", "")
        team_name = data.get("team_name", "")

        if not team_id:
            team = self.db.get_duty_team_by_name(team_name)
            if team is None:
                raise SnapshotError(f"班组不存在: {team_name}（ID: {team_id}）")
            team_id = team.id

        if not self.db.duty_team_exists(team_id):
            raise SnapshotConflictError(
                f"班组已被删除: {team_name} ({team_id})，无法导入快照。"
            )

        if self.db.duty_snapshot_exists(snapshot_id) and conflict_strategy != "force":
            raise SnapshotConflictError(
                f"快照ID已存在: {snapshot_id}，不允许重复导入。"
                f"\n提示: 使用 --conflict-strategy force 可覆盖导入。"
            )

        if self.db.duty_snapshot_exists(snapshot_id) and conflict_strategy == "force":
            with self.db._conn() as conn:
                conn.execute("DELETE FROM duty_snapshot_contents WHERE snapshot_id = ?",
                             (snapshot_id,))
                conn.execute("DELETE FROM duty_snapshots WHERE id = ?",
                             (snapshot_id,))

        snapshot_date = data.get("snapshot_date", self._today())
        snapshot_point = data.get("snapshot_point", "")

        if (self.db.duty_snapshot_unique_exists(team_id, snapshot_date, snapshot_point)
                and conflict_strategy != "force"):
            raise SnapshotConflictError(
                f"班组 {team_name} 在 {snapshot_date} 时点 {snapshot_point} "
                f"已有活跃快照，不允许重复导入。"
                f"\n提示: 使用 --conflict-strategy force 可覆盖导入。"
            )

        if conflict_strategy == "force":
            existing_snaps = self.db.filter_duty_snapshots(
                team_id=team_id, snapshot_date=snapshot_date,
            )
            for es in existing_snaps:
                if es.snapshot_point == snapshot_point:
                    with self.db._conn() as conn:
                        conn.execute("DELETE FROM duty_snapshot_contents WHERE snapshot_id = ?",
                                     (es.id,))
                        conn.execute("DELETE FROM duty_snapshots WHERE id = ?",
                                     (es.id,))

        now = self._now()
        snapshot = DutySnapshot(
            id=snapshot_id or _generate_snapshot_id("SNAP-"),
            team_id=team_id,
            team_name=team_name,
            snapshot_date=snapshot_date,
            snapshot_point=snapshot_point,
            operator=operator or data.get("operator", "import"),
            status=SNAPSHOT_STATUS_IMPORTED,
            note=data.get("note", ""),
            source="import",
            checksum=data.get("checksum", ""),
            created_at=data.get("created_at", now),
            updated_at=now,
        )

        content_data = data.get("content", {})
        content = DutySnapshotContent(
            snapshot_id=snapshot.id,
            team_info=content_data.get("team_info", {}),
            members=content_data.get("members", []),
            schedules=content_data.get("schedules", []),
            handovers=content_data.get("handovers", []),
            escalation_logs=content_data.get("escalation_logs", []),
            escalation_levels=content_data.get("escalation_levels", []),
            time_windows=content_data.get("time_windows", []),
            meta=content_data.get("meta", {}),
        )

        self.db.insert_duty_snapshot(snapshot, content)
        return snapshot, content

    def rollback_last_import(self, operator: str = "",
                               team_id: str | None = None) -> SnapshotRollbackResult:
        """回滚最近一次错误导入"""
        if not self.snapshot_cfg.allow_rollback:
            raise SnapshotPermissionError(
                "配置中已禁用回滚操作。\n"
                "提示: 在配置中设置 snapshot.allow_rollback: true 可启用回滚。"
            )

        self._check_permission(operator, self.snapshot_cfg.allowed_import_roles)

        last_log = self.db.get_last_failed_import_log(team_id)
        if last_log is None:
            return SnapshotRollbackResult(
                snapshot_id="", operator=operator, deleted=False
            )

        snapshot_id = last_log.snapshot_id
        if not snapshot_id:
            return SnapshotRollbackResult(
                snapshot_id="", operator=operator, deleted=False
            )

        snapshot = self.db.get_duty_snapshot(snapshot_id)
        if snapshot is None:
            return SnapshotRollbackResult(
                snapshot_id=snapshot_id, operator=operator, deleted=False
            )

        snapshot.status = SNAPSHOT_STATUS_ROLLED_BACK
        snapshot.updated_at = self._now()
        self.db.update_duty_snapshot(snapshot)

        self._log_operation(
            SNAPSHOT_OP_ROLLBACK, operator,
            team_id=snapshot.team_id,
            snapshot_id=snapshot_id,
            status="success",
            detail=f"回滚快照 {snapshot_id} (原日志: {last_log.id})",
        )

        return SnapshotRollbackResult(
            snapshot_id=snapshot_id, operator=operator, deleted=True
        )

    def verify_snapshot_consistency(self, snapshot_id: str) -> dict[str, Any]:
        """验证快照内容与当前数据库的一致性"""
        snapshot = self.db.get_duty_snapshot(snapshot_id)
        if snapshot is None:
            raise SnapshotError(f"快照不存在: {snapshot_id}")

        content = self.db.get_duty_snapshot_content(snapshot_id)
        if content is None:
            raise SnapshotError(f"快照内容缺失: {snapshot_id}")

        result: dict[str, Any] = {
            "snapshot_id": snapshot_id,
            "team_name": snapshot.team_name,
            "snapshot_date": snapshot.snapshot_date,
            "consistent": True,
            "checks": [],
        }

        current_members = self.db.get_duty_members_by_team(snapshot.team_id)
        snap_member_ids = {m.get("member_id", m.get("name", "")) for m in content.members}
        current_member_ids = {m.id for m in current_members}
        if snap_member_ids != current_member_ids:
            result["consistent"] = False
            result["checks"].append({
                "type": "members",
                "status": "diff",
                "detail": f"快照: {len(snap_member_ids)}人, 当前: {len(current_member_ids)}人",
            })
        else:
            result["checks"].append({
                "type": "members",
                "status": "match",
                "detail": f"{len(current_member_ids)}人",
            })

        current_schedules = self.db.get_duty_schedules_by_date(
            snapshot.team_id, snapshot.snapshot_date
        )
        if not current_schedules:
            all_s = self.db.get_all_duty_schedules_by_team(snapshot.team_id)
            current_schedules = [s for s in all_s if s.schedule_date == snapshot.snapshot_date]
        snap_sched_count = len(content.schedules)
        current_sched_count = len(current_schedules)
        if snap_sched_count != current_sched_count:
            result["consistent"] = False
            result["checks"].append({
                "type": "schedules",
                "status": "diff",
                "detail": f"快照: {snap_sched_count}条, 当前: {current_sched_count}条",
            })
        else:
            result["checks"].append({
                "type": "schedules",
                "status": "match",
                "detail": f"{current_sched_count}条",
            })

        current_checksum = self._compute_checksum(content)
        if current_checksum != snapshot.checksum:
            result["consistent"] = False
            result["checks"].append({
                "type": "checksum",
                "status": "diff",
                "detail": "快照内容已被修改",
            })
        else:
            result["checks"].append({
                "type": "checksum",
                "status": "match",
                "detail": "校验和一致",
            })

        return result

    def format_verify_result(self, result: dict[str, Any]) -> str:
        """格式化验证结果"""
        lines = [
            f"快照一致性验证: {result['snapshot_id']}",
            f"  班组: {result['team_name']}",
            f"  日期: {result['snapshot_date']}",
            f"  整体一致: {'是' if result['consistent'] else '否'}",
            "",
        ]
        for check in result.get("checks", []):
            status_icon = "✓" if check["status"] == "match" else "✗"
            lines.append(
                f"  [{status_icon}] {check['type']}: {check['detail']}"
            )
        return "\n".join(lines)

    def list_logs_formatted(self, team_id: str | None = None,
                             operation: str | None = None,
                             operator: str | None = None,
                             limit: int = 50) -> str:
        """格式化操作日志列表"""
        logs = self.db.list_snapshot_logs(
            team_id=team_id, operation=operation,
            operator=operator, limit=limit,
        )
        if not logs:
            return "没有快照操作日志"

        header = (
            f"{'日志ID':<22} {'操作':<10} {'操作人':<10} "
            f"{'状态':<10} {'创建时间':<20}"
        )
        lines = [header, "-" * len(header)]
        for l in logs:
            op_label = {
                SNAPSHOT_OP_GENERATE: "生成",
                SNAPSHOT_OP_EXPORT: "导出",
                SNAPSHOT_OP_IMPORT: "导入",
                SNAPSHOT_OP_ROLLBACK: "回滚",
                SNAPSHOT_OP_DELETE: "删除",
                SNAPSHOT_OP_DIFF: "比对",
            }.get(l.operation, l.operation)
            lines.append(
                f"{l.id:<22} {op_label:<10} {l.operator:<10} "
                f"{l.status:<10} {l.created_at:<20}"
            )
            if l.error_message:
                lines.append(f"  错误: {l.error_message}")

        lines.append("")
        lines.append(f"共 {len(logs)} 条日志")
        return "\n".join(lines)

    def format_diff_detail(self, diff_id: str) -> str:
        """格式化差异详情"""
        diff = self.db.get_duty_snapshot_diff(diff_id)
        if diff is None:
            return f"差异记录不存在: {diff_id}"

        summary = json.loads(diff.diff_summary_json) if diff.diff_summary_json else {}
        detail = json.loads(diff.diff_detail_json) if diff.diff_detail_json else {}

        lines = [
            f"差异详情: {diff.id}",
            f"  快照A: {diff.snapshot_a_id}",
            f"  快照B: {diff.snapshot_b_id}",
            f"  班组: {diff.team_id}",
            f"  存在冲突: {'是' if diff.has_conflicts else '否'}",
            "",
        ]

        if not summary:
            lines.append("  无差异")
            return "\n".join(lines)

        for section, info in summary.items():
            lines.append(f"  [{section}]")
            if isinstance(info, dict):
                for key, val in info.items():
                    lines.append(f"    {key}: {val}")

        if detail.get("conflicts"):
            lines.append("")
            lines.append("  冲突列表:")
            for c in detail["conflicts"]:
                lines.append(f"    ⚠ {c}")

        return "\n".join(lines)
