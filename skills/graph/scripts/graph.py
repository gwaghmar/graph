#!/usr/bin/env python3
"""Graph Skill runtime: a plan gate and run recorder. Rules are enforced here, not in prompts."""
from __future__ import annotations
import argparse, contextlib, hashlib, html, json, os, re, shlex, shutil, subprocess, sys, time, uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from preflight import run_proc

ROOT = Path('.graph/runs')
STATUSES = ('pending', 'running', 'retrying', 'complete', 'failed', 'skipped', 'cached')
DONE = ('complete', 'cached', 'skipped')
RUN_ID = re.compile(r'^[A-Za-z0-9_-]+$')

def die(code, msg):
    print(msg, file=sys.stderr); raise SystemExit(code)


def now(): return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
def epoch(): return time.time()

def check_run_id(run_id):
    if not RUN_ID.match(run_id or ''): die(2, f'Bad run id: {run_id!r}. Use only letters, digits, "_" and "-".')

def ensure_ignore():
    """Keep run history and the preflight cache out of git, on every install route."""
    g = Path('.graph/.gitignore')
    if g.exists(): return
    with contextlib.suppress(OSError):
        g.parent.mkdir(parents=True, exist_ok=True); g.write_text('*\n', encoding='utf-8')

def load(run_id):
    check_run_id(run_id)
    p = ROOT / run_id / 'state.json'
    if not p.exists(): die(2, f'Unknown run: {run_id}. Run "init" first or check the run id.')
    return p, json.loads(p.read_text(encoding='utf-8'))

def save(p, data):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')
    os.replace(tmp, p)

def safe_id(value):
    cleaned=''.join(c if c.isalnum() else '_' for c in value)
    return cleaned if cleaned==value else f"{cleaned}_{hashlib.sha256(value.encode()).hexdigest()[:4]}"

@contextlib.contextmanager
def run_lock(run_id, timeout=10, stale=30):
    """Serialize state read-modify-write across parallel node updates."""
    (ROOT/run_id).mkdir(parents=True, exist_ok=True)
    lock=ROOT/run_id/'.lock'; deadline=epoch()+timeout
    while True:
        try:
            fd=os.open(lock, os.O_CREAT|os.O_EXCL|os.O_WRONLY); break
        except FileExistsError:
            with contextlib.suppress(OSError):
                if epoch()-lock.stat().st_mtime>stale: lock.unlink(); continue
            if epoch()>deadline: die(3, f'Timed out waiting for state lock: {lock}. Retry, or delete the lock file if no other command runs.')
            time.sleep(0.05)
    try: yield
    finally:
        os.close(fd)
        with contextlib.suppress(OSError): lock.unlink()

def get_node(d, node_id):
    return next((n for n in d.get('nodes', []) if n.get('id') == node_id), None)

def descendants(d, roots):
    selected = set(roots)
    changed = True
    while changed:
        changed = False
        for n in d.get('nodes', []):
            if n['id'] not in selected and any(dep in selected for dep in n.get('depends_on', [])):
                selected.add(n['id']); changed = True
    return selected

def cmd_init(a):
    if a.max_retries < 0: die(2, '--max-retries must be 0 or more.')
    rid = a.id or uuid.uuid4().hex[:10]
    check_run_id(rid)
    if (ROOT / rid).exists(): die(3, f'Refused: run {rid} already exists. Pick a new --id, or continue it with "resume".')
    ensure_ignore()
    p = ROOT / rid / 'state.json'
    data = {
        'schema_version': 3, 'id': rid, 'task': a.task, 'host': a.host, 'max_retries': a.max_retries,
        'quality_commands': a.quality_command or [], 'status': 'planning', 'created_at': now(), 'updated_at': now(),
        'git': git_info(), 'nodes': [], 'events': [], 'quality': []
    }
    save(p, data); render_files(p, data); print(rid)

def unmet(d, n):
    return [dep for dep in n.get('depends_on', []) if (get_node(d, dep) or {}).get('status') not in DONE]

TARGETS = ('web', 'phone', 'desktop', 'api', 'data', 'none')
RISKS = ('low', 'high')

def field_problems(n):
    """Problems with the optional check, target and risk fields of one node."""
    out = []
    if n.get('check') is not None and not (isinstance(n['check'], str) and n['check'].strip()):
        out.append(f"{n['id']}: check must be a non-empty command string")
    if n.get('target') is not None and n['target'] not in TARGETS:
        out.append(f"{n['id']}: target must be one of {', '.join(TARGETS)}")
    if n.get('risk') is not None and n['risk'] not in RISKS:
        out.append(f"{n['id']}: risk must be one of {', '.join(RISKS)}")
    return out

def tail_lines(text, n=40):
    return '\n'.join((text or '').splitlines()[-n:])

def run_check(command, timeout):
    """Run a plan check command. shell=True is used ONLY for plan and quality commands: they may contain
    pipes or &&, and are never built from untrusted input by this script."""
    started = epoch()
    code, out, _, timed_out = run_proc(command, timeout, shell=True, merge=True)
    if timed_out: code, out = None, out + f'\n[check timed out after {timeout}s]'
    return {'check_exit': code, 'check_output': tail_lines(out), 'check_ms': round((epoch() - started) * 1000)}

# Allowed moves. A new node counts as pending. "retrying" is only reached from failed.
MOVES = {'pending': ('pending', 'running', 'skipped'), 'running': ('running', 'complete', 'failed'),
         'retrying': ('retrying', 'complete', 'failed'), 'failed': ('failed', 'retrying'),
         'complete': ('complete',), 'skipped': ('skipped',), 'cached': ('cached',)}

