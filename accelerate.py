#!/usr/bin/env python3
"""Download remaining public ATIF objects with resumable parallel byte ranges.

Uses Harbor's publicly shipped Supabase publishable key, never a personal token.
Storage row access rules still apply. No server-side access checks are bypassed.
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import threading
import time
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

import crawl

CONSTANTS = "https://raw.githubusercontent.com/harbor-framework/harbor/main/src/harbor/auth/constants.py"


def storage_config():
    with urlopen(CONSTANTS, timeout=30) as response:
        source = response.read().decode()
    values = {}
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    values[target.id] = node.value.value
    base = values["DEFAULT_SUPABASE_URL"]
    if not re.fullmatch(r"https://[a-z0-9]+\.supabase\.co", base):
        raise ValueError("Unrecognized upstream public storage host")
    return {"base_url": base + "/storage/v1/object/results/", "public_key": values["DEFAULT_SUPABASE_PUBLISHABLE_KEY"], "source": CONSTANTS}


def headers(config):
    return {"apikey": config["public_key"], "Accept-Encoding": "identity", "User-Agent": "HarborScienceArchive/1.0"}


def plan(root, workers=32, errors_only=False):
    catalog = json.loads((root / "catalog.json").read_text())
    config = storage_config()
    trials = [t for t in catalog["trials"] if crawl.cache_status(root / "_runs" / (t["id"] + ".json")) != "downloaded"]
    previous = {}
    index = root / "_parts/remote-index.json"
    if errors_only and index.exists():
        previous = {r["id"]: r for r in json.loads(index.read_text())["files"]}
        trials = [t for t in trials if t["id"] not in previous or previous[t["id"]].get("error")]
    def head(trial):
        path = f"trials/{trial['id']}/trajectory.json"
        url = config["base_url"] + path
        try:
            try:
                response = urlopen(Request(url, method="HEAD", headers=headers(config)), timeout=30)
            except HTTPError as exc:
                if exc.code not in (400,404):
                    raise
                # The canonical storage path is occasionally customized. Read
                # the public trial metadata before declaring the object absent.
                page = f"{catalog['origin']}/jobs/{trial['job_id']}/trials/{trial['id']}"
                with urlopen(page, timeout=30) as stream:
                    detail = crawl.find_props(stream.read().decode(), "trial")["trial"]
                path = detail.get("trajectory_path")
                if not path:
                    return {"id": trial["id"], "unavailable": True, "url": page, "error": "No public trajectory_path"}
                url = config["base_url"] + quote(path, safe="/")
                response = urlopen(Request(url, method="HEAD", headers=headers(config)), timeout=30)
            with response:
                size = int(response.headers["Content-Length"])
                return {"id": trial["id"], "url": url, "size": size, "etag": response.headers.get("ETag")}
        except Exception as exc:
            return {"id": trial["id"], "url": url, "error": f"{type(exc).__name__}: {exc}"}
    rows = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(head, trial) for trial in trials]
        for future in as_completed(futures):
            rows.append(future.result())
            if len(rows) % 100 == 0:
                print(f"Storage inventory {len(rows)}/{len(trials)}", flush=True)
    previous.update({r["id"]: r for r in rows})
    rows = list(previous.values())
    config.update(files=rows, created_at=crawl.now())
    crawl.atomic(root / "_parts/remote-index.json", crawl.dumps(config))
    print(f"Public storage inventory: {len(rows)} objects, {sum(r.get('size',0) for r in rows)/1e9:.2f} GB uncompressed, {sum('error' in r for r in rows)} missing/error paths", flush=True)
    return config


class Meter:
    def __init__(self, interval=0.08):
        self.lock = threading.Lock()
        self.bytes = self.active = self.requests = 0
        self.next_request = 0.0
        self.interval = interval

    def enter(self):
        with self.lock:
            delay = max(0, self.next_request - time.monotonic())
            self.next_request = max(self.next_request, time.monotonic()) + self.interval
        if delay:
            time.sleep(delay)
        with self.lock:
            self.active += 1
            self.requests += 1

    def leave(self):
        with self.lock:
            self.active -= 1

    def received(self, amount):
        with self.lock:
            self.bytes += amount

    def stats(self):
        with self.lock:
            return {"network_bytes": self.bytes, "active_requests": self.active, "requests": self.requests}


def part_path(root, item, start, end):
    return root / "_parts" / item["id"] / f"{start:014d}-{end:014d}.part"


def redacted_reading_copy(source):
    """Project unquoted upstream [REDACTED] values to null for reading only."""
    position = copied = 0
    pieces = []
    while position < len(source):
        quote_at = source.find('"', position)
        marker_at = source.find('[REDACTED]', position)
        if marker_at >= 0 and (quote_at < 0 or marker_at < quote_at):
            pieces.extend([source[copied:marker_at], 'null'])
            position = copied = marker_at + len('[REDACTED]')
        elif quote_at >= 0:
            _, position = json.decoder.scanstring(source, quote_at + 1, True)
        else:
            break
    if not pieces:
        return None
    pieces.append(source[copied:])
    value = json.loads(''.join(pieces))
    return value if isinstance(value,dict) and isinstance(value.get('steps'),list) else None


def fetch_part(root, item, start, end, config, meter, timeout=30, retries=2):
    path = part_path(root, item, start, end)
    path.parent.mkdir(parents=True, exist_ok=True)
    expected = end - start + 1
    for attempt in range(retries + 1):
        have = path.stat().st_size if path.exists() else 0
        if have == expected:
            return
        if have > expected:
            raise ValueError(f"Unexpected partial length: {path}")
        request_headers = {**headers(config), "Range": f"bytes={start+have}-{end}"}
        if item.get("etag"):
            request_headers["If-Match"] = item["etag"]
        meter.enter()
        try:
            with urlopen(Request(item["url"], headers=request_headers), timeout=timeout) as response:
                expected_range = f"bytes {start+have}-{end}/{item['size']}"
                if response.status != 206 or response.headers.get("Content-Range") != expected_range:
                    raise ValueError("Server did not honor the exact byte range; partial file was not appended")
                if item.get("etag") and response.headers.get("ETag") != item["etag"]:
                    raise ValueError("Source ETag changed; partial file was not appended")
                with path.open("ab") as output:
                    while chunk := response.read(64 * 1024):
                        if output.tell() + len(chunk) > expected:
                            raise ValueError("Response exceeds requested byte range")
                        output.write(chunk)
                        output.flush()
                        meter.received(len(chunk))
            if path.stat().st_size != expected:
                raise ValueError("Truncated range response")
            return
        except Exception:
            if attempt == retries:
                raise
            time.sleep(min(4, 2 ** attempt))
        finally:
            meter.leave()


def finalize(root, item, chunks, parse_slots=None):
    directory = root / "_parts" / item["id"]
    raw = directory / "original.json"
    digest = hashlib.sha256()
    with raw.open("wb") as output:
        for start, end in chunks:
            path = part_path(root, item, start, end)
            if path.stat().st_size != end - start + 1:
                raise ValueError("Incomplete source range")
            with path.open("rb") as source:
                while block := source.read(1024 * 1024):
                    output.write(block)
                    digest.update(block)
    if raw.stat().st_size != item["size"]:
        raise ValueError("Assembled object size differs from source Content-Length")
    # Bound JSON parsing separately from network transfers. Large image-heavy
    # objects can be hundreds of MB; network ranges never accumulate in RAM.
    with parse_slots or nullcontext():
        source = raw.read_bytes().decode("utf-8")
        parse_error = None
        try:
            trajectory = json.loads(source, parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Nonfinite JSON constant: " + value)))
            if not isinstance(trajectory, dict) or not isinstance(trajectory.get("steps"), list):
                raise ValueError("Source object is not an ATIF trajectory with steps[]")
            reading = None
        except (ValueError,TypeError) as exc:
            parse_error = f"{type(exc).__name__}: {exc}"
            trajectory = None
            try:
                reading = redacted_reading_copy(source)
            except (ValueError,TypeError):
                reading = None
        del trajectory
    target = root / "_runs" / (item["id"] + ".json")
    target.parent.mkdir(exist_ok=True)
    temp = target.with_suffix(".storage.tmp")
    metadata = {"error": None, "fetched_at": crawl.now(), "source_url": item["url"], "source_bytes": item["size"],
                "source_sha256": digest.hexdigest(), "download_transport": "public_storage_ranges"}
    if parse_error:
        crawl.atomic(temp,crawl.dumps({"status":"downloaded","trajectory":None,"raw_trajectory":source,
                     "reading_trajectory":reading,"source_format":"nonstandard_json","parse_error":parse_error,**metadata}))
    else:
        with temp.open("wb") as output, raw.open("rb") as original:
            output.write(b'{"status":"downloaded","trajectory":')
            shutil.copyfileobj(original, output)
            output.write(("," + crawl.dumps(metadata)[1:]).encode())
    temp.replace(target)
    # A complete cache now owns every original byte; range fragments are only
    # retained for incomplete objects so reruns can resume at the byte offset.
    for start, end in chunks:
        part_path(root, item, start, end).unlink()
    raw.unlink()


def fetch(root, site, workers=128, chunk_bytes=4*1024*1024, timeout=30, retries=2, refresh_plan=False):
    index = root / "_parts/remote-index.json"
    config = plan(root, min(workers, 48)) if refresh_plan or not index.exists() else json.loads(index.read_text())
    catalog = json.loads((root / "catalog.json").read_text())
    known = {r["id"]: r for r in config["files"]}
    trials = [t for t in catalog["trials"] if crawl.cache_status(root / "_runs" / (t["id"] + ".json")) != "downloaded"]
    counts = Counter(cached=len(catalog["trials"]) - len(trials))
    counts_lock = threading.Lock()
    def bump(name):
        with counts_lock:
            counts[name] += 1
    meter = Meter()
    clock_start, started = time.monotonic(), crawl.now()
    stop = threading.Event()
    phase = ["running"]
    chunks, outstanding, items = {}, {}, {}
    resumed_bytes = 0
    parse_slots = threading.BoundedSemaphore(2)
    def progress(state="running"):
        elapsed = max(.001, time.monotonic() - clock_start)
        stats = meter.stats()
        with counts_lock:
            snapshot = dict(counts)
        completed = sum(snapshot.values())
        status = {"pid": os.getpid(), "state": state, "transport": "public_storage_ranges", "started_at": started, "updated_at": crawl.now(),
                  "counts": snapshot, "selected_trials": len(catalog["trials"]), "completed": completed,
                  "remaining": len(catalog["trials"]) - completed, "elapsed_seconds": round(elapsed,1),
                  "downloaded_per_minute": round(snapshot.get("downloaded",0)*60/elapsed,2), "network_mbps": round(stats["network_bytes"]/1e6/elapsed,3),
                  "transfer_total_bytes": sum(i["size"] for i in items.values()), "transfer_received_bytes": resumed_bytes + stats["network_bytes"], **stats}
        crawl.atomic(root / "crawl-status.json", crawl.dumps(status))
        crawl.atomic(site / "data/crawl-status.json", crawl.dumps(status))
    def monitor():
        ticks = 0
        while not stop.wait(2):
            progress(phase[0])
            ticks += 1
            if ticks % 15 == 0:
                elapsed = time.monotonic() - clock_start
                print(f"Storage: {dict(counts)} · {meter.stats()['network_bytes']/1e6/elapsed:.2f} MB/s", flush=True)
    for trial in trials:
        item = known.get(trial["id"])
        if not item or not item.get("size"):
            counts["unavailable" if item and item.get("unavailable") else "error"] += 1
            crawl.atomic(root / "_runs" / (trial["id"] + ".json"), crawl.dumps({"status":"unavailable" if item and item.get("unavailable") else "error", "trajectory":None,
                         "error":item.get("error", "Storage metadata unavailable") if item else "Storage metadata missing; rerun with --refresh-plan",
                         "source_url":item.get("url") if item else None,"fetched_at":crawl.now()}))
            continue
        items[item["id"]] = item
        directory = root / "_parts" / item["id"]
        signature = {"url": item["url"], "size": item["size"], "etag": item.get("etag"), "chunk_bytes": chunk_bytes}
        saved = directory / "object.json"
        if saved.exists() and json.loads(saved.read_text()) != signature:
            raise ValueError(f"Source object or chunk size changed: preserve/remove {directory} before retrying")
        crawl.atomic(saved, crawl.dumps(signature))
        chunks[item["id"]] = [(start, min(start+chunk_bytes, item["size"])-1) for start in range(0,item["size"],chunk_bytes)]
        resumed_bytes += sum(part_path(root,item,start,end).stat().st_size for start,end in chunks[item["id"]] if part_path(root,item,start,end).exists())
        outstanding[item["id"]] = len(chunks[item["id"]])
    progress()
    heartbeat = threading.Thread(target=monitor, daemon=True)
    heartbeat.start()
    def refresh_site():
        while not stop.wait(120):
            if phase[0] != "running":
                return
            try:
                crawl.snapshot(root,site,web_text_only=True)
            except Exception as exc:
                print(f"Snapshot update deferred: {exc}",flush=True)
    snapshots = threading.Thread(target=refresh_site,daemon=True)
    snapshots.start()
    print(f"Fetching {len(items)} remaining objects in resumable byte ranges, {sum(i['size'] for i in items.values())/1e9:.2f} GB before compression, {workers} connections", flush=True)
    failed, validation = set(), []
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool, ThreadPoolExecutor(max_workers=2) as parsers:
            jobs = {}
            # Round-robin chunks: short trajectories finish while huge objects
            # share the available connections, instead of monopolizing them.
            for offset in range(max((len(v) for v in chunks.values()),default=0)):
                for id, parts in chunks.items():
                    if offset < len(parts):
                        start,end = parts[offset]
                        jobs[pool.submit(fetch_part,root,items[id],start,end,config,meter,timeout,retries)] = id
            def validate(id):
                try:
                    finalize(root, items[id], chunks[id], parse_slots)
                    bump("downloaded")
                except Exception as exc:
                    bump("error")
                    crawl.atomic(root / "_runs" / (id+".json"), crawl.dumps({"status":"error","trajectory":None,"error":f"Storage validation: {type(exc).__name__}: {exc}","source_url":items[id]["url"],"fetched_at":crawl.now()}))
            for future in as_completed(jobs):
                id = jobs[future]
                try:
                    future.result()
                except Exception as exc:
                    if id not in failed:
                        failed.add(id)
                        bump("error")
                        crawl.atomic(root / "_runs" / (id+".json"), crawl.dumps({"status":"error","trajectory":None,"error":f"Storage range: {type(exc).__name__}: {exc}","source_url":items[id]["url"],"fetched_at":crawl.now()}))
                outstanding[id] -= 1
                if outstanding[id] == 0 and id not in failed:
                    validation.append(parsers.submit(validate,id))
            for future in validation:
                future.result()
    except BaseException:
        stop.set()
        heartbeat.join()
        progress("interrupted")
        raise
    phase[0] = "building"
    progress("building")
    try:
        crawl.build(root,site,web_text_only=True)
    except BaseException:
        progress("build_failed")
        raise
    finally:
        stop.set()
        heartbeat.join()
    progress("complete")
    return 1 if counts["error"] else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command",choices=["plan","fetch"])
    parser.add_argument("--data",type=Path,default=crawl.HERE/"dataset")
    parser.add_argument("--site",type=Path,default=crawl.HERE/"dist")
    parser.add_argument("--workers",type=int,default=128)
    parser.add_argument("--chunk-mb",type=int,default=4)
    parser.add_argument("--timeout",type=float,default=30)
    parser.add_argument("--retries",type=int,default=2)
    parser.add_argument("--refresh-plan",action="store_true")
    parser.add_argument("--errors-only",action="store_true",help="Recheck only missing/error paths in an existing plan")
    args=parser.parse_args()
    if not 1<=args.workers<=128 or args.chunk_mb<1 or args.timeout<=0 or args.retries<0:
        parser.error("workers 1..128, positive chunk/timeout, nonnegative retries required")
    root,site=args.data.resolve(),args.site.resolve()
    if args.command=="plan":
        plan(root,min(48,args.workers),args.errors_only)
        return 0
    with crawl.dataset_lock(root):
        return fetch(root,site,args.workers,args.chunk_mb*1024*1024,args.timeout,args.retries,args.refresh_plan)


if __name__=="__main__":
    raise SystemExit(main())
