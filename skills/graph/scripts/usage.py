#!/usr/bin/env python3
"""Graph: real token ledger and API-price-equivalent cost.
Reads usage numbers, model ids, timestamps and ids from the host's own transcript files. It never reads or stores
message content, secrets or tokens. Cost is what the tokens would cost at API prices, not what a plan charges."""
from __future__ import annotations
import calendar, contextlib, datetime, json, os, re, shutil, sys, time, urllib.request
from pathlib import Path

import preflight

LABEL = 'API-price equivalent. On a subscription you pay your plan, not this.'
PRICE_URL = 'https://models.dev/api.json'
PRICE_PROVIDERS = ('anthropic', 'openai', 'google')
PRICE_TTL_DAYS = 30
SHIPPED = Path(__file__).resolve().parent / 'prices.json'
FIELDS = ('input', 'output', 'cache_read', 'cache_write', 'reasoning')

def epoch_of(text):
    try: return datetime.datetime.fromisoformat(str(text).replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError): return None

def num(v): return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else 0

# ---- prices ----
def cache_path(home): return home / '.cache' / 'graph-skill' / 'prices.json'

def read_prices(p):
    try: d = json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError): return None
    return d if isinstance(d, dict) and isinstance(d.get('models'), dict) and d.get('as_of') else None

def age_days(as_of, today=None):
    try: return (datetime.date.fromisoformat(today or time.strftime('%Y-%m-%d', time.gmtime())) - datetime.date.fromisoformat(as_of)).days
    except (ValueError, TypeError): return None

def load_prices(home, today=None):
    """Cache if it is under 30 days old, else the shipped snapshot. Returns (table, as_of, where, age in days)."""
    c = read_prices(cache_path(home))
    if c:
        a = age_days(c['as_of'], today)
        if a is not None and 0 <= a < PRICE_TTL_DAYS: return c['models'], c['as_of'], 'cache', a
    s = read_prices(SHIPPED) or {'models': {}, 'as_of': 'unknown'}
    return s['models'], s['as_of'], 'shipped snapshot', age_days(s['as_of'], today)

def _strip(mid):
    mid = re.sub(r'\[.*\]$', '', str(mid).strip()).split('/')[-1]
    return re.sub(r'[-@](\d{8}|\d{4}-\d{2}-\d{2})$', '', mid)

def find_price(table, model):
    if not model: return None
    if model in table: return table[model]
    short = _strip(model)
    if short in table: return table[short]
    for k, v in table.items():
        if _strip(k) == short: return v
    return None

def build_table(api):
    out = {}
    for prov in PRICE_PROVIDERS:
        models = (api.get(prov) or {}).get('models') if isinstance(api.get(prov), dict) else None
        for mid, m in (models or {}).items():
            c = m.get('cost') if isinstance(m, dict) else None
            if isinstance(c, dict) and isinstance(c.get('input'), (int, float)) and isinstance(c.get('output'), (int, float)):
                out[mid] = {'input': c['input'], 'output': c['output'], 'cache_read': c.get('cache_read') or 0, 'cache_write': c.get('cache_write') or 0}
                for flag in ('reasoning', 'tool_call'):  # capability flags, used to rank models into tiers
                    if isinstance(m.get(flag), bool): out[mid][flag] = m[flag]
    return dict(sorted(out.items()))

def refresh_prices(home, url=None):
    """Fetch the price list and write the cache. Returns (count, error text or None)."""
    url = url or os.environ.get('GRAPH_PRICES_URL') or PRICE_URL
    try:
        with urllib.request.urlopen(url, timeout=10) as r: api = json.loads(r.read().decode('utf-8'))
        table = build_table(api)
        if not table: return 0, 'the price list had no usable models'
        p = cache_path(home); p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix('.tmp')
        tmp.write_text(json.dumps({'as_of': time.strftime('%Y-%m-%d', time.gmtime()), 'source_url': PRICE_URL, 'models': table}, indent=1) + '\n', encoding='utf-8')
        os.replace(tmp, p)
        return len(table), None
    except Exception as e: return 0, f'{type(e).__name__}'

def cost_of(price, r):
    if not price: return None
    h1 = min(r['cache_write'], r.get('cache_write_1h', 0))  # 1 hour cache writes cost 2x the input price, 5 minute writes the cache_write price
    return (r['input'] * price['input'] + r['output'] * price['output'] + r['cache_read'] * price.get('cache_read', 0)
            + (r['cache_write'] - h1) * price.get('cache_write', 0) + h1 * (2 * price['input'] if price.get('cache_write') else 0)) / 1e6

