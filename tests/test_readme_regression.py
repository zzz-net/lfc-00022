# -*- coding: utf-8 -*-
"""
README 回归测试
覆盖三个维度：
  1. 文档编码与换行可读（UTF-8、无 U+FFFD、每一行可读）
  2. 命令逐条可复制（README 中所有 bash 代码块内的命令均可成功 --help）
  3. 重启后模板仍能读回（template-save 后重新加载数据库仍能读取）
"""
import io
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import shutil

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
README = os.path.join(ROOT, "README.md")
PY = sys.executable
CLI_MODULE = "inspection_cli.cli"
CONFIG = os.path.join(ROOT, "samples", "config.yaml")


def _run(*args, cwd=ROOT):
    return subprocess.run(
        [PY, "-m", CLI_MODULE, "-c", CONFIG] + list(args),
        cwd=cwd,
        capture_output=True, text=True, encoding="gbk", errors="replace",
    )


# ---------------------------------------------------------------------------
# 1. 文档编码与换行可读
# ---------------------------------------------------------------------------

class TestReadmeEncoding:
    def test_utf8_no_bom(self):
        with io.open(README, "rb") as f:
            head = f.read(4)
        assert not head.startswith(b"\xef\xbb\xbf"), \
            "README 不应带 UTF-8 BOM"

    def test_no_ufffd(self):
        with io.open(README, "r", encoding="utf-8") as f:
            content = f.read()
        assert "\ufffd" not in content, \
            "README 不应含 U+FFFD 替换字符（原文编码损坏）"

    def test_every_line_is_readable(self):
        with io.open(README, "r", encoding="utf-8") as f:
            lines = f.readlines()
        assert len(lines) >= 100, "README 行数过少，可能文件损坏"
        # 每一行都应是合法的可读文本，不含控制字符（允许 tab 和普通空白）
        for i, ln in enumerate(lines, 1):
            for ch in ln.rstrip("\n\r"):
                if ch in "\t" or ch == " ":
                    continue
                o = ord(ch)
                if o < 32 or o == 127:
                    pytest.fail(
                        "README 第 {} 行含不可读控制字符 U+{:04X}".format(i, o)
                    )

    def test_lf_line_endings(self):
        with io.open(README, "rb") as f:
            raw = f.read()
        assert b"\r" not in raw, \
            "README 应使用 LF 换行，不含 CR"


# ---------------------------------------------------------------------------
# 2. 命令逐条可复制 —— 提取 README 中所有 ```bash 代码块内的命令，
#    用 --help 或实际执行验证命令存在且参数格式合法。
# ---------------------------------------------------------------------------

# 合法的命令子命令（从 CLI 实际 help 中取）
_LEGAL_CMDS = {
    "annotate", "batch-annotate", "batch-cleanup", "batch-detail",
    "batch-logs", "export", "import", "init-demo", "list", "merge",
    "statuses", "template-copy", "template-delete", "template-export",
    "template-import", "template-import-detail", "template-import-logs",
    "template-list", "template-save", "template-show", "undo",
}
# 文档里的命令用了 -c samples/config.yaml，且占位符如 <事件ID>、<模板名> 等。
# 为了"可复制"校验，我们：
#   a) 去掉所有 -c samples/config.yaml 和占位符参数
#   b) 对每条命令追加 --help 再跑一次，验证子命令与参数格式不会因解析错误直接崩
#   c) 所有命令的 exit 码应该是 0（help 输出正常）

_PLACEHOLDER_RE = re.compile(r"<[^>]+>")


def _extract_bash_commands():
    """提取 README 里所有 ```bash ... ``` 代码块中的每一行命令。"""
    with io.open(README, "r", encoding="utf-8") as f:
        content = f.read()
    blocks = re.findall(r"```bash\n(.*?)```", content, re.DOTALL)
    cmds = []
    for block in blocks:
        for line in block.splitlines():
            s = line.strip()
            if not s:
                continue
            if s.startswith("#"):
                continue
            cmds.append(s)
    return cmds


