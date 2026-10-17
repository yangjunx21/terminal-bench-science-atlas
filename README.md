# Terminal-Bench Science Atlas

Archive public [Harbor Terminal-Bench Science](https://hub.harborframework.com/datasets/terminal-bench-science/terminal-bench-science/latest?tab=leaderboard) trajectories, preserve the original ATIF, and compare models on each task in a static website.

- Code: https://github.com/yangjunx21/terminal-bench-science-atlas
- Dataset: https://huggingface.co/datasets/yangjunxiao2021/terminal-bench-science-trajectories
- [中文使用说明](docs/usage.zh.md)

Python 3.9+ on macOS/Linux; downloading, packaging, analysis and serving use only the Python standard library. The reader includes local, pinned Markdown, KaTeX, syntax-highlighting and sanitization assets, so rendering needs no CDN. Hugging Face upload additionally needs the `hf` CLI or `uvx`. Both repositories were initially created private.

## Download manually

```sh
git clone git@github.com:yangjunx21/terminal-bench-science-atlas.git
cd terminal-bench-science-atlas
python3 crawl.py status
python3 -u crawl.py crawl --workers 8
```

The default output is `dataset/`, next to the script. A successful trajectory is cached under `dataset/_runs/<trial-id>.json`. Downloads resume automatically; rerunning retries failed and missing trajectories. Avoid `--refresh` unless you intend to fetch successful runs again. A file lock prevents two crawlers/builders from writing the same dataset at once. `status` checks the actual process lock, so a stale progress file does not imply downloading is still running.

Press Ctrl+C to stop; active requests finish before a final local export. A full checkpoint can take time because ATIF may include large embedded images. For unattended execution on macOS:

```sh
caffeinate -i python3 -u crawl.py crawl --workers 8 --checkpoint-every 0 2>&1 | tee crawl.log
```

`--checkpoint-every 0` exports at the end; raw successful cache files are saved as each request finishes. The first run discovers public leaderboard configurations. Use `--rediscover` later to refresh the leaderboard without discarding successful downloads.

The crawler skips cache headers without parsing full ATIF, prioritizes new trials before known failures, and defers original-file fallbacks so slow image-heavy files do not block ordinary trajectories. Increase `--workers` up to 64 if the connection benefits from more parallel transfers; request starts remain rate limited. The website shows live speed and distinguishes downloaded cache files from its last exported reading index.

For remaining large objects, stop the ordinary crawler and use resumable parallel storage ranges:

```sh
python3 accelerate.py plan
caffeinate -i python3 -u accelerate.py fetch --workers 128
```

This reads the same public storage objects as Harbor's official CLI, using its published **anonymous publishable key** and enforcing the storage service's access rules. It requires no personal Harbor token. `plan` may run alongside a crawl; `fetch` holds the shared dataset writer lock. Interrupted ranges stay under `dataset/_parts/` and resume from their saved byte offsets. Source size, ETag and exact Content-Range are checked before appending. Complete JSON is validated with bounded memory before entering the success cache. Rerun `fetch` to resume, or `fetch --refresh-plan` to refresh missing metadata. During storage download, a fresh reading snapshot is exported periodically and again at the end. Network ranges are streamed to disk; at most two source objects are parsed concurrently even with 128 connections. Very large source files can still take substantial time; the live page reports transferred bytes as well as trajectory counts.

On the original development machine, `dataset` is a local symlink to `/Users/junxiao/code/OpenDiscovery/tools/harbor-science/dataset`, reusing the existing download. The symlink and data are ignored by Git. A new clone creates its own dataset directory.

## Comparison website

```sh
python3 crawl.py build
python3 crawl.py serve --port 8765
```

Open http://localhost:8765. To update the reading snapshot during an active download, run `python3 crawl.py snapshot` in another terminal; it freezes cache files and exports a separate snapshot without pausing the crawler. The site includes task search, model selection, run selection, aligned side-by-side trajectories, tool arguments/results, official leaderboard scores, archive coverage, and a task/model performance matrix. `dist/` is static and can be hosted on any static server. It loads only the selected task files. To create smaller web assets while keeping dataset JSONL lossless:

```sh
python3 crawl.py build --web-text-only
```

The page labels this text projection explicitly. Embedded binary image payloads are omitted only from web copies. Original downloads and full dataset JSONL remain unchanged.

Task prompts, agent messages and exposed reasoning support Markdown headings, lists, tables, fenced code and TeX (`$...$`, `$$...$$`, `\(...\)`, `\[...\]`). Code and display formulas have copy buttons. The “原始文本” selector shows source text; tool arguments and observations always preserve literal text. Raw HTML and unsafe TeX commands are disabled. Browser assets and their licenses/checksums live in `web/vendor/`.

## Prepare and upload a Hugging Face snapshot

```sh
# Install uv if neither hf nor uvx is available: https://docs.astral.sh/uv/
uvx --from huggingface_hub hf auth login
python3 publish.py prepare
python3 publish.py upload
```

The uploader uses the existing local HF login; do not put access tokens in code or Git. It creates a **private** dataset by default. Existing repository visibility is preserved. Use `--repo OWNER/NAME` for a different dataset. Upload interrupted files by rerunning the same upload command. After more Harbor downloads, rerun `prepare` and `upload`; unchanged task groups are reused.

For a one-shot background sync that uploads the prepared capture now, waits for the active downloader/retry manager, then prepares and uploads the final local capture:

```sh
python3 -u sync_dataset.py
```

On the original development machine this is already running with a sleep inhibitor; progress is in `dataset/hf-sync.log`. Do not start a duplicate. The stage writer lock prevents overlapping packaging/uploads.

`prepare` is offline and freezes the atomically written cache files before packaging, so a separate manual crawl may continue. Its output is `hub-export/`:

```text
analysis/{trials,models,tasks}.csv
trajectories/models/<model>/<configuration-id>/<task>.jsonl.gz
manifest.json
catalog.json
inventory.json
upload-status.json
README.md
```

Each compressed task file contains one JSON object per run, preserving the full original `trajectory` plus source trial metadata and explicit pending/error states. `inventory.json` includes SHA-256 checksums. Model configuration IDs prevent agent/reasoning variants from being mixed. The HF Dataset Viewer exposes flat `trials`, `models` and `tasks` configurations; nested raw ATIF stays as lossless downloadable archives. `upload-status.json` marks when all prepared files are on the Hub; counts describe the capture snapshot even while upload is in progress.

## Analyze

Offline summary after preparing:

```sh
python3 analyze.py
python3 analyze.py --group task_name
```

Or load just the small analysis table without downloading the raw corpus:

```python
from datasets import load_dataset

df = load_dataset(
    "yangjunxiao2021/terminal-bench-science-trajectories",
    "trials", split="test", token=True,
).to_pandas()
scored = df[(df.is_scored == 1) & df.reward.notna()]
summary = scored.assign(passed=scored.reward.eq(1)).groupby(
    ["model", "model_id"]
).agg(n=("passed", "size"), success_rate=("passed", "mean"))
print(summary.sort_values("success_rate", ascending=False))
```

Raw download examples and provenance are in the dataset card. The initial snapshot is partial: some public runs fail to download and some leaderboard configurations do not expose trial listings. Missing scores are never counted as failures. Official leaderboard rates and observed scored-run rates have different denominators. Repeated run ordinals do not identify matched seeds.

## Checks

```sh
python3 -m unittest discover -v
npm ci
npm test
npm run check
```

Node.js 24+ is needed only for renderer tests and `npm run vendor` (rebuild pinned browser assets); normal download/serve does not need Node or npm.

Code is MIT licensed. Source benchmark prompts, trajectories, canaries and media retain their upstream terms; this project does not assign them a new license.

Some source files contain unquoted `[REDACTED]` tokens and are not valid JSON. These are archived exactly as text in `raw_trajectory`, with `trajectory: null`, `source_format: nonstandard_json`, and an explicit parse error. A separate `reading_trajectory` projects only unquoted redaction markers to null; quoted message text is preserved. The website labels this reading copy. No missing token counts are invented.