def guard(a, d, n):
    """Refuse any change the rules forbid. n is the stored node, or None for a new node."""
    prev = (n or {}).get('status', 'pending')
    if a.status == 'cached': die(3, f"Refused: {a.node} cannot be marked cached, nothing records a cache hit. Run it, or skip it with --reason.")
    if a.status not in MOVES[prev]:
        die(3, f"Refused: {a.node} cannot go from {prev} to {a.status}. Allowed next: {', '.join(MOVES[prev]) or 'none'}.")
    if a.status == 'skipped' and not (a.reason or (n or {}).get('skip_reason')):
        die(3, f"Refused: skipping {a.node} needs a reason. Add --reason \"<why>\".")
    old_deps = (n or {}).get('depends_on', [])
    deps = a.depends_on if a.depends_on is not None else old_deps
    # Plan fields may change only in a planning call: node still pending, run still planning, new status pending.
    planning_call = prev == 'pending' and a.status == 'pending' and d.get('status') == 'planning'
    if n is not None and deps != old_deps and not planning_call:
        die(3, f"Refused: dependencies of {a.node} are locked once planning ends. Set them with --status pending before the first node runs.")
    if n is not None and a.check is not None and a.check != n.get('check') and not planning_call:
        die(3, f"Refused: the check of {a.node} is locked once planning ends. Set it with --status pending before the first node runs.")
    if a.status != 'pending':
        waiting = unmet(d, {'depends_on': deps})
        if waiting: die(3, f"Refused: {a.node} cannot move to {a.status}, waiting on {', '.join(waiting)}. Finish those nodes first.")
    if a.status in ('running', 'retrying') and prev in ('pending', 'failed'):
        limit = d.get('max_retries', 2) + 1
        if (n or {}).get('attempts', 0) >= limit:
            die(3, f"Refused: {a.node} used all {limit} attempts (max retries {limit - 1}). Mark it failed and stop, or ask the user.")
    return deps

def cmd_node(a):
    if a.status not in STATUSES: die(2, f"Invalid status '{a.status}'. Use one of: {', '.join(STATUSES)}.")
    if a.check_timeout < 1: die(2, '--check-timeout must be 1 or more.')
    new = {'id': a.node, 'check': a.check, 'target': a.target, 'risk': a.risk}
    bad = field_problems(new)
    if bad: die(2, f"Bad plan field: {bad[0]}.")
    _, d0 = load(a.run)
    n0 = get_node(d0, a.node)
    guard(a, d0, n0)
    cmd = (n0 or {}).get('check')
    result = run_check(cmd, a.check_timeout) if a.status == 'complete' and cmd else None
    with run_lock(a.run):
        apply_node(a, result)

def apply_node(a, result):
    p, d = load(a.run)
    n = get_node(d, a.node)
    created = n is None
    deps = guard(a, d, n)
    if created:
        n = {'id': a.node, 'role': a.role, 'status': 'pending', 'depends_on': [],
             'model': a.model, 'attempts': 0, 'files': [], 'created_at': now(),
             'started_epoch': None, 'ended_epoch': None}
    previous = None if created else n.get('status')
    if created: d['nodes'].append(n)
    n.update({k: v for k, v in {'role': a.role, 'status': a.status, 'model': a.model, 'check': a.check,
                                'target': a.target, 'risk': a.risk, 'goal': a.goal, 'skip_reason': a.reason}.items() if v is not None})
    n['depends_on'] = deps
    if a.files: n['files'] = sorted(set(n.get('files', []) + a.files))
    if a.status in ('running', 'retrying') and previous not in ('running', 'retrying'):
        n['attempts'] = n.get('attempts', 0) + 1
        n['started_epoch'] = epoch(); n['started_at'] = now()
        if d.get('status') == 'planning': d['status'] = 'running'
    refused = None
    if result:
        n.update(result)
        if result['check_exit'] != 0:
            n['status'] = 'failed'
            refused = (f"Refused: check failed for {a.node} (exit {result['check_exit']}), node set to failed. "
                       f"Run \"brief {a.run} {a.node}\" and hand it to a fresh subagent to retry.")
    if n['status'] in ('complete', 'failed', 'skipped', 'cached'):
        n['ended_epoch'] = epoch(); n['ended_at'] = now()
        if n.get('started_epoch'): n['duration_ms'] = round((n['ended_epoch'] - n['started_epoch']) * 1000)
    n['updated_at'] = now(); d['updated_at'] = now(); save(p, d); render_files(p, d)
    print(ascii_graph(d))
    if refused: die(3, refused)

def cmd_brief(a):
    _, d = load(a.run)
    n = get_node(d, a.node) or die(2, f'Unknown node: {a.node}. Check the node id.')
    limit = d.get('max_retries', 2) + 1
    out = [f"RETRY BRIEF for node {n['id']} (attempt {n.get('attempts', 0) + 1} of {limit})",
           f"Goal: {n.get('goal') or n.get('role', '')}", f"Task: {d['task']}",
           f"Check: {n.get('check') or 'none'}"]
    if n.get('check_output'):
        out += [f"Last check exit code: {n.get('check_exit')}", 'Failing output (tail):', n['check_output']]
    else: out.append('No failing output recorded.')
    if n.get('files'): out.append('Files recorded for this node: ' + ', '.join(n['files']))
    if shutil.which('git'):
        try:
            r = subprocess.run(['git', 'diff', '--stat'], text=True, capture_output=True, encoding='utf-8', errors='replace', timeout=10)
            if r.returncode == 0 and r.stdout.strip(): out += ['Files changed (git diff --stat):', r.stdout.rstrip()]
        except (OSError, subprocess.SubprocessError): pass
    out.append('Start fresh: read the files, fix the cause of the failure, make the check pass. Do not rely on earlier context.')
    print('\n'.join(out))

