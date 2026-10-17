#!/usr/bin/env python3
"""Summarize the local flat trial table without loading raw ATIF trajectories."""
import argparse
from collections import defaultdict
import csv
import json
from pathlib import Path


def summarize(path, group="model_id", models=None):
    models=models or {}
    groups = defaultdict(lambda: {"listed": 0, "downloaded": 0, "scored": 0, "passed": 0})
    with path.open(newline="", encoding="utf-8") as stream:
        for row in csv.DictReader(stream):
            item = groups[row[group]]
            if group == "model_id":
                model=models.get(row["model_id"],{})
                item.update(model=row.get("model",model.get("name",row["model_id"])), agent=row.get("agent",model.get("agent","")), reasoning_effort=row.get("reasoning_effort",model.get("effort","")))
            item["listed"] += 1
            item["downloaded"] += row["download_status"] == "downloaded"
            scored=row.get("is_scored",row.get("scored","True")).lower() in ("1","true")
            if scored and row["reward"] != "":
                reward = float(row["reward"])
                item["scored"] += 1
                item["passed"] += reward == 1
    return [{group: key, **value, "observed_success_rate": value["passed"] / value["scored"] if value["scored"] else None}
            for key, value in sorted(groups.items())]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    here=Path(__file__).resolve().parent
    local=here/"dist/data/runs.csv"
    parser.add_argument("--trials", type=Path, default=local if local.exists() else here/"hub-export/analysis/trials.csv")
    parser.add_argument("--catalog",type=Path,default=here/"dataset/catalog.json")
    parser.add_argument("--group", choices=["model_id", "task_name"], default="model_id")
    args = parser.parse_args()
    models={m["id"]:m for m in json.loads(args.catalog.read_text())["models"]} if args.catalog.exists() else {}
    print(json.dumps(summarize(args.trials, args.group, models), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
