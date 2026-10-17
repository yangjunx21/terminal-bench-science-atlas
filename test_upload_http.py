"""Bounded commits and remote checksum validation for resumed dataset uploads."""
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import upload_http


class HttpUploadTests(unittest.TestCase):
    def test_batches_respect_bytes_and_file_limits_without_losing_large_files(self):
        files = [{'path': str(i), 'bytes': size} for i, size in enumerate([9, 8, 7, 6, 80, 2])]
        batches = list(upload_http.batches(files, max_files=2, max_bytes=15))
        self.assertEqual(sorted(item['path'] for batch in batches for item in batch), sorted(item['path'] for item in files))
        for batch in batches:
            self.assertLessEqual(len(batch), 2)
            self.assertTrue(sum(item['bytes'] for item in batch) <= 15 or len(batch) == 1)
        self.assertEqual(batches[-1][0]['bytes'], 80)

    def test_existing_file_needs_matching_size_and_content_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'data.gz'
            path.write_bytes(b'archive')
            descriptor = {'bytes': 7, 'sha256': hashlib.sha256(b'archive').hexdigest()}
            remote = SimpleNamespace(size=7, lfs=SimpleNamespace(sha256=descriptor['sha256']))
            self.assertTrue(upload_http.remote_matches(remote, descriptor, path))
            remote.lfs.sha256 = 'wrong'
            self.assertFalse(upload_http.remote_matches(remote, descriptor, path))
            remote.lfs = None
            remote.blob_id = hashlib.sha1(b'blob 7\0archive').hexdigest()
            self.assertTrue(upload_http.remote_matches(remote, descriptor, path))
            remote.size = 6
            self.assertFalse(upload_http.remote_matches(remote, descriptor, path))
            self.assertFalse(upload_http.remote_matches(None, descriptor, path))

    def test_successful_calls_cannot_mark_complete_if_remote_archive_is_missing(self):
        states = []
        class Api:
            def create_repo(self, *args, **kwargs):
                self.private = kwargs['private']
            def dataset_info(self, *args, **kwargs):
                return SimpleNamespace(siblings=[])
            def create_commit(self, **kwargs):
                payload = kwargs['operations'][-1].path_or_fileobj
                states.append(json.loads(payload))
                return SimpleNamespace(oid='test')
        api = Api()
        module = SimpleNamespace(HfApi=lambda: api, CommitOperationAdd=lambda **kwargs: SimpleNamespace(**kwargs))
        with tempfile.TemporaryDirectory() as directory, patch.dict('sys.modules', {'huggingface_hub': module}), patch.dict('os.environ'):
            stage = Path(directory)
            (stage/'manifest.json').write_text(json.dumps({'created_at': 'test'}))
            (stage/'inventory.json').write_text(json.dumps([{'path':'archive.gz','bytes':7,'sha256':'expected'}]))
            with self.assertRaisesRegex(RuntimeError, 'Remote checksum verification failed'):
                upload_http.upload(stage, 'owner/dataset')
            self.assertTrue(api.private)
            self.assertTrue(all(state['state'] != 'complete' for state in states))
            self.assertEqual(json.loads((stage/'upload-status.json').read_text())['state'], 'interrupted')


if __name__ == '__main__':
    unittest.main()