def verify_steps(n, r):
    """Ordered (name, state, note) steps for the node target. state is ok or no. Stops after the first missing step."""
    t, v, os_ = n.get('target') or 'none', r.get('verify', {}), r.get('os')
    mcp = [m.lower() for m in r.get('mcp', [])]
    def has(*hints): return any(h in m for m in mcp for h in hints)
    def yn(ok, work): return ('ok', '') if ok else ('no', work)
    chrome = has('chrome', 'browser', 'playwright', 'puppeteer')
    shot = os_ in ('mac', 'windows') or any(shutil.which(x) for x in ('scrot', 'import', 'gnome-screenshot', 'grim'))
    mobile = ('iOS Simulator', *yn(os_ == 'mac' and v.get('ios_simulator') == 'yes', 'install Xcode, or use browser device emulation only')) \
        if os_ == 'mac' else ('Android emulator', *yn(v.get('android_emulator') == 'yes', 'install Android Studio and create an AVD, or ask the user to test on a phone'))
    plans = {
        'web': [('build, tests, http status', 'ok', ''),
                ('headless browser screenshot', *yn(v.get('headless_browser') == 'yes', 'install Chrome or Edge, or ask the user for a screenshot')),
                ('real browser', *yn(chrome, 'add a browser MCP server, or ask the user to open the page'))],
        'phone': [('browser device emulation', *yn(v.get('headless_browser') == 'yes' or chrome, 'install Chrome or Edge')),
                  mobile,
                  ('iPhone Mirroring (needs user consent)', *yn(os_ == 'mac' and v.get('iphone_mirroring') == 'yes', 'ask the user to check on the real phone'))],
        'desktop': [('tests and launch exit code', 'ok', ''),
                    ('screenshot', *yn(shot, 'install a screenshot tool, or ask the user for one')),
                    ('computer-use', *yn(has('computer'), 'add a computer-use MCP server, or ask the user to look'))],
        'api': [('contract tests or curl', 'ok', ''), ('staging call (needs staging URL and user consent)', 'ok', '')],
        'data': [('counts and schema diff', 'ok', ''), ('reconcile with source', 'ok', '')]}
    out = []
    for step in plans.get(t, []):
        out.append(step)
        if step[1] == 'no': break
    if t == 'phone' and os_ not in ('mac',): out = [s for s in out if 'iPhone Mirroring' not in s[0]]
    return out

def cmd_verify_plan(a):
    _, d = load(a.run)
    n = get_node(d, a.node) or die(2, f'Unknown node: {a.node}. Check the node id.')
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import preflight
        r = preflight.scan(Path.cwd(), Path.home(), os.environ)
    except Exception as e:
        die(2, f'preflight scan failed ({type(e).__name__}). Run "preflight --refresh" and try again.')
    t, risk = n.get('target') or 'none', n.get('risk') or 'low'
    if t == 'none': print(f"{a.node}: target none, no extra verification. Rely on the node check."); return
    steps = verify_steps(n, r)
    print(f"Verify {a.node}: target {t}, risk {risk}, cheapest first")
    for i, (name, st, note) in enumerate(steps, 1):
        print(f"  {i}. {name}" + ('' if st == 'ok' else f" [missing] workaround: {note}"))
    if risk == 'high' and len(steps) > 1 and steps[1][1] == 'ok':
        print(f"High risk: after step 1 passes, escalate one step to step 2 ({steps[1][0]}).")
    elif risk == 'high': print('High risk: step 2 is not available here, use the workaround and report it.')

def cmd_next(a):
    _, d = load(a.run); limit = d.get('max_retries', 2) + 1
    ready = [n['id'] for n in d.get('nodes', []) if not unmet(d, n) and (
        n.get('status') == 'pending' or (n.get('status') == 'failed' and n.get('attempts', 0) < limit))]
    print('\n'.join(ready) if ready else 'none')

def cmd_event(a):
    p,d=load(a.run); d['events'].append({'at':now(),'type':a.type,'message':a.message});d['updated_at']=now();save(p,d);render_files(p,d)

def mermaid(d):
    lines=['flowchart LR']
    for n in d.get('nodes',[]):
        label=f"{n['id']}<br/>{n.get('role','')}<br/>{n.get('status','')}".replace('"','#quot;')
        lines.append(f'  {safe_id(n["id"])}["{label}"]')
    for n in d.get('nodes',[]):
        for dep in n.get('depends_on',[]): lines.append(f'  {safe_id(dep)} --> {safe_id(n["id"])}')
    return '\n'.join(lines)+'\n'

def stats(d):
    nodes=d.get('nodes',[]); statuses={}
    for n in nodes: statuses[n.get('status','unknown')]=statuses.get(n.get('status','unknown'),0)+1
    duration=sum(n.get('duration_ms',0) or 0 for n in nodes)
    starts=[n['started_epoch'] for n in nodes if n.get('started_epoch')]
    ends=[n.get('ended_epoch') or epoch() for n in nodes if n.get('started_epoch')]
    wall=round((max(ends)-min(starts))*1000) if starts else 0
    return {'nodes':len(nodes),'completed':sum(statuses.get(x,0) for x in DONE),
            'failed':statuses.get('failed',0),'retries':sum(max(0,n.get('attempts',0)-1) for n in nodes),
            'files_changed':len({f for n in nodes for f in n.get('files',[])}),'duration_ms':duration,'wall_ms':wall}

GLYPHS={'complete':'✔','cached':'⚡','failed':'✖','running':'▶','retrying':'↻','pending':'○','skipped':'~'}

def bar(done,total,width=14):
    filled=round(done/total*width) if total else 0
    return '█'*filled+'░'*(width-filled)

