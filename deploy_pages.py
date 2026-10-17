#!/usr/bin/env python3
"""Publish the exported reading snapshot through bounded parallel Git pushes."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

HERE = Path(__file__).resolve().parent
DEFAULT_REPO = 'yangjunx21/terminal-bench-science-atlas'


def enable_pages(repo=DEFAULT_REPO, branch='codex/public-site'):
    endpoint = f'repos/{repo}/pages'
    response = subprocess.run(['gh', 'api', endpoint], capture_output=True, text=True)
    settings = json.loads(response.stdout)
    desired = {'source': {'branch': branch, 'path': '/'}, 'build_type': 'legacy'}
    if response.returncode:
        if str(settings.get('status')) != '404':
            raise RuntimeError('Unable to read GitHub Pages settings')
        method = 'POST'
    elif settings.get('source') == desired['source'] and settings.get('build_type') == 'legacy':
        return settings
    else:
        method = 'PUT'
    result = subprocess.run(['gh', 'api', '--method', method, endpoint, '--input', '-'],
                            input=json.dumps(desired), capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def publish(root, state, branch, workers):
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        target = 2048 if hard == resource.RLIM_INFINITY else min(2048, hard)
        if soft < target:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
    except (ImportError, OSError, ValueError):
        pass
    def git(args, **kwargs):
        return subprocess.check_output(['git', *args], cwd=root, **kwargs)

    git(['check-ref-format', 'refs/heads/' + branch])
    tree = git(['rev-parse', 'HEAD^{tree}'], text=True).strip()
    entries = []
    for line in git(['ls-tree', '-rlz', 'HEAD']).split(b'\0'):
        if line:
            head, path = line.split(b'\t', 1)
            mode, kind, sha, size = head.split()
            entries.append((int(size), mode + b' ' + kind + b' ' + sha + b'\t' + path + b'\0'))
    old = json.loads(state.read_text()) if state.exists() else {}
    if old.get('tree') == tree:
        shards = old['shards']
    else:
        count = min(workers, len(entries))
        bins, sizes = [[] for _ in range(count)], [0] * count
        for size, line in sorted(entries, reverse=True):
            index = min(range(count), key=lambda index: sizes[index])
            bins[index].append(line)
            sizes[index] += size
        shards, stamp = [], str(int(time.time()))
        for index, lines in enumerate(bins):
            with tempfile.TemporaryDirectory() as directory:
                env = dict(os.environ, GIT_INDEX_FILE=str(Path(directory) / 'index'))
                git(['read-tree', '--empty'], env=env)
                subprocess.run(['git', 'update-index', '-z', '--index-info'], input=b''.join(lines), env=env, cwd=root, check=True)
                partial = git(['write-tree'], env=env, text=True).strip()
                sha = git(['commit-tree', partial, '-m', f'Public reading snapshot part {index + 1}/{count}'], text=True).strip()
            name = f'codex/site-part-{stamp}-{index:02d}'
            git(['update-ref', 'refs/heads/' + name, sha])
            shards.append({'branch': name, 'sha': sha, 'bytes': sizes[index]})
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps({'tree': tree, 'shards': shards}))

    def push(item):
        for attempt in range(3):
            result = subprocess.run(['git', '-c', 'http.version=HTTP/1.1', '-c', 'pack.threads=1', '-c', 'pack.compression=1',
                                     'push', '--quiet', 'origin', item['branch']], cwd=root, capture_output=True, text=True, timeout=600)
            if result.returncode == 0:
                return
            time.sleep(3 * (attempt + 1))
        raise RuntimeError('Static snapshot part push failed; rerun to resume')

    remaining = shards
    for attempt in range(3):
        failed = []
        completed = len(shards) - len(remaining)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            pending = {pool.submit(push, item): item for item in remaining}
            for future in as_completed(pending):
                try:
                    future.result()
                    completed += 1
                except Exception:
                    failed.append(pending[future])
                print(f'Uploaded {completed}/{len(shards)} parts', flush=True)
        if not failed:
            break
        remaining = failed
    else:
        raise RuntimeError(f'{len(remaining)} snapshot parts still need retrying')
    parents = []
    for item in shards:
        parents += ['-p', item['sha']]
    previous = git(['ls-remote', '--heads', 'origin', 'refs/heads/' + branch], text=True).strip()
    if previous:
        subprocess.run(['git', 'fetch', '--quiet', 'origin', branch], cwd=root, check=True)
        parents += ['-p', previous.split()[0]]
    sha = git(['commit-tree', tree, *parents, '-m', 'Publish the complete TB Science reading snapshot'], text=True).strip()
    git(['update-ref', 'refs/heads/' + branch, sha])
    subprocess.run(['git', 'push', 'origin', branch], cwd=root, check=True)
    subprocess.run(['git', 'push', '--quiet', 'origin', '--delete', *[item['branch'] for item in shards]], cwd=root, check=True)
    print(json.dumps({'published_branch': branch, 'commit': sha}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checkout', type=Path, default=HERE / 'pages-host')
    parser.add_argument('--state', type=Path, default=HERE / 'dataset/pages-shards.json')
    parser.add_argument('--branch', default='codex/public-site')
    parser.add_argument('--workers', type=int, default=32)
    parser.add_argument('--enable', action='store_true', help='Enable GitHub Pages after the complete branch push')
    parser.add_argument('--repo', default=DEFAULT_REPO)
    args = parser.parse_args()
    if not 1 <= args.workers <= 128:
        parser.error('--workers must be between 1 and 128')
    publish(args.checkout.resolve(), args.state.resolve(), args.branch, args.workers)
    if args.enable:
        settings = enable_pages(args.repo, args.branch)
        print(json.dumps({'site': settings['html_url'], 'status': settings['status']}), flush=True)


if __name__ == '__main__':
    main()
