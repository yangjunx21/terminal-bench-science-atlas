import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from hf_multipart import MultipartUploader, completion_payload


class MultipartTests(unittest.TestCase):
    def test_completion_keeps_server_etags_in_part_order(self):
        self.assertEqual(completion_payload(['"a"', '"b"'], 'hash'),
                         {'oid':'hash','parts':[{'partNumber':1,'etag':'"a"'}, {'partNumber':2,'etag':'"b"'}]})
        with self.assertRaises(ValueError):
            completion_payload(['"a"', None], 'hash')

    def test_part_upload_uses_exact_slice_and_keeps_signed_url_out_of_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'source';source.write_bytes(b'0123456789')
            uploader=MultipartUploader(workers=1)
            url='https://example.test/object?signature=temporary-secret'
            def run(command, **kwargs):
                self.assertNotIn(url,command)
                self.assertEqual(json.loads(kwargs['input'].split(' = ',1)[1]),url)
                part=Path(command[command.index('--upload-file')+1])
                self.assertEqual(part.read_bytes(),b'3456')
                return SimpleNamespace(returncode=0,stdout='HTTP/1.1 200 OK\r\nETag: "expected"\r\n\r\nTB_BYTES=4\nTB_HTTP=200\n')
            try:
                with patch('hf_multipart.subprocess.run',side_effect=run):
                    self.assertEqual(uploader.put(source,3,4,url,'part'), '"expected"')
                self.assertFalse((Path(uploader.temporary.name)/'part').exists())
            finally:uploader.close()

    def test_short_or_unacknowledged_part_cannot_count_as_transferred(self):
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'source';source.write_bytes(b'0123456789')
            uploader=MultipartUploader(workers=1)
            try:
                with patch('hf_multipart.subprocess.run',return_value=SimpleNamespace(returncode=0,stdout='ETag: "x"\nTB_BYTES=3')), patch('hf_multipart.time.sleep'):
                    with self.assertRaises(RuntimeError):uploader.put(source,0,4,'https://example.test','part')
                self.assertEqual(uploader.received,0)
            finally:uploader.close()

    def test_basic_upload_accepts_success_without_multipart_etag(self):
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'source';source.write_bytes(b'1234')
            uploader=MultipartUploader(workers=1)
            try:
                with patch('hf_multipart.subprocess.run',return_value=SimpleNamespace(returncode=0,stdout='TB_BYTES=4\nTB_HTTP=200\n')):
                    self.assertIsNone(uploader.put(source,0,4,'https://example.test','part',require_etag=False))
                self.assertEqual(uploader.received,4)
            finally:uploader.close()


if __name__=='__main__':unittest.main()
