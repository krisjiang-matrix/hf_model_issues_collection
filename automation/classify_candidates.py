#!/usr/bin/env python3
"""
HF 模型问题速查 - LLM 候选分类 + 入库
=====================================
读取 automation/candidates/candidates-*.json 的候选，
调用本地 vLLM/qwen3.8 (或远端兼容 API) 逐条判定：
  - is_bug:     是否为真实模型/引擎问题
  - verdict:    归因（模型本身/量化问题/引擎问题/组合问题/环境问题）
  - category:   症状分类
  - model:      涉及的模型名
  - confidence: 判定置信度
  - brief:      一句话根因

质量门控（自动入库但防污染主库）：
  - is_bug=true 且 verdict 有效 且 confidence=high  → 自动写入 data/<target>.json
  - 其余（is_bug=false / medium-low / JSON 解析失败）→ 落 automation/review/ 待人工，
    不进主库。库的价值在判得准不在全。

去重：按候选 url 在现有 data/*.json 的 sources[].url 中查找，命中则跳过。

用法：
  python classify_candidates.py                       # 处理最新候选文件
  python classify_candidates.py candidates-YYYY-MM-DD.json   # 指定文件
  LLM_BASE_URL=xxx python classify_candidates.py      # 覆盖 LLM 端点（默认本地 qwen）
"""
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

BASE = Path(__file__).parent
DATA_DIR = BASE.parent / "data"
CANDIDATES_DIR = BASE / "candidates"
REVIEW_DIR = BASE / "review"

LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "http://10.202.5.241:8001/v1/chat/completions")
LLM_MODEL = os.environ.get("LLM_MODEL", "qwen3.8")

# 模型名 → 主库文件 的路由（确定性第一层）
ROUTE_RULES = [
    (("Qwen", "QwenLM", "unsloth/Qwen"), "qwen.json"),
]
# 量化/其他厂前缀→family 映射，按子串匹配 repo
REPO_FAMILY = [
    ("Qwen", "Qwen"),
    ("QwenLM", "Qwen"),
    ("deepseek", "DeepSeek"),
    ("zai-org", "GLM"),
    ("GLM", "GLM"),
    ("moonshot", "Kimi"),
    ("MiniMax", "MiniMax"),
    ("meta-llama", "Llama"),
    ("google", "Gemma"),
    ("gemma", "Gemma"),
    ("mistral", "Mistral"),
    ("openai", "OpenAI"),
    ("cohere", "Cohere"),
    ("microsoft", "Phi"),
    ("Phi", "Phi"),
    ("grok", "Grok"),
    ("x-ai", "Grok"),
]
# family → 目标文件
FAMILY_FILE = {
    "Qwen": "qwen.json",
    "DeepSeek": "cn-models.json",
    "GLM": "cn-models.json",
    "Kimi": "cn-models.json",
    "MiniMax": "cn-models.json",
    "混元": "cn-models.json",
    "ERNIE": "cn-models.json",
    "多厂商": "cn-models.json",
    "Llama": "intl-models.json",
    "Gemma": "intl-models.json",
    "Mistral": "intl-models.json",
    "OpenAI": "intl-models.json",
    "Cohere": "intl-models.json",
    "Phi": "intl-models.json",
    "Grok": "intl-models.json",
}
# 引擎/框架仓库 → 一般归 general（引擎层通用问题），除非明确对单一模型
ENGINE_REPOS = {"vllm-project", "sgl-project", "ggml-org", "ollama",
                "huggingface", "ml-explore", "opencode-ai", "cline",
                "RooCodeInc", "continuedev", "openhands"}
# family 白名单——LLM 输出的 model 若含这些则覆盖
MODEL_HINT_FAMILY = {
    "qwen": "Qwen", "deepseek": "DeepSeek", "glm": "GLM", "zhipu": "GLM",
    "kimi": "Kimi", "moonshot": "Kimi", "minimax": "MiniMax", "hunyuan": "混元",
    "ernie": "ERNIE", "llama": "Llama", "gemma": "Gemma", "mistral": "Mistral",
    "gpt-oss": "OpenAI", "cohere": "Cohere", "phi": "Phi", "grok": "Grok",
}
# 文件名 → id 前缀
FNAME_PREFIX = {
    "qwen.json": "qwen", "cn-models.json": "cn",
    "intl-models.json": "intl", "general.json": "gen",
}

VERDICTS = {"模型本身", "量化问题", "引擎问题", "组合问题", "环境问题"}


