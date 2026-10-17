#!/usr/bin/env python3
"""Summarize the local flat trial table without loading raw ATIF trajectories."""
import argparse
from collections import defaultdict
import csv
import json
from pathlib import Path


def summarize(path, group="model_id"):
    groups = defaultdict(lambda: {"listed": 0, "downloaded": 0, "scored": 0, "passed": 0})
    with path.open(newline="", encoding="utf-8") as stream:
        for row in csv.DictReader(stream):
            item = groups[row[group]]
            if group == "model_id":
                item.update(model=row["model"], agent=row["agent"], reasoning_effort=row["reasoning_effort"])
            item["listed"] += 1
            item["downloaded"] += row["download_status"] == "downloaded"
            if row["is_scored"] == "1" and row["reward"] != "":
                reward = float(row["reward"])
                item["scored"] += 1
                item["passed"] += reward == 1
    return [{group: key, **value, "observed_success_rate": value["passed"] / value["scored"] if value["scored"] else None}
            for key, value in sorted(groups.items())]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trials", type=Path, default=Path(__file__).resolve().parent / "hub-export/analysis/trials.csv")
    parser.add_argument("--group", choices=["model_id", "task_name"], default="model_id")
    args = parser.parse_args()
    print(json.dumps(summarize(args.trials, args.group), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
