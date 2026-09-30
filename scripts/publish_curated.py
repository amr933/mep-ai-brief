#!/usr/bin/env python3
# Manual publish of a curated issue: reads site/issue_NN.json, builds index.html
# + archive copy, and commits/pushes. Used when the LLM is unreliable.
import json, os, re, sys, subprocess
from datetime import datetime, timezone, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
from generate_newsletter import build_web_page, esc_html, GOATCOUNTER_CODE  # noqa

issue_json = sys.argv[1] if len(sys.argv) > 1 else None
if not issue_json:
    print("usage: publish_curated.py site/issue_1.json")
    sys.exit(2)

data = json.load(open(issue_json, encoding="utf-8"))
issue_no = data["issue"]
items = data["items"]

# normalize to the shape build_web_page expects
news = []
for it in items:
    news.append({
        "القسم": it["section"],
        "العنوان": it["title"],
        "الملخص": it["summary"],
        "لماذا يهمك": it["why"],
        "الرابط": it["link"],
    })

today = datetime.now(timezone(timedelta(hours=3)))
date_str = today.strftime("%Y-%m-%d")
site_dir = os.path.join(BASE, "..", "site")
site_dir = os.path.abspath(site_dir)

# archive links from what exists (dict date -> (url, issue)) -> sorted list of dicts
arch_dir = os.path.join(site_dir, "archive")
archive_dict = {}
if os.path.isdir(arch_dir):
    for fname in sorted(os.listdir(arch_dir)):
        if fname.endswith(".html") and "__" in fname:
            d, n = fname.replace(".html", "").rsplit("__", 1)
            archive_dict[d] = (f"archive/{fname}", int(n))
archive_dict[date_str] = (f"archive/{date_str}__{issue_no:02d}.html", issue_no)
archive_links = [
    {"url": v[0], "label": f"العدد {v[1]} — {k}"}
    for k, v in sorted(archive_dict.items(), reverse=True)
]

html = build_web_page(issue_no, news, archive_links)

# main page
with open(os.path.join(site_dir, "index.html"), "w", encoding="utf-8") as f:
    f.write(html)

# archive copy
os.makedirs(arch_dir, exist_ok=True)
arch_name = f"{date_str}__{issue_no:02d}.html"
with open(os.path.join(arch_dir, arch_name), "w", encoding="utf-8") as f:
    f.write(html)
print(f"[curated] index.html + archive/{arch_name} written")

# archive index json
idx_path = os.path.join(site_dir, "archive_index.json")
idx = {}
if os.path.exists(idx_path):
    try:
        idx = json.load(open(idx_path, encoding="utf-8"))
    except Exception:
        idx = {}
idx[arch_name] = {"issue": issue_no, "date": date_str}
json.dump(idx, open(idx_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

# commit + push
repo = os.path.abspath(os.path.join(BASE, ".."))
subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
subprocess.run(["git", "commit", "-m", f"content: curated issue #{issue_no}"], cwd=repo, check=True)
subprocess.run(["git", "push"], cwd=repo, check=True)
print("[curated] pushed")
