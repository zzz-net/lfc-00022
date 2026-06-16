"""数据导入模块：读取 CSV 和 JSON 巡检记录"""
from __future__ import annotations

import csv
import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .config import AppConfig
from .database import Database, SourceRecord
from .validator import RecordValidator, ValidationResult


@dataclass
class ImportResult:
    """导入结果"""
    total: int = 0
    imported: int = 0
    duplicates: int = 0
    errors: int = 0
    validation_result: ValidationResult = field(default_factory=ValidationResult)
    error_messages: list[str] = field(default_factory=list)

    def formatted(self) -> str:
        lines = [
            f"总计记录: {self.total}",
            f"成功导入: {self.imported}",
            f"重复跳过: {self.duplicates}",
            f"校验错误: {self.errors}",
        ]
        if self.validation_result.errors:
            lines.append("")
            lines.append(self.validation_result.formatted())
        if self.error_messages:
            lines.append("")
            lines.append("文件错误:")
            lines.extend(f"  - {m}" for m in self.error_messages)
        return "\n".join(lines)


class RecordImporter:
    """巡检记录导入器"""

    def __init__(self, db: Database, config: AppConfig):
        self.db = db
        self.config = config
        self.validator = RecordValidator(config.validation)

    def _make_record_id(self, record: dict[str, Any]) -> str:
        """基于记录内容生成稳定的唯一 ID，用于去重"""
        key_parts = [
            str(record.get("device_id", "")),
            str(record.get("event_time", "")),
            str(record.get("issue_type", "")),
            str(record.get("severity", "")),
            str(record.get("description", "")),
        ]
        key = "|".join(key_parts)
        return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]

    def _normalize_record(self, raw: dict[str, Any]) -> dict[str, Any]:
        """规范化记录字段名（兼容不同大小写）"""
        normalized = {}
        field_map = {
            "device_id": ["device_id", "deviceId", "device", "设备编号", "设备ID"],
            "event_time": ["event_time", "eventTime", "time", "timestamp", "时间", "巡检时间"],
            "issue_type": ["issue_type", "issueType", "type", "问题类型", "类型"],
            "severity": ["severity", "level", "严重级别", "级别", "等级"],
            "description": ["description", "desc", "message", "描述", "备注"],
        }
        for target, aliases in field_map.items():
            for alias in aliases:
                if alias in raw and raw[alias] not in (None, ""):
                    normalized[target] = str(raw[alias]).strip()
                    break
            if target not in normalized:
                normalized[target] = ""
        return normalized

    def import_file(self, file_path: str) -> ImportResult:
        """导入单个文件（CSV 或 JSON）"""
        result = ImportResult()

        if not os.path.exists(file_path):
            result.error_messages.append(f"文件不存在: {file_path}")
            return result

        ext = os.path.splitext(file_path)[1].lower()
        try:
            if ext == ".csv":
                raw_records, start_row = self._read_csv(file_path)
            elif ext in (".json", ".jsonl"):
                raw_records, start_row = self._read_json(file_path)
            else:
                result.error_messages.append(f"不支持的文件格式: {ext}")
                return result
        except Exception as e:
            result.error_messages.append(f"读取文件失败: {e}")
            return result

        result.total = len(raw_records)

        normalized = [self._normalize_record(r) for r in raw_records]

        validation = self.validator.validate_batch(normalized, start_row=start_row)
        result.validation_result = validation
        if not validation.is_valid:
            result.errors = len(validation.errors)
            return result

        import_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        existing_ids = self.db.get_record_ids()
        filename = os.path.basename(file_path)

        for idx, record in enumerate(normalized):
            record_id = self._make_record_id(record)
            if record_id in existing_ids:
                result.duplicates += 1
                continue

            source_rec = SourceRecord(
                id=record_id,
                device_id=record["device_id"],
                event_time=self._normalize_time(record["event_time"]),
                issue_type=record["issue_type"],
                severity=record["severity"],
                description=record["description"],
                source_file=filename,
                source_row=start_row + idx,
                import_time=import_time,
            )
            self.db.insert_record(source_rec)
            existing_ids.add(record_id)
            result.imported += 1

        return result

    def _normalize_time(self, time_str: str) -> str:
        """将时间统一规范化为 YYYY-MM-DD HH:MM:SS 格式"""
        for fmt in self.config.validation.time_formats:
            try:
                dt = datetime.strptime(time_str, fmt)
                return dt.strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
        return time_str

    def _read_csv(self, file_path: str) -> tuple[list[dict[str, Any]], int]:
        """读取 CSV 文件，返回 (记录列表, 起始行号)"""
        records: list[dict[str, Any]] = []
        with open(file_path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                records.append(dict(row))
        return records, 2

    def _read_json(self, file_path: str) -> tuple[list[dict[str, Any]], int]:
        """读取 JSON 或 JSONL 文件"""
        records: list[dict[str, Any]] = []
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read().strip()

        if not content:
            return records, 1

        if file_path.endswith(".jsonl"):
            for idx, line in enumerate(content.splitlines(), start=1):
                line = line.strip()
                if line:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        records.append(obj)
            return records, 1
        else:
            data = json.loads(content)
            if isinstance(data, list):
                records = [r for r in data if isinstance(r, dict)]
            elif isinstance(data, dict):
                if "records" in data and isinstance(data["records"], list):
                    records = [r for r in data["records"] if isinstance(r, dict)]
                else:
                    records = [data]
            return records, 1
