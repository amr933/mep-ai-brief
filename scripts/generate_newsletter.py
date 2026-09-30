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
    """Issue number = highest archived issue + 1.

    On CI there is no state file, so the archive on disk (committed by
    previous runs) is the durable source of truth. A missing/empty archive
    means this is issue #1.
    """
    n = 0
    try:
        if os.path.isdir(ARCHIVE_DIR):
            for fname in os.listdir(ARCHIVE_DIR):
                m = ARCHIVE_FILE_RE.match(fname)
                if m:
                    n = max(n, int(m.group(1)))
    except Exception:
        n = 0
    n += 1

    # local state file still kept in sync for the standalone desktop script
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, encoding="utf-8") as f:
                local_n = int(json.load(f).get("issue", 0))
            n = max(n, local_n + 1)
    except Exception:
        pass
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

ARCHIVE_FILE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}__(\d+)\.html$")

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
EDITOR_PROMPT = """أنت محرّر هندسي محترف لنشرة "MEP Daily" العربية اليومية المتخصصة في تطبيقات الذكاء الاصطناعي في هندسة التكييف (HVAC) ومكافحة الحريق والأعمال الصحية والغازات الطبية.

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
    """Telegram = link-only: a short pointer to today's web edition.

    The full newsletter lives on the website; Telegram only announces it.
    """
    today = datetime.now(timezone(timedelta(hours=3)))
    lines = []
    lines.append("📡 *MEP Daily*")
    lines.append(f"العدد رقم {issue} • {today.strftime('%Y-%m-%d')}")
    lines.append("")
    lines.append("كل ما هو جديد في عالم الـ MEP — موجز يومي لأحدث الأخبار والتطبيقات في التكييف ومكافحة الحريق والأعمال الصحية والغازات الطبية.")
    lines.append("")
    lines.append(f"يمكنك الاطلاع على العدد {issue} من هنا:")
    lines.append(WEB_URL)
    return "\n".join(lines)


# ---------------------------------------------------------------- web edition
CSS = """
@import url('https://fonts.googleapis.com/css2?family=Cairo:wght@400;600;700;900&display=swap');

:root {
  --bg: #f0f4f7;
  --card: #ffffff;
  --ink: #17222e;
  --muted: #5d6f7f;
  --brand: #0e4d64;
  --brand2: #1b7a9e;
  --accent: #e4b04a;
  --line: #dde7ee;
  --soft: #eef6f9;
}

* { box-sizing: border-box; margin: 0; padding: 0; }
html { scroll-behavior: smooth; }
body {
  font-family: 'Cairo', 'Segoe UI', 'Tahoma', sans-serif;
  background: var(--bg);
  color: var(--ink);
  direction: rtl;
  line-height: 1.85;
}

