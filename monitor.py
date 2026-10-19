#!/usr/bin/env python3
"""
Twitter -> Telegram Auto-Translator (standalone, GitHub Actions edition) v4
==========================================================================
Dedup-hardened rewrite. Key guarantees (each tweet is sent EXACTLY once):

 1. state.json   : per-account high-water marks, shared with the sandbox daemon
 2. ledger.json  : registry of EVERY tweet id ever successfully sent
 3. Before each send:  skip if id is in the ledger or <= the state mark
 4. After each send:   push state + ledger to GitHub IMMEDIATELY (with
    fresh-sha retry). If the push fails after retries the run ABORTS
    (fail-closed) so a broken push can never cause re-send floods.
 5. Run aborts (exit 1) => the chain does not dispatch a follow-up run;
    the */5 schedule retries within 5 minutes.

Accounts (9): hey_itsmyturn, MilitaryIr, Osint613 (Iran-only),
no_itsmyturn, _KhodaBiamorz, cicikhanoom, rezasadx, shahryarshefigh,
baaaarzaaaakh
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

# hard caps: nothing may ever spam the channel again
MAX_PER_ACCOUNT = 5       # max sends per account per run
MAX_PER_RUN = 15          # absolute send cap per run
LEDGER_MAX = 500          # keep newest N sent-ids in the registry

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
]

IRAN_KEYWORDS = [
    'iran', 'iranian', 'tehran', 'irgc', 'isfahan', 'esfahan', 'tabriz',
    'shiraz', 'persian gulf', 'bandar abbas', 'bushehr', 'natanz',
    'fordow', 'fordo', 'qom', 'mashhad', 'kermanshah', 'khuzestan',
    'ahvaz', 'hormuz', 'khamenei', 'pezeshkian', 'parchin', 'semnan',
    'irna', 'fars news', 'mahan air', 'mehsa',
    'ایران', 'تهران', 'اصفهان', 'شیراز', 'تبریز', 'سپاه', 'خامنه',
    'پزیشکیان', 'هرمز', 'بندرعباس', 'مشهد', 'قم', 'اهواز',
]

PUSH_RETRIES = 4


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


def push_state_and_ledger(state, ledger):
    """Push BOTH files with fresh-sha retry (handles concurrent writers).
    Raises PushFailed after PUSH_RETRIES consecutive failures."""
    for attempt in range(1, PUSH_RETRIES + 1):
        try:
            r_state, sha_state = gh_get('state.json')
            r_ledger, sha_ledger = gh_get('ledger.json')
            merged_state = dict(r_state or {})
            for k, v in state.items():
                if str(v).isdigit() and (k not in merged_state or
                                        int(v) > int(merged_state[k])):
                    merged_state[k] = v
            merged_ledger = dict(r_ledger or {})
            merged_ledger.update(ledger)
            # prune ledger to newest LEDGER_MAX entries (keep insertion order)
            if len(merged_ledger) > LEDGER_MAX:
                merged_ledger = dict(list(merged_ledger.items())[-LEDGER_MAX:])
            if merged_state != (r_state or {}):
                gh_put('state.json', merged_state, sha_state)
                log("  state pushed to GitHub")
            if merged_ledger != (r_ledger or {}):
                gh_put('ledger.json', merged_ledger, sha_ledger)
                log("  ledger pushed to GitHub")
            # adopt remote-ahead values locally
            for k, v in merged_state.items():
                if str(v).isdigit() and k in state and int(v) > int(state[k]):
                    state[k] = v
            ledger.clear()
            ledger.update(merged_ledger)
            return True
        except PushFailed as e:
            log(f"  push attempt {attempt}/{PUSH_RETRIES} failed: {e}")
            time.sleep(2 * attempt)
    raise PushFailed("state/ledger push failed after all retries")


# ------------------- TWITTER -------------------

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
    try:
        raw = curl(f"https://x.com/{handle}").decode('utf-8', errors='ignore')
        ids = _extract_ids(raw, handle)
        if ids:
            return ids
    except Exception as e:
        log(f"  direct fetch failed: {e}")
    try:
        raw = curl(
            f"https://x-com.translate.goog/{handle}"
            "?_x_tr_sl=auto&_x_tr_tl=en&_x_tr_hl=en"
        ).decode('utf-8', errors='ignore')
        ids = _extract_ids(raw, handle)
        if ids:
            return ids
    except Exception as e:
        log(f"  translate.goog fetch failed: {e}")
    try:
        raw = curl(
            f"https://twitter-com.translate.goog/{handle}"
            "?_x_tr_sl=auto&_x_tr_tl=en&_x_tr_hl=en"
        ).decode('utf-8', errors='ignore')
        ids = _extract_ids(raw, handle)
        if ids:
            return ids
    except Exception as e:
        log(f"  twitter-com translate.goog fetch failed: {e}")
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


# ------------------- TRANSLATION (free, no key) -------------------

def is_persian(text):
    fa = len(re.findall(r'[\u0600-\u06FF]', text))
    total = len(re.findall(r'[\w\u0600-\u06FF]', text))
    return total > 0 and fa / total > 0.35


def gtx_translate(text, src='auto'):
    try:
        url = ('https://translate.googleapis.com/translate_a/single?' +
               urllib.parse.urlencode(
                   {'client': 'gtx', 'sl': src, 'tl': 'fa', 'dt': 't',
                    'q': text[:4500]}))
        req = urllib.request.Request(url, headers={'User-Agent': UA})
        d = json.load(urllib.request.urlopen(req, timeout=15))
        out = ''.join(seg[0] for seg in d[0] if seg and seg[0])
        return out.strip() or None
    except Exception as e:
        log(f"  translate error: {e}")
        return None


def iran_related(text):
    t = text.lower()
    return any(k in t for k in IRAN_KEYWORDS)


# ------------------- TELEGRAM -------------------

def tg(method, params):
    try:
        return http_post(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
                         params)
    except Exception as e:
        body = getattr(e, 'read', lambda: b'')()
        try:
            return json.loads(body)
        except Exception:
            return {'ok': False, 'description': str(e)}


def esc(s):
    return html.escape(s)


def send_tweet_message(acct, tw, translation):
    handle = acct['handle']
    link = f"https://x.com/{handle}/status/{tw['id']}"
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

    parts = [f"<b>{esc(tw['full_name'])} (@{esc(handle)})</b> | {date_label}",
             ""]
    parts.append(f"<blockquote>{esc(tw['text'].strip())}</blockquote>")
    if translation:
        parts.append("")
        parts.append(f"ترجمه: {esc(translation)}")
    parts.append("")
    parts.append(f'<a href="{link}">مشاهده توییت</a>')
    caption = "\n".join(parts)

    if len(tw['media']) >= 2:
        media = []
        for i, u in enumerate(tw['media'][:10]):
            item = {'type': 'photo', 'media': u}
            if i == 0:
                item['caption'] = caption[:1024]
                item['parse_mode'] = 'HTML'
            media.append(item)
        return tg('sendMediaGroup', {
            'chat_id': CHAT_ID, 'media': json.dumps(media, ensure_ascii=False)})
    if tw['media']:
        return tg('sendPhoto', {
            'chat_id': CHAT_ID, 'photo': tw['media'][0],
            'caption': caption[:1024], 'parse_mode': 'HTML'})
    return tg('sendMessage', {
        'chat_id': CHAT_ID, 'text': caption[:4096], 'parse_mode': 'HTML',
        'disable_web_page_preview': 'true'})


# ------------------- MAIN -------------------

def process_account(acct, state, ledger, counters):
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
    for tid in new_ids:
        if tid in ledger:
            log(f"  skip {tid}: already sent (ledger)")
            continue
        if counters['sent'] >= MAX_PER_RUN:
            log("  run send-cap reached, stopping this account")
            break
        tw = fetch_tweet(tid, handle)
        if not tw:
            continue
        if tw['reply_to']:
            log(f"  skip reply {tid}")
            continue
        if tw['text'].startswith('RT @'):
            log(f"  skip retweet {tid}")
            continue
        if acct['iran_only'] and not iran_related(tw['text']):
            log(f"  skip non-Iran tweet {tid}")
            continue
        translation = None
        if not is_persian(tw['text']):
            translation = gtx_translate(tw['text'])
        res = send_tweet_message(acct, tw, translation)
        if res.get('ok'):
            sent += 1
            counters['sent'] += 1
            log(f"  SENT {tid}: {tw['text'][:60]!r}")
            state[handle] = tid
            ledger[tid] = handle
            # CRITICAL: persist immediately; abort the run if we can't
            push_state_and_ledger(state, ledger)
        else:
            log(f"  send FAILED {tid}: {res.get('description')}")
            break
        time.sleep(2.5)

    # advance mark past all *decided* tweets so skips aren't re-evaluated;
    # only reached when nothing aborted
    decided_max = max(new_ids, key=int)
    if int(decided_max) > int(state[handle]):
        state[handle] = decided_max
        push_state_and_ledger(state, ledger)
    return sent


def main():
    if not BOT_TOKEN:
        log("FATAL: BOT_TOKEN env var is not set (GitHub secret missing)")
        sys.exit(1)
    log("Twitter->Telegram monitor v4 run starting")
    state = {}
    try:
        state_file = open(STATE_FILE, encoding='utf-8')
        state.update(json.load(state_file))
        state_file.close()
    except Exception:
        pass
    # FAIL-CLOSED: remote state is the single source of truth; if we cannot
    # read it we must not send anything (that is what caused the flood).
    try:
        remote_state, _ = gh_get('state.json')
        if remote_state is not None:
            for k, v in remote_state.items():
                if str(v).isdigit() and (k not in state or int(v) > int(state[k])):
                    state[k] = v
        remote_ledger, _ = gh_get('ledger.json')
    except Exception as e:
        log(f"remote state pull failed ({e}); aborting run to stay safe")
        sys.exit(0)
    ledger = dict(remote_ledger or {})

    counters = {'sent': 0}
    total = 0
    try:
        for acct in ACCOUNTS:
            try:
                total += process_account(acct, state, ledger, counters)
            except PushFailed as e:
                log(f"FATAL: {e} - aborting run (chain pauses, schedule retries)")
                sys.exit(1)
            except Exception as e:
                log(f"  account error @{acct['handle']}: {e}")
    except Exception as e:
        log(f"unexpected error: {e}")
    log(f"Done: {total} new tweets posted")


if __name__ == '__main__':
    main()
