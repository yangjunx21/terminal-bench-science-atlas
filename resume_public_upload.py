#!/usr/bin/env python3
"""Resume the prepared public archive and verify every committed content hash."""
import argparse
from pathlib import Path
import subprocess
import time


HERE = Path(__file__).resolve().parent


def website_push_active():
    rows = subprocess.check_output(['ps', '-axo', 'command='], text=True)
    return any(line.strip().startswith(('python3 -u /tmp/tb-science-pages-shards.py',
                                       '/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9/Resources/Python.app/Contents/MacOS/Python -u /tmp/tb-science-pages-shards.py'))
               for line in rows.splitlines())


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wait-for-site',action='store_true')
    parser.add_argument('--workers',type=int,default=128)
    args=parser.parse_args()
    stop = HERE/'dataset/upload.stop'
    print('Continuing the public archive upload.', flush=True)
    while args.wait_for_site and website_push_active():
        if stop.exists():
            return 0
        time.sleep(5)
    for attempt in range(1, 4):
        if stop.exists():
            return 0
        print(f'Public archive upload pass {attempt}: parallel multipart transfer.', flush=True)
        result = subprocess.run(['uvx', '--from', 'huggingface_hub', 'python', str(HERE/'upload_http.py'),
                                 '--stage', str(HERE/'hub-export'), '--repo', 'yangjunxiao2021/terminal-bench-science-trajectories',
                                 '--public', '--multipart-workers', str(args.workers)])
        if result.returncode == 0:
            print('Public dataset archive upload completed and verified.', flush=True)
            return 0
        time.sleep(20)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
