#!/usr/bin/env python3
"""Graph outside finds: search what people already built, check it, and use it inside the project.
All network access: no login, stdlib urllib, 10 second timeout, User-Agent "graph-skill", fail soft.
Everything fetched is untrusted data: it is shown as short plain text and never run or followed."""
from __future__ import annotations
import calendar, contextlib, json, math, os, re, shutil, stat, subprocess, sys, time, urllib.error, urllib.parse, urllib.request
from pathlib import Path

TIMEOUT = 10
UA = 'graph-skill'
BAR_STARS, BAR_MENTIONS, WINDOW_DAYS = 200, 3, 90
MAX_BYTES = 2_000_000
CLONE_TIMEOUT, CLONE_MAX = 60, 50 * 1024 * 1024
MAX_ENRICH = 12
REPO_RE = re.compile(r'^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$')
PART_RE = re.compile(r'^[A-Za-z0-9_-]{1,40}$')
GH_LINK = re.compile(r'github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)', re.I)
NOT_OWNERS = {'topics', 'search', 'orgs', 'sponsors', 'features', 'marketplace', 'settings', 'about', 'login', 'explore',
              'collections', 'trending', 'apps', 'notifications', 'pulls', 'issues', 'users', 'enterprise', 'pricing', 'readme'}
MARKER = '.graph-outside-owner'
VENDOR, TMP = Path('.graph/vendor'), Path('.graph/tmp')
SKILL_HOMES = (Path('.claude/skills'), Path('.agents/skills'))

# ---------- small helpers ----------
def now(): return time.time()

def clean(text, n=160):
    """Fetched text as one short plain line: no control or hidden characters."""
    t = re.sub(r'[\x00-\x1f\x7f​-‏ -‮⁠-⁤﻿]|[\U000e0000-\U000e007f]', ' ', str(text or ''))
    t = ' '.join(t.split())
    return t[:n]

def parse_iso(s):
    try: return calendar.timegm(time.strptime((s or '')[:19], '%Y-%m-%dT%H:%M:%S'))
    except (ValueError, TypeError): return None

