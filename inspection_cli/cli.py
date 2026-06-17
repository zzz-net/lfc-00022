"""CLI 入口：基于 Click 构建完整命令链"""
from __future__ import annotations

import os
import sys

import click

from . import __version__
from .annotation import AnnotationError, AnnotationManager
from .batch import BatchOperationError, BatchOperationManager, BatchFilter, BatchUpdate
from .config import AppConfig, ConfigError
from .database import (
    CONFLICT_STRATEGY_ABORT, CONFLICT_STRATEGY_FORCE, CONFLICT_STRATEGY_SKIP,
    Database, VALID_STATUSES,
)
from .exporter import Exporter
from .importer import RecordImporter
from .merger import EventMerger
from .database import (
    TEMPLATE_IMPORT_CONFLICT_OVERWRITE, TEMPLATE_IMPORT_CONFLICT_RENAME,
    TEMPLATE_IMPORT_CONFLICT_SKIP,
)
from .templates import (
    TemplateError, TemplateImportError, TemplateManager,
    TemplateVersionError,
)


class CliContext:
    """CLI 上下文对象"""

    def __init__(self, config_path: str | None):
        try:
            self.config = AppConfig.load(config_path)
        except ConfigError as e:
            click.echo(f"错误: {e}", err=True)
            click.echo("提示: 配置错误时不会清空已有数据，请修复配置后重试。", err=True)
            sys.exit(1)

        self.db = Database(self.config.db_path)
        self.importer = RecordImporter(self.db, self.config)
        self.merger = EventMerger(self.db, self.config)
        self.annotation_manager = AnnotationManager(self.db)
        self.batch_manager = BatchOperationManager(self.db, self.config)
        self.exporter = Exporter(self.db, self.config)
        self.template_manager = TemplateManager(self.db, self.config)


pass_ctx = click.make_pass_decorator(CliContext)


@click.group(help="本地巡检记录整理 CLI 工具")
@click.version_option(__version__, "-V", "--version")
@click.option("-c", "--config", "config_path", type=click.Path(dir_okay=False),
              default=None, help="规则配置文件 (YAML)")
@click.pass_context
def main(ctx: click.Context, config_path: str | None) -> None:
    """巡检记录整理 CLI 主入口"""
    ctx.obj = CliContext(config_path)


@main.command("import", help="导入 CSV 或 JSON 巡检记录")
@click.argument("files", nargs=-1, type=click.Path(exists=True, dir_okay=False), required=True)
@pass_ctx
def cmd_import(ctx: CliContext, files: tuple[str, ...]) -> None:
    """导入巡检记录文件"""
    total_imported = 0
    total_duplicates = 0
    total_errors = 0
    has_validation_errors = False

    for file_path in files:
        click.echo(f"处理文件: {file_path}")
        result = ctx.importer.import_file(file_path)
        click.echo(result.formatted())
        click.echo()

        total_imported += result.imported
        total_duplicates += result.duplicates
        total_errors += result.errors
        if not result.validation_result.is_valid or result.error_messages:
            has_validation_errors = True

    click.echo("===== 汇总 =====")
    click.echo(f"新增导入: {total_imported}")
    click.echo(f"重复跳过: {total_duplicates}")
    click.echo(f"校验错误: {total_errors}")

    if has_validation_errors:
        click.echo()
        click.echo("注意: 存在错误的记录未被导入，请修正后重新导入。")
        click.echo("      已导入的有效记录不受影响。")


@main.command("merge", help="按设备与时间窗口归并相近异常为事件")
@click.option("--no-preserve", is_flag=True, default=False,
              help="不保留已有标注状态（默认保留）")
@pass_ctx
def cmd_merge(ctx: CliContext, no_preserve: bool) -> None:
    """执行事件归并"""
    result = ctx.merger.merge(preserve_annotations=not no_preserve)
    click.echo(result.formatted())


@main.command("list", help="列出所有事件")
@pass_ctx
def cmd_list(ctx: CliContext) -> None:
    """列出事件"""
    click.echo(ctx.exporter.list_events())


@main.command("annotate", help="标注事件状态")
@click.argument("event_id")
@click.argument("status",
                type=click.Choice(["confirmed", "false_positive", "closed"], case_sensitive=False))
