#!/usr/bin/env python3
"""Graph: route nodes by capability tier (deep, build, quick), not by model name.
Values come from data only: the user's tiers.json, the host's own catalog, scripts/capabilities.json and the models.dev price list.
It never reads secrets, tokens or message content."""
from __future__ import annotations
import contextlib, json, os, re, shutil, time
from pathlib import Path

import usage
from preflight import run_proc

TIERS = ('quick', 'build', 'deep')          # low to high
EFFORTS = ('low', 'medium', 'high')
# The one place that maps a node role to a tier. Unknown roles use build.
ROLE_TIER = {
    'design': 'deep', 'architect': 'deep', 'architecture': 'deep', 'review': 'deep', 'audit': 'deep', 'security': 'deep',
    'migration': 'deep', 'rootcause': 'deep', 'root-cause': 'deep', 'debug': 'deep', 'analysis': 'deep', 'plan': 'deep',
    'build': 'build', 'implement': 'build', 'fix': 'build', 'refactor': 'build', 'test': 'build', 'code': 'build',
    'worker': 'build', 'research': 'build', 'docs': 'build',
    'lookup': 'quick', 'search': 'quick', 'rename': 'quick', 'config': 'quick', 'list': 'quick', 'find': 'quick',
}
DEFAULT_TIER = 'build'
HOSTS = ('claude-code', 'codex', 'opencode', 'cursor')
HOST_NAME = {'claude': 'claude-code', 'codex': 'codex', 'opencode': 'opencode', 'cursor': 'cursor'}
CATALOG_CMD = {'codex': ['codex', 'debug', 'models'], 'opencode': ['opencode', 'models', '--verbose']}
CATALOG_TTL = 24 * 3600
CAPS = Path(__file__).resolve().parent / 'capabilities.json'
SAFE_VALUE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:/@+\[\]-]{0,119}$')
NOT_APPLIED = 'host default, routing not applied'

def canon_host(text):
    return HOST_NAME.get(usage.host_key(text), 'unknown')

def tier_of(n):
    """(tier, how). --needs wins, else the role table."""
    if n.get('needs') in TIERS: return n['needs'], 'needs'
    return ROLE_TIER.get(str(n.get('role') or '').lower(), DEFAULT_TIER), 'role'

def up(tier): return TIERS[min(TIERS.index(tier) + 1, len(TIERS) - 1)]

def attempt_info(d, n):
    """(attempt number the node is on or about to start, attempts allowed, whether it is the last one after retries)."""
    limit = d.get('max_retries', 2) + 1
    cur = n.get('attempts', 0) if n.get('status') in ('running', 'retrying') else n.get('attempts', 0) + 1
    return cur, limit, limit > 1 and cur >= limit

# ---- data sources ----
def overrides_path(home): return home / '.config' / 'graph-skill' / 'tiers.json'

def load_overrides(home, host):
    """{tier: value} for this host from the user file. Bad entries are skipped and named in the notes."""
    try: data = json.loads(overrides_path(home).read_text(encoding='utf-8'))
    except (OSError, ValueError): return {}, []
    entry = data.get(host) if isinstance(data, dict) else None
    if not isinstance(entry, dict): return {}, []
    out, notes = {}, []
    for t, v in entry.items():
        if t in TIERS and isinstance(v, str) and SAFE_VALUE.match(v): out[t] = v
        else: notes.append(f'ignored bad entry {t!r} in ~/.config/graph-skill/tiers.json')
    return out, notes

def load_caps():
    try: d = json.loads(CAPS.read_text(encoding='utf-8'))
    except (OSError, ValueError): return {}
    return d.get('hosts') if isinstance(d, dict) and isinstance(d.get('hosts'), dict) else {}

def _num(v): return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None

