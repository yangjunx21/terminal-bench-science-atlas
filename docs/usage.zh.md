## 当前本地工作流

独立项目目录：`/Users/junxiao/code/terminal-bench-science-atlas`。其中 `dataset` 和 `dist` 复用 OpenDiscovery 下已有的数据与网页目录，不会重新下载一份。

```sh
cd /Users/junxiao/code/terminal-bench-science-atlas
python3 crawl.py status
# 普通轨迹：先取新条目，再重试失败条目
python3 -u crawl.py crawl --workers 64 --checkpoint-every 0 --web-text-only
# 大文件：先停止普通爬虫，再用字节级分块续传
python3 accelerate.py plan
caffeinate -i python3 -u accelerate.py fetch --workers 128
# 查看实时日志
 tail -f dataset/crawl-storage.log
# 网页可在下载时更新，下载完成也会自动导出
python3 crawl.py snapshot
python3 crawl.py serve --port 8765
```

页面地址：<http://localhost:8765>。后台下载会周期性更新网页快照；原始文件很大时，网页索引会落后于实时归档数量，页面会分别显示。源文件含未加引号的 `[REDACTED]` 时，完整原文保存在 `raw_trajectory`，阅读副本只将被脱敏的字段留空并明确标注。

GitHub/Hugging Face 均先建为私有；数据上传暂缓。后续上传命令见根目录 README。

---

# Science Atlas — Terminal-Bench Science dataset + analysis

独立、零第三方依赖的 Harbor 公开轨迹下载器与静态分析网站。Python 3.9+；浏览器需支持 `DecompressionStream`。

```bash
cd terminal-bench-science-atlas
python3 crawl.py crawl --workers 16
python3 crawl.py serve --port 8765
# 打开 http://localhost:8765
```

下载器从**数据集全部公开榜单**自动发现配置与 trial 分页，不只下载给定示例 job。支持传入用户提供的 trial URL；此时默认定位 Terminal-Bench Science 数据集。`--url` 也接受 dataset / leaderboard row URL。默认输出 `dataset/`，网页输出 `dist/`，均不进入 Git。

```bash
# 只枚举运行与指标；仍生成可分析的网页
python3 crawl.py discover

# 已成功下载的 trial 自动跳过；错误 / 缺失下次重试
python3 crawl.py crawl

# 更新榜单，保留成功下载的轨迹
python3 crawl.py crawl --rediscover

# 强制重新请求榜单及全部选定轨迹
python3 crawl.py crawl --refresh

# 单题 / 指定模型；筛选仅限制下载，保留完整榜单及运行清单
python3 crawl.py crawl --task traffic-flux-inversion
python3 crawl.py crawl --model 'GPT-6 Astra' --model 'Opus 5.5'
python3 crawl.py crawl --limit 10

# 单独重建；不访问网络
python3 crawl.py build

# 供线上发布的轻量文本阅读包；本地 dataset 中的 JSONL 仍保持完整无损
python3 crawl.py build --web-text-only --site /path/to/web-text

# 自定义位置、并发、限速（请求启动间隔为全局值）
python3 crawl.py crawl --output /path/to/dataset --site /path/to/dist \
  --workers 16 --interval 0.08 --retries 4 --timeout 90

# 后台全量下载，默认每 250 条重建网页，结束时最终重建
nohup python3 -u crawl.py crawl --workers 16 > dataset/crawl.log 2>&1 &
tail -f dataset/crawl.log
# 进度还会写入 dataset/crawl-status.json

# 无网络回归验证
python3 -m unittest discover -s . -p 'test_*.py'
node --check web/app.js
```

网页功能：数据集概览、模型官方榜单指标、下载覆盖率、70 题检索与难度/分歧排序、最多 4 模型横向比较、每个配置的独立运行切换、系统提示开关、最终回复/Agent 消息/工具调用过滤、步骤全文检索、同步滚动、原始 ATIF 字段展开、可复制的题目+模型+运行链接、逐题 JSONL 下载、成本散点图、题目成功率矩阵与配对题目胜平负、CSV 与 manifest 导出。手机端支持横向滑动阅读。

## 数据格式

```text
dataset/
  catalog.json                 # 发现的榜单配置与全部 trial 元数据
  manifest.json                # 可供分析的索引（无轨迹大文本）
  models/
    <model-label>-<hash>/
      <leaderboard-row-id>/     # 区分同一模型的 agent / effort / 多次提交
        <task-name>-<hash>.jsonl
  _runs/<trial-id>.json         # 原子写入的逐运行下载缓存，用于续传
  _http/<request-hash>.txt      # 榜单与分页请求缓存
dist/
  index.html / app.js / style.css
  data/manifest.json
  data/runs.csv
  data/models/.../*.jsonl.gz    # 同一 JSONL 的无损 gzip，按题按需加载
  data/models/.../*.part-*      # 超过 24 MiB 的 gzip 按字节分片，由网页自动拼接
```

