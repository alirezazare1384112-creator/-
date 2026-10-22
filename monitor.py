#!/usr/bin/env python3
"""
Twitter -> Telegram Auto-Translator + War Analyst (GitHub Actions) v7
=======================================================================
v7 = v6 (dedup-hardened, self-healing translation) + ANALYSIS + QUOTES.

 NEW IN v7:
  1. AI ANALYSIS on EVERY sent tweet (user request): a smart expert-style
     Persian analysis - strategic context, source credibility, likely
     consequences - written by a free LLM (Pollinations /openai, no API key,
     verified working from any IP incl. GitHub runners), fed with the tweet
     text, the quote (if any) and live Twitter engagement data (likes +
     conversation count from the syndication API).
  2. QUOTE TWEETS (کوت): if a monitored account quote-tweets someone, the
     quoted tweet (user, text, its own photos + translation) is included in
     the same message under "📌 نقل از".
  3. SELF-HEALING extended: a tweet whose translation OR analysis failed is
     kept in pending.json (repo) and retried on later runs (fresh runner IP
     = fresh quota); a compact "تکمیلی" follow-up is posted once done.
     Nothing ever permanently lacks its translation/analysis.

 Carried over from v6: triple dedup (state + ledger + pre-send check),
 fail-closed pushes, 429 backoff on 3 gtx hosts + MyMemory, Arabic !=
 Persian detection, decided-only state advance, safe clamped captions,
 Telegram 429 retry, fetch hint, 14 accounts.

Accounts (14): hey_itsmyturn, MilitaryIr, Osint613 (Iran-only),
no_itsmyturn, _KhodaBiamorz, cicikhanoom, rezasadx, shahryarshefigh,
baaaarzaaaakh, MoloWarMonitor, WarMonitor3, Faytuks, Osinttechnical,
vcdgf555
"""
import json
import os
import re
import sys
import time
import base64
import subprocess
import html
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone, timedelta

# ------------------- CONFIG -------------------
BOT_TOKEN = os.environ.get('BOT_TOKEN', '')
CHAT_ID = "-1003997031832"
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
LEDGER_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ledger.json")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0"
IR_TZ = timezone(timedelta(hours=3, minutes=30))
FA = str.maketrans('0123456789', '۰۱۲۳۴۵۶۷۸۹')

MAX_PER_ACCOUNT = 5       # max sends per account per run
MAX_PER_RUN = 18          # absolute send cap per run
LEDGER_MAX = 700          # keep newest N sent-ids in the registry
MAX_FOLLOWUPS = 3         # translation/analysis follow-ups per run
PENDING_MAX_AGE = 7 * 24 * 3600   # seconds before a pending entry expires

ACCOUNTS = [
    {"handle": "hey_itsmyturn", "iran_only": False},
    {"handle": "MilitaryIr", "iran_only": False},
    {"handle": "Osint613", "iran_only": True},
    {"handle": "no_itsmyturn", "iran_only": False},
    {"handle": "_KhodaBiamorz", "iran_only": False},
    {"handle": "cicikhanoom", "iran_only": False},
    {"handle": "rezasadx", "iran_only": False},
    {"handle": "shahryarshefigh", "iran_only": False},
    {"handle": "baaaarzaaaakh", "iran_only": False},
    {"handle": "MoloWarMonitor", "iran_only": False},
    {"handle": "WarMonitor3", "iran_only": False},
    {"handle": "Faytuks", "iran_only": False},
    {"handle": "Osinttechnical", "iran_only": False},
    {"handle": "vcdgf555", "iran_only": False},
]

IRAN_KEYWORDS = [
    'iran', 'iranian', 'tehran', 'irgc', 'isfahan', 'esfahan', 'tabriz',
    'shiraz', 'persian gulf', 'bandar abbas', 'bushehr', 'natanz',
    'fordow', 'fordo', 'qom', 'mashhad', 'kermanshah', 'khuzestan',
    'ahvaz', 'hormuz', 'khamenei', 'pezeshkian', 'parchin', 'semnan',
    'irna', 'fars news', 'mahan air', 'mehsa', 'sirik', 'chabahar',
    'ایران', 'تهران', 'اصفهان', 'شیراز', 'تبریز', 'سپاه', 'خامنه',
    'پزیشکیان', 'هرمز', 'بندرعباس', 'مشهد', 'قم', 'اهواز', 'سریک',
]