def days_ago(s):
    t = parse_iso(s)
    return None if t is None else max(0, int((now() - t) // 86400))

def date_ago(days): return time.strftime('%Y-%m-%d', time.gmtime(now() - days * 86400))

def paint(text, code):
    if os.environ.get('NO_COLOR') or not sys.stdout.isatty(): return text
    return f'\033[{code}m{text}\033[0m'

def valid_repo(repo):
    return bool(REPO_RE.match(repo or '')) and all(x not in ('.', '..') and not x.startswith('-') for x in repo.split('/'))

def vendor_dir(repo): return VENDOR / repo.replace('/', '__')

# ---------- network (the only place that touches urllib) ----------
def fetch(url):
    """GET one url. Return (status, text, headers). status is None when the network failed. Never raises."""
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, r.read(MAX_BYTES).decode('utf-8', errors='replace'), dict(r.headers)
    except urllib.error.HTTPError as e:
        body = ''
        with contextlib.suppress(Exception): body = e.read(2000).decode('utf-8', errors='replace')
        return e.code, body, dict(e.headers or {})
    except Exception as e:  # URLError, timeout, bad url: fail soft
        return None, type(e).__name__, {}

def get_json(url, state=None):
    """Return (data, error). On GitHub rate limits wait once, then give up for the rest of this search."""
    for attempt in (0, 1, 2):
        code, text, hdr = fetch(url)
        if code in (403, 429) and 'api.github.com' in url:
            low = {k.lower(): v for k, v in hdr.items()}
            if state is not None and attempt >= 1: state['limited'] = True
            if attempt == 0:
                wait = 2.0
                with contextlib.suppress(ValueError, TypeError):
                    if low.get('retry-after'): wait = float(low['retry-after'])
                    elif low.get('x-ratelimit-reset') and low.get('x-ratelimit-remaining') == '0': wait = float(low['x-ratelimit-reset']) - now()
                time.sleep(max(0.0, min(wait, 5.0))); continue
            return None, 'GitHub rate limit'
        if code == 422 and 'arctic-shift' in url and 'imeout' in text and attempt < 2: time.sleep(2.0 * (attempt + 1)); continue  # the archive says "slow down a bit": retry twice
        if code is None: return None, f'network failed ({text})'
        if code != 200: return None, f'HTTP {code}'
        try: return json.loads(text), None
        except ValueError: return None, 'bad JSON'
    return None, 'failed'

# ---------- search ----------
def repo_key(owner, name):
    name = re.sub(r'\.git$', '', name.rstrip('.'), flags=re.I).rstrip('.')
    if not owner or not name or owner.lower() in NOT_OWNERS: return None
    key = f'{owner}/{name}'
    return key if valid_repo(key) else None

def repos_in(text):
    return {k.lower(): k for m in GH_LINK.finditer(text or '') for k in [repo_key(*m.groups())] if k}

REDDIT = 'https://arctic-shift.photon-reddit.com/api'
MAX_SUBS = 3

def reddit_subs(query):
    """Text search needs a subreddit or author, so find subreddits first. The subreddit endpoint matches name prefixes only,
    so try each query word (and the words joined). Rank by subscribers, skip adult ones. Return (names, error)."""
    words = [w for w in re.findall(r'[a-z0-9]+', query.lower()) if len(w) >= 2][:4] or [re.sub(r'[^a-z0-9]+', '', query.lower())[:30]]
    prefixes = [x for x in dict.fromkeys(words + ([''.join(words)] if len(words) > 1 else [])) if x]
    if not prefixes: return [], 'the query has no usable words'
    found, errs = {}, []
    for pre in prefixes:
        data, err = get_json(f'{REDDIT}/subreddits/search?subreddit_prefix={urllib.parse.quote(pre)}&limit=10')
        if err: errs.append(f'subreddits {err}'); continue
        for it in (data.get('data') if isinstance(data, dict) else None) or []:
            if isinstance(it, dict) and it.get('display_name') and not it.get('over18') and re.match(r'^[A-Za-z0-9_]{2,40}$', str(it['display_name'])):
                found[str(it['display_name'])] = int(it.get('subscribers') or 0)
    return sorted(found, key=lambda n: -found[n])[:MAX_SUBS], '; '.join(errs) or None

def search_reddit(query):
    """Posts and comments of the last 90 days inside the top subreddits for the query. Return (items, error). Each item is (author, text).
    arctic-shift refuses a text search without a subreddit or author, so no subreddit means no search."""
    subs, serr = reddit_subs(query)
    after, q, texts, errs = date_ago(WINDOW_DAYS), urllib.parse.quote_plus(query), [], []
    if not subs: return texts, serr or 'no subreddit matches the query words'
    for sub in subs:
        for kind, field in (('posts', 'query'), ('comments', 'body')):
            data, err = get_json(f'{REDDIT}/{kind}/search?{field}={q}&subreddit={sub}&after={after}&limit=50')
            if err:
                errs.append(f'{sub} {kind} {err}')
                if '429' in err: return texts, '; '.join(errs)  # rate limited: stop, keep what came back
                continue
            if isinstance(data, dict) and data.get('error'): errs.append(f'{sub} {kind} {clean(data["error"], 60)}'); continue
            for it in (data.get('data') if isinstance(data, dict) else None) or []:
                if isinstance(it, dict):
                    row = (str(it.get('author') or ''), ' '.join(str(it.get(k) or '') for k in ('title', 'selftext', 'body', 'url')))
                    if row not in texts: texts.append(row)
    return texts, '; '.join(errs + ([serr] if serr else [])) or None

def search_hn(query):
    since = int(now() - WINDOW_DAYS * 86400)
    data, err = get_json('https://hn.algolia.com/api/v1/search?query=%s&tags=(story,comment)&numericFilters=created_at_i>%d&hitsPerPage=50' % (urllib.parse.quote_plus(query), since))
    texts = []
    for it in (data.get('hits') if isinstance(data, dict) else None) or []:
        if isinstance(it, dict): texts.append((str(it.get('author') or ''), ' '.join(str(it.get(k) or '') for k in ('title', 'url', 'story_text', 'comment_text'))))
    return texts, err

def meta_from(item):
    if not isinstance(item, dict) or not item.get('full_name'): return None
    lic = item.get('license')
    return {'stars': int(item.get('stargazers_count') or 0), 'pushed_at': item.get('pushed_at'), 'archived': bool(item.get('archived')),
            'license': clean((lic or {}).get('spdx_id') if isinstance(lic, dict) else lic, 30) or 'none', 'description': clean(item.get('description'), 140)}

def search_github(query, st):
    """Repo search sorted by stars, pushed in the last 90 days. Return {repo: meta}, errors."""
    found, errs = {}, []
    since = date_ago(WINDOW_DAYS)
    for words in (query, f'{query} skill', f'{query} claude skill'):
        if st.get('limited'): break
        q = urllib.parse.quote_plus(f'{words} pushed:>{since}')
        data, err = get_json(f'https://api.github.com/search/repositories?q={q}&sort=stars&order=desc&per_page=10', st)
        if err: errs.append(err); continue
        for it in (data.get('items') if isinstance(data, dict) else None) or []:
            m = meta_from(it)
            key = repo_key(*(str(it.get('full_name')).split('/', 1) + [''])[:2]) if m else None
            if key: found.setdefault(key.lower(), (key, m))
    return found, sorted(set(errs))

def enrich(key, st):
    """Metadata of one repo, or None."""
    if st.get('limited'): return None
    data, err = get_json(f'https://api.github.com/repos/{key}', st)
    return meta_from(data) if not err and isinstance(data, dict) else None

def gh_parts(url):
    m = GH_LINK.search(url or '')
    return repo_key(*m.groups()) if m else None

# ---------- trust bar and rank ----------
MAX_MENTIONS = 10

def total_mentions(c): return min(MAX_MENTIONS, sum(max(0, int(v)) for v in (c.get('mentions') or {}).values()))

def judge(c):
    """Apply the trust bar. Return (passes, reason). Popular + active: 200 stars or 3 mentions, and pushed within 90 days."""
    m = total_mentions(c)
    if not c.get('repo'):
        return (m >= BAR_MENTIONS, 'idea only' if m >= BAR_MENTIONS else f'only {m} mention(s), needs {BAR_MENTIONS}')
    if c.get('archived'): return False, 'archived'
    if c.get('pushed_at') is None: return False, 'could not check activity'
    age = days_ago(c['pushed_at'])
    if age is None or age > WINDOW_DAYS: return False, f'not updated in {WINDOW_DAYS} days'
    if (c.get('stars') or 0) < BAR_STARS and m < BAR_MENTIONS: return False, f'under {BAR_STARS} stars and under {BAR_MENTIONS} mentions'
    if (c.get('stars') or 0) < 1: return False, 'no stars yet, mentions alone are not enough'
    return True, 'ok'

def score(c):
    """Mentions count most, then stars (log), then recency."""
    age = days_ago(c.get('pushed_at')) if c.get('pushed_at') else None
    rec = 0.0 if age is None else max(0.0, 1 - age / WINDOW_DAYS)
    return round(total_mentions(c) * 3 + math.log10((c.get('stars') or 0) + 1) * 1.5 + rec, 3)

def finalize(doc):
    for c in doc['candidates']:
        c['passes'], c['why'] = judge(c); c['score'] = score(c)
    shown = sorted((c for c in doc['candidates'] if c['passes']), key=lambda c: -c['score'])
    doc['hidden'] = len(doc['candidates']) - len(shown)
    doc['top'] = shown[:3]
    doc['recommended'] = next((c['repo'] for c in shown[:3] if c.get('repo')), None)
    return doc

def finds_path(run_dir, part): return run_dir / 'finds' / f'{part}.json'

def save_doc(run_dir, part, doc):
    p = finds_path(run_dir, part); p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix('.tmp'); tmp.write_text(json.dumps(doc, indent=2) + '\n', encoding='utf-8'); os.replace(tmp, p)

def load_doc(run_dir, part):
    try: return json.loads(finds_path(run_dir, part).read_text(encoding='utf-8'))
    except (OSError, ValueError): return None

def new_cand(repo=None, url=None):
    return {'repo': repo, 'url': url or (f'https://github.com/{repo}' if repo else None), 'stars': None, 'pushed_at': None, 'license': None,
            'description': '', 'archived': False, 'mentions': {}, 'notes': []}

def do_find(run_dir, part, query):
    """Search Reddit, then HN, then GitHub. Write the finds file. Return the doc."""
    st, sources, cands = {}, {}, {}
    mention_rows = []
    for name, fn in (('reddit', search_reddit), ('hn', search_hn)):
        texts, err = fn(query)
        sources[name] = f'failed: {err}' if err and not texts else ('ok' + (f' (partly: {err})' if err else ''))
        seen = {}
        for i, (author, t) in enumerate(texts):
            who = author.lower() if author and author not in ('[deleted]', 'None') else f'?{i}'
            for low, key in repos_in(t).items():
                if who == key.split('/')[0].lower(): continue  # the owner talking about their own repo
                c = cands.setdefault(low, new_cand(key)); seen.setdefault(low, set()).add(who)
                c['mentions'][name] = len(seen[low])  # distinct people, not raw links
    found, errs = search_github(query, st)
    sources['github'] = ('ok' if not errs else f"failed: {', '.join(errs)}") if found or not errs else f"failed: {', '.join(errs)}"
    if errs and found: sources['github'] = f"ok (partly: {', '.join(errs)})"
    for low, (key, m) in found.items():
        c = cands.setdefault(low, new_cand(key)); c.update(m)
    todo = sorted((c for c in cands.values() if c['pushed_at'] is None), key=lambda c: -total_mentions(c))
    for c in todo[:MAX_ENRICH]:
        m = enrich(c['repo'], st)
        if m: c.update(m)
        else: c['notes'].append('metadata not available')
    if st.get('limited'): sources['github'] = (sources['github'] + ' ; rate limited, some repos not checked').strip()
    doc = {'part': part, 'query': clean(query, 200), 'at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'sources': sources, 'candidates': list(cands.values())}
    finalize(doc); save_doc(run_dir, part, doc)
    return doc

def do_candidate(run_dir, part, url, mentions, note):
    """Add a find the model made through the host's web search. GitHub urls are enriched. Return the doc."""
    if not re.match(r'^https?://[^\s]+$', url or ''): raise ValueError('--url must be an http or https link.')
    if mentions < 0: raise ValueError('--mentions must be 0 or more.')
    mentions = min(mentions, MAX_MENTIONS)
    doc = load_doc(run_dir, part) or {'part': part, 'query': '', 'at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'sources': {}, 'candidates': []}
    repo = gh_parts(url) if urllib.parse.urlparse(url).netloc.lower().endswith('github.com') else None
    host = urllib.parse.urlparse(url).netloc.lower()
    src = 'x' if host.split(':')[0] in ('x.com', 'twitter.com', 'api.fxtwitter.com', 'fxtwitter.com', 'www.x.com', 'www.twitter.com') else 'web'
    key = (repo or url).lower()
    c = next((x for x in doc['candidates'] if (x.get('repo') or x.get('url') or '').lower() == key), None)
    if c is None:
        c = new_cand(repo, None if repo else url); doc['candidates'].append(c)
        if repo:
            m = enrich(repo, {})
            if m: c.update(m)
            else: c['notes'].append('metadata not available')
    c['mentions'][src] = c['mentions'].get(src, 0) + mentions
    if note: c['notes'].append(clean(note, 200))
    if not repo and not c.get('description'): c['description'] = clean(note, 140)
    finalize(doc); save_doc(run_dir, part, doc)
    return doc

def show_doc(doc, run, cmd_prefix):
    """Plain, short lines for the user. Fetched text is cleaned."""
    L = [paint(f"PART: {doc['part']}", '1') + (f"   searched: {clean(doc.get('query'), 80)}" if doc.get('query') else '')]
    L.append('Sources: ' + ', '.join(f'{k} {v}' for k, v in doc.get('sources', {}).items()) if doc.get('sources') else 'Sources: added by hand')
    top = doc.get('top') or []
    if not top: L.append(paint('Nothing good found for this part.', '33'))
    for i, c in enumerate(top, 1):
        name = c.get('repo') or clean(c.get('url'), 70)
        tag = paint('  RECOMMENDED', '1;32') if c.get('repo') and c['repo'] == doc.get('recommended') else ('  idea only, no repo' if not c.get('repo') else '')
        L.append(f'{i}. {paint(name, "1")}{tag}')
        m = c.get('mentions') or {}
        ment = ', '.join(f'{k} {v}' for k, v in sorted(m.items()) if v) or 'none'
        if c.get('repo'):
            age = days_ago(c['pushed_at']) if c.get('pushed_at') else None
            L.append(f"   stars {c.get('stars')}  |  updated {'unknown' if age is None else f'{age} days ago'}  |  {c.get('license') or '?'}  |  mentions {total_mentions(c)} ({ment})")
        else: L.append(f'   mentions {total_mentions(c)} ({ment})')
        d = c.get('description') or (c.get('notes') or [''])[-1]
        if d: L.append(f'   {clean(d, 110)}')
    L.append(f"hidden: {doc.get('hidden', 0)} below bar")
    if doc.get('recommended'):
        L.append(paint(f"DO THIS: scan the top pick.  {cmd_prefix} scan --run {run} --part {doc['part']} --pick {doc['recommended']}", '1;36'))
    else: L.append(paint(f"DO THIS: add finds from X or web with {cmd_prefix} candidate, or log that none were found.", '1;36'))
    return '\n'.join(L)

# ---------- safety scan ----------
DOC_EXT = {'.md', '.mdc', '.txt', '.json', '.yml', '.yaml', '.toml', '.html', '.htm', '.xml', '.cfg', '.ini'}
BIN_EXT = {'.exe', '.dll', '.so', '.dylib', '.bin', '.com', '.msi', '.app', '.jar', '.class', '.pyc', '.o', '.a'}
MAGIC = (b'\x7fELF', b'MZ', b'\xfe\xed\xfa', b'\xcf\xfa\xed\xfe', b'\xca\xfe\xba\xbe', b'\xce\xfa\xed\xfe')
SKIP_DIRS = {'.git', 'node_modules', '__pycache__', '.venv', 'venv'}  # never copied; a repo that ships them is blocked
MAX_FILES, MAX_READ = 4000, 8_000_000
I = re.I
CODE_RULES = [  # (level, rule, regex)
    ('warn', 'network call', re.compile(r'\b(urllib|requests\.(get|post|put)|httpx|aiohttp|http\.client|socket\.socket|fetch\s*\(|XMLHttpRequest|axios|node-fetch|net\.connect|WebSocket\s*\()|\b(curl|wget|Invoke-WebRequest|iwr)\b|https?://(?!github\.com|www\.w3\.org)', I)),
    ('warn', 'runs shell or code', re.compile(r'\b(os\.system|os\.popen|subprocess|child_process|execSync|spawnSync|Runtime\.getRuntime|popen|shell=True)\b|\beval\s*\(|\bexec\s*\(|\bnew Function\s*\(|\bspawn\s*\(|\bexecFile\s*\(')),
    ('block', 'reads a secret place (ssh, cloud, keychain, browser)', re.compile(r'(~|\$HOME|\bHOME\b|Path\.home\(\)|expanduser|%USERPROFILE%)[^\n]{0,40}(\.ssh|\.aws|\.gnupg|\.kube|\.docker|Keychains?|\.netrc|\.npmrc|\.pypirc|Chrome|Firefox|Brave|Edge|Login Data|Cookies)|/\.(ssh|aws|gnupg)\b|security\s+find-(generic|internet)-password|\bid_(rsa|ed25519)\b|\.config/gcloud|\.git-credentials', I)),
    ('block', 'reads the host config of an agent', re.compile(r'(~|\$HOME|\bHOME\b|Path\.home\(\)|expanduser|%USERPROFILE%|homedir\(\))[^\n]{0,40}\.(claude|codex|agents|config/opencode|cursor)\b|["\']~/\.(claude|codex)\b')),
    ('warn', 'touches .env or tokens', re.compile(r'(\.env\b|\b(API_KEY|SECRET_KEY|ACCESS_TOKEN|AUTH_TOKEN|GITHUB_TOKEN|OPENAI_API_KEY|ANTHROPIC_API_KEY)\b|process\.env\b|os\.environ)')),
    ('warn', 'writes outside the repo', re.compile(r'(write|open|copy|mkdir|rm|mv|cp|>>?|tee|unlink|rmtree)[^\n]{0,60}(~/|\$HOME|Path\.home\(\)|expanduser|%USERPROFILE%|/etc/|/usr/|/var/|\.\./\.\.)', I)),
    ('block', 'hides code (decode then run)', re.compile(r'(eval|exec|Function)\s*\(\s*[^\n]{0,40}(atob|b64decode|base64|fromCharCode|decompress|unhexlify|codecs\.decode)|(base64|b64decode)[^\n]{0,60}\|\s*(sh|bash|python3?|node)\b', I)),
    ('warn', 'pipes a download into a shell', re.compile(r'(curl|wget)[^\n|]*\|\s*(sudo\s+)?(sh|bash|zsh|python3?|node)\b', I)),
]
BLOB = re.compile(r'[A-Za-z0-9+/=_-]{300,}')
INJECT = [
    re.compile(r'\bignore\s+(all\s+)?(the\s+)?(previous|prior|above|earlier|your)\s+(instructions?|rules?|prompts?|messages?)', I),
    re.compile(r'\bdisregard\s+(all\s+)?(the\s+)?(previous|prior|above|earlier|your|any)\s+[a-z ]{0,20}(instructions?|rules?)', I),
    re.compile(r"\b(do not|don't|dont|never)\s+(tell|inform|show|reveal\s+(this\s+)?to|mention\s+(this\s+)?to)\s+the\s+(user|human|owner)", I),
    re.compile(r'\bwithout\s+(telling|informing|asking)\s+the\s+(user|human|owner)', I),
    re.compile(r'\b(forget|override|discard|drop)\s+(every|all|any|the)?\s*(earlier|previous|prior|above|existing|your)?\s*(guidelines?|instructions?|rules?|constraints?)', I),
    re.compile(r'\bkeep\s+this\s+(between\s+us|secret|hidden|quiet)', I),
    re.compile(r"\binclude\s+the\s+user'?s?\s+(\.env|secrets?|tokens?|credentials?|ssh|api keys?)", I),
    re.compile(r'\bsend\b[^\n]{0,100}\bto\s+https?://', I),
    re.compile(r'\b(upload|post|exfiltrate)\b[^\n]{0,80}\b(secrets?|tokens?|credentials?|api keys?|\.env|ssh)\b[^\n]{0,60}\bhttps?://', I),
]
SECRET_TOK = re.compile(r'(\.ssh\b|\bid_(rsa|ed25519|ecdsa)\b|\.aws\b|\.gnupg|\.kube\b|\.netrc|\.npmrc|\.pypirc|\.git-credentials|Login Data|Keychains?\b|\.config/gcloud|~/\.(claude|codex)\b|\.claude/settings|\.codex/(auth|config))', I)
NET_TOK = re.compile(r'\b(curl|wget|nc|ncat|Invoke-WebRequest|iwr|urllib|requests|httpx|aiohttp|http\.client|XMLHttpRequest|axios|fetch)\b|https?://(?!github\.com|www\.w3\.org)', I)
SHELL_LINE = re.compile(r'^\s*(\$\s|sudo\s|rm\s|chmod\s|bash\s|sh\s|zsh\s|python3?\s|node\s|npx?\s|pip3?\s|brew\s|curl\s|wget\s)', I)
COMMENT = re.compile(r'<!--(.*?)-->', re.S)
COMMENT_BAD = re.compile(r'\b(ignore|instructions?|you must|you should|assistant|system prompt|do not tell|run|execute|curl|wget|send|secret|token|password|always|never)\b', I)
HIDDEN_CH = re.compile(r'[​-‏‪-‮⁠-⁤﻿]|[\U000e0000-\U000e007f]')
AGENT_FILES = {'skill.md', 'agents.md', 'agent.md', 'claude.md', 'claude.local.md', 'gem' + 'ini.md', '.cursorrules', '.windsurfrules', 'copilot-instructions.md'}
AGENT_DIRS = {'prompts', 'commands', 'agents'}
LEGAL_FILE = re.compile(r'^(licen[sc]e|copying|notice|copyright|unlicense)([-_][a-z0-9_-]*)?(\.(txt|md))?$', I)  # any other extension (license.js) is code
MD_LINK = re.compile(r'\[[^\]\n]*\]\(\s*<?https?://[^)\n]*\)|^\s*\[[^\]\n]+\]:\s*<?https?://\S+|<https?://[^>\s]+>')
LIFECYCLE = ('preinstall', 'install', 'postinstall', 'prepare', 'prepack', 'prepublish', 'prepublishOnly')

def add(F, level, rule, rel, line=0): F.append({'level': level, 'rule': rule, 'file': clean(rel, 120), 'line': line})

def line_of(text, idx): return text.count('\n', 0, idx) + 1

def scan_frontmatter(F, rel, text):
    m = re.match(r'^---\s*\n(.*?)\n---', text, re.S)
    if not m: return
    lines = m.group(1).splitlines()
    for i, ln in enumerate(lines):
        key = ln.split(':', 1)[0].strip().lower() if ':' in ln and not ln.startswith((' ', '\t', '-')) else ''
        if key == 'hooks': add(F, 'block', 'SKILL.md frontmatter sets hooks', rel, i + 2)
        if key in ('allowed-tools', 'allowed_tools'):
            val = ln.split(':', 1)[1]
            for nxt in lines[i + 1:]:
                if nxt.startswith((' ', '\t', '-')): val += ' ' + nxt
                else: break
            if '*' in val or re.search(r'\bBash\b(?!\s*\()', val): add(F, 'block', 'SKILL.md frontmatter allows tools with a wildcard', rel, i + 2)

def in_skill_folder(root, path):
    """True if the file sits in or under a folder (up to the scan root) that holds a SKILL.md. The agent loads those files."""
    d = Path(path).parent; root = Path(root)
    while True:
        with contextlib.suppress(OSError):
            if any(x.name.lower() == 'skill.md' for x in d.iterdir()): return True
        if d == root or d == d.parent: return False
        d = d.parent

def is_agent_file(rel, root=None, path=None):
    parts = rel.lower().split('/')
    if parts[-1] in AGENT_FILES or parts[-1].endswith('.mdc') or any(x in AGENT_DIRS for x in parts[:-1]): return True
    return bool(root and path and parts[-1].endswith(('.md', '.txt')) and in_skill_folder(root, path))

def scan_file(F, root, path):
    rel = path.relative_to(root).as_posix()
    ext = path.suffix.lower()
    try:
        if path.stat().st_size > MAX_READ: add(F, 'block', f'file over {MAX_READ // 1000000} MB, cannot be scanned in full', rel); return
        raw = path.read_bytes()
    except OSError: add(F, 'block', 'file could not be read, so it was not scanned', rel); return
    name = path.name.lower()
    if ext in BIN_EXT or raw.startswith(MAGIC) or b'\x00' in raw[:4096]:
        add(F, 'block' if ext in BIN_EXT - {'.pyc', '.class'} or raw.startswith(MAGIC) else 'warn', 'binary file', rel); return
    text = raw.decode('utf-8', errors='replace')
    if name == 'skill.md': scan_frontmatter(F, rel, text)
    if name == 'package.json':
        with contextlib.suppress(ValueError):
            scripts = (json.loads(text) or {}).get('scripts') or {}
            for k in LIFECYCLE:
                if isinstance(scripts.get(k), str):
                    bad = re.search(r'curl|wget|https?://|node\s+-e|eval|base64|\|\s*(sh|bash)', scripts[k], I)
                    add(F, 'block' if bad else 'warn', f'package.json runs "{k}" on install' + (' and it fetches or hides code' if bad else ''), rel, line_of(text, text.find(k)))
    if name == 'setup.py': add(F, 'warn', 'setup.py runs code on install', rel)
    if name == 'pyproject.toml' and re.search(r'cmdclass|build-backend\s*=\s*["\'](?!setuptools|hatchling|flit|poetry|pdm)', text): add(F, 'warn', 'pyproject build hook', rel)
    legal = bool(LEGAL_FILE.match(name))
    agent = is_agent_file(rel, root, path)
    is_code = ext not in DOC_EXT and not legal
    strict = 'block' if (agent or is_code) else 'warn'  # a bare injection phrase in plain docs is a warning. Hidden text and exfil stay BLOCK everywhere
    if ext in DOC_EXT or name == 'skill.md' or legal:
        for rx in INJECT:
            m = rx.search(text)
            if m: add(F, strict, 'prompt-injection phrase', rel, line_of(text, m.start())); break
        for m in COMMENT.finditer(text):
            if COMMENT_BAD.search(m.group(1)): add(F, 'block', 'hidden html comment with instructions', rel, line_of(text, m.start())); break
        m = HIDDEN_CH.search(text)
        if m and (ext in ('.md', '.mdc', '.txt') or legal): add(F, 'block', 'hidden unicode characters in text', rel, line_of(text, m.start()))
    is_prose = ext in ('.md', '.mdc', '.txt') or name == 'skill.md' or legal
    seen, has_secret, has_net = set(), None, False
    prose_rules = [r for r in CODE_RULES if r[1] == 'pipes a download into a shell']  # secret places in prose are judged per file below
    for i, ln in enumerate(text.splitlines(), 1):  # every line, whole, no length cut-off
        if BLOB.search(ln) and 'blob' not in seen: seen.add('blob'); add(F, 'warn', 'long encoded or obfuscated blob', rel, i)
        if has_secret is None and SECRET_TOK.search(ln): has_secret = i
        nl = ln
        if legal: nl = ''  # license text has links, they are not network use
        elif not agent and not is_code: nl = MD_LINK.sub(' ', ln)  # markdown links in plain docs are not network use
        if not has_net and NET_TOK.search(nl): has_net = True
        if is_code: rules = CODE_RULES
        elif is_prose: rules = prose_rules
        else: rules = []
        for level, rule, rx in rules:
            if rule not in seen and rx.search(ln): seen.add(rule); add(F, level, rule, rel, i)
        if name == 'skill.md' and 'skill-run' not in seen and SHELL_LINE.search(ln): seen.add('skill-run'); add(F, 'warn', 'SKILL.md has a shell command', rel, i)
        if name == 'skill.md' and 'skill-url' not in seen and NET_TOK.search(ln): seen.add('skill-url'); add(F, 'warn', 'SKILL.md has a URL or network command', rel, i)
    if has_secret and has_net: add(F, 'block', 'names a secret place and uses the network in one file', rel, has_secret)
    elif has_secret and (is_code or is_prose) and 'reads a secret place (ssh, cloud, keychain, browser)' not in seen: add(F, 'warn', 'names a secret place', rel, has_secret)

def scan_tree(root):
    """Static scan of every text file. Nothing in the repo is run. Return findings and a count of files."""
    F, n = [], 0
    root = Path(root)
    hooks = root / '.git' / 'hooks'
    if hooks.is_dir():
        for h in hooks.iterdir():
            if h.is_file() and not h.name.endswith('.sample'): add(F, 'warn', 'git hook', f'.git/hooks/{h.name}')
    for sub in ('.husky', '.githooks', '.pre-commit-config.yaml', 'lefthook.yml', 'lefthook.yaml'):
        if (root / sub).exists(): add(F, 'warn', 'git hook config', sub)
    real = os.path.realpath(root)
    for dp, dirs, files in os.walk(root, followlinks=False):
        for d in dirs:
            if d in SKIP_DIRS and d != '.git': add(F, 'block', f'ships a {d} folder (not scanned, never used)', (Path(dp) / d).relative_to(root).as_posix())
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for nm in sorted(files + [d for d in dirs if (Path(dp) / d).is_symlink()]):
            p = Path(dp) / nm
            if p.is_symlink():
                tgt = os.path.realpath(p)
                add(F, 'warn' if tgt.startswith(real + os.sep) else 'block', 'symlink' + ('' if tgt.startswith(real + os.sep) else ' that points outside the repo'), p.relative_to(root).as_posix()); continue
            if not p.is_file(): continue
            n += 1
            if n > MAX_FILES: add(F, 'block', f'more than {MAX_FILES} files, not fully scanned', '.'); return F, n
            scan_file(F, root, p)
    return F, n

def verdict(F): return 'block' if any(f['level'] == 'block' for f in F) else 'warn' if F else 'clean'

def tree_size(root):
    tot = 0
    for dp, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d != '.git']  # only what is scanned counts, not git's own data
        for f in files:
            with contextlib.suppress(OSError): tot += os.lstat(os.path.join(dp, f)).st_size
    return tot

def rmtree(p):
    def onerr(fn, path, _):
        with contextlib.suppress(OSError): os.chmod(path, stat.S_IWRITE); fn(path)
    shutil.rmtree(p, onerror=onerr)

def git_env():
    # Scrubbed env: no user git config (insteadOf rewrites, filters), no ssh agent, no tokens.
    env = {k: v for k, v in os.environ.items() if k in ('PATH', 'LANG', 'SYSTEMROOT', 'COMSPEC', 'PATHEXT', 'TMPDIR', 'TEMP', 'TMP', 'HTTPS_PROXY', 'HTTP_PROXY', 'NO_PROXY', 'SSL_CERT_FILE')}
    env.update(GIT_TERMINAL_PROMPT='0', GCM_INTERACTIVE='never', GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1', GIT_LFS_SKIP_SMUDGE='1', GIT_ASKPASS='', SSH_ASKPASS='')
    return env

GIT_SAFE = ['git', '-c', 'credential.helper=', '-c', 'core.sshCommand=false', '-c', 'core.hooksPath=' + os.devnull, '-c', 'protocol.ext.allow=never']

def git(args, cwd=None, timeout=CLONE_TIMEOUT):
    """Run one git command with the scrubbed env. Return (stdout, error text or None)."""
    try: r = subprocess.run(GIT_SAFE + list(args), capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout, env=git_env(), stdin=subprocess.DEVNULL, cwd=cwd)
    except subprocess.TimeoutExpired: return '', f'git took longer than {timeout} seconds'
    except OSError as e: return '', f'git is not available ({type(e).__name__})'
    return (r.stdout, None) if r.returncode == 0 else ('', 'git failed: ' + clean(r.stderr, 120))

def clone(repo, dest, sparse=False):
    """Shallow clone of a public repo into dest. Return None or an error text. No hooks, no prompts, no credentials.
    sparse=True is a partial clone (no file contents yet, only the top folder checked out)."""
    extra = ['--filter=blob:none', '--sparse'] if sparse else []
    _, err = git(['clone', '--depth', '1', '--no-tags', '--single-branch', *extra, '--', f'https://github.com/{repo}.git', str(dest)])
    return err.replace('git failed: ', 'clone failed: ') if err and err.startswith('git failed') else err

def sparse_skill_dirs(dest, only=None):
    """In a partial clone: folders (up to 3 levels down, max 10) that hold a SKILL.md, or just `only`. Folder names are validated."""
    if only: return [only]
    out, err = git(['ls-tree', '-r', '--name-only', 'HEAD'], cwd=dest)
    if err: return []
    dirs = []
    for ln in out.splitlines():
        parts = ln.split('/')
        if parts[-1] == 'SKILL.md' and len(parts) - 1 <= 3 and all(re.match(r'^[A-Za-z0-9_. -]{1,80}$', x) and x not in ('.', '..') and not x.startswith('-') for x in parts[:-1]):
            dirs.append('/'.join(parts[:-1]))
    return sorted(set(dirs))[:10]

def safe_subpath(path):
    p = (path or '').strip().strip('/')
    ok = bool(p) and not p.startswith('-') and all(re.match(r'^[A-Za-z0-9_. -]{1,80}$', x) and x not in ('.', '..') for x in p.split('/')) and len(p.split('/')) <= 4
    return p if ok else None

def head_commit(dest):
    with contextlib.suppress(Exception):
        r = subprocess.run(['git', '-C', str(dest), 'rev-parse', 'HEAD'], capture_output=True, text=True, timeout=10)
        if r.returncode == 0: return r.stdout.strip()[:40]
    return None

def finish_scan(dest, repo, run):
    commit = head_commit(dest)
    F, n = scan_tree(dest)
    if (dest / '.git').exists(): rmtree(dest / '.git')
    with contextlib.suppress(OSError): (dest / MARKER).write_text(json.dumps({'owner': 'graph-skill', 'run': run, 'source': repo}) + '\n', encoding='utf-8')
    F.sort(key=lambda f: (f['level'] != 'block', f['file'], f['line']))
    return {'repo': repo, 'verdict': verdict(F), 'files': n, 'commit': commit, 'findings': F, 'at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}

def big_result(repo, why):
    return {'repo': repo, 'verdict': 'big', 'files': 0, 'commit': None, 'findings': [], 'note': f'too big: reference only ({why})', 'at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}

def do_scan(repo, run=None, path=None):
    """Clone into .graph/vendor, scan, drop .git. Return (result dict, error).
    A repo over the size limit gets a sparse partial clone of only its skill folders (or --path). If that does not fit
    or there is no skill folder, the result is verdict 'big': reference only, nothing copied."""
    dest = vendor_dir(repo)
    if dest.exists(): rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    sub = None
    if path:
        sub = safe_subpath(path)
        if not sub: return None, '--path must be a plain folder name inside the repo'
    if not sub:
        err = clone(repo, dest)
        if err:
            if dest.exists(): rmtree(dest)
            return None, err
        size = tree_size(dest)
        if size <= CLONE_MAX: return finish_scan(dest, repo, run), None
        rmtree(dest)
    reason = 'the whole repo is over the limit' if not sub else 'only the --path folder was fetched'
    err = clone(repo, dest, sparse=True)
    if err:
        if dest.exists(): rmtree(dest)
        return None, err
    dirs = sparse_skill_dirs(dest, sub)
    if not dirs:
        rmtree(dest); return big_result(repo, f'{reason}, no skill folder found'), None
    _, err = git(['sparse-checkout', 'set', '--cone', '--', *dirs], cwd=dest)
    if err:
        rmtree(dest); return big_result(repo, f'{reason}, partial fetch failed'), None
    if tree_size(dest) > CLONE_MAX:
        rmtree(dest); return big_result(repo, f'{reason}, the skill folders are still over {CLONE_MAX // 1048576} MB'), None
    res = finish_scan(dest, repo, run); res['sparse'] = dirs
    return res, None

def show_scan(res, run, part, cmd_prefix):
    v = res['verdict']
    col = {'clean': '1;32', 'warn': '1;33', 'block': '1;31', 'big': '1;33', 'package': '1;33'}[v]
    L = [paint(f"SCAN: {res['repo']}  =  {'TOO BIG' if v == 'big' else v.upper()}", col), f"files scanned: {res['files']}   findings: {len(res['findings'])}"]
    if res.get('sparse'): L.append('only fetched: ' + ', '.join(clean(x, 40) for x in res['sparse']))
    if v == 'big':
        L.append(paint(res.get('note') or 'too big: reference only', '1;33'))
        L.append(paint(f"DO THIS: use it as a reference only: {cmd_prefix} pick --run {run} --part {part} --use {res['repo']} --as reference\n         Or if it is a well-known package: ... --as package --package npm:<name> (or pip:<name>)", '1;36'))
        return '\n'.join(L)
    for f in res['findings'][:10]:
        L.append(f"  [{f['level']}] {f['file']}" + (f":{f['line']}" if f['line'] else '') + f"  {f['rule']}")
    if len(res['findings']) > 10: L.append(f"  ... {len(res['findings']) - 10} more in the scan file")
    if v == 'block': L.append(paint('BLOCKED. This one cannot be picked. Pick another.', '1;31'))
    else: L.append(paint(f"DO THIS: show this to the user. If they keep it: {cmd_prefix} pick --run {run} --part {part} --use {res['repo']} --as skill|reference|tool", '1;36'))
    return '\n'.join(L)

# ---------- packages: well-known packages are installed by npm or pip, not cloned ----------
PKG_ECOS = ('npm', 'pip')
NPM_RE = re.compile(r'^(@[a-z0-9][a-z0-9._-]{0,60}/)?[a-z0-9][a-z0-9._-]{0,60}$')
PIP_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,60}$')
NPM_MIN_DOWNLOADS = 10_000  # per month

def parse_package(spec):
    """'npm:shadcn' or 'pip:requests' -> (eco, name). Raises ValueError with a short message."""
    eco, _, name = str(spec or '').partition(':')
    if eco not in PKG_ECOS or not name: raise ValueError('--package must look like npm:<name> or pip:<name>.')
    if not (NPM_RE if eco == 'npm' else PIP_RE).match(name): raise ValueError('That package name is not valid.')
    return eco, name

def repo_in_url(url, repo):
    m = GH_LINK.search(str(url or ''))
    return bool(m) and repo_key(*m.groups()).lower() == repo.lower()

def check_package(eco, name, repo):
    """Trust from registry data (no clone, no install). npm: last-month downloads. PyPI: the project JSON.
    Both must point back to the repo. Return (info dict, error)."""
    info = {'package': f'{eco}:{name}', 'eco': eco, 'name': name}
    if eco == 'npm':
        data, err = get_json('https://api.npmjs.org/downloads/point/last-month/' + urllib.parse.quote(name, safe='@/'))
        n = data.get('downloads') if isinstance(data, dict) else None
        if err or not isinstance(n, int): return None, f'could not read npm download numbers ({clean(err or "no data", 60)}). Not trusted.'
        info['downloads'] = n
        if n < NPM_MIN_DOWNLOADS: return None, f'only {n:,} npm downloads last month, needs {NPM_MIN_DOWNLOADS:,}'
        meta, _err = get_json('https://registry.npmjs.org/' + urllib.parse.quote(name, safe='@/') + '/latest')
        r = meta.get('repository') if isinstance(meta, dict) else None
        url = r.get('url') if isinstance(r, dict) else r
        if not url: return None, 'the npm registry data could not be read or names no repository. Not trusted.'
        if not repo_in_url(url, repo): return None, f'the npm package points to {clean(url, 60)}, not {repo}'
        info['repo_checked'] = True
    else:
        data, err = get_json('https://pypi.org/pypi/' + urllib.parse.quote(name) + '/json')
        i = data.get('info') if isinstance(data, dict) else None
        if err or not isinstance(i, dict): return None, f'could not read the PyPI entry ({clean(err or "no data", 60)}). Not trusted.'
        links = list((i.get('project_urls') or {}).values()) + [i.get('home_page')]
        if not any(repo_in_url(u, repo) for u in links): return None, f'the PyPI entry does not point to {repo}'
        info['version'] = clean(i.get('version'), 30); info['repo_checked'] = True
    return info, None

# ---------- pick, install, cleanup ----------
def skill_dirs(vendor):
    """Folders with a SKILL.md: the repo root, else folders up to 3 levels down (max 10)."""
    vendor = Path(vendor)
    if (vendor / 'SKILL.md').is_file(): return [(vendor.name.split('__', 1)[-1], vendor)]
    out = []
    for dp, dirs, files in os.walk(vendor, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and (Path(dp) / d).relative_to(vendor).parts.__len__() <= 3)
        if 'SKILL.md' in files and Path(dp) != vendor: out.append((Path(dp).name, Path(dp)))
    return out[:10]

def safe_name(n):
    n = re.sub(r'[^A-Za-z0-9._-]', '-', n).strip('.-') or 'skill'
    return n[:60]

def copy_tree(src, dst):
    for dp, dirs, files in os.walk(src, followlinks=False):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not (Path(dp) / d).is_symlink()]
        rel = Path(dp).relative_to(src)
        (Path(dst) / rel).mkdir(parents=True, exist_ok=True)
        for f in files:
            s = Path(dp) / f
            if s.is_symlink(): continue
            shutil.copyfile(s, Path(dst) / rel / f)

def owned(d, run=None):
    try: m = json.loads((Path(d) / MARKER).read_text(encoding='utf-8'))
    except (OSError, ValueError): return None
    return m if isinstance(m, dict) and m.get('owner') == 'graph-skill' and (run is None or m.get('run') == run) else None

def install_skill(run, repo):
    """Copy the repo's skill folders into the project's .claude/skills and .agents/skills with an owner marker.
    Return (installed paths, error). Never overwrites a folder that is not ours."""
    found = skill_dirs(vendor_dir(repo))
    if not found: return [], 'no SKILL.md in this repo'
    names = [safe_name(n) for n, _ in found]
    for home in SKILL_HOMES:
        for nm in names:
            t = home / nm
            m = owned(t)
            if t.exists() and not m: return [], f'{t.as_posix()} already exists and is not ours. Not overwritten.'
            if t.exists() and m.get('run') != run: return [], f'{t.as_posix()} belongs to another run. Not overwritten.'
    done = []
    for (nm, src), name in zip(found, names):
        for home in SKILL_HOMES:
            t = home / name
            if t.exists(): rmtree(t)
            copy_tree(src, t)
            (t / MARKER).write_text(json.dumps({'owner': 'graph-skill', 'run': run, 'source': repo}) + '\n', encoding='utf-8')
            done.append(t.as_posix())
    return done, None

def cleanup(run, keep):
    """Remove outside skills we installed for this run and the vendor folder, except kept picks. Return (removed, kept)."""
    removed, kept = [], []
    for home in SKILL_HOMES:
        if not home.is_dir(): continue
        for d in sorted(home.iterdir()):
            m = owned(d, run) if d.is_dir() else None
            if not m: continue
            if m.get('source') in keep: kept.append(d.as_posix()); continue
            with contextlib.suppress(Exception): rmtree(d); removed.append(d.as_posix())
    if VENDOR.is_dir():
        for d in sorted(VENDOR.iterdir()):
            repo = d.name.replace('__', '/', 1)
            m = owned(d) if d.is_dir() else None
            if m and m.get('run') not in (run, None): continue  # another run's scanned copy
            if repo in keep: kept.append(d.as_posix()); continue
            with contextlib.suppress(Exception): rmtree(d) if d.is_dir() else d.unlink(); removed.append(d.as_posix())
        with contextlib.suppress(OSError): VENDOR.rmdir()
    if (TMP / run).is_dir():
        with contextlib.suppress(Exception): rmtree(TMP / run)
    with contextlib.suppress(OSError): TMP.rmdir()
    return removed, kept

# ---------- vendor-run ----------
def scrubbed_env(tmp_home):
    """PATH, LANG and temporary home folders only. Not a full sandbox: see the docs."""
    tmp_home = Path(tmp_home).resolve()
    for sub in ('home', 'config', 'data', 'cache', 'tmp'): (tmp_home / sub).mkdir(parents=True, exist_ok=True)
    env = {'PATH': os.environ.get('PATH', os.defpath), 'LANG': os.environ.get('LANG') or 'C.UTF-8',
           'HOME': str(tmp_home / 'home'), 'USERPROFILE': str(tmp_home / 'home'),
           'XDG_CONFIG_HOME': str(tmp_home / 'config'), 'XDG_DATA_HOME': str(tmp_home / 'data'), 'XDG_CACHE_HOME': str(tmp_home / 'cache'),
           'TMPDIR': str(tmp_home / 'tmp'), 'TEMP': str(tmp_home / 'tmp'), 'TMP': str(tmp_home / 'tmp')}
    if os.name == 'nt':
        for k in ('SYSTEMROOT', 'COMSPEC', 'PATHEXT'):
            if os.environ.get(k): env[k] = os.environ[k]
    return env

# ---------- vendor-run sandbox and agent-config guard ----------
GUARDED = ('.claude', '.agents', '.codex', '.cursor', '.git/hooks', '.graph/runs')

def sandbox_wrap(argv, work, tmp_home, project, platform=None):
    """Wrap argv so the tool can read and write only its work folder and temp home, with no access to the real home.
    Return (argv, None) or (None, reason). macOS: sandbox-exec. Linux: bwrap. Else: no sandbox available."""
    plat = platform or sys.platform
    home, work, tmp_home, project = (os.path.realpath(str(x)) for x in (Path.home(), work, tmp_home, project))
    # The program itself (often a python in ~/.local or ~/.pyenv) must stay readable. Its install folder is allowed, never the whole home.
    extra = []
    prog = shutil.which(argv[0]) or argv[0]
    for cand in (os.path.abspath(prog), os.path.realpath(prog)):
        if cand.startswith(home + os.sep):
            extra.append(cand)
            prefix = os.path.dirname(os.path.dirname(cand))
            if prefix.startswith(home + os.sep) and prefix != home: extra.append(prefix)
    extra = sorted(set(extra))
    if plat == 'darwin' and shutil.which('sandbox-exec'):
        prof = ('(version 1)(allow default)(deny file-read* (subpath (param "HOME")))(deny file-write*)'
                '(allow file-read* (subpath (param "WORK")) (subpath (param "TMPH")))'
                '(allow file-write* (subpath (param "WORK")) (subpath (param "TMPH")) (literal "/dev/null") (literal "/dev/tty") (subpath "/dev/fd"))')
        params = ['-D', f'HOME={home}', '-D', f'WORK={work}', '-D', f'TMPH={tmp_home}']
        for i, e in enumerate(extra):
            prof += f'(allow file-read* (subpath (param "X{i}")))'; params += ['-D', f'X{i}={e}']
        for i, g in enumerate(GUARDED):
            prof += f'(deny file-write* (subpath (param "G{i}")))'; params += ['-D', f'G{i}={os.path.join(project, g)}']
        return ['sandbox-exec', *params, '-p', prof, *argv], None
    if plat.startswith('linux') and shutil.which('bwrap'):
        cmd = ['bwrap', '--ro-bind', '/', '/', '--dev', '/dev', '--proc', '/proc', '--tmpfs', home, '--bind', work, work, '--bind', tmp_home, tmp_home,
               '--unshare-pid', '--die-with-parent', '--chdir', work]
        for e in extra:
            if os.path.exists(e) and not e.startswith(work + os.sep): cmd += ['--ro-bind', e, e]
        for g in GUARDED:
            gp = os.path.join(project, g)
            if os.path.isdir(gp) and gp.startswith(work + os.sep): cmd += ['--ro-bind', gp, gp]
        return cmd + ['--', *argv], None
    return None, 'no sandbox on this system (macOS needs sandbox-exec, Linux needs bwrap, Windows has none)'

def guard_snapshot(project):
    """Bytes of every small file in the guarded agent-config places, so changes can be found and undone."""
    snap = {}
    for g in GUARDED:
        base = Path(project) / g
        if g == '.graph/runs': continue  # the runtime's own state is checked by hash below, not copied
        if not base.exists(): continue
        for dp, _, files in os.walk(base, followlinks=False):
            for f in files:
                p = Path(dp) / f
                with contextlib.suppress(OSError):
                    if not p.is_symlink() and p.stat().st_size <= 1_000_000: snap[p.relative_to(project).as_posix()] = p.read_bytes()
    return snap

def guard_restore(project, before):
    """Undo writes to agent config. Return the list of changed paths (relative)."""
    after, changed = guard_snapshot(project), []
    for rel in sorted(set(before) | set(after)):
        if before.get(rel) == after.get(rel): continue
        p = Path(project) / rel; changed.append(rel)
        with contextlib.suppress(OSError):
            if rel in before: p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(before[rel])
            else: p.unlink()
    return changed
