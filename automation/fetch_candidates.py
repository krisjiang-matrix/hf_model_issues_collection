#!/usr/bin/env python3
"""
HF 模型问题速查 - 每日候选爬取脚本
====================================
数据源：
  A. GitHub Search API  — 引擎仓库 + Agent 框架仓库 × 模型关键词，搜新建 issue
  B. HF API             — 官方/量化 orgs 最近更新的模型 → Discussions 新帖

输出：automation/candidates/candidates-YYYY-MM-DD.json
去重：automation/seen_urls.json（持久化，按 URL）

用法：
  python fetch_candidates.py                # 日常模式：抓昨天至今
  python fetch_candidates.py --backfill     # 回填模式：按 repos.yaml 的 backfill_days
  GITHUB_TOKEN=xxx python fetch_candidates.py   # 带 token（配额 30 req/min，匿名 10）

设计原则：只产出"候选"，不做自动入库。LLM 分类 + 人工审核是下游步骤。
"""
import json
import os
import sys
import time
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("需要 PyYAML: pip install pyyaml")

BASE = Path(__file__).parent
CONFIG_PATH = BASE / "repos.yaml"
SEEN_PATH = BASE / "seen_urls.json"
CANDIDATES_DIR = BASE / "candidates"

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
HF_TOKEN = os.environ.get("HF_TOKEN", "")

UA = {"User-Agent": "hf-model-pitfalls-fetcher/1.0"}


def http_get(url, headers=None, timeout=30):
    """简单 GET，返回 parsed JSON；失败返回 None 并打印原因"""
    hdrs = dict(UA)
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"  [HTTP {e.code}] {url[:120]}", file=sys.stderr)
        if e.code == 403:
            print("  可能触发 rate limit，考虑设置 GITHUB_TOKEN", file=sys.stderr)
        if e.code in (403, 422):
            # 403=限速 / 422=repo 不存在或查询非法 → 抛出特殊异常让调用方跳过该仓库
            raise RateLimitOrBadQuery(e.code, str(e))
        return None
    except Exception as e:
        print(f"  [ERR] {url[:120]}: {e}", file=sys.stderr)
        return None


