# 本地巡检记录 CLI

可复用的本地巡检记录整理工具，支持 CSV/JSON 导入、数据校验、事件归并、状态标注、批量操作、可复用模板、撤销操作和结果导出。所有数据持久化存储在 SQLite 中。

## 目录结构

```
lfc-00022/
+-- inspection_cli/          # 核心包
|   +-- __init__.py
|   +-- cli.py               # CLI 入口
|   +-- config.py            # 配置模块
|   +-- database.py          # SQLite 持久化层
|   +-- importer.py          # 数据导入 (CSV/JSON)
|   +-- validator.py         # 数据校验
|   +-- merger.py            # 事件归并
|   +-- annotation.py        # 标注管理
|   +-- batch.py             # 批量操作管理器
|   +-- templates.py         # 批量任务模板管理器
|   +-- exporter.py          # 结果导出
+-- tests/                   # 测试套件
|   +-- test_batch_operations.py
|   +-- test_undo_annotation.py
|   +-- test_templates.py
|   +-- test_cli_template_e2e.py
+-- samples/                   # 样例数据
|   +-- config.yaml          # 样例配置
|   +-- config_bad.yaml      # 错误配置（用于测试）
|   +-- inspection_sample.csv
|   +-- inspection_sample.json
|   +-- inspection_errors.csv  # 含错误数据（用于测试）
+-- pyproject.toml
+-- requirements.txt
+-- README.md
```

## 安装

```bash
# 方式一：直接用模块运行
python -m inspection_cli.cli --help

# 方式二：安装后直接调用
pip install -e .
```

## 快速开始

```bash
python -m inspection_cli.cli -c samples/config.yaml init-demo
```

## 完整命令链

以下是从导入到导出的完整工作流程：

```bash
# 1. 导入 CSV 记录
python -m inspection_cli.cli -c samples/config.yaml import samples/inspection_sample.csv

# 2. 导入 JSON 记录
python -m inspection_cli.cli -c samples/config.yaml import samples/inspection_sample.json

# 3. 按设备与时间窗口归并相近异常为事件
python -m inspection_cli.cli -c samples/config.yaml merge

# 4. 查看归并后的事件列表
python -m inspection_cli.cli -c samples/config.yaml list

# 5. 标注事件状态（已确认 / 误报 / 已关闭）
python -m inspection_cli.cli -c samples/config.yaml annotate <事件ID> confirmed -H 张三 -n "现场已核实"

# 6. 查看可用状态
python -m inspection_cli.cli -c samples/config.yaml statuses

# 7. 导出 CSV 汇总
python -m inspection_cli.cli -c samples/config.yaml export events.csv

# 8. 撤销最后一次标注
python -m inspection_cli.cli -c samples/config.yaml undo <事件ID>

# 9. 批量标注（按筛选条件一次修改多个事件）
python -m inspection_cli.cli -c samples/config.yaml batch-annotate --statuses unconfirmed --set-status closed --set-handler 批量管理员 --set-note "批量关闭" -H 操作人

# 10. 查看批量操作日志
python -m inspection_cli.cli -c samples/config.yaml batch-logs

# 11. 查看某次批量操作详情
python -m inspection_cli.cli -c samples/config.yaml batch-detail <批量操作ID>

# 12. 保存常用批量规则为模板（下次直接套用）
python -m inspection_cli.cli -c samples/config.yaml template-save -n close-unconfirmed -d "批量关闭待确认事件" --statuses unconfirmed --set-status closed --set-handler 批量管理员 --set-note "模板批量关闭"

# 13. 用模板执行批量操作
python -m inspection_cli.cli -c samples/config.yaml batch-annotate --use-template close-unconfirmed -H 操作员
```

## 批量标注

batch-annotate 按筛选条件选中一批事件，统一修改状态、处理人或备注。执行前自动预览，确认后才真正写入。

### 筛选条件

| 选项 | 说明 |
|------|------|
| `--event-ids EVT1,EVT2` | 按事件 ID 筛选，逗号分隔 |
| `--device-ids DEV1,DEV2` | 按设备编号筛选，逗号分隔 |
| `--statuses unconfirmed,confirmed` | 按当前状态筛选，逗号分隔 |
| `--time-from "2026-06-15 00:00:00"` | 事件最后出现时间 >= 此值 |
| `--time-to "2026-06-30 23:59:59"` | 事件首次出现时间 <= 此值 |

### 更新内容