def node_levels(d):
    nodes=d.get('nodes',[]); levels={}
    def level(n,seen=frozenset()):
        if n['id'] in levels: return levels[n['id']]
        if n['id'] in seen: return 0
        deps=[get_node(d,x) for x in n.get('depends_on',[])]
        levels[n['id']]=1+max((level(x,seen|{n['id']}) for x in deps if x),default=-1)
        return levels[n['id']]
    for n in nodes: level(n)
    return levels

def ascii_graph(d):
    nodes=d.get('nodes',[]); levels=node_levels(d); s=stats(d)
    head=f"◉ {d['id']} · {d.get('status','?')}  [{bar(s['completed'],s['nodes'])}] {s['completed']}/{s['nodes']}"
    extras=[e for e in (f"✖ {s['failed']} failed" if s['failed'] else '', f"↻ {s['retries']} retried" if s['retries'] else '', f"⚡ {sum(1 for n in nodes if n.get('status')=='cached')} cached" if any(n.get('status')=='cached' for n in nodes) else '') if e]
    if extras: head+='  '+' · '.join(extras)
    lines=[head]
    ordered=sorted(nodes,key=lambda n:(levels[n['id']],nodes.index(n)))
    per_level={}
    for n in ordered: per_level.setdefault(levels[n['id']],[]).append(n)
    for n in ordered:
        lv=levels[n['id']]
        conn='' if lv==0 else '   '*(lv-1)+('└─ ' if per_level[lv][-1] is n else '├─ ')
        tags=' ⚡' if n.get('status')=='cached' else ''
        if n.get('attempts',0)>1: tags+=f" ↻x{n['attempts']-1}"
        dur=f" · {n['duration_ms']/1000:.1f}s" if n.get('duration_ms') else ''
        deps=' ← '+','.join(n['depends_on']) if n.get('depends_on') else ''
        lines.append(f"{conn}{GLYPHS.get(n.get('status'),'?')} {n['id']} · {n.get('role','')}{dur}{tags}{deps}")
    return '\n'.join(lines)

def summary_text(d):
    s=stats(d)
    rule='━'*46
    lines=[rule, f"  Graph summary · {d.get('status','?')}", rule, f"Task: {d['task']}", '', ascii_graph(d), '']
    checks=d.get('quality',[])
    if checks:
        lines.append('Checks')
        lines+= [f"  {'✔' if q.get('status') in ('passed','no-tests') else '✖'} {q.get('command','')} · {q.get('duration_ms',0)}ms" for q in checks]
    files=sorted({f for n in d.get('nodes',[]) for f in n.get('files',[])})
    if files: lines.append(f"Files ({len(files)}): "+', '.join(files))
    lines.append(f"Stats: retries {s['retries']} · wall {s['wall_ms']/1000:.1f}s · compute {s['duration_ms']/1000:.1f}s")
    lines.append(f"Report: {ROOT/d['id']/'graph.html'}")
    lines.append(rule)
    return '\n'.join(lines)

def build_summary(d):
    """Plain-language run summary built only from state. Short sentences, no claim the state does not show."""
    nodes=d.get('nodes',[]); status=d.get('status','unknown')
    failed=[n for n in nodes if n.get('status')=='failed']
    open_=[n for n in nodes if n.get('status') not in DONE and n.get('status')!='failed']
    q=d.get('quality',[]); latest={x['command']:x for x in q}
    badq=[x for x in latest.values() if x.get('required') and x.get('status') in ('failed','timeout')]
    skipped=[n['id'] for n in nodes if n.get('status') in ('skipped','cached')]
    nocheck=[n['id'] for n in nodes if n.get('status') in ('complete',) and n.get('check_exit') is None]
    ran=[n for n in nodes if n.get('check_exit') is not None]+[x for x in q if x.get('status') in ('passed','no-tests')]
    done_text='The run is complete. All nodes are done.'
    if skipped: done_text+=f" Skipped or cached: {', '.join(skipped)}."
    if nocheck: done_text+=f" No check ran for: {', '.join(nocheck)}."
    done_text+=' The checks that ran passed.' if ran else ' No check ran.'
    said={'complete':done_text,
          'complete-unverified':'The run is done, but no check proved the work. Treat it as unverified.',
          'failed':'The run failed.'}.get(status,f'The run is not finished. Status is {status}.')
    if status=='complete' and (failed or open_ or badq): said='The run says complete, but the state shows open problems. Do not trust it.'
    L=['# Run summary','',said,'','## What you asked','',d.get('task','') or 'No task recorded.','','## What changed','']
    if nodes:
        for n in nodes:
            f=n.get('files',[])
            L.append(f"- Node {n['id']} ({n.get('role','')}) is {n.get('status')}." + (f" Files: {', '.join(f)}." if f else ' No files recorded.'))
    else: L.append('- No nodes were recorded.')
    L+=['','## What was checked','']
    checks=0
    for n in nodes:
        if n.get('check_exit') is not None or n.get('check_output'):
            checks+=1; ok=n.get('check_exit')==0
            L.append(f"- Node {n['id']}: check `{n.get('check','')}` {'passed' if ok else 'failed'} (exit {n.get('check_exit')}).")
        elif n.get('status') in DONE and n.get('status')!='skipped' and not n.get('check'):
            L.append(f"- Node {n['id']}: no check ran. Nothing proves this node.")
    for x in q:
        checks+=1; L.append(f"- Quality check `{x.get('command','')}`: {x.get('status','unknown')}.")
    if not checks and not any(l.startswith('- Node') for l in L[L.index('## What was checked'):]): L.append('- No check ran.')
    L+=['','## What failed','']
    out=[f"- Node {n['id']} failed after {n.get('attempts',0)} attempt(s)." + (f" Check exit was {n['check_exit']}." if n.get('check_exit') not in (None,0) else '') for n in failed]
    out+=[f"- Quality check `{x.get('command','')}` {x.get('status')}." for x in badq]
    L+=out or ['- Nothing failed.']
    L+=['','## What you must do next','']
    todo=[f"- Fix node {n['id']}. Run the brief command for it and retry with a fresh agent." for n in failed]
    todo+=[f"- Fix the failing check `{x.get('command','')}`, then run quality again." for x in badq]
    todo+=[f"- Finish node {n['id']}. It is {n.get('status')}." for n in open_]
    if status=='complete-unverified': todo.append('- Test the result yourself. No check proved it.')
    todo+=[f"- {g}" for g in d.get('access_gaps',[])]
    L+=todo or ['- Nothing. All nodes are done and checked.']
    return '\n'.join(L)+'\n'