def gh_headers():
    h = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        h["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return h


class RateLimitOrBadQuery(Exception):
    """403 (限速) 或 422 (repo 不存在/查询非法) → 调用方应跳过该查询而非重试"""

    def __init__(self, code, detail=""):
        self.code = code
        super().__init__(f"GitHub API {code}: {detail[:200]}")


def load_seen():
    if SEEN_PATH.exists():
        return set(json.loads(SEEN_PATH.read_text(encoding="utf-8")))
    return set()


def save_seen(seen):
    # 只保留最近 20000 条防止无限增长
    lst = sorted(seen)[-20000:]
    SEEN_PATH.write_text(json.dumps(lst, ensure_ascii=False, indent=0), encoding="utf-8")


# ---------- A. GitHub Search ----------

def search_github_issues(repo, keyword, since_iso):
    """在指定 repo 搜 keyword，since 之后创建的 issue。返回候选列表。"""
    q = f"repo:{repo} {keyword} created:>{since_iso} is:issue"
    url = ("https://api.github.com/search/issues?q=" + urllib.parse.quote(q)
           + "&sort=created&order=desc&per_page=20")
    try:
        data = http_get(url, gh_headers())
    except RateLimitOrBadQuery as e:
        # 坏仓库/查询会被 Search API 报 422，限速报 403；
        # 二者都应跳过该查询而不是把整轮拖死。422 多因仓库名失效（见 repos.yaml 注释）
        print(f"  ! 跳过 {repo} / {keyword[:30]}...：{e}", file=sys.stderr)
        return None  # None 表示"此 repo/关键词本次不可用"，调用方据此停掉对该 repo 的后续查询
    if not data:
        return []
    out = []
    for item in data.get("items", []):
        body = (item.get("body") or "")[:500]
        out.append({
            "source_type": "github-issue",
            "repo": repo,
            "matched_keyword": keyword,
            "title": item.get("title", ""),
            "url": item.get("html_url", ""),
            "state": item.get("state", ""),
            "created_at": item.get("created_at", "")[:10],
            "updated_at": item.get("updated_at", "")[:10],
            "comments": item.get("comments", 0),
            "snippet": body.replace("\r", " ").replace("\n", " "),
        })
    return out


def fetch_github(cfg, since_iso, seen):
    cands = []
    repos = cfg.get("engine_repos", []) + cfg.get("agent_framework_repos", [])
    # 汇总所有模型关键词（官方 orgs 的 keywords 并集）
    model_kws = sorted({kw for org in cfg.get("official_orgs", []) for kw in org.get("keywords", [])})
    global_kws = cfg.get("global_keywords", [])

    # 策略：每个 repo × 每组关键词。模型关键词按 3 个一组拼接 OR，减少请求数
    def chunks(lst, n):
        for i in range(0, len(lst), n):
            yield lst[i:i + n]

    total_req = 0
    for repo in repos:
        repo_blocked = False  # 一旦某个查询 403/422，跳过该 repo 其余所有查询
        for group in chunks(model_kws, 3):
            kw = " OR ".join(group)
            results = search_github_issues(repo, kw, since_iso)
            total_req += 1
            if results is None:
                repo_blocked = True
                break
            for r in results:
                if r["url"] and r["url"] not in seen:
                    cands.append(r)
                    seen.add(r["url"])
            time.sleep(2 if GITHUB_TOKEN else 6)  # 匿名配额 10 req/min
        if repo_blocked:
            print(f"  ! 跳过 {repo} 的剩余查询", file=sys.stderr)
            continue
        for kw in global_kws:
            results = search_github_issues(repo, kw, since_iso)
            total_req += 1
            if results is None:
                repo_blocked = True
                break
            for r in results:
                if r["url"] and r["url"] not in seen:
                    cands.append(r)
                    seen.add(r["url"])
            time.sleep(2 if GITHUB_TOKEN else 6)
        if repo_blocked:
            print(f"  ! 跳过 {repo} 的剩余查询", file=sys.stderr)
    print(f"[GitHub] {total_req} 次请求，新增候选 {len(cands)} 条")
    return cands


# ---------- B. Hugging Face Discussions ----------

def hf_recent_models(author, limit=30):
    """拿 org 最近更新的模型列表"""
    url = (f"https://huggingface.co/api/models?author={urllib.parse.quote(author)}"
           f"&sort=lastModified&direction=-1&limit={limit}")
    data = http_get(url, {"Authorization": f"Bearer {HF_TOKEN}"} if HF_TOKEN else {})
    if not data:
        return []
    return [m.get("id") for m in data if m.get("id")]


def hf_discussions(model_id, since_dt):
    """拿模型仓库的 discussions，过滤出 since 之后有活动的"""
    url = f"https://huggingface.co/api/models/{urllib.parse.quote(model_id, safe='/')}/discussions?p=0"
    data = http_get(url, {"Authorization": f"Bearer {HF_TOKEN}"} if HF_TOKEN else {})
    if not data:
        return []
    out = []
    discussions = data if isinstance(data, list) else data.get("discussions", [])
    for d in discussions:
        # HF API 字段：num, title, createdAt, updatedAt（或 lastActivityAt）
        updated = d.get("updatedAt") or d.get("lastActivityAt") or d.get("createdAt") or ""
        try:
            udt = datetime.fromisoformat(updated.replace("Z", "+00:00"))
        except Exception:
            continue
        if udt < since_dt:
            continue
        num = d.get("num")
        out.append({
            "source_type": "hf-discussion",
            "repo": model_id,
            "matched_keyword": "",
            "title": d.get("title", ""),
            "url": f"https://huggingface.co/{model_id}/discussions/{num}",
            "state": d.get("status", ""),
            "created_at": (d.get("createdAt") or "")[:10],
            "updated_at": updated[:10],
            "comments": d.get("numComments", 0),
            "snippet": "",
        })
    return out


def fetch_hf(cfg, since_dt, seen):
    cands = []
    orgs = [o["hf_author"] for o in cfg.get("official_orgs", [])] + \
           [o["hf_author"] for o in cfg.get("quant_orgs", [])]
    for author in orgs:
        models = hf_recent_models(author, limit=30)
        print(f"[HF] {author}: {len(models)} 个近期模型")
        for mid in models:
            for r in hf_discussions(mid, since_dt):
                if r["url"] not in seen:
                    cands.append(r)
                    seen.add(r["url"])
            time.sleep(0.5)
    print(f"[HF] 新增候选 {len(cands)} 条")
    return cands


# ---------- main ----------

def main():
    backfill = "--backfill" in sys.argv
    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))

    now = datetime.now(timezone.utc)
    if backfill:
        days = int(cfg.get("backfill_days", 3))
    else:
        days = 1
    since_dt = now - timedelta(days=days)
    since_iso = since_dt.strftime("%Y-%m-%d")
    print(f"爬取窗口: since {since_iso} ({'backfill' if backfill else 'daily'})")

    seen = load_seen()
    print(f"已见 URL 数: {len(seen)}")

    cands = []
    try:
        cands += fetch_hf(cfg, since_dt, seen)
    except Exception as e:
        print(f"[HF] 爬取中止（不影响 GitHub）：{e}", file=sys.stderr)
    try:
        cands += fetch_github(cfg, since_iso, seen)
    except Exception as e:
        print(f"[GitHub] 爬取中止：{e}", file=sys.stderr)

    save_seen(seen)

    CANDIDATES_DIR.mkdir(exist_ok=True)
    out_path = CANDIDATES_DIR / f"candidates-{now.strftime('%Y-%m-%d')}.json"
    payload = {
        "generated_at": now.isoformat(),
        "since": since_iso,
        "count": len(cands),
        "candidates": cands,
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"输出: {out_path} ({len(cands)} 条候选)")


if __name__ == "__main__":
    main()