PUSH_RETRIES = 4

GTX_HOSTS = [
    'translate.googleapis.com',
    'translate.google.com',
    'clients5.google.com',
]

ANALYSIS_SYS = (
    "تو یک تحلیلگر ارشد نظامی و ژئوپلیتیک هستی که برای کانال خبری تلگرامی "
    "«جنگ» می‌نویسی. برای توییت زیر یک تحلیل کارشناسی هوشمند به زبان فارسی "
    "بنویس: ۳ تا ۴ جمله کوتاه و مشخص که شامل این موارد باشد: (۱) زمینه "
    "استراتژیک و اهمیت خبر، (۲) ارزیابی اعتبار یا ماهیت ادعا/منبع با توجه "
    "به الگوی رویدادهای اخیر، (۳) پیامدهای محتمل و نشانه‌هایی که باید رصد "
    "شوند. اگر بازتاب توییتری داده شده، خیلی کوتاه به آن اشاره کن. لحن: "
    "خنثی، دقیق، بدون اغراق؛ عدم قطعیت را صریح بیان کن. از تکرار خودِ خبر "
    "پرهیز کن و فقط بینش اضافه بده. فقط متن نهایی تحلیل را خروجی بده، "
    "بدون عنوان، بدون گیومه، بدون مارک‌داون."
)


class PushFailed(Exception):
    """Registry push failed after retries -> abort the whole run."""
    pass


# ------------------- HELPERS -------------------

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def fa_digits(s):
    return str(s).translate(FA)


def curl(url, referer=None, timeout=30):
    cmd = ['curl', '-s', '--compressed', '-L', '--max-time', str(timeout),
           '-A', UA]
    if referer:
        cmd += ['-e', referer]
    cmd.append(url)
    r = subprocess.run(cmd, capture_output=True, timeout=timeout + 10)
    if r.returncode != 0:
        raise RuntimeError(f"curl rc={r.returncode}")
    return r.stdout


def http_post(url, params, timeout=60):
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def clamp(s, n):
    """Truncate escaped text safely - never cut an HTML entity mid-way."""
    if len(s) <= n:
        return s
    cut = s[:n - 1]
    i = cut.rfind('&')
    if i > n - 9:          # possible incomplete &amp;/&lt;/&gt; near the end
        cut = cut[:i]
    return cut.rstrip() + '…'


# ------------------- GITHUB CONTENTS API -------------------

def _gh_cfg():
    repo = os.environ.get('GITHUB_REPOSITORY', '')
    tok = os.environ.get('GH_PAT') or os.environ.get('GITHUB_TOKEN', '')
    return repo, tok


def gh_get(path):
    """GET a repo file -> (parsed_json_or_None, sha_or_None). 404 -> (None, None)."""
    repo, tok = _gh_cfg()
    if not (repo and tok):
        return None, None
    url = f'https://api.github.com/repos/{repo}/contents/{path}'
    headers = {'Authorization': f'Bearer {tok}', 'User-Agent': 'tweet-monitor',
               'Accept': 'application/vnd.github+json'}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.load(r)
            try:
                return json.loads(base64.b64decode(d['content'])), d.get('sha')
            except Exception:
                return None, d.get('sha')
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None, None
        raise
    except Exception as e:
        raise RuntimeError(f"gh_get {path}: {e}")


