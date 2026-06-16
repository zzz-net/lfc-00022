"""CLI 入口：基于 Click 构建完整命令链"""
from __future__ import annotations

import os
import sys

import click

from . import __version__
from .annotation import AnnotationError, AnnotationManager
from .config import AppConfig, ConfigError
from .database import Database
from .exporter import Exporter
from .importer import RecordImporter
from .merger import EventMerger


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
        self.exporter = Exporter(self.db, self.config)


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


if __name__ == "__main__":
    main()