@click.option("-H", "--handler", required=True, help="处理人姓名")
@click.option("-n", "--note", default="", help="备注信息")
@pass_ctx
def cmd_annotate(ctx: CliContext, event_id: str, status: str, handler: str, note: str) -> None:
    """标注事件"""
    try:
        result = ctx.annotation_manager.annotate(
            event_id=event_id,
            status=status.lower(),
            handler=handler,
            note=note,
        )
        click.echo(result.formatted())
    except AnnotationError as e:
        click.echo(f"错误: {e}", err=True)
        sys.exit(1)


@main.command("undo", help="撤销事件的最后一次标注")
@click.argument("event_id")
@pass_ctx
def cmd_undo(ctx: CliContext, event_id: str) -> None:
    """撤销标注"""
    try:
        result = ctx.annotation_manager.undo(event_id)
        click.echo(result.formatted())
    except AnnotationError as e:
        click.echo(f"错误: {e}", err=True)
        sys.exit(1)


@main.command("export", help="导出事件汇总")
@click.argument("output_path", type=click.Path(dir_okay=False))
@click.option("-f", "--format", "fmt",
              type=click.Choice(["csv", "json"], case_sensitive=False),
              default=None, help="导出格式（默认按文件后缀推断）")
@click.option("--with-records", is_flag=True, default=False,
              help="JSON 导出时包含来源记录详情")
@pass_ctx
def cmd_export(ctx: CliContext, output_path: str, fmt: str | None, with_records: bool) -> None:
    """导出事件"""
    if fmt:
        fmt = fmt.lower()
    result = ctx.exporter.export_events(output_path, fmt=fmt, include_records=with_records)
    click.echo(result.formatted())


@main.command("statuses", help="列出所有可用的事件状态")
@pass_ctx
def cmd_statuses(ctx: CliContext) -> None:
    """列出可用状态"""
    click.echo(ctx.annotation_manager.list_statuses())


@main.command("init-demo", help="使用样例配置快速演示完整流程（导入→归并→标注→导出）")
@click.option("-c", "--config", "demo_config", type=click.Path(dir_okay=False),
              default=None, help="自定义配置文件路径")
@click.option("--csv", "demo_csv", type=click.Path(dir_okay=False),
              default=None, help="自定义 CSV 文件")
@click.option("--json", "demo_json", type=click.Path(dir_okay=False),
              default=None, help="自定义 JSON 文件")
@click.option("-H", "--handler", default="demo-user", help="演示用处理人")
@pass_ctx
def cmd_demo(ctx: CliContext, demo_config: str | None, demo_csv: str | None,
             demo_json: str | None, handler: str) -> None:
    """演示完整流程"""
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    samples_dir = os.path.join(base_dir, "samples")

    csv_file = demo_csv or os.path.join(samples_dir, "inspection_sample.csv")
    json_file = demo_json or os.path.join(samples_dir, "inspection_sample.json")

    click.echo("=" * 60)
    click.echo("步骤 1/5: 导入 CSV 巡检记录")
    click.echo("=" * 60)
    if os.path.exists(csv_file):
        result = ctx.importer.import_file(csv_file)
        click.echo(result.formatted())
    else:
        click.echo(f"跳过: CSV 文件不存在 ({csv_file})")
    click.echo()

    click.echo("=" * 60)
    click.echo("步骤 2/5: 导入 JSON 巡检记录")
    click.echo("=" * 60)
    if os.path.exists(json_file):
        result = ctx.importer.import_file(json_file)
        click.echo(result.formatted())
    else:
        click.echo(f"跳过: JSON 文件不存在 ({json_file})")
    click.echo()

    click.echo("=" * 60)
    click.echo("步骤 3/5: 归并事件")
    click.echo("=" * 60)
    result = ctx.merger.merge(preserve_annotations=True)
    click.echo(result.formatted())
    click.echo()

    click.echo("=" * 60)
    click.echo("步骤 4/5: 标注事件（演示）")
    click.echo("=" * 60)
    events = ctx.db.get_all_events()
    if events:
        demo_event = events[0]
        try:
            ann_result = ctx.annotation_manager.annotate(
                event_id=demo_event.id,
                status="confirmed",
                handler=handler,
                note="演示标注 - 已确认",
            )
            click.echo(ann_result.formatted())
        except AnnotationError as e:
            click.echo(f"标注跳过: {e}")
    else:
        click.echo("无事件可标注")
    click.echo()

    click.echo("=" * 60)
    click.echo("步骤 5/5: 导出结果")
    click.echo("=" * 60)
    csv_out = os.path.join(os.getcwd(), "events_export.csv")
    json_out = os.path.join(os.getcwd(), "events_export.json")

    r1 = ctx.exporter.export_events(csv_out, fmt="csv")
    click.echo(r1.formatted())
    r2 = ctx.exporter.export_events(json_out, fmt="json", include_records=True)
    click.echo(r2.formatted())

    click.echo()
    click.echo("演示完成！使用 `inspection-cli list` 查看事件列表。")


