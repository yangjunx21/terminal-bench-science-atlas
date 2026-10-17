#!/usr/bin/env python3
"""Prepare a lossless Hub snapshot, then upload it with the authenticated hf CLI."""
from __future__ import annotations

import argparse
from collections import defaultdict
import csv
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import crawl

HERE = Path(__file__).resolve().parent
DEFAULT_REPO = "yangjunxiao2021/terminal-bench-science-trajectories"
CODE_REPO = "https://github.com/yangjunx21/terminal-bench-science-atlas"


def file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_csv(path, rows, fields=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = fields or list(rows[0])
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    temp.replace(path)


def card(manifest, repo):
    counts = manifest["counts"]
    return f"""---
pretty_name: Terminal-Bench Science Trajectories
language:
- en
tags:
- benchmark
- agent-trajectories
- science
- evaluation
configs:
- config_name: trials
  data_files:
  - split: test
    path: analysis/trials.csv
- config_name: models
  data_files:
  - split: test
    path: analysis/models.csv
- config_name: tasks
  data_files:
  - split: test
    path: analysis/tasks.csv
---

# Terminal-Bench Science trajectories

An archival snapshot of public Harbor evaluation runs, with original ATIF
trajectories and flat analysis tables. Code and cross-model comparison UI:
[{CODE_REPO}]({CODE_REPO}).

**This is a partial capture, not a claim that all trajectories were downloaded.**

| Snapshot | Value |
|---|---:|
| Captured at (UTC) | {manifest['created_at']} |
| Model / agent configurations | {len(manifest['models'])} |
| Tasks | {len(manifest['tasks'])} |
| Publicly listed trials | {manifest['listed_trials']} |
| Source trajectories captured | {counts.get('downloaded', 0)} |
| Download errors | {counts.get('error', 0)} |
| Source returned no trajectory | {counts.get('unavailable', 0)} |
| Not downloaded yet | {counts.get('pending', 0)} |
| Official trials with no public listing | {manifest['unlisted_trials']} |

Raw files upload incrementally. `upload-status.json` records whether the complete
prepared snapshot has finished uploading. Capture counts above describe the local
snapshot; during upload, a raw object listed in the index may not be on the Hub yet.

## Layout

- `analysis/trials.csv`: one row per listed trial, including missing/error/pending states.
- `analysis/models.csv`: official leaderboard metrics and archive coverage for every configuration.
- `analysis/tasks.csv`: per-task counts and observed scored-run success rates.
- `trajectories/models/<model>/<configuration-id>/<task>.jsonl.gz`: lossless JSONL,
  one full run per line, including repeated runs. `trajectory` is the unmodified
  original ATIF object when valid, or null with an explicit download state.
  Nonstandard source JSON is preserved exactly in `raw_trajectory`; a separately
  labeled `reading_trajectory` may project unquoted `[REDACTED]` markers to null.
- `manifest.json`, `catalog.json`, `inventory.json`: provenance and file checksums.

The Dataset Viewer reads only the explicitly configured flat CSV tables. It does
not infer an Arrow schema across heterogeneous nested ATIF objects.

## Analyze

```python
from datasets import load_dataset
df = load_dataset("{repo}", "trials", split="test", token=True).to_pandas()
scored = df[(df.is_scored == 1) & df.reward.notna()]
rates = scored.assign(passed=scored.reward.eq(1)).groupby("model").passed.mean()
print(rates.sort_values(ascending=False))
```

```python
import gzip, json
from huggingface_hub import hf_hub_download
row = df.loc[df.download_status.eq("downloaded")].iloc[0]
path = hf_hub_download("{repo}", row.raw_path, repo_type="dataset", token=True)
with gzip.open(path, "rt") as f:
    runs = [json.loads(line) for line in f]
trajectory = next(r["trajectory"] for r in runs if r["trial_id"] == row.trial_id)
```

## Method and provenance

Source: [{manifest['source_url']}]({manifest['source_url']}). All source trial links,
job IDs, model / agent settings, errors, and retrieval timestamps are preserved.
Private, unlisted and deleted source runs are not reconstructed. Missing reward
values are not treated as zero. Observed analysis rates use `is_scored == 1` and
numeric reward; success means reward equals 1. Official leaderboard rates use the
source's own denominator and are kept separately. Run ordinals are not matched seeds.

This dataset is intended for analysis of evaluation behavior. Original benchmark
canary text and embedded images are preserved. Code licensing is separate from
upstream benchmark content; no new license is assigned to that content.
"""


def prepare(root, stage, repo=DEFAULT_REPO):
    catalog = json.loads((root / "catalog.json").read_text())
    stage.mkdir(parents=True, exist_ok=True)
    state_file = stage / "_state.json"
    previous = json.loads(state_file.read_text()) if state_file.exists() else {}
    state, groups = {}, defaultdict(list)
    models = {m["id"]: m for m in catalog["models"]}
    for trial in catalog["trials"]:
        groups[(trial["model_id"], trial["task_name"])].append(trial)
    rows, files, counts = [], [], defaultdict(int)
    # Source cache files are atomically replaced, so hardlinks give a stable
    # read-only snapshot even if the user resumes downloading in another terminal.
    with tempfile.TemporaryDirectory(prefix="harbor-hf-snapshot-") as directory:
        frozen = Path(directory)
        for source in (root / "_runs").glob("*.json"):
            try:
                os.link(source, frozen / source.name)
            except FileNotFoundError:
                continue
            except OSError:
                shutil.copy2(source, frozen / source.name)
        for index, ((model_id, task), trials) in enumerate(sorted(groups.items()), 1):
            model = models[model_id]
            relative = f"trajectories/models/{model['directory']}/{model_id}/{crawl.slug(task)}.jsonl.gz"
            target = stage / relative
            versions = [(t["id"], (frozen / (t["id"] + ".json")).stat().st_mtime_ns, (frozen / (t["id"] + ".json")).stat().st_size)
                        if (frozen / (t["id"] + ".json")).exists() else (t["id"], None) for t in trials]
            fingerprint = hashlib.sha256(crawl.dumps([2, model, trials, versions]).encode()).hexdigest()
            old = previous.get(relative)
            if old and old["fingerprint"] == fingerprint and target.exists():
                batch, descriptor = old["rows"], old["file"]
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                temp = target.with_suffix(target.suffix + ".tmp")
                batch = []
                with temp.open("wb") as stream:
                    with gzip.GzipFile(filename="", fileobj=stream, mode="wb", mtime=0, compresslevel=6) as output:
                        for trial in sorted(trials, key=lambda t: (t.get("job_name") or "", t["id"])):
                            cached = frozen / (trial["id"] + ".json")
                            result = json.loads(cached.read_text()) if cached.exists() else {"status": "pending", "trajectory": None}
                            trajectory = result.get("trajectory")
                            record = {
                                "schema_version": "harbor-science-v1", "model_id": model_id, "model": model["name"],
                                "agent": model["agent"], "reasoning_effort": model["effort"], "task_name": task,
                                "trial_id": trial["id"], "job_id": trial["job_id"],
                                "source_url": f"{catalog['origin']}/jobs/{trial['job_id']}/trials/{trial['id']}",
                                "trajectory_url": result.get("source_url"), "fetched_at": result.get("fetched_at"),
                                "download_status": result["status"], "download_error": result.get("error"),
                                "trial": trial, "trajectory": trajectory,
                            }
                            for key in ("raw_trajectory","reading_trajectory","source_format","parse_error","source_sha256","source_bytes","download_transport"):
                                if key in result:
                                    record[key] = result[key]
                            output.write((crawl.dumps(record) + "\n").encode())
                            reading = trajectory or result.get("reading_trajectory")
                            steps = reading.get("steps", []) if reading else []
                            batch.append({
                                "trial_id": trial["id"], "model_id": model_id, "model": model["name"], "agent": model["agent"],
                                "reasoning_effort": model["effort"], "task_name": task, "job_id": trial["job_id"],
                                "source_format":result.get("source_format","json"), "parse_error":result.get("parse_error"),
                                "reward": trial.get("reward"), "is_scored": int(trial.get("is_scored", True) is not False),
                                "status": trial.get("status"), "download_status": result["status"], "download_error": result.get("error"),
                                "input_tokens": trial.get("input_tokens"), "output_tokens": trial.get("output_tokens"),
                                "cache_tokens": trial.get("cache_tokens"), "cost_usd": trial.get("cost_usd"),
                                "duration_seconds": crawl.duration(trial), "steps": len(steps),
                                "tool_calls": sum(len(s.get("tool_calls") or []) for s in steps),
                                "source_url": record["source_url"], "raw_path": relative,
                                "trajectory_sha256": hashlib.sha256(crawl.dumps(trajectory).encode()).hexdigest() if trajectory else None,
                            })
                temp.replace(target)
                descriptor = {"path": relative, "bytes": target.stat().st_size, "sha256": file_hash(target), "records": len(batch)}
            rows.extend(batch)
            files.append(descriptor)
            for row in batch:
                counts[row["download_status"]] += 1
            state[relative] = {"fingerprint": fingerprint, "file": descriptor, "rows": batch}
            if index % 50 == 0 or index == len(groups):
                print(f"Prepared {index}/{len(groups)} task files", flush=True)
    official = sum(m["official_metrics"].get("tasks", m["expected_trials"]) for m in models.values())
    tasks = sorted({t["task_name"] for t in catalog["trials"]})
    manifest = {
        "schema_version": "harbor-science-hf-v1", "created_at": crawl.now(), "repo_id": repo,
        "source_url": catalog["source_url"], "listed_trials": len(rows), "official_trials": official,
        "unlisted_trials": max(0, official - len(rows)), "counts": dict(counts), "models": list(models.values()),
        "tasks": tasks, "raw_bytes_compressed": sum(f["bytes"] for f in files), "raw_files": len(files),
        "discovery_complete": catalog.get("discovery_complete", False), "discovery_errors": catalog.get("discovery_errors", []),
    }
    model_rows = []
    for model in models.values():
        subset = [r for r in rows if r["model_id"] == model["id"]]
        model_rows.append({"model_id": model["id"], "model": model["name"], "agent": model["agent"], "reasoning_effort": model["effort"],
                           "official_trials": model["official_metrics"].get("tasks"), "listed_trials": len(subset),
                           "downloaded": sum(r["download_status"] == "downloaded" for r in subset),
                           "official_accuracy_pct": model["official_metrics"].get("accuracy"),
                           "official_passes": model["official_metrics"].get("passes"),
                           "official_tokens": model["official_metrics"].get("total_tokens"),
                           "official_cost_usd": model["official_metrics"].get("total_cost_usd"), "source_url": model["source_url"]})
    task_rows = []
    for task in tasks:
        subset = [r for r in rows if r["task_name"] == task]
        scored = [r for r in subset if r["is_scored"] and type(r["reward"]) in (int, float)]
        passed = sum(r["reward"] == 1 for r in scored)
        task_rows.append({"task_name": task, "listed_trials": len(subset), "scored_trials": len(scored), "passed": passed,
                          "observed_success_rate": passed / len(scored) if scored else None,
                          "downloaded": sum(r["download_status"] == "downloaded" for r in subset)})
    write_csv(stage / "analysis/trials.csv", rows)
    write_csv(stage / "analysis/models.csv", model_rows)
    write_csv(stage / "analysis/tasks.csv", task_rows)
    crawl.atomic(stage / "catalog.json", crawl.dumps(catalog))
    crawl.atomic(stage / "manifest.json", crawl.dumps(manifest))
    crawl.atomic(stage / "inventory.json", crawl.dumps(files))
    crawl.atomic(stage / "README.md", card(manifest, repo))
    crawl.atomic(stage / "upload-status.json", crawl.dumps({"state": "prepared", "snapshot_at": manifest["created_at"], "updated_at": crawl.now()}))
    crawl.atomic(state_file, crawl.dumps(state))
    print(f"Snapshot: {dict(counts)}, {manifest['raw_bytes_compressed']/1e9:.2f} GB compressed; {stage}", flush=True)
    return manifest


def hf_command():
    if shutil.which("hf"):
        return [shutil.which("hf")]
    if shutil.which("uvx"):
        return [shutil.which("uvx"), "--from", "huggingface_hub", "hf"]
    raise RuntimeError("Install the Hugging Face CLI (hf) or uv before uploading. See README.md.")


def upload(stage, repo, public=False, metadata_only=False, transport='http'):
    if transport == 'http' and not metadata_only:
        if shutil.which('uvx'):
            runtime = [shutil.which('uvx'), '--from', 'huggingface_hub', 'python']
        else:
            runtime = [sys.executable]
        command = runtime + [str(HERE / 'upload_http.py'), '--stage', str(stage), '--repo', repo, '--parent-lock-held']
        if public:
            command.append('--public')
        subprocess.run(command, check=True)
        return
    base = hf_command()
    subprocess.run(base + ["repos", "create", repo, "--type", "dataset", "--exist-ok", "--public" if public else "--private"], check=True)
    manifest = json.loads((stage / "manifest.json").read_text())
    def status(state):
        crawl.atomic(stage / "upload-status.json", crawl.dumps({"state": state, "snapshot_at": manifest["created_at"], "updated_at": crawl.now()}))
    status("metadata_only" if metadata_only else "uploading_raw")
    with tempfile.TemporaryDirectory(prefix="harbor-hf-metadata-") as directory:
        metadata = Path(directory)
        for name in ["README.md", "manifest.json", "catalog.json", "inventory.json", "upload-status.json"]:
            shutil.copy2(stage / name, metadata / name)
        shutil.copytree(stage / "analysis", metadata / "analysis")
        subprocess.run(base + ["upload", repo, str(metadata), ".", "--type", "dataset", "--commit-message", "Update trajectory snapshot metadata and analysis tables"], check=True)
    if not metadata_only:
        try:
            # hf >=2.2 upload is resumable and splits large folders into commits.
            subprocess.run(base + ["upload", repo, str(stage / "trajectories"), "trajectories", "--type", "dataset", "--include", "*.jsonl.gz", "--commit-message", "Upload lossless model/task trajectory JSONL"], check=True)
        except BaseException:
            status("interrupted")
            raise
        status("complete")
        subprocess.run(base + ["upload", repo, str(stage / "upload-status.json"), "upload-status.json", "--type", "dataset", "--commit-message", "Mark prepared snapshot upload complete"], check=True)
    print(f"https://huggingface.co/datasets/{repo}", flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "upload"])
    parser.add_argument("--data", type=Path, default=HERE / "dataset")
    parser.add_argument("--stage", type=Path, default=HERE / "hub-export")
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--public", action="store_true", help="Create a public dataset; default private. Existing visibility is preserved.")
    parser.add_argument("--metadata-only", action="store_true", help="Upload only dataset card, provenance and analysis tables")
    parser.add_argument('--transport', choices=['http', 'xet'], default='http', help='HTTP uses bounded, verified, resumable commits')
    args = parser.parse_args(argv)
    stage = args.stage.resolve()
    with crawl.dataset_lock(stage):
        if args.command == "prepare":
            prepare(args.data.resolve(), stage, args.repo)
        else:
            upload(stage, args.repo, args.public, args.metadata_only, args.transport)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