# ---- collectors: each returns a list of records {model, agent, ts, input, output, cache_read, cache_write, reasoning} ----
def claude_dirs(home, cwd, project=None):
    roots = [preflight.project_root(cwd, None, project), Path(cwd)]
    out = []
    for r in roots:
        d = home / '.claude' / 'projects' / re.sub(r'[^A-Za-z0-9]', '-', str(r))
        if d.is_dir() and d not in out: out.append(d)
    return out

def claude_sub_dirs(home, cwd, project=None):
    """Project folders for sessions started in a sub folder or worktree of the project. Only a guess from the name:
    each record's own cwd is checked before it counts, so a sibling folder with a similar name never leaks in."""
    base, out = home / '.claude' / 'projects', []
    if not base.is_dir(): return out
    exact = {d.name for d in claude_dirs(home, cwd, project)}
    for r in [preflight.project_root(cwd, None, project), Path(cwd)]:
        enc = re.sub(r'[^A-Za-z0-9]', '-', str(r)) + '-'
        for d in sorted(base.iterdir()):
            if d.is_dir() and d.name.startswith(enc) and d.name not in exact and d not in out: out.append(d)
    return out

def collect_claude(home, cwd, t0, t1, session=None, project=None):
    """Every session file of the project that touches the run window, plus each session's subagent files.
    A headless call is its own session, so a run with many calls has many files. With session=None all of them count.
    Records are de-duplicated by message id, so a resumed session that copies old messages does not count twice."""
    best = {}
    roots = [preflight.project_root(cwd, None, project), Path(cwd)]
    subs = claude_sub_dirs(home, cwd, project)
    for base in claude_dirs(home, cwd, project) + subs:
        files = [(f, None) for f in base.glob('*.jsonl')] + [(f, f.stem[6:]) for f in base.glob('*/subagents/agent-*.jsonl')]
        for f, fid in sorted(files, key=lambda x: str(x[0])):
            sid_file = f.parent.parent.name if fid else f.stem
            try:
                if f.stat().st_mtime < t0 - 5: continue
                lines = f.read_text(encoding='utf-8', errors='replace').splitlines()
            except OSError: continue
            for line in lines:
                if '"usage"' not in line: continue
                try: o = json.loads(line)
                except ValueError: continue
                m = o.get('message') if isinstance(o, dict) else None
                u = m.get('usage') if isinstance(m, dict) else None
                if not isinstance(u, dict) or o.get('type') != 'assistant': continue
                if session and o.get('sessionId') != session: continue
                if base in subs and not (o.get('cwd') and any(same_dir(o['cwd'], r) for r in roots)): continue
                ts = epoch_of(o.get('timestamp'))
                model = m.get('model')
                if ts is None or model in (None, '<synthetic>'): continue
                det = u.get('output_tokens_details') if isinstance(u.get('output_tokens_details'), dict) else {}
                rec = {'model': str(model), 'ts': ts, 'session': str(o.get('sessionId') or sid_file), 'agent': fid or (o.get('agentId') if o.get('isSidechain') else None),
                       'input': num(u.get('input_tokens')), 'output': num(u.get('output_tokens')), 'cache_read': num(u.get('cache_read_input_tokens')),
                       'cache_write': num(u.get('cache_creation_input_tokens')), 'reasoning': num(det.get('thinking_tokens')),
                       'cache_write_1h': num((u.get('cache_creation') or {}).get('ephemeral_1h_input_tokens') if isinstance(u.get('cache_creation'), dict) else 0)}
                if not any(rec[k] for k in FIELDS): continue
                key = m.get('id') or o.get('requestId') or f'{f}:{len(best)}'
                old = best.get(key)
                if old is None or rec['output'] >= old['output']: best[key] = rec
    return [r for r in best.values() if t0 <= r['ts'] <= t1]

def same_dir(a, b):
    a, b = os.path.normcase(os.path.realpath(str(a))), os.path.normcase(os.path.realpath(str(b)))
    return a == b or a.startswith(b.rstrip('/\\') + os.sep)