# ---------- 路由 ----------

def repo_to_family(repo):
    """从 repo id 推断 model family；引擎仓库返回 None 让 LLM model 决定"""
    low = repo.lower()
    owner = repo.split("/")[0].lower() if "/" in repo else low
    if owner in ENGINE_REPOS:
        return None
    for sub, fam in REPO_FAMILY:
        if sub.lower() in low:
            return fam
    return None


# 引擎/框架名——model 若只是引擎名（无具体模型），不应归到模型家族
ENGINE_NAMES = {"vllm", "vllm-project", "sglang", "sgl", "llama.cpp", "llama-cpp",
                "ollama", "transformers", "mlx", "mlx-lm", "opencode", "cline",
                "roo-code", "continue", "openhands"}


def model_to_family(model):
    """从模型名推断 family。若 model 是引擎/框架名（含描述）则返回 None（→ general）。
    仅当出现具体模型家族名（如 Llama-4、GLM-5）才归属家族。"""
    if not model:
        return None
    low = model.lower()
    # 1) 明确含引擎产品名（子串）→ 归引擎，除非同时出现具体模型型号
    for e in ("ollama", "vllm", "sglang", "llama.cpp", "llama-cpp", "transformers",
              "mlx-lm", "mlx", "opencode", "roo-code", "openhands", "cline"):
        if e in low:
            # 例外：含具体模型家族型号（如 vllm 跑 Qwen3 的标题，model 字段一般只填引擎名）
            return None
    # 2) 走模型家族子串
    for sub, fam in MODEL_HINT_FAMILY.items():
        if sub in low:
            return fam
    return None


def resolve_family(cand, llm_model):
    """repo 路由优先；若为引擎仓库则用 LLM 返回的 model 名判定。仍无法判定→general"""
    fam = repo_to_family(cand.get("repo", ""))
    if fam:
        return fam
    mfam = model_to_family(llm_model)
    return mfam if mfam else "通用"


# ---------- LLM 调用 ----------

