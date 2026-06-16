"""End-to-end CLI integration test for batch template workflow.

Covers:
  1. template-save + template-list + template-show + template-copy + template-delete
  2. Cross-restart template persistence
  3. batch-annotate --use-template + batch-logs + batch-detail + export consistency
  4. Re-import + merge + re-apply template (version monotonicity)
"""
from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest


class TestCLITemplateE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.project_root = os.path.join(os.path.dirname(__file__), "..")
        cls.sample_config = os.path.join(cls.project_root, "samples", "config.yaml")
        cls.csv_path = os.path.join(cls.project_root, "samples", "inspection_sample.csv")
        cls.json_path = os.path.join(cls.project_root, "samples", "inspection_sample.json")

        cls.tmp_dir = tempfile.mkdtemp()
        cls.db_path = os.path.join(cls.tmp_dir, "e2e_test.db")
        cls.tmp_config = os.path.join(cls.tmp_dir, "config.yaml")

        with open(cls.sample_config, "r", encoding="utf-8") as f:
            cfg = f.read()
        cfg = cfg.replace('db_path: "inspection.db"', f'db_path: "{cls.db_path.replace(os.sep, "/")}"')
        with open(cls.tmp_config, "w", encoding="utf-8") as f:
            f.write(cfg)

        cls.cli_base = [sys.executable, "-m", "inspection_cli.cli", "-c", cls.tmp_config]

    def _run(self, args, input_text=None):
        result = subprocess.run(
            self.cli_base + args,
            capture_output=True, text=True, encoding="gbk", errors="replace",
            input=input_text, timeout=60,
        )
        return result

    def test_01_import_and_merge(self):
        r = self._run(["import", self.csv_path])
        self.assertEqual(r.returncode, 0, f"import csv failed: {r.stderr}\n{r.stdout}")
        r = self._run(["import", self.json_path])
        self.assertEqual(r.returncode, 0, f"import json failed: {r.stderr}\n{r.stdout}")
        r = self._run(["merge"])
        self.assertEqual(r.returncode, 0, f"merge failed: {r.stderr}\n{r.stdout}")

    def test_02_template_save_list_show(self):
        r = self._run([
            "template-save", "-n", "close-unc", "-d", "close unconfirmed",
            "--statuses", "unconfirmed", "--set-status", "closed",
            "--set-handler", "Admin", "--set-note", "batch close",
            "--conflict-strategy", "skip",
        ])
        self.assertEqual(r.returncode, 0, f"template-save failed: {r.stderr}\n{r.stdout}")
        self.assertIn("close-unc", r.stdout)

        r = self._run(["template-list"])
        self.assertEqual(r.returncode, 0, f"template-list failed: {r.stderr}\n{r.stdout}")
        self.assertIn("close-unc", r.stdout)

        r = self._run(["template-show", "close-unc"])
        self.assertEqual(r.returncode, 0, f"template-show failed: {r.stderr}\n{r.stdout}")
        self.assertIn("close-unc", r.stdout)
        self.assertIn("unconfirmed", r.stdout)

    def test_03_template_copy_delete(self):
        r = self._run(["template-copy", "close-unc", "close-v2", "-d", "v2 copy"])
        self.assertEqual(r.returncode, 0, f"template-copy failed: {r.stderr}\n{r.stdout}")
        self.assertIn("close-v2", r.stdout)

        r = self._run(["template-delete", "close-v2", "-y"])
        self.assertEqual(r.returncode, 0, f"template-delete failed: {r.stderr}\n{r.stdout}")

        r = self._run(["template-list"])
        self.assertNotIn("close-v2", r.stdout)
        self.assertIn("close-unc", r.stdout)

    def test_04_cross_restart_persistence(self):
        self._run([
            "template-save", "-n", "persist-test", "-d", "p",
            "--statuses", "unconfirmed", "--set-status", "confirmed",
            "--set-handler", "X",
        ])
        r1 = self._run(["template-show", "persist-test"])
        self.assertEqual(r1.returncode, 0, f"before restart: {r1.stderr}")

        r2 = self._run(["template-show", "persist-test"])
        self.assertEqual(r2.returncode, 0, f"after restart: {r2.stderr}")
        self.assertIn("persist-test", r2.stdout)

    def test_05_batch_annotate_with_template(self):
        r = self._run([
            "batch-annotate", "--use-template", "close-unc",
            "-H", "E2ETester", "-y",
        ])
        self.assertEqual(r.returncode, 0, f"batch-annotate failed: {r.stderr}\n{r.stdout}")
        self.assertIn("BATCH-", r.stdout)

    def test_06_batch_logs_and_detail(self):
        r = self._run(["batch-logs", "-n", "1"])
        self.assertEqual(r.returncode, 0, f"batch-logs failed: {r.stderr}\n{r.stdout}")

        batch_id = None
        for line in r.stdout.splitlines():
            for token in line.split():
                if token.startswith("BATCH-"):
                    batch_id = token
                    break
            if batch_id:
                break
        self.assertIsNotNone(batch_id, f"No BATCH-ID in:\n{r.stdout}")

        r = self._run(["batch-detail", batch_id])
        self.assertEqual(r.returncode, 0, f"batch-detail failed: {r.stderr}\n{r.stdout}")
        self.assertIn(batch_id, r.stdout)

    def test_07_export_csv_json_consistency(self):
        csv_path = os.path.join(self.tmp_dir, "e2e_check.csv")
        json_path = os.path.join(self.tmp_dir, "e2e_check.json")

        r = self._run(["export", csv_path])
        self.assertEqual(r.returncode, 0, f"export csv failed: {r.stderr}")
        r = self._run(["export", json_path, "-f", "json", "--with-records"])
        self.assertEqual(r.returncode, 0, f"export json failed: {r.stderr}")

        with open(csv_path, encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        csv_closed = [r for r in rows if r.get("status") == "closed"]

        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)
        events = data.get("events", data) if isinstance(data, dict) else data
        json_closed = [e for e in events if e.get("status") == "closed"]

        self.assertEqual(len(csv_closed), len(json_closed),
                         f"CSV closed={len(csv_closed)} != JSON closed={len(json_closed)}")

        for c in csv_closed:
            j = next(e for e in json_closed if e["event_id"] == c["event_id"])
            self.assertEqual(c.get("handler"), j.get("handler"),
                             f"handler mismatch for {c['event_id']}: CSV={c.get('handler')} JSON={j.get('handler')}")
            self.assertEqual(int(c["version"]), j["version"],
                             f"version mismatch for {c['event_id']}: CSV={c['version']} JSON={j['version']}")

    def test_08_reimport_merge_and_reapply_template(self):
        self._run([
            "template-save", "-n", "reopen-closed", "-d", "reopen for test",
            "--statuses", "closed", "--set-status", "confirmed",
            "--set-handler", "Reopener", "--overwrite",
        ])

        r = self._run(["import", self.csv_path])
        self.assertEqual(r.returncode, 0, f"reimport failed: {r.stderr}")
        r = self._run(["merge"])
        self.assertEqual(r.returncode, 0, f"re-merge failed: {r.stderr}")

        r = self._run([
            "batch-annotate", "--use-template", "reopen-closed",
            "-H", "ReimportTester", "-y",
        ])
        self.assertEqual(r.returncode, 0, f"batch-annotate reopen failed: {r.stderr}\n{r.stdout}")
        self.assertIn("BATCH-", r.stdout)

        csv_path = os.path.join(self.tmp_dir, "e2e_reimport.csv")
        self._run(["export", csv_path])
        with open(csv_path, encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        for row in rows:
            v = int(row.get("version", 0))
            self.assertGreaterEqual(v, 1, f"version >=1 expected, got {v} for {row.get('event_id')}")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