def parse_codex(text):
    d = json.loads(text)
    items = d if isinstance(d, list) else (d.get('models') if isinstance(d, dict) else None)
    out = []
    for m in items or []:
        if not isinstance(m, dict) or not isinstance(m.get('slug'), str): continue
        if m.get('visibility') not in (None, 'list'): continue
        if not SAFE_VALUE.match(m['slug']): continue
        lv = m.get('supported_reasoning_levels') or []
        efforts = [x.get('effort') if isinstance(x, dict) else x for x in lv if (x.get('effort') if isinstance(x, dict) else x)]
        out.append({'id': m['slug'], 'context': _num(m.get('context_window')), 'efforts': [e for e in efforts if isinstance(e, str)],
                    'default_effort': m.get('default_reasoning_level') if isinstance(m.get('default_reasoning_level'), str) else None,
                    'reasoning': bool(efforts), 'tool_call': None, 'price': None})
    return out

def parse_opencode(text):
    """`opencode models --verbose`: an id line, then a JSON block, repeated."""
    dec, pos, out = json.JSONDecoder(), 0, []
    rx = re.compile(r'^([A-Za-z0-9_.-]+/\S+)[ \t]*$', re.M)
    while True:
        m = rx.search(text, pos)
        if not m: break
        pos = m.end(); j = pos
        while j < len(text) and text[j].isspace(): j += 1
        obj = None
        if j < len(text) and text[j] == '{':
            with contextlib.suppress(ValueError):
                obj, pos = dec.raw_decode(text, j)
        if not isinstance(obj, dict) or not SAFE_VALUE.match(m.group(1)): continue
        cap = obj.get('capabilities') if isinstance(obj.get('capabilities'), dict) else {}
        cost = obj.get('cost') if isinstance(obj.get('cost'), dict) else {}
        lim = obj.get('limit') if isinstance(obj.get('limit'), dict) else {}
        tc = cap.get('toolcall', cap.get('tool_call', obj.get('tool_call')))
        rs = cap.get('reasoning', obj.get('reasoning'))
        out.append({'id': m.group(1), 'context': _num(lim.get('context')), 'efforts': [], 'default_effort': None,
                    'reasoning': rs if isinstance(rs, bool) else None, 'tool_call': tc if isinstance(tc, bool) else None, 'price': _num(cost.get('output'))})
    return out

def fetch_catalog(host, home, refresh=False, now=None):
    """(models or None, note, age in seconds or None). Cached for 24 hours. The cache holds ids and capability numbers only."""
    cmd = CATALOG_CMD.get(host)
    if not cmd: return None, f'{host} has no model catalog', None
    now = now or time.time()
    p = home / '.cache' / 'graph-skill' / f'catalog-{host}.json'
    cached = None
    with contextlib.suppress(OSError, ValueError):
        c = json.loads(p.read_text(encoding='utf-8'))
        if isinstance(c, dict) and isinstance(c.get('models'), list) and _num(c.get('at')) is not None: cached = c
    if cached and not refresh and 0 <= now - cached['at'] < CATALOG_TTL: return cached['models'], f'{host} catalog (cache)', now - cached['at']
    stale = (cached['models'], f'{host} catalog (stale cache, refresh failed)', now - cached['at']) if cached else None
    exe = shutil.which(cmd[0])
    if not exe: return stale or (None, f'{cmd[0]} is not installed', None)
    try:
        code, out, _, timed = run_proc([exe, *cmd[1:]], 20)
        models = (parse_codex if host == 'codex' else parse_opencode)(out) if code == 0 and not timed else []
    except Exception: models = []
    if not models: return stale or (None, f'{host} catalog could not be read', None)
    with contextlib.suppress(OSError):
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix('.tmp'); tmp.write_text(json.dumps({'at': now, 'models': models}) + '\n', encoding='utf-8'); os.replace(tmp, p)
    return models, f'{host} catalog (fresh)', 0

