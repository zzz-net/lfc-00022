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


class TemplateManager:
    """批量任务模板管理器"""

    def __init__(self, db: Database, config: AppConfig):
        self.db = db
        self.config = config

    def save_template(self, name: str, description: str,
                      batch_filter: BatchFilter, batch_update: BatchUpdate,
                      conflict_strategy: Optional[str] = None,
                      overwrite: bool = False) -> BatchTemplate:
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
                      new_description: Optional[str] = None) -> BatchTemplate:
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
        template = self.get_template(name)
        if template is None:
            return False
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
                    self.save_template(
                        name=final_name,
                        description=description,
                        batch_filter=batch_filter,
                        batch_update=batch_update,
                        conflict_strategy=tpl_cs,
                        overwrite=(item_status == "overwritten"),
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
