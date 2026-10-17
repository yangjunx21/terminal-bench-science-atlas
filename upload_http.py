#!/usr/bin/env python3
"""Upload and verify bounded HTTP/LFS batches using the existing HF login."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import time

import crawl


def batches(files, max_files=20, max_bytes=64 * 1024 * 1024):
    batch, size = [], 0
    for item in sorted(files, key=lambda item: (item['bytes'], item['path'])):
        if batch and (len(batch) >= max_files or size + item['bytes'] > max_bytes):
            yield batch
            batch, size = [], 0
        batch.append(item)
        size += item['bytes']
    if batch:
        yield batch


def remote_matches(remote, descriptor, local):
    if remote is None or remote.size != descriptor['bytes']:
        return False
    lfs = getattr(remote, 'lfs', None)
    if lfs:
        digest = lfs.get('sha256') if isinstance(lfs, dict) else lfs.sha256
        return digest == descriptor['sha256']
    # Small archives may be ordinary Git blobs rather than LFS objects.
    digest = hashlib.sha1(f"blob {descriptor['bytes']}\0".encode())
    with local.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest() == getattr(remote, 'blob_id', None)


def upload(stage, repo, public=False):
    # Set before importing the SDK. The Xet path stalled on this connection.
    os.environ['HF_HUB_DISABLE_XET'] = '1'
    os.environ['HF_HUB_DISABLE_PROGRESS_BARS'] = '1'
    from huggingface_hub import CommitOperationAdd, HfApi

    api = HfApi()
    api.create_repo(repo, repo_type='dataset', private=not public, exist_ok=True)
    manifest = json.loads((stage / 'manifest.json').read_text())
    files = json.loads((stage / 'inventory.json').read_text())
    total_bytes = sum(item['bytes'] for item in files)
    confirmed = []
    started = time.monotonic()

    def status(state, known=None, **extra):
        known = confirmed if known is None else known
        return {'state': state, 'snapshot_at': manifest['created_at'], 'updated_at': crawl.now(),
                'transport': 'http_lfs', 'confirmed_files': len(known), 'total_files': len(files),
                'confirmed_bytes': sum(item['bytes'] for item in known), 'total_bytes': total_bytes, **extra}

    def commit(paths, state, message):
        payload = (crawl.dumps(state) + '\n').encode()
        for attempt in range(1, 5):
            try:
                # Fresh operations on retry: the SDK mutates upload state on them.
                operations = [CommitOperationAdd(path_in_repo=path, path_or_fileobj=stage / path) for path in paths]
                operations.append(CommitOperationAdd(path_in_repo='upload-status.json', path_or_fileobj=payload))
                result = api.create_commit(repo_id=repo, repo_type='dataset', operations=operations,
                                           commit_message=message, num_threads=8)
                crawl.atomic(stage / 'upload-status.json', crawl.dumps(state))
                print(f"Committed {len(paths)} files: {result.oid}", flush=True)
                return result
            except Exception as error:
                print(f"Commit attempt {attempt}/4: {type(error).__name__}", flush=True)
                if attempt == 4:
                    raise
                time.sleep(min(15, attempt * 3))

    def remote_index():
        info = api.dataset_info(repo, files_metadata=True)
        return {item.rfilename: item for item in info.siblings}

    try:
        remote = remote_index()
        confirmed = [item for item in files if remote_matches(remote.get(item['path']), item, stage / item['path'])]
        known = {item['path'] for item in confirmed}
        metadata = ['README.md', 'manifest.json', 'catalog.json', 'inventory.json',
                    'analysis/trials.csv', 'analysis/models.csv', 'analysis/tasks.csv']
        commit(metadata, status('uploading_raw'), 'Update final trajectory capture and analysis tables')
        pending = [item for item in files if item['path'] not in known]
        print(f"Verified {len(confirmed)}/{len(files)} existing archives; {len(pending)} remaining.", flush=True)
        for batch in batches(pending):
            next_known = confirmed + batch
            commit([item['path'] for item in batch], status('uploading_raw', next_known),
                   f"Archive trajectories {len(next_known)}/{len(files)}")
            confirmed = next_known
            elapsed = time.monotonic() - started
            print(f"Progress: {len(confirmed)}/{len(files)} archives; "
                  f"{sum(item['bytes'] for item in confirmed)/1e9:.3f}/{total_bytes/1e9:.3f} GB committed; "
                  f"elapsed {elapsed:.0f}s", flush=True)
        # Never mark complete solely because the upload calls returned successfully.
        remote = remote_index()
        missing = [item['path'] for item in files
                   if not remote_matches(remote.get(item['path']), item, stage / item['path'])]
        if missing:
            raise RuntimeError(f"Remote checksum verification failed for {len(missing)} archives")
        commit([], status('complete', verified_files=len(files)), 'Verify all trajectory archives and complete upload')
        print(f"Verified complete: https://huggingface.co/datasets/{repo}", flush=True)
    except BaseException as error:
        crawl.atomic(stage / 'upload-status.json', crawl.dumps(status('interrupted', error_type=type(error).__name__)))
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', type=Path, required=True)
    parser.add_argument('--repo', required=True)
    parser.add_argument('--public', action='store_true')
    parser.add_argument('--parent-lock-held', action='store_true')
    args = parser.parse_args()
    if args.parent_lock_held:
        upload(args.stage.resolve(), args.repo, args.public)
    else:
        with crawl.dataset_lock(args.stage.resolve()):
            upload(args.stage.resolve(), args.repo, args.public)


if __name__ == '__main__':
    main()