class TestReadmeCommands:
    @pytest.fixture(scope="class")
    def commands(self):
        cmds = _extract_bash_commands()
        assert len(cmds) >= 15, "README 中命令数量过少（提取到 {} 条）".format(len(cmds))
        return cmds

    def test_each_cmd_has_valid_subcommand(self, commands):
        """每条命令的首个子命令必须在 CLI 注册表中（跳过 pip 等非 CLI 命令）。"""
        import shlex
        for cmd in commands:
            try:
                parts = shlex.split(cmd)
            except ValueError:
                # shlex 解析失败时用空格简单拆分
                parts = cmd.split()
            if not parts:
                continue
            i = 0
            # 跳过解释器
            while i < len(parts) and parts[i] in {"python", "python3", "pip", "pip3"}:
                i += 1
            # 跳过 -m xxx
            if i < len(parts) and parts[i] == "-m":
                i += 2
            # 跳过全局选项如 -c xxx --config xxx
            while i < len(parts) and parts[i].startswith("-"):
                opt = parts[i]
                i += 1
                if opt in {"-c", "--config"} and i < len(parts):
                    i += 1
            if i >= len(parts):
                # 不是 CLI 命令（如 pip install），跳过
                continue
            subcmd = parts[i]
            # 跳过非 CLI 子命令
            if subcmd in {"install", "freeze", "list"} and parts[0] in {"pip", "pip3", "python", "python3"}:
                continue
            assert subcmd in _LEGAL_CMDS, \
                "未知子命令 '{}'，命令: {}".format(subcmd, cmd)

    def test_each_cmd_runs_with_help(self, commands):
        """将命令中的占位符去掉，末尾加 --help 实际执行，验证命令可以被解析。"""
        import shlex
        for cmd in commands:
            # 只处理我们自己 CLI 的命令（python -m inspection_cli.cli）
            if "inspection_cli.cli" not in cmd:
                continue
            # 跳过故意用坏配置（config_bad）的失败场景示例
            if "config_bad" in cmd:
                continue
            # 去掉占位符参数（形如 <事件ID> 这种）
            sanitized = _PLACEHOLDER_RE.sub("", cmd)
            try:
                parts = shlex.split(sanitized)
            except ValueError:
                parts = sanitized.split()
            # 末尾追加 --help
            if "--help" not in parts and "-h" not in parts:
                parts.append("--help")
            result = subprocess.run(
                parts,
                cwd=ROOT,
                capture_output=True, text=True, encoding="gbk", errors="replace",
            )
            assert result.returncode == 0, (
                "README 命令无法解析（追加 --help 后返回码 {}）：\n"
                "  原始: {}\n"
                "  执行: {}\n"
                "  stderr: {}".format(
                    result.returncode, cmd, " ".join(parts), result.stderr
                )
            )


# ---------------------------------------------------------------------------
# 3. 重启后模板仍能读回
# ---------------------------------------------------------------------------

class TestReadmeRestartPersistence:
    @pytest.fixture()
    def isolated_db(self, tmp_path):
        """在临时目录创建专用数据库，避免污染现有 inspection.db。"""
        tmp_db = tmp_path / "test.db"
        # 拷贝一份配置并修改 db_path
        import yaml
        with io.open(CONFIG, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        cfg["db_path"] = str(tmp_db)
        tmp_cfg = tmp_path / "config.yaml"
        with io.open(tmp_cfg, "w", encoding="utf-8") as f:
            yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False)
        return tmp_cfg, tmp_db

    def _run_cfg(self, cfg, *args):
        return subprocess.run(
            [PY, "-m", CLI_MODULE, "-c", str(cfg)] + list(args),
            cwd=ROOT,
            capture_output=True, text=True, encoding="gbk", errors="replace",
        )

    def test_template_persists_across_invocation(self, isolated_db):
        cfg, db_path = isolated_db
        tpl = "daily-close"
        desc = "每日批量关闭待确认"

        # 第一次 CLI 进程：导入 -> 归并 -> 保存模板
        r = self._run_cfg(
            cfg, "import", os.path.join(ROOT, "samples", "inspection_sample.csv"),
        )
        assert r.returncode == 0, r.stderr
        r = self._run_cfg(cfg, "merge")
        assert r.returncode == 0, r.stderr
        r = self._run_cfg(
            cfg,
            "template-save",
            "-n", tpl,
            "-d", desc,
            "--statuses", "unconfirmed",
            "--set-status", "closed",
            "--set-handler", "admin",
            "--set-note", "批量关闭",
        )
        assert r.returncode == 0, r.stderr
        assert tpl in r.stdout

        # 直接通过 SQLite 确认模板入库
        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute(
                "SELECT name, description FROM batch_templates WHERE name = ?",
                (tpl,),
            ).fetchall()
        finally:
            conn.close()
        assert len(rows) == 1
        assert rows[0][0] == tpl
        assert rows[0][1] == desc

        # 第二次 CLI 进程：重新起一个新进程，读取模板（模拟重启）
        r2 = self._run_cfg(cfg, "template-show", tpl)
        assert r2.returncode == 0, r2.stderr
        assert tpl in r2.stdout
        assert desc in r2.stdout
        assert "兼容" in r2.stdout  # 兼容性检查输出存在

        # 第三次 CLI 进程：使用模板执行批量，然后 batch-detail 核对
        r3 = self._run_cfg(
            cfg, "batch-annotate", "--use-template", tpl, "-H", "tester", "-y",
        )
        assert r3.returncode == 0, r3.stderr
        m = re.search(r"BATCH-[A-F0-9]+", r3.stdout)
        assert m, "批量操作 ID 未在输出中出现: " + r3.stdout
        batch_id = m.group(0)

        # batch-detail 验证：成功数 = event 总数
        r4 = self._run_cfg(cfg, "batch-detail", batch_id)
        assert r4.returncode == 0, r4.stderr
        detail_total = re.search(r"总计:\s*(\d+)", r4.stdout)
        detail_success = re.search(r"成功:\s*(\d+)", r4.stdout)
        assert detail_total and detail_success
        assert detail_total.group(1) == detail_success.group(1)

        # 导出 CSV 交叉核对
        tmp_csv = os.path.join(os.path.dirname(str(db_path)), "out.csv")
        r5 = self._run_cfg(cfg, "export", tmp_csv)
        assert r5.returncode == 0, r5.stderr
        closed_count = 0
        with io.open(tmp_csv, "r", encoding="utf-8") as f:
            import csv
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("status") == "closed":
                    closed_count += 1
        assert str(closed_count) == detail_success.group(1), (
            "CSV closed {} 与 batch-detail 成功 {} 不一致".format(
                closed_count, detail_success.group(1)
            )
        )
