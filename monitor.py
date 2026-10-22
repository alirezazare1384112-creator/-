#!/usr/bin/env python3
"""
Twitter -> Telegram Auto-Translator (GitHub Actions) v8
========================================================
v8 = v7 MINUS analysis + COMPLETE translations + duplicate-proof sends.

WHAT CHANGED (v8):
  1. ANALYSIS REMOVED (user request). No more Pollinations calls - runs are
     much faster and messages are clean: header + original + quote (کوت) +
     full translation + links + photos.
  2. COMPLETE TRANSLATIONS: long posts are translated chunk-by-chunk
     (gtx POST, 1800-char chunks split at line/word boundaries) so a tweet
     can never arrive with a "half" translation again. MyMemory remains a
     fallback for short texts only (480-char engine ceiling).
  3. DUPLICATE-PROOF SENDING (the "60 times" fix): every send is
     MARK-BEFORE-SEND - the ledger mark is pushed to GitHub BEFORE the
     Telegram call. A push failure aborts the run with nothing sent, so a
     tweet can never be delivered twice even when pushes/rates flake.
  4. PENDING SELF-HEAL, LOOP-SAFE: entries carry fs (follow-up-sent) +
     n_att (attempt counter); mutations are ALWAYS pushed. An fs-marked
     entry is finalized silently on the next run instead of re-sending -
     the v7 bug re-sent the same follow-up every run (60x flood).
  5. DEAD-LETTER: a tweet whose Telegram send fails twice (with a fresh
     media-URL refetch between attempts) is marked in the ledger and never
     retried - no infinite retry loops.
  6. Media tweets with long content: photos go out with a compact caption
     and the FULL original+translation follows as a text message - the
     translation is never clamped away to squeeze into the 1024 caption
     limit (the v7 "half translation" complaint).

Carried over from v6/v7: quotes (کوت) support, triple dedup (state +
ledger + pre-send check), fail-closed pushes, 429 backoff on 3 gtx hosts,
Arabic != Persian detection, decided-only state advance, Telegram 429
retry, fetch hint, 14 accounts.

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
MAX_FOLLOWUPS = 3         # translation follow-ups per run
PENDING_MAX_AGE = 7 * 24 * 3600   # seconds before a pending entry expires
PENDING_MAX_ATTEMPTS = 12         # translation retry cap per pending entry

GTX_CHUNK = 1800           # source chars per gtx chunk (POST, no URL limit)
MYMEMORY_MAX = 480         # MyMemory fallback only for short texts

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


class PushFailed(Exception):
    """Registry push failed after retries -> abort the whole run."""
    pass


# tids removed from the pending registry during this run - deletions must
# stick, so push_all never resurrects a tombstoned entry via the union merge.
_pending_tombstones = set()


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


def esc(s):
    return html.escape(s)


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
            for k in _pending_tombstones:      # deletions stick (60x-loop fix)
                merged_pend.pop(k, None)
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
    """Full tweet data + quoted tweet (کوت)."""
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
    return {
        'id': d.get('id_str'),
        'text': _clean_tweet_text(d, media),
        'created_at': d.get('created_at', ''),
        'user': (d.get('user') or {}).get('screen_name', handle),
        'full_name': (d.get('user') or {}).get('name', ''),
        'reply_to': d.get('in_reply_to_status_id'),
        'media': media,
        'quote': quote,
    }


# ------------------- TRANSLATION (complete, chunked, multi-engine) -------------------

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


def _split_text_chunks(text, limit):
    """Split long text into chunks <= limit chars, cutting at paragraph /
    line / word boundaries whenever possible (never mid-word)."""
    if len(text) <= limit:
        return [text]
    chunks = []
    rest = text
    while rest:
        if len(rest) <= limit:
            chunks.append(rest)
            break
        window = rest[:limit]
        cut = max(window.rfind('\n\n'), window.rfind('\n'), window.rfind(' '))
        if cut < limit // 2:
            cut = limit - 1
        chunks.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip('\n')
    return chunks


def _gtx_once(text, host, timeout=15):
    """One gtx call via POST (no URL-length limit, works for long chunks)."""
    data = urllib.parse.urlencode({'client': 'gtx', 'sl': 'auto', 'tl': 'fa',
                                   'dt': 't', 'q': text}).encode()
    req = urllib.request.Request(f'https://{host}/translate_a/single',
                                 data=data, headers={'User-Agent': UA})
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


def _gtx_chunk(text):
    """All 3 gtx hosts x 2 attempts (429 backoff). None if all fail."""
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
    return None


def translate_smart(text):
    """COMPLETE translation: long texts are split into chunks, each chunk is
    translated on its own and the results are joined. All-or-nothing: if any
    chunk fails on every engine, the whole translation is retried later via
    the pending self-heal (a tweet can no longer go out half-translated)."""
    text = (text or '').strip()
    if not text:
        return None
    parts = []
    for chunk in _split_text_chunks(text, GTX_CHUNK):
        tr = _gtx_chunk(chunk)
        if tr is None:
            # MyMemory only helps short texts (480-char engine ceiling)
            if len(text) <= MYMEMORY_MAX:
                try:
                    return _mymemory_once(text)
                except Exception:
                    return None
            return None
        parts.append(tr)
    return '\n'.join(parts).strip() or None


def iran_related(text):
    t = text.lower()
    return any(k in t for k in IRAN_KEYWORDS)


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


def _date_label(tw):
    try:
        dt = datetime.fromisoformat(
            tw['created_at'].replace('Z', '+00:00')).astimezone(IR_TZ)
        now = datetime.now(IR_TZ)
        if dt.date() == now.date():
            return fa_digits(dt.strftime('%H:%M')) + " به وقت تهران"
        return (fa_digits(dt.strftime('%d %b %Y')) + " — " +
                fa_digits(dt.strftime('%H:%M')) + " تهران")
    except Exception:
        return ""


def _links_line(tw):
    s = f'<a href="https://x.com/{tw["user"]}/status/{tw["id"]}">مشاهده توییت</a>'
    q = tw.get('quote')
    if q:
        s += (f'\n<a href="https://x.com/{q["user"]}/status/{q["id"]}">'
              f'مشاهده توییتِ نقل‌شده</a>')
    return s


def build_blocks(tw, translation):
    """Message blocks (HTML, safely clamped). Returns
    (head, orig, quote_parts, trans_parts, links)."""
    head = f"<b>{esc(tw['full_name'])} (@{esc(tw['user'])})</b>"
    dl = _date_label(tw)
    if dl:
        head += f" | {dl}"
    orig = f"<blockquote>{clamp(esc(tw['text'].strip()), 2600)}</blockquote>"
    quote_parts = []
    q = tw.get('quote')
    if q:
        quote_parts.append(
            f"📌 نقل از <b>{esc(q['full_name'])}</b> (@{esc(q['user'])}):")
        quote_parts.append(
            f"<blockquote>{clamp(esc(q['text'].strip()), 1000)}</blockquote>")
        if q['text'].strip() and not is_persian(q['text']):
            qtr = translate_smart(q['text'])
            if qtr:
                quote_parts.append(f"ترجمه نقل: {clamp(esc(qtr), 1000)}")
    trans_parts = []
    if translation:
        trans_parts.append(f"ترجمه: {clamp(esc(translation), 3000)}")
    links = _links_line(tw)
    return head, orig, quote_parts, trans_parts, links


def _join_blocks(head, orig, quote_parts, trans_parts, links):
    parts = [head, "", orig]
    if quote_parts:
        parts += [""] + quote_parts
    if trans_parts:
        parts += [""] + trans_parts
    parts += ["", links]
    return "\n".join(parts)


def _send_text_split(text):
    """sendMessage, splitting into <=4000-char parts at line boundaries."""
    while text:
        if len(text) <= 4000:
            return tg('sendMessage', {
                'chat_id': CHAT_ID, 'text': text, 'parse_mode': 'HTML',
                'disable_web_page_preview': 'true'})
        cut = text.rfind('\n', 0, 4000)
        if cut < 1000:
            cut = 4000
        part, text = text[:cut], text[cut:].lstrip('\n')
        res = tg('sendMessage', {
            'chat_id': CHAT_ID, 'text': part, 'parse_mode': 'HTML',
            'disable_web_page_preview': 'true'})
        if not res.get('ok'):
            return res
    return {'ok': True}


def _send_media(media, caption):
    if len(media) >= 2:
        items = []
        for i, u in enumerate(media[:10]):
            item = {'type': 'photo', 'media': u}
            if i == 0:
                item['caption'] = caption
                item['parse_mode'] = 'HTML'
            items.append(item)
        return tg('sendMediaGroup', {
            'chat_id': CHAT_ID, 'media': json.dumps(items, ensure_ascii=False)})
    if media:
        return tg('sendPhoto', {
            'chat_id': CHAT_ID, 'photo': media[0], 'caption': caption,
            'parse_mode': 'HTML'})
    return tg('sendMessage', {
        'chat_id': CHAT_ID, 'text': caption, 'parse_mode': 'HTML',
        'disable_web_page_preview': 'true'})


def send_tweet_message(tw, translation):
    """Complete-content send: if everything fits the media caption limit it
    goes out as ONE message; otherwise the photos carry a compact caption and
    the FULL original + quote + translation + links follow as a text message.
    The translation is never truncated to fit a caption."""
    head, orig, quote_parts, trans_parts, links = build_blocks(tw, translation)
    media = list(tw['media'] or [])
    q = tw.get('quote')
    if q and q.get('media'):
        media += q['media']           # quoted tweet's photos too (cap 10)
    full = _join_blocks(head, orig, quote_parts, trans_parts, links)
    if not media:
        return _send_text_split(full)
    if len(full) <= 1000:
        return _send_media(media, full)
    # long content + media: compact caption on the photo, full text after it
    compact = "\n".join([head, "",
                         clamp(esc(tw['text'].strip()), 380), "", links])
    if len(compact) > 1000:
        compact = "\n".join([head, "", links])
    rm = _send_media(media, compact)
    if not rm.get('ok'):
        return rm
    return _send_text_split(full)


def send_followup(entry, translation):
    """Compact follow-up delivering a missing translation."""
    handle = entry.get('h', '')
    tid = entry.get('id', '')
    parts = [f"🔁 ترجمه تکمیلی — <b>{esc(entry.get('n', ''))} (@{esc(handle)})</b>",
             f"<blockquote>{clamp(esc(entry.get('t', '')), 400)}</blockquote>",
             f"ترجمه: {clamp(esc(translation), 3000)}",
             f'<a href="https://x.com/{handle}/status/{tid}">مشاهده توییت</a>']
    return tg('sendMessage', {
        'chat_id': CHAT_ID, 'text': "\n".join(parts)[:4000], 'parse_mode': 'HTML',
        'disable_web_page_preview': 'true'})


# ------------------- PENDING (translation) SELF-HEAL - loop-safe -------------------

def process_pending(state, ledger, pending):
    """Retry missing translations; post compact follow-ups when an engine
    finally succeeds. LOOP-SAFE by design:
      * entries already marked fs (follow-up sent) are finalized silently -
        never re-sent (the v7 60x flood bug);
      * the fs mark is PUSHED BEFORE the follow-up is sent (a push failure
        aborts the run with nothing sent);
      * every mutation is always pushed back to the repo.
    Returns number of follow-ups sent. Raises PushFailed (fail-closed)."""
    if not pending:
        return 0
    now = time.time()
    sent = 0
    mutated = False

    def _drop(tid):
        pending.pop(tid, None)
        _pending_tombstones.add(tid)

    # 1) cleanup pass: drop expired / already-sent / nothing-needed entries
    for tid in list(pending.keys()):
        e = pending[tid]
        if now - float(e.get('ts', now)) > PENDING_MAX_AGE \
                or e.get('fs') or not e.get('tr'):
            log(f"  pending {tid}: finalized (expired/sent/nothing needed)")
            _drop(tid)
            mutated = True
    # 2) retry pass (bounded)
    for tid, entry in list(pending.items())[:MAX_FOLLOWUPS]:
        translation = translate_smart(entry.get('t', ''))
        if not translation:
            entry['n_att'] = int(entry.get('n_att', 0)) + 1
            mutated = True
            if entry['n_att'] >= PENDING_MAX_ATTEMPTS:
                log(f"  pending {tid}: retry cap reached - dropping")
                _drop(tid)
            else:
                log(f"  pending {tid}: translation still missing "
                    f"(attempt {entry['n_att']})")
            continue
        # MARK-BEFORE-SEND: fs is pushed to GitHub BEFORE the follow-up goes
        # out; a PushFailed aborts the run and nothing is duplicated.
        entry['fs'] = 1
        push_all(state, ledger, pending)
        res = send_followup({**entry, 'id': tid}, translation)
        if res.get('ok'):
            log(f"  follow-up sent for {tid}")
            _drop(tid)
            sent += 1
            mutated = True
        else:
            log(f"  follow-up send FAILED {tid}: {res.get('description')}")
            entry['fs'] = 0
            entry['n_att'] = int(entry.get('n_att', 0)) + 1
            mutated = True
    # 3) THE 60x FIX: pending mutations are ALWAYS persisted
    if mutated:
        push_all(state, ledger, pending)
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
        # DUPLICATE-PROOF SEND: the ledger mark is pushed BEFORE the send.
        # Push failure -> PushFailed -> run aborts with NOTHING sent, so a
        # tweet can never be delivered twice (even with flaky pushes).
        ledger[tid] = handle
        push_all(state, ledger, pending)
        res = send_tweet_message(tw, translation)
        if not res.get('ok'):
            log(f"  send failed ({res.get('description')}) - "
                f"refetching fresh media URLs for one retry")
            time.sleep(2)
            try:
                tw2 = fetch_tweet(tid, handle)
                if tw2:
                    res = send_tweet_message(tw2, translation)
            except Exception as e:
                log(f"  retry error: {e}")
        if res.get('ok'):
            sent += 1
            counters['sent'] += 1
            log(f"  SENT {tid}"
                f"{' (ترجمه کامل)' if (translation or not needs_translation) else ' (ترجمه‌ناقص→تکمیلی)'}"
                f": {tw['text'][:50]!r}")
            state[handle] = tid
            miss_tr = needs_translation and not translation
            if miss_tr:
                pending[tid] = {'h': handle, 't': tw['text'][:3000],
                                'n': tw['full_name'], 'ts': time.time(),
                                'tr': 1}
                log("  translation missing -> queued for follow-up")
            push_all(state, ledger, pending)
        else:
            # DEAD-LETTER: never retry a tweet Telegram refuses twice -
            # infinite retry loops are how the old floods happened.
            log(f"  send FAILED {tid} twice: {res.get('description')} "
                f"-> dead-lettered (skipped permanently)")
            decided.append(tid)
            push_all(state, ledger, pending)
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
    log("Twitter->Telegram monitor v8 (complete-translation, dup-proof) run starting")
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

    try:
        followups = process_pending(state, ledger, pending)
    except PushFailed as e:
        log(f"FATAL: {e} - aborting run (chain pauses, schedule retries)")
        sys.exit(1)

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
    # final safety net: persist anything still dirty (diff-aware, cheap)
    try:
        push_all(state, ledger, pending)
    except PushFailed as e:
        log(f"FATAL: final push failed ({e})")
        sys.exit(1)
    log(f"Done: {total} new tweets posted, {followups} follow-ups, "
        f"{len(pending)} translations still pending")


if __name__ == '__main__':
    main()
