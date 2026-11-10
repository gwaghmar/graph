#!/usr/bin/env python3
"""Graph Skill runtime: a plan gate and run recorder. Rules are enforced here, not in prompts."""
from __future__ import annotations
import argparse, contextlib, hashlib, html, json, os, shlex, shutil, subprocess, sys, time, uuid
from pathlib import Path

ROOT = Path('.graph/runs')
STATUSES = ('pending', 'running', 'retrying', 'complete', 'failed', 'skipped', 'cached')
DONE = ('complete', 'cached', 'skipped')

def die(code, msg):
    print(msg, file=sys.stderr); raise SystemExit(code)


def now(): return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
def epoch(): return time.time()

def load(run_id):
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
    p = ROOT / rid / 'state.json'
    data = {
        'schema_version': 3, 'id': rid, 'task': a.task, 'host': a.host, 'max_retries': a.max_retries,
        'status': 'planning', 'created_at': now(), 'updated_at': now(),
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
    """Run a plan check command. shell=True is used ONLY here: the command is a user or plan command
    (it may contain pipes or &&), never built from untrusted input by this script."""
    started = epoch()
    try:
        r = subprocess.run(command, shell=True, text=True, encoding='utf-8', errors='replace',
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
        code, out = r.returncode, r.stdout
    except subprocess.TimeoutExpired as e:
        out = e.stdout if isinstance(e.stdout, str) else (e.stdout or b'').decode('utf-8', 'replace')
        code, out = None, (out or '') + f'\n[check timed out after {timeout}s]'
    return {'check_exit': code, 'check_output': tail_lines(out), 'check_ms': round((epoch() - started) * 1000)}

def cmd_node(a):
    if a.status not in STATUSES: die(2, f"Invalid status '{a.status}'. Use one of: {', '.join(STATUSES)}.")
    if a.check_timeout < 1: die(2, '--check-timeout must be 1 or more.')
    new = {'id': a.node, 'check': a.check, 'target': a.target, 'risk': a.risk}
    bad = field_problems(new)
    if bad: die(2, f"Bad plan field: {bad[0]}.")
    _, d0 = load(a.run)
    n0 = get_node(d0, a.node)
    cmd = a.check if a.check is not None else (n0 or {}).get('check')
    result = run_check(cmd, a.check_timeout) if a.status == 'complete' and cmd else None
    with run_lock(a.run):
        apply_node(a, result)

def apply_node(a, result):
    p, d = load(a.run)
    n = get_node(d, a.node)
    created = n is None
    if created:
        n = {'id': a.node, 'role': a.role, 'status': 'pending', 'depends_on': [],
             'model': a.model, 'attempts': 0, 'files': [], 'created_at': now(),
             'started_epoch': None, 'ended_epoch': None}
    previous = None if created else n.get('status')
    deps = a.depends_on if a.depends_on is not None else n.get('depends_on', [])
    if a.status in ('running', 'retrying'):
        waiting = unmet(d, {'depends_on': deps})
        if waiting: die(3, f"Refused: {a.node} cannot start, waiting on {', '.join(waiting)}. Finish those nodes first.")
        if previous not in ('running', 'retrying'):
            limit = d.get('max_retries', 2) + 1
            if n.get('attempts', 0) >= limit:
                die(3, f"Refused: {a.node} used all {limit} attempts (max retries {limit - 1}). Mark it failed and stop, or ask the user.")
    if created: d['nodes'].append(n)
    n.update({k: v for k, v in {'role': a.role, 'status': a.status, 'model': a.model, 'check': a.check,
                                'target': a.target, 'risk': a.risk, 'goal': a.goal}.items() if v is not None})
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

def render_files(p,d):
    out=p.parent; mm=mermaid(d); (out/'graph.mmd').write_text(mm, encoding='utf-8')
    s=stats(d)
    running=not d.get('finished_at') and d.get('status') not in ('complete','complete-unverified','failed')
    refresh='<meta http-equiv="refresh" content="2">' if running else ''
    live=' · <span class="live">● live — refreshes every 2s</span>' if running else ''
    payload=json.dumps(d).replace('<','\\u003c')
    page=f'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">{refresh}<title>Graph run {html.escape(d['id'])}</title>
<style>body{{font-family:Inter,system-ui;background:#0b0d10;color:#e8eaed;margin:0}}main{{max-width:1200px;margin:auto;padding:28px}}.grid{{display:grid;grid-template-columns:2fr 1fr;gap:18px}}.card{{background:#15191f;border:1px solid #2a3038;border-radius:14px;padding:18px}}.stats{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:16px 0}}.stat{{background:#15191f;border:1px solid #2a3038;border-radius:10px;padding:12px}}.node{{cursor:pointer}}.node rect{{fill:#20262e;stroke:#59636f;stroke-width:2}}.node.complete rect,.node.cached rect{{stroke:#52b788}}.node.failed rect{{stroke:#ff6b6b}}.node.running rect,.node.retrying rect{{stroke:#ffd166}}.node.skipped rect{{stroke:#9aa0a6;stroke-dasharray:4}}text{{fill:#e8eaed;font-size:12px}}pre{{white-space:pre-wrap}}table{{width:100%;border-collapse:collapse}}td,th{{border-bottom:1px solid #2a3038;padding:8px;text-align:left}}small,.muted{{color:#9aa0a6}}.live{{color:#ffd166}}@media(max-width:800px){{.grid{{grid-template-columns:1fr}}.stats{{grid-template-columns:repeat(2,1fr)}}}}</style></head><body><main>
<h1>Graph run {html.escape(d['id'])}</h1><p>{html.escape(d['task'])}</p><p class="muted">Host: {html.escape(str(d.get('host') or 'unknown'))} · Updated: {html.escape(d.get('updated_at',''))} {live}</p>
<div class="stats">{''.join(f'<div class="stat"><small>{k.replace("_"," ").title()}</small><div><b>{v:,}</b></div></div>' for k,v in s.items())}</div>
<div class="grid"><section class="card"><h2>Execution graph</h2><svg id="graph" width="100%" viewBox="0 0 900 500"></svg></section><aside class="card"><h2>Node details</h2><pre id="details">Select a node</pre></aside></div>
<section class="card" style="margin-top:18px"><h2>Quality checks</h2><table><thead><tr><th>Command</th><th>Status</th><th>Duration</th></tr></thead><tbody>{''.join(f'<tr><td>{html.escape(q.get("command",""))}</td><td>{html.escape(q.get("status",""))}</td><td>{q.get("duration_ms",0)} ms</td></tr>' for q in d.get('quality',[])) or '<tr><td colspan="3">No checks recorded</td></tr>'}</tbody></table></section>
<script>const data={payload};const svg=document.getElementById('graph'),details=document.getElementById('details');const nodes=data.nodes||[];const levels={{}};function level(n,seen=new Set()){{if(levels[n.id]!=null)return levels[n.id];if(seen.has(n.id))return 0;seen.add(n.id);let deps=n.depends_on||[];return levels[n.id]=deps.length?1+Math.max(...deps.map(id=>{{let d=nodes.find(x=>x.id===id);return d?level(d,new Set(seen)):0}})):0}}nodes.forEach(n=>level(n));let by={{}};nodes.forEach(n=>(by[levels[n.id]]??=[]).push(n));let pos={{}};Object.keys(by).forEach(l=>by[l].forEach((n,i)=>pos[n.id]={{x:40+Number(l)*210,y:40+i*95}}));const ps=Object.values(pos);if(ps.length)svg.setAttribute('viewBox',`0 0 ${{Math.max(600,...ps.map(q=>q.x+190))}} ${{Math.max(200,...ps.map(q=>q.y+96))}}`);nodes.forEach(n=>(n.depends_on||[]).forEach(dep=>{{if(!pos[dep]||!pos[n.id])return;svg.insertAdjacentHTML('beforeend',`<line x1="${{pos[dep].x+150}}" y1="${{pos[dep].y+28}}" x2="${{pos[n.id].x}}" y2="${{pos[n.id].y+28}}" stroke="#59636f" stroke-width="2"/>`)}}));const NS='http://www.w3.org/2000/svg';nodes.forEach(n=>{{let p=pos[n.id];let g=document.createElementNS(NS,'g');g.setAttribute('class','node '+n.status);let rect=document.createElementNS(NS,'rect');rect.setAttribute('x',p.x);rect.setAttribute('y',p.y);rect.setAttribute('width',150);rect.setAttribute('height',56);rect.setAttribute('rx',9);let t1=document.createElementNS(NS,'text');t1.setAttribute('x',p.x+10);t1.setAttribute('y',p.y+22);t1.textContent=n.id;let t2=document.createElementNS(NS,'text');t2.setAttribute('x',p.x+10);t2.setAttribute('y',p.y+42);t2.textContent=n.status;g.append(rect,t1,t2);g.onclick=()=>{{details.textContent=JSON.stringify(n,null,2)}};svg.appendChild(g)}});</script></main></body></html>'''
    (out/'graph.html').write_text(page, encoding='utf-8')

def cmd_render(a):
    p,d=load(a.run); render_files(p,d); print(p.parent/'graph.html')

def detect_quality_commands():
    candidates=[]
    if Path('package.json').exists():
        try:
            scripts=json.loads(Path('package.json').read_text(encoding='utf-8')).get('scripts',{})
            for name in ('lint','typecheck','test'):
                if name in scripts: candidates.append(['npm','run',name,'--if-present'])
        except Exception: pass
    tests_dir=Path('tests')
    pats=('test_*.py','*_test.py')
    has_py_tests=any(tests_dir.rglob(g) for g in pats) if tests_dir.is_dir() else False
    has_py_tests=has_py_tests or any(any(Path('.').glob(g)) for g in pats) or (tests_dir/'conftest.py').exists()
    if has_py_tests:
        with contextlib.suppress(OSError):
            if subprocess.run([sys.executable,'-c','import pytest'],capture_output=True).returncode==0: candidates.append([sys.executable,'-m','pytest','-q'])
    if Path('Cargo.toml').exists(): candidates.append(['cargo','test','--quiet'])
    if Path('go.mod').exists(): candidates.append(['go','test','./...'])
    return candidates

def cmd_quality(a):
    load(a.run)
    commands=[shlex.split(c, posix=(os.name!='nt')) for c in a.command] if a.command else detect_quality_commands()
    records=[];failed=False
    for argv in commands:
        command=' '.join(argv); started=epoch()
        exe=shutil.which(argv[0]) if argv else None
        if not exe:
            failed=True; record={'at':now(),'command':command,'status':'failed','exit_code':None,'duration_ms':0,'stdout':'','stderr':f'command not found: {argv[0] if argv else ""}'}
        else:
            try:
                r=subprocess.run([exe]+argv[1:],text=True,capture_output=True,timeout=a.timeout,encoding='utf-8',errors='replace')
                is_pytest=argv[1:3]==['-m','pytest']
                status='passed' if r.returncode==0 else 'no-tests' if (is_pytest and r.returncode==5) else 'failed'
                failed |= status=='failed'
                record={'at':now(),'command':command,'status':status,'exit_code':r.returncode,'duration_ms':round((epoch()-started)*1000),'stdout':r.stdout[-8000:],'stderr':r.stderr[-8000:]}
            except subprocess.TimeoutExpired as e:
                failed=True; record={'at':now(),'command':command,'status':'timeout','exit_code':None,'duration_ms':round((epoch()-started)*1000),'stdout':str(e.stdout or '')[-8000:],'stderr':str(e.stderr or '')[-8000:]}
        records.append(record)
        print(f"{record['status'].upper()}: {command}")
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
        q=d.get('quality',[])
        if q:
            last=[x for x in q if x.get('batch')==q[-1].get('batch')]
            bad=[x['command'] for x in last if x.get('status') in ('failed','timeout')]
            if bad: die(3,f"Refused: latest quality run failed ({'; '.join(bad)}). Fix it and run quality again, or use --status failed.")
        else:
            status='complete-unverified'; print('No quality checks were detected: run recorded as complete-unverified.')
    d['status']=status; d['finished_at']=now(); d['updated_at']=now(); save(p,d); render_files(p,d); print(summary_text(d))

def cmd_tree(a):
    _,d=load(a.run); print(ascii_graph(d))

def cmd_summary(a):
    _,d=load(a.run); print(summary_text(d))

def cmd_status(a):
    _,d=load(a.run); print(json.dumps({'run':d,'stats':stats(d)},indent=2))

def cmd_preflight(a):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import preflight
        needs=[n.strip() for n in (a.needs or '').split(',') if n.strip()]
        print(preflight.run_preflight(needs, a.refresh, a.with_accounts, a.json))
    except ValueError as e: die(2, str(e))
    except Exception as e: print(f'preflight scan incomplete ({type(e).__name__}). Continue without it or retry with --refresh.')

def main():
    ap=argparse.ArgumentParser();sp=ap.add_subparsers(required=True)
    x=sp.add_parser('init');x.add_argument('task');x.add_argument('--id');x.add_argument('--host');x.add_argument('--max-retries',type=int,default=2);x.set_defaults(fn=cmd_init)
    x=sp.add_parser('node');x.add_argument('run');x.add_argument('node');x.add_argument('--role',required=True);x.add_argument('--status',required=True);x.add_argument('--depends-on',nargs='*');x.add_argument('--model');x.add_argument('--files',nargs='*');x.add_argument('--check');x.add_argument('--target');x.add_argument('--risk');x.add_argument('--goal');x.add_argument('--check-timeout',type=int,default=300);x.set_defaults(fn=cmd_node,nolock=True)
    x=sp.add_parser('brief');x.add_argument('run');x.add_argument('node');x.set_defaults(fn=cmd_brief,nolock=True)
    x=sp.add_parser('verify-plan');x.add_argument('run');x.add_argument('node');x.set_defaults(fn=cmd_verify_plan,nolock=True)
    x=sp.add_parser('next');x.add_argument('run');x.set_defaults(fn=cmd_next,nolock=True)
    x=sp.add_parser('event');x.add_argument('run');x.add_argument('type');x.add_argument('message');x.set_defaults(fn=cmd_event)
    x=sp.add_parser('quality');x.add_argument('run');x.add_argument('--command',action='append');x.add_argument('--timeout',type=int,default=300);x.set_defaults(fn=cmd_quality,nolock=True)
    x=sp.add_parser('validate');x.add_argument('run');x.set_defaults(fn=cmd_validate,nolock=True)
    x=sp.add_parser('retry-plan');x.add_argument('run');x.add_argument('--include-dependents',action='store_true');x.set_defaults(fn=cmd_retry_plan,nolock=True)
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
    if run_id and not getattr(a,'nolock',False) and (ROOT/run_id).exists():
        with run_lock(run_id): a.fn(a)
    else: a.fn(a)
if __name__=='__main__':main()
