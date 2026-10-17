#!/usr/bin/env python3
"""Finish interrupted range downloads after the current writer releases its lock."""
import json
from pathlib import Path
import subprocess
import sys
import time

import crawl


def main():
    root=(Path(__file__).resolve().parent/'dataset').resolve()
    stop=root/'download.stop'
    for attempt in range(1,4):
        while crawl.writer_active(root) or crawl.legacy_writer_active(root):
            if stop.exists():
                return 0
            time.sleep(10)
        if stop.exists():
            return 0
        catalog=json.loads((root/'catalog.json').read_text())
        remaining=[t['id'] for t in catalog['trials']
                   if crawl.cache_status(root/'_runs'/(t['id']+'.json')) not in ('downloaded','unavailable')]
        crawl.atomic(root/'resume-status.json',crawl.dumps({'state':'retrying' if remaining else 'complete',
                     'attempt':attempt,'remaining':len(remaining),'updated_at':crawl.now()}))
        if not remaining:
            print('All accessible source trajectories have been archived.',flush=True)
            return 0
        print(f'Resume pass {attempt}: {len(remaining)} unfinished trajectories; preserving existing byte ranges.',flush=True)
        subprocess.run([sys.executable,'-u',str(Path(__file__).with_name('accelerate.py')),
                        'fetch','--workers','128','--timeout','60','--retries','4'],check=False)
    crawl.atomic(root/'resume-status.json',crawl.dumps({'state':'retry_passes_finished','updated_at':crawl.now()}))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
