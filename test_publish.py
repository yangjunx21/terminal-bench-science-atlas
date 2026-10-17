"""Network-free tests for archival packaging and download process detection."""
import csv
import gzip
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import crawl
import publish


class PublishTests(unittest.TestCase):
    def test_legacy_running_process_is_detected_and_blocks_a_new_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            crawl.atomic(root / "crawl-status.json", crawl.dumps({"state": "running", "pid": -123}))
            with patch("subprocess.check_output", return_value="python3 -u crawl.py crawl --workers 8"):
                self.assertTrue(crawl.legacy_writer_active(root))
                with self.assertRaisesRegex(RuntimeError, "older crawler"):
                    with crawl.dataset_lock(root):
                        self.fail("Legacy writer was ignored")
            with patch("subprocess.check_output", return_value="some unrelated command"):
                self.assertFalse(crawl.legacy_writer_active(root))
                with crawl.dataset_lock(root):
                    pass

    def test_lock_contention_and_release(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertFalse(crawl.writer_active(root))
            with crawl.dataset_lock(root):
                self.assertTrue(crawl.writer_active(root))
                with self.assertRaisesRegex(RuntimeError, "Another crawl/build"):
                    with crawl.dataset_lock(root):
                        self.fail("A second writer acquired the lock")
            self.assertFalse(crawl.writer_active(root))

    def test_snapshot_preserves_raw_and_invalidates_changed_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root, stage = Path(directory) / "data", Path(directory) / "stage"
            model = {"id": "m", "name": "Model", "directory": "model", "agent": "agent", "effort": "high",
                     "expected_trials": 2, "official_metrics": {"tasks": 2}, "source_url": "https://source/model"}
            hidden = dict(model, id="hidden", directory="hidden")
            trial = {"id": "a", "model_id": "m", "job_id": "j", "task_name": "org/task", "reward": 1, "is_scored": True}
            pending = dict(trial, id="b", reward=None, is_scored=False)
            catalog = {"models": [model, hidden], "trials": [trial, pending], "origin": "https://hub.harborframework.com",
                       "source_url": "https://hub.harborframework.com/datasets/o/d/latest", "discovery_complete": True}
            raw = {"steps": [{"message": "中文\ncode", "tool_calls": [{"name": "bash", "args": {"a": 1}}]}],
                   "opaque": {"image": "data:image/png;base64,abcdef", "unknown": [1, None]}}
            crawl.atomic(root / "catalog.json", crawl.dumps(catalog))
            crawl.atomic(root / "_runs/a.json", crawl.dumps({"status": "downloaded", "trajectory": raw}))
            manifest = publish.prepare(root, stage)
            self.assertEqual(manifest["counts"], {"downloaded": 1, "pending": 1})
            self.assertEqual(manifest["unlisted_trials"], 2)
            descriptor = json.loads((stage / "inventory.json").read_text())[0]
            target = stage / descriptor["path"]
            with gzip.open(target, "rt") as stream:
                records = [json.loads(line) for line in stream]
            self.assertEqual(records[0]["trajectory"], raw)
            self.assertEqual(records[1]["download_status"], "pending")
            self.assertIsNone(records[1]["trajectory"])
            self.assertEqual(publish.file_hash(target), descriptor["sha256"])
            old_mtime = target.stat().st_mtime_ns
            publish.prepare(root, stage)
            self.assertEqual(target.stat().st_mtime_ns, old_mtime)
            crawl.atomic(root / "_runs/b.json", crawl.dumps({"status": "unavailable", "trajectory": None}))
            updated = publish.prepare(root, stage)
            self.assertEqual(updated["counts"], {"downloaded": 1, "unavailable": 1})
            with (stage / "analysis/trials.csv").open() as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(rows[0]["tool_calls"], "1")
            self.assertEqual(rows[1]["is_scored"], "0")
            self.assertEqual(rows[1]["reward"], "")
            self.assertEqual(crawl.inventory(root)["counts"], {"downloaded": 1, "unavailable": 1})


if __name__ == "__main__":
    unittest.main()