def verify_plans(d):
    """Verify steps per node with a target, from a preflight scan. Empty on any scan problem."""
    nodes=[n for n in d.get('nodes',[]) if (n.get('target') or 'none')!='none']
    if not nodes: return {}
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import preflight
        r=preflight.scan(Path.cwd(), Path.home(), os.environ)
        return {n['id']:[list(x) for x in verify_steps(n,r)] for n in nodes}
    except Exception: return {}

def md_html(text):
    out=[]; items=[]
    def flush():
        if items: out.append('<ul>'+''.join(items)+'</ul>'); items.clear()
    for line in text.splitlines():
        e=html.escape(line)
        if line.startswith('- '): items.append('<li>'+re.sub(r'`([^`]*)`',r'<code>\1</code>',html.escape(line[2:]))+'</li>'); continue
        flush()
        if line.startswith('## '): out.append(f'<h3>{html.escape(line[3:])}</h3>')
        elif line.startswith('# '): out.append(f'<h2>{html.escape(line[2:])}</h2>')
        elif line.strip(): out.append(f'<p>{e}</p>')
    flush(); return '\n'.join(out)

def node_evidence(n, steps):
    e=html.escape; st=n.get('status','')
    rows=[f"<b>Status:</b> {e(st)} · <b>Attempts:</b> {n.get('attempts',0)}"]
    if n.get('files'): rows.append('<b>Files:</b> '+e(', '.join(n['files'])))
    if n.get('target'): rows.append(f"<b>Target:</b> {e(n['target'])} · <b>Risk:</b> {e(n.get('risk') or 'low')}")
    if n.get('check'):
        code='no result' if n.get('check_exit') is None and not n.get('check_output') else f"exit {n.get('check_exit')}"
        rows.append(f"<b>Check:</b> <code>{e(n['check'])}</code> · {code}")
        out=n.get('check_output')
        if out: rows.append(f"<details><summary>Output tail</summary><pre>{e(out)}</pre></details>")
    else: rows.append('<b>Check:</b> none')
    if steps: rows.append('<b>Verify steps:</b><ol>'+''.join(f"<li>{e(a)}" + ('' if b=='ok' else f" <span class=bad>missing</span>: {e(c)}") + '</li>' for a,b,c in steps)+'</ol>')
    return f'<section class="card ev {e(st)}"><h3>{e(n["id"])} <small>{e(n.get("role",""))}</small></h3>'+''.join(f'<p>{r}</p>' if not r.startswith('<details') and not r.startswith('<b>Verify') else r for r in rows)+'</section>'

CSS=""":root{--bg:#fff;--fg:#1d2127;--card:#f5f6f8;--line:#d5d9df;--mute:#5d6672;--ok:#1b7f4d;--bad:#c62828;--warn:#9a6700}
@media(prefers-color-scheme:dark){:root{--bg:#0b0d10;--fg:#e8eaed;--card:#15191f;--line:#2a3038;--mute:#9aa0a6;--ok:#52b788;--bad:#ff6b6b;--warn:#ffd166}}
body{font-family:system-ui,sans-serif;background:var(--bg);color:var(--fg);margin:0;line-height:1.5}main{max-width:1000px;margin:auto;padding:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;margin:14px 0}h1{font-size:1.4rem}h3{margin:.6em 0 .2em}small,.muted{color:var(--mute)}
.node rect{fill:var(--card);stroke:var(--mute);stroke-width:2}.node.complete rect,.node.cached rect{stroke:var(--ok)}.node.failed rect{stroke:var(--bad)}.node.running rect,.node.retrying rect{stroke:var(--warn)}.node.skipped rect{stroke-dasharray:4}
text{fill:var(--fg);font-size:12px}svg{width:100%;height:auto}line{stroke:var(--mute)}pre{white-space:pre-wrap;word-break:break-word;overflow-x:auto}code{word-break:break-all}.bad{color:var(--bad)}.ev.failed{border-color:var(--bad)}.ev.complete{border-color:var(--ok)}"""

def render_files(p,d):
    out=p.parent; (out/'graph.mmd').write_text(mermaid(d), encoding='utf-8')
    running=not d.get('finished_at') and d.get('status') not in ('complete','complete-unverified','failed')
    refresh='<meta http-equiv="refresh" content="2">' if running else ''
    live=' · live, refreshes every 2s' if running else ''
    payload=json.dumps({'nodes':d.get('nodes',[])}).replace('<','\\u003c')
    steps=d.get('verify_steps') or {}
    ev=''.join(node_evidence(n,steps.get(n['id'])) for n in d.get('nodes',[]))
    page=f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">{refresh}<title>Graph run {html.escape(d['id'])}</title><style>{CSS}</style></head><body><main>
