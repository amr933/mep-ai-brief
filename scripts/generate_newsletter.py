#!/usr/bin/env python3
"""MEP Newsletter — daily AI-MEP news engine.

Produces, per run:
  1. site/index.html         — the latest issue (web edition)
  2. site/archive/<date>.html— permanent archive page
  3. telegram message        — sent to the channel/chat
Everything is driven by one pipeline: search -> editorial LLM -> format.
"""
import os
import sys
import json
import re
import time
import subprocess
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta


# ---------------------------------------------------------------- credentials
def _load_env_key(name):
    home = os.environ.get("HOME") or os.path.expanduser("~")
    env_path = os.path.join(home, "AppData", "Local", "hermes", ".env")
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                if line.startswith(name + "="):
                    v = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if v:
                        return v
    return os.environ.get(name, "")


BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or "8811437259:AAEkfiT-v3alMzM4H5jL_er9tGsU26wruOM"
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID") or "7758983309"
# Web edition URL (used for analytics + "read full issue" link). Set via env/secret.
WEB_URL = os.getenv("MEP_WEB_URL", "https://amr933.github.io/mep-ai-brief/").rstrip("/")
# GoatCounter analytics code (e.g. "my-site" for my-site.goatcounter.com). Empty = no tracking.
GOATCOUNTER_CODE = os.getenv("GOATCOUNTER_CODE", "")
LLM_API_KEY = _load_env_key("HERMES_CUSTOM_ATRIA_1_API_KEY") or os.getenv("MEP_LLM_API_KEY", "")
LLM_BASE_URL = os.getenv("MEP_LLM_BASE_URL", "https://api.atria-asi.ai/v1")
LLM_MODEL = os.getenv("MEP_LLM_MODEL", "Atria-Dawn-Preview")

try:
    import requests
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "requests"])
    import requests

# Directories. When run from the repo root, everything lands inside ./site
SITE_DIR = os.path.join(os.getcwd(), "site")
ARCHIVE_DIR = os.path.join(SITE_DIR, "archive")
STATE_FILE = os.path.join(
    os.environ.get("HOME") or os.path.expanduser("~"),
    "AppData", "Local", "hermes", "mep_newsletter_state.json",
)


def next_issue():
    n = 0
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, encoding="utf-8") as f:
                n = int(json.load(f).get("issue", 0))
    except Exception:
        n = 0
    n += 1
    try:
        d = os.path.dirname(STATE_FILE)
        if not os.path.isdir(d):
            os.makedirs(d)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"issue": n, "last_run": datetime.now().isoformat()}, f)
    except Exception:
        pass
    return n


# ---------------------------------------------------------------- search layer
SECTIONS = [
    ("hvac", "❄️ التكييف والتهوية", "AI HVAC OR \"artificial intelligence\" air conditioning heating ventilation"),
    ("fire", "🔥 مكافحة الحريق", "AI fire detection OR fire suppression OR firefighting technology"),
    ("plumb", "🚰 الأعمال الصحية", "AI water management OR leak detection OR smart plumbing"),
    ("medical", "🏥 الغازات الطبية", "AI medical gas OR hospital pipeline monitoring OR healthcare HVAC"),
]

DOMAIN_RE = re.compile(r"https?://([^/]+)")


def _google_news_search(query, limit=8, max_days_old=45):
    """Google News RSS feed — no API key needed, works on any server."""
    import html as _html
    import time as _time
    q = urllib.parse.quote_plus(query)
    url = f"https://news.google.com/rss/search?q={q}&hl=en&gl=US&ceid=US:en"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode("utf-8", errors="ignore")
    except Exception as e:
        print(f"[search] google news failed: {e}", file=sys.stderr)
        return []

    items = re.findall(r"<item>(.*?)</item>", body, re.S)
    out = []
    now = _time.time()
    for it in items:
        if len(out) >= limit:
            break
        t = re.search(r"<title>(.*?)</title>", it, re.S)
        l = re.search(r"<link>(.*?)</link>", it, re.S)
        d = re.search(r"<pubDate>(.*?)</pubDate>", it, re.S)
        s = re.search(r"<description>(.*?)</description>", it, re.S)
        if not (t and l):
            continue
        title = _html.unescape(t.group(1)).strip()
        link = _html.unescape(l.group(1)).strip()
        # strip the trailing " - Source Name" from Google News titles
        if " - " in title:
            title = re.sub(r"\s+-\s+[^-]+$", "", title).strip()
        desc = _html.unescape(s.group(1)).strip() if s else ""
        if desc.startswith("<"):
            desc = re.sub(r"<[^>]+>", " ", desc).strip()
        # age filter
        if d:
            try:
                dt = datetime.strptime(d.group(1).strip(), "%a, %d %b %Y %H:%M:%S %Z")
                age_days = (now - dt.timestamp()) / 86400
                if age_days > max_days_old:
                    continue
            except Exception:
                pass
        if title and link:
            out.append({"title": title, "url": link, "description": desc})
    return out