def _parse_csv_list(ctx, param, value):
    """解析逗号分隔的列表参数"""
    if value is None:
        return None
    return [item.strip() for item in value.split(",") if item.strip()]


@main.command("batch-annotate", help="批量标注事件状态（支持筛选、预览、版本冲突检测）")
@click.option("--event-ids", callback=_parse_csv_list, default=None,
              help="按事件ID筛选，多个ID用逗号分隔")
@click.option("--device-ids", callback=_parse_csv_list, default=None,
              help="按设备编号筛选，多个编号用逗号分隔")
@click.option("--statuses", callback=_parse_csv_list, default=None,
              help="按当前状态筛选，多个状态用逗号分隔（unconfirmed/confirmed/false_positive/closed）")
@click.option("--time-from", default=None,
              help="按时间窗口筛选：事件最后出现时间 >= 此值（格式：YYYY-MM-DD HH:MM:SS）")
@click.option("--time-to", default=None,
              help="按时间窗口筛选：事件首次出现时间 <= 此值（格式：YYYY-MM-DD HH:MM:SS）")
@click.option("--set-status", "new_status",
              type=click.Choice(list(VALID_STATUSES), case_sensitive=False),
              default=None, help="批量修改的目标状态")
@click.option("--set-handler", default=None, help="批量修改的处理人")
@click.option("--set-note", default=None, help="批量修改的备注")
@click.option("-H", "--handler", required=True, help="执行批量操作的操作人")
@click.option("--conflict-strategy",
              type=click.Choice([CONFLICT_STRATEGY_SKIP, CONFLICT_STRATEGY_ABORT, CONFLICT_STRATEGY_FORCE],
                                case_sensitive=False),
              default=None, help="版本冲突处理策略（skip/abort/force），默认使用配置文件中的设置")
@click.option("--yes", "-y", is_flag=True, default=False,
              help="跳过确认直接执行")
@pass_ctx
def cmd_batch_annotate(ctx: CliContext, event_ids, device_ids, statuses,
                       time_from, time_to, new_status, set_handler, set_note,
                       handler, conflict_strategy, yes) -> None:
    """批量标注事件"""
    batch_filter = BatchFilter(
        event_ids=event_ids,
        device_ids=device_ids,
        statuses=statuses,
        time_from=time_from,
        time_to=time_to,
    )

    batch_update = BatchUpdate(
        status=new_status.lower() if new_status else None,
        handler=set_handler,
        note=set_note,
    )

    if conflict_strategy:
        conflict_strategy = conflict_strategy.lower()

    try:
        preview_events = ctx.batch_manager.preview(batch_filter)
        preview = ctx.batch_manager.format_preview(
            preview_events, batch_filter, batch_update
        )
        click.echo(preview.formatted())
        click.echo()

        if not preview_events:
            return

        if not yes:
            click.echo(f"将修改 {len(preview_events)} 个事件。")
            click.echo("请仔细检查以上预览内容。")
            if not click.confirm("确认执行批量操作？", default=False):
                click.echo("已取消批量操作。")
                return

        result = ctx.batch_manager.execute(
            batch_filter=batch_filter,
            batch_update=batch_update,
            operator=handler,
            conflict_strategy=conflict_strategy,
            preview_events=preview_events,
        )
        click.echo(result.formatted())

    except BatchOperationError as e:
        click.echo(f"错误: {e}", err=True)
        sys.exit(1)


@main.command("batch-logs", help="查看批量操作日志")
@click.option("-n", "--limit", type=int, default=20, help="显示最近的N条记录")
@pass_ctx
def cmd_batch_logs(ctx: CliContext, limit: int) -> None:
    """查看批量操作日志"""
    click.echo(ctx.batch_manager.get_batch_logs(limit))