<h1>Graph run {html.escape(d['id'])} <small>{html.escape(str(d.get('status','')))}</small></h1><p class="muted">Updated {html.escape(d.get('updated_at',''))}{live}</p>
<section class="card">{md_html(build_summary(d))}</section>
<section class="card"><h2>Diagram</h2><svg id="graph" viewBox="0 0 900 300" role="img" aria-label="Execution graph"></svg></section>
<h2>Evidence per node</h2>{ev or '<p class="muted">No nodes yet.</p>'}
<script>const nodes={payload}.nodes;const svg=document.getElementById('graph'),NS='http://www.w3.org/2000/svg',L={{}};
function lv(n,s=new Set()){{if(L[n.id]!=null)return L[n.id];if(s.has(n.id))return 0;s.add(n.id);const d=n.depends_on||[];return L[n.id]=d.length?1+Math.max(...d.map(i=>{{const x=nodes.find(y=>y.id===i);return x?lv(x,new Set(s)):0}})):0}}
nodes.forEach(n=>lv(n));const by={{}},pos={{}};nodes.forEach(n=>(by[L[n.id]]??=[]).push(n));
Object.keys(by).forEach(l=>by[l].forEach((n,i)=>pos[n.id]={{x:20+l*190,y:20+i*80}}));const ps=Object.values(pos);
if(ps.length)svg.setAttribute('viewBox',`0 0 ${{Math.max(400,...ps.map(q=>q.x+170))}} ${{Math.max(120,...ps.map(q=>q.y+76))}}`);
function el(t,a,x){{const e=document.createElementNS(NS,t);for(const k in a)e.setAttribute(k,a[k]);if(x!=null)e.textContent=x;return e}}
nodes.forEach(n=>(n.depends_on||[]).forEach(d=>{{if(pos[d]&&pos[n.id])svg.appendChild(el('line',{{x1:pos[d].x+150,y1:pos[d].y+28,x2:pos[n.id].x,y2:pos[n.id].y+28,'stroke-width':2}}))}}));
nodes.forEach(n=>{{const p=pos[n.id],g=el('g',{{class:'node '+n.status}});g.append(el('rect',{{x:p.x,y:p.y,width:150,height:56,rx:9}}),el('text',{{x:p.x+10,y:p.y+22}},n.id),el('text',{{x:p.x+10,y:p.y+42}},n.status));svg.appendChild(g)}});</script></main></body></html>"""
    (out/'graph.html').write_text(page, encoding='utf-8')

def cmd_render(a):
    p,d=load(a.run); render_files(p,d); print(p.parent/'graph.html')

def detect_quality_commands():
    """Detected commands as shell strings."""
    candidates=[]
    if Path('package.json').exists():
        try:
            scripts=json.loads(Path('package.json').read_text(encoding='utf-8')).get('scripts',{})
            for name in ('lint','typecheck','test'):
                if name in scripts: candidates.append(['npm','run',name,'--if-present'])
        except Exception: pass
    tests_dir=Path('tests')
    pats=('test_*.py','*_test.py')
    has_py_tests=any(next(tests_dir.rglob(g),None) for g in pats) if tests_dir.is_dir() else False
    has_py_tests=has_py_tests or any(any(Path('.').glob(g)) for g in pats) or (tests_dir/'conftest.py').exists()
    if has_py_tests:
        with contextlib.suppress(OSError):
            if subprocess.run([sys.executable,'-c','import pytest'],capture_output=True).returncode==0: candidates.append([sys.executable,'-m','pytest','-q'])
    if Path('Cargo.toml').exists(): candidates.append(['cargo','test','--quiet'])
    if Path('go.mod').exists(): candidates.append(['go','test','./...'])
    join=subprocess.list2cmdline if os.name=='nt' else shlex.join
    return [join(c) for c in candidates]

def latest_required(d):
    """Latest record of each required quality command, in first-seen order."""
    need=list(d.get('quality_commands',[])); need+=[x['command'] for x in d.get('quality',[]) if x.get('required') and x['command'] not in need]
    return {c:next((x for x in reversed(d.get('quality',[])) if x.get('command')==c),None) for c in need}

def cmd_quality(a):
    _,d0=load(a.run)
    if a.command: commands=[(c,False) for c in a.command]
    else:
        commands=[(c,True) for c in dict.fromkeys(d0.get('quality_commands',[])+detect_quality_commands())]
    records=[];failed=False
    for command,required in commands:
        started=epoch()
        code,out,err,timed_out=run_proc(command,a.timeout,shell=True)
        is_pytest=' -m pytest' in command
        status='timeout' if timed_out else 'passed' if code==0 else 'no-tests' if (is_pytest and code==5) else 'failed'
        failed |= status in ('failed','timeout')
        record={'at':now(),'command':command,'status':status,'exit_code':code,'required':required,'duration_ms':round((epoch()-started)*1000),'stdout':out[-8000:],'stderr':err[-8000:]}
        records.append(record)
        print(f"{record['status'].upper()}: {command}" + ('' if required else ' (extra: does not satisfy the finish gate)'))
    with run_lock(a.run):
        p,d=load(a.run)
        batch=max((q.get('batch',0) for q in d.get('quality',[])),default=0)+1
        for r in records: r['batch']=batch
        if not commands: d['events'].append({'at':now(),'type':'quality','message':'No local quality command detected'})
        d.setdefault('quality',[]).extend(records)
        d['updated_at']=now();save(p,d);render_files(p,d)
    if not commands: print('No local quality command detected');return
    if failed: raise SystemExit(1)

def cmd_validate(a):
    _,d=load(a.run); nodes=d.get('nodes',[]); ids={n['id'] for n in nodes}; problems=[]
    for n in nodes:
        for dep in n.get('depends_on',[]):
            if dep==n['id']: problems.append(f"self-dependency: {n['id']}")
            elif dep not in ids: problems.append(f"unknown dependency: {n['id']} ← {dep}")
    for n in nodes: problems += field_problems(n)
    graph={n['id']:[dep for dep in n.get('depends_on',[]) if dep in ids and dep!=n['id']] for n in nodes}
    done=set()
    def visit(nid,path):
        if nid in done: return
        if nid in path:
            problems.append('cycle: '+' → '.join(path[path.index(nid):]+[nid])); return
        for dep in graph.get(nid,[]): visit(dep,path+[nid])
        done.add(nid)
    for nid in graph: visit(nid,[])
    if problems:
        print('\n'.join(f'✖ {x}' for x in problems)); raise SystemExit(1)
    print(f'✔ plan valid: {len(nodes)} nodes, all dependencies known, no cycles')

def js(x): return json.dumps(x, ensure_ascii=True)

def cmd_export_workflow(a):
    _, d = load(a.run); nodes = d.get('nodes', [])
    if not nodes: die(2, f'Run {a.run} has no nodes. Add the plan first, then export.')
    try:
        with contextlib.redirect_stdout(sys.stderr): cmd_validate(argparse.Namespace(run=a.run))
    except SystemExit:
        die(3, f'Refused: plan for {a.run} is not valid. Fix it until "validate {a.run}" passes, then export.')
    levels = node_levels(d); by = {}
    for n in nodes: by.setdefault(levels[n['id']], []).append(n)
    g = f'"{Path(sys.executable or "python3").as_posix()}" "{Path(__file__).resolve().as_posix()}"'
    def mark(n, status): return f"{g} node {shlex.quote(a.run)} {shlex.quote(n['id'])} --role {shlex.quote(n.get('role') or 'worker')} --status {status}"
    def prompt(n):
        lines = [f"You are one node of a graph run. Node: {n['id']}. Goal: {n.get('goal') or n.get('role') or n['id']}."]
        if n.get('files'): lines.append('Files: ' + ', '.join(n['files']) + '.')
        lines += ['1. Before work, run: ' + mark(n, 'running'), '2. Do the work.']
        if n.get('check'): lines.append(f"3. Check (the runtime runs it for you on complete): {n['check']}")
        lines += [f"{'4' if n.get('check') else '3'}. When done, run: {mark(n, 'complete')}",
                  'If a command exits 3, it is a refusal. Read the line and follow it. Do not work around it.',
                  f"If you cannot finish, run: {mark(n, 'failed')}",
                  'Reply with ok true only if the complete command succeeded, else ok false with a short summary.']
        return '\n'.join(lines)
    title = lambda i: f'Level {i + 1}'
    phases = ',\n'.join(f'    {{ title: {js(title(i))}, detail: {js(", ".join(n["id"] for n in by[k]))} }}' for i, k in enumerate(sorted(by)))
    out = ['export const meta = {', f'  name: {js("graph-" + a.run)},',
           f'  description: {js("Graph run " + a.run + ": " + str(d.get("task", ""))[:120])},',
           '  phases: [', phases, '  ],', '}', '',
           "const schema = { type: 'object', required: ['ok', 'summary'], properties: { ok: { type: 'boolean' }, summary: { type: 'string' } } }",
           'let stopped = false', '']
    for i in sorted(by):
        out += [f'if (!stopped) {{', f'  phase({js(title(i))})', '  const results = await parallel([']
        out += [f"    () => agent({js(prompt(n))}, {{ label: {js(n['id'])}, schema }})," for n in by[i]]
        out += ['  ])', '  const bad = results.findIndex(r => r === null || !r.ok)', '  if (bad !== -1) {',
                f'    log("Node failed or returned nothing at {title(i)}. Stopping so dependents do not start.")',
                f"    await agent({js('Run: ' + g + ' finish ' + shlex.quote(a.run) + ' --status failed')}, {{ label: 'mark-failed' }})",
                '    stopped = true', '  }', '}', '']
    out += ['if (!stopped) {', f"  await agent({js('Run: ' + g + ' quality ' + shlex.quote(a.run) + ' then ' + g + ' finish ' + shlex.quote(a.run) + ' --status complete')}, {{ label: 'finish' }})", '}', '']
    path = Path(a.out) if a.out else Path('.claude/workflows') / f'graph-{a.run}.js'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('\n'.join(out), encoding='utf-8')
    print(f'Wrote {path}: {len(nodes)} agents in {len(by)} levels. Run it with the Workflow tool.')

def cmd_retry_plan(a):
    _,d=load(a.run); failed=[n['id'] for n in d.get('nodes',[]) if n.get('status')=='failed']
    selected=descendants(d,failed) if a.include_dependents else set(failed)
    ordered=[n['id'] for n in d.get('nodes',[]) if n['id'] in selected]
    print(json.dumps({'failed':failed,'retry':ordered},indent=2))

def cmd_resume(a):
    candidates=[]
    for p in ROOT.glob('*/state.json'):
        try:
            d=json.loads(p.read_text(encoding='utf-8'))
            if d.get('status') not in ('complete','complete-unverified') and (not d.get('finished_at') or d.get('status')=='failed'): candidates.append((p.stat().st_mtime,d))
        except Exception: pass
    if not candidates: die(2,'No incomplete run found')
    _,d=max(candidates,key=lambda x:x[0])
    if d.get('finished_at'):
        with run_lock(d['id']):
            p,d=load(d['id']); d['status']='running'; d.pop('finished_at',None)
            d['events'].append({'at':now(),'type':'resume','message':'reopened a failed run'}); d['updated_at']=now(); save(p,d); render_files(p,d)
    print(json.dumps({'run':d['id'],'task':d['task'],'status':d['status'],'graph':str(ROOT/d['id']/'graph.html')},indent=2))

def git_info():
    def run(*args):
        try: r=subprocess.run(['git',*args],text=True,capture_output=True)
        except OSError: return None
        return r.stdout.strip() if r.returncode==0 else None
    return {'commit':run('rev-parse','HEAD'),'branch':run('branch','--show-current'),'dirty':bool(run('status','--porcelain'))}

def cmd_finish(a):
    p,d=load(a.run); status=a.status
    if status=='complete':
        open_nodes=[n['id'] for n in d.get('nodes',[]) if n.get('status') not in DONE]
        if open_nodes: die(3,f"Refused: cannot finish as complete, nodes not done: {', '.join(open_nodes)}. Finish them, or use --status failed.")
        req=latest_required(d)
        missing=[c for c,x in req.items() if x is None]
        bad=[c for c,x in req.items() if x and x.get('status') in ('failed','timeout')]
        if missing: die(3,f"Refused: required quality command never ran ({'; '.join(missing)}). Run quality, or use --status failed.")
        if bad: die(3,f"Refused: latest result of a required quality command failed ({'; '.join(bad)}). Fix it and run quality again, or use --status failed.")
        if not any(x.get('status')=='passed' for x in req.values()):
            status='complete-unverified'; print('No required quality check passed: run recorded as complete-unverified.')
    d['status']=status; d['finished_at']=now(); d['updated_at']=now()
    d['verify_steps']=verify_plans(d)
    d['access_gaps']=[f"Node {i} could not be fully verified: {a} is missing. Workaround: {c}." for i,st in d['verify_steps'].items() for a,b,c in st if b!='ok']
    save(p, d); render_files(p, d)
    text=build_summary(d); (p.parent/'summary.md').write_text(text, encoding='utf-8')
    print(text); print(f"Report: {p.parent/'graph.html'}")

def cmd_tree(a):
    _,d=load(a.run); print(ascii_graph(d))

def cmd_summary(a):
    _,d=load(a.run); print(summary_text(d))

def cmd_status(a):
    _,d=load(a.run); print(json.dumps({'run':d,'stats':stats(d)},indent=2))

def cmd_preflight(a):
    ensure_ignore()
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import preflight
        needs=[n.strip() for n in (a.needs or '').split(',') if n.strip()]
        print(preflight.run_preflight(needs, a.refresh, a.with_accounts, a.json))
    except ValueError as e: die(2, str(e))
    except Exception as e: print(f'preflight scan incomplete ({type(e).__name__}). Continue without it or retry with --refresh.')

def main():
    ap=argparse.ArgumentParser();sp=ap.add_subparsers(required=True)
    x=sp.add_parser('init');x.add_argument('task');x.add_argument('--id');x.add_argument('--host');x.add_argument('--max-retries',type=int,default=2);x.add_argument('--quality-command',action='append');x.set_defaults(fn=cmd_init)
    x=sp.add_parser('node');x.add_argument('run');x.add_argument('node');x.add_argument('--role',required=True);x.add_argument('--status',required=True);x.add_argument('--depends-on',nargs='*');x.add_argument('--model');x.add_argument('--files',nargs='*');x.add_argument('--check');x.add_argument('--target');x.add_argument('--risk');x.add_argument('--goal');x.add_argument('--reason');x.add_argument('--check-timeout',type=int,default=300);x.set_defaults(fn=cmd_node,nolock=True)
    x=sp.add_parser('brief');x.add_argument('run');x.add_argument('node');x.set_defaults(fn=cmd_brief,nolock=True)
    x=sp.add_parser('verify-plan');x.add_argument('run');x.add_argument('node');x.set_defaults(fn=cmd_verify_plan,nolock=True)
    x=sp.add_parser('next');x.add_argument('run');x.set_defaults(fn=cmd_next,nolock=True)
    x=sp.add_parser('event');x.add_argument('run');x.add_argument('type');x.add_argument('message');x.set_defaults(fn=cmd_event)
    x=sp.add_parser('quality');x.add_argument('run');x.add_argument('--command',action='append');x.add_argument('--timeout',type=int,default=300);x.set_defaults(fn=cmd_quality,nolock=True)
    x=sp.add_parser('validate');x.add_argument('run');x.set_defaults(fn=cmd_validate,nolock=True)
    x=sp.add_parser('retry-plan');x.add_argument('run');x.add_argument('--include-dependents',action='store_true');x.set_defaults(fn=cmd_retry_plan,nolock=True)
    x=sp.add_parser('export-workflow');x.add_argument('run');x.add_argument('--out');x.set_defaults(fn=cmd_export_workflow,nolock=True)
    x=sp.add_parser('resume');x.set_defaults(fn=cmd_resume)
    x=sp.add_parser('render');x.add_argument('run');x.set_defaults(fn=cmd_render,nolock=True)
    x=sp.add_parser('tree');x.add_argument('run');x.set_defaults(fn=cmd_tree,nolock=True)
    x=sp.add_parser('summary');x.add_argument('run');x.set_defaults(fn=cmd_summary,nolock=True)
    x=sp.add_parser('finish');x.add_argument('run');x.add_argument('--status',default='complete',choices=['complete','failed']);x.set_defaults(fn=cmd_finish)
    x=sp.add_parser('status');x.add_argument('run');x.set_defaults(fn=cmd_status,nolock=True)
    x=sp.add_parser('preflight');x.add_argument('--needs');x.add_argument('--refresh',action='store_true');x.add_argument('--with-accounts',action='store_true');x.add_argument('--json',action='store_true');x.set_defaults(fn=cmd_preflight,nolock=True)
    a=ap.parse_args()
    try: sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception: pass
    run_id=getattr(a,'run',None)
    if run_id: check_run_id(run_id)
    if run_id and not getattr(a,'nolock',False) and (ROOT/run_id).exists():
        with run_lock(run_id): a.fn(a)
    else: a.fn(a)
if __name__=='__main__':main()
