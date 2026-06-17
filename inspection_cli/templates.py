"""批量任务模板管理：命名模板的保存、读取、复制、删除、导入导出和冲突检测"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from .batch import (
    BatchFilter, BatchOperationError, BatchUpdate,
)
from .config import AppConfig
from .database import (
    BatchTemplate, Database, TEMPLATE_IMPORT_CONFLICT_OVERWRITE,
    TEMPLATE_IMPORT_CONFLICT_RENAME, TEMPLATE_IMPORT_CONFLICT_SKIP,
    TEMPLATE_IMPORT_LOG_STATUS_FAILED, TEMPLATE_IMPORT_LOG_STATUS_PARTIAL,
    TEMPLATE_IMPORT_LOG_STATUS_ROLLED_BACK,
    TEMPLATE_IMPORT_LOG_STATUS_SUCCESS, VALID_CONFLICT_STRATEGIES,
    VALID_STATUSES, VALID_TEMPLATE_IMPORT_CONFLICT_STRATEGIES,
    TemplateVersion, TEMPLATE_VERSION_OP_CREATE,
    TEMPLATE_VERSION_OP_UPDATE, TEMPLATE_VERSION_OP_OVERWRITE,
    TEMPLATE_VERSION_OP_IMPORT, TEMPLATE_VERSION_OP_DELETE_BACKUP,
    TEMPLATE_VERSION_OP_ROLLBACK, VALID_TEMPLATE_VERSION_OPERATIONS,
)


TEMPLATE_EXPORT_VERSION = "1.0"


class TemplateError(Exception):
    """模板操作错误"""
    pass


class TemplateExportError(TemplateError):
    """模板导出错误"""
    pass


class TemplateImportError(TemplateError):
    """模板导入错误"""
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


@dataclass
class TemplateExportResult:
    """模板导出结果"""
    file_path: str
    template_count: int
    template_names: list[str] = field(default_factory=list)

    def formatted(self) -> str:
        lines = [f"已导出 {self.template_count} 个模板到 {self.file_path}"]
        if self.template_names:
            lines.append("")
            lines.append("导出的模板:")
            for name in self.template_names:
                lines.append(f"  - {name}")
        return "\n".join(lines)


@dataclass
class TemplateImportItemResult:
    """单个模板的导入结果"""
    original_name: str
    final_name: str
    status: str
    reason: str = ""

    def formatted(self) -> str:
        status_labels = {
            "success": "成功",
            "skipped": "跳过",
            "overwritten": "覆盖",
            "renamed": "重命名",
            "error": "错误",
        }
        label = status_labels.get(self.status, self.status)
        line = f"[{label}] {self.original_name}"
        if self.original_name != self.final_name:
            line += f" → {self.final_name}"
        if self.reason:
            line += f"  ({self.reason})"
        return line


@dataclass
class TemplateImportResult:
    """模板导入结果"""
    log_id: str = ""
    total_count: int = 0
    success_count: int = 0
    skipped_count: int = 0
    overwritten_count: int = 0
    renamed_count: int = 0
    error_count: int = 0
    status: str = "pending"
    error_message: str = ""
    items: list[TemplateImportItemResult] = field(default_factory=list)
    rolled_back: bool = False

    @property
    def has_errors(self) -> bool:
        return self.error_count > 0 or self.rolled_back

    def formatted(self) -> str:
        lines = []
        if self.rolled_back:
            lines.append("模板导入已回滚，未对环境产生任何变更。")
            lines.append("")
        lines.append(f"导入日志ID: {self.log_id}")
        lines.append(f"总计: {self.total_count}")
        if self.success_count:
            lines.append(f"成功: {self.success_count}")
        if self.skipped_count:
            lines.append(f"跳过: {self.skipped_count}")
        if self.overwritten_count:
            lines.append(f"覆盖: {self.overwritten_count}")
        if self.renamed_count:
            lines.append(f"重命名: {self.renamed_count}")
        if self.error_count:
            lines.append(f"错误: {self.error_count}")
        if self.error_message:
            lines.append(f"错误信息: {self.error_message}")

        if self.items:
            lines.append("")
            lines.append("详情:")
            for item in self.items:
                lines.append(f"  {item.formatted()}")

        return "\n".join(lines)


@dataclass
class TemplateFieldDiff:
    """两个模板版本之间的单字段差异"""
    field: str
    old_value: Any
    new_value: Any

    def formatted(self) -> str:
        def _fmt(v: Any) -> str:
            if v is None:
                return "(未设置)"
            if isinstance(v, list):
                return ", ".join(str(x) for x in v) if v else "(空列表)"
            return str(v)
        return f"{self.field}: {_fmt(self.old_value)} → {_fmt(self.new_value)}"


@dataclass
class TemplateDiffResult:
    """两个模板版本之间的完整差异"""
    template_name: str
    old_version: int
    new_version: int
    filter_diffs: list[TemplateFieldDiff] = field(default_factory=list)
    update_diffs: list[TemplateFieldDiff] = field(default_factory=list)
    other_diffs: list[TemplateFieldDiff] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.filter_diffs or self.update_diffs or self.other_diffs)

    def formatted(self) -> str:
        if not self.has_changes:
            return f"模板 '{self.template_name}' 版本 {self.old_version} 与 {self.new_version} 完全一致。"
        lines = [f"模板 '{self.template_name}' 版本差异: v{self.old_version} → v{self.new_version}"]
        lines.append("=" * 60)
        if self.filter_diffs:
            lines.append("")
            lines.append("筛选条件变更:")
            for d in self.filter_diffs:
                lines.append(f"  {d.formatted()}")
        if self.update_diffs:
            lines.append("")
            lines.append("更新内容变更:")
            for d in self.update_diffs:
                lines.append(f"  {d.formatted()}")
        if self.other_diffs:
            lines.append("")
            lines.append("其他变更:")
            for d in self.other_diffs:
                lines.append(f"  {d.formatted()}")
        return "\n".join(lines)


@dataclass
class TemplateRollbackPreview:
    """版本回滚预览信息"""
    template_name: str
    target_version: int
    current_version: int
    diff: TemplateDiffResult
    affected_filters: list[str] = field(default_factory=list)
    affected_updates: list[str] = field(default_factory=list)

    def formatted(self) -> str:
        lines = [f"即将回滚模板 '{self.template_name}' 到版本 {self.target_version}（当前最新版本 {self.current_version}）"]
        lines.append("=" * 60)
        if self.affected_filters:
            lines.append("")
            lines.append(f"将影响以下 {len(self.affected_filters)} 个筛选条件字段:")
            for f in self.affected_filters:
                lines.append(f"  - {f}")
        else:
            lines.append("")
            lines.append("筛选条件无变化。")
        if self.affected_updates:
            lines.append("")
            lines.append(f"将影响以下 {len(self.affected_updates)} 个批量更新字段:")
            for u in self.affected_updates:
                lines.append(f"  - {u}")
        else:
            lines.append("")
            lines.append("批量更新内容无变化。")
        lines.append("")
        lines.append(self.diff.formatted())
        return "\n".join(lines)


@dataclass
class TemplateRollbackResult:
    """版本回滚执行结果"""
    template_name: str
    template_id: str
    from_version: int
    to_version: int
    new_version_number: int
    success: bool
    error_message: str = ""

    def formatted(self) -> str:
        if self.success:
            return (f"模板 '{self.template_name}' 已成功回滚到版本 {self.to_version}。\n"
                    f"产生新版本号: {self.new_version_number}（从 v{self.from_version} 回滚）")
        return f"模板 '{self.template_name}' 回滚失败: {self.error_message}"


class TemplateVersionError(TemplateError):
    """模板版本操作错误"""
    pass


class TemplateManager:
    """批量任务模板管理器"""

    def __init__(self, db: Database, config: AppConfig):
        self.db = db
        self.config = config

    def save_template(self, name: str, description: str,
                      batch_filter: BatchFilter, batch_update: BatchUpdate,
                      conflict_strategy: Optional[str] = None,
                      overwrite: bool = False,
                      operator: str = "",
                      source_file: str = "",
                      operation_override: Optional[str] = None) -> BatchTemplate:
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

        filters_json = batch_filter.to_json()
        updates_json = batch_update.to_json()

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
                filters=filters_json,
                updates=updates_json,
                conflict_strategy=conflict_strategy,
                updated_at=now,
            )
            op_type = operation_override or TEMPLATE_VERSION_OP_OVERWRITE
            parent_version = self.db.get_next_template_version(template_id) - 1
            change_summary = self._build_change_summary(existing, description, filters_json, updates_json, conflict_strategy)
        else:
            template_id = "TPL-" + uuid.uuid4().hex[:12].upper()
            self.db.insert_template(
                template_id=template_id,
                name=name,
                description=description,
                filters=filters_json,
                updates=updates_json,
                conflict_strategy=conflict_strategy,
                created_at=now,
                updated_at=now,
            )
            op_type = operation_override or TEMPLATE_VERSION_OP_CREATE
            parent_version = 0
            change_summary = "模板新建"

        version_number = self.db.get_next_template_version(template_id)
        self.db.insert_template_version(
            template_id=template_id,
            template_name=name,
            version=version_number,
            description=description,
            filters=filters_json,
            updates=updates_json,
            conflict_strategy=conflict_strategy,
            operation_type=op_type,
            operator=operator,
            source_file=source_file,
            parent_version=parent_version,
            branch_tag="",
            change_summary=change_summary,
        )

        saved = self.db.get_template(template_id)
        if saved is None:
            raise TemplateError(f"保存模板失败: {name}")
        return saved

    def _build_change_summary(self, old: BatchTemplate, new_description: str,
                              new_filters_json: str, new_updates_json: str,
                              new_conflict_strategy: str) -> str:
        """构建两个模板版本之间的变更摘要"""
        import json as _json
        changes = []
        if old.description != new_description:
            changes.append("描述")
        if old.filters != new_filters_json:
            changes.append("筛选条件")
        if old.updates != new_updates_json:
            changes.append("更新内容")
        if old.conflict_strategy != new_conflict_strategy:
            changes.append("冲突策略")
        if not changes:
            return "内容未变更"
        return "变更: " + ", ".join(changes)

    def get_template(self, name: str) -> Optional[BatchTemplate]:
        if not name or not name.strip():
            raise TemplateError("模板名称不能为空")
        return self.db.get_template_by_name(name.strip())

    def get_template_or_error(self, name: str) -> BatchTemplate:
        template = self.get_template(name)
        if template is None:
            raise TemplateError(f"模板不存在: '{name}'。使用 template-list 查看所有模板。")
        return template

    def list_templates(self) -> list[BatchTemplate]:
        return self.db.get_all_templates()

    def copy_template(self, source_name: str, target_name: str,
                      new_description: Optional[str] = None,
                      operator: str = "") -> BatchTemplate:
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

        filter_dict = json.loads(source.filters)
        update_dict = json.loads(source.updates)
        bf = BatchFilter(
            event_ids=filter_dict.get("event_ids"),
            device_ids=filter_dict.get("device_ids"),
            statuses=filter_dict.get("statuses"),
            time_from=filter_dict.get("time_from"),
            time_to=filter_dict.get("time_to"),
        )
        bu = BatchUpdate(
            status=update_dict.get("status"),
            handler=update_dict.get("handler"),
            note=update_dict.get("note"),
        )

        return self.save_template(
            name=target_name,
            description=description,
            batch_filter=bf,
            batch_update=bu,
            conflict_strategy=source.conflict_strategy,
            operator=operator,
            operation_override=TEMPLATE_VERSION_OP_CREATE,
        )

    def delete_template(self, name: str, operator: str = "") -> bool:
        template = self.get_template(name)
        if template is None:
            return False

        version_number = self.db.get_next_template_version(template.id)
        self.db.insert_template_version(
            template_id=template.id,
            template_name=template.name,
            version=version_number,
            description=template.description,
            filters=template.filters,
            updates=template.updates,
            conflict_strategy=template.conflict_strategy,
            operation_type=TEMPLATE_VERSION_OP_DELETE_BACKUP,
            operator=operator,
            source_file="",
            parent_version=version_number - 1 if version_number > 1 else 0,
            branch_tag="",
            change_summary="删除前备份快照，可用于恢复",
        )

        self.db.delete_template(template.id)
        return True

    def template_to_objects(self, template: BatchTemplate) -> tuple[BatchFilter, BatchUpdate, str]:
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
        for fmt in formats:
            try:
                datetime.strptime(time_str, fmt)
                return True
            except ValueError:
                continue
        return False

    # ============ 导出功能 ============

    def export_template(self, name: str) -> dict[str, Any]:
        template = self.get_template_or_error(name)
        return template.to_export_dict()

    def export_templates(self, names: Optional[list[str]] = None) -> dict[str, Any]:
        if names is None:
            templates = self.list_templates()
        else:
            templates = []
            for name in names:
                tpl = self.get_template_or_error(name)
                templates.append(tpl)

        template_list = [t.to_export_dict() for t in templates]
        return {
            "version": TEMPLATE_EXPORT_VERSION,
            "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "template_count": len(template_list),
            "templates": template_list,
        }

    def export_templates_to_file(self, output_path: str,
                                 names: Optional[list[str]] = None,
                                 operator: str = "") -> TemplateExportResult:
        data = self.export_templates(names)

        os.makedirs(os.path.dirname(os.path.abspath(output_path)) or ".", exist_ok=True)

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        exported_names = [t["name"] for t in data["templates"]]

        log_id = self.db.create_template_import_log(
            operation_type="export",
            operator=operator,
            source_file=os.path.abspath(output_path),
            total_count=len(exported_names),
            conflict_strategy="n/a",
        )
        self.db.update_template_import_log_counts(
            log_id,
            success_count=len(exported_names),
            skipped_count=0,
            overwritten_count=0,
            renamed_count=0,
            error_count=0,
        )
        self.db.complete_template_import_log(log_id, TEMPLATE_IMPORT_LOG_STATUS_SUCCESS)
        for name in exported_names:
            self.db.add_template_import_item(log_id, name, name, "success", "导出成功")

        return TemplateExportResult(
            file_path=os.path.abspath(output_path),
            template_count=len(exported_names),
            template_names=exported_names,
        )

    # ============ 导入功能 ============

    def _validate_import_template_data(self, tpl_data: dict[str, Any]) -> list[str]:
        errors = []
        required_fields = ["name", "filters", "updates"]
        for field in required_fields:
            if field not in tpl_data:
                errors.append(f"缺少必填字段: {field}")

        if "name" in tpl_data:
            name = str(tpl_data["name"]).strip()
            if not name:
                errors.append("模板名称不能为空")

        if "filters" in tpl_data and not isinstance(tpl_data["filters"], dict):
            errors.append("filters 必须是字典")

        if "updates" in tpl_data and not isinstance(tpl_data["updates"], dict):
            errors.append("updates 必须是字典")

        if "conflict_strategy" in tpl_data:
            cs = tpl_data["conflict_strategy"]
            if cs not in VALID_CONFLICT_STRATEGIES:
                errors.append(
                    f"冲突策略 '{cs}' 无效，"
                    f"允许值: {', '.join(sorted(VALID_CONFLICT_STRATEGIES))}"
                )

        return errors

    def _validate_import_data(self, data: Any) -> list[dict[str, Any]]:
        if not isinstance(data, dict):
            raise TemplateImportError("导入文件格式错误：根节点必须是字典")

        if "version" not in data:
            raise TemplateImportError("导入文件格式错误：缺少 'version' 字段")
        file_version = str(data.get("version", ""))
        if not file_version.startswith(TEMPLATE_EXPORT_VERSION.split(".")[0] + "."):
            raise TemplateImportError(
                f"导入文件版本不兼容：文件版本 {file_version}，"
                f"当前支持版本 {TEMPLATE_EXPORT_VERSION}"
            )

        if "templates" not in data or not isinstance(data["templates"], list):
            raise TemplateImportError("导入文件格式错误：缺少 'templates' 列表")

        templates = data["templates"]
        if not templates:
            raise TemplateImportError("导入的模板列表为空")

        validated = []
        for idx, tpl_data in enumerate(templates):
            if not isinstance(tpl_data, dict):
                raise TemplateImportError(
                    f"第 {idx + 1} 个模板格式错误：必须是字典"
                )
            errs = self._validate_import_template_data(tpl_data)
            if errs:
                name = tpl_data.get("name", f"<第{idx + 1}个>")
                raise TemplateImportError(
                    f"模板 '{name}' 数据验证失败: " + "; ".join(errs)
                )
            validated.append(tpl_data)

        return validated

    def _resolve_rename(self, base_name: str, existing_names: set[str]) -> str:
        counter = 1
        while True:
            candidate = f"{base_name}-imported-{counter}"
            if candidate not in existing_names:
                return candidate
            counter += 1

    def import_templates_from_file(self, file_path: str,
                                   conflict_strategy: str = TEMPLATE_IMPORT_CONFLICT_SKIP,
                                   operator: str = "",
                                   validate_compatibility: bool = True,
                                   rollback_on_error: bool = True) -> TemplateImportResult:
        if conflict_strategy not in VALID_TEMPLATE_IMPORT_CONFLICT_STRATEGIES:
            raise TemplateImportError(
                f"无效的冲突策略: {conflict_strategy}，"
                f"允许值: {', '.join(sorted(VALID_TEMPLATE_IMPORT_CONFLICT_STRATEGIES))}"
            )

        if not os.path.exists(file_path):
            raise TemplateImportError(f"导入文件不存在: {file_path}")

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                raw_data = json.load(f)
        except json.JSONDecodeError as e:
            raise TemplateImportError(f"导入文件 JSON 格式错误: {e}") from e
        except Exception as e:
            raise TemplateImportError(f"读取导入文件失败: {e}") from e

        templates_data = self._validate_import_data(raw_data)

        return self._do_import(
            templates_data=templates_data,
            source_file=os.path.abspath(file_path),
            conflict_strategy=conflict_strategy,
            operator=operator,
            validate_compatibility=validate_compatibility,
            rollback_on_error=rollback_on_error,
        )

    def _do_import(self, templates_data: list[dict[str, Any]],
                   source_file: str,
                   conflict_strategy: str,
                   operator: str,
                   validate_compatibility: bool,
                   rollback_on_error: bool) -> TemplateImportResult:
        result = TemplateImportResult(total_count=len(templates_data))

        result.log_id = self.db.create_template_import_log(
            operation_type="import",
            operator=operator,
            source_file=source_file,
            total_count=len(templates_data),
            conflict_strategy=conflict_strategy,
        )

        created_names: list[str] = []
        overwritten_originals: dict[str, BatchTemplate] = {}

        try:
            existing_names = {t.name for t in self.list_templates()}

            for tpl_data in templates_data:
                original_name = str(tpl_data["name"]).strip()
                final_name = original_name
                item_status = "success"
                item_reason = ""

                if original_name in existing_names:
                    if conflict_strategy == TEMPLATE_IMPORT_CONFLICT_SKIP:
                        result.skipped_count += 1
                        item_status = "skipped"
                        item_reason = "名称已存在，使用 skip 策略跳过"
                        result.items.append(TemplateImportItemResult(
                            original_name=original_name,
                            final_name=final_name,
                            status=item_status,
                            reason=item_reason,
                        ))
                        self.db.add_template_import_item(
                            result.log_id, original_name, final_name,
                            item_status, item_reason
                        )
                        continue
                    elif conflict_strategy == TEMPLATE_IMPORT_CONFLICT_OVERWRITE:
                        original_tpl = self.get_template(original_name)
                        if original_tpl:
                            overwritten_originals[original_name] = original_tpl
                        item_status = "overwritten"
                        item_reason = "名称已存在，使用 overwrite 策略覆盖"
                        result.overwritten_count += 1
                    elif conflict_strategy == TEMPLATE_IMPORT_CONFLICT_RENAME:
                        final_name = self._resolve_rename(original_name, existing_names)
                        item_status = "renamed"
                        item_reason = f"名称已存在，自动重命名为 '{final_name}'"
                        result.renamed_count += 1

                filters_json = json.dumps(tpl_data["filters"], ensure_ascii=False)
                updates_json = json.dumps(tpl_data["updates"], ensure_ascii=False)
                tpl_cs = tpl_data.get("conflict_strategy", self.config.batch.conflict_strategy)
                if tpl_cs not in VALID_CONFLICT_STRATEGIES:
                    tpl_cs = self.config.batch.conflict_strategy

                temp_template = BatchTemplate(
                    id="",
                    name=final_name,
                    description=str(tpl_data.get("description", "")),
                    filters=filters_json,
                    updates=updates_json,
                    conflict_strategy=tpl_cs,
                    created_at="",
                    updated_at="",
                )

                if validate_compatibility:
                    validation = self.validate_template(temp_template)
                    if not validation.is_valid:
                        result.error_count += 1
                        err_msgs = [str(e) for e in validation.errors]
                        item_status = "error"
                        item_reason = "兼容性校验失败: " + "; ".join(err_msgs)
                        result.items.append(TemplateImportItemResult(
                            original_name=original_name,
                            final_name=final_name,
                            status=item_status,
                            reason=item_reason,
                        ))
                        self.db.add_template_import_item(
                            result.log_id, original_name, final_name,
                            item_status, item_reason
                        )
                        continue

                description = str(tpl_data.get("description", ""))
                bf_dict = tpl_data["filters"]
                bu_dict = tpl_data["updates"]
                batch_filter = BatchFilter(
                    event_ids=bf_dict.get("event_ids"),
                    device_ids=bf_dict.get("device_ids"),
                    statuses=bf_dict.get("statuses"),
                    time_from=bf_dict.get("time_from"),
                    time_to=bf_dict.get("time_to"),
                )
                batch_update = BatchUpdate(
                    status=bu_dict.get("status"),
                    handler=bu_dict.get("handler"),
                    note=bu_dict.get("note"),
                )

                try:
                    op_override = (TEMPLATE_VERSION_OP_OVERWRITE
                                   if item_status == "overwritten"
                                   else TEMPLATE_VERSION_OP_IMPORT)
                    self.save_template(
                        name=final_name,
                        description=description,
                        batch_filter=batch_filter,
                        batch_update=batch_update,
                        conflict_strategy=tpl_cs,
                        overwrite=(item_status == "overwritten"),
                        operator=operator,
                        source_file=source_file,
                        operation_override=op_override,
                    )
                    result.success_count += 1
                    if item_status == "overwritten":
                        item_reason += "，覆盖成功"
                    elif item_status == "renamed":
                        item_reason += "，导入成功"
                    else:
                        item_status = "success"
                        item_reason = "导入成功"
                    existing_names.add(final_name)
                    if item_status != "overwritten":
                        created_names.append(final_name)
                except TemplateError as e:
                    result.error_count += 1
                    item_status = "error"
                    item_reason = f"保存失败: {e}"

                result.items.append(TemplateImportItemResult(
                    original_name=original_name,
                    final_name=final_name,
                    status=item_status,
                    reason=item_reason,
                ))
                self.db.add_template_import_item(
                    result.log_id, original_name, final_name,
                    item_status, item_reason
                )

            if result.error_count > 0 and rollback_on_error:
                if created_names:
                    self.db.delete_templates_by_names(created_names)
                for orig_name, orig_tpl in overwritten_originals.items():
                    try:
                        bf_orig, bu_orig, cs_orig = self.template_to_objects(orig_tpl)
                        self.save_template(
                            name=orig_name,
                            description=orig_tpl.description,
                            batch_filter=bf_orig,
                            batch_update=bu_orig,
                            conflict_strategy=cs_orig,
                            overwrite=True,
                        )
                    except Exception:
                        pass
                rollback_total = len(created_names) + len(overwritten_originals)
                result.rolled_back = True
                result.status = TEMPLATE_IMPORT_LOG_STATUS_ROLLED_BACK
                result.error_message = (
                    f"检测到 {result.error_count} 个错误，已回滚所有变更。"
                    f"回滚了 {rollback_total} 个模板。"
                )
                self.db.complete_template_import_log(
                    result.log_id, TEMPLATE_IMPORT_LOG_STATUS_ROLLED_BACK,
                    result.error_message
                )
            else:
                if result.error_count == 0:
                    result.status = TEMPLATE_IMPORT_LOG_STATUS_SUCCESS
                else:
                    result.status = TEMPLATE_IMPORT_LOG_STATUS_PARTIAL
                self.db.complete_template_import_log(result.log_id, result.status)

        except Exception as e:
            if rollback_on_error:
                if created_names:
                    self.db.delete_templates_by_names(created_names)
                for orig_name, orig_tpl in overwritten_originals.items():
                    try:
                        bf_orig, bu_orig, cs_orig = self.template_to_objects(orig_tpl)
                        self.save_template(
                            name=orig_name,
                            description=orig_tpl.description,
                            batch_filter=bf_orig,
                            batch_update=bu_orig,
                            conflict_strategy=cs_orig,
                            overwrite=True,
                        )
                    except Exception:
                        pass
                result.rolled_back = True
            result.error_count = max(result.error_count, 1)
            result.status = TEMPLATE_IMPORT_LOG_STATUS_FAILED
            result.error_message = f"导入过程发生意外错误: {e}"
            self.db.complete_template_import_log(
                result.log_id, TEMPLATE_IMPORT_LOG_STATUS_FAILED,
                result.error_message
            )

        self.db.update_template_import_log_counts(
            result.log_id,
            success_count=result.success_count,
            skipped_count=result.skipped_count,
            overwritten_count=result.overwritten_count,
            renamed_count=result.renamed_count,
            error_count=result.error_count,
        )

        return result

    # ============ 导入日志查看 ============

    def get_template_import_logs(self, limit: int = 20) -> str:
        logs = self.db.get_recent_template_import_logs(limit)
        if not logs:
            return "暂无模板导入导出日志。"

        status_labels = {
            TEMPLATE_IMPORT_LOG_STATUS_SUCCESS: "成功",
            TEMPLATE_IMPORT_LOG_STATUS_PARTIAL: "部分完成",
            TEMPLATE_IMPORT_LOG_STATUS_FAILED: "失败",
            TEMPLATE_IMPORT_LOG_STATUS_ROLLED_BACK: "已回滚",
            "pending": "进行中",
        }
        op_labels = {
            "import": "导入",
            "export": "导出",
        }

        lines = [f"最近 {len(logs)} 条模板导入导出日志:"]
        lines.append("")
        header = (f"{'日志ID':<22} {'操作':<6} {'状态':<8} {'操作人':<10} "
                  f"{'总数':<6} {'成功':<6} {'跳过':<6} {'覆盖':<6} "
                  f"{'重命名':<6} {'错误':<6} {'创建时间':<20}")
        lines.append(header)
        lines.append("-" * len(header))

        for log in logs:
            op = op_labels.get(log.get("operation_type", ""), log.get("operation_type", ""))
            st = status_labels.get(log.get("status", ""), log.get("status", ""))
            operator = log.get("operator", "") or "-"
            lines.append(
                f"{log.get('id', ''):<22} {op:<6} {st:<8} {operator:<10} "
                f"{log.get('total_count', 0):<6} {log.get('success_count', 0):<6} "
                f"{log.get('skipped_count', 0):<6} {log.get('overwritten_count', 0):<6} "
                f"{log.get('renamed_count', 0):<6} {log.get('error_count', 0):<6} "
                f"{log.get('created_at', ''):<20}"
            )

        return "\n".join(lines)

    def get_template_import_log_detail(self, log_id: str) -> str:
        log = self.db.get_template_import_log(log_id)
        if not log:
            return f"日志不存在: {log_id}"

        items = self.db.get_template_import_items(log_id)

        status_labels = {
            TEMPLATE_IMPORT_LOG_STATUS_SUCCESS: "成功",
            TEMPLATE_IMPORT_LOG_STATUS_PARTIAL: "部分完成",
            TEMPLATE_IMPORT_LOG_STATUS_FAILED: "失败",
            TEMPLATE_IMPORT_LOG_STATUS_ROLLED_BACK: "已回滚",
            "pending": "进行中",
        }
        op_labels = {
            "import": "导入",
            "export": "导出",
        }
        item_status_labels = {
            "success": "成功",
            "skipped": "跳过",
            "overwritten": "覆盖",
            "renamed": "重命名",
            "error": "错误",
        }

        lines = [f"模板导入导出日志详情: {log_id}"]
        lines.append("=" * 60)
        lines.append(f"操作类型: {op_labels.get(log.get('operation_type', ''), log.get('operation_type', ''))}")
        lines.append(f"状态: {status_labels.get(log.get('status', ''), log.get('status', ''))}")
        if log.get("operator"):
            lines.append(f"操作人: {log['operator']}")
        if log.get("source_file"):
            lines.append(f"来源文件: {log['source_file']}")
        lines.append(f"冲突策略: {log.get('conflict_strategy', '')}")
        lines.append(f"创建时间: {log.get('created_at', '')}")
        if log.get("completed_at"):
            lines.append(f"完成时间: {log['completed_at']}")
        lines.append("")
        lines.append(
            f"总计: {log.get('total_count', 0)} | "
            f"成功: {log.get('success_count', 0)} | "
            f"跳过: {log.get('skipped_count', 0)} | "
            f"覆盖: {log.get('overwritten_count', 0)} | "
            f"重命名: {log.get('renamed_count', 0)} | "
            f"错误: {log.get('error_count', 0)}"
        )
        if log.get("error_message"):
            lines.append(f"错误信息: {log['error_message']}")

        if items:
            lines.append("")
            lines.append("各模板处理详情:")
            lines.append("-" * 60)
            for item in items:
                st = item_status_labels.get(item.get("status", ""), item.get("status", ""))
                line = f"[{st}] {item.get('template_name', '')}"
                if item.get("template_name") != item.get("final_name"):
                    line += f" → {item.get('final_name', '')}"
                if item.get("reason"):
                    line += f"  ({item['reason']})"
                lines.append(line)

        return "\n".join(lines)

    # ============ 版本历史查询 ============

    def list_template_versions(self, name: str) -> list[TemplateVersion]:
        """获取指定模板的所有版本历史（包括同名已删除分支，按版本号降序）"""
        template = self.get_template(name)
        all_versions = self.db.get_template_versions_by_name(name)
        if not all_versions and template is None:
            raise TemplateVersionError(
                f"模板不存在: '{name}'。使用 template-list 查看所有模板。"
            )
        if template is not None:
            current_versions = self.db.get_template_versions(template.id)
            existing_ids = {v.id for v in current_versions}
            for v in all_versions:
                if v.id not in existing_ids:
                    current_versions.append(v)
            current_versions.sort(key=lambda v: v.version, reverse=True)
            return current_versions
        all_versions.sort(key=lambda v: v.version, reverse=True)
        return all_versions

    def format_template_versions(self, versions: list[TemplateVersion]) -> str:
        """格式化版本列表输出"""
        if not versions:
            return "该模板暂无版本历史记录。"

        op_labels = {
            TEMPLATE_VERSION_OP_CREATE: "新建",
            TEMPLATE_VERSION_OP_UPDATE: "修改",
            TEMPLATE_VERSION_OP_OVERWRITE: "覆盖",
            TEMPLATE_VERSION_OP_IMPORT: "导入",
            TEMPLATE_VERSION_OP_DELETE_BACKUP: "删除备份",
            TEMPLATE_VERSION_OP_ROLLBACK: "回滚",
        }

        template_name = versions[0].template_name
        template_ids = sorted({v.template_id for v in versions})
        id_info = "" if len(template_ids) == 1 else f"（{len(template_ids)} 个历史分叉）"
        lines = [f"模板 '{template_name}' 共 {len(versions)} 个版本历史{id_info}:"]
        lines.append("")
        header = (f"{'版本':<8} {'操作':<8} {'操作人':<12} "
                  f"{'快照时间':<20} 变更摘要")
        lines.append(header)
        lines.append("-" * len(header))

        for v in versions:
            op = op_labels.get(v.operation_type, v.operation_type)
            operator = v.operator or "-"
            ver_str = f"v{v.version}"
            branch_tag = f" [{v.branch_tag}]" if v.branch_tag else ""
            lines.append(
                f"{ver_str:<8} {op:<8} {operator:<12} "
                f"{v.snapshot_at:<20} {v.change_summary}{branch_tag}"
            )

        return "\n".join(lines)

    # ============ 版本差异对比 ============

    def _version_to_dicts(self, version: TemplateVersion) -> tuple[dict, dict]:
        """将版本快照的 filters/updates JSON 转为字典"""
        return json.loads(version.filters), json.loads(version.updates)

    def _template_to_dicts(self, template: BatchTemplate) -> tuple[dict, dict]:
        """将模板的 filters/updates JSON 转为字典"""
        return json.loads(template.filters), json.loads(template.updates)

    def diff_template_versions(self, name: str, version_a: int,
                               version_b: int) -> TemplateDiffResult:
        """对比指定模板的两个版本差异（支持跨分支版本）"""
        va = self._find_version_by_number(name, version_a)
        vb = self._find_version_by_number(name, version_b)

        fa, ua = self._version_to_dicts(va)
        fb, ub = self._version_to_dicts(vb)

        return self._build_diff(name, version_a, version_b, fa, ua, va, fb, ub, vb)

    def diff_template_version_with_current(self, name: str,
                                           version: int) -> TemplateDiffResult:
        """对比指定版本与当前模板的差异（支持跨分支版本）"""
        template = self.get_template_or_error(name)
        v = self._find_version_by_number(name, version)

        f_current, u_current = self._template_to_dicts(template)
        f_old, u_old = self._version_to_dicts(v)
        return self._build_diff(name, version, 0, f_old, u_old, v,
                                f_current, u_current, None)

    def _build_diff(self, name: str, ver_a: int, ver_b: int,
                    fa: dict, ua: dict, va_obj: Optional[TemplateVersion],
                    fb: dict, ub: dict, vb_obj: Optional[TemplateVersion]) -> TemplateDiffResult:
        """构建两个版本之间的差异"""
        result = TemplateDiffResult(
            template_name=name,
            old_version=ver_a,
            new_version=ver_b if ver_b > 0 else -1,
        )

        filter_fields = [
            ("event_ids", "事件ID"),
            ("device_ids", "设备编号"),
            ("statuses", "状态"),
            ("time_from", "起始时间"),
            ("time_to", "结束时间"),
        ]
        for key, label in filter_fields:
            if fa.get(key) != fb.get(key):
                result.filter_diffs.append(TemplateFieldDiff(
                    field=label,
                    old_value=fa.get(key),
                    new_value=fb.get(key),
                ))

        update_fields = [
            ("status", "目标状态"),
            ("handler", "处理人"),
            ("note", "备注"),
        ]
        for key, label in update_fields:
            if ua.get(key) != ub.get(key):
                result.update_diffs.append(TemplateFieldDiff(
                    field=label,
                    old_value=ua.get(key),
                    new_value=ub.get(key),
                ))

        desc_a = va_obj.description if va_obj else ""
        desc_b = vb_obj.description if vb_obj else ""
        if desc_a != desc_b:
            result.other_diffs.append(TemplateFieldDiff(
                field="描述", old_value=desc_a, new_value=desc_b,
            ))

        cs_a = va_obj.conflict_strategy if va_obj else ""
        cs_b = vb_obj.conflict_strategy if vb_obj else ""
        if cs_a != cs_b:
            result.other_diffs.append(TemplateFieldDiff(
                field="冲突策略", old_value=cs_a, new_value=cs_b,
            ))

        return result

    # ============ 版本兼容性检查 ============

    def validate_version_for_rollback(self, version: TemplateVersion
                                      ) -> TemplateValidationResult:
        """验证某个历史版本是否与当前配置兼容（用于回滚前检查）"""
        temp_template = BatchTemplate(
            id=version.template_id,
            name=version.template_name,
            description=version.description,
            filters=version.filters,
            updates=version.updates,
            conflict_strategy=version.conflict_strategy,
            created_at="",
            updated_at="",
        )
        return self.validate_template(temp_template)

    def check_name_branch_conflict(self, template_name: str,
                                   target_version: TemplateVersion) -> Optional[str]:
        """检查同名模板分叉冲突。如果目标版本来自另一个 template_id 但同名，返回处理建议"""
        all_versions = self.db.get_template_versions_by_name(template_name)
        if not all_versions:
            return None

        current = self.get_template(template_name)
        if current is None:
            existing_template_ids = {v.template_id for v in all_versions}
            if len(existing_template_ids) > 1:
                ids_list = ", ".join(sorted(existing_template_ids))
                return (
                    f"发现同名模板 '{template_name}' 存在多个历史分叉（template_id: {ids_list}）。\n"
                    f"处理建议：\n"
                    f"  1. 使用 template-copy 为目标版本创建新名称的副本\n"
                    f"  2. 先删除当前模板（版本历史保留），再使用目标版本号回滚\n"
                    f"  3. 明确指定要基于哪个 template_id 的版本回滚（当前不支持，请使用方案1或2）"
                )
            return None

        if current.id != target_version.template_id:
            return (
                f"目标版本（template_id={target_version.template_id}）"
                f"与当前模板（template_id={current.id}）"
                f"名称相同但来源不同，存在同名分叉。\n"
                f"处理建议：\n"
                f"  1. 使用 template-copy 将目标版本内容复制为新名称的模板\n"
                f"  2. 先 template-delete 当前模板（版本历史保留），再回滚\n"
                f"  3. 放弃回滚，手动修改当前模板以匹配目标版本内容"
            )
        return None

    # ============ 版本回滚 ============

    def _find_version_by_number(self, name: str, target_version: int) -> TemplateVersion:
        """从所有同名版本中查找指定版本号（跨分支）。
        若多个分支有相同版本号，抛出歧义错误，提示处理建议。"""
        all_versions = self.db.get_template_versions_by_name(name)
        if not all_versions:
            raise TemplateVersionError(
                f"模板 '{name}' 的版本历史为空。"
                f"使用 template-list 查看所有模板。"
            )
        matches = [v for v in all_versions if v.version == target_version]
        if not matches:
            available = sorted({v.version for v in all_versions})
            raise TemplateVersionError(
                f"模板 '{name}' 的版本 {target_version} 不存在。"
                f"可用版本: {available}。"
                f"使用 template-versions {name} 查看详细列表。"
            )
        current = self.get_template(name)
        match_template_ids = {v.template_id for v in matches}
        if len(match_template_ids) > 1:
            ids_list = ", ".join(sorted(match_template_ids))
            raise TemplateVersionError(
                f"版本 {target_version} 在多个同名分叉中同时存在（template_id: {ids_list}），存在歧义。\n"
                f"处理建议：\n"
                f"  1. 使用 template-versions {name} 查看各分支版本详情\n"
                f"  2. 若想基于旧分支版本恢复，先 template-delete 当前模板（版本历史保留），再执行回滚\n"
                f"  3. 使用 template-copy 将目标内容复制为新名称的模板"
            )
        if current is not None and current.id not in match_template_ids:
            only_match = matches[0]
            raise TemplateVersionError(
                f"目标版本 v{target_version}（template_id={only_match.template_id}）"
                f"与当前模板（template_id={current.id}）名称相同但属于不同分叉。\n"
                f"处理建议：\n"
                f"  1. 先 template-delete 当前模板（版本历史保留），再执行回滚\n"
                f"  2. 使用 template-copy 将目标版本内容复制为新名称的模板"
            )
        return matches[0]

    def preview_rollback(self, name: str, target_version: int
                         ) -> TemplateRollbackPreview:
        """预览回滚到指定版本的影响"""
        template = self.get_template_or_error(name)
        version_obj = self._find_version_by_number(name, target_version)

        validation = self.validate_version_for_rollback(version_obj)
        if not validation.is_valid:
            raise TemplateVersionError(
                f"目标版本与当前配置不兼容，无法回滚：\n{validation.formatted()}\n"
                f"处理建议：\n"
                f"  1. 修复兼容性问题后重新保存该版本\n"
                f"  2. 选择其他兼容的历史版本\n"
                f"  3. 使用 --no-validate 跳过兼容性检查（不推荐，可能导致模板无法执行）"
            )

        conflict_msg = self.check_name_branch_conflict(name, version_obj)
        if conflict_msg:
            raise TemplateVersionError(conflict_msg)

        current_max = self.db.get_next_template_version(template.id) - 1
        diff = self.diff_template_versions(name, target_version, current_max)

        affected_filters = [d.field for d in diff.filter_diffs]
        affected_updates = [d.field for d in diff.update_diffs]

        return TemplateRollbackPreview(
            template_name=name,
            target_version=target_version,
            current_version=current_max,
            diff=diff,
            affected_filters=affected_filters,
            affected_updates=affected_updates,
        )

    def rollback_template(self, name: str, target_version: int,
                          operator: str = "",
                          validate_compatibility: bool = True
                          ) -> TemplateRollbackResult:
        """将模板回滚到指定历史版本"""
        template = self.get_template_or_error(name)

        try:
            version_obj = self._find_version_by_number(name, target_version)
        except TemplateVersionError as e:
            return TemplateRollbackResult(
                template_name=name,
                template_id=template.id,
                from_version=0,
                to_version=target_version,
                new_version_number=0,
                success=False,
                error_message=str(e),
            )

        if validate_compatibility:
            validation = self.validate_version_for_rollback(version_obj)
            if not validation.is_valid:
                return TemplateRollbackResult(
                    template_name=name,
                    template_id=template.id,
                    from_version=0,
                    to_version=target_version,
                    new_version_number=0,
                    success=False,
                    error_message=f"目标版本与当前配置不兼容：{validation.formatted()}",
                )

        conflict_msg = self.check_name_branch_conflict(name, version_obj)
        if conflict_msg:
            return TemplateRollbackResult(
                template_name=name,
                template_id=template.id,
                from_version=0,
                to_version=target_version,
                new_version_number=0,
                success=False,
                error_message=conflict_msg,
            )

        filter_dict = json.loads(version_obj.filters)
        update_dict = json.loads(version_obj.updates)
        bf = BatchFilter(
            event_ids=filter_dict.get("event_ids"),
            device_ids=filter_dict.get("device_ids"),
            statuses=filter_dict.get("statuses"),
            time_from=filter_dict.get("time_from"),
            time_to=filter_dict.get("time_to"),
        )
        bu = BatchUpdate(
            status=update_dict.get("status"),
            handler=update_dict.get("handler"),
            note=update_dict.get("note"),
        )

        current_max = self.db.get_next_template_version(template.id) - 1

        try:
            self.save_template(
                name=name,
                description=version_obj.description,
                batch_filter=bf,
                batch_update=bu,
                conflict_strategy=version_obj.conflict_strategy,
                overwrite=True,
                operator=operator,
                operation_override=TEMPLATE_VERSION_OP_ROLLBACK,
            )
        except TemplateError as e:
            return TemplateRollbackResult(
                template_name=name,
                template_id=template.id,
                from_version=current_max,
                to_version=target_version,
                new_version_number=0,
                success=False,
                error_message=str(e),
            )

        new_version = self.db.get_next_template_version(template.id) - 1

        return TemplateRollbackResult(
            template_name=name,
            template_id=template.id,
            from_version=current_max,
            to_version=target_version,
            new_version_number=new_version,
            success=True,
        )

    # ============ 删除后恢复 ============

    def restore_deleted_template(self, name: str, operator: str = ""
                                 ) -> Optional[BatchTemplate]:
        """从删除前的备份快照恢复已删除的模板（复用原 template_id）"""
        versions = self.db.get_template_versions_by_name(name)
        delete_versions = [v for v in versions
                           if v.operation_type == TEMPLATE_VERSION_OP_DELETE_BACKUP]
        if not delete_versions:
            return None

        latest = delete_versions[0]
        filter_dict = json.loads(latest.filters)
        update_dict = json.loads(latest.updates)
        bf = BatchFilter(
            event_ids=filter_dict.get("event_ids"),
            device_ids=filter_dict.get("device_ids"),
            statuses=filter_dict.get("statuses"),
            time_from=filter_dict.get("time_from"),
            time_to=filter_dict.get("time_to"),
        )
        bu = BatchUpdate(
            status=update_dict.get("status"),
            handler=update_dict.get("handler"),
            note=update_dict.get("note"),
        )

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        original_id = latest.template_id
        self.db.insert_template(
            template_id=original_id,
            name=name,
            description=latest.description,
            filters=bf.to_json(),
            updates=bu.to_json(),
            conflict_strategy=latest.conflict_strategy,
            created_at=now,
            updated_at=now,
        )

        version_number = self.db.get_next_template_version(original_id)
        self.db.insert_template_version(
            template_id=original_id,
            template_name=name,
            version=version_number,
            description=latest.description,
            filters=bf.to_json(),
            updates=bu.to_json(),
            conflict_strategy=latest.conflict_strategy,
            operation_type=TEMPLATE_VERSION_OP_ROLLBACK,
            operator=operator,
            source_file="",
            parent_version=latest.version,
            branch_tag="",
            change_summary=f"从删除备份恢复（来源版本 v{latest.version}）",
        )

        restored = self.db.get_template(original_id)
        if restored is None:
            raise TemplateError(f"恢复模板失败: {name}")
        return restored