def collect_codex(home, cwd, t0, t1, session=None, project=None):
    root = preflight.project_root(cwd, None, project)
    recs = []
    base = home / '.codex' / 'sessions'
    if not base.is_dir(): return recs
    for f in base.rglob('rollout-*.jsonl'):
        try:
            if f.stat().st_mtime < t0 - 5: continue
            lines = f.read_text(encoding='utf-8', errors='replace').splitlines()
        except OSError: continue
        model, prev, ok, sid = 'unknown', {}, False, None
        for line in lines:
            if not any(w in line for w in ('"session_meta"', '"turn_context"', '"token_count"')): continue
            try: o = json.loads(line)
            except ValueError: continue
            p = o.get('payload') if isinstance(o, dict) else None
            if not isinstance(p, dict): continue
            if o.get('type') == 'session_meta':
                sid = p.get('id'); c = p.get('cwd')
                ok = bool(c) and (same_dir(c, root) or same_dir(c, cwd)) and (not session or sid == session)
                if not ok: break
            elif o.get('type') == 'turn_context' and p.get('model'): model = str(p['model'])
            elif p.get('type') == 'token_count' and ok:
                info = p.get('info'); tot = info.get('total_token_usage') if isinstance(info, dict) else None
                ts = epoch_of(o.get('timestamp'))
                if not isinstance(tot, dict) or ts is None: continue
                d = {k: max(0, num(tot.get(k)) - prev.get(k, 0)) for k in ('input_tokens', 'cached_input_tokens', 'cache_write_input_tokens', 'output_tokens', 'reasoning_output_tokens')}
                prev = {k: num(tot.get(k)) for k in d}
                if not any(d.values()) or not (t0 <= ts <= t1): continue
                recs.append({'model': model, 'ts': ts, 'agent': None, 'output': d['output_tokens'], 'reasoning': d['reasoning_output_tokens'],
                             'cache_read': d['cached_input_tokens'], 'cache_write': d['cache_write_input_tokens'],
                             'input': max(0, d['input_tokens'] - d['cached_input_tokens'] - d['cache_write_input_tokens'])})
    return recs

def host_key(host):
    h = (host or '').lower()
    for k in ('claude', 'codex', 'opencode', 'cursor'):
        if k in h: return k
    return 'unknown'

# ---- attribution and totals ----
def node_spans(d, now):
    out = {}
    for n in d.get('nodes', []):
        s = [list(x) for x in n.get('spans', []) if len(x) == 2]
        if n.get('status') in ('running', 'retrying') and n.get('started_epoch'): s.append([n['started_epoch'], now])
        if s: out[n['id']] = s
    return out

def attribute(recs, d, now):
    """Map each agent id to a node id, 'shared' (overlaps 2 or more nodes) or 'unattributed'. Main session is 'orchestrator'."""
    spans, by_agent = node_spans(d, now), {}
    for r in recs:
        if r['agent']: by_agent.setdefault(r['agent'], []).append(r['ts'])
    who = {}
    for a, ts in by_agent.items():
        a0, a1 = min(ts), max(ts)
        hit = [n for n, ss in spans.items() if any(s[0] <= a1 and s[1] >= a0 for s in ss)]
        who[a] = hit[0] if len(hit) == 1 else 'shared' if hit else 'unattributed'
    return who

def build(recs, d, table, now):
    who = attribute(recs, d, now)
    rows, agents = {}, {}
    for r in recs:
        w = who[r['agent']] if r['agent'] else 'orchestrator'
        c = cost_of(find_price(table, r['model']), r)
        approx = str(r['model']).rstrip().endswith('[1m]')  # long-context rows may be priced higher than the base price
        for store, key in ((rows, (w, r['model'])), (agents, (r['agent'], w) if r['agent'] else None)):
            if key is None: continue
            b = store.setdefault(key, dict.fromkeys(FIELDS, 0) | {'cost': 0.0, 'unknown': False, 'approx': False})
            for k in FIELDS: b[k] += r[k]
            b['approx'] = b['approx'] or approx
            if c is None: b['unknown'] = True
            else: b['cost'] += c
    def rowlist(store, names):
        return [dict(zip(names, k)) | {f: v[f] for f in FIELDS} | {'cost': round(v['cost'], 6), 'unknown': v['unknown'], 'approx': v['approx']} for k, v in sorted(store.items(), key=lambda kv: str(kv[0]))]
    return rowlist(rows, ('who', 'model')), rowlist(agents, ('agent', 'who'))

def sums(rows):
    t = dict.fromkeys(FIELDS, 0) | {'cost': 0.0, 'unknown': False, 'approx': False}
    for r in rows:
        for k in FIELDS: t[k] += r[k]
        t['approx'] = t['approx'] or bool(r.get('approx'))
        t['cost'] += r['cost']; t['unknown'] = t['unknown'] or r['unknown']
    return t

