#!/usr/bin/env python3
"""
Twitter -> Telegram Auto-Translator (standalone, GitHub Actions edition)
=========================================================================
Runs every ~5 minutes via GitHub Actions cron. No external dependencies
(standard library + curl only). Translation: free Google endpoint.

Accounts:
  - hey_itsmyturn   (all tweets)
  - MilitaryIr      (all tweets)
  - Osint613        (only Iran-related tweets, keyword-filtered)
  - no_itsmyturn    (all tweets)
  - _KhodaBiamorz   (all tweets, Persian source)
  - cicikhanoom     (all tweets, Persian source)
  - rezasadx        (all tweets, Persian source)
  - shahryarshefigh (all tweets, Persian source)
  - baaaarzaaaakh   (all tweets, Persian source)

State:   state.json next to this file (committed back to the repo by the workflow)
"""
import json
import os
import re
import subprocess
import sys
import time
import html
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta

# ------------------- CONFIG -------------------
# BOT_TOKEN comes from the GitHub Actions secret "BOT_TOKEN" (env var).
# For local testing, set the BOT_TOKEN environment variable manually.
BOT_TOKEN = os.environ.get('BOT_TOKEN', '')
CHAT_ID = "-1003997031832"
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0"
IR_TZ = timezone(timedelta(hours=3, minutes=30))
FA = str.maketrans('0123456789', '۰۱۲۳۴۵۶۷۸۹')

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

# ------------------- HELPERS -------------------


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def fa_digits(s):
    return str(s).translate(FA)


def curl(url, referer=None, timeout=30):
    """Fetch a URL via curl (handles gzip, redirects, cloud-friendly)."""
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


# ------------------- STATE -------------------

def load_state():
    try:
        with open(STATE_FILE, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=1)


# ------------------- TWITTER -------------------

def fetch_profile_ids(handle):
    raw = curl(f"https://x.com/{handle}").decode('utf-8', errors='ignore')
    ids = re.findall(rf'href="/{re.escape(handle)}/status/(\d+)"', raw,
                     re.IGNORECASE)
    seen, uniq = set(), []
    for i in ids:
        if i not in seen:
            seen.add(i)
            uniq.append(i)
    return uniq


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

def process_account(acct, state):
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

    if handle not in state:
        # first run: seed state at NEWEST tweet id (max by value, not page order),
        # send nothing (no flood)
        state[handle] = max(ids, key=int)
        save_state(state)
        log(f"  first run: seeded state at {state[handle]}")
        return 0

    last_id = state[handle]
    new_ids = sorted([i for i in ids if int(i) > int(last_id)], key=int)
    # flood guard: never send more than 8 tweets in one run
    if len(new_ids) > 8:
        log(f"  flood guard: {len(new_ids)} new ids, sending only newest 8")
        new_ids = new_ids[-8:]
    sent = 0
    failed = False
    for tid in new_ids:
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
            log(f"  SENT {tid}: {tw['text'][:60]!r}")
            state[handle] = tid
            save_state(state)
        else:
            log(f"  send FAILED {tid}: {res.get('description')}")
            failed = True
            break
        time.sleep(2.5)
    if not failed and new_ids:
        state[handle] = new_ids[-1]
        save_state(state)
    if not new_ids:
        log("  no new tweets")
    return sent


def main():
    if not BOT_TOKEN:
        log("FATAL: BOT_TOKEN env var is not set (GitHub secret missing)")
        sys.exit(1)
    log("Twitter->Telegram monitor run starting")
    state = load_state()
    total = 0
    for acct in ACCOUNTS:
        try:
            total += process_account(acct, state)
        except Exception as e:
            log(f"  account error @{acct['handle']}: {e}")
    log(f"Done: {total} new tweets posted")
    # always exit 0 so the workflow stays green even on partial network errors
    sys.exit(0)


if __name__ == '__main__':
    main()