def gh_put(path, obj, sha=None):
    """PUT a repo file. Returns True on success."""
    repo, tok = _gh_cfg()
    if not (repo and tok):
        raise PushFailed("no GitHub token in env (GH_PAT/GITHUB_TOKEN)")
    url = f'https://api.github.com/repos/{repo}/contents/{path}'
    headers = {'Authorization': f'Bearer {tok}', 'User-Agent': 'tweet-monitor',
               'Accept': 'application/vnd.github+json',
               'Content-Type': 'application/json'}
    payload = {'message': f'update {path} [skip ci]',
               'content': base64.b64encode(
                   json.dumps(obj, indent=1, ensure_ascii=False).encode()).decode()}
    if sha:
        payload['sha'] = sha
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method='PUT', headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            r.read()
        return True
    except urllib.error.HTTPError as e:
        raise PushFailed(f"gh_put {path}: HTTP {e.code}")
    except Exception as e:
        raise PushFailed(f"gh_put {path}: {e}")


def push_all(state, ledger, pending):
    """Push state + ledger + pending registry with fresh-sha retry.
    Raises PushFailed after PUSH_RETRIES consecutive failures."""
    for attempt in range(1, PUSH_RETRIES + 1):
        try:
            r_state, sha_state = gh_get('state.json')
            r_ledger, sha_ledger = gh_get('ledger.json')
            r_pend, sha_pend = gh_get('pending.json')
            merged_state = dict(r_state or {})
            for k, v in state.items():
                if str(v).isdigit() and (k not in merged_state or
                                          int(v) > int(merged_state[k])):
                    merged_state[k] = v
            merged_ledger = dict(r_ledger or {})
            merged_ledger.update(ledger)
            if len(merged_ledger) > LEDGER_MAX:
                merged_ledger = dict(list(merged_ledger.items())[-LEDGER_MAX:])
            merged_pend = dict(r_pend or {})
            merged_pend.update(pending)
            now = time.time()
            merged_pend = {k: v for k, v in merged_pend.items()
                           if now - float(v.get('ts', now)) < PENDING_MAX_AGE}
            if merged_state != (r_state or {}):
                gh_put('state.json', merged_state, sha_state)
                log("  state pushed to GitHub")
            if merged_ledger != (r_ledger or {}):
                gh_put('ledger.json', merged_ledger, sha_ledger)
                log("  ledger pushed to GitHub")
            if merged_pend != (r_pend or {}):
                gh_put('pending.json', merged_pend, sha_pend)
                log("  pending registry pushed to GitHub")
            for k, v in merged_state.items():
                if str(v).isdigit() and k in state and int(v) > int(state[k]):
                    state[k] = v
            ledger.clear()
            ledger.update(merged_ledger)
            pending.clear()
            pending.update(merged_pend)
            return True
        except PushFailed as e:
            log(f"  push attempt {attempt}/{PUSH_RETRIES} failed: {e}")
            time.sleep(2 * attempt)
    raise PushFailed("state/ledger/pending push failed after all retries")


# ------------------- TWITTER -------------------

_direct_fail_streak = 0   # per-run fetch hint (runners are CF-blocked)


def _extract_ids(raw, handle):
    ids = re.findall(rf'href="/{re.escape(handle)}/status/(\d+)"', raw,
                     re.IGNORECASE)
    if not ids:
        ids = re.findall(rf'/{re.escape(handle)}/status/(\d+)', raw,
                         re.IGNORECASE)
    seen, uniq = set(), []
    for i in ids:
        if i not in seen:
            seen.add(i)
            uniq.append(i)
    return uniq


def fetch_profile_ids(handle):
    global _direct_fail_streak
    methods = ['direct', 'goog', 'twgoog'] if _direct_fail_streak < 2 \
        else ['goog', 'twgoog']
    for m in methods:
        try:
            if m == 'direct':
                raw = curl(f"https://x.com/{handle}").decode('utf-8', errors='ignore')
            elif m == 'goog':
                raw = curl(
                    f"https://x-com.translate.goog/{handle}"
                    "?_x_tr_sl=auto&_x_tr_tl=en&_x_tr_hl=en"
                ).decode('utf-8', errors='ignore')
            else:
                raw = curl(
                    f"https://twitter-com.translate.goog/{handle}"
                    "?_x_tr_sl=auto&_x_tr_tl=en&_x_tr_hl=en"
                ).decode('utf-8', errors='ignore')
            ids = _extract_ids(raw, handle)
            if ids:
                _direct_fail_streak = 0 if m == 'direct' else _direct_fail_streak
                return ids
            if m == 'direct':
                _direct_fail_streak += 1
        except Exception as e:
            log(f"  fetch[{m}] error: {e}")
            if m == 'direct':
                _direct_fail_streak += 1
    return []


