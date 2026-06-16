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
class AppConfig:
    """应用配置"""
    validation: ValidationRules = field(default_factory=ValidationRules)
    event_merge: EventMergeConfig = field(default_factory=EventMergeConfig)
    export: ExportConfig = field(default_factory=ExportConfig)
    batch: BatchConfig = field(default_factory=BatchConfig)
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

        if "db_path" in raw:
            if not isinstance(raw["db_path"], str):
                raise ConfigError("db_path 必须是字符串")
            cfg.db_path = raw["db_path"]

        return cfg