def collect(d, home, cwd, host=None, session=None, project=None, today=None, now=None):
    """Return the usage dict stored in the run state. Fails soft: status 'unknown' with a reason."""
    now = now or time.time()
    h = host_key(host or d.get('host_detected') or d.get('host'))
    out = {'status': 'unknown', 'host': h, 'at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now)), 'label': LABEL}
    try:
        t0 = calendar.timegm(time.strptime(d.get('created_at', ''), '%Y-%m-%dT%H:%M:%SZ'))
        fin = d.get('finished_at'); t1 = calendar.timegm(time.strptime(fin, '%Y-%m-%dT%H:%M:%SZ')) + 1 if fin else now
        if h == 'claude': recs = collect_claude(home, cwd, t0, t1, session, project)
        elif h == 'codex': recs = collect_codex(home, cwd, t0, t1, session, project)
        else:
            out['note'] = f'Usage is not read for host "{h}". Tokens are unknown.'; return out
        table, as_of, where, age = load_prices(home, today)
        rows, agents = build(recs, d, table, now)
        out.update(status='ok', rows=rows, agents=agents, totals=sums(rows), prices={'as_of': as_of, 'from': where, 'age_days': age}, records=len(recs),
                   sessions=len({r['session'] for r in recs if r.get('session')}))
        if not recs: out['note'] = 'No usage lines were found for this project in the run window.'
    except Exception as e: out['note'] = f'Usage could not be read ({type(e).__name__}).'
    return out

# ---- report ----
def n(v): return f'{v:,}'

def money(b):
    if b['unknown'] and not b['cost']: return 'n/a'
    return f"${b['cost']:,.2f}" + (' + n/a' if b['unknown'] else '') + (' (approx, long context)' if b.get('approx') else '')

def table_lines(first, groups):
    L = [f'| {first} | Input | Output | Cache read | Cache write | Reasoning | Cost |', '|---|---|---|---|---|---|---|']
    for name, rows in groups:
        t = sums(rows); L.append(f"| {name} | {n(t['input'])} | {n(t['output'])} | {n(t['cache_read'])} | {n(t['cache_write'])} | {n(t['reasoning'])} | {money(t)} |")
    return L

def group(rows, key):
    g = {}
    for r in rows: g.setdefault(r[key], []).append(r)
    return sorted(g.items(), key=lambda kv: (kv[0] in ('orchestrator', 'shared', 'unattributed'), kv[0]))

def report_lines(u):
    """Markdown lines for the summary. Never raises on a missing or partial usage dict."""
    if not isinstance(u, dict) or u.get('status') != 'ok':
        return [f"Token use is unknown. {(u or {}).get('note', 'It was not collected.')}"]
    p = u.get('prices') or {}
    age = p.get('age_days')
    L = [LABEL, '', f"Prices as of {p.get('as_of', 'unknown')} ({p.get('from', '?')}" + (f', {age} days old' if age is not None else '') + ').']
    if u.get('note'): L.append(u['note'])
    if (u.get('sessions') or 0) > 1: L.append(f"Counted {u['sessions']} host sessions that overlap this run (each headless call is its own session).")
    rows = u.get('rows') or []
    if rows:
        L += ['', *table_lines('Node', group(rows, 'who')), '', *table_lines('Model', group(rows, 'model'))]
        ag = sorted(u.get('agents') or [], key=lambda a: -a['cost'])
        if ag: L += ['', *table_lines('Subagent', [(f"{a['agent'][:8]} ({a['who']})", [a]) for a in ag[:15]])] + ([f'{len(ag) - 15} more subagents are in the run state.'] if len(ag) > 15 else [])
        t = u['totals']
        L += ['', f"Total: {n(t['input'])} input, {n(t['output'])} output, {n(t['cache_read'])} cache read, {n(t['cache_write'])} cache write, {n(t['reasoning'])} reasoning (counted inside output). Cost {money(t)}."]
        if any(r['unknown'] for r in rows): L.append('Cost is n/a for models missing from the price list. Tokens are still counted.')
    return L

def history_entries(d, u):
    """One line per finished node: date, host, role, tier, token totals, duration. No content, no paths."""
    if not isinstance(u, dict) or u.get('status') != 'ok' or not u.get('rows'): return []
    out = []
    for nd in d.get('nodes', []):
        if nd.get('status') not in ('complete', 'failed'): continue
        t = sums([r for r in u.get('rows', []) if r['who'] == nd['id']])
        e = {'date': time.strftime('%Y-%m-%d', time.gmtime()), 'host': u.get('host'), 'role': nd.get('role'), 'run': d.get('id'), 'node': nd['id'],
             'tokens': {k: t[k] for k in FIELDS}, 'duration_ms': nd.get('duration_ms')}
        if nd.get('tier'): e['tier'] = nd['tier']
        out.append(e)
    return out

def write_history(home, entries):
    if not entries: return
    p = home / '.config' / 'graph-skill' / 'history.jsonl'
    p.parent.mkdir(parents=True, exist_ok=True)
    seen = set()
    with contextlib.suppress(OSError, ValueError):
        for line in p.read_text(encoding='utf-8').splitlines():
            o = json.loads(line); seen.add((o.get('run'), o.get('node')))
    new = [e for e in entries if (e['run'], e['node']) not in seen]
    if new:
        with open(p, 'a', encoding='utf-8') as f: f.write(''.join(json.dumps(e) + '\n' for e in new))
