"""Network-free regression tests: python3 -m unittest discover -s tools/harbor-science."""
import gzip
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

spec = importlib.util.spec_from_file_location("harbor_science", Path(__file__).with_name("crawl.py"))
crawl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(crawl)


def html(props):
    return '<script>self.__next_f.push([1,' + json.dumps('a:' + json.dumps(props) + '\n') + '])</script>'


class ArchiveTests(unittest.TestCase):
    def test_flight_html_escapes_and_text_records(self):
        props = {"trial": {"message": '中文\n"quoted" </script> \\n'}}
        self.assertEqual(crawl.find_props(html(props), "trial"), props)
        self.assertEqual(list(crawl.rsc_objects('1:T14,text not JSON\n2:{"steps":[]}')), [{"steps": []}])

    def test_pagination_collects_all_trials(self):
        class Client:
            origin = "https://hub.harborframework.com"
            def request(self, path):
                page = 2 if "page=2" in path else 1
                return html({"initialTrials": {"items": [{"id": str(page)}], "total": 2, "total_pages": 2}})
        row = {"id": "row", "metadata": {"model_display": {"label": "Model"}}, "metrics": {"tasks": 2}}
        model, trials = crawl.discover_row(Client(), "/datasets/o/d/latest", {"name": "b", "id": "b"}, row)
        self.assertEqual({t["id"] for t in trials}, {"1", "2"})
        self.assertEqual(model["coverage"], "public_trials")

    def test_pagination_refuses_silent_duplicate_page(self):
        class Client:
            origin = "https://hub.harborframework.com"
            def request(self, path):
                return html({"initialTrials": {"items": [{"id": "same"}], "total": 2, "total_pages": 2}})
        with self.assertRaisesRegex(ValueError, "Pagination incomplete"):
            crawl.discover_row(Client(), "/datasets/o/d/latest", {"name": "b", "id": "b"}, {"id":"r", "metadata":{}, "metrics":{}})

    def test_metadata_only_row_is_explicit(self):
        class Client:
            origin = "https://hub.harborframework.com"
            def request(self, path):
                return html({"initialTrials": {"items": [], "total": 0, "total_pages": 0}})
        model, trials = crawl.discover_row(Client(), "/datasets/o/d/latest", {"name":"b", "id":"b"}, {"id":"r", "metadata":{}, "metrics":{"tasks":210}})
        self.assertEqual(model["official_trials"], 210)
        self.assertEqual(model["coverage"], "metadata_only")
        self.assertFalse(trials)

    def test_resume_and_retry_preserve_atif_without_modification(self):
        trajectory = {"schema_version":"ATIF-v1.7", "steps":[{"source":"agent", "message":"中文\ncode", "unknown_field":{"x":1}}], "extra": {"opaque": [1,2]}}
        class Client:
            origin = "https://hub.harborframework.com"
            calls = 0
            def request(self, path, **kwargs):
                self.calls += 1
                return json.dumps(trajectory)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); client = Client(); trial = {"id":"trial", "job_id":"job"}
            self.assertEqual(crawl.download(client,root,trial), "downloaded")
            self.assertEqual(crawl.download(client,root,trial), "cached")
            self.assertEqual(client.calls,1)
            self.assertEqual(json.loads((root/"_runs/trial.json").read_text())["trajectory"],trajectory)
            self.assertEqual(crawl.download(client,root,trial,True), "downloaded")
            self.assertEqual(client.calls,2)

    def test_missing_and_invalid_trajectory_are_not_successes(self):
        class Client:
            origin = "https://hub.harborframework.com"
            def request(self, path, **kwargs): return 'null'
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);client=Client();trial={"id":"t","job_id":"j"}
            self.assertEqual(crawl.download(client,root,trial), "unavailable")
            client.request = lambda *a, **kw: '{"error":"not ATIF"}'
            self.assertEqual(crawl.download(client,root,trial), "error")

    def test_server_limit_falls_back_to_public_original_atif(self):
        class Client:
            origin = "https://hub.harborframework.com"
            paths = []
            def request(self, path, **kwargs):
                self.paths.append(path)
                if "/files/agent/trajectory.json" not in path:
                    raise HTTPError(self.origin + path, 500, "response too large", {}, None)
                return '{"schema_version":"ATIF-v1.7","steps":[]}'
        with tempfile.TemporaryDirectory() as directory:
            client=Client();root=Path(directory)
            self.assertEqual(crawl.download(client,root,{"id":"t","job_id":"j"}),"downloaded")
            self.assertEqual(len(client.paths),2)
            self.assertIn('/files/agent/trajectory.json?',json.loads((root/'_runs/t.json').read_text())["source_url"])

    def test_null_normalized_route_can_still_have_an_original(self):
        class Client:
            origin = "https://hub.harborframework.com"
            def request(self, path, **kwargs):
                return '{"steps":[]}' if '/files/agent/' in path else 'null'
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(crawl.download(Client(),Path(directory),{"id":"t","job_id":"j"}),"downloaded")

    def test_export_lossless_multiple_runs_and_missing_scores(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/"dataset";site=Path(directory)/"site"
            model={"id":"m", "name":"Model", "directory":"model", "agent":"agent", "effort":"high", "expected_trials":2, "official_metrics":{"tasks":2}}
            other=dict(model,id="hidden",directory="hidden",expected_trials=0)
            trials=[{"id":"a", "model_id":"m", "job_id":"job", "task_name":"org/task", "reward":0}, {"id":"b", "model_id":"m", "job_id":"job2", "task_name":"org/task", "reward":None}]
            trajectory={"steps":[{"message":"x\ny","source":"agent","tool_calls":[{"arguments":{"x":"a"}}]}],"opaque":True}
            crawl.atomic(root/"catalog.json",crawl.dumps({"models":[model,other],"trials":trials,"origin":"https://hub.harborframework.com","dataset":"o/d","source_url":"https://hub.harborframework.com","discovery_complete":True}))
            crawl.atomic(root/"_runs/a.json",crawl.dumps({"status":"downloaded", "trajectory":trajectory}))
            manifest=crawl.build(root,site)
            self.assertEqual(manifest["counts"],{"downloaded":1,"pending":1})
            self.assertEqual(manifest["unlisted_trials"],2)
            path=root/manifest["runs"][0]["path"]
            lines=[json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(len(lines),2)
            self.assertEqual(lines[0]["trajectory"],trajectory)
            self.assertEqual(manifest["runs"][0]["tool_calls"],1)
            self.assertIsNone(manifest["runs"][1]["reward"])
            self.assertEqual(gzip.decompress((site/"data"/(manifest["runs"][0]["path"]+".gz")).read_bytes()),path.read_bytes())
            crawl.build(root,site)
            self.assertEqual(path.read_text().count('\n'),2)
            crawl.atomic(root/"_runs/b.json",crawl.dumps({"status":"downloaded", "trajectory":trajectory}))
            with patch.object(crawl,"STATIC_ASSET_BYTES",64):
                updated=crawl.build(root,site)
            self.assertEqual(updated["counts"],{"downloaded":2})
            parts=updated["files"][updated["runs"][0]["path"]]["parts"]
            self.assertGreater(len(parts),1)
            self.assertTrue(all((site/"data"/part).stat().st_size<=64 for part in parts))
            self.assertEqual(gzip.decompress(b''.join((site/"data"/part).read_bytes() for part in parts)),path.read_bytes())

    def test_path_collisions_and_traversal(self):
        self.assertNotEqual(crawl.slug("A/B"),crawl.slug("A B"))
        self.assertNotIn("/",crawl.slug("../../escape"))
        self.assertNotIn("..",crawl.slug("../../escape")[:2])

    def test_text_projection_is_explicit_and_does_not_mutate_original(self):
        original={"steps":[{"message":"keep code","observation":{"content":[{"type":"image","source":{"type":"base64","data":"abcdef","media_type":"image/png"}},{"type":"text","text":"keep tool output"}]}}]}
        copy=json.loads(json.dumps(original))
        projected=crawl.text_projection(original)
        self.assertEqual(original,copy)
        self.assertNotIn('abcdef',json.dumps(projected))
        self.assertIn('keep code',json.dumps(projected))
        self.assertIn('keep tool output',json.dumps(projected))
        self.assertIn('text_only',json.dumps(projected))
        encoded = "A" * 4096
        text = "keep prefix [{'type':'image','source':{'type':'base64','data':'" + encoded + "'}}] keep suffix"
        reduced = crawl.text_projection(text)
        self.assertNotIn(encoded,reduced)
        self.assertIn("keep prefix",reduced)
        self.assertIn("keep suffix",reduced)


if __name__ == "__main__":
    unittest.main()
