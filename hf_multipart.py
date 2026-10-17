"""Parallel parts for the Hub's multipart LFS protocol; signed URLs stay in memory."""
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import math
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time
import xml.etree.ElementTree as ET


def completion_payload(etags, digest):
    if not etags or any(not value for value in etags):
        raise ValueError('Missing multipart ETag')
    return {'oid': digest, 'parts': [{'partNumber': i + 1, 'etag': etag} for i, etag in enumerate(etags)]}


class MultipartUploader:
    def __init__(self, workers=32):
        try:
            import resource
            soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
            target = 2048 if hard == resource.RLIM_INFINITY else min(2048, hard)
            if soft < target:
                resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
        except (ImportError, OSError, ValueError):
            pass
        self.pool = ThreadPoolExecutor(max_workers=workers)
        self.temporary = tempfile.TemporaryDirectory(prefix='tb-science-upload-parts-')
        self.lock = threading.Lock()
        self.large_gate = threading.Semaphore(1)
        self.received = 0
        self.started = time.monotonic()
        self.last_report = self.started

    def close(self):
        self.pool.shutdown(wait=True, cancel_futures=True)
        self.temporary.cleanup()

    def put(self, source, offset, size, url, part_name, require_etag=True):
        path = Path(self.temporary.name) / part_name
        response_path = path.with_name(path.name + '.response')
        try:
            with source.open('rb') as original, path.open('wb') as destination:
                original.seek(offset)
                remaining = size
                while remaining:
                    chunk = original.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ValueError('Source archive is truncated')
                    destination.write(chunk)
                    remaining -= len(chunk)
            config = 'url = ' + json.dumps(url) + '\n'
            for attempt in range(1, 9):
                result = subprocess.run([
                    '/usr/bin/curl', '--config', '-', '--upload-file', str(path),
                    '--silent', '--show-error', '--fail-with-body', '--location', '--max-redirs', '10',
                    '--proto', '=https', '--proto-redir', '=https', '--connect-timeout', '20',
                    '--max-time', '1800', '--dump-header', '-', '--output', str(response_path),
                    '--write-out', '\nTB_BYTES=%{size_upload}\nTB_HTTP=%{http_code}\n',
                ], input=config, text=True, capture_output=True)
                if result.returncode == 0:
                    etags = re.findall(r'^etag:\s*([^\r\n]+)', result.stdout, re.MULTILINE | re.IGNORECASE)
                    transferred = re.findall(r'TB_BYTES=(\d+)', result.stdout)
                    statuses = re.findall(r'TB_HTTP=(\d+)', result.stdout)
                    if transferred and int(transferred[-1]) == size and statuses and 200 <= int(statuses[-1]) < 300 and (etags or not require_etag):
                        with self.lock:
                            self.received += size
                            now = time.monotonic()
                            if now - self.last_report > 15:
                                print(f'Multipart: {self.received/1e9:.3f} GB transferred, '
                                      f'{self.received/max(1,now-self.started)/1e6:.2f} MB/s', flush=True)
                                self.last_report = now
                        return etags[-1].strip() if etags else None
                statuses = re.findall(r'TB_HTTP=(\d+)', result.stdout)
                server_code = None
                try:
                    server_code = ET.fromstring(response_path.read_bytes()).findtext('Code')
                except (OSError, ET.ParseError):
                    pass
                if statuses and (int(statuses[-1]) in (401, 403, 404) or
                                 int(statuses[-1]) == 400 and server_code not in ('RequestTimeout', 'IncompleteBody')):
                    break  # Refresh the upload plan instead of retrying an expired URL.
                if attempt < 8:
                    time.sleep(min(15, attempt * 2))
            statuses = re.findall(r'TB_HTTP=(\d+)', result.stdout)
            sent = re.findall(r'TB_BYTES=(\d+)', result.stdout)
            detail = f'curl={result.returncode}, http={statuses[-1] if statuses else "unknown"}, sent={sent[-1] if sent else 0}/{size}'
            print('Part request failed: ' + detail, flush=True)
            raise RuntimeError('Multipart part failed: ' + detail)
        finally:
            path.unlink(missing_ok=True)
            response_path.unlink(missing_ok=True)

    def upload(self, source, descriptor, repo):
        if descriptor['bytes'] > 64 * 1024 * 1024:
            # Give one large archive the available part connections. Too many
            # simultaneous objects can starve sockets and cause S3 idle timeouts.
            with self.large_gate:
                return self._upload(source, descriptor, repo)
        return self._upload(source, descriptor, repo)

    def _upload(self, source, descriptor, repo):
        from huggingface_hub.lfs import UploadInfo, LFS_HEADERS, post_lfs_batch_info
        from huggingface_hub.utils import build_hf_headers, get_session, hf_raise_for_status

        with source.open('rb') as stream:
            sample = stream.read(512)
        info = UploadInfo(size=descriptor['bytes'], sample=sample, sha256=bytes.fromhex(descriptor['sha256']))
        actions, errors, _ = post_lfs_batch_info([info], token=None, repo_type='dataset', repo_id=repo,
                                                transfers=['multipart', 'basic'])
        if errors or not actions:
            raise RuntimeError('Hub rejected upload metadata')
        instructions = actions[0].get('actions')
        if not instructions:
            return
        action = instructions['upload']
        headers = action.get('header', {})
        chunk_size = int(headers.get('chunk_size', descriptor['bytes']))
        if 'chunk_size' not in headers:
            self.put(source, 0, descriptor['bytes'], action['href'], descriptor['sha256'] + '.whole', require_etag=False)
        else:
            parts = sorted((int(key), value) for key, value in headers.items() if key.isdigit())
            if [key for key, _ in parts] != list(range(1, math.ceil(descriptor['bytes'] / chunk_size) + 1)):
                raise ValueError('Hub returned an incomplete multipart plan')
            etags = [None] * len(parts)
            futures = {}
            for number, url in parts:
                offset = (number - 1) * chunk_size
                future = self.pool.submit(self.put, source, offset, min(chunk_size, descriptor['bytes'] - offset),
                                          url, f'{descriptor["sha256"]}.{number}')
                futures[future] = number - 1
            failed = False
            for future in as_completed(futures):
                if future.cancelled():
                    continue
                try:
                    etags[futures[future]] = future.result()
                except Exception:
                    failed = True
                    for other in futures:
                        other.cancel()
            if failed:
                # Every started part has exited and removed its temporary file
                # before another attempt can use the same archive/part names.
                raise RuntimeError('Multipart transfer did not finish every part')
            response = get_session().post(action['href'], json=completion_payload(etags, descriptor['sha256']),
                                          headers=LFS_HEADERS, timeout=120)
            hf_raise_for_status(response)
        verify = instructions.get('verify')
        if verify:
            response = get_session().post(verify['href'], json={'oid': descriptor['sha256'], 'size': descriptor['bytes']},
                                          headers=build_hf_headers(), timeout=120)
            hf_raise_for_status(response)