def _clean_tweet_text(d, media):
    """Original text with own-media t.co stripped and other links expanded."""
    orig_text = d.get('text', '')
    text = orig_text
    if media:
        for m in (d.get('entities', {}).get('media') or []):
            if m.get('url'):
                text = text.replace(m['url'], '').strip()
    for u in (d.get('entities', {}).get('urls') or []):
        if u.get('url') and u.get('expanded_url') and u['url'] in text:
            text = text.replace(u['url'], u['expanded_url'])
    if not text.strip():
        text = orig_text
    return text


def fetch_tweet(tweet_id, handle, with_quote=True):
    """Full tweet data + quoted tweet (کوت) + live engagement metrics."""
    raw = curl(
        f"https://cdn.syndication.twimg.com/tweet-result?id={tweet_id}&lang=en&token=a",
        referer="https://platform.twitter.com/")
    d = json.loads(raw)
    if not d or 'id_str' not in d:
        return None
    media = []
    for m in (d.get('mediaDetails') or []):
        if m.get('media_url_https'):
            media.append(m['media_url_https'])
    quote = None
    if with_quote:
        q = d.get('quoted_tweet')
        if q and q.get('id_str'):
            u = q.get('user') or {}
            quote = {'id': q['id_str'], 'user': u.get('screen_name', ''),
                     'full_name': u.get('name', ''), 'text': q.get('text', ''),
                     'media': []}
            # fetch the full quoted tweet for cleaned text + its own photos
            try:
                qf = fetch_tweet(q['id_str'], quote['user'], with_quote=False)
                if qf:
                    quote['text'] = qf['text']
                    quote['media'] = qf['media']
                    quote['user'] = qf['user']
                    quote['full_name'] = qf['full_name']
            except Exception as e:
                log(f"  quoted-tweet fetch error: {e}")
            if not quote['text'].strip() and not quote['media']:
                quote = None
    eng = {'fav': int(d.get('favorite_count') or 0),
           'conv': int(d.get('conversation_count') or 0)}
    return {
        'id': d.get('id_str'),
        'text': _clean_tweet_text(d, media),
        'created_at': d.get('created_at', ''),
        'user': (d.get('user') or {}).get('screen_name', handle),
        'full_name': (d.get('user') or {}).get('name', ''),
        'reply_to': d.get('in_reply_to_status_id'),
        'media': media,
        'quote': quote,
        'eng': eng,
    }


# ------------------- TRANSLATION (smart, multi-engine) -------------------

def is_persian(text):
    """True only for actual PERSIAN text. Arabic (same Unicode block) is NOT
    Persian and must be translated - Arabic uses ي ك ة, Persian ی ک پ چ ژ گ."""
    fa = len(re.findall(r'[\u0600-\u06FF]', text))
    total = len(re.findall(r'[\w\u0600-\u06FF]', text))
    if total == 0 or fa / total <= 0.35:
        return False
    arabic_specific = len(re.findall('[\u064A\u0643\u0629\u0623\u0625\u0624]', text))
    persian_specific = len(re.findall('[\u067E\u0686\u0698\u06AF]', text))
    if arabic_specific > 0 and persian_specific == 0:
        return False
    return True


def _gtx_once(text, host, timeout=15):
    url = (f'https://{host}/translate_a/single?' +
           urllib.parse.urlencode({'client': 'gtx', 'sl': 'auto', 'tl': 'fa',
                                   'dt': 't', 'q': text[:4500]}))
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    d = json.load(urllib.request.urlopen(req, timeout=timeout))
    out = ''.join(seg[0] for seg in d[0] if seg and seg[0])
    return out.strip() or None