@main.command("batch-detail", help="查看批量操作详情")
@click.argument("batch_id")
@pass_ctx
def cmd_batch_detail(ctx: CliContext, batch_id: str) -> None:
    """查看批量操作详情"""
    click.echo(ctx.batch_manager.get_batch_detail(batch_id))


@main.command("batch-cleanup", help="清理指定天数前的批量操作日志")
@click.option("--days", type=int, default=None,
              help="清理多少天前的日志，默认使用配置文件中的设置")
@click.option("--yes", "-y", is_flag=True, default=False,
              help="跳过确认直接执行")
@pass_ctx
def cmd_batch_cleanup(ctx: CliContext, days: int | None, yes: bool) -> None:
    """清理批量操作日志"""
    if days is None:
        days = ctx.config.batch.log_retention_days

    if not yes:
        click.echo(f"将清理 {days} 天前的批量操作日志。")
        if not click.confirm("确认执行清理？", default=False):
            click.echo("已取消。")
            return

    deleted = ctx.batch_manager.cleanup_old_logs(days)
    click.echo(f"已清理 {deleted} 条批量操作日志。")


@main.command("template-save", help="将当前批量标注参数保存为命名模板")
@click.option("-n", "--name", required=True, help="模板名称（唯一标识）")
@click.option("-d", "--description", default="", help="模板描述")
@click.option("--event-ids", callback=_parse_csv_list, default=None,
              help="按事件ID筛选，多个ID用逗号分隔")
@click.option("--device-ids", callback=_parse_csv_list, default=None,
              help="按设备编号筛选，多个编号用逗号分隔")
@click.option("--statuses", callback=_parse_csv_list, default=None,
              help="按当前状态筛选，多个状态用逗号分隔")
@click.option("--time-from", default=None,
              help="按时间窗口筛选：事件最后出现时间 >= 此值")
@click.option("--time-to", default=None,
              help="按时间窗口筛选：事件首次出现时间 <= 此值")
@click.option("--set-status", "new_status",
              type=click.Choice(list(VALID_STATUSES), case_sensitive=False),
              default=None, help="批量修改的目标状态")
@click.option("--set-handler", default=None, help="批量修改的处理人")
@click.option("--set-note", default=None, help="批量修改的备注")
@click.option("--conflict-strategy",
              type=click.Choice([CONFLICT_STRATEGY_SKIP, CONFLICT_STRATEGY_ABORT, CONFLICT_STRATEGY_FORCE],
                                case_sensitive=False),
              default=None, help="版本冲突处理策略（skip/abort/force）")
@click.option("--overwrite", is_flag=True, default=False,
              help="覆盖同名模板")
@click.option("-H", "--operator", default="", help="操作人（用于版本历史记录）")
@pass_ctx
def cmd_template_save(ctx: CliContext, name: str, description: str,
                      event_ids, device_ids, statuses,
                      time_from, time_to, new_status, set_handler, set_note,
                      conflict_strategy, overwrite: bool, operator: str) -> None:
    """保存批量任务模板"""
    batch_filter = BatchFilter(
        event_ids=event_ids,
        device_ids=device_ids,
        statuses=statuses,
        time_from=time_from,
        time_to=time_to,
    )

    batch_update = BatchUpdate(
        status=new_status.lower() if new_status else None,
        handler=set_handler,
        note=set_note,
    )

    if conflict_strategy:
        conflict_strategy = conflict_strategy.lower()

    try:
        template = ctx.template_manager.save_template(
            name=name,
            description=description,
            batch_filter=batch_filter,
            batch_update=batch_update,
            conflict_strategy=conflict_strategy,
            overwrite=overwrite,
            operator=operator,
        )
        click.echo(f"模板 '{template.name}' 保存成功！")
        click.echo()
        click.echo(ctx.template_manager.format_template_detail(template))
    except TemplateError as e:
        click.echo(f"错误: {e}", err=True)
        sys.exit(1)


@main.command("template-list", help="列出所有已保存的批量任务模板")
@pass_ctx
def cmd_template_list(ctx: CliContext) -> None:
    """列出模板列表"""
    templates = ctx.template_manager.list_templates()
    click.echo(ctx.template_manager.format_template_list(templates))


