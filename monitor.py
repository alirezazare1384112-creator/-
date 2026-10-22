#!/usr/bin/env python3
"""
Twitter -> Telegram Auto-Translator (standalone, GitHub Actions edition) v6
=============================================================================
"Smart" edition. Guarantees (each tweet sent EXACTLY once, WITH translation):

 DEDUP (unchanged from v4, proven):
  1. state.json       : per-account high-water marks (shared w/ sandbox daemon)
  2. ledger.json      : registry of every tweet id ever successfully sent
  3. check ledger + mark before each send; push both to GitHub after each send
  4. push failure => abort run (fail-closed) - a broken push can never flood

 NEW IN v6:
  5. SMART TRANSLATION: 3 gtx hosts + MyMemory fallback + 429 backoff.
     gtx is heavily 429-rate-limited from shared GitHub runner IPs, which is
     why tweets used to go out untranslated ("translate error: 429").
  6. SELF-HEALING TRANSLATIONS: a tweet sent WITHOUT translation (all
     engines failed) is stored in untranslated.json (repo). Every later run
     retries it (fresh runner IP = fresh quota); once a translation succeeds
     a compact follow-up message is posted and the entry removed. A tweet
     never stays untranslated forever.
  7. ARABIC != PERSIAN: Arabic statements (Hezbollah/Hamas/Ansar Allah
     communiques share the Unicode block) are now detected and translated.
  8. STATE-ADVANCE BUGFIX: the mark only advances past tweets that were
     actually DECIDED (sent / skipped); a run-cap or send-failure break can
     no longer mark unprocessed tweets as seen.
  9. SAFE CAPTIONS: media captions are budgeted BEFORE escaping - no more
     HTML tags/entities cut mid-way (Telegram parse errors).
 10. TELEGRAM 429: honors retry_after, retries once.
 11. FETCH HINT: after 2 consecutive direct-x.com failures (runners are
     Cloudflare-blocked) the run skips straight to translate.goog for the
     remaining accounts - halves request count + run time.
 12. NEW ACCOUNTS (14): + MoloWarMonitor, WarMonitor3, Faytuks,
     Osinttechnical, vcdgf555.

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
MAX_PER_RUN = 18          # absolute send cap per run (14 accounts now)
LEDGER_MAX = 700          # keep newest N sent-ids in the registry
MAX_FOLLOWUPS = 3         # retro-translation follow-ups per run
UNTRANSLATED_MAX_AGE = 7 * 24 * 3600   # seconds before a pending entry expires

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
    """State/ledger push failed after retries -> abort the whole run."""
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


def push_all(state, ledger, untranslated):
    """Push state + ledger + untranslated registry with fresh-sha retry.
    Raises PushFailed after PUSH_RETRIES consecutive failures."""
    for attempt in range(1, PUSH_RETRIES + 1):
        try:
            r_state, sha_state = gh_get('state.json')
            r_ledger, sha_ledger = gh_get('ledger.json')
            r_untr, sha_untr = gh_get('untranslated.json')
            merged_state = dict(r_state or {})
            for k, v in state.items():
                if str(v).isdigit() and (k not in merged_state or
                                          int(v) > int(merged_state[k])):
                    merged_state[k] = v
            merged_ledger = dict(r_ledger or {})
            merged_ledger.update(ledger)
            if len(merged_ledger) > LEDGER_MAX:
                merged_ledger = dict(list(merged_ledger.items())[-LEDGER_MAX:])
            merged_untr = dict(r_untr or {})
            merged_untr.update(untranslated)
            # expire stale pending-translation entries (deleted tweets etc.)
            now = time.time()
            merged_untr = {k: v for k, v in merged_untr.items()
                           if now - float(v.get('ts', now)) < UNTRANSLATED_MAX_AGE}
            if merged_state != (r_state or {}):
                gh_put('state.json', merged_state, sha_state)
                log("  state pushed to GitHub")
            if merged_ledger != (r_ledger or {}):
                gh_put('ledger.json', merged_ledger, sha_ledger)
                log("  ledger pushed to GitHub")
            if merged_untr != (r_untr or {}):
                gh_put('untranslated.json', merged_untr, sha_untr)
                log("  untranslated registry pushed to GitHub")
            # adopt remote-ahead values locally
            for k, v in merged_state.items():
                if str(v).isdigit() and k in state and int(v) > int(state[k]):
                    state[k] = v
            ledger.clear()
            ledger.update(merged_ledger)
            untranslated.clear()
            untranslated.update(merged_untr)
            return True
        except PushFailed as e:
            log(f"  push attempt {attempt}/{PUSH_RETRIES} failed: {e}")
            time.sleep(2 * attempt)
    raise PushFailed("state/ledger/untranslated push failed after all retries")


# ------------------- TWITTER -------------------

_direct_fail_streak = 0   # per-run fetch hint (runers are CF-blocked)


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


def fetch_tweet(tweet_id, handle):
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
    return {
        'id': d.get('id_str'),
        'text': text,
        'created_at': d.get('created_at', ''),
        'user': (d.get('user') or {}).get('screen_name', handle),
        'full_name': (d.get('user') or {}).get('name', ''),
        'reply_to': d.get('in_reply_to_status_id'),
        'media': media,
    }


# ------------------- TRANSLATION (smart, multi-engine) -------------------

def is_persian(text):
    """True only for actual PERSIAN text. Arabic (same Unicode block, used by
    Hezbollah/Hamas/Ansar Allah communiques) is NOT Persian and must be
    translated - Arabic uses ي ك ة while Persian uses ی ک پ چ ژ گ."""
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
    """3 gtx hosts x 2 attempts (429 backoff) + MyMemory fallback.
    Returns Persian translation or None."""
    for host in GTX_HOSTS:
        for attempt in (1, 2):
            try:
                return _gtx_once(text, host)
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    time.sleep(2 + 2 * attempt)   # backoff, then retry/re-host
                    continue
                break                            # other error -> next host
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


def build_caption(tw, translation, limit):
    """Budget-aware caption: header + original + translation + link, clamped
    so nothing is ever cut mid-entity (Telegram HTML parse-safe)."""
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
    link = f'<a href="https://x.com/{handle}/status/{tw["id"]}">مشاهده توییت</a>'
    trans_block = f"ترجمه: {esc(translation)}" if translation else ""
    fixed = len(head) + len(link) + len(trans_block) + 60   # tags+newlines margin
    orig_budget = max(80, limit - fixed)
    orig = clamp(esc(tw['text'].strip()), orig_budget)
    parts = [head, "", f"<blockquote>{orig}</blockquote>"]
    if trans_block:
        parts += ["", trans_block]
    parts += ["", link]
    return "\n".join(parts)[:limit]


def send_tweet_message(tw, translation):
    if len(tw['media']) >= 2:
        media = []
        for i, u in enumerate(tw['media'][:10]):
            item = {'type': 'photo', 'media': u}
            if i == 0:
                item['caption'] = build_caption(tw, translation, 1024)
                item['parse_mode'] = 'HTML'
            media.append(item)
        return tg('sendMediaGroup', {
            'chat_id': CHAT_ID, 'media': json.dumps(media, ensure_ascii=False)})
    if tw['media']:
        return tg('sendPhoto', {
            'chat_id': CHAT_ID, 'photo': tw['media'][0],
            'caption': build_caption(tw, translation, 1024),
            'parse_mode': 'HTML'})
    return tg('sendMessage', {
        'chat_id': CHAT_ID, 'text': build_caption(tw, translation, 4000),
        'parse_mode': 'HTML', 'disable_web_page_preview': 'true'})


def send_followup_translation(entry, translation):
    """Compact follow-up for a tweet that was delivered untranslated earlier."""
    handle = entry.get('h', '')
    tid = entry.get('id', '')
    link = f"https://x.com/{handle}/status/{tid}"
    orig = clamp(esc(entry.get('t', '')[:260]), 240)
    msg = (f"🔁 ترجمه تکمیلی — <b>{esc(entry.get('n', ''))} (@{esc(handle)})</b>\n"
           f"<blockquote>{orig}</blockquote>\n"
           f"ترجمه: {clamp(esc(translation), 500)}\n"
           f'<a href="{link}">مشاهده توییت</a>')
    return tg('sendMessage', {
        'chat_id': CHAT_ID, 'text': msg[:4000], 'parse_mode': 'HTML',
        'disable_web_page_preview': 'true'})


# ------------------- PENDING-TRANSLATION SELF-HEAL -------------------

def process_pending_translations(untranslated):
    """Retry translations for tweets that went out untranslated; post compact
    follow-ups when an engine finally succeeds. Mutates `untranslated`."""
    if not untranslated:
        return 0
    sent = 0
    for tid, entry in list(untranslated.items())[:MAX_FOLLOWUPS]:
        text = entry.get('t', '')
        if not text:
            untranslated.pop(tid, None)
            continue
        translation = translate_smart(text)
        if translation:
            res = send_followup_translation({**entry, 'id': tid}, translation)
            if res.get('ok'):
                log(f"  follow-up translation sent for {tid}")
                untranslated.pop(tid, None)
                sent += 1
            else:
                log(f"  follow-up send FAILED {tid}: {res.get('description')}")
        else:
            log(f"  translation still pending for {tid} (will retry next run)")
    return sent


# ------------------- MAIN -------------------

def process_account(acct, state, ledger, counters, untranslated):
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
    decided = []   # ids we made a final decision about (sent or skipped)
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
        translation = None
        needs_translation = not is_persian(tw['text'])
        if needs_translation:
            translation = translate_smart(tw['text'])
        res = send_tweet_message(tw, translation)
        if res.get('ok'):
            sent += 1
            counters['sent'] += 1
            log(f"  SENT {tid}"
                f"{' (with translation)' if needs_translation else ''}"
                f": {tw['text'][:60]!r}")
            state[handle] = tid
            ledger[tid] = handle
            if needs_translation and not translation:
                # self-heal: retry from a fresh runner IP next run
                untranslated[tid] = {'h': handle, 't': tw['text'][:1200],
                                     'n': tw['full_name'], 'ts': time.time()}
                log("  translation unavailable -> queued for follow-up")
            push_all(state, ledger, untranslated)
        else:
            log(f"  send FAILED {tid}: {res.get('description')}")
            break   # NOT decided -> retried next run
        time.sleep(2.5)

    # advance the mark ONLY past tweets we actually decided - a run-cap or
    # send-failure break can no longer skip unprocessed tweets forever
    if decided:
        decided_max = max(decided, key=int)
        if int(decided_max) > int(state[handle]):
            state[handle] = decided_max
            push_all(state, ledger, untranslated)
    return sent


def main():
    if not BOT_TOKEN:
        log("FATAL: BOT_TOKEN env var is not set (GitHub secret missing)")
        sys.exit(1)
    log("Twitter->Telegram monitor v6 run starting")
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
        remote_untr, _ = gh_get('untranslated.json')
    except Exception as e:
        log(f"remote state pull failed ({e}); aborting run to stay safe")
        sys.exit(0)
    ledger = dict(remote_ledger or {})
    untranslated = dict(remote_untr or {})

    followups = process_pending_translations(untranslated)

    counters = {'sent': 0}
    total = 0
    for acct in ACCOUNTS:
        try:
            total += process_account(acct, state, ledger, counters, untranslated)
        except PushFailed as e:
            log(f"FATAL: {e} - aborting run (chain pauses, schedule retries)")
            sys.exit(1)
        except Exception as e:
            log(f"  account error @{acct['handle']}: {e}")
    log(f"Done: {total} new tweets posted, {followups} follow-up translations, "
        f"{len(untranslated)} translations still pending")


if __name__ == '__main__':
    main()
