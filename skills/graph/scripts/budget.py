#!/usr/bin/env python3
"""Graph: usage budget, estimates and refill planning.
Reads usage-left numbers (percentages, reset times) from the host's own files. Never reads message content, secrets or tokens.
Everything here is approximate and says so. Nothing is scheduled: the output is a suggestion for the user."""
from __future__ import annotations
import contextlib, datetime, json, math, re, statistics, time
from pathlib import Path

import routing, usage

SHIPPED = Path(__file__).resolve().parent / 'estimates.json'
CLAUDE_MAX_AGE = 600     # seconds: an older statusline cache is stale
MIN_HISTORY = 5          # samples needed before history replaces the shipped defaults
MIN_CALIB = 3            # runs needed before tokens can be turned into percent
APPROX = 'Approximate. Other sessions and the host share these limits, so real use can differ.'
DEFAULT_LABEL = 'default, not your history'
HHMM = re.compile(r'^([01]?\d|2[0-3]):([0-5]\d)$')

def n(v): return f'{int(v):,}'

def local_time(ts, now=None):
    """Local clock time, with the date when it is not today."""
    now = now if now is not None else time.time()
    t = time.localtime(ts)
    return time.strftime('%H:%M', t) if time.strftime('%Y-%m-%d', t) == time.strftime('%Y-%m-%d', time.localtime(now)) else time.strftime('%Y-%m-%d %H:%M', t)

def local_iso(ts):
    return datetime.datetime.fromtimestamp(ts).astimezone().isoformat(timespec='minutes')

def window_name(minutes):
    if not isinstance(minutes, (int, float)) or minutes <= 0: return 'window'
    m = int(minutes)
    return f'{m // 1440}d' if m % 1440 == 0 else f'{m // 60}h' if m % 60 == 0 else f'{m}m'

def pct(v):
    return min(100.0, max(0.0, float(v))) if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v else None

def epoch_value(v):
    if isinstance(v, (int, float)) and not isinstance(v, bool): return v / 1000 if v > 1e12 else float(v)
    return usage.epoch_of(v) if v is not None else None

def win(name, used, minutes, resets, source, age):
    return {'name': name, 'used_pct': round(used, 1), 'window_minutes': minutes, 'resets_at': resets, 'source': source, 'age_s': max(0, round(age))}

# ---- adapters: each returns {'status': 'ok', 'windows': [...]} or {'status': 'unknown', 'reason': text} ----
def unknown(reason): return {'status': 'unknown', 'windows': [], 'reason': reason}

def codex_windows(home, now):
    base = home / '.codex' / 'sessions'
    if not base.is_dir(): return unknown('no Codex sessions folder')
    files = []
    for f in base.rglob('rollout-*.jsonl'):
        with contextlib.suppress(OSError): files.append((f.stat().st_mtime, f))
    for _, f in sorted(files, key=lambda x: x[0], reverse=True)[:5]:
        try: lines = f.read_text(encoding='utf-8', errors='replace').splitlines()
        except OSError: continue
        for line in reversed(lines):
            if '"rate_limits"' not in line: continue
            try: o = json.loads(line)
            except ValueError: continue
            p = o.get('payload') if isinstance(o, dict) else None
            rl = p.get('rate_limits') if isinstance(p, dict) and p.get('type') == 'token_count' else None
            if not isinstance(rl, dict): continue
            ts = usage.epoch_of(o.get('timestamp')) or now
            out = []
            for key in ('primary', 'secondary'):
                w = rl.get(key)
                if not isinstance(w, dict): continue
                used, mins, resets = pct(w.get('used_percent')), w.get('window_minutes'), epoch_value(w.get('resets_at'))
                if used is None: continue
                mins = mins if isinstance(mins, (int, float)) and not isinstance(mins, bool) else None
                if resets is not None and resets <= now: out.append(win(window_name(mins), 0, mins, None, 'refilled', now - ts))
                else: out.append(win(window_name(mins), used, mins, resets, 'codex session file', now - ts))
            if out: return {'status': 'ok', 'windows': out}
    return unknown('no rate limit data in the newest Codex sessions')