def llm_classify(cand):
    """调用 LLM 判定单条候选，返回 dict 或 None(解析失败)"""
    title = cand.get("title", "")[:200]
    body = cand.get("snippet", "")[:800]
    repo = cand.get("repo", "")
    sys_p = (
        "你是开源模型/推理引擎问题分析器。给定一条 issue/讨论，判断它是否属于真实、可归因的"
        "模型/量化/引擎技术问题。规则："
        "1) 先简短思考，但最终必须在消息末尾输出唯一一个 JSON 对象，用 ```json 围栏包裹，"
        "围栏外不要有任何其他文字。"
        "2) 闲聊、功能请求、'什么时候支持'、benchmark 结果分享、用法咨询、模型同名但无关内容"
        "一律 is_bug=false。"
        "3) 若是真 bug 且能定位归属才 is_bug=true。verdict 取值仅限：模型本身/量化问题/"
        "引擎问题/组合问题/环境问题。"
        "4) category 取值仅限：循环重复/加载失败/输出错误/性能异常/内存泄漏/工具调用/多模态/其他。"
        "5) model 填涉及的具体模型名（如 Qwen3.5-32B），引擎层通用 bug 填引擎名(如 vLLM)。"
        "6) brief 用一句 40 字内中文概述根因。confidence: 根因明确=high，推测=medium，很模糊=low。"
        "输出 JSON 结构固定："
        '{"is_bug":bool,"verdict":"","category":"","model":"","confidence":"","brief":""}'
    )
    user_p = (
        f"REPO: {repo}\n"
        f"TITLE: {title}\n"
        f"BODY: {body}\n\n"
        "请判定并输出 JSON。"
    )
    payload = {
        "model": LLM_MODEL,
        "messages": [{"role": "system", "content": sys_p},
                     {"role": "user", "content": user_p}],
        "max_tokens": 4000,   # qwen3.8 强制思考，reasoning 可能吃 1500+ token；必须给足否则截断 content 为空
        "temperature": 0.1,
    }
    req = urllib.request.Request(
        LLM_BASE_URL, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        d = json.loads(r.read().decode("utf-8"))
    content = d["choices"][0]["message"].get("content", "")
    return _extract_json(content)


def _extract_json(text):
    """从 LLM 输出里抽出最后的 JSON 对象（容忍围栏/前后文）。失败返回 None"""
    if not text:
        return None
    # 优先取 ```json 围栏
    m = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    if not m:
        m = re.search(r"```\s*(\{.*?\})\s*```", text, re.DOTALL)
    if not m:
        m = re.search(r"(\{.*\})", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def validate(j):
    """校验/清洗 LLM 输出字段，返回 (is_bug, verdict, category, model, confidence, brief)"""
    if not isinstance(j, dict):
        return None
    is_bug = bool(j.get("is_bug"))
    verdict = j.get("verdict", "")
    if verdict not in VERDICTS:
        verdict = "其他"
    category = j.get("category", "") or "其他"
    model = (j.get("model", "") or "").strip()
    conf = j.get("confidence", "")
    if conf not in {"high", "medium", "low"}:
        conf = "medium"
    brief = (j.get("brief", "") or "").strip()
    return is_bug, verdict, category, model, conf, brief


# ---------- 现有主库加载/去重 ----------

def load_all_existing():
    """读 data/*.json，返回 {url: True} 与各文件现有 id 列表"""
    seen_urls = {}
    id_nums = {}
    for fp in DATA_DIR.glob("*.json"):
        key = fp.name
        id_nums[key] = []
        data = json.loads(fp.read_text(encoding="utf-8"))
        for item in data:
            id_nums[key].append(item.get("id", ""))
            for s in item.get("sources", []):
                if s.get("url"):
                    seen_urls[s["url"]] = True
    return seen_urls, id_nums


def next_id(fname, prefix):
    """为指定文件生成下一个 id，如 qwen-021。进程内自增，避免重复。"""
    # 惰性初始化各文件的计数器：记录已见过的最高序号，供进程内 alloc
    if not hasattr(next_id, "_max_seen"):
        next_id._max_seen = {}
        next_id._taken = {}
    cur = next_id._taken.get(fname, 0)
    if cur == 0:
        # 首启：读主库当前最大序号 + 追溯本进程 dry-run/live 已写入磁盘的最新
        existing = load_all_existing()[1].get(fname, [])
        nums = []
        for i in existing:
            m = re.match(rf"^{re.escape(prefix)}-(\d+)$", i)
            if m:
                nums.append(int(m.group(1)))
        # 也纳入 seen_urls 无关；但主库可能已被本进程 append，直接读最新
        if (DATA_DIR / fname).exists():
            for it in json.loads((DATA_DIR / fname).read_text(encoding="utf-8")):
                m = re.match(rf"^{re.escape(prefix)}-(\d+)$", it.get("id", ""))
                if m:
                    nums.append(int(m.group(1)))
        cur = (max(nums) + 1 if nums else 1) - 1
    cur += 1
    next_id._taken[fname] = cur
    return f"{prefix}-{cur:03d}"


def append_entry(fname, entry):
    """把新条目追加到 data/<fname>，保持升序 id"""
    path = DATA_DIR / fname
    data = json.loads(path.read_text(encoding="utf-8"))
    data.append(entry)
    data.sort(key=lambda x: x.get("id", ""))
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return fname


def _norm_url(u):
    return u.rstrip("/").lower()


def build_entry(cand, verdict, category, model, conf, brief, family, fname):
    """构造网站 schema 完整条目"""
    now = time.strftime("%Y-%m-%d")
    url = cand.get("url", "")
    quant = ""
    repo = cand.get("repo", "")
    # 粗判量化：repo 属量化 org 或 model 名带 GGUF/AWQ/量化后缀
    q_orgs = {"unsloth", "bartowski", "RedHatAI", "AesSedai", "cyankiwi", "inferRouter"}
    owner = repo.split("/")[0].lower()
    if owner in q_orgs or re.search(r"GGUF|AWQ|GPTQ|int4|int8|nf4|fp4|nvfp4", model, re.I):
        quant = owner if owner in q_orgs else "量化"
    prefix = FNAME_PREFIX[fname]
    return {
        "id": next_id(fname, prefix),
        "model": model,
        "variant": f"{repo}",
        "family": family,
        "quant": quant,
        "engine": cand.get("engine", "") or "",
        "engineVersion": "",
        "category": category,
        "symptom": brief,
        "rootCause": "",
        "workaround": "",
        "status": "待核验",
        "fixedIn": "",
        "severity": "unknown",
        "verdict": verdict,
        "confidence": conf,
        "date": (cand.get("created_at") or now)[:7],
        "sources": [{
            "title": cand.get("title", ""),
            "url": url,
            "type": cand.get("source_type", ""),
            "date": (cand.get("created_at") or "")[:10],
        }],
        "created_at": now,
        "updated_at": now,
        "_auto": True,  # 标记为自动入库，便于识别（前端忽略此字段）
    }


# ---------- main ----------

def main():
    REVIEW_DIR.mkdir(exist_ok=True)
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    args = [a for a in args if not a.startswith("--")]
    target = args[0] if args and args[0].endswith(".json") else None
    cand_files = sorted(CANDIDATES_DIR.glob("candidates-*.json"))
    if not cand_files:
        print("没有候选文件", file=sys.stderr)
        return
    target_file = None
    if target:
        p = CANDIDATES_DIR / target
        if not p.exists():
            print(f"候选文件不存在: {p}", file=sys.stderr)
            return
        target_file = p
    else:
        target_file = cand_files[-1]
    payload = json.loads(target_file.read_text(encoding="utf-8"))
    cands = payload.get("candidates", [])
    print(f"处理 {target_file.name}: {len(cands)} 条候选")
    seen_urls, _ = load_all_existing()
    print(f"主库已有 source url: {len(seen_urls)}")

    auto_added = []
    review_items = []
    dedup_skipped = 0
    for i, cand in enumerate(cands, 1):
        url = cand.get("url", "")
        if url and _norm_url(url) in seen_urls:
            dedup_skipped += 1
            print(f"[{i}/{len(cands)}] 跳过(已入库): {cand.get('title','')[:50]}")
            continue
        print(f"[{i}/{len(cands)}] 分类中: {cand.get('title','')[:50]} ...")
        try:
            j = llm_classify(cand)
        except Exception as e:
            print(f"    LLM 调用失败: {e}", file=sys.stderr)
            review_items.append({"candidate": cand, "auto": False,
                                 "reason": f"LLM error: {e}"})
            time.sleep(1)
            continue
        parsed = validate(j) if j else None
        if parsed is None:
            review_items.append({"candidate": cand, "auto": False,
                                 "reason": "JSON 解析失败"})
            print("    判定解析失败 → review")
            continue
        is_bug, verdict, category, model, conf, brief = parsed
        family = resolve_family(cand, model)
        fname = FAMILY_FILE.get(family, "general.json")
        # engine 层且无法归属具体模型的，落 general（family=通用）
        if repo_to_family(cand.get("repo", "")) is None and model_to_family(model) is None:
            family = "通用"
            fname = "general.json"
        entry = build_entry(cand, verdict, category, model, conf, brief,
                            family, fname)
        qualify = is_bug and conf == "high"
        if not dry_run and qualify:
            append_entry(fname, entry)
            seen_urls[_norm_url(url)] = True
            auto_added.append((fname, entry["id"], verdict, model))
            print(f"    ✅ 自动入库 {fname} [{entry['id']}] {verdict} conf={conf}")
        else:
            reason = "DRY-RUN(qualified)" if (dry_run and qualify) else \
                f"is_bug={is_bug} conf={conf} verdict={verdict}"
            review_items.append({
                "candidate": cand,
                "auto": qualify,
                "reason": reason,
                "target_file": fname,
                "entry": entry if dry_run else None,
                "llm": {"is_bug": is_bug, "verdict": verdict, "category": category,
                        "model": model, "confidence": conf, "brief": brief},
            })
            tag = "qualified" if qualify else "not-qualified"
            print(f"    → review[{tag}] is_bug={is_bug} conf={conf} fname={fname}")
        time.sleep(0.5)

    # 汇总输出
    summary = {
        "processed_file": target_file.name,
        "mode": "dry-run" if dry_run else "live",
        "auto_added": auto_added,
        "auto_count": len(auto_added),
        "dedup_skipped": dedup_skipped,
        "review_count": len(review_items),
    }
    rev_path = REVIEW_DIR / f"review-{target_file.name.replace('candidates-','').replace('.json','')}.json"
    rev_path.write_text(json.dumps(review_items, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n=== 汇总 ===")
    print(f"模式: {'DRY-RUN(未写库)' if dry_run else 'LIVE'}")
    print(f"自动入库(或符合入库资格): {len(auto_added) + sum(1 for r in review_items if r.get('auto') and dry_run)}")
    print(f"去重跳过: {dedup_skipped}")
    print(f"review 明细: {len(review_items)} → {rev_path}")
    out = {"auto_added": auto_added}
    (REVIEW_DIR / f"summary-{time.strftime('%Y%m%d')}.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