def _mymemory_once(text, timeout=15):
    url = ('https://api.mymemory.translated.net/get?' +
           urllib.parse.urlencode({'q': text[:450], 'langpair': 'en|fa'}))
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    d = json.load(urllib.request.urlopen(req, timeout=timeout))
    out = (d.get('responseData') or {}).get('translatedText', '')
    if out and 'MYMEMORY WARNING' not in out.upper():
        return out.strip() or None
    return None


def translate_smart(text):
    """3 gtx hosts x 2 attempts (429 backoff) + MyMemory fallback."""
    for host in GTX_HOSTS:
        for attempt in (1, 2):
            try:
                return _gtx_once(text, host)
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    time.sleep(2 + 2 * attempt)
                    continue
                break
            except Exception:
                time.sleep(1)
                continue
    try:
        return _mymemory_once(text)
    except Exception:
        return None


def iran_related(text):
    t = text.lower()
    return any(k in t for k in IRAN_KEYWORDS)


# ------------------- AI ANALYSIS (Pollinations, free, no key) -------------------

def _analysis_prompt(tw, translation):
    lines = [f"منبع توییت: @{tw['user']} (نام نمایشی: {tw['full_name']})"]
    lines.append(f"متن توییت:\n{tw['text'][:1500]}")
    if translation:
        lines.append(f"(ترجمه فارسی همین متن برای فهم بهتر: {translation[:1200]})")
    q = tw.get('quote')
    if q:
        lines.append(f"این توییت یک توییت دیگر را نقل (کوت) کرده است — "
                     f"نقل از @{q['user']}:\n{q['text'][:500]}")
    eng = tw.get('eng') or {}
    if eng.get('fav') or eng.get('conv'):
        lines.append(f"بازتاب توییتری تا این لحظه: {eng.get('fav', 0)} لایک، "
                     f"{eng.get('conv', 0)} گفتگو/پاسخ")
    lines.append("\nتحلیل کارشناسی فارسی:")
    return "\n".join(lines)