def claude_cache_path(home): return home / '.cache' / 'graph-skill' / 'claude-rate-limits.json'

def claude_windows(home, now):
    p = claude_cache_path(home)
    try: data = json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError): return unknown('the statusline helper is not enabled (run: npx graph-skill statusline --enable)')
    if not isinstance(data, dict): return unknown('the statusline cache is unreadable')
    written = epoch_value(data.get('written_at'))
    if written is None:
        with contextlib.suppress(OSError): written = p.stat().st_mtime
    if written is None or now - written > CLAUDE_MAX_AGE:
        return unknown(f"the statusline cache is stale ({'unknown age' if written is None else str(round((now - written) / 60)) + ' minutes old'}). It refreshes while Claude Code runs")
    out = []
    rl = data.get('rate_limits') if isinstance(data.get('rate_limits'), dict) else {}
    for key, mins in (('five_hour', 300), ('seven_day', 10080)):
        w = rl.get(key)
        used = pct(w.get('used_percentage')) if isinstance(w, dict) else None
        resets = epoch_value(w.get('resets_at')) if isinstance(w, dict) else None
        if used is None or (resets is not None and resets <= now): continue
        out.append(win(window_name(mins), used, mins, resets, 'statusline cache', now - written))
    return {'status': 'ok', 'windows': out} if out else unknown('the statusline cache has no current rate limits (stale reset time, or no subscription limits)')

def user_windows(user, now):
    """The user's own answer, recorded with budget --left-pct N --resets HH:MM. Dropped once its reset time has passed."""
    if not isinstance(user, dict) or pct(user.get('left_pct')) is None: return None
    resets = user.get('resets_at')
    if isinstance(resets, (int, float)) and resets <= now: return None
    return {'status': 'ok', 'windows': [win('user', 100 - pct(user['left_pct']), None, resets, 'user', now - (user.get('at_epoch') or now))]}

def parse_hhmm(text, now):
    """Next local occurrence of HH:MM as an epoch, or None."""
    m = HHMM.match((text or '').strip())
    if not m: return None
    t = time.localtime(now)
    cand = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, int(m.group(1)), int(m.group(2)), 0, 0, 0, -1))
    return cand if cand > now else cand + 86400

def usage_left(host, home, now, user=None):
    res = claude_windows(home, now) if host == 'claude' else codex_windows(home, now) if host == 'codex' else unknown(f'no usage source for host "{host}"')
    if res['status'] != 'ok':
        u = user_windows(user, now)
        if u: u['adapter_note'] = res['reason']; return u
    return res

# ---- estimates ----
def fresh(t):
    """Tokens that count for the estimate: input + output + cache write. Cache reads are cheap and left out."""
    return sum(int(t.get(k) or 0) for k in ('input', 'output', 'cache_write')) if isinstance(t, dict) else 0

def read_jsonl(p):
    out = []
    with contextlib.suppress(OSError):
        for line in p.read_text(encoding='utf-8').splitlines():
            with contextlib.suppress(ValueError):
                o = json.loads(line)
                if isinstance(o, dict): out.append(o)
    return out

def history_path(home): return home / '.config' / 'graph-skill' / 'history.jsonl'
def calib_path(home): return home / '.config' / 'graph-skill' / 'budget-calib.jsonl'

def pctile(vals, q):
    """Nearest-rank percentile."""
    s = sorted(vals); return s[min(len(s) - 1, max(0, math.ceil(len(s) * q) - 1))]

def defaults():
    with contextlib.suppress(OSError, ValueError):
        d = json.loads(SHIPPED.read_text(encoding='utf-8'))
        if isinstance(d.get('tiers'), dict): return d
    return {'tiers': {'deep': {'median': 120000, 'p80': 250000}, 'build': {'median': 80000, 'p80': 180000}, 'quick': {'median': 15000, 'p80': 40000}}}

def estimate_one(tier, role, hist, dflt):
    vals = [fresh(e.get('tokens')) for e in hist if e.get('tier') == tier and e.get('role') == role]
    vals = [v for v in vals if v > 0]
    if len(vals) >= MIN_HISTORY:
        return {'median': round(statistics.median(vals)), 'p80': pctile(vals, 0.8), 'source': f'your history, {len(vals)} samples', 'own': True}
    t = dflt['tiers'].get(tier) or dflt['tiers'].get(routing.DEFAULT_TIER) or {'median': 0, 'p80': 0}
    return {'median': int(t['median']), 'p80': int(t['p80']), 'source': DEFAULT_LABEL, 'own': False}