**每题一个 JSONL，每行一次完整运行**。多次运行分别保存，按 job 名称和 trial ID 稳定排序。行内字段：

| 字段 | 含义 |
|---|---|
| `schema_version` | `harbor-science-v1` |
| `model_id`, `model`, `agent`, `reasoning_effort` | 配置标识和可读名称 |
| `task_name`, `trial_id`, `job_id` | 源站唯一标识 |
| `source_url`, `fetched_at` | 原始轨迹页、实际抓取时间 |
| `trajectory_url` | 实际下载 ATIF 的 API / 原始文件 URL |
| `download_status` | `downloaded` / `pending` / `unavailable` / `error` |
| `download_error` | 失败原因，成功为 null |
| `trial` | 原始运行元数据、reward、token、成本、时间、评分标记 |
| `trajectory` | **未经删减的原始 ATIF 对象**；不可用时为 null |

未知 ATIF 字段也会保留；manifest 包含轨迹规范化 JSON 的 SHA-256。原始图片、工作目录产物和与轨迹分开的日志文件不下载；ATIF 中的引用原样保留。文本中的 image 引用在网页显示路径，可回到 Harbor 查看。

默认网页与本地 JSONL 一样无损。可选 `--web-text-only` 仅为网页导出移除内嵌图片/二进制，保留文字、代码、工具调用和文本返回；网页顶部、原始字段及下载按钮会明确标记文本版，下载文件名带 `.text.jsonl`。此选项不会修改 `dataset/models/.../*.jsonl` 或逐 trial 下载缓存。适用于托管上传大文件受限的情形，完整图片仍可从本地数据或 Harbor 原始页面查看。manifest 中的轨迹 SHA-256 始终对应本地完整 ATIF。

## 覆盖与分析口径

本次调查（2026-10-17）：公开榜单 **17 个配置，每个 210 次评测**。其中 **12 个配置公开 trial 列表，共 2,520 次运行、70 道题**；Fable 5.1、DeepSeek V4.1 Flash、DeepSeek V4 Pro、Gemini 3.8 Flash、Gemini 3.7 Flash 的行页面返回空 job / trial 列表。这 5 个配置保留官方指标，并标明“仅榜单分数”；其余 1,050 次评测不能被当作已下载。可用轨迹的实际归档状态以 `manifest.json` 为准。上游更新后可用 `--rediscover` 重查。

榜单页与成本图使用官方聚合值，不用下载子集覆盖官方值。矩阵分母仅使用 `is_scored != false` 且 reward 为数值的运行，成功定义为 reward = 1；未知分数不当作零。配对分析只比较双方所有预期运行均有数值评分的共同题目，按每题平均 reward 统计胜/平/负，**不假定运行序号代表匹配的随机种子**。运行耗时包含环境准备及验证。推理仅显示源轨迹实际公开的内容，不从 token 数推断。

发现完整性和下载完整性分开记录：分页数量不符会报错；行发现失败保存在 `discovery_errors`；无法访问/无 ATIF 的运行保留记录与原因。断点续传不会把失败缓存视为成功。默认每 250 次完成请求重建本地网站；可用 `--checkpoint-every 0` 仅在结束时构建。各配置轮流下载，使部分完成的数据也可横向比较。Ctrl-C 停止排队请求，等待进行中的请求结束后写出当前网站。

## 接口与维护

Harbor 目前以 Next.js Flight HTML 提供 SSR 的 trial 分页，通过只读 `fetchLeaderboardWithRows` server action 读取榜单，通过 `/api/trials/<id>/trajectory?jobId=<id>` 读取 ATIF。标准轨迹端点返回 404/5xx 时回退到同一公开文件浏览器的 `/api/trials/<id>/files/agent/trajectory.json?jobId=<id>`，适用于超大 ATIF。请求使用 gzip。脚本自动从当前前端 bundle 查找 action ID，不固定写死部署哈希，也不执行远端脚本。站点接口变化会显式报错，需要更新解析逻辑。没有权限的资源不尝试绕过，429/暂时错误进行退避重试。

刷新发现需用 `--rediscover`；`discover` 默认也使用缓存。数据目录同一时刻只运行一个 crawl / build 写入进程。发布可直接托管 `dist/`（GitHub Pages / 静态服务器 / Sites），不依赖后端。外部 Google Fonts 仅用于排版，加载失败会回退系统字体。`dist` 中包含真实公开轨迹，体积可能较大，部署前检查平台单文件与总大小限制。
