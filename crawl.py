#!/usr/bin/env python3
"""Archive public Harbor trajectories and build a dependency-free comparison site.

Python 3.9+, standard library only. See README.md for commands and schema.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from contextlib import nullcontext, contextmanager
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import gzip
import hashlib
import io
from http.client import IncompleteRead
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve().parent
DEFAULT_URL = "https://hub.harborframework.com/datasets/terminal-bench-science/terminal-bench-science/latest?tab=leaderboard"
STATIC_ASSET_BYTES = 24 * 1024 * 1024


@contextmanager
def dataset_lock(root, wait=False):
    """Prevent concurrent crawl / build writers; the OS releases stale locks."""
    import fcntl
    root.mkdir(parents=True, exist_ok=True)
    with (root / "_crawl.lock").open("a+") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            raise RuntimeError(f"Another crawl/build is writing {root}. Stop it before starting another writer.") from None
        try:
            if legacy_writer_active(root):
                raise RuntimeError(f"An older crawler is still writing {root}. Stop it before starting another writer.")
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def writer_active(root):
    import fcntl
    lock = root / "_crawl.lock"
    if not lock.exists():
        return False
    with lock.open("a+") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(stream, fcntl.LOCK_UN)
    return False


def legacy_writer_active(root):
    """Recognize a still-running crawler started before file locks existed."""
    status = root / "crawl-status.json"
    if not status.exists():
        return False
    old = json.loads(status.read_text())
    if old.get("state") != "running" or old.get("pid") == os.getpid():
        return False
    try:
        import subprocess
        command = subprocess.check_output(["ps", "-p", str(old["pid"]), "-o", "command="], text=True, stderr=subprocess.DEVNULL)
        return "crawl.py" in command and (" crawl" in command or " build" in command)
    except (KeyError, OSError, subprocess.CalledProcessError):
        return False


def inventory(root):
    counts = defaultdict(int)
    size = 0
    for path in (root / "_runs").glob("*.json"):
        with path.open(encoding="utf-8") as stream:
            match = re.search(r'"status"\s*:\s*"([^"]+)"', stream.read(512))
        counts[match.group(1) if match else "unknown"] += 1
        size += path.stat().st_size
    return {"counts": dict(counts), "cache_bytes": size}


def cache_status(path):
    """Read only the cache header; resuming must not parse gigabytes of ATIF."""
    if not path.exists():
        return "pending"
    try:
        with path.open(encoding="utf-8") as stream:
            match = re.search(r'"status"\s*:\s*"([^\"]+)"', stream.read(512))
        return match.group(1) if match else "error"
    except (OSError, UnicodeError):
        return "error"


def now():
    return datetime.now(timezone.utc).isoformat()


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=path.name + ".", suffix=".tmp", delete=False) as stream:
        temp = Path(stream.name)
        stream.write(data)
    try:
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def slug(value):
    clean = re.sub(r"[^a-z0-9._-]+", "-", value.lower()).strip(".-")[:100] or "item"
    return clean + "-" + hashlib.sha256(value.encode()).hexdigest()[:8]


def rsc_objects(text):
    """Read Next.js Flight JSON records from SSR HTML or server-action responses.

    Length-prefixed text records and module/reference records are deliberately
    ignored. We only inspect concrete JSON objects; never execute page scripts.
    """
    if "self.__next_f.push" in text:
        text = "".join(json.loads(m.group(1)) for m in re.finditer(
            r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)', text))
    for line in text.splitlines():
        if ":" not in line:
            continue
        try:
            yield json.loads(line.split(":", 1)[1])
        except (ValueError, TypeError):
            continue


def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def find_props(text, key):
    for obj in rsc_objects(text):
        for child in walk(obj):
            if key in child:
                return child
    raise ValueError(f"Harbor response has no {key!r}. Check URL, visibility, or an upstream schema change.")


class Client:
    def __init__(self, origin, cache, interval=0.08, retries=4, timeout=90, refresh=False):
        self.origin, self.cache = origin.rstrip("/"), Path(cache)
        self.interval, self.retries, self.timeout = interval, retries, timeout
        self.lock, self.next_request = threading.Lock(), 0.0
        self.refresh = refresh
        self.large_requests = threading.BoundedSemaphore(2)
        self.large_bodies = threading.BoundedSemaphore(2)
        self.metrics = {"network_bytes": 0, "requests": 0, "active_requests": 0, "request_errors": 0}
        self.metrics_lock = threading.Lock()

    def stats(self):
        with self.metrics_lock:
            return dict(self.metrics)

    def request(self, path, *, body=None, headers=None, cache=True, retries=None):
        url = path if path.startswith("https://") else self.origin + path
        key = hashlib.sha256((url + (body or "")).encode()).hexdigest()
        target = self.cache / (key + ".txt")
        if cache and target.exists() and not self.refresh:
            return target.read_text(encoding="utf-8")
        retries = self.retries if retries is None else retries
        for attempt in range(retries + 1):
            with self.lock:
                delay = max(0, self.next_request - time.monotonic())
                self.next_request = max(self.next_request, time.monotonic()) + self.interval
            if delay:
                time.sleep(delay)
            with self.metrics_lock:
                self.metrics["requests"] += 1
                self.metrics["active_requests"] += 1
            large_body = False
            try:
                req = Request(url, data=body.encode() if body is not None else None, headers={
                    "User-Agent": "HarborScienceArchive/1.0 (public research dataset downloader)",
                    "Accept-Encoding": "gzip",
                    **(headers or {}),
                })
                with urlopen(req, timeout=self.timeout) as response:
                    buffer = io.BytesIO()
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        buffer.write(chunk)
                        with self.metrics_lock:
                            self.metrics["network_bytes"] += len(chunk)
                        if buffer.tell() > 8 * 1024 * 1024 and not large_body:
                            self.large_bodies.acquire()
                            large_body = True
                    raw = buffer.getvalue()
                    if response.headers.get("Content-Encoding") == "gzip":
                        raw = gzip.decompress(raw)
                    text = raw.decode("utf-8")
                if cache:
                    atomic(target, text)
                return text
            except (HTTPError, URLError, TimeoutError, socket.timeout, ConnectionError, IncompleteRead) as exc:
                with self.metrics_lock:
                    self.metrics["request_errors"] += 1
                code = getattr(exc, "code", None)
                if code in (401, 403, 404) or attempt == retries:
                    raise
                retry_after = getattr(exc, "headers", {}).get("Retry-After", "")
                wait = float(retry_after) if retry_after.isdigit() else min(30, 2 ** attempt)
                time.sleep(wait)
            finally:
                if large_body:
                    self.large_bodies.release()
                with self.metrics_lock:
                    self.metrics["active_requests"] -= 1
        raise RuntimeError("request retry exhausted")


def discover(client, dataset_path, leaderboard_name=None):
    html = client.request(dataset_path + "?tab=leaderboard")
    props = find_props(html, "leaderboards")
    summaries = props["leaderboards"]
    if leaderboard_name:
        summaries = [b for b in summaries if b["name"] == leaderboard_name]
    if not summaries:
        raise ValueError("No matching public leaderboards")
    # Harbor currently loads rows with a read-only Next.js server action. Discover
    # its build-specific ID from the shipped bundle instead of hard-coding it.
    scripts = list(dict.fromkeys(re.findall(r'src="([^\"]+\.js)"', html)))
    action = None
    for script in scripts:
        js = client.request(script)
        match = re.search(r'createServerReference\)\("([a-f0-9]+)"[^;]{0,250}?"fetchLeaderboardWithRows"', js)
        if match:
            action = match.group(1)
            break
    if not action:
        raise ValueError("Cannot discover fetchLeaderboardWithRows action; Harbor's frontend may have changed")
    boards = []
    for summary in summaries:
        response = client.request(dataset_path + "?tab=leaderboard", body=dumps([summary["id"]]), headers={
            "Next-Action": action, "Content-Type": "text/plain;charset=UTF-8", "Accept": "text/x-component",
        })
        board = find_props(response, "rows")
        boards.append(board)
    return boards


def discover_row(client, dataset_path, board, row):
    path = f"{dataset_path}/leaderboards/{quote(board['name'])}/rows/{row['id']}"
    props = find_props(client.request(path + "?tab=results&page=1"), "initialTrials")
    pages = props["initialTrials"]
    trials = list(pages["items"])
    for page in range(2, pages["total_pages"] + 1):
        more = find_props(client.request(path + "?" + urlencode({"tab": "results", "page": page})), "initialTrials")
        trials.extend(more["initialTrials"]["items"])
    unique = {t["id"]: t for t in trials}
    if len(unique) != pages["total"]:
        raise ValueError(f"Pagination incomplete for {row['id']}: {len(unique)}/{pages['total']}")
    meta = row["metadata"]
    name = meta.get("model_display", {}).get("label", row["id"])
    model = {
        "id": row["id"], "name": name,
        "directory": slug(name), "agent": meta.get("agent_display", {}).get("label", ""),
        "effort": meta.get("reasoning_effort", ""), "metadata": meta,
        "leaderboard_id": board["id"], "leaderboard": board["name"],
        "official_metrics": row["metrics"], "rank": row.get("rank"),
        "source_url": client.origin + path, "expected_trials": pages["total"],
        "official_trials": row["metrics"].get("tasks"),
        "coverage": "public_trials" if unique else "metadata_only",
    }
    return model, list(unique.values())


def duration(trial):
    try:
        start = datetime.fromisoformat(trial["started_at"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(trial["finished_at"].replace("Z", "+00:00"))
        return max(0, (end - start).total_seconds())
    except (ValueError, TypeError, KeyError):
        return None


def download(client, root, trial, refresh=False, defer_original=False, original_only=False):
    target = root / "_runs" / (trial["id"] + ".json")
    if not refresh and cache_status(target) == "downloaded":
        return "cached"
    path = f"/api/trials/{trial['id']}/trajectory?" + urlencode({"jobId": trial["job_id"]})
    try:
        fallback = original_only
        if not original_only:
            try:
                trajectory = json.loads(client.request(path, cache=False, retries=0))
                fallback = trajectory is None
            except HTTPError as exc:
                if exc.code not in (404, 500, 502, 503, 504):
                    raise
                fallback = True
            except (TimeoutError, socket.timeout, URLError, ConnectionError, IncompleteRead, json.JSONDecodeError, EOFError, gzip.BadGzipFile):
                fallback = True
        if fallback:
            if defer_original:
                return "deferred"
            # Large ATIFs can exceed the normalized trajectory route's response
            # limit. The same public file browser exposes the original ATIF.
            path = f"/api/trials/{trial['id']}/files/agent/trajectory.json?" + urlencode({"jobId": trial["job_id"]})
            with getattr(client, "large_requests", nullcontext()):
                try:
                    trajectory = json.loads(client.request(path, cache=False))
                except HTTPError as exc:
                    if exc.code != 404:
                        raise
                    trajectory = None
        if trajectory is None:
            result = {"status": "unavailable", "error": "Harbor returned no ATIF trajectory", "trajectory": None}
        elif not isinstance(trajectory, dict) or not isinstance(trajectory.get("steps"), list):
            raise ValueError("Unexpected trajectory schema: expected ATIF object with steps[]")
        else:
            result = {"status": "downloaded", "trajectory": trajectory, "error": None}
    except Exception as exc:
        result = {"status": "error", "trajectory": None, "error": f"{type(exc).__name__}: {exc}"}
    result.update({"fetched_at": now(), "source_url": client.origin + path})
    atomic(target, dumps(result))
    return result["status"]


def text_projection(value):
    """Omit embedded binary payloads from the optional web-only reading copy."""
    if isinstance(value, list):
        return [text_projection(item) for item in value]
    if isinstance(value, dict):
        if value.get("type") in ("image", "input_image", "image_url"):
            return {"type": "image", "source": {"path": "Embedded image omitted in web text view; see original Harbor trial"}, "web_projection": "text_only"}
        return {key: ("[Embedded binary omitted in web text view]" if key in ("base64", "b64_json") and isinstance(item, str) else text_projection(item)) for key, item in value.items()}
    if isinstance(value, str) and ("base64" in value or "data:image/" in value or "b64_json" in value):
        # Some agents serialize tool observations as JSON / Python repr strings,
        # including images. Redact binary spans inside those strings as well.
        value = re.sub(r"data:image/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/_=\r\n-]+", "[Embedded image omitted in web text view]", value)
        value = re.sub(r"""(\\?["'](?:data|base64|b64_json)\\?["']\s*:\s*\\?["'])([A-Za-z0-9+/_=-]{1024,})(\\?["'])""", r"\1[Embedded binary omitted in web text view]\3", value)
        return value
    return value


def build(root, site, web_text_only=False):
    with dataset_lock(site, wait=True):
        return _build(root, site, web_text_only)


def snapshot(root, site, web_text_only=True):
    """Export a stable reading snapshot while the crawler keeps downloading."""
    frozen = root / "_web-snapshot"
    with dataset_lock(frozen):
        shutil.copy2(root / "catalog.json", frozen / "catalog.json")
        cache = frozen / "_runs"
        cache.mkdir(exist_ok=True)
        for source in (root / "_runs").glob("*.json"):
            target = cache / source.name
            if target.exists() and target.stat().st_ino == source.stat().st_ino:
                continue
            temporary = target.with_suffix(".link.tmp")
            temporary.unlink(missing_ok=True)
            try:
                os.link(source, temporary)
            except OSError:
                shutil.copy2(source, temporary)
            temporary.replace(target)
        return build(frozen, site, web_text_only)


def _build(root, site, web_text_only=False):
    catalog = json.loads((root / "catalog.json").read_text())
    state_path = root / "_build-state.json"
    previous = json.loads(state_path.read_text()) if state_path.exists() else {}
    state = {}
    groups = defaultdict(list)
    for item in catalog["trials"]:
        groups[(item["model_id"], item["task_name"])].append(item)
    models = {m["id"]: dict(m) for m in catalog["models"]}
    for model in models.values():
        model.setdefault("official_trials", model["official_metrics"].get("tasks"))
        model.setdefault("coverage", "public_trials" if model["expected_trials"] else "metadata_only")
        atomic(root / "models" / model["directory"] / model["id"] / "_metadata.json", dumps(model))
    tasks, runs, counts, files = {}, [], defaultdict(int), {}
    for (model_id, task), items in sorted(groups.items()):
        model = models[model_id]
        # Model directory, separate config directories prevent accidental merging
        # of agents / reasoning settings / submissions of the same model.
        relative = f"models/{model['directory']}/{model_id}/{slug(task)}.jsonl"
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        fingerprint = hashlib.sha256(dumps([5, web_text_only, model, items, [
            (str(t["id"]), (root / "_runs" / (t["id"] + ".json")).stat().st_mtime_ns,
             (root / "_runs" / (t["id"] + ".json")).stat().st_size)
            if (root / "_runs" / (t["id"] + ".json")).exists() else (t["id"], None)
            for t in items
        ]]).encode()).hexdigest()
        old = previous.get(relative)
        if old and old["fingerprint"] == fingerprint and target.exists() and all((site / "data" / part).exists() for part in old["file"]["parts"]):
            runs.extend(old["runs"])
            for run in old["runs"]:
                counts[run["download_status"]] += 1
            files[relative] = old["file"]
            tasks.setdefault(task, {"id": task, "name": task.split("/")[-1]})
            state[relative] = old
            continue
        group_start = len(runs)
        temp = target.with_suffix(".jsonl.tmp")
        with temp.open("w", encoding="utf-8") as out:
            for trial in sorted(items, key=lambda t: (t.get("job_name") or "", t["id"])):
                cached = root / "_runs" / (trial["id"] + ".json")
                data = json.loads(cached.read_text()) if cached.exists() else {"status": "pending", "trajectory": None}
                trajectory = data.get("trajectory")
                reading = trajectory or data.get("reading_trajectory")
                steps = reading.get("steps", []) if reading else []
                row = {
                    "schema_version": "harbor-science-v1", "model_id": model_id,
                    "model": model["name"], "agent": model["agent"], "reasoning_effort": model["effort"],
                    "task_name": task, "trial_id": trial["id"], "job_id": trial["job_id"],
                    "source_url": f"{catalog['origin']}/jobs/{trial['job_id']}/trials/{trial['id']}",
                    "download_status": data["status"], "download_error": data.get("error"),
                    "trajectory_url": data.get("source_url"),
                    "fetched_at": data.get("fetched_at"), "trial": trial, "trajectory": trajectory,
                }
                for key in ("raw_trajectory","reading_trajectory","source_format","parse_error","source_sha256","source_bytes","download_transport"):
                    if key in data:
                        row[key] = data[key]
                out.write(dumps(row) + "\n")
                summary = {k: row[k] for k in ("model_id", "task_name", "trial_id", "job_id", "source_url", "download_status", "download_error")}
                summary.update({
                    "path": relative, "reward": trial.get("reward"), "status": trial.get("status"), "source_format":data.get("source_format","json"),
                    "scored": trial.get("is_scored", True), "error_type": trial.get("error_type"),
                    "input_tokens": trial.get("input_tokens"), "output_tokens": trial.get("output_tokens"),
                    "cache_tokens": trial.get("cache_tokens"), "cost_usd": trial.get("cost_usd"),
                    "duration_seconds": duration(trial), "job_name": trial.get("job_title") or trial.get("job_name"),
                    "steps": len(steps), "tool_calls": sum(len(s.get("tool_calls") or []) for s in steps),
                    "trajectory_sha256": hashlib.sha256(dumps(trajectory).encode()).hexdigest() if trajectory else None,
                })
                runs.append(summary)
                counts[data["status"]] += 1
                tasks.setdefault(task, {"id": task, "name": task.split("/")[-1]})
        temp.replace(target)
        # Deterministic compression keeps individual static assets small. The
        # original uncompressed, lossless JSONL remains in the dataset directory.
        dest = site / "data" / (relative + ".gz")
        dest.parent.mkdir(parents=True, exist_ok=True)
        web_bytes = 0
        with target.open("rb") as src, dest.with_name(dest.name + ".tmp").open("wb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0, compresslevel=6) as gz:
                if web_text_only:
                    for line in src:
                        row = text_projection(json.loads(line))
                        if row.get("raw_trajectory"):
                            row["raw_trajectory"] = "Original nonstandard JSON text is preserved in the local full JSONL; this web copy uses reading_trajectory."
                        row["web_projection"] = "text_only"
                        encoded = (dumps(row) + "\n").encode("utf-8")
                        web_bytes += len(encoded)
                        gz.write(encoded)
                else:
                    shutil.copyfileobj(src, gz)
                    web_bytes = target.stat().st_size
        dest.with_name(dest.name + ".tmp").replace(dest)
        digest = hashlib.sha256()
        with dest.open("rb") as compressed:
            for block in iter(lambda: compressed.read(1024 * 1024), b""):
                digest.update(block)
        asset = relative + "." + digest.hexdigest()[:16] + ".gz"
        versioned = site / "data" / asset
        dest.replace(versioned)
        dest = versioned
        descriptor = {"raw_bytes": web_bytes, "original_bytes": target.stat().st_size, "compressed_bytes": dest.stat().st_size, "parts": [asset]}
        # Static hosting commonly limits individual assets to 25 MiB. Binary
        # chunks reassemble to the exact gzip stream; local JSONL stays whole.
        chunk_bytes = STATIC_ASSET_BYTES
        if dest.stat().st_size > chunk_bytes:
            descriptor["parts"] = []
            with dest.open("rb") as source:
                index = 0
                while chunk := source.read(chunk_bytes):
                    part = dest.with_name(dest.name + f".part-{index:04d}")
                    part.write_bytes(chunk)
                    descriptor["parts"].append(asset + f".part-{index:04d}")
                    index += 1
            dest.unlink()
        files[relative] = descriptor
        state[relative] = {"fingerprint": fingerprint, "file": descriptor, "runs": runs[group_start:]}
        for stale in dest.parent.glob(dest.name + ".part-*"):
            if str(stale.relative_to(site / "data")) not in descriptor["parts"]:
                stale.unlink()
    official_trials = sum(m.get("official_trials") or m["official_metrics"].get("tasks") or m["expected_trials"] for m in models.values())
    unlisted = max(0, official_trials - len(catalog["trials"]))
    manifest = {
        "schema_version": "harbor-science-v1", "generated_at": now(), "source_url": catalog["source_url"],
        "dataset": catalog["dataset"], "models": list(models.values()), "tasks": sorted(tasks.values(), key=lambda t: t["id"]),
        "runs": runs, "counts": dict(counts), "expected_trials": len(catalog["trials"]),
        "official_trials": official_trials, "unlisted_trials": unlisted,
        "expected_trials_per_task": catalog.get("expected_trials_per_task", 3),
        "files": files,
        "web_projection": "text_only" if web_text_only else "full",
        "discovery_complete": catalog.get("discovery_complete", False), "discovery_errors": catalog.get("discovery_errors", []),
    }
    atomic(root / "manifest.json", dumps(manifest))
    atomic(site / "data" / "manifest.json", dumps(manifest))
    atomic(state_path, dumps(state))
    for path in (HERE / "web").iterdir():
        if path.is_file():
            shutil.copy2(path, site / path.name)
    # A static-site CSV inventory is convenient for pandas / spreadsheets.
    import csv
    with (site / "data" / "runs.csv.tmp").open("w", newline="", encoding="utf-8") as f:
        keys = [k for k in runs[0] if k != "download_error"] if runs else ["trial_id"]
        writer = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(runs)
    (site / "data" / "runs.csv.tmp").replace(site / "data" / "runs.csv")
    print(f"Built {site}: {len(models)} configurations, {len(tasks)} tasks, {len(runs)} runs, {dict(counts)}", flush=True)
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["crawl", "discover", "build", "snapshot", "serve", "status"], nargs="?", default="crawl")
    parser.add_argument("--url", default=DEFAULT_URL, help="Harbor dataset, leaderboard row, or trial URL")
    parser.add_argument("--leaderboard", help="Only this leaderboard name (default: all public leaderboards)")
    parser.add_argument("--output", type=Path, default=HERE / "dataset")
    parser.add_argument("--site", type=Path, default=HERE / "dist")
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--interval", type=float, default=0.08, help="Minimum time between request starts, globally")
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--retries", type=int, default=1)
    parser.add_argument("--refresh", action="store_true", help="Refresh discovery and downloaded trajectories")
    parser.add_argument("--rediscover", action="store_true", help="Refresh leaderboard discovery while preserving successful downloads")
    parser.add_argument("--model", action="append", default=[], help="Download only matching model label/id; repeatable")
    parser.add_argument("--task", action="append", default=[], help="Download only matching task name; repeatable")
    parser.add_argument("--limit", type=int, help="Maximum trajectory requests (for a smoke test)")
    parser.add_argument("--checkpoint-every", type=int, default=250, help="Rebuild the local site after this many completed requests (0: end only)")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--web-text-only", action="store_true", help="Omit embedded binary images from web assets only; dataset JSONL remains lossless")
    args = parser.parse_args(argv)
    if not 1 <= args.workers <= 64 or args.interval < 0 or args.retries < 0 or args.timeout <= 0 or args.checkpoint_every < 0:
        parser.error("workers must be 1..64; interval/retries nonnegative; timeout positive")
    root, site = args.output.resolve(), args.site.resolve()
    if args.command in ("crawl", "discover"):
        parsed = urlparse(args.url)
        if parsed.scheme != "https" or parsed.hostname != "hub.harborframework.com":
            parser.error("--url must be an https://hub.harborframework.com URL")
    if args.command == "status":
        old_path = root / "crawl-status.json"
        old = json.loads(old_path.read_text()) if old_path.exists() else None
        active = writer_active(root) or legacy_writer_active(root)
        print(json.dumps({"state": "running" if active else "stopped", "output": str(root), **inventory(root), "last_session": old}, ensure_ascii=False, indent=2))
        return 0
    if args.command == "serve":
        from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
        from functools import partial
        print(f"http://localhost:{args.port}", flush=True)
        ThreadingHTTPServer(("127.0.0.1", args.port), partial(SimpleHTTPRequestHandler, directory=str(site))).serve_forever()
        return 0
    if args.command == "snapshot":
        snapshot(root,site,web_text_only=True)
        return 0
    try:
        with dataset_lock(root):
            return execute(args, root, site)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2


def execute(args, root, site):
    if args.command == "build":
        build(root, site, args.web_text_only)
        return 0
    parsed = urlparse(args.url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    match = re.match(r"(/datasets/[^/]+/[^/]+)(?:/([^/?]+))?", parsed.path)
    dataset_path = match.group(1) + "/" + (match.group(2) or "latest") if match else urlparse(DEFAULT_URL).path
    client = Client(origin, root / "_http", interval=args.interval, retries=args.retries, timeout=args.timeout, refresh=args.refresh or args.rediscover)
    catalog_path = root / "catalog.json"
    if catalog_path.exists() and not (args.refresh or args.rediscover):
        catalog = json.loads(catalog_path.read_text())
        if catalog["dataset"] != dataset_path:
            raise ValueError("Output contains a different dataset; choose another --output or use --refresh")
        if args.leaderboard and any(m["leaderboard"] != args.leaderboard for m in catalog["models"]):
            raise ValueError("Cached catalog uses other leaderboards; use --rediscover to change the selection")
    else:
        print("Discovering all public leaderboard rows…", flush=True)
        boards = discover(client, dataset_path, args.leaderboard)
        catalog = {"origin": origin, "dataset": dataset_path, "source_url": origin + dataset_path + "?tab=leaderboard", "discovered_at": now(), "models": [], "trials": [], "discovery_errors": []}
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(discover_row, client, dataset_path, board, row): row for board in boards for row in board["rows"] if row.get("status") != "hide"}
            for future in as_completed(futures):
                row = futures[future]
                try:
                    model, trials = future.result()
                    catalog["models"].append(model)
                    catalog["trials"].extend(dict(t, model_id=model["id"]) for t in trials)
                    print(f"  {model['name']} / {model['agent']} ({model['effort']}): {len(trials)} trials", flush=True)
                except Exception as exc:
                    catalog["discovery_errors"].append({"row_id": row["id"], "error": str(exc)})
                    print(f"  DISCOVERY ERROR {row['id']}: {exc}", file=sys.stderr, flush=True)
        catalog["models"].sort(key=lambda m: (m["rank"] or 9999, m["id"]))
        catalog["discovery_complete"] = not catalog["discovery_errors"]
        atomic(catalog_path, dumps(catalog))
    if args.command == "discover":
        build(root, site, args.web_text_only)
        return 1 if catalog.get("discovery_errors") else 0
    trials = catalog["trials"]
    models = {m["id"]: m for m in catalog["models"]}
    if args.model:
        trials = [t for t in trials if any(q.lower() in (models[t["model_id"]]["name"] + t["model_id"]).lower() for q in args.model)]
    if args.task:
        trials = [t for t in trials if any(q.lower() in t["task_name"].lower() for q in args.task)]
    if args.limit is not None:
        trials = trials[:max(0, args.limit)]
    trials = list({trial["id"]: trial for trial in trials}.values())
    # Round-robin configurations so a partial crawl remains useful for comparisons.
    by_model = defaultdict(list)
    for trial in trials:
        by_model[trial["model_id"]].append(trial)
    trials = [items[index] for index in range(max((len(v) for v in by_model.values()), default=0)) for items in by_model.values() if index < len(items)]
    counts = defaultdict(int)
    new_trials, retry_trials = [], []
    for trial in trials:
        state = cache_status(root / "_runs" / (trial["id"] + ".json"))
        if state == "downloaded" and not args.refresh:
            counts["cached"] += 1
        elif state == "pending":
            new_trials.append(trial)
        else:
            retry_trials.append(trial)
    queue = new_trials + retry_trials
    print(f"Selected {len(trials)} trials: skip {counts['cached']} cached; fetch {len(new_trials)} new, then retry {len(retry_trials)} failures; {args.workers} workers.", flush=True)
    pool = ThreadPoolExecutor(max_workers=args.workers)
    pending = []
    interrupted = False
    started = now()
    clock_start = time.monotonic()
    completed = counts["cached"]
    monitor_stop = threading.Event()
    last_checkpoint = 0
    def progress(completed, state="running"):
        elapsed = max(0.001, time.monotonic() - clock_start)
        metrics = client.stats()
        status = dumps({"pid": os.getpid(), "state": state, "started_at": started, "updated_at": now(), "selected_trials": len(trials), "completed": completed, "remaining": len(trials) - completed, "counts": dict(counts), "elapsed_seconds": round(elapsed, 1), "downloaded_per_minute": round(counts.get("downloaded", 0) * 60 / elapsed, 2), "network_mbps": round(metrics["network_bytes"] / 1e6 / elapsed, 3), **metrics})
        atomic(root / "crawl-status.json", status)
        atomic(site / "data" / "crawl-status.json", status)
    progress(completed)
    def monitor():
        ticks = 0
        while not monitor_stop.wait(2):
            progress(completed)
            ticks += 1
            if ticks % 15 == 0:
                stats = client.stats()
                elapsed = max(0.001, time.monotonic() - clock_start)
                print(f"  {completed}/{len(trials)} · new {counts.get('downloaded', 0)} · {counts.get('downloaded', 0)*60/elapsed:.1f} trajectories/min · {stats['network_bytes']/1e6/elapsed:.2f} MB/s · active {stats['active_requests']}", flush=True)
    monitor_thread = threading.Thread(target=monitor, daemon=True)
    monitor_thread.start()
    try:
        pending = {pool.submit(download, client, root, t, args.refresh, True): t for t in queue}
        original_trials = []
        for future in as_completed(pending):
            result = future.result()
            if result == "deferred":
                original_trials.append(pending[future])
                continue
            counts[result] += 1
            completed += 1
            i = completed
            if i % 25 == 0 or i == len(trials):
                print(f"  {i}/{len(trials)}: {dict(counts)}", flush=True)
                progress(i)
            changed = sum(value for key, value in counts.items() if key != "cached")
            if args.checkpoint_every and changed - last_checkpoint >= args.checkpoint_every and i != len(trials):
                build(root, site, args.web_text_only)
                last_checkpoint = changed
        if original_trials:
            print(f"Fetching {len(original_trials)} original ATIF files with 2 memory-bounded workers…", flush=True)
            pending = [pool.submit(download, client, root, t, args.refresh, False, True) for t in original_trials]
            for future in as_completed(pending):
                counts[future.result()] += 1
                completed += 1
                if completed % 10 == 0 or completed == len(trials):
                    print(f"  {completed}/{len(trials)}: {dict(counts)}", flush=True)
                    progress(completed)
    except KeyboardInterrupt:
        interrupted = True
        for future in pending:
            future.cancel()
        print("Stopping new requests; preserving completed downloads…", flush=True)
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
        monitor_stop.set()
        monitor_thread.join()
        progress(sum(counts.values()), "building")
        manifest = build(root, site, args.web_text_only)
        progress(sum(counts.values()), "interrupted" if interrupted else "complete")
    if interrupted:
        return 130
    return 1 if catalog.get("discovery_errors") or counts["error"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
