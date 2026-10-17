#!/usr/bin/env python3
"""Check the public site, repository, and dataset without any login or access token."""
import argparse
import gzip
import hashlib
import json
from pathlib import Path
from urllib.parse import urljoin
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


def verify(site):
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
    code, hub = read_json(CODE_API), read_json(HF_API)
    assert code['private'] is False and hub['private'] is False
    upload = read_json(HF_FILES + 'upload-status.json')
    inventory = read_json(HF_FILES + 'inventory.json')
    present = {row['rfilename'] for row in hub['siblings']}
    candidate = min((row for row in inventory if row['path'] in present and 20000 < row['bytes'] < 100000), key=lambda row: row['bytes'])
    archive = fetch(HF_FILES + candidate['path'])
    assert len(archive) == candidate['bytes'] and hashlib.sha256(archive).hexdigest() == candidate['sha256']
    result = {'site': site, 'anonymous_site_ok': True, 'comparison_models_checked': checked,
              'site_counts': manifest['counts'], 'github_public': True, 'hf_public': True,
              'hf_raw_files': sum(path.endswith('.jsonl.gz') for path in present),
              'hf_raw_sample_sha256_ok': True, 'hf_upload': upload}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--site', required=True)
    parser.add_argument('--require-complete', action='store_true')
    args = parser.parse_args()
    result = verify(args.site)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    directory = Path(__file__).resolve().parent/'dataset'
    if directory.exists():
        (directory/'publication-check.json').write_text(json.dumps(result, ensure_ascii=False))
    if args.require_complete and result['hf_upload']['state'] != 'complete':
        raise SystemExit(2)


if __name__ == '__main__':
    main()