| 选项 | 说明 |
|------|------|
| `--set-status closed` | 目标状态（closed / confirmed / false_positive / unconfirmed） |
| `--set-handler 张三` | 目标处理人 |
| `--set-note "备注"` | 目标备注 |

### 其他选项

| 选项 | 说明 |
|------|------|
| `-H, --handler` | 必填，本次操作的操作人 |
| `--conflict-strategy skip` | 版本冲突策略：skip（跳过）/ abort（中止）/ force（强制），默认使用配置文件 |
| `-y, --yes` | 跳过确认直接执行 |
| `--use-template 模板名` | 使用已保存模板（见下方模板章节） |

### 示例

```bash
# 把所有待确认事件关闭
python -m inspection_cli.cli -c samples/config.yaml batch-annotate --statuses unconfirmed --set-status closed --set-handler 管理员 --set-note "批量关闭" -H 操作员

# 只关闭 DEV-A001 和 DEV-B002 的事件
python -m inspection_cli.cli -c samples/config.yaml batch-annotate --device-ids DEV-A001,DEV-B002 --set-status confirmed --set-handler 值班员 -H 操作员

# 按时间窗口筛选并修改
python -m inspection_cli.cli -c samples/config.yaml batch-annotate --time-from "2026-06-15 00:00:00" --time-to "2026-06-15 23:59:59" --set-status false_positive --set-note "当日全为误报" -H 操作员

# 强制覆盖冲突
python -m inspection_cli.cli -c samples/config.yaml batch-annotate --statuses unconfirmed --set-status closed -H 操作员 --conflict-strategy force
```

执行后会输出预览和确认提示；确认后输出批次 ID、成功数、跳过数、冲突数。

## 批量操作日志

每次 batch-annotate 执行都会在数据库中留下日志记录，可事后查看。

### batch-logs - 查看批量操作日志

```bash
# 查看最近 20 条（默认）
python -m inspection_cli.cli -c samples/config.yaml batch-logs

# 查看最近 5 条
python -m inspection_cli.cli -c samples/config.yaml batch-logs -n 5
```

输出包含：批量 ID、类型、状态、操作人、总数 / 成功 / 跳过 / 冲突 / 错误、创建时间。

### batch-detail - 查看某次批量操作详情

```bash
python -m inspection_cli.cli -c samples/config.yaml batch-detail <批量操作ID>
```

输出包含：筛选条件 JSON、更新内容 JSON、每个事件的处理结果（成功 / 跳过 / 冲突 / 错误及原因）。

### batch-cleanup - 清理旧日志

```bash
# 清理 30 天前的日志（默认）
python -m inspection_cli.cli -c samples/config.yaml batch-cleanup

# 清理 7 天前的日志
python -m inspection_cli.cli -c samples/config.yaml batch-cleanup --days 7
```

### 结果核对

执行 batch-annotate 后，用三步对账法确认结果一致：

第一步：batch-logs 看总数

```bash
python -m inspection_cli.cli -c samples/config.yaml batch-logs -n 1
```

输出示例：
```
批量ID                   类型         状态           操作人          总数     成功     跳过     冲突     错误     创建时间
-------------------------------------------------------------------------------------------------------------------
BATCH-XXXXXXXXXXXX     annotate   已完成          tester     10     10     0      0      0      2026-06-17 ...
```

记下：批次 ID、成功数、冲突数。

第二步：batch-detail 逐条核

```bash
python -m inspection_cli.cli -c samples/config.yaml batch-detail <批量操作ID>
```

输出包含每个事件的处理结果，例如：
```
总计: 10 | 成功: 10 | 跳过: 0 | 冲突: 0 | 错误: 0

各事件处理详情:
------------------------------------------------------------
[成功] EVT-XXXXXXXXXXXX (v1 -> v2) 状态: unconfirmed -> closed 处理人:  -> admin
[成功] EVT-XXXXXXXXXXXX (v1 -> v2) 状态: unconfirmed -> closed 处理人:  -> admin
...
```

核对要点：
- 批次总数 / 成功 / 跳过 / 冲突 / 错误 与 batch-logs 完全一致
- 每个成功事件的 version 从旧版本 +1
- 状态、处理人、备注与预期更新内容一致

第三步：导出文件交叉验

```bash
python -m inspection_cli.cli -c samples/config.yaml export check.csv
python -m inspection_cli.cli -c samples/config.yaml export check.json -f json --with-records
```

核对要点：
- CSV 中 closed 状态的事件数 = batch-detail 成功数
- JSON 中 closed 状态的事件数 = CSV closed 数
- 每个事件的 handler / note / version 与 batch-detail 记录一一对应

