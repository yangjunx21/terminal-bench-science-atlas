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
    args=parser.parse_args()
    here=Path(__file__).resolve().parent
    root,stage=(here/'dataset').resolve(),here/'hub-export'
    with crawl.dataset_lock(stage):
        if not (stage/'manifest.json').exists():
            publish.prepare(root,stage,args.repo)
        print('Uploading prepared partial capture.',flush=True)
        publish.upload(stage,args.repo)
        print('Initial snapshot uploaded; waiting for local download and retry passes.',flush=True)
        while download_active(root):
            time.sleep(20)
        print('Preparing and uploading the final local capture.',flush=True)
        publish.prepare(root,stage,args.repo)
        publish.upload(stage,args.repo)
    return 0


if __name__=='__main__':
    raise SystemExit(main())