# ---- ranking ----
def rank(cands):
    """cands: {id, price, reasoning, tool_call}. Returns {tier: id} using output price. A tier with no fit is left out.
    deep: highest price among reasoning models. quick: lowest price among tool-calling models.
    build: the median-priced reasoning model (lower median); with one model it is deep, with two it is the cheaper one."""
    priced = [c for c in cands if c.get('price') is not None]
    reason = sorted((c for c in priced if c.get('reasoning')), key=lambda c: (c['price'], c['id']))
    tool = sorted((c for c in priced if c.get('tool_call') is not False), key=lambda c: (c['price'], c['id']))
    out = {}
    if reason:
        out['deep'] = max(reason, key=lambda c: (c['price'], c['id']))['id']
        out['build'] = reason[(len(reason) - 1) // 2]['id']
    if tool: out['quick'] = tool[0]['id']
    return out

def with_prices(models, table):
    out = []
    for m in models:
        e = usage.find_price(table, m['id']) or {}
        price = e.get('output') if _num(e.get('output')) is not None else m.get('price')
        reasoning = e['reasoning'] if isinstance(e.get('reasoning'), bool) else m.get('reasoning')
        tool = e['tool_call'] if isinstance(e.get('tool_call'), bool) else m.get('tool_call')
        out.append(dict(m, price=price, reasoning=bool(reasoning), tool_call=tool))
    return out

def tier_values(host, home, refresh=False, today=None, now=None):
    """{'tiers': {tier: {value, source, efforts, default_effort}}, 'notes': [...], 'data': [...]} for one host."""
    tv = {'tiers': {}, 'notes': [], 'data': []}
    ov, ov_notes = load_overrides(home, host)
    tv['notes'] += ov_notes
    cat_models = None
    if host == 'claude-code':
        c = (load_caps().get('claude-code') or {})
        for t, v in (c.get('tiers') or {}).items():
            if t in TIERS and isinstance(v, str) and SAFE_VALUE.match(v):
                tv['tiers'][t] = {'value': v, 'source': 'host alias (capabilities.json)', 'efforts': list(EFFORTS), 'default_effort': None}
        if tv['tiers']: tv['data'].append(f"capabilities.json as of {c.get('as_of', 'unknown')}")
    elif host in CATALOG_CMD and len(ov) < len(TIERS):
        models, note, age = fetch_catalog(host, home, refresh, now)
        cat_models = models
        tv['data'].append(note + ('' if age is None else f', {round(age / 3600, 1)}h old'))
        if models:
            table, as_of, where, tage = usage.load_prices(home, today)
            tv['data'].append(f"models.dev {where}, as of {as_of}" + ('' if tage is None else f' ({tage} days old)'))
            cands = with_prices(models, table)
            by_id = {m['id']: m for m in models}
            for t, mid in rank(cands).items():
                tv['tiers'][t] = {'value': mid, 'source': f'{host} catalog + models.dev', 'efforts': by_id[mid]['efforts'], 'default_effort': by_id[mid]['default_effort']}
            if not tv['tiers']: tv['notes'].append('no catalog model had a price in models.dev, so nothing could be ranked')
        else: tv['notes'].append(note)
    for t, v in ov.items():
        meta = next((m for m in (cat_models or []) if m['id'] == v), None)
        tv['tiers'][t] = {'value': v, 'source': 'your override (~/.config/graph-skill/tiers.json)', 'efforts': (meta or {}).get('efforts') or [], 'default_effort': (meta or {}).get('default_effort')}
    if ov: tv['data'].append('your tiers.json')
    return tv

def pick(tv, tier, high):
    """(tier used, entry or None). Same tier first, then upward. Downward only when the node is not high risk."""
    order = list(TIERS[TIERS.index(tier):]) + ([] if high else list(reversed(TIERS[:TIERS.index(tier)])))
    for t in order:
        if t in tv['tiers']: return t, tv['tiers'][t]
    return tier, None

# ---- resolve ----
def fmt(host, value, effort):
    if value is None: return 'host default' if host in ('cursor', 'unknown') else NOT_APPLIED
    if host == 'claude-code': return f'model={value}' + (f' effort={effort}' if effort else '')
    if host == 'codex': return f'-m {value}' + (f' -c model_reasoning_effort={effort}' if effort else '')
    if host == 'opencode': return f'--model {value}'
    return 'host default'

def route(d, n, host, home, today=None, now=None):
    """Work out the host value for one node attempt. Rules enforced here: high risk never goes below its tier,
    and the last allowed attempt after retries goes one tier up."""
    asked, how = tier_of(n)
    attempt, limit, last = attempt_info(d, n)
    high = n.get('risk') == 'high'
    used, esc, notes = asked, False, []
    effort = n.get('effort')
    if last:
        if asked != 'deep': used, esc = up(asked), True
        else:
            if effort != 'high': notes.append('already deep: effort raised to high on the last attempt')
            effort = 'high'
    r = {'host': host, 'node': n['id'], 'tier': asked, 'tier_by': how, 'tier_used': used, 'escalated': esc, 'attempt': attempt, 'limit': limit,
         'risk': n.get('risk') or 'low', 'value': None, 'effort': None, 'source': None, 'manual': False, 'notes': notes, 'at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
    if host in ('cursor', 'unknown'):
        r['source'] = 'this host cannot be told which model to use'
    else:
        ent = None
        if n.get('model'):
            r['manual'] = True; r['source'] = 'manual --model override'
            r['value'] = n['model']; notes.append('manual model override: tier rules are not checked')
        else:
            tv = tier_values(host, home, today=today, now=now)
            t2, ent = pick(tv, used, high)
            notes += tv['notes']
            if ent:
                r['value'], r['source'] = ent['value'], ent['source']
                if t2 != used: r['tier_used'] = t2; notes.append(f'no {used} value here, used {t2}')
            else:
                r['source'] = 'no value found'
                if high: notes.append('high risk: no lower tier was used')
        if r['value'] and host in ('claude-code', 'codex'):
            if effort and ent and ent.get('efforts') and effort not in ent['efforts']:
                notes.append(f"effort {effort} is not supported by {r['value']}; its default is used")
                effort = ent.get('default_effort')
            elif not effort and ent: effort = ent.get('default_effort') if host == 'codex' else None
            r['effort'] = effort
        elif effort and host == 'opencode': notes.append('opencode: effort is not applied')
    r['text'] = fmt(host, r['value'], r['effort'])
    return r

def describe(r):
    L = [r['text'], f"tier {r['tier']} (from {'--needs' if r['tier_by'] == 'needs' else 'role'})" + (f", used {r['tier_used']}" if r['tier_used'] != r['tier'] else '')
         + f", host {r['host']}, attempt {r['attempt']} of {r['limit']}" + (f", source: {r['source']}" if r['source'] else '')]
    if r['escalated']: L.append(f"Escalated: this is the last allowed attempt, so it goes one tier up ({r['tier']} -> {r['tier_used']}).")
    L += [f'Note: {x}' for x in r['notes']]
    return '\n'.join(L)

def keep(r):
    """The part of a route that is stored on the node and in the log."""
    return {k: r[k] for k in ('host', 'tier', 'tier_used', 'escalated', 'attempt', 'value', 'effort', 'manual', 'text', 'at')}

# ---- report ----
def same_model(value, ran):
    a, b = str(value or '').lower().split('/')[-1], str(ran or '').lower().split('/')[-1]
    return bool(a and b) and (a == b or a in b or b in a)

def report_lines(d):
    nodes = [n for n in d.get('nodes', []) if n.get('routes') or n.get('needs') or n.get('model')]
    if not nodes: return ['No routing was recorded. No node had --needs and none was resolved.']
    rows = (d.get('usage') or {}).get('rows') if isinstance(d.get('usage'), dict) else None
    L = ['| Node | Tier asked | Resolved | Ran | Note |', '|---|---|---|---|---|']
    cell = lambda s: str(s).replace('|', '/').replace('\n', ' ')
    for n in nodes:
        tier, how = tier_of(n); rs = n.get('routes') or []
        values = [r['value'] for r in rs if r.get('value')]
        ran = sorted({r['model'] for r in rows or [] if r.get('who') == n['id']})
        notes = []
        if not rs: notes.append('no resolve was recorded')
        if any(r.get('escalated') for r in rs): notes.append('escalated one tier on the last attempt')
        if any(r.get('manual') for r in rs): notes.append('manual model override')
        if rs and not values: notes.append('routing not applied')
        if values and not ran: notes.append('model that ran is unknown')
        bad = [m for m in ran if values and not any(same_model(v, m) for v in values)]
        if bad: notes.append('MISMATCH: ' + ', '.join(bad) + ' ran, not ' + ', '.join(sorted(set(values))))
        L.append(f"| {cell(n['id'])} | {tier} ({how}) | {cell(rs[-1]['text'] if rs else 'none')} | {cell(', '.join(ran) or 'unknown')} | {cell('; '.join(notes) or 'ok')} |")
    return L