三者（batch-logs 总数、batch-detail 明细、导出文件）应当完全吻合。

## 批量任务模板

当常用的筛选条件和更新内容需要反复使用时，可以保存为命名模板，之后直接拿来执行。模板持久化存储在 SQLite 中，重启 CLI 后继续可用。

### template-save - 保存模板

```bash
python -m inspection_cli.cli -c samples/config.yaml template-save -n <模板名> -d "<描述>" --statuses unconfirmed --set-status closed --set-handler 批量管理员 --set-note "模板批量关闭" --conflict-strategy skip
```

| 选项 | 说明 |
|------|------|
| `-n, --name` | 必填，模板名称（唯一标识） |
| `-d, --description` | 模板描述 |
| `--event-ids` / `--device-ids` / `--statuses` / `--time-from` / `--time-to` | 筛选条件（与 batch-annotate 相同） |
| `--set-status` / `--set-handler` / `--set-note` | 更新内容（与 batch-annotate 相同） |
| `--conflict-strategy` | 冲突策略（skip / abort / force），默认使用配置文件 |
| `--overwrite` | 覆盖同名模板 |

示例：

```bash
# 保存关闭所有待确认模板
python -m inspection_cli.cli -c samples/config.yaml template-save -n close-unconfirmed -d "每日下班前批量关闭未处理事件" --statuses unconfirmed --set-status closed --set-handler 批量管理员 --set-note "模板批量关闭" --conflict-strategy skip

# 保存按设备确认模板
python -m inspection_cli.cli -c samples/config.yaml template-save -n confirm-ab -d "确认 DEV-A001/B002 的告警" --device-ids DEV-A001,DEV-B002 --set-status confirmed --set-handler 值班员 --set-note "已现场核实" --conflict-strategy abort

# 配置变更后覆盖旧模板
python -m inspection_cli.cli -c samples/config.yaml template-save -n close-unconfirmed -d "更新后的规则" --statuses unconfirmed,false_positive --set-status closed --set-handler 新管理员 --overwrite
```

### template-list - 列出所有模板

```bash
python -m inspection_cli.cli -c samples/config.yaml template-list
```

输出包含：模板名称、冲突策略、创建 / 更新时间、描述。

### template-show - 查看模板详情和兼容性检查

```bash
python -m inspection_cli.cli -c samples/config.yaml template-show <模板名>
```

输出模板的完整筛选条件和更新内容，并自动执行与当前配置的兼容性检查：

- ERROR（阻止执行）：模板中的状态 / 时间格式 / 冲突策略 / 处理人与当前配置冲突
- WARNING（提示）：设备编号可能不符合当前编号模式

存在 ERROR 时，batch-annotate --use-template 会明确报错退出，不会静默降级。

### template-copy - 复制模板

```bash
# 复制模板（描述默认加副本后缀）
python -m inspection_cli.cli -c samples/config.yaml template-copy <源模板名> <目标模板名>

# 自定义描述
python -m inspection_cli.cli -c samples/config.yaml template-copy close-unconfirmed close-old -d "只关闭7天前的事件"
```

### template-delete - 删除模板

```bash
# 带二次确认
python -m inspection_cli.cli -c samples/config.yaml template-delete <模板名>

# 跳过确认
python -m inspection_cli.cli -c samples/config.yaml template-delete <模板名> -y
```

### 使用模板执行批量操作

batch-annotate 的 --use-template 选项会加载模板中保存的筛选条件和更新内容，命令行参数可以覆盖模板中的设置（命令行优先级更高）。

```bash
# 基本用法：用模板的筛选和更新
python -m inspection_cli.cli -c samples/config.yaml batch-annotate --use-template close-unconfirmed -H 操作员

# 覆盖模板中的状态（筛选仍用模板的）
python -m inspection_cli.cli -c samples/config.yaml batch-annotate --use-template close-unconfirmed --set-status false_positive -H 操作员

# 脚本化场景：跳过确认
python -m inspection_cli.cli -c samples/config.yaml batch-annotate --use-template confirm-ab -H 操作员 -y
```

### 模板冲突检测与报错

如果模板保存时的状态 / 时间格式 / 冲突策略在当前配置中已失效，执行时会明确报错退出，不会偷偷降级成默认行为：

