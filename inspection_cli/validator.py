"""数据校验模块：校验设备编号、时间、问题类型和严重级别"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .config import ValidationRules


@dataclass
class ValidationError:
    """单条校验错误"""
    row: int
    field: str
    message: str
    value: Any = None

    def __str__(self) -> str:
        base = f"第 {self.row} 行: [{self.field}] {self.message}"
        if self.value is not None:
            base += f" (值: {self.value!r})"
        return base


@dataclass
class ValidationResult:
    """校验结果"""
    errors: list[ValidationError] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return len(self.errors) == 0

    def add(self, row: int, field: str, message: str, value: Any = None) -> None:
        self.errors.append(ValidationError(row, field, message, value))

    def formatted(self) -> str:
        if self.is_valid:
            return "校验通过"
        lines = [f"发现 {len(self.errors)} 个错误:"]
        lines.extend(f"  - {e}" for e in self.errors)
        return "\n".join(lines)


class RecordValidator:
    """巡检记录校验器"""

    def __init__(self, rules: ValidationRules):
        self.rules = rules
        self._device_re = re.compile(rules.device_id_pattern)

    def validate_record(self, record: dict[str, Any], row: int) -> list[ValidationError]:
        """校验单条记录，返回错误列表"""
        errors: list[ValidationError] = []

        device_id = record.get("device_id", "")
        if not device_id:
            errors.append(ValidationError(row, "device_id", "设备编号不能为空"))
        elif not self._device_re.match(device_id):
            errors.append(ValidationError(
                row, "device_id",
                f"设备编号格式不匹配，期望模式: {self.rules.device_id_pattern}",
                device_id
            ))

        event_time = record.get("event_time", "")
        if not event_time:
            errors.append(ValidationError(row, "event_time", "时间不能为空"))
        else:
            parsed = False
            for fmt in self.rules.time_formats:
                try:
                    datetime.strptime(str(event_time), fmt)
                    parsed = True
                    break
                except ValueError:
                    continue
            if not parsed:
                errors.append(ValidationError(
                    row, "event_time",
                    f"时间格式错误，支持的格式: {', '.join(self.rules.time_formats)}",
                    event_time
                ))

        issue_type = record.get("issue_type", "")
        if not issue_type:
            errors.append(ValidationError(row, "issue_type", "问题类型不能为空"))
        elif issue_type not in self.rules.valid_issue_types:
            errors.append(ValidationError(
                row, "issue_type",
                f"问题类型无效，允许值: {', '.join(self.rules.valid_issue_types)}",
                issue_type
            ))

        severity = record.get("severity", "")
        if not severity:
            errors.append(ValidationError(row, "severity", "严重级别不能为空"))
        elif severity not in self.rules.valid_severities:
            errors.append(ValidationError(
                row, "severity",
                f"严重级别无效，允许值: {', '.join(self.rules.valid_severities)}",
                severity
            ))

        return errors

    def validate_batch(self, records: list[dict[str, Any]],
                       start_row: int = 2) -> ValidationResult:
        """批量校验，start_row 为 CSV/JSON 数据起始行号（默认跳过表头第 1 行）"""
        result = ValidationResult()
        for idx, record in enumerate(records):
            row = start_row + idx
            errs = self.validate_record(record, row)
            result.errors.extend(errs)
        return result
