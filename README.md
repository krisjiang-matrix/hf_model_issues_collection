# HF 模型问题速查 (Model Pitfall Lookup)

聚合 Hugging Face 开源模型的真实问题：HF Discussions · 推理引擎 GitHub Issues · Reddit · Agent 框架 issue 区交叉验证，按 模型×量化×引擎 结构化检索。

## 运行

- **在线版**：任意静态服务器托管本目录，打开 `index.html`（数据走 `fetch data/*.json`）
- **离线版**：直接双击 `app.html`（数据已内嵌，无需服务器）

## 目录结构

```
├── index.html            # 主站（数据与页面分离，fetch JSON）
├── app.html              # 离线内嵌版（由脚本重新生成，勿手改）
├── data/*.json           # 问题条目库（人工审核后的正式数据）
└── automation/
    ├── repos.yaml        # 监控仓库清单（官方9家+量化6家+引擎6个+agent框架5个）
    ├── fetch_candidates.py   # 每日爬取脚本 → 候选 JSON（不自动入库）
    ├── seen_urls.json    # 去重状态
    └── candidates/       # 每日候选输出
```

## 每日自动化

`.github/workflows/daily-fetch.yml`：每天 UTC 01:00（北京 09:00）运行，产出候选到 `automation/candidates/` 并自动 commit。

本地手动跑：

```bash
pip install -r automation/requirements.txt
python automation/fetch_candidates.py              # 抓昨天至今
python automation/fetch_candidates.py --backfill   # 回填 N 天（repos.yaml: backfill_days）
```

可选 Secrets：`HF_TOKEN`（提高 HF API 配额）；`GITHUB_TOKEN` 由 Actions 自动注入。

## 数据流

```
爬取候选 → LLM 初筛分类(待接入) → 人工审核 → data/*.json → 前端展示
```

候选**不会**自动进入正式库——条目质量（verdict 归因、根因、解决方案）需要人工/LLM 判定后写入 `data/`，并补 `created_at`/`updated_at` 字段。

## 条目 schema

见 `data/` 中现有条目。关键字段：`verdict`（模型本身/量化/引擎/组合/环境问题）、`confidence`、`sources[]`（必须真实可溯源）。