@main.command("template-show", help="查看指定模板的详细内容和兼容性检查")
@click.argument("name")
@pass_ctx
def cmd_template_show(ctx: CliContext, name: str) -> None:
    """查看模板详情"""
    try:
        template = ctx.template_manager.get_template_or_error(name)
        click.echo(ctx.template_manager.format_template_detail(template))
        click.echo()
        validation = ctx.template_manager.validate_template(template)
        click.echo("兼容性检查:")
        click.echo(validation.formatted())
    except TemplateError as e:
        click.echo(f"错误: {e}", err=True)
        sys.exit(1)


@main.command("template-copy", help="复制现有模板为新名称")
@click.argument("source_name")
@click.argument("target_name")
@click.option("-d", "--description", default=None, help="新模板描述（默认：源描述 + 副本）")
@click.option("-H", "--operator", default="", help="操作人（用于版本历史记录）")
@pass_ctx
def cmd_template_copy(ctx: CliContext, source_name: str, target_name: str,
                      description: str | None, operator: str) -> None:
    """复制模板"""
    try:
        new_tpl = ctx.template_manager.copy_template(
            source_name=source_name,
            target_name=target_name,
            new_description=description,
            operator=operator,
        )
        click.echo(f"已复制模板: '{source_name}' → '{target_name}'")
        click.echo()
        click.echo(ctx.template_manager.format_template_detail(new_tpl))
    except TemplateError as e:
        click.echo(f"错误: {e}", err=True)
        sys.exit(1)


@main.command("template-delete", help="删除指定的批量任务模板（版本历史保留，可用于恢复）")
@click.argument("name")
@click.option("--yes", "-y", is_flag=True, default=False,
              help="跳过确认直接删除")
@click.option("-H", "--operator", default="", help="操作人（用于版本历史记录）")
@pass_ctx
def cmd_template_delete(ctx: CliContext, name: str, yes: bool, operator: str) -> None:
    """删除模板"""
    try:
        template = ctx.template_manager.get_template(name)
        if template is None:
            click.echo(f"模板不存在: '{name}'")
            sys.exit(1)

        if not yes:
            click.echo(f"将删除模板 '{name}'。")
            click.echo("提示：删除前会自动创建备份快照，版本历史将保留，可用于后续恢复。")
            if not click.confirm("确认删除？", default=False):
                click.echo("已取消。")
                return

        deleted = ctx.template_manager.delete_template(name, operator=operator)
        if deleted:
            click.echo(f"模板 '{name}' 已删除。版本历史已保留，可使用历史版本恢复。")
        else:
            click.echo(f"模板 '{name}' 删除失败。")
            sys.exit(1)
    except TemplateError as e:
        click.echo(f"错误: {e}", err=True)
        sys.exit(1)


@main.command("template-export", help="将模板导出为 JSON 文件（单个或批量）")
@click.argument("output_path", type=click.Path(dir_okay=False))
@click.option("-n", "--name", "names", multiple=True, default=None,
              help="指定要导出的模板名称（可多次使用），不指定时导出全部模板")
@click.option("-H", "--operator", default="", help="操作人（用于日志）")
@pass_ctx
def cmd_template_export(ctx: CliContext, output_path: str, names: tuple[str, ...],
                        operator: str) -> None:
    """导出模板"""
    try:
        name_list = list(names) if names else None
        result = ctx.template_manager.export_templates_to_file(
            output_path=output_path,
            names=name_list,
            operator=operator,
        )
        click.echo(result.formatted())
    except TemplateError as e:
        click.echo(f"错误: {e}", err=True)
        sys.exit(1)


@main.command("template-import", help="从 JSON 文件导入模板")
@click.argument("file_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--conflict-strategy",
              type=click.Choice(["skip", "overwrite", "rename"], case_sensitive=False),
              default="skip",
              help="名称冲突处理策略：skip（跳过）/ overwrite（覆盖）/ rename（自动重命名），默认 skip")
@click.option("--no-validate", is_flag=True, default=False,
              help="跳过兼容性检查（不推荐）")
@click.option("--no-rollback", is_flag=True, default=False,
              help="出错时不回滚，保留已成功导入的模板")
@click.option("-H", "--operator", default="", help="操作人（用于日志）")
@pass_ctx
def cmd_template_import(ctx: CliContext, file_path: str, conflict_strategy: str,
                        no_validate: bool, no_rollback: bool, operator: str) -> None:
    """导入模板"""
    try:
        if conflict_strategy:
            conflict_strategy = conflict_strategy.lower()
        result = ctx.template_manager.import_templates_from_file(
            file_path=file_path,
            conflict_strategy=conflict_strategy,
            operator=operator,
            validate_compatibility=not no_validate,
            rollback_on_error=not no_rollback,
        )
        click.echo(result.formatted())
        if result.has_errors:
            sys.exit(1)
    except (TemplateError, TemplateImportError) as e:
        click.echo(f"错误: {e}", err=True)
        sys.exit(1)


