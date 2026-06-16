# 本地巡检记录整理 CLI 工具

可复用的本地巡检记录整理工具，支持 CSV/JSON 导入、数据校验、事件归并、状态标注、批量操作、可复用模板、撤销操作和结果导出。所有数据持久化存储在 SQLite 中。

## 目录结构

```
lfc-00022/
├── inspection_cli/         # 核心包
│   ├── __init__.py
│   ├── cli.py              # CLI 入口
│   ├── config.py           # 配置模块
│   ├── database.py         # SQLite 持久化层
│   ├── importer.py         # 数据导入（CSV/JSON）
│   ├── validator.py        # 数据校验
│   ├── merger.py           # 事件归并
│   ├── annotation.py       # 标注管理
│   └── exporter.py         # 结果导出
├── samples/                # 样例数据
│   ├── config.yaml         # 样例配置
│   ├── config_bad.yaml     # 错误配置（用于测试）
│   ├── inspection_sample.csv
│   ├── inspection_sample.json
│   └── inspection_errors.csv  # 含错误数据（用于测试）
├── pyproject.toml
├── requirements.txt
└── README.md
```

## 安装

```bash
# 安装依赖
pip install -r requirements.txt

# 或安装为可执行命令
pip install -e .
```

## 快速开始

使用样例数据一键演示完整流程：

```bash
# 方式一：通过 Python 模块调用
python -m inspection_cli.cli -c samples/config.yaml init-demo

# 方式二：安装后直接调用
inspection-cli -c samples/config.yaml init-demo
```

## 完整命令链

以下是从导入到导出的完整工作流程：

```bash
# 1. 导入 CSV 巡检记录
python -m inspection_cli.cli -c samples/config.yaml import samples/inspection_sample.csv

# 2. 导入 JSON 巡检记录（可一次导入多个文件）
python -m inspection_cli.cli -c samples/config.yaml import samples/inspection_sample.json

# 3. 按设备与时间窗口归并相近异常为事件
python -m inspection_cli.cli -c samples/config.yaml merge

# 4. 查看事件列表
python -m inspection_cli.cli -c samples/config.yaml list

# 5. 标注事件状态（已确认 / 误报 / 已关闭）
python -m inspection_cli.cli -c samples/config.yaml annotate <事件ID> confirmed -H 张三 -n "现场已核实"

# 6. 查看可用状态
python -m inspection_cli.cli -c samples/config.yaml statuses

# 7. 导出 CSV 汇总
python -m inspection_cli.cli -c samples/config.yaml export events.csv

# 8. 导出 JSON 汇总（含来源记录详情）
python -m inspection_cli.cli -c samples/config.yaml export events.json -f json --with-records

# 9. 撤销最后一次标注
python -m inspection_cli.cli -c samples/config.yaml undo <事件ID>
```

## 状态说明

| 状态 | 说明 |
|------|------|
| unconfirmed | 待确认（默认） |
| confirmed | 已确认 |
| false_positive | 误报 |
| closed | 已关闭 |

## 配置文件说明（YAML）

```yaml
validation:
  device_id_pattern: "^DEV-[A-Z0-9]{3,10}$"  # 设备编号正则
  time_formats:                                # 接受的时间格式
    - "%Y-%m-%d %H:%M:%S"
    - "%Y-%m-%dT%H:%M:%S"
    - "%Y/%m/%d %H:%M:%S"
  valid_issue_types:                           # 允许的问题类型
    - temperature
    - pressure
    - vibration
    - voltage
    - current
    - connectivity
    - performance
    - security
    - other
  valid_severities:                            # 允许的严重级别
    - critical
    - warning
    - info

event_merge:
  time_window_minutes: 30                      # 时间窗口（分钟）
  same_device_only: true                       # 仅同设备归并
  same_issue_type: true                        # 仅同问题类型归并

export:
  csv_field_order:                             # CSV 导出字段顺序
    - event_id
    - status
    - device_id
    - first_seen
    - last_seen
    - severity
    - issue_type
    - record_count
    - handler
    - note
    - source_record_ids

db_path: "inspection.db"                       # SQLite 数据库路径
```

## 失败场景验证

### 1. 时间格式错误 — 指出具体行号

```bash
python -m inspection_cli.cli -c samples/config.yaml import samples/inspection_errors.csv
```

预期输出类似：
```
发现 4 个错误:
  - 第 3 行: [device_id] 设备编号格式不匹配，期望模式: ^DEV-[A-Z0-9]{3,10}$ (值: 'BAD-ID')
  - 第 4 行: [event_time] 时间格式错误，支持的格式: %Y-%m-%d %H:%M:%S, %Y-%m-%dT%H:%M:%S, %Y/%m/%d %H:%M:%S (值: 'not-a-time')
  - 第 5 行: [issue_type] 问题类型无效，允许值: temperature, pressure, ...
  - 第 6 行: [severity] 严重级别无效，允许值: critical, warning, info (值: 'fatal')
```

### 2. 配置写错 — 不清空已有数据

先导入正常数据：
```bash
python -m inspection_cli.cli -c samples/config.yaml import samples/inspection_sample.csv
python -m inspection_cli.cli -c samples/config.yaml merge
python -m inspection_cli.cli -c samples/config.yaml list
```

然后使用错误配置：
```bash
python -m inspection_cli.cli -c samples/config_bad.yaml list
```

预期输出（报错退出，但不影响已有数据）：
```
错误: validation.time_formats 必须是列表
提示: 配置错误时不会清空已有数据，请修复配置后重试。
```

再切回正确配置验证数据仍然存在：
```bash
python -m inspection_cli.cli -c samples/config.yaml list
```

### 3. 重复导入 — 不制造重复事件

```bash
# 第一次导入
python -m inspection_cli.cli -c samples/config.yaml import samples/inspection_sample.csv

# 第二次导入同一文件
python -m inspection_cli.cli -c samples/config.yaml import samples/inspection_sample.csv

# 归并
python -m inspection_cli.cli -c samples/config.yaml merge

# 查看事件数量（应该不变）
python -m inspection_cli.cli -c samples/config.yaml list
```

第二次导入输出应显示 "重复跳过: 10"，事件数量不增加。

### 4. 没有标注历史时撤销 — 返回清晰错误

```bash
# 对一个未标注过的事件执行撤销
python -m inspection_cli.cli -c samples/config.yaml undo <事件ID>
```

预期输出：
```
错误: 事件 EVT-XXXXXXXXXXXX 没有标注历史，无法撤销。当前状态为 unconfirmed（待确认），尚未进行过任何标注操作。
```

## 持久化与一致性

- 所有来源记录、事件、标注历史、批量操作日志、**批量任务模板**均保存在 SQLite 数据库（默认 `inspection.db`）。
- 重新运行 CLI 后再次导出，事件状态、处理人、备注、来源记录 ID 和字段顺序均保持一致。
- **模板跨重启持久化**：`template-save` 保存的模板在重启 CLI 后通过 `template-list` / `template-show` 仍可查看和使用。
- 使用 `merge` 命令重新归并时，默认保留已有事件的标注状态（使用 `--no-preserve` 可清除）。
- **模板冲突不静默降级**：若模板中的状态 / 时间格式 / 冲突策略与当前配置不兼容，`batch-annotate --use-template` 执行前明确报错退出，不会偷偷套用默认值。
- **批量结果一致性**：模板执行的成功数、冲突数在 `batch-detail`、`batch-logs`、导出的 CSV / JSON 三者之间完全对应，version 字段保持单调递增。
