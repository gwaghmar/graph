#!/usr/bin/env python3
"""Permissions memory: ask the user once, remember the answer. Per user, not per project.
File: ~/.config/graph-skill/permissions.json (folder override: GRAPH_SKILL_CONFIG_DIR).
Entry: {"decision": "allowed|denied|skip", "at": "<UTC time>", "note": "<the user's words>"}.
It never approves a post. Each growth post needs its own tap, so post-like keys are refused here."""
from __future__ import annotations
import contextlib, json, os, re, shutil, tempfile, time
from pathlib import Path

DECISIONS = ('allowed', 'denied', 'skip')
KEY = re.compile(r'^[a-z][a-z0-9-]{1,30}(:[A-Za-z0-9._@/ -]{1,80})?$')
POSTY = re.compile(r'(^|[:_-])(post|posts|posting|publish|reply|replies|grow|growth|tweet|dm)([:_-]|$)', re.I)

def config_dir():
    d = os.environ.get('GRAPH_SKILL_CONFIG_DIR')
    return Path(d) if d else Path.home() / '.config' / 'graph-skill'

def path(): return config_dir() / 'permissions.json'

def now(): return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())

def load():
    try: d = json.loads(path().read_text(encoding='utf-8'))
    except (OSError, ValueError): return {}
    return {k: v for k, v in d.items() if isinstance(v, dict) and v.get('decision') in DECISIONS} if isinstance(d, dict) else {}

def save(data):
    p = path(); p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f: json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, p)
    finally:
        with contextlib.suppress(OSError): os.unlink(tmp)

def check_key(key):
    """Return an error text, or None when the key is fine."""
    if not KEY.match(key or ''): return 'the key must look like login:vercel (a word, then an optional :name).'
    if POSTY.search(key): return 'a post cannot be allowed here. Each growth post needs its own tap at the checkpoint.'
    return None

def get(key):
    return load().get(key)

def record(key, decision, note):
    data = load(); data[key] = {'decision': decision, 'at': now(), 'note': (note or '').strip()[:300]}; save(data)

def forget(key):
    data = load(); had = data.pop(key, None) is not None
    if had: save(data)
    return had

def noop_browser():
    """A browser command that does nothing, so no CLI can open a sign-in page."""
    if os.name == 'nt': return ''
    return shutil.which('true') or '/usr/bin/true'

def quiet_env(base=None):
    """Environment for every subprocess the runtime starts: no prompts, no browser."""
    env = dict(os.environ if base is None else base)
    env['CI'] = '1'; env['BROWSER'] = noop_browser()
    return env
