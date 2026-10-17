#!/usr/bin/env python3
"""Upload the prepared snapshot, then sync once the active download finishes."""
import argparse
import json
from pathlib import Path
import subprocess
import time

import crawl
import publish


def download_active(root):
    if crawl.writer_active(root) or crawl.legacy_writer_active(root):
        return True
    launcher=root/'resume-launcher.json'
    if not launcher.exists():
        return False
    try:
        pid=json.loads(launcher.read_text())['pid']
        command=subprocess.check_output(['ps','-p',str(pid),'-o','command='],text=True,stderr=subprocess.DEVNULL)
        return 'continue_download.py' in command
    except (KeyError,OSError,subprocess.CalledProcessError):
        return False


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo',default=publish.DEFAULT_REPO)
    parser.add_argument('--wait-for-stage',action='store_true',help='Wait for an existing snapshot packager before acquiring the stage lock')
    args=parser.parse_args()
    here=Path(__file__).resolve().parent
    root,stage=(here/'dataset').resolve(),here/'hub-export'
    if args.wait_for_stage and crawl.writer_active(stage):
        print('Waiting for current snapshot packaging to finish.',flush=True)
        while crawl.writer_active(stage):
            time.sleep(5)
    with crawl.dataset_lock(stage):
        publish.prepare(root,stage,args.repo)
        snapshot=json.loads((stage/'manifest.json').read_text())['created_at']
        print('Uploading current local capture.',flush=True)
        publish.upload(stage,args.repo)
        print('Initial snapshot uploaded; waiting for local download and retry passes.',flush=True)
        while download_active(root):
            time.sleep(20)
        latest=json.loads((root/'manifest.json').read_text()).get('generated_at','')
        if latest > snapshot:
            print('Preparing and uploading the final local capture.',flush=True)
            publish.prepare(root,stage,args.repo)
            publish.upload(stage,args.repo)
    return 0


if __name__=='__main__':
    raise SystemExit(main())
