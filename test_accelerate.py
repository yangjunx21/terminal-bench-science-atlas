import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import accelerate
import crawl


class Response(io.BytesIO):
    def __init__(self, value, content_range, etag='"v1"'):
        super().__init__(value)
        self.status = 206
        self.headers = {"Content-Range": content_range, "ETag": etag}


class RangeTests(unittest.TestCase):
    def test_redaction_projection_preserves_quoted_message_text(self):
        source='{"steps":[{"message":"keep [REDACTED] in text","completion_tokens":[REDACTED]}]}'
        reading=accelerate.redacted_reading_copy(source)
        self.assertEqual(reading['steps'][0]['message'],'keep [REDACTED] in text')
        self.assertIsNone(reading['steps'][0]['completion_tokens'])

    def test_nonstandard_source_is_archived_exactly_with_a_labeled_reading_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            source='{"steps":[\r\n{"message":"keep [REDACTED]","completion_tokens":[REDACTED]}\r\n]}'
            raw=source.encode()
            item={"id":"redacted","url":"https://example.test/public","size":len(raw)}
            path=accelerate.part_path(root,item,0,len(raw)-1)
            path.parent.mkdir(parents=True)
            path.write_bytes(raw)
            accelerate.finalize(root,item,[(0,len(raw)-1)])
            cached=json.loads((root/'_runs/redacted.json').read_text())
            self.assertEqual(cached['raw_trajectory'],source)
            self.assertIsNone(cached['trajectory'])
            self.assertEqual(cached['source_format'],'nonstandard_json')
            self.assertEqual(cached['reading_trajectory']['steps'][0]['message'],'keep [REDACTED]')

    def test_truncated_range_resumes_from_the_saved_offset(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            item = {"id":"t", "url":"https://example.test/public", "size":8, "etag":'"v1"'}
            requests = []
            def open_response(request, **kwargs):
                requests.append(request.get_header("Range"))
                return Response(b"abc", "bytes 0-7/8") if len(requests)==1 else Response(b"defgh", "bytes 3-7/8")
            with patch.object(accelerate,"urlopen",side_effect=open_response), patch.object(accelerate.time,"sleep"):
                accelerate.fetch_part(root,item,0,7,{"public_key":"public"},accelerate.Meter(interval=0))
            self.assertEqual(requests,["bytes=0-7","bytes=3-7"])
            self.assertEqual(accelerate.part_path(root,item,0,7).read_bytes(),b"abcdefgh")

    def test_mismatched_source_range_never_appends(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            item = {"id":"t", "url":"https://example.test/public", "size":8, "etag":'"v1"'}
            path = accelerate.part_path(root,item,0,7)
            path.parent.mkdir(parents=True)
            path.write_bytes(b"abc")
            with patch.object(accelerate,"urlopen",return_value=Response(b"abcdefgh","bytes 0-7/8")):
                with self.assertRaisesRegex(ValueError,"exact byte range"):
                    accelerate.fetch_part(root,item,0,7,{"public_key":"public"},accelerate.Meter(interval=0),retries=0)
            self.assertEqual(path.read_bytes(),b"abc")

    def test_assembled_source_is_validated_then_wrapped_losslessly(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = {"steps":[{"message":"中文\n", "opaque":{"base64":"abcdef"}}],"extra":[1,None]}
            raw = json.dumps(original,ensure_ascii=False,indent=2).encode()
            item = {"id":"t", "url":"https://example.test/public", "size":len(raw)}
            chunks = [(start,min(start+13,len(raw))-1) for start in range(0,len(raw),13)]
            for start,end in chunks:
                path = accelerate.part_path(root,item,start,end)
                path.parent.mkdir(parents=True,exist_ok=True)
                path.write_bytes(raw[start:end+1])
            accelerate.finalize(root,item,chunks)
            result = json.loads((root/'_runs/t.json').read_text())
            self.assertEqual(result["trajectory"],original)
            self.assertEqual(result["source_bytes"],len(raw))
            self.assertEqual(crawl.cache_status(root/'_runs/t.json'),"downloaded")
            self.assertFalse(list((root/'_parts/t').glob('*.part')))


if __name__=="__main__":
    unittest.main()