def lighter(n):
    """The lower tier for a non-critical low-risk node, else None. Review, design and other deep roles never go lighter."""
    tier = n.get('tier') or routing.tier_of(n)[0]
    if (n.get('risk') or 'low') == 'high' or tier == 'quick': return None
    if routing.ROLE_TIER.get(str(n.get('role') or '').lower(), routing.DEFAULT_TIER) == 'deep': return None
    return routing.TIERS[routing.TIERS.index(tier) - 1]

def plan_estimate(nodes, levels, hist, dflt, use_light=False):
    """Rows per node that is not finished. levels maps node id to its 1-based plan level."""
    rows = []
    for nd in nodes:
        if nd.get('status') in ('complete', 'cached', 'skipped'): continue
        tier = nd.get('tier') or routing.tier_of(nd)[0]
        if use_light and lighter(nd): tier = lighter(nd)
        rows.append(dict(estimate_one(tier, nd.get('role'), hist, dflt), id=nd['id'], level=levels.get(nd['id'], 1), tier=tier, role=nd.get('role')))
    return rows

def sum_by(rows, key, field):
    out = {}
    for r in rows: out[r[key]] = out.get(r[key], 0) + r[field]
    return out

# ---- calibration: tokens per 1% of a window, learned from past runs ----
def tokens_per_pct(home, host, wname):
    """(tokens per 1 percent, sample count). The value is None below 3 samples."""
    rates = [e['tokens'] / (e['pct_after'] - e['pct_before']) for e in read_jsonl(calib_path(home))
             if e.get('host') == host and e.get('window') == wname and isinstance(e.get('tokens'), (int, float)) and e.get('tokens') > 0
             and isinstance(e.get('pct_before'), (int, float)) and isinstance(e.get('pct_after'), (int, float)) and e['pct_after'] - e['pct_before'] >= 1]
    return (round(statistics.median(rates)) if len(rates) >= MIN_CALIB else None), len(rates)

def record_calibration(home, host, run, before, after, tokens, now):
    """One line per window when usage rose between the budget step and finish with no refill in between. Returns lines added."""
    if tokens <= 0: return 0
    p = calib_path(home); seen = {(e.get('run'), e.get('window')) for e in read_jsonl(p)}
    new = []
    for b in before:
        a = next((x for x in after if x['name'] == b['name'] and x['window_minutes'] == b['window_minutes']), None)
        if not a or 'user' in (a['source'], b['source']) or 'refilled' in (a['source'], b['source']) or (run, b['name']) in seen: continue
        if (b['resets_at'] or 0) and (a['resets_at'] or 0) and abs(a['resets_at'] - b['resets_at']) > 120: continue  # refilled in between
        if a['used_pct'] - b['used_pct'] < 1: continue
        new.append({'at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now)), 'host': host, 'window': b['name'], 'window_minutes': b['window_minutes'],
                    'run': run, 'pct_before': b['used_pct'], 'pct_after': a['used_pct'], 'tokens': tokens})
    if new:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, 'a', encoding='utf-8') as f: f.write(''.join(json.dumps(e) + '\n' for e in new))
    return len(new)

# ---- recommendation ----
def capacity(windows, home, host):
    """[(window, tokens left or None, samples)]. Tokens left needs a learned tokens-per-percent."""
    out = []
    for w in windows:
        tpp, k = tokens_per_pct(home, host, w['name'])
        out.append((w, None if tpp is None else (100 - w['used_pct']) * tpp, k))
    return out

def fit_levels(rows, cap):
    """Highest level L such that levels up to L fit in cap tokens (by p80), the next level after it, and the count."""
    per = sum_by(rows, 'level', 'p80'); used = 0; done = []
    for lv in sorted(per):
        if used + per[lv] > cap: break
        used += per[lv]; done.append(lv)
    after = [lv for lv in sorted(per) if lv not in done]
    return (done[-1] if done else 0), (after[0] if after else None)

