"""事件归并模块：按设备与时间窗口将相近异常归并成事件"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from .config import AppConfig, EventMergeConfig
from .database import Database, Event, SourceRecord


SEVERITY_ORDER = {"critical": 3, "warning": 2, "info": 1}


@dataclass
class MergeResult:
    """归并结果"""
    total_records: int = 0
    event_count: int = 0
    preserved_annotations: int = 0

    def formatted(self) -> str:
        return (
            f"处理记录: {self.total_records}\n"
            f"生成事件: {self.event_count}\n"
            f"保留标注: {self.preserved_annotations}"
        )


class EventMerger:
    """事件归并器"""

    def __init__(self, db: Database, config: AppConfig):
        self.db = db
        self.config = config
        self.merge_cfg: EventMergeConfig = config.event_merge

    def _parse_time(self, time_str: str) -> datetime:
        return datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")

    def _format_time(self, dt: datetime) -> str:
        return dt.strftime("%Y-%m-%d %H:%M:%S")

    def _merge_key(self, record: SourceRecord) -> tuple:
        """生成归并键"""
        key_parts = []
        if self.merge_cfg.same_device_only:
            key_parts.append(record.device_id)
        else:
            key_parts.append("*")
        if self.merge_cfg.same_issue_type:
            key_parts.append(record.issue_type)
        else:
            key_parts.append("*")
        return tuple(key_parts)

    def _higher_severity(self, sev_a: str, sev_b: str) -> str:
        return sev_a if SEVERITY_ORDER.get(sev_a, 0) >= SEVERITY_ORDER.get(sev_b, 0) else sev_b

    def merge(self, preserve_annotations: bool = True) -> MergeResult:
        """执行事件归并

        Args:
            preserve_annotations: 是否保留已有事件的标注状态
        """
        result = MergeResult()

        records = self.db.get_all_records()
        result.total_records = len(records)

        if not records:
            self.db.clear_events()
            return result

        existing_events: dict[str, Event] = {}
        if preserve_annotations:
            for ev in self.db.get_all_events():
                existing_events[ev.id] = ev

        self.db.clear_events()

        records_by_key: dict[tuple, list[SourceRecord]] = {}
        for rec in records:
            key = self._merge_key(rec)
            records_by_key.setdefault(key, []).append(rec)

        window = timedelta(minutes=self.merge_cfg.time_window_minutes)

        for key, recs in records_by_key.items():
            recs_sorted = sorted(recs, key=lambda r: self._parse_time(r.event_time))
            clusters: list[list[SourceRecord]] = []
            current: list[SourceRecord] = []
            current_end: Optional[datetime] = None

            for rec in recs_sorted:
                rec_time = self._parse_time(rec.event_time)
                if current and current_end and (rec_time - current_end) <= window:
                    current.append(rec)
                    current_end = max(current_end, rec_time)
                else:
                    if current:
                        clusters.append(current)
                    current = [rec]
                    current_end = rec_time
            if current:
                clusters.append(current)

            for cluster in clusters:
                event_id = self._make_event_id(cluster, key)
                first_seen = self._parse_time(cluster[0].event_time)
                last_seen = first_seen
                severity = cluster[0].severity
                record_ids: list[str] = []

                for rec in cluster:
                    rt = self._parse_time(rec.event_time)
                    first_seen = min(first_seen, rt)
                    last_seen = max(last_seen, rt)
                    severity = self._higher_severity(severity, rec.severity)
                    record_ids.append(rec.id)

                event = Event(
                    id=event_id,
                    device_id=cluster[0].device_id,
                    first_seen=self._format_time(first_seen),
                    last_seen=self._format_time(last_seen),
                    issue_type=cluster[0].issue_type,
                    severity=severity,
                    record_count=len(cluster),
                    record_ids=sorted(record_ids),
                )

                if preserve_annotations and event_id in existing_events:
                    old = existing_events[event_id]
                    event.status = old.status
                    event.handler = old.handler
                    event.note = old.note
                    event.version = old.version
                    result.preserved_annotations += 1

                self.db.insert_event(event)
                result.event_count += 1

        return result

    def _make_event_id(self, cluster: list[SourceRecord], key: tuple) -> str:
        """基于归并内容生成稳定的事件 ID"""
        first_time = self._parse_time(cluster[0].event_time)
        rounded = first_time.replace(
            minute=(first_time.minute // self.merge_cfg.time_window_minutes)
            * self.merge_cfg.time_window_minutes,
            second=0,
            microsecond=0,
        )
        id_parts = [
            str(k) for k in key
        ] + [
            rounded.strftime("%Y%m%d%H%M"),
            cluster[0].device_id,
            cluster[0].issue_type,
        ]
        raw = "|".join(id_parts)
        return "EVT-" + uuid.uuid5(uuid.NAMESPACE_DNS, raw).hex[:12].upper()