def _ddg_search(query, limit=5):
    """DuckDuckGo Lite HTML results."""
    import html as _html
    try:
        resp = requests.post(
            "https://html.duckduckgo.com/html/",
            data={"q": query},
            timeout=20,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        page = resp.text
    except Exception as e:
        print(f"[search] duckduckgo failed: {e}", file=sys.stderr)
        return []

    results = []
    anchors = re.findall(
        r'<a[^>]*class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', page
    )
    snips = re.findall(r'<a[^>]*class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>', page)
    for i, (href, t) in enumerate(anchors[:limit]):
        title = _html.unescape(re.sub(r"<[^>]+>", "", t)).strip()
        desc = ""
        if i < len(snips):
            desc = _html.unescape(re.sub(r"<[^>]+>", "", snips[i])).strip()
        if not title:
            continue
        url = href
        m = re.search(r"uddg=([^&]+)", href)
        if m:
            try:
                from urllib.parse import unquote
                url = unquote(m.group(1))
            except Exception:
                pass
        if url.startswith("//"):
            url = "https:" + url
        results.append({"title": title, "url": url, "description": desc})
    return results


def web_search(query, limit=5):
    """Search the web. Order: Google News RSS -> Hermes tool -> DuckDuckGo HTML."""
    items = _google_news_search(query, limit=limit)
    if items:
        return items
    try:
        from hermes_tools import web_search as _ws
        res = _ws(query, limit)
        if isinstance(res, dict):
            items = res.get("data", {}).get("web", []) or []
        elif isinstance(res, list):
            items = res
        if items:
            return items
    except Exception:
        pass
    return _ddg_search(query, limit)


def fetch_raw_items():
    raw = []
    for key, label, query in SECTIONS:
        try:
            items = web_search(query, limit=5)
            if not isinstance(items, list):
                items = []
        except Exception as e:
            print(f"[search] {key} failed: {e}", file=sys.stderr)
            items = []
        for i, it in enumerate(items[:6]):
            title = (it.get("title") or "").strip()
            url = (it.get("url") or "").strip()
            desc = (it.get("description") or "").strip()
            if not title or not url:
                continue
            m = DOMAIN_RE.match(url)
            source = m.group(1) if m else ""
            raw.append({"section": key, "title": title, "url": url, "desc": desc[:200],
                        "source": source})
    # dedupe by URL
    seen = set()
    out = []
    for it in raw:
        if it["url"] in seen:
            continue
        seen.add(it["url"])
        out.append(it)
    return out


# ---------------------------------------------------------------- editorial LLM
EDITOR_PROMPT = """أنت محرّر هندسي محترف لنشرة "MEP Newsletter" العربية اليومية المتخصصة في تطبيقات الذكاء الاصطناعي في هندسة التكييف (HVAC) ومكافحة الحريق والأعمال الصحية والغازات الطبية.

ستصلك مجموعة من الأخبار المرشحة مقسمة على 4 أقسام. لكل قسم، اكتب بطاقة خبر واحدة لكل عنوان مرشح.

قواعد صارمة:
- لا تتجاهل أي قسم من الأقسام الأربعة، ولا تترك أي قسم فارغاً.
- اكتب بطاقة لكل عنوان مرشح (لا تدمج ولا تحذف).
- إذا لم يكن هناك وصف، اعتمد على العنوان فقط.
- حافظ على الرابط الأصلي كما هو دون تعديل.
- أي سطر خارج التنسيق التالي سيُعتبر خطأ:

[SECTION_LABEL]
القسم: <اسم القسم كما هو مكتوب>
العنوان: <عنوان عربي واضح>
الملخص: <سطرين بالعربية>
لماذا يهمك: <سطر واحد>
الرابط: <الرابط الأصلي>
[/SECTION_LABEL]
"""


def editorial_pass(raw_items):
    if not raw_items:
        return []

    lines = []
    for key, label, _ in SECTIONS:
        items = [i for i in raw_items if i["section"] == key]
        if not items:
            continue
        lines.append(f"=== القسم: {label} ===")
        for i, it in enumerate(items, 1):
            lines.append(f"{i}. العنوان: {it['title']}")
            lines.append(f"   الرابط: {it['url']}")
            if it.get("desc"):
                lines.append(f"   الوصف: {it['desc'][:300]}")
        lines.append("")

    candidates = "\n".join(lines)
    prompt = EDITOR_PROMPT + "\nالأخبار المرشحة:\n\n" + candidates

    # Ask for a compact, predictable JSON-ish stream we can count on
    # NOTE: Atria-Dawn-Preview is a reasoning model: reasoning_tokens come out of the
    # same max_tokens budget BEFORE content. A small budget yields finish_reason=length
    # with content=None. Keep the budget generous.
    payload = json.dumps({
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": "أنت محرر تقني عربي محترف. اتبع التعليمات حرفياً."},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 20000,
        "temperature": 0.2,
    }).encode("utf-8")

    last_err = None
    text = ""
    for attempt in range(6):
        try:
            req = urllib.request.Request(
                f"{LLM_BASE_URL}/chat/completions",
                data=payload,
                headers={
                    "Authorization": f"Bearer {LLM_API_KEY}",
                    "Content-Type": "application/json",
                    "User-Agent": "curl/8.7.1",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=900) as r:
                resp = json.load(r)
            # tolerate providers that return content in different shapes
            choice = resp.get("choices", [{}])[0]
            raw = (choice.get("message") or {}).get("content")
            # Atria is a reasoning model: if content is empty but reasoning exists,
            # the reasoning budget ate the whole max_tokens. Retry with more.
            if not raw:
                rc = (choice.get("message") or {}).get("reasoning_content")
                if rc:
                    print(f"[mep] LLM produced {len(rc)} reasoning chars but no content "
                          f"(finish={choice.get('finish_reason')}); retrying", file=sys.stderr, flush=True)
                raise ValueError("empty content from LLM")
            text = raw.strip()
            last_err = None
            break
        except Exception as e:
            last_err = e
            wait = 8 * (attempt + 1)
            print(f"[mep] LLM attempt {attempt+1} failed: {e} (retrying in {wait}s)",
                  file=sys.stderr, flush=True)
            time.sleep(wait)
    if last_err is not None:
        print(f"[mep] editorial LLM failed after retries: {last_err}", file=sys.stderr, flush=True)
        return []

    if text.startswith("```"):
        text = "\n".join(l for l in text.splitlines() if not l.startswith("```")).strip()

    items = []
    current = None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("[SECTION_LABEL]"):
            current = {}
            items.append(current)
        elif s.startswith("[/SECTION_LABEL]"):
            current = None
        elif current is not None and ":" in s:
            k, v = s.split(":", 1)
            current[k.strip()] = v.strip()
    return items


# ---------------------------------------------------------------- telegram
def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"}
    resp = requests.post(url, json=payload, timeout=30)
    resp.raise_for_status()
    return resp.json()


def send_in_chunks(text, max_len=3500):
    if len(text) <= max_len:
        return [send_telegram_message(text)]

    parts, buf, size = [], [], 0
    for line in text.splitlines():
        add = len(line) + 1
        if size + add > max_len and buf:
            parts.append("\n".join(buf))
            buf, size = [], 0
        buf.append(line)
        size += add
    if buf:
        parts.append("\n".join(buf))

    results = []
    for i, part in enumerate(parts, 1):
        if i > 1:
            part = f"*(تابع العدد {i})*\n\n" + part
        results.append(send_telegram_message(part))
    return results


# ---------------------------------------------------------------- formatting
def esc_md(s):
    return (s.replace("*", "")
             .replace("_", " ")
             .replace("`", "'")
             .replace("[", "(")
             .replace("]", ")"))


def esc_html(s):
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;"))


def build_telegram_message(issue, news):
    today = datetime.now(timezone(timedelta(hours=3)))
    lines = []
    lines.append("📡 *MEP Newsletter*")
    lines.append(f"العدد رقم {issue} • {today.strftime('%Y-%m-%d')}")
    lines.append("")
    lines.append("ملخص يومي لأحدث تطبيقات الذكاء الاصطناعي في هندسة التكييف ومكافحة الحريق والأعمال الصحية والغازات الطبية — مختاراً ومراجعاً هندسياً.")
    lines.append("")
    lines.append("━" * 18)

    for key, label, _ in SECTIONS:
        section_items = [n for n in news if n.get("القسم", "").strip() == label]
        if not section_items:
            continue
        lines.append("")
        lines.append(f"*{label}*")
        lines.append("")
        for n in section_items:
            title = esc_md(n.get("العنوان", "بدون عنوان"))
            summary = esc_md(n.get("الملخص", ""))
            why = esc_md(n.get("لماذا يهمك", ""))
            link = (n.get("الرابط") or "").strip()
            lines.append(f"▸ *{title}*")
            if summary:
                lines.append(summary)
            if why:
                lines.append(f"_💡 لماذا يهمك: {why}_")
            if link:
                lines.append(f"[🔗 المصدر]({link})")
            lines.append("")

    lines.append("━" * 18)
    lines.append("")
    lines.append("MEP Newsletter — نشرة هندسية يومية")
    lines.append(f"🌐 اقرأ النشرة كاملة: {WEB_URL}")
    lines.append("للاشتراك أو الاقتراحات: تواصل معنا")
    return "\n".join(lines)


# ---------------------------------------------------------------- web edition
CSS = """
@import url('https://fonts.googleapis.com/css2?family=Cairo:wght@400;600;700;900&display=swap');

* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: 'Cairo', 'Segoe UI', 'Tahoma', sans-serif;
  background: #f4f6f8;
  color: #1a202c;
  direction: rtl;
  line-height: 1.8;
}
.wrap { max-width: 820px; margin: 0 auto; padding: 24px 16px 80px; }
header {
  background: linear-gradient(135deg, #0f4c5c 0%, #2a6f97 100%);
  color: #fff; border-radius: 16px; padding: 28px 24px; margin-bottom: 24px;
  box-shadow: 0 6px 24px rgba(15,76,92,.18);
}
header h1 { font-size: 26px; margin-bottom: 6px; }
header .sub { opacity: .9; font-size: 14px; }
header .issue { display: inline-block; margin-top: 12px; background: rgba(255,255,255,.16);
  padding: 4px 14px; border-radius: 999px; font-size: 13px; }
header .links { margin-top: 14px; font-size: 13px; opacity: .95; }
header .links a { color: #fff; text-decoration: underline; }
.intro { background: #fff; border-radius: 12px; padding: 16px 18px; margin-bottom: 22px;
  color: #4a5568; font-size: 15px; border: 1px solid #e2e8f0; }
section { margin-bottom: 30px; }
section h2 { font-size: 20px; margin-bottom: 12px; padding-bottom: 8px;
  border-bottom: 3px solid #0f4c5c; display: inline-block; }
.card { background: #fff; border-radius: 12px; padding: 18px 20px; margin-bottom: 14px;
  border: 1px solid #e2e8f0; box-shadow: 0 2px 8px rgba(0,0,0,.04); }
.card h3 { font-size: 17px; margin-bottom: 8px; color: #0f4c5c; }
.card p { font-size: 15px; color: #2d3748; margin-bottom: 10px; }
.why { background: #ebf8ff; border-right: 4px solid #2a6f97; border-radius: 8px;
  padding: 10px 14px; font-size: 14px; color: #2c5282; margin-bottom: 10px; }
.source { font-size: 13px; }
.source a { color: #2a6f97; text-decoration: none; }
.source a:hover { text-decoration: underline; }
footer { text-align: center; color: #718096; font-size: 13px; margin-top: 40px; }
footer a { color: #2a6f97; }
@media (max-width: 640px) { header h1 { font-size: 21px; } .card { padding: 14px; } }
"""

GC_SNIPPET = """
<!-- GoatCounter analytics -->
<script data-goatcounter="https://{code}.goatcounter.com/count"
        async src="//gc.zgo.at/count.js"></script>
"""


def build_web_page(issue, news, archive_links=None):
    today = datetime.now(timezone(timedelta(hours=3)))
    date_str = today.strftime("%Y-%m-%d")

    parts = []
    parts.append("<!DOCTYPE html>")
    parts.append('<html lang="ar" dir="rtl">')
    parts.append("<head>")
    parts.append('<meta charset="utf-8">')
    parts.append('<meta name="viewport" content="width=device-width, initial-scale=1">')
    parts.append(f"<title>MEP Newsletter — العدد {issue}</title>")
    parts.append(f"<style>{CSS}</style>")
    parts.append("</head>")
    if GOATCOUNTER_CODE:
        gc = GC_SNIPPET.replace("{code}", GOATCOUNTER_CODE)
        parts.insert(-1, gc)
    parts.append("<body>")
    parts.append('<div class="wrap">')

    parts.append("<header>")
    parts.append("<h1>📡 MEP Newsletter</h1>")
    parts.append('<div class="sub">ملخص يومي لتطبيقات الذكاء الاصطناعي في هندسة التكييف ومكافحة الحريق والأعمال الصحية والغازات الطبية</div>')
    parts.append(f'<span class="issue">العدد {issue} • {date_str}</span>')
    parts.append("</header>")

    parts.append('<div class="intro">')
    parts.append("هذه النشرة اليومية تقدم لك ملخصاً هندسياً محترفاً لأحدث تطبيقات الذكاء الاصطناعي ")
    parts.append("في مجالات الـ MEP، مع ذكر المصادر الأصلية لكل خبر للتعمق والرجوع إليها.")
    parts.append("</div>")

    for key, label, _ in SECTIONS:
        section_items = [n for n in news if n.get("القسم", "").strip() == label]
        if not section_items:
            continue
        parts.append("<section>")
        parts.append(f"<h2>{label}</h2>")
        for n in section_items:
            title = esc_html(n.get("العنوان", "بدون عنوان"))
            summary = esc_html(n.get("الملخص", ""))
            why = esc_html(n.get("لماذا يهمك", ""))
            link = (n.get("الرابط") or "").strip()
            source = ""
            m = DOMAIN_RE.match(link)
            if m:
                source = m.group(1)

            parts.append('<div class="card">')
            parts.append(f"<h3>{title}</h3>")
            if summary:
                parts.append(f"<p>{summary}</p>")
            if why:
                parts.append(f'<div class="why">💡 <strong>لماذا يهمك:</strong> {why}</div>')
            if link:
                parts.append(f'<div class="source">🔗 المصدر: <a href="{esc_html(link)}">{esc_html(source or link)}</a></div>')
            parts.append("</div>")
        parts.append("</section>")

    if archive_links:
        parts.append("<section>")
        parts.append("<h2>📚 الأرشيف</h2>")
        for a in archive_links[:10]:
            parts.append(f'<div class="card"><a href="{esc_html(a["url"])}">{esc_html(a["label"])}</a></div>')
        parts.append("</section>")

    parts.append("<footer>")
    parts.append("MEP Newsletter — نشرة هندسية يومية | ")
    parts.append('<a href="https://github.com/amr933/mep-ai-brief">المشروع على GitHub</a>')
    parts.append("</footer>")

    parts.append("</div>")
    parts.append("</body>")
    parts.append("</html>")

    return "\n".join(parts)


ARCHIVE_INDEX = os.path.join(SITE_DIR, "archive_index.json")


def save_web_edition(issue, news):
    """Write site/index.html + archive page + update archive index."""
    os.makedirs(SITE_DIR, exist_ok=True)
    os.makedirs(ARCHIVE_DIR, exist_ok=True)

    today = datetime.now(timezone(timedelta(hours=3)))
    date_str = today.strftime("%Y-%m-%d")

    # archive links
    archive_links = []
    if os.path.exists(ARCHIVE_INDEX):
        try:
            with open(ARCHIVE_INDEX, encoding="utf-8") as f:
                archive_links = json.load(f)
        except Exception:
            archive_links = []

    html = build_web_page(issue, news, archive_links)
    index_path = os.path.join(SITE_DIR, "index.html")
    with open(index_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[mep] web edition written: {index_path}")

    # archive copy
    safe_date = re.sub(r"[^\d-]", "", date_str)
    archive_path = os.path.join(ARCHIVE_DIR, f"{safe_date}.html")
    with open(archive_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[mep] archive copy: {archive_path}")

    # update archive index (newest first), absolute from site root
    entry = {"label": f"العدد {issue} — {date_str}", "url": f"archive/{safe_date}.html"}
    archive_links.insert(0, entry)
    archive_links = archive_links[:60]
    with open(ARCHIVE_INDEX, "w", encoding="utf-8") as f:
        json.dump(archive_links, f, ensure_ascii=False, indent=1)
    return index_path


# ---------------------------------------------------------------- main
def main():
    issue = next_issue()
    print(f"[mep] issue #{issue} starting", flush=True)

    raw = fetch_raw_items()
    print(f"[mep] {len(raw)} raw candidates", flush=True)
    if not raw:
        print("[mep] no candidates found", flush=True)
        return

    news = editorial_pass(raw)
    print(f"[mep] {len(news)} edited items", flush=True)
    if not news:
        print("[mep] editorial pass returned nothing", file=sys.stderr, flush=True)
        return

    # 1) web edition (always, even if Telegram fails)
    try:
        save_web_edition(issue, news)
    except Exception as e:
        print(f"[mep] web edition failed: {e}", file=sys.stderr, flush=True)

    # 2) telegram message
    msg = build_telegram_message(issue, news)
    print("[mep] telegram message length:", len(msg), flush=True)
    try:
        results = send_in_chunks(msg)
        print(f"[mep] sent {len(results)} message(s)", flush=True)
    except Exception as e:
        print(f"[mep] FAILED to send telegram: {e}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