def recommend(res, rows, rows_light, light_ids, home, host, now, run=None):
    """A dict with kind and text. kind: all, light, levels, wait, unknown."""
    if res['status'] != 'ok' or not res['windows']:
        return {'kind': 'unknown', 'text': 'unknown: user decides. Usage left is unknown here. Ask the user how much is left and when it resets, then run budget --run <id> --left-pct N --resets HH:MM.'}
    if not rows: return {'kind': 'all', 'text': 'all now: no unfinished nodes to estimate.'}
    caps = capacity(res['windows'], home, host)
    known = [(w, c) for w, c, _ in caps if c is not None]
    if not known:
        return {'kind': 'unknown', 'text': 'unknown: user decides. Tokens cannot be converted to percent yet (needs 3 finished runs on this host with usage numbers). Show the user the left percent and the token estimate.'}
    w, cap = min(known, key=lambda x: x[1])
    total, total_light = sum(r['p80'] for r in rows), sum(r['p80'] for r in rows_light)
    if total <= cap: return {'kind': 'all', 'text': f"all now: the plan needs up to about {n(total)} tokens and about {n(cap)} are left in the {w['name']} window."}
    if light_ids and total_light <= cap:
        return {'kind': 'light', 'light_ids': light_ids, 'text': f"now with lighter tier for non-critical low-risk nodes ({', '.join(light_ids)}): set --needs one tier lower on them while they are pending, then approve again. About {n(total_light)} tokens then fit in the {w['name']} window (about {n(cap)} left)."}
    last, nxt = fit_levels(rows, cap)
    when = w.get('resets_at')
    at = f"after the reset at {local_time(when, now)}" if when else 'after the window resets'
    if last >= 1 and nxt:
        s = f"levels 1-{last} now and the rest {at}: run defer {run or '<run>'} --from-level {nxt}" + (f" --not-before {local_iso(when)}" if when else ' --not-before <ISO time>') + '. Nothing is scheduled. Run resume then.'
        return {'kind': 'levels', 'fit_level': last, 'from_level': nxt, 'resets_at': when, 'text': s}
    return {'kind': 'wait', 'resets_at': when, 'text': f"nothing fits now: wait {at}, or the user decides (lighter tiers or a smaller plan). Needs up to about {n(total)} tokens, about {n(cap)} left."}

# ---- build and report ----
def build(d, levels, home, now, host, user=None):
    """The budget record stored in state.json under budget."""
    res = usage_left(host, home, now, user)
    hist, dflt = read_jsonl(history_path(home)), defaults()
    nodes = d.get('nodes', [])
    rows = plan_estimate(nodes, levels, hist, dflt)
    cands = [nd['id'] for nd in nodes if nd.get('status') not in ('complete', 'cached', 'skipped') and lighter(nd)]
    rows_light = plan_estimate(nodes, levels, hist, dflt, use_light=True) if cands else rows
    rec = recommend(res, rows, rows_light, cands, home, host, now, d.get('id'))
    conv = {w['name']: dict(zip(('tokens_per_pct', 'samples'), tokens_per_pct(home, host, w['name']))) for w in res['windows']}
    b = {'at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now)), 'at_epoch': now, 'host': host, 'status': res['status'], 'reason': res.get('reason'),
         'before': res['windows'], 'conversion': conv, 'estimate': rows, 'recommendation': rec}
    if res.get('adapter_note'): b['adapter_note'] = res['adapter_note']
    return b