```
模板 'old-template' 与当前配置存在冲突，无法执行：
发现 2 个错误（阻止执行）:
  [ERROR] filters.statuses: 筛选状态 'archived' 不再有效，当前允许状态: ...
  [ERROR] updates.status: 目标状态 'rejected' 不再有效，当前允许状态: ...
提示: 请修复冲突后使用 --overwrite 重新保存模板，或使用命令行参数覆盖。
```

### 端到端操作示例（可直接复制粘贴）

以下是从保存模板到对账验真的完整流程，按步骤执行即可复现。

Step 1: 准备数据

```bash
python -m inspection_cli.cli -c samples/config.yaml import samples/inspection_sample.csv
python -m inspection_cli.cli -c samples/config.yaml import samples/inspection_sample.json
python -m inspection_cli.cli -c samples/config.yaml merge
```

预期：归并后 11 个事件，全部为 unconfirmed（待确认）状态。

Step 2: 保存模板

```bash
python -m inspection_cli.cli -c samples/config.yaml template-save -n close-unconfirmed -d "每日批量关闭待确认事件" --statuses unconfirmed --set-status closed --set-handler 批量管理员 --set-note "模板批量关闭" --conflict-strategy skip
```

预期输出：
```
模板 'close-unconfirmed' 保存成功！

模板详情: close-unconfirmed
============================================================
模板ID: TPL-XXXXXXXXXXXX
描述: 每日批量关闭待确认事件
冲突策略: skip
...
```

Step 3: 查看模板列表和详情

```bash
# 列出所有模板
python -m inspection_cli.cli -c samples/config.yaml template-list

# 查看详情（自动检查与当前配置的兼容性）
python -m inspection_cli.cli -c samples/config.yaml template-show close-unconfirmed
```

template-show 输出包含兼容性检查：
- 兼容 -> 模板与当前配置完全兼容。
- 有冲突 -> 列出 ERROR / WARNING 项，存在 ERROR 时模板不可执行

Step 4: 验证跨重启持久化

```bash
# 关掉当前终端再开一个（或直接重新执行命令），再次查看
python -m inspection_cli.cli -c samples/config.yaml template-show close-unconfirmed
```

预期：模板仍然存在，内容与保存时完全一致。

Step 5: 复制模板并微调

```bash
# 复制一份用于不同场景
python -m inspection_cli.cli -c samples/config.yaml template-copy close-unconfirmed confirm-unc -d "将待确认改为已确认"

# 覆盖更新复制后的模板
python -m inspection_cli.cli -c samples/config.yaml template-save -n confirm-unc -d "确认所有待确认事件" --statuses unconfirmed --set-status confirmed --set-handler 值班员 --set-note "批量确认" --conflict-strategy skip --overwrite

# 不再需要的模板可删除
python -m inspection_cli.cli -c samples/config.yaml template-delete confirm-unc -y
```

Step 6: 用模板执行批量操作

```bash
python -m inspection_cli.cli -c samples/config.yaml batch-annotate --use-template close-unconfirmed -H 操作员 -y
```

预期输出（节选）：
```
使用模板: close-unconfirmed
模板说明: 筛选: 状态筛选: unconfirmed | 更新: 状态->closed; 处理人->批量管理员; 备注->模板批量关闭 | 冲突策略: skip

批量操作 BATCH-XXXXXXXXXXXX 完成
总计: 11
成功: 11
跳过: 0
冲突: 0
错误: 0
```

记下批次 ID（BATCH-XXXXXXXXXXXX）。

Step 7: 三步对账验真

```bash
# 第一步：batch-logs 看总数
python -m inspection_cli.cli -c samples/config.yaml batch-logs -n 1

# 第二步：batch-detail 逐条核（用第 6 步拿到的批次 ID）
python -m inspection_cli.cli -c samples/config.yaml batch-detail <BATCH-ID>

# 第三步：导出文件交叉验
python -m inspection_cli.cli -c samples/config.yaml export check.csv
python -m inspection_cli.cli -c samples/config.yaml export check.json -f json --with-records
```

对账要点：
1. batch-logs 的成功数 = batch-detail 成功条目数
2. batch-detail 中每个成功事件的新状态 / 处理人 / 备注 = 模板中的设置
3. CSV 中 closed 状态的行数 = 成功数
4. JSON 中 closed 状态的事件数 = CSV closed 数
5. 每个事件的 version = 旧版本 + 1

Step 8: 重复导入归并后再用模板（验证 version 不回退）