@main.command("template-import-logs", help="查看模板导入导出日志")
@click.option("-n", "--limit", type=int, default=20, help="显示最近的N条记录")
@pass_ctx
def cmd_template_import_logs(ctx: CliContext, limit: int) -> None:
    """查看模板导入导出日志"""
    click.echo(ctx.template_manager.get_template_import_logs(limit))


@main.command("template-import-detail", help="查看某次模板导入/导出的详细信息")
@click.argument("log_id")
@pass_ctx
def cmd_template_import_detail(ctx: CliContext, log_id: str) -> None:
    """查看模板导入/导出详情"""
    click.echo(ctx.template_manager.get_template_import_log_detail(log_id))


@main.command("template-versions", help="查看模板的版本历史列表")
@click.argument("name")
@pass_ctx
def cmd_template_versions(ctx: CliContext, name: str) -> None:
    """查看模板版本历史"""
    try:
        versions = ctx.template_manager.list_template_versions(name)
        click.echo(ctx.template_manager.format_template_versions(versions))
    except (TemplateError, TemplateVersionError) as e:
        click.echo(f"错误: {e}", err=True)
        sys.exit(1)


@main.command("template-diff", help="对比模板两个版本的字段差异摘要")
@click.argument("name")
@click.option("--from", "from_version", type=int, required=True,
              help="起始版本号")
@click.option("--to", "to_version", type=int, default=None,
              help="目标版本号（默认：当前最新版本）")
@pass_ctx
def cmd_template_diff(ctx: CliContext, name: str, from_version: int,
                      to_version: int | None) -> None:
    """对比模板版本差异"""
    try:
        if to_version is None:
            diff = ctx.template_manager.diff_template_version_with_current(
                name, from_version
            )
        else:
            diff = ctx.template_manager.diff_template_versions(
                name, from_version, to_version
            )
        click.echo(diff.formatted())
    except (TemplateError, TemplateVersionError) as e:
        click.echo(f"错误: {e}", err=True)
        sys.exit(1)


@main.command("template-rollback", help="将模板回滚到指定历史版本")
@click.argument("name")
@click.argument("version", type=int)
@click.option("-H", "--operator", default="", help="操作人（用于版本历史记录）")
@click.option("--no-validate", is_flag=True, default=False,
              help="跳过兼容性检查（不推荐）")
@click.option("--yes", "-y", is_flag=True, default=False,
              help="跳过确认直接执行回滚")
@pass_ctx
def cmd_template_rollback(ctx: CliContext, name: str, version: int,
                   operator: str, no_validate: bool, yes: bool) -> None:
    """回滚模板到指定版本"""
    try:
        if not no_validate:
            try:
                preview = ctx.template_manager.preview_rollback(name, version)
                click.echo(preview.formatted())
                click.echo()
            except TemplateVersionError as e:
                click.echo(f"错误: {e}", err=True)
                sys.exit(1)

            if not yes:
                if preview.affected_filters or preview.affected_updates:
                    click.echo("回滚不会更改上述筛选条件和/或更新字段。")
                click.echo("请仔细检查以上变更内容。")
                if not click.confirm("确认执行回滚？", default=False):
                    click.echo("已取消回滚。")
                    return

        result = ctx.template_manager.rollback_template(
            name=name,
            target_version=version,
            operator=operator,
            validate_compatibility=not no_validate,
        )
        click.echo(result.formatted())
        if not result.success:
            sys.exit(1)
    except (TemplateError, TemplateVersionError) as e:
        click.echo(f"错误: {e}", err=True)
        sys.exit(1)


@main.command("batch-annotate", help="批量标注事件状态（支持筛选、模板、预览、版本冲突检测）")
@click.option("--event-ids", callback=_parse_csv_list, default=None,
              help="按事件ID筛选，多个ID用逗号分隔")
@click.option("--device-ids", callback=_parse_csv_list, default=None,
              help="按设备编号筛选，多个编号用逗号分隔")
@click.option("--statuses", callback=_parse_csv_list, default=None,
              help="按当前状态筛选，多个状态用逗号分隔（unconfirmed/confirmed/false_positive/closed）")