def lines(b, now=None):
    """Plain text for the budget command and the approval message."""
    now = now if now is not None else time.time()
    L = [f"Usage budget ({APPROX})", 'Usage left:']
    if b['status'] != 'ok' or not b['before']:
        L.append(f"  unknown: {b.get('reason') or 'no source'}")
    for w in b['before']:
        age = f", {w['age_s']}s old" if w['age_s'] else ''
        rs = f", resets {local_time(w['resets_at'], now)}" if w.get('resets_at') else ''
        L.append(f"  {w['name']}: {round(100 - w['used_pct'])}% left ({round(w['used_pct'])}% used){rs}, source {w['source']}{age}")
    if b.get('adapter_note'): L.append(f"  (the host source said: {b['adapter_note']})")
    rows = b['estimate']
    if rows:
        L.append('Estimate (fresh tokens = input + output + cache write, median-p80):')
        lv = sum_by(rows, 'level', 'median'), sum_by(rows, 'level', 'p80')
        cnt = sum_by([dict(r, one=1) for r in rows], 'level', 'one')
        for k in sorted(lv[0]): L.append(f"  level {k}: {n(lv[0][k])}-{n(lv[1][k])} ({cnt[k]} node{'s' if cnt[k] != 1 else ''})")
        tm, tp = sum_by(rows, 'tier', 'median'), sum_by(rows, 'tier', 'p80')
        L.append('  by tier: ' + ', '.join(f"{t} {n(tm[t])}-{n(tp[t])}" for t in tm))
        L.append(f"  total: {n(sum(r['median'] for r in rows))}-{n(sum(r['p80'] for r in rows))}")
        srcs = sorted({r['source'] for r in rows})
        L.append('  basis: ' + '; '.join(srcs))
    else: L.append('Estimate: no unfinished nodes yet. Run budget again after you add the plan.')
    for name, c in (b.get('conversion') or {}).items():
        L.append(f"  {name}: about {n(c['tokens_per_pct'])} tokens per 1% (from {c['samples']} runs)" if c['tokens_per_pct'] else f"  {name}: cannot convert to % yet ({c['samples']} of {MIN_CALIB} runs learned), tokens only")
    L.append('Recommendation: ' + b['recommendation']['text'])
    return L

def finish(d, home, now, host, fresh_total):
    """Record usage after the run and learn tokens per percent. Returns the 'after' windows."""
    b = d.get('budget')
    if not isinstance(b, dict): return None
    res = usage_left(host, home, now, d.get('budget_user'))
    b['after'] = res['windows']; b['after_reason'] = res.get('reason')
    if res['status'] == 'ok' and b.get('before'): b['calibration_lines'] = record_calibration(home, host, d.get('id'), b['before'], res['windows'], fresh_total, now)
    return b

def report_lines(d):
    """Markdown for the summary: usage before and after, estimate versus actual. Never raises on partial data."""
    if d.get('skip_budget'): return [f"The budget step was skipped: {d['skip_budget']}"]
    b = d.get('budget')
    if not isinstance(b, dict): return ['No budget was recorded for this run.']
    L = [APPROX, '']
    after = {w['name']: w for w in b.get('after') or []}
    if b.get('before'):
        L += ['| Window | Used before | Used after | Source |', '|---|---|---|---|']
        for w in b['before']:
            a = after.get(w['name'])
            L.append(f"| {w['name']} | {round(w['used_pct'])}% | {str(round(a['used_pct'])) + '%' if a else 'unknown'} | {w['source']}" + (f" -> {a['source']}" if a and a['source'] != w['source'] else '') + ' |')
        L.append('')
    else: L += [f"Usage left was unknown at the budget step ({b.get('reason') or 'no source'}).", '']
    rows, u = b.get('estimate') or [], d.get('usage') or {}
    if rows and u.get('status') == 'ok':
        actual = {}
        for r in u.get('rows') or []: actual[r['who']] = actual.get(r['who'], 0) + fresh(r)
        L += ['| Node | Tier | Estimate (median) | Actual | Error | Basis |', '|---|---|---|---|---|---|']
        te = ta = 0
        for r in rows:
            a = actual.get(r['id'], 0); te += r['median']; ta += a
            L.append(f"| {r['id']} | {r['tier']} | {n(r['median'])} | {n(a)} | {err(a, r['median'])} | {r['source']} |")
        L.append(f"| total | | {n(te)} | {n(ta)} | {err(ta, te)} | |")
        L.append('')
    elif rows: L += ['Actual tokens are unknown, so there is no estimate comparison.', '']
    if b.get('calibration_lines'): L.append(f"Learned tokens per percent from this run for {b['calibration_lines']} window(s).")
    return L

def err(actual, est):
    return 'n/a' if not est else f'{(actual - est) / est * 100:+.0f}%'