```bash
# 再次导入同一文件（应全部重复跳过）
python -m inspection_cli.cli -c samples/config.yaml import samples/inspection_sample.csv

# 重新归并（保留已标注状态，version 不回退）
python -m inspection_cli.cli -c samples/config.yaml merge

# 模板仍然可用（筛选 unconfirmed 应无匹配，因为之前已全部关闭）
python -m inspection_cli.cli -c samples/config.yaml batch-annotate --use-template close-unconfirmed -H 操作员 -y
```

预期：没有符合条件的事件（因为都已关闭），不会错误操作。

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
  same_issue_type: true                       # 仅同问题类型归并

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

### 1. 时间格式错误 - 指出具体行号

```bash
python -m inspection_cli.cli -c samples/config.yaml import samples/inspection_errors.csv
```

预期输出类似：
```
发现 4 个错误:
  - 行 3: [device_id] 设备编号格式不匹配，期望模式: ^DEV-[A-Z0-9]{3,10}$ (值: 'BAD-ID')
  - 行 4: [event_time] 时间格式错误，支持的格式: %Y-%m-%d %H:%M:%S, ... (值: 'not-a-time')
  - 行 5: [issue_type] 问题类型无效，允许值: temperature, pressure, ...
  - 行 6: [severity] 严重级别无效，允许值: critical, warning, info (值: 'fatal')
```

### 2. 配置写错 - 不清空已有数据

```bash
python -m inspection_cli.cli -c samples/config.yaml import samples/inspection_sample.csv
python -m inspection_cli.cli -c samples/config_bad.yaml statuses
```

预期输出（报错退出，但不影响已有数据）：
```
错误: validation.time_formats 必须是列表
提示: 配置错误时不会清空已有数据，请修复配置后重试。
```

### 3. 重复导入 - 不制造重复事件

```bash
# 第一次导入
python -m inspection_cli.cli -c samples/config.yaml import samples/inspection_sample.csv
python -m inspection_cli.cli -c samples/config.yaml merge

# 第二次导入同一文件
python -m inspection_cli.cli -c samples/config.yaml import samples/inspection_sample.csv
python -m inspection_cli.cli -c samples/config.yaml merge
```

第二次导入输出应显示 重复跳过: 10，事件数量不增加。

### 4. 没有标注历史时撤销 - 返回清晰错误

```bash
python -m inspection_cli.cli -c samples/config.yaml import samples/inspection_sample.csv
python -m inspection_cli.cli -c samples/config.yaml merge
python -m inspection_cli.cli -c samples/config.yaml undo <某个未标注事件的事件ID>
```

预期输出：
```
错误: 事件 EVT-XXXXXXXXXXXX 没有标注历史，无法撤销。当前状态为 unconfirmed（待确认），尚未进行过任何标注操作。
```

## 持久化与一致性

- 所有来源记录、事件、标注历史、批量操作日志、批量任务模板均保存在 SQLite 数据库（默认 inspection.db）。
- 重新运行 CLI 后再次导出，事件状态、处理人、备注、来源记录 ID 和字段顺序均保持一致。
- 模板跨重启持久化：template-save 保存的模板在重启 CLI 后通过 template-list / template-show 仍可查看和使用。
- 模板冲突不静默降级：若模板中的状态 / 时间格式 / 冲突策略与当前配置不兼容，batch-annotate --use-template 执行前明确报错退出，不会偷偷套用默认值。
- 批量结果一致性：模板执行的成功数、冲突数在 batch-detail、batch-logs、导出的 CSV / JSON 三者之间完全对应，version 字段保持单调递增。
- 使用 merge 命令重新归并时，默认保留已有事件的标注状态（使用 --no-preserve 可清除）。


## 值班对账快照

支持将班组、成员、当天排班、交接结果和升级命中日志在指定时点固化成可追溯快照，用于核对 CLI 展示与 SQLite 实际落库是否一致。快照、差异结果和操作日志均持久化，程序重启后仍可查询。

### 快照命令一览

| 命令 | 说明 |
|------|------|
| `snapshot-generate` | 生成快照 |
| `snapshot-query` | 按日期或班组查询快照 |
| `snapshot-show` | 查看快照详情 |
| `snapshot-diff` | 比对两份快照差异 |
| `snapshot-diff-detail` | 查看差异详情 |
| `snapshot-export` | 导出快照（JSON/CSV） |
| `snapshot-import` | 导入快照（JSON/CSV） |
| `snapshot-rollback` | 回滚最近一次错误导入 |
| `snapshot-verify` | 验证快照与当前数据库一致性 |
| `snapshot-logs` | 查看快照操作日志 |

### 快照可复现流程（可直接复制粘贴）