@click.option("--time-from", default=None,
              help="按时间窗口筛选：事件最后出现时间 >= 此值（格式：YYYY-MM-DD HH:MM:SS）")
@click.option("--time-to", default=None,
              help="按时间窗口筛选：事件首次出现时间 <= 此值（格式：YYYY-MM-DD HH:MM:SS）")
@click.option("--set-status", "new_status",
              type=click.Choice(list(VALID_STATUSES), case_sensitive=False),
              default=None, help="批量修改的目标状态")
@click.option("--set-handler", default=None, help="批量修改的处理人")
@click.option("--set-note", default=None, help="批量修改的备注")
@click.option("--use-template", "template_name", default=None,
              help="使用已保存的模板（命令行参数可覆盖模板中的设置）")
@click.option("-H", "--handler", required=False, default=None, help="执行批量操作的操作人（使用模板时可省略）")
@click.option("--conflict-strategy",
              type=click.Choice([CONFLICT_STRATEGY_SKIP, CONFLICT_STRATEGY_ABORT, CONFLICT_STRATEGY_FORCE],
                                case_sensitive=False),
              default=None, help="版本冲突处理策略（skip/abort/force），默认使用配置文件中的设置")
@click.option("--yes", "-y", is_flag=True, default=False,
              help="跳过确认直接执行")
@pass_ctx
def cmd_batch_annotate(ctx: CliContext, event_ids, device_ids, statuses,
                       time_from, time_to, new_status, set_handler, set_note,
                       template_name, handler, conflict_strategy, yes) -> None:
    """批量标注事件（支持模板）"""
    batch_filter = BatchFilter()
    batch_update = BatchUpdate()
    template_conflict_strategy = None
    template = None

    if template_name:
        try:
            template = ctx.template_manager.get_template_or_error(template_name)
            validation = ctx.template_manager.validate_template(template)
            if not validation.is_valid:
                click.echo(f"模板 '{template_name}' 与当前配置存在冲突，无法执行：", err=True)
                click.echo(validation.formatted(), err=True)
                click.echo("", err=True)
                click.echo("提示: 请修复冲突后使用 --overwrite 重新保存模板，或使用命令行参数覆盖。", err=True)
                sys.exit(1)

            bf, bu, cs = ctx.template_manager.template_to_objects(template)
            batch_filter = bf
            batch_update = bu
            template_conflict_strategy = cs
        except TemplateError as e:
            click.echo(f"错误: {e}", err=True)
            sys.exit(1)

    if event_ids is not None:
        batch_filter.event_ids = event_ids
    if device_ids is not None:
        batch_filter.device_ids = device_ids
    if statuses is not None:
        batch_filter.statuses = statuses
    if time_from is not None:
        batch_filter.time_from = time_from
    if time_to is not None:
        batch_filter.time_to = time_to

    if new_status:
        batch_update.status = new_status.lower()
    if set_handler is not None:
        batch_update.handler = set_handler
    if set_note is not None:
        batch_update.note = set_note

    final_conflict_strategy = None
    if conflict_strategy:
        final_conflict_strategy = conflict_strategy.lower()
    elif template_conflict_strategy:
        final_conflict_strategy = template_conflict_strategy

    if handler is None:
        click.echo("错误: 必须指定操作人（-H/--handler）", err=True)
        sys.exit(1)

    if template_name:
        click.echo(f"使用模板: {template_name}")
        click.echo(f"模板说明: {template.describe()}")
        click.echo()

    try:
        preview_events = ctx.batch_manager.preview(batch_filter)
        preview = ctx.batch_manager.format_preview(
            preview_events, batch_filter, batch_update
        )
        click.echo(preview.formatted())
        click.echo()

        if not preview_events:
            return

        if not yes:
            click.echo(f"将修改 {len(preview_events)} 个事件。")
            click.echo("请仔细检查以上预览内容。")
            if not click.confirm("确认执行批量操作？", default=False):
                click.echo("已取消批量操作。")
                return

        result = ctx.batch_manager.execute(
            batch_filter=batch_filter,
            batch_update=batch_update,
            operator=handler,
            conflict_strategy=final_conflict_strategy,
            preview_events=preview_events,
        )
        click.echo(result.formatted())

    except BatchOperationError as e:
        click.echo(f"错误: {e}", err=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