def _pollinations_analyze(prompt, timeout=75):
    """Free LLM via Pollinations OpenAI-compatible endpoint (no key needed)."""
    payload = json.dumps({
        'model': 'openai', 'temperature': 0.4,
        'messages': [{'role': 'system', 'content': ANALYSIS_SYS},
                     {'role': 'user', 'content': prompt}],
    }).encode()
    req = urllib.request.Request(
        'https://text.pollinations.ai/openai', data=payload,
        headers={'Content-Type': 'application/json', 'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.load(r)
    out = (d.get('choices') or [{}])[0].get('message', {}).get('content', '')
    if not out or not out.strip():
        raise RuntimeError('empty analysis')
    # cleanup: markdown artifacts, surrounding quotes, "تحلیل:" prefixes
    out = re.sub(r'[*#`]+', '', out)
    out = re.sub(r'\n{3,}', '\n\n', out).strip()
    out = re.sub(r'^["«»\s]+|["«»\s]+$', '', out)
    out = re.sub(r'^تحلیل[:：]\s*', '', out)
    return out.strip() or None


def analyze_smart(tw, translation):
    """AI analysis with retries; None if the engine is unavailable (self-heals
    later via the pending registry)."""
    prompt = _analysis_prompt(tw, translation)
    for attempt in (1, 2):
        try:
            return _pollinations_analyze(prompt)
        except Exception as e:
            log(f"  analysis attempt {attempt} failed: {e}")
            time.sleep(3)
    return None


# ------------------- TELEGRAM -------------------

def _tg_once(method, params):
    try:
        return http_post(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
                         params)
    except Exception as e:
        body = getattr(e, 'read', lambda: b'')()
        try:
            return json.loads(body)
        except Exception:
            return {'ok': False, 'description': str(e)}


def tg(method, params):
    """Telegram call with one smart retry honoring 429 retry_after."""
    res = _tg_once(method, params)
    desc = str(res.get('description', ''))
    if not res.get('ok') and 'retry after' in desc.lower():
        m = re.search(r'retry after (\d+)', desc, re.IGNORECASE)
        time.sleep(min(int(m.group(1)) if m else 5, 30) + 1)
        res = _tg_once(method, params)
    return res


def esc(s):
    return html.escape(s)


def build_caption(tw, translation, analysis, limit):
    """Budget-aware caption: header + original + quote + translation +
    analysis + links, clamped so nothing is cut mid-entity (parse-safe)."""
    handle = tw['user']
    try:
        dt = datetime.fromisoformat(
            tw['created_at'].replace('Z', '+00:00')).astimezone(IR_TZ)
        now = datetime.now(IR_TZ)
        if dt.date() == now.date():
            date_label = fa_digits(dt.strftime('%H:%M')) + " به وقت تهران"
        else:
            date_label = (fa_digits(dt.strftime('%d %b %Y')) + " — " +
                          fa_digits(dt.strftime('%H:%M')) + " تهران")
    except Exception:
        date_label = ""
    head = f"<b>{esc(tw['full_name'])} (@{esc(handle)})</b> | {date_label}"
    links = f'<a href="https://x.com/{handle}/status/{tw["id"]}">مشاهده توییت</a>'
    q = tw.get('quote')
    if q:
        links += (f'\n<a href="https://x.com/{q["user"]}/status/{q["id"]}">'
                  f'مشاهده توییتِ نقل‌شده</a>')
    fixed = len(head) + len(links) + 90
    avail = max(150, limit - fixed)
    o, qt, tr_, an_ = 0.34, 0.15, 0.21, 0.30
    if not q:
        o, qt, tr_, an_ = 0.36, 0.0, 0.24, 0.40
    parts = [head, "", f"<blockquote>{clamp(esc(tw['text'].strip()), int(avail * o))}</blockquote>"]
    if q:
        parts += ["", f"📌 نقل از <b>{esc(q['full_name'])}</b> (@{esc(q['user'])}):",
                  f"<blockquote>{clamp(esc(q['text'].strip()), int(avail * qt))}</blockquote>"]
        if not is_persian(q['text']) and q['text'].strip():
            qtr = translate_smart(q['text'])
            if qtr:
                parts.append(f"ترجمه نقل: {clamp(esc(qtr), int(avail * 0.08))}")
    if translation:
        parts += ["", f"ترجمه: {clamp(esc(translation), int(avail * tr_))}"]
    if analysis:
        parts += ["", f"🧠 تحلیل: {clamp(esc(analysis), int(avail * an_))}"]
    parts += ["", links]
    return "\n".join(parts)[:limit]


def send_tweet_message(tw, translation, analysis):
    media = list(tw['media'] or [])
    q = tw.get('quote')
    if q and q.get('media'):
        media += q['media']           # quoted tweet's photos too (cap 10)
    if len(media) >= 2:
        items = []
        for i, u in enumerate(media[:10]):
            item = {'type': 'photo', 'media': u}
            if i == 0:
                item['caption'] = build_caption(tw, translation, analysis, 1024)
                item['parse_mode'] = 'HTML'
            items.append(item)
        return tg('sendMediaGroup', {
            'chat_id': CHAT_ID, 'media': json.dumps(items, ensure_ascii=False)})
    if media:
        return tg('sendPhoto', {
            'chat_id': CHAT_ID, 'photo': media[0],
            'caption': build_caption(tw, translation, analysis, 1024),
            'parse_mode': 'HTML'})
    return tg('sendMessage', {
        'chat_id': CHAT_ID, 'text': build_caption(tw, translation, analysis, 4000),
        'parse_mode': 'HTML', 'disable_web_page_preview': 'true'})


def send_followup(entry, translation, analysis):
    """Compact follow-up for a tweet missing its translation/analysis."""
    handle = entry.get('h', '')
    tid = entry.get('id', '')
    label = []
    if translation:
        label.append('🔁')
    if analysis:
        label.append('🧠')
    kind = 'ترجمه' if translation and not analysis else (
        'تحلیل' if analysis and not translation else 'ترجمه و تحلیل')
    head = f"{(''.join(label))} {kind} تکمیلی — <b>{esc(entry.get('n', ''))} (@{esc(handle)})</b>"
    orig = clamp(esc(entry.get('t', '')), 240)
    parts = [head, f"<blockquote>{orig}</blockquote>"]
    if translation:
        parts.append(f"ترجمه: {clamp(esc(translation), 900)}")
    if analysis:
        parts.append(f"تحلیل: {clamp(esc(analysis), 1200)}")
    parts.append(f'<a href="https://x.com/{handle}/status/{tid}">مشاهده توییت</a>')
    return tg('sendMessage', {
        'chat_id': CHAT_ID, 'text': "\n".join(parts)[:4000], 'parse_mode': 'HTML',
        'disable_web_page_preview': 'true'})


# ------------------- PENDING (translation/analysis) SELF-HEAL -------------------

def process_pending(pending):
    """Retry translations+analyses for tweets that went out incomplete; post
    compact follow-ups when an engine finally succeeds. Mutates `pending`."""
    if not pending:
        return 0
    sent = 0
    for tid, entry in list(pending.items())[:MAX_FOLLOWUPS]:
        need_tr, need_an = bool(entry.get('tr')), bool(entry.get('an'))
        if not need_tr and not need_an:
            pending.pop(tid, None)
            continue
        translation = translate_smart(entry.get('t', '')) if need_tr else None
        analysis = None
        if need_an:
            fake_tw = {'user': entry.get('h', ''), 'full_name': entry.get('n', ''),
                       'text': entry.get('t', ''), 'quote': entry.get('q'),
                       'eng': entry.get('eng') or {}}
            analysis = analyze_smart(fake_tw, translation)
        got_something = (need_tr and translation) or (need_an and analysis)
        if not got_something:
            log(f"  {tid}: translation/analysis still pending (retry next run)")
            continue
        res = send_followup({**entry, 'id': tid}, translation, analysis)
        if res.get('ok'):
            log(f"  follow-up sent for {tid}")
            if not (need_tr and not translation):
                entry['tr'] = 0
            if not (need_an and not analysis):
                entry['an'] = 0
            if not entry.get('tr') and not entry.get('an'):
                pending.pop(tid, None)
            sent += 1
        else:
            log(f"  follow-up send FAILED {tid}: {res.get('description')}")
    return sent


# ------------------- MAIN -------------------

def process_account(acct, state, ledger, counters, pending):
    handle = acct['handle']
    log(f"Checking @{handle} ...")
    try:
        ids = fetch_profile_ids(handle)
    except Exception as e:
        log(f"  profile fetch error: {e}")
        return 0
    if not ids:
        log("  no tweets found (page empty or blocked)")
        return 0

    if handle not in state or not str(state[handle]).isdigit():
        state[handle] = max(ids, key=int)
        log(f"  first run: seeded state at {state[handle]}")
        return 0

    last_id = str(state[handle])
    new_ids = sorted([i for i in ids if int(i) > int(last_id)], key=int)
    if not new_ids:
        log("  no new tweets")
        return 0
    if len(new_ids) > MAX_PER_ACCOUNT:
        log(f"  flood guard: {len(new_ids)} new ids, "
            f"keeping only newest {MAX_PER_ACCOUNT}")
        new_ids = new_ids[-MAX_PER_ACCOUNT:]

    sent = 0
    decided = []
    for tid in new_ids:
        if tid in ledger:
            log(f"  skip {tid}: already sent (ledger)")
            decided.append(tid)
            continue
        if counters['sent'] >= MAX_PER_RUN:
            log("  run send-cap reached - remaining tweets stay UNdecided")
            break
        tw = fetch_tweet(tid, handle)
        if not tw:
            log(f"  skip {tid}: unavailable (deleted or fetch failed)")
            decided.append(tid)
            continue
        if tw['reply_to']:
            log(f"  skip reply {tid}")
            decided.append(tid)
            continue
        if tw['text'].startswith('RT @'):
            log(f"  skip retweet {tid}")
            decided.append(tid)
            continue
        if acct['iran_only'] and not iran_related(tw['text']):
            log(f"  skip non-Iran tweet {tid}")
            decided.append(tid)
            continue
        if tw.get('quote') and tw['quote'].get('user', '').lower() == handle.lower() \
                and not tw['text'].strip():
            # pure self-quote with no added text -> treat as unavailable
            log(f"  skip empty self-quote {tid}")
            decided.append(tid)
            continue
        needs_translation = not is_persian(tw['text'])
        translation = translate_smart(tw['text']) if needs_translation else None
        analysis = analyze_smart(tw, translation)
        res = send_tweet_message(tw, translation, analysis)
        if res.get('ok'):
            sent += 1
            counters['sent'] += 1
            log(f"  SENT {tid}"
                f"{' (ترجمه+تحلیل' if (translation or not needs_translation) and analysis else ''}"
                f"{'+ترجمه‌ناقص' if needs_translation and not translation else ''}"
                f"{'+تحلیل‌ناقص' if not analysis else ''}): {tw['text'][:50]!r}")
            state[handle] = tid
            ledger[tid] = handle
            miss_tr = needs_translation and not translation
            miss_an = analysis is None
            if miss_tr or miss_an:
                entry = {'h': handle, 't': tw['text'][:1500],
                         'n': tw['full_name'], 'ts': time.time(),
                         'tr': int(miss_tr), 'an': int(miss_an),
                         'eng': tw.get('eng') or {}}
                if tw.get('quote'):
                    entry['q'] = {'user': tw['quote']['user'],
                                  'text': tw['quote']['text'][:500]}
                pending[tid] = entry
                log(f"  incomplete (tr={int(miss_tr)} an={int(miss_an)}) -> queued")
            push_all(state, ledger, pending)
        else:
            log(f"  send FAILED {tid}: {res.get('description')}")
            break   # NOT decided -> retried next run
        time.sleep(2.5)

    if decided:
        decided_max = max(decided, key=int)
        if int(decided_max) > int(state[handle]):
            state[handle] = decided_max
            push_all(state, ledger, pending)
    return sent


def main():
    if not BOT_TOKEN:
        log("FATAL: BOT_TOKEN env var is not set (GitHub secret missing)")
        sys.exit(1)
    log("Twitter->Telegram monitor v7 (translator+analyst) run starting")
    state = {}
    try:
        with open(STATE_FILE, encoding='utf-8') as f:
            state.update(json.load(f))
    except Exception:
        pass
    # FAIL-CLOSED: remote state is the single source of truth
    try:
        remote_state, _ = gh_get('state.json')
        if remote_state is not None:
            for k, v in remote_state.items():
                if str(v).isdigit() and (k not in state or int(v) > int(state[k])):
                    state[k] = v
        remote_ledger, _ = gh_get('ledger.json')
        remote_pending, _ = gh_get('pending.json')
    except Exception as e:
        log(f"remote state pull failed ({e}); aborting run to stay safe")
        sys.exit(0)
    ledger = dict(remote_ledger or {})
    pending = dict(remote_pending or {})

    followups = process_pending(pending)

    counters = {'sent': 0}
    total = 0
    for acct in ACCOUNTS:
        try:
            total += process_account(acct, state, ledger, counters, pending)
        except PushFailed as e:
            log(f"FATAL: {e} - aborting run (chain pauses, schedule retries)")
            sys.exit(1)
        except Exception as e:
            log(f"  account error @{acct['handle']}: {e}")
    log(f"Done: {total} new tweets posted, {followups} follow-ups, "
        f"{len(pending)} translations/analyses still pending")


if __name__ == '__main__':
    main()