.hero {
  background: linear-gradient(135deg, #09344a 0%, #0e4d64 55%, #1b7a9e 100%);
  color: #fff;
  padding: 40px 20px 34px;
  border-bottom: 5px solid var(--accent);
}
.hero-in { max-width: 820px; margin: 0 auto; text-align: center; }
.hero .brandline {
  display: inline-flex; align-items: center; gap: 10px;
  font-size: 15px; letter-spacing: 2px; opacity: .95; font-weight: 700;
}
.hero .brandline .dot {
  width: 10px; height: 10px; border-radius: 50%; background: var(--accent);
  box-shadow: 0 0 12px rgba(228,176,74,.9);
}
.hero h1 { font-size: 32px; margin: 12px 0 8px; font-weight: 900; }
.hero .sub { opacity: .92; font-size: 15px; max-width: 600px; margin: 0 auto; }
.hero .chips { margin-top: 16px; display: flex; flex-wrap: wrap; gap: 8px; justify-content: center; }
.hero .chip {
  background: rgba(255,255,255,.14);
  border: 1px solid rgba(255,255,255,.28);
  padding: 5px 14px; border-radius: 999px; font-size: 13px;
}
.hero .issue-line { margin-top: 18px; font-size: 14px; opacity: .85; }
.hero .issue-line b { color: var(--accent); font-size: 16px; }

.wrap { max-width: 820px; margin: 0 auto; padding: 26px 16px 70px; }

.intro {
  background: linear-gradient(180deg, #ffffff, #f2f8fb);
  border: 1px solid var(--line);
  border-radius: 14px;
  padding: 18px 20px;
  margin-bottom: 26px;
  color: var(--muted);
  font-size: 15px;
  box-shadow: 0 2px 10px rgba(14,77,100,.05);
}
.intro b { color: var(--brand); }

section { margin-bottom: 34px; }
.sec-head {
  display: flex; align-items: center; gap: 10px;
  margin-bottom: 14px;
}
.sec-head .ico {
  width: 34px; height: 34px; border-radius: 10px;
  background: var(--soft); border: 1px solid var(--line);
  display: flex; align-items: center; justify-content: center; font-size: 18px;
}
.sec-head h2 { font-size: 20px; color: var(--brand); font-weight: 800; }
.sec-head .rule { flex: 1; height: 2px; background: linear-gradient(90deg, var(--brand2), transparent); }

.card {
  background: var(--card);
  border-radius: 14px;
  padding: 20px 22px;
  margin-bottom: 14px;
  border: 1px solid var(--line);
  box-shadow: 0 3px 12px rgba(23,34,46,.05);
  transition: transform .15s ease, box-shadow .15s ease;
}
.card:hover { transform: translateY(-2px); box-shadow: 0 8px 22px rgba(14,77,100,.12); }
.card h3 { font-size: 17px; margin-bottom: 9px; color: var(--brand); line-height: 1.6; }
.card p { font-size: 15px; color: #26384a; margin-bottom: 12px; }
.why {
  background: var(--soft);
  border-right: 4px solid var(--brand2);
  border-radius: 10px;
  padding: 11px 15px;
  font-size: 14px;
  color: #24536b;
  margin-bottom: 12px;
}
.why b { color: var(--brand); }
.source { font-size: 13px; display: flex; align-items: center; gap: 7px; }
.source svg { flex: 0 0 auto; }
.source a { color: var(--brand2); text-decoration: none; font-weight: 600; }
.source a:hover { text-decoration: underline; }

.archive-list { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.archive-list a {
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: 10px;
  padding: 12px 14px;
  text-decoration: none;
  color: var(--ink);
  font-size: 14px;
  font-weight: 600;
  transition: .15s ease;
}
.archive-list a:hover { border-color: var(--brand2); color: var(--brand); }

footer {
  text-align: center;
  color: var(--muted);
  font-size: 14px;
  margin-top: 46px;
  padding-top: 22px;
  border-top: 1px solid var(--line);
}
footer .fbrand { color: var(--brand); font-weight: 800; font-size: 16px; margin-bottom: 4px; }
footer .views { margin-top: 12px; font-size: 14px; color: var(--muted); }
footer a { color: var(--brand2); text-decoration: none; }
footer a:hover { text-decoration: underline; }

@media (max-width: 640px) {
  .hero h1 { font-size: 23px; }
  .card { padding: 16px; }
  .archive-list { grid-template-columns: 1fr; }
}
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
    parts.append(f"<title>MEP Daily — العدد {issue}</title>")
    parts.append(f"<style>{CSS}</style>")
    parts.append("</head>")
    if GOATCOUNTER_CODE:
        gc = GC_SNIPPET.replace("{code}", GOATCOUNTER_CODE)
        parts.insert(-1, gc)
    parts.append("<body>")

    # ---- hero band (full-width, outside .wrap)
    parts.append('<div class="hero">')
    parts.append('<div class="hero-in">')
    parts.append('<div class="brandline"><span class="dot"></span>MEP DAILY</div>')
    parts.append("<h1>كل ما هو جديد في عالم الـ MEP</h1>")
    parts.append('<div class="sub">موجز يومي لأحدث الأخبار والتطبيقات في التكييف والتهوية ومكافحة الحريق والأعمال الصحية والغازات الطبية — مُحرَّر هندسياً باللغة العربية.</div>')
    parts.append('<div class="chips">')
    parts.append('<span class="chip">❄️ التكييف</span>')
    parts.append('<span class="chip">🔥 الحريق</span>')
    parts.append('<span class="chip">🚰 الصحي</span>')
    parts.append('<span class="chip">🏥 الغازات الطبية</span>')
    parts.append('</div>')
    parts.append(f'<div class="issue-line">العدد <b>{issue}</b> • {date_str} — يصلك كل صباح الساعة 8 بتوقيت مصر</div>')
    parts.append('</div>')
    parts.append('</div>')

    parts.append('<div class="wrap">')

    parts.append('<div class="intro">')
    parts.append("<b>لماذا هذه النشرة؟</b> لأن المهندس لا يجد وقتاً لمتابعة عشرات المصادر يومياً. نختار لك الأهم، نلخصه بدقة هندسية، ونذكر مع كل خبر مصدره الأصلي — خلال أقل من 3 دقائق.")
    parts.append("</div>")

    for key, label, _ in SECTIONS:
        section_items = [n for n in news if n.get("القسم", "").strip() == label]
        if not section_items:
            continue
        parts.append("<section>")
        parts.append('<div class="sec-head">')
        parts.append(f'<div class="ico">{label[0]}</div>')
        parts.append(f"<h2>{label[1:].strip()}</h2>")
        parts.append('<div class="rule"></div>')
        parts.append('</div>')
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
                parts.append(f'<div class="why">💡 <b>لماذا يهمك:</b> {why}</div>')
            if link:
                link_icon = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="#1b7a9e" stroke-width="2.2" stroke-linecap="round"><path d="M10 13a5 5 0 0 0 7 0l2-2a5 5 0 0 0-7-7"/><path d="M14 11a5 5 0 0 0-7 0l-2 2a5 5 0 0 0 7 7"/></svg>'
                parts.append(f'<div class="source">{link_icon}<a href="{esc_html(link)}" target="_blank" rel="noopener">{esc_html(source or link)}</a></div>')
            parts.append("</div>")
        parts.append("</section>")

    if archive_links:
        parts.append("<section>")
        parts.append('<div class="sec-head">')
        parts.append('<div class="ico">📚</div>')
        parts.append("<h2>الأرشيف</h2>")
        parts.append('<div class="rule"></div>')
        parts.append('</div>')
        parts.append('<div class="archive-list">')
        for a in archive_links[:12]:
            parts.append(f'<a href="{esc_html(a["url"])}">{esc_html(a["label"])}</a>')
        parts.append('</div>')
        parts.append("</section>")

    parts.append("<footer>")
    parts.append('<div class="fbrand">MEP Daily</div>')
    parts.append("<div>جميع الحقوق محفوظة لـ Nexus Solutions</div>")
    parts.append("</footer>")

    parts.append("</div>")
    parts.append("</body>")
    parts.append("</html>")

    return "\n".join(parts)


def save_web_edition(issue, news):
    """Write site/index.html + archive page + update archive index.

    The current issue always overwrites index.html. Archive pages are keyed by
    date AND issue number, and the archive list is rebuilt from the files on
    disk so the index can never drift from what actually exists.
    """
    os.makedirs(SITE_DIR, exist_ok=True)
    os.makedirs(ARCHIVE_DIR, exist_ok=True)

    today = datetime.now(timezone(timedelta(hours=3)))
    date_str = today.strftime("%Y-%m-%d")

    # rebuild archive links from the files that actually exist on disk
    archive_links = []
    for fname in sorted(os.listdir(ARCHIVE_DIR), reverse=True):
        if not fname.endswith(".html"):
            continue
        base = fname[:-5]  # strip .html
        if "__" in base:
            d, num = base.split("__", 1)
            label = f"العدد {int(num)} — {d}"
        else:
            label = base
        archive_links.append({"label": label, "url": f"archive/{fname}"})

    html = build_web_page(issue, news, archive_links)
    index_path = os.path.join(SITE_DIR, "index.html")
    with open(index_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[mep] web edition written: {index_path}")

    # archive copy: date + issue number. Re-running the same issue on the same
    # day just regenerates the same file — no duplicates, no collisions.
    safe_date = re.sub(r"[^\d-]", "", date_str)
    archive_name = f"{safe_date}__{issue:02d}.html"
    archive_path = os.path.join(ARCHIVE_DIR, archive_name)
    with open(archive_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[mep] archive copy: {archive_path}")

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
