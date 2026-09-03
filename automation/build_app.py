#!/usr/bin/env python3
"""
从 index.html 生成离线内嵌版 app.html
====================================
app.html = index.html 的前端代码 + 把所有 data/*.json 内嵌为 MODEL_DATA。
适用：离线/无 HTTP 服务器环境（fetch 在 file:// 不可用）。

用法：
  python automation/build_app.py
输出：app.html（勿手改此文件，只由本脚本重新生成）
"""
import json
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
IDX = BASE / "index.html"
DATA_DIR = BASE / "data"
OUT = BASE / "app.html"

# 1. 合并数据（固定顺序与 index.html loadData 一致）
ORDER = ["qwen.json", "cn-models.json", "intl-models.json", "general.json"]
merged = []
for f in ORDER:
    p = DATA_DIR / f
    if p.exists():
        merged.extend(json.loads(p.read_text(encoding="utf-8")))

data_json = json.dumps(merged, ensure_ascii=False)

html = IDX.read_text(encoding="utf-8")

# 2. 注入数据块（插到 "const NEW_DAYS" 之前）
anchor = "const NEW_DAYS = 7;"
data_block = f'<script>const MODEL_DATA={data_json};</script>\n\n'
if anchor in html:
    html = html.replace(anchor, data_block + anchor, 1)
else:
    raise SystemExit("找不到注入锚点 const NEW_DAYS")

# 3. 改 loadData：优先用内嵌 MODEL_DATA，否则回退 fetch
old_load = """async function loadData() {
  const files = ['qwen.json', 'cn-models.json', 'intl-models.json', 'general.json'];
  for (const f of files) {
    try {
      const resp = await fetch('data/' + f);
      if (resp.ok) ALL_DATA = ALL_DATA.concat(await resp.json());
    } catch(e) { console.warn('Failed to load ' + f, e); }
  }
  init();
}"""
new_load = """async function loadData() {
  if (typeof MODEL_DATA !== 'undefined' && Array.isArray(MODEL_DATA) && MODEL_DATA.length) {
    ALL_DATA = MODEL_DATA; init(); return;
  }
  const files = ['qwen.json', 'cn-models.json', 'intl-models.json', 'general.json'];
  for (const f of files) {
    try {
      const resp = await fetch('data/' + f);
      if (resp.ok) ALL_DATA = ALL_DATA.concat(await resp.json());
    } catch(e) { console.warn('Failed to load ' + f, e); }
  }
  init();
}"""
if old_load in html:
    html = html.replace(old_load, new_load, 1)
else:
    raise SystemExit("找不到 loadData 定义，请检查 index.html 是否被改动")

OUT.write_text(html, encoding="utf-8")
print(f"app.html 已生成: {len(merged)} 条数据内嵌 | {OUT}")
