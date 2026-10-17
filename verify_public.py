#!/usr/bin/env python3
"""Check the public site, repository, and dataset without any login or access token."""
import argparse
from collections import Counter
import csv
import gzip
import hashlib
import json
import io
from pathlib import Path
from urllib.parse import quote, urljoin
from urllib.request import Request, urlopen

CODE_API = 'https://api.github.com/repos/yangjunx21/terminal-bench-science-atlas'
HF_REPO = 'yangjunxiao2021/terminal-bench-science-trajectories'
HF_API = 'https://huggingface.co/api/datasets/' + HF_REPO
HF_FILES = 'https://huggingface.co/datasets/' + HF_REPO + '/resolve/main/'


def fetch(url):
    with urlopen(Request(url, headers={'User-Agent': 'tb-science-public-verifier'}), timeout=90) as response:
        if response.status != 200:
            raise RuntimeError('Public request did not return HTTP 200')
        return response.read()


def read_json(url):
    return json.loads(fetch(url))


def verify(site, revision=None):
    site = site.rstrip('/') + '/'
    assert 'TB Science 轨迹分析' in fetch(site).decode('utf-8')
    manifest = read_json(urljoin(site, 'data/manifest.json'))
    assert manifest['public_snapshot'] is True
    target = 'terminal-bench-science/traffic-flux-inversion'
    checked = []
    for model in manifest['models'][:2]:
        run = next(row for row in manifest['runs'] if row['model_id'] == model['id'] and row['task_name'] == target)
        descriptor = manifest['files'][run['path']]
        archive = b''.join(fetch(urljoin(site, 'data/' + part)) for part in descriptor['parts'])
        rows = [json.loads(line) for line in gzip.decompress(archive).decode().splitlines() if line]
        assert any(row['trial_id'] == run['trial_id'] and (row.get('trajectory') or row.get('reading_trajectory')) for row in rows)
        checked.append(model['name'])
    hub_api = HF_API + ('/revision/' + quote(revision, safe='') if revision else '')
    code, hub = read_json(CODE_API), read_json(hub_api)
    assert code['private'] is False and hub['private'] is False
    files_url = HF_FILES.replace('/resolve/main/', '/resolve/' + hub['sha'] + '/')
    upload = read_json(files_url + 'upload-status.json')
    inventory = read_json(files_url + 'inventory.json')
    rows = {name: list(csv.DictReader(io.StringIO(fetch(files_url + 'analysis/' + name + '.csv').decode())))
            for name in ('trials', 'models', 'tasks')}
    tables = {name: len(records) for name, records in rows.items()}
    assert tables == {'trials': len(manifest['runs']), 'models': len(manifest['models']), 'tasks': len(manifest['tasks'])}
    assert {row['trial_id'] for row in rows['trials']} == {run['trial_id'] for run in manifest['runs']}
    assert dict(Counter(row['download_status'] for row in rows['trials'])) == manifest['counts']
    present = {row['rfilename'] for row in hub['siblings']}
    candidate = min((row for row in inventory if row['path'] in present and 20000 < row['bytes'] < 100000), key=lambda row: row['bytes'])
    archive = fetch(files_url + candidate['path'])
    assert len(archive) == candidate['bytes'] and hashlib.sha256(archive).hexdigest() == candidate['sha256']
    result = {'site': site, 'anonymous_site_ok': True, 'comparison_models_checked': checked,
              'site_counts': manifest['counts'], 'github_public': True, 'hf_public': True,
              'hf_raw_files': sum(path.endswith('.jsonl.gz') for path in present),
              'hf_raw_sample_sha256_ok': True, 'hf_upload': upload, 'hf_revision': hub['sha']}
    result['anonymous_analysis_rows'] = tables
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--site', required=True)
    parser.add_argument('--require-complete', action='store_true')
    parser.add_argument('--revision', help='Verify one immutable Hugging Face commit instead of the latest head')
    args = parser.parse_args()
    result = verify(args.site, args.revision)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    directory = Path(__file__).resolve().parent/'dataset'
    if directory.exists():
        (directory/'publication-check.json').write_text(json.dumps(result, ensure_ascii=False))
    if args.require_complete and result['hf_upload']['state'] != 'complete':
        raise SystemExit(2)


if __name__ == '__main__':
    main()
