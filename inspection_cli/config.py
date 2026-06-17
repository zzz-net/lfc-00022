"""配置模块：读取和校验规则配置文件"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import yaml


class ConfigError(Exception):
    """配置文件错误"""
    pass


@dataclass
class ValidationRules:
    """校验规则"""
    device_id_pattern: str = r"^DEV-[A-Z0-9]{3,10}$"
    time_formats: list[str] = field(default_factory=lambda: [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
    ])
    valid_issue_types: list[str] = field(default_factory=lambda: [
        "temperature", "pressure", "vibration", "voltage",
        "current", "connectivity", "performance", "security", "other"
    ])
    valid_severities: list[str] = field(default_factory=lambda: [
        "critical", "warning", "info"
    ])


@dataclass
class EventMergeConfig:
    """事件归并配置"""
    time_window_minutes: int = 30
    same_device_only: bool = True
    same_issue_type: bool = True


@dataclass
class ExportConfig:
    """导出配置"""
    csv_field_order: list[str] = field(default_factory=lambda: [
        "event_id", "status", "device_id", "version",
        "first_seen", "last_seen",
        "severity", "issue_type", "record_count", "handler", "note",
        "source_record_ids"
    ])


@dataclass
class BatchConfig:
    """批量操作配置"""
    preview_fields: list[str] = field(default_factory=lambda: [
        "event_id", "status", "device_id", "first_seen", "last_seen",
        "severity", "issue_type", "handler", "note"
    ])
    conflict_strategy: str = "skip"
    log_retention_days: int = 30


@dataclass
class TicketConfig:
    """工单配置"""
    valid_priorities: list[str] = field(default_factory=lambda: [
        "low", "medium", "high", "critical"
    ])
    assignable_users: list[str] = field(default_factory=list)
    default_priority: str = "medium"
    allow_closed_event_ticket: bool = False
    allow_duplicate_open_ticket: bool = False
    log_retention_days: int = 90

    def priority_labels(self) -> dict[str, str]:
        return {
            "low": "低",
            "medium": "中",
            "high": "高",
            "critical": "紧急",
        }


@dataclass
class DutyConfig:
    """值班排班配置"""
    available_teams: list[str] = field(default_factory=list)
    max_escalation_levels: int = 3
    handover_allowed_roles: list[str] = field(default_factory=lambda: ["leader", "manager"])
    default_rollback_window_hours: int = 24
    log_retention_days: int = 90
    valid_roles: list[str] = field(default_factory=lambda: ["leader", "engineer", "operator", "manager"])
    valid_shifts: list[str] = field(default_factory=lambda: ["morning", "afternoon", "night", "day", "custom"])

    def role_labels(self) -> dict[str, str]:
        return {
            "leader": "班组长",
            "engineer": "工程师",
            "operator": "操作员",
            "manager": "经理",
        }

    def shift_labels(self) -> dict[str, str]:
        return {
            "morning": "早班 (08:00-16:00)",
            "afternoon": "中班 (16:00-00:00)",
            "night": "夜班 (00:00-08:00)",
            "day": "日班 (09:00-18:00)",
            "custom": "自定义",
        }


@dataclass
class AppConfig:
    """应用配置"""
    validation: ValidationRules = field(default_factory=ValidationRules)
    event_merge: EventMergeConfig = field(default_factory=EventMergeConfig)
    export: ExportConfig = field(default_factory=ExportConfig)
    batch: BatchConfig = field(default_factory=BatchConfig)
    ticket: TicketConfig = field(default_factory=TicketConfig)
    duty: DutyConfig = field(default_factory=DutyConfig)
    db_path: str = "inspection.db"

    @classmethod
    def load(cls, config_path: str | None = None) -> "AppConfig":
        """从 YAML 文件加载配置，配置错误不清空已有数据（此处为新建配置，不涉及数据）"""
        if config_path is None:
            return cls()

        if not os.path.exists(config_path):
            raise ConfigError(f"配置文件不存在: {config_path}")

        try:
            with open(config_path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
        except yaml.YAMLError as e:
            raise ConfigError(f"配置文件 YAML 格式错误: {e}") from e

        if not isinstance(raw, dict):
            raise ConfigError("配置文件根节点必须是字典")

        return cls._from_dict(raw)

    @classmethod
    def _from_dict(cls, raw: dict[str, Any]) -> "AppConfig":
        cfg = cls()

        if "validation" in raw:
            v = raw["validation"]
            if not isinstance(v, dict):
                raise ConfigError("validation 必须是字典")
            if "device_id_pattern" in v:
                if not isinstance(v["device_id_pattern"], str):
                    raise ConfigError("validation.device_id_pattern 必须是字符串")
                cfg.validation.device_id_pattern = v["device_id_pattern"]
            if "time_formats" in v:
                if not isinstance(v["time_formats"], list):
                    raise ConfigError("validation.time_formats 必须是列表")
                if not all(isinstance(tf, str) for tf in v["time_formats"]):
                    raise ConfigError("validation.time_formats 列表元素必须是字符串")
                cfg.validation.time_formats = v["time_formats"]
            if "valid_issue_types" in v:
                if not isinstance(v["valid_issue_types"], list):
                    raise ConfigError("validation.valid_issue_types 必须是列表")
                if not all(isinstance(t, str) for t in v["valid_issue_types"]):
                    raise ConfigError("validation.valid_issue_types 列表元素必须是字符串")
                cfg.validation.valid_issue_types = v["valid_issue_types"]
            if "valid_severities" in v:
                if not isinstance(v["valid_severities"], list):
                    raise ConfigError("validation.valid_severities 必须是列表")
                if not all(isinstance(s, str) for s in v["valid_severities"]):
                    raise ConfigError("validation.valid_severities 列表元素必须是字符串")
                cfg.validation.valid_severities = v["valid_severities"]

        if "event_merge" in raw:
            em = raw["event_merge"]
            if not isinstance(em, dict):
                raise ConfigError("event_merge 必须是字典")
            if "time_window_minutes" in em:
                if not isinstance(em["time_window_minutes"], int) or em["time_window_minutes"] <= 0:
                    raise ConfigError("event_merge.time_window_minutes 必须是正整数")
                cfg.event_merge.time_window_minutes = em["time_window_minutes"]
            if "same_device_only" in em:
                if not isinstance(em["same_device_only"], bool):
                    raise ConfigError("event_merge.same_device_only 必须是布尔值")
                cfg.event_merge.same_device_only = em["same_device_only"]
            if "same_issue_type" in em:
                if not isinstance(em["same_issue_type"], bool):
                    raise ConfigError("event_merge.same_issue_type 必须是布尔值")
                cfg.event_merge.same_issue_type = em["same_issue_type"]

        if "export" in raw:
            ex = raw["export"]
            if not isinstance(ex, dict):
                raise ConfigError("export 必须是字典")
            if "csv_field_order" in ex:
                if not isinstance(ex["csv_field_order"], list):
                    raise ConfigError("export.csv_field_order 必须是列表")
                if not all(isinstance(f, str) for f in ex["csv_field_order"]):
                    raise ConfigError("export.csv_field_order 列表元素必须是字符串")
                cfg.export.csv_field_order = ex["csv_field_order"]

        if "batch" in raw:
            bt = raw["batch"]
            if not isinstance(bt, dict):
                raise ConfigError("batch 必须是字典")
            if "preview_fields" in bt:
                if not isinstance(bt["preview_fields"], list):
                    raise ConfigError("batch.preview_fields 必须是列表")
                if not all(isinstance(f, str) for f in bt["preview_fields"]):
                    raise ConfigError("batch.preview_fields 列表元素必须是字符串")
                cfg.batch.preview_fields = bt["preview_fields"]
            if "conflict_strategy" in bt:
                if not isinstance(bt["conflict_strategy"], str):
                    raise ConfigError("batch.conflict_strategy 必须是字符串")
                if bt["conflict_strategy"] not in {"skip", "abort", "force"}:
                    raise ConfigError(
                        "batch.conflict_strategy 必须是 skip、abort 或 force"
                    )
                cfg.batch.conflict_strategy = bt["conflict_strategy"]
            if "log_retention_days" in bt:
                if not isinstance(bt["log_retention_days"], int) or bt["log_retention_days"] <= 0:
                    raise ConfigError("batch.log_retention_days 必须是正整数")
                cfg.batch.log_retention_days = bt["log_retention_days"]

        if "ticket" in raw:
            tk = raw["ticket"]
            if not isinstance(tk, dict):
                raise ConfigError("ticket 必须是字典")
            if "valid_priorities" in tk:
                if not isinstance(tk["valid_priorities"], list):
                    raise ConfigError("ticket.valid_priorities 必须是列表")
                if not all(isinstance(p, str) for p in tk["valid_priorities"]):
                    raise ConfigError("ticket.valid_priorities 列表元素必须是字符串")
                if not tk["valid_priorities"]:
                    raise ConfigError("ticket.valid_priorities 不能为空列表")
                cfg.ticket.valid_priorities = tk["valid_priorities"]
            if "assignable_users" in tk:
                if not isinstance(tk["assignable_users"], list):
                    raise ConfigError("ticket.assignable_users 必须是列表")
                if not all(isinstance(u, str) for u in tk["assignable_users"]):
                    raise ConfigError("ticket.assignable_users 列表元素必须是字符串")
                cfg.ticket.assignable_users = tk["assignable_users"]
            if "default_priority" in tk:
                if not isinstance(tk["default_priority"], str):
                    raise ConfigError("ticket.default_priority 必须是字符串")
                if tk["default_priority"] not in cfg.ticket.valid_priorities:
                    raise ConfigError(
                        f"ticket.default_priority ({tk['default_priority']}) "
                        f"不在 valid_priorities 列表中"
                    )
                cfg.ticket.default_priority = tk["default_priority"]
            if "allow_closed_event_ticket" in tk:
                if not isinstance(tk["allow_closed_event_ticket"], bool):
                    raise ConfigError("ticket.allow_closed_event_ticket 必须是布尔值")
                cfg.ticket.allow_closed_event_ticket = tk["allow_closed_event_ticket"]
            if "allow_duplicate_open_ticket" in tk:
                if not isinstance(tk["allow_duplicate_open_ticket"], bool):
                    raise ConfigError("ticket.allow_duplicate_open_ticket 必须是布尔值")
                cfg.ticket.allow_duplicate_open_ticket = tk["allow_duplicate_open_ticket"]
            if "log_retention_days" in tk:
                if not isinstance(tk["log_retention_days"], int) or tk["log_retention_days"] <= 0:
                    raise ConfigError("ticket.log_retention_days 必须是正整数")
                cfg.ticket.log_retention_days = tk["log_retention_days"]

        if "duty" in raw:
            dt = raw["duty"]
            if not isinstance(dt, dict):
                raise ConfigError("duty 必须是字典")
            if "available_teams" in dt:
                if not isinstance(dt["available_teams"], list):
                    raise ConfigError("duty.available_teams 必须是列表")
                if not all(isinstance(t, str) for t in dt["available_teams"]):
                    raise ConfigError("duty.available_teams 列表元素必须是字符串")
                cfg.duty.available_teams = dt["available_teams"]
            if "max_escalation_levels" in dt:
                if not isinstance(dt["max_escalation_levels"], int) or dt["max_escalation_levels"] <= 0:
                    raise ConfigError("duty.max_escalation_levels 必须是正整数")
                cfg.duty.max_escalation_levels = dt["max_escalation_levels"]
            if "handover_allowed_roles" in dt:
                if not isinstance(dt["handover_allowed_roles"], list):
                    raise ConfigError("duty.handover_allowed_roles 必须是列表")
                if not all(isinstance(r, str) for r in dt["handover_allowed_roles"]):
                    raise ConfigError("duty.handover_allowed_roles 列表元素必须是字符串")
                cfg.duty.handover_allowed_roles = dt["handover_allowed_roles"]
            if "default_rollback_window_hours" in dt:
                if not isinstance(dt["default_rollback_window_hours"], int) or dt["default_rollback_window_hours"] <= 0:
                    raise ConfigError("duty.default_rollback_window_hours 必须是正整数")
                cfg.duty.default_rollback_window_hours = dt["default_rollback_window_hours"]
            if "log_retention_days" in dt:
                if not isinstance(dt["log_retention_days"], int) or dt["log_retention_days"] <= 0:
                    raise ConfigError("duty.log_retention_days 必须是正整数")
                cfg.duty.log_retention_days = dt["log_retention_days"]
            if "valid_roles" in dt:
                if not isinstance(dt["valid_roles"], list):
                    raise ConfigError("duty.valid_roles 必须是列表")
                if not all(isinstance(r, str) for r in dt["valid_roles"]):
                    raise ConfigError("duty.valid_roles 列表元素必须是字符串")
                if not dt["valid_roles"]:
                    raise ConfigError("duty.valid_roles 不能为空列表")
                cfg.duty.valid_roles = dt["valid_roles"]
            if "valid_shifts" in dt:
                if not isinstance(dt["valid_shifts"], list):
                    raise ConfigError("duty.valid_shifts 必须是列表")
                if not all(isinstance(s, str) for s in dt["valid_shifts"]):
                    raise ConfigError("duty.valid_shifts 列表元素必须是字符串")
                if not dt["valid_shifts"]:
                    raise ConfigError("duty.valid_shifts 不能为空列表")
                cfg.duty.valid_shifts = dt["valid_shifts"]

        if "db_path" in raw:
            if not isinstance(raw["db_path"], str):
                raise ConfigError("db_path 必须是字符串")
            cfg.db_path = raw["db_path"]

        return cfg