```bash
# 1. 初始化并导入数据
python -m inspection_cli.cli -c samples/config.yaml init-demo

# 2. 创建班组
python -m inspection_cli.cli -c samples/config.yaml duty-team-create --name "运维一班" --description "主班组"

# 3. 添加成员
python -m inspection_cli.cli -c samples/config.yaml duty-member-add --team-id TEAM-XXXXXX --name "张班长" --role leader
python -m inspection_cli.cli -c samples/config.yaml duty-member-add --team-id TEAM-XXXXXX --name "李工程师" --role engineer

# 4. 排班
python -m inspection_cli.cli -c samples/config.yaml duty-schedule-set --team-id TEAM-XXXXXX --member-name "张班长" --date 2026-06-17 --shift morning
python -m inspection_cli.cli -c samples/config.yaml duty-schedule-set --team-id TEAM-XXXXXX --member-name "李工程师" --date 2026-06-17 --shift afternoon

# 5. 交班
python -m inspection_cli.cli -c samples/config.yaml duty-handover --team-id TEAM-XXXXXX --operator "张班长" --to "李工程师" --note "早班交中班"

# 6. 生成快照（交班前）
python -m inspection_cli.cli -c samples/config.yaml snapshot-generate --team-id TEAM-XXXXXX -H "张班长" -p "早班前" -d 2026-06-17 -n "交班前快照"

# 7. 生成快照（交班后，需在配置中设置 allow_generate_after_handover: true）
python -m inspection_cli.cli -c samples/config.yaml snapshot-generate --team-id TEAM-XXXXXX -H "张班长" -p "交班后" -d 2026-06-17 -n "交班后快照"

# 8. 查询快照
python -m inspection_cli.cli -c samples/config.yaml snapshot-query --team-id TEAM-XXXXXX --snapshot-date 2026-06-17

# 9. 导出快照（JSON）
python -m inspection_cli.cli -c samples/config.yaml snapshot-export snap_export.json --team-id TEAM-XXXXXX -f json -H "张班长"

# 10. 导出快照（CSV）
python -m inspection_cli.cli -c samples/config.yaml snapshot-export snap_export.csv --team-id TEAM-XXXXXX -f csv -H "张班长"

# 11. 导回快照
python -m inspection_cli.cli -c samples/config.yaml snapshot-import snap_export.json --conflict-strategy force -H "管理员"

# 12. 验证快照与数据库一致性
python -m inspection_cli.cli -c samples/config.yaml snapshot-verify SNAP-XXXXXXXXXXXX

# 13. 比对两份快照差异
python -m inspection_cli.cli -c samples/config.yaml snapshot-diff SNAP-AAAAAAAAAAAA SNAP-BBBBBBBBBBBB -H "张班长"

# 14. 查看操作日志
python -m inspection_cli.cli -c samples/config.yaml snapshot-logs --team-id TEAM-XXXXXX

# 15. 回滚最近一次错误导入（如需要）
python -m inspection_cli.cli -c samples/config.yaml snapshot-rollback -H "管理员"
```

> **注意**: 请将 `TEAM-XXXXXX` 和 `SNAP-XXXXXXXXXXXX` 替换为实际的 ID。

### 快照配置

在 `config.yaml` 中可配置以下快照约束：

```yaml
snapshot:
  exportable_teams: ["运维一班", "运维二班"]     # 可导出的班组，空列表表示不限制
  allow_rollback: true                           # 是否允许回滚错误导入
  max_retention_per_team: 100                    # 每个班组最大保留快照数
  require_team_for_generation: true              # 生成快照是否必须指定班组
  allow_generate_after_handover: false           # 是否允许交班后生成快照
  log_retention_days: 180                        # 操作日志保留天数
  allowed_export_roles: ["leader", "manager"]    # 允许导出的角色
  allowed_generate_roles: ["leader", "manager", "engineer"]  # 允许生成的角色
  allowed_import_roles: ["manager"]              # 允许导入的角色
```

### 冲突提示

快照操作在以下场景会给出明确冲突提示：

- **重复导入**: 快照 ID 已存在时提示使用 `--conflict-strategy force` 覆盖
- **班组被删**: 导入快照时发现班组不存在，明确提示"班组已被删除"
- **交班后限制**: 交班后生成快照会提示配置 `allow_generate_after_handover: true`
- **权限不足**: 角色不在允许列表时提示具体允许的角色
- **可导出班组限制**: 导出不在 `exportable_teams` 列表中的班组时提示
