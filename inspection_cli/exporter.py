"""导出模块：CSV 和 JSON 导出"""
from __future__ import annotations

import csv
import json
import os
from dataclasses import dataclass
from typing import Any, Optional

from .config import AppConfig
from .database import Database, Event, SourceRecord


@dataclass
class ExportResult:
    """导出结果"""
    file_path: str
    event_count: int
    format: str

    def formatted(self) -> str:
        return f"已导出 {self.event_count} 条事件到 {self.file_path} ({self.format.upper()})"


class Exporter:
    """数据导出器"""

    def __init__(self, db: Database, config: AppConfig):
        self.db = db
        self.config = config

    def export_events(self, output_path: str, fmt: Optional[str] = None,
                      include_records: bool = False) -> ExportResult:
        """导出事件列表

        Args:
            output_path: 输出文件路径
            fmt: 格式 (csv/json)，为 None 时根据后缀推断
            include_records: 是否包含来源记录详情
        """
        if fmt is None:
            ext = os.path.splitext(output_path)[1].lower().lstrip(".")
            fmt = ext if ext in ("csv", "json") else "csv"

        if fmt not in ("csv", "json"):
            raise ValueError(f"不支持的导出格式: {fmt}")

        events = self.db.get_all_events()

        if fmt == "csv":
            self._export_csv(events, output_path)
        else:
            self._export_json(events, output_path, include_records)

        return ExportResult(
            file_path=os.path.abspath(output_path),
            event_count=len(events),
            format=fmt,
        )

    def _order_fields(self, event_dict: dict[str, Any]) -> dict[str, Any]:
        """按配置的字段顺序输出"""
        ordered: dict[str, Any] = {}
        for field in self.config.export.csv_field_order:
            if field in event_dict:
                ordered[field] = event_dict[field]
        for key, value in event_dict.items():
            if key not in ordered:
                ordered[key] = value
        return ordered

    def _export_csv(self, events: list[Event], output_path: str) -> None:
        field_order = self.config.export.csv_field_order

        os.makedirs(os.path.dirname(os.path.abspath(output_path)) or ".", exist_ok=True)

        with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=field_order, extrasaction="ignore")
            writer.writeheader()
            for ev in events:
                row = self._order_fields(ev.to_dict())
                writer.writerow(row)

    def _export_json(self, events: list[Event], output_path: str,
                     include_records: bool = False) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(output_path)) or ".", exist_ok=True)

        event_list: list[dict[str, Any]] = []
        records_by_id: dict[str, SourceRecord] = {}

        if include_records:
            for rec in self.db.get_all_records():
                records_by_id[rec.id] = rec

        for ev in events:
            ev_dict = self._order_fields(ev.to_dict())
            if include_records:
                ev_dict["records"] = [
                    records_by_id[rid].to_dict()
                    for rid in ev.record_ids if rid in records_by_id
                ]
            event_list.append(ev_dict)

        output = {
            "version": "1.0",
            "event_count": len(events),
            "events": event_list,
        }

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False, indent=2)

    def list_events(self) -> str:
        """列出所有事件（用于 CLI 展示）"""
        events = self.db.get_all_events()
        if not events:
            return "没有事件，请先导入数据并归并。"

        lines = [f"共 {len(events)} 个事件:"]
        lines.append("")
        header = f"{'事件ID':<22} {'状态':<10} {'设备':<14} {'首次出现':<20} {'最后出现':<20} {'级别':<10} {'类型':<14} {'记录数':<6}"
        lines.append(header)
        lines.append("-" * len(header))
        for ev in events:
            lines.append(
                f"{ev.id:<22} {ev.status:<10} {ev.device_id:<14} "
                f"{ev.first_seen:<20} {ev.last_seen:<20} "
                f"{ev.severity:<10} {ev.issue_type:<14} {ev.record_count:<6}"
            )
        return "\n".join(lines)
