"""批量任务模板管理：命名模板的保存、读取、复制、删除和冲突检测"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from .batch import (
    BatchFilter, BatchOperationError, BatchUpdate,
)
from .config import AppConfig
from .database import (
    BatchTemplate, Database, VALID_CONFLICT_STRATEGIES, VALID_STATUSES,
)


class TemplateError(Exception):
    """模板操作错误"""
    pass


class TemplateValidationIssue:
    """模板兼容性验证问题"""

    def __init__(self, level: str, field: str, message: str):
        self.level = level
        self.field = field
        self.message = message

    def __str__(self) -> str:
        return f"[{self.level.upper()}] {self.field}: {self.message}"


@dataclass
class TemplateValidationResult:
    """模板兼容性验证结果"""
    issues: list[TemplateValidationIssue] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return not any(i.level == "error" for i in self.issues)

    @property
    def has_warnings(self) -> bool:
        return any(i.level == "warning" for i in self.issues)

    @property
    def errors(self) -> list[TemplateValidationIssue]:
        return [i for i in self.issues if i.level == "error"]

    @property
    def warnings(self) -> list[TemplateValidationIssue]:
        return [i for i in self.issues if i.level == "warning"]

    def formatted(self) -> str:
        if not self.issues:
            return "模板与当前配置完全兼容。"
        lines = []
        if self.errors:
            lines.append(f"发现 {len(self.errors)} 个错误（阻止执行）:")
            for e in self.errors:
                lines.append(f"  {e}")
        if self.warnings:
            lines.append(f"发现 {len(self.warnings)} 个警告:")
            for w in self.warnings:
                lines.append(f"  {w}")
        return "\n".join(lines)


class TemplateManager:
    """批量任务模板管理器"""

    def __init__(self, db: Database, config: AppConfig):
        self.db = db
        self.config = config

    def save_template(self, name: str, description: str,
                      batch_filter: BatchFilter, batch_update: BatchUpdate,
                      conflict_strategy: Optional[str] = None,
                      overwrite: bool = False) -> BatchTemplate:
        """保存批量任务模板

        Args:
            name: 模板名称（唯一标识）
            description: 模板描述
            batch_filter: 筛选条件
            batch_update: 更新内容
            conflict_strategy: 冲突策略，默认使用配置文件设置
            overwrite: 是否覆盖同名模板

        Raises:
            TemplateError: 模板已存在且 overwrite=False
        """
        if not name or not name.strip():
            raise TemplateError("模板名称不能为空")

        name = name.strip()
        if conflict_strategy is None:
            conflict_strategy = self.config.batch.conflict_strategy

        if conflict_strategy not in VALID_CONFLICT_STRATEGIES:
            raise TemplateError(
                f"无效的冲突策略: {conflict_strategy}，"
                f"允许值: {', '.join(sorted(VALID_CONFLICT_STRATEGIES))}"
            )

        if (batch_update.status is None and
                batch_update.handler is None and
                batch_update.note is None):
            raise TemplateError("模板没有指定任何更新内容")

        existing = self.db.get_template_by_name(name)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if existing:
            if not overwrite:
                raise TemplateError(
                    f"模板 '{name}' 已存在。使用 --overwrite 覆盖，"
                    f"或使用 template-copy 复制为新名称。"
                )
            template_id = existing.id
            self.db.update_template(
                template_id=template_id,
                description=description,
                filters=batch_filter.to_json(),
                updates=batch_update.to_json(),
                conflict_strategy=conflict_strategy,
                updated_at=now,
            )
        else:
            template_id = "TPL-" + uuid.uuid4().hex[:12].upper()
            self.db.insert_template(
                template_id=template_id,
                name=name,
                description=description,
                filters=batch_filter.to_json(),
                updates=batch_update.to_json(),
                conflict_strategy=conflict_strategy,
                created_at=now,
                updated_at=now,
            )

        saved = self.db.get_template(template_id)
        if saved is None:
            raise TemplateError(f"保存模板失败: {name}")
        return saved

    def get_template(self, name: str) -> Optional[BatchTemplate]:
        """按名称获取模板"""
        if not name or not name.strip():
            raise TemplateError("模板名称不能为空")
        return self.db.get_template_by_name(name.strip())

    def get_template_or_error(self, name: str) -> BatchTemplate:
        """获取模板，不存在则抛出错误"""
        template = self.get_template(name)
        if template is None:
            raise TemplateError(f"模板不存在: '{name}'。使用 template-list 查看所有模板。")
        return template

    def list_templates(self) -> list[BatchTemplate]:
        """列出所有模板"""
        return self.db.get_all_templates()

    def copy_template(self, source_name: str, target_name: str,
                      new_description: Optional[str] = None) -> BatchTemplate:
        """复制模板到新名称

        Args:
            source_name: 源模板名称
            target_name: 目标模板名称
            new_description: 新描述，为 None 时使用源描述 + '(副本)'
        """
        if not target_name or not target_name.strip():
            raise TemplateError("目标模板名称不能为空")
        target_name = target_name.strip()

        source = self.get_template_or_error(source_name)
        existing = self.db.get_template_by_name(target_name)
        if existing:
            raise TemplateError(
                f"目标模板 '{target_name}' 已存在，请使用其他名称。"
            )

        description = new_description
        if description is None:
            description = source.description + " (副本)" if source.description else "(副本)"

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        new_id = "TPL-" + uuid.uuid4().hex[:12].upper()
        self.db.insert_template(
            template_id=new_id,
            name=target_name,
            description=description,
            filters=source.filters,
            updates=source.updates,
            conflict_strategy=source.conflict_strategy,
            created_at=now,
            updated_at=now,
        )

        result = self.db.get_template(new_id)
        if result is None:
            raise TemplateError(f"复制模板失败: {source_name} → {target_name}")
        return result

    def delete_template(self, name: str) -> bool:
        """删除模板

        Returns:
            是否实际删除了模板
        """
        template = self.get_template(name)
        if template is None:
            return False
        self.db.delete_template(template.id)
        return True

    def template_to_objects(self, template: BatchTemplate) -> tuple[BatchFilter, BatchUpdate, str]:
        """从模板反序列化出 BatchFilter、BatchUpdate 和 conflict_strategy"""
        filter_dict = json.loads(template.filters)
        update_dict = json.loads(template.updates)

        batch_filter = BatchFilter(
            event_ids=filter_dict.get("event_ids"),
            device_ids=filter_dict.get("device_ids"),
            statuses=filter_dict.get("statuses"),
            time_from=filter_dict.get("time_from"),
            time_to=filter_dict.get("time_to"),
        )
        batch_update = BatchUpdate(
            status=update_dict.get("status"),
            handler=update_dict.get("handler"),
            note=update_dict.get("note"),
        )
        return batch_filter, batch_update, template.conflict_strategy

    def validate_template(self, template: BatchTemplate) -> TemplateValidationResult:
        """验证模板与当前配置的兼容性

        检查项：
        - 模板中筛选的状态是否在 VALID_STATUSES 中
        - 模板中目标状态是否在 VALID_STATUSES 中
        - 模板中时间范围是否能被当前配置的 time_formats 正确解析
        - 冲突策略是否有效

        Returns:
            TemplateValidationResult 包含所有错误和警告
        """
        result = TemplateValidationResult()
        filter_dict = json.loads(template.filters)
        update_dict = json.loads(template.updates)

        if template.conflict_strategy not in VALID_CONFLICT_STRATEGIES:
            result.issues.append(TemplateValidationIssue(
                "error", "conflict_strategy",
                f"冲突策略 '{template.conflict_strategy}' 不再有效，"
                f"当前允许值: {', '.join(sorted(VALID_CONFLICT_STRATEGIES))}"
            ))

        statuses_filter = filter_dict.get("statuses")
        if statuses_filter:
            for s in statuses_filter:
                if s not in VALID_STATUSES:
                    result.issues.append(TemplateValidationIssue(
                        "error", "filters.statuses",
                        f"筛选状态 '{s}' 不再有效，"
                        f"当前允许状态: {', '.join(sorted(VALID_STATUSES))}"
                    ))

        target_status = update_dict.get("status")
        if target_status and target_status not in VALID_STATUSES:
            result.issues.append(TemplateValidationIssue(
                "error", "updates.status",
                f"目标状态 '{target_status}' 不再有效，"
                f"当前允许状态: {', '.join(sorted(VALID_STATUSES))}"
            ))

        time_formats = self.config.validation.time_formats
        time_from = filter_dict.get("time_from")
        if time_from:
            if not self._try_parse_time(time_from, time_formats):
                result.issues.append(TemplateValidationIssue(
                    "error", "filters.time_from",
                    f"起始时间 '{time_from}' 无法使用当前配置的时间格式解析。"
                    f"支持格式: {', '.join(time_formats)}"
                ))

        time_to = filter_dict.get("time_to")
        if time_to:
            if not self._try_parse_time(time_to, time_formats):
                result.issues.append(TemplateValidationIssue(
                    "error", "filters.time_to",
                    f"结束时间 '{time_to}' 无法使用当前配置的时间格式解析。"
                    f"支持格式: {', '.join(time_formats)}"
                ))

        if update_dict.get("handler") is not None and not update_dict.get("handler", "").strip():
            result.issues.append(TemplateValidationIssue(
                "error", "updates.handler",
                "处理人为空字符串"
            ))

        device_ids = filter_dict.get("device_ids")
        if device_ids:
            pattern = self.config.validation.device_id_pattern
            import re
            try:
                compiled = re.compile(pattern)
                for did in device_ids:
                    if not compiled.match(did):
                        result.issues.append(TemplateValidationIssue(
                            "warning", "filters.device_ids",
                            f"设备编号 '{did}' 可能不符合当前设备编号模式: {pattern}"
                        ))
            except re.error:
                pass

        return result

    def format_template_list(self, templates: list[BatchTemplate]) -> str:
        """格式化模板列表"""
        if not templates:
            return "暂无模板。使用 template-save 保存你的第一个批量任务模板。"

        lines = [f"共 {len(templates)} 个模板:"]
        lines.append("")
        header = f"{'模板名称':<24} {'冲突策略':<10} {'创建时间':<20} {'更新时间':<20} 描述"
        lines.append(header)
        lines.append("-" * len(header))

        for tpl in templates:
            desc = (tpl.description[:40] + "...") if len(tpl.description) > 40 else tpl.description
            lines.append(
                f"{tpl.name:<24} {tpl.conflict_strategy:<10} "
                f"{tpl.created_at:<20} {tpl.updated_at:<20} {desc}"
            )

        return "\n".join(lines)

    def format_template_detail(self, template: BatchTemplate) -> str:
        """格式化模板详情"""
        filter_dict = json.loads(template.filters)
        update_dict = json.loads(template.updates)

        lines = [f"模板详情: {template.name}"]
        lines.append("=" * 60)
        lines.append(f"模板ID: {template.id}")
        lines.append(f"描述: {template.description or '(无)'}")
        lines.append(f"冲突策略: {template.conflict_strategy}")
        lines.append(f"创建时间: {template.created_at}")
        lines.append(f"更新时间: {template.updated_at}")
        lines.append("")
        lines.append("筛选条件:")
        for key, label in [
            ("event_ids", "事件ID"),
            ("device_ids", "设备编号"),
            ("statuses", "状态"),
            ("time_from", "起始时间"),
            ("time_to", "结束时间"),
        ]:
            val = filter_dict.get(key)
            if val:
                if isinstance(val, list):
                    lines.append(f"  {label}: {', '.join(val)}")
                else:
                    lines.append(f"  {label}: {val}")
            else:
                lines.append(f"  {label}: (未设置)")

        lines.append("")
        lines.append("更新内容:")
        for key, label in [
            ("status", "状态"),
            ("handler", "处理人"),
            ("note", "备注"),
        ]:
            val = update_dict.get(key)
            if val is None:
                lines.append(f"  {label}: (不修改)")
            elif key == "note":
                lines.append(f"  {label}: {val or '(清空)'}")
            else:
                lines.append(f"  {label}: {val}")

        return "\n".join(lines)

    @staticmethod
    def _try_parse_time(time_str: str, formats: list[str]) -> bool:
        """尝试用多种格式解析时间"""
        for fmt in formats:
            try:
                datetime.strptime(time_str, fmt)
                return True
            except ValueError:
                continue
        return False
