#!/usr/bin/env python3
"""Local runtime for Graph Skill: state, checks, cache, retries, resume, and reports."""
from __future__ import annotations
import argparse, hashlib, html, json, os, shlex, subprocess, time, uuid
from pathlib import Path

ROOT = Path('.graph/runs')
CACHE = Path('.graph/cache')


def now(): return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
def epoch(): return time.time()

def load(run_id):
    p = ROOT / run_id / 'state.json'
    if not p.exists(): raise SystemExit(f'Unknown run: {run_id}')
    return p, json.loads(p.read_text())

def save(p, data):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, indent=2) + '\n')
    os.replace(tmp, p)

def safe_id(value): return ''.join(c if c.isalnum() else '_' for c in value)

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
    rid = a.id or uuid.uuid4().hex[:10]
    p = ROOT / rid / 'state.json'
    data = {
        'schema_version': 2, 'id': rid, 'task': a.task, 'host': a.host,
        'status': 'planning', 'created_at': now(), 'updated_at': now(),
        'git': git_info(), 'nodes': [], 'events': [], 'quality': [],
        'usage': {'input_tokens': 0, 'output_tokens': 0, 'cached_tokens': 0, 'estimated': False, 'sources': []}
    }
    save(p, data); render_files(p, data); print(rid)

def cmd_node(a):
    p, d = load(a.run)
    n = get_node(d, a.node)
    created = n is None
    if created:
        n = {'id': a.node, 'role': a.role, 'status': a.status, 'depends_on': a.depends_on or [],
             'model': a.model, 'attempts': 0, 'cache_hit': False, 'files': [], 'checks': [],
             'created_at': now(), 'started_epoch': None, 'ended_epoch': None}
        d['nodes'].append(n)
    previous = None if created else n.get('status')
    n.update({k:v for k,v in {'role':a.role,'status':a.status,'model':a.model}.items() if v is not None})
    if a.depends_on is not None: n['depends_on'] = a.depends_on
    if a.files: n['files'] = sorted(set(n.get('files', []) + a.files))
    if a.cache_hit: n['cache_hit'] = True
    if a.status in ('running','retrying') and previous not in ('running','retrying'):
        n['attempts'] = n.get('attempts', 0) + 1
        n['started_epoch'] = epoch(); n['started_at'] = now()
    if a.status in ('complete','passed','failed','skipped','cached'):
        n['ended_epoch'] = epoch(); n['ended_at'] = now()
        if n.get('started_epoch'): n['duration_ms'] = round((n['ended_epoch'] - n['started_epoch']) * 1000)
    n['updated_at'] = now(); d['updated_at'] = now(); save(p, d); render_files(p, d)

def cmd_event(a):
    p,d=load(a.run); d['events'].append({'at':now(),'type':a.type,'message':a.message});d['updated_at']=now();save(p,d);render_files(p,d)

def cmd_usage(a):
    p,d=load(a.run)
    usage=d.setdefault('usage', {'input_tokens':0,'output_tokens':0,'cached_tokens':0,'estimated':False,'sources':[]})
    usage['input_tokens'] += a.input or 0; usage['output_tokens'] += a.output or 0; usage['cached_tokens'] += a.cached or 0
    usage['estimated'] = usage.get('estimated',False) or a.estimated
    usage.setdefault('sources',[]).append({'at':now(),'node':a.node,'model':a.model,'input':a.input or 0,'output':a.output or 0,'cached':a.cached or 0,'estimated':a.estimated})
    d['updated_at']=now();save(p,d);render_files(p,d)

def mermaid(d):
    lines=['flowchart LR']
    for n in d.get('nodes',[]):
        label=f"{n['id']}\\n{n.get('role','')}\\n{n.get('status','')}"
        lines.append(f'  {safe_id(n["id"])}["{label}"]')
    for n in d.get('nodes',[]):
        for dep in n.get('depends_on',[]): lines.append(f'  {safe_id(dep)} --> {safe_id(n["id"])}')
    return '\n'.join(lines)+'\n'

def stats(d):
    nodes=d.get('nodes',[]); statuses={}
    for n in nodes: statuses[n.get('status','unknown')]=statuses.get(n.get('status','unknown'),0)+1
    duration=sum(n.get('duration_ms',0) or 0 for n in nodes)
    return {'nodes':len(nodes),'completed':sum(statuses.get(x,0) for x in ('complete','passed','cached','skipped')),
            'failed':statuses.get('failed',0),'retries':sum(max(0,n.get('attempts',0)-1) for n in nodes),
            'cache_hits':sum(1 for n in nodes if n.get('cache_hit') or n.get('status')=='cached'),
            'files_changed':len({f for n in nodes for f in n.get('files',[])}),'duration_ms':duration}

def render_files(p,d):
    out=p.parent; mm=mermaid(d); (out/'graph.mmd').write_text(mm)
    s=stats(d); usage=d.get('usage',{}); total=usage.get('input_tokens',0)+usage.get('output_tokens',0)
    payload=json.dumps(d).replace('</','<\\/')
    page=f'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Graph run {html.escape(d['id'])}</title>
<style>body{{font-family:Inter,system-ui;background:#0b0d10;color:#e8eaed;margin:0}}main{{max-width:1200px;margin:auto;padding:28px}}.grid{{display:grid;grid-template-columns:2fr 1fr;gap:18px}}.card{{background:#15191f;border:1px solid #2a3038;border-radius:14px;padding:18px}}.stats{{display:grid;grid-template-columns:repeat(6,1fr);gap:10px;margin:16px 0}}.stat{{background:#15191f;border:1px solid #2a3038;border-radius:10px;padding:12px}}.node{{cursor:pointer}}.node rect{{fill:#20262e;stroke:#59636f;stroke-width:2}}.node.passed rect,.node.complete rect,.node.cached rect{{stroke:#52b788}}.node.failed rect{{stroke:#ff6b6b}}.node.running rect,.node.retrying rect{{stroke:#ffd166}}text{{fill:#e8eaed;font-size:12px}}pre{{white-space:pre-wrap}}table{{width:100%;border-collapse:collapse}}td,th{{border-bottom:1px solid #2a3038;padding:8px;text-align:left}}small,.muted{{color:#9aa0a6}}@media(max-width:800px){{.grid{{grid-template-columns:1fr}}.stats{{grid-template-columns:repeat(2,1fr)}}}}</style></head><body><main>
<h1>Graph run {html.escape(d['id'])}</h1><p>{html.escape(d['task'])}</p><p class="muted">Host: {html.escape(str(d.get('host') or 'unknown'))} · Updated: {html.escape(d.get('updated_at',''))} · Tokens recorded: {total:,}{' estimated' if usage.get('estimated') else ''}</p>
<div class="stats">{''.join(f'<div class="stat"><small>{k.replace("_"," ").title()}</small><div><b>{v:,}</b></div></div>' for k,v in s.items())}</div>
<div class="grid"><section class="card"><h2>Execution graph</h2><svg id="graph" width="100%" viewBox="0 0 900 500"></svg></section><aside class="card"><h2>Node details</h2><pre id="details">Select a node</pre></aside></div>
<section class="card" style="margin-top:18px"><h2>Quality checks</h2><table><thead><tr><th>Command</th><th>Status</th><th>Duration</th></tr></thead><tbody>{''.join(f'<tr><td>{html.escape(q.get("command",""))}</td><td>{html.escape(q.get("status",""))}</td><td>{q.get("duration_ms",0)} ms</td></tr>' for q in d.get('quality',[])) or '<tr><td colspan="3">No checks recorded</td></tr>'}</tbody></table></section>
<script>const data={payload};const svg=document.getElementById('graph'),details=document.getElementById('details');const nodes=data.nodes||[];const levels={{}};function level(n,seen=new Set()){{if(levels[n.id]!=null)return levels[n.id];if(seen.has(n.id))return 0;seen.add(n.id);let deps=n.depends_on||[];return levels[n.id]=deps.length?1+Math.max(...deps.map(id=>{{let d=nodes.find(x=>x.id===id);return d?level(d,new Set(seen)):0}})):0}}nodes.forEach(n=>level(n));let by={{}};nodes.forEach(n=>(by[levels[n.id]]??=[]).push(n));let pos={{}};Object.keys(by).forEach(l=>by[l].forEach((n,i)=>pos[n.id]={{x:40+Number(l)*210,y:40+i*95}}));nodes.forEach(n=>(n.depends_on||[]).forEach(dep=>{{if(!pos[dep]||!pos[n.id])return;svg.insertAdjacentHTML('beforeend',`<line x1="${{pos[dep].x+150}}" y1="${{pos[dep].y+28}}" x2="${{pos[n.id].x}}" y2="${{pos[n.id].y+28}}" stroke="#59636f" stroke-width="2"/>`)}}));nodes.forEach(n=>{{let p=pos[n.id];let g=document.createElementNS('http://www.w3.org/2000/svg','g');g.setAttribute('class','node '+n.status);g.innerHTML=`<rect x="${{p.x}}" y="${{p.y}}" width="150" height="56" rx="9"/><text x="${{p.x+10}}" y="${{p.y+22}}">${{n.id}}</text><text x="${{p.x+10}}" y="${{p.y+42}}">${{n.status}}${{n.cache_hit?' · cache':''}}</text>`;g.onclick=()=>details.textContent=JSON.stringify(n,null,2);svg.appendChild(g)}});</script></main></body></html>'''
    (out/'graph.html').write_text(page)

def cmd_render(a):
    p,d=load(a.run); render_files(p,d); print(p.parent/'graph.html')

def detect_quality_commands():
    candidates=[]
    if Path('package.json').exists():
        try:
            pkg=json.loads(Path('package.json').read_text()); scripts=pkg.get('scripts',{})
            for name in ('lint','typecheck','test'):
                if name in scripts: candidates.append(f'npm run {name} --if-present')
        except Exception: pass
    if Path('pyproject.toml').exists() or Path('pytest.ini').exists() or Path('tests').exists(): candidates.append('python3 -m pytest -q')
    if Path('Cargo.toml').exists(): candidates.append('cargo test --quiet')
    if Path('go.mod').exists(): candidates.append('go test ./...')
    return candidates

def cmd_quality(a):
    p,d=load(a.run); commands=a.command or detect_quality_commands()
    if not commands:
        d['events'].append({'at':now(),'type':'quality','message':'No local quality command detected'});save(p,d);render_files(p,d);print('No local quality command detected');return
    failed=False
    for command in commands:
        started=epoch()
        try:
            r=subprocess.run(command,shell=True,text=True,capture_output=True,timeout=a.timeout)
            status='passed' if r.returncode==0 else 'failed'; failed |= r.returncode!=0
            record={'at':now(),'command':command,'status':status,'exit_code':r.returncode,'duration_ms':round((epoch()-started)*1000),'stdout':r.stdout[-8000:],'stderr':r.stderr[-8000:]}
        except subprocess.TimeoutExpired as e:
            failed=True; record={'at':now(),'command':command,'status':'timeout','exit_code':None,'duration_ms':round((epoch()-started)*1000),'stdout':str(e.stdout or '')[-8000:],'stderr':str(e.stderr or '')[-8000:]}
        d.setdefault('quality',[]).append(record)
        print(f"{record['status'].upper()}: {command}")
    d['updated_at']=now();save(p,d);render_files(p,d)
    if failed: raise SystemExit(1)

def file_hash(paths):
    h=hashlib.sha256()
    for raw in sorted(set(paths)):
        p=Path(raw); h.update(raw.encode())
        if p.is_file(): h.update(p.read_bytes())
        elif p.is_dir():
            for child in sorted(x for x in p.rglob('*') if x.is_file() and '.git' not in x.parts and '.graph' not in x.parts):
                h.update(str(child).encode()); h.update(child.read_bytes())
    return h.hexdigest()

def cache_key(task,node,files,version='1'):
    payload=json.dumps({'task':task,'node':node,'files_hash':file_hash(files),'version':version},sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()

def cmd_cache_key(a):
    _,d=load(a.run); print(cache_key(d['task'],a.node,a.files or [],a.version))

def cmd_cache_get(a):
    p,d=load(a.run); key=cache_key(d['task'],a.node,a.files or [],a.version); cp=CACHE/f'{key}.json'
    if not cp.exists(): print('MISS'); raise SystemExit(2)
    item=json.loads(cp.read_text()); n=get_node(d,a.node)
    if n: n.update({'status':'cached','cache_hit':True,'cache_key':key,'updated_at':now()})
    d['events'].append({'at':now(),'type':'cache_hit','message':a.node});save(p,d);render_files(p,d);print(json.dumps(item,indent=2))

def cmd_cache_put(a):
    p,d=load(a.run); key=cache_key(d['task'],a.node,a.files or [],a.version); CACHE.mkdir(parents=True,exist_ok=True)
    value={'key':key,'run':a.run,'node':a.node,'created_at':now(),'files':a.files or [],'result':a.result}
    (CACHE/f'{key}.json').write_text(json.dumps(value,indent=2)+'\n'); n=get_node(d,a.node)
    if n: n['cache_key']=key
    save(p,d);render_files(p,d);print(key)

def cmd_retry_plan(a):
    _,d=load(a.run); failed=[n['id'] for n in d.get('nodes',[]) if n.get('status')=='failed']
    selected=descendants(d,failed) if a.include_dependents else set(failed)
    ordered=[n['id'] for n in d.get('nodes',[]) if n['id'] in selected]
    print(json.dumps({'failed':failed,'retry':ordered},indent=2))

def cmd_resume(a):
    candidates=[]
    for p in ROOT.glob('*/state.json'):
        try:
            d=json.loads(p.read_text())
            if d.get('status') not in ('complete','passed'): candidates.append((p.stat().st_mtime,d))
        except Exception: pass
    if not candidates: raise SystemExit('No incomplete run found')
    _,d=max(candidates,key=lambda x:x[0]); print(json.dumps({'run':d['id'],'task':d['task'],'status':d['status'],'graph':str(ROOT/d['id']/'graph.html')},indent=2))

def git_info():
    def run(*args):
        r=subprocess.run(['git',*args],text=True,capture_output=True); return r.stdout.strip() if r.returncode==0 else None
    return {'commit':run('rev-parse','HEAD'),'branch':run('branch','--show-current'),'dirty':bool(run('status','--porcelain'))}

def cmd_commit(a):
    p,d=load(a.run)
    if not a.yes: raise SystemExit('Refusing to commit without --yes')
    subprocess.run(['git','add','-A'],check=True)
    message=a.message or f"graph: {d['task'][:60]}"
    subprocess.run(['git','commit','-m',message],check=True); d['events'].append({'at':now(),'type':'commit','message':message});save(p,d);render_files(p,d)

def cmd_finish(a):
    p,d=load(a.run); d['status']=a.status; d['updated_at']=now(); save(p,d); render_files(p,d); print(p.parent/'graph.html')

def cmd_status(a):
    _,d=load(a.run); print(json.dumps({'run':d,'stats':stats(d)},indent=2))

def main():
    ap=argparse.ArgumentParser();sp=ap.add_subparsers(required=True)
    x=sp.add_parser('init');x.add_argument('task');x.add_argument('--id');x.add_argument('--host');x.set_defaults(fn=cmd_init)
    x=sp.add_parser('node');x.add_argument('run');x.add_argument('node');x.add_argument('--role',required=True);x.add_argument('--status',required=True);x.add_argument('--depends-on',nargs='*');x.add_argument('--model');x.add_argument('--files',nargs='*');x.add_argument('--cache-hit',action='store_true');x.set_defaults(fn=cmd_node)
    x=sp.add_parser('event');x.add_argument('run');x.add_argument('type');x.add_argument('message');x.set_defaults(fn=cmd_event)
    x=sp.add_parser('usage');x.add_argument('run');x.add_argument('--node');x.add_argument('--model');x.add_argument('--input',type=int,default=0);x.add_argument('--output',type=int,default=0);x.add_argument('--cached',type=int,default=0);x.add_argument('--estimated',action='store_true');x.set_defaults(fn=cmd_usage)
    x=sp.add_parser('quality');x.add_argument('run');x.add_argument('--command',action='append');x.add_argument('--timeout',type=int,default=300);x.set_defaults(fn=cmd_quality)
    x=sp.add_parser('cache-key');x.add_argument('run');x.add_argument('node');x.add_argument('--files',nargs='*');x.add_argument('--version',default='1');x.set_defaults(fn=cmd_cache_key)
    x=sp.add_parser('cache-get');x.add_argument('run');x.add_argument('node');x.add_argument('--files',nargs='*');x.add_argument('--version',default='1');x.set_defaults(fn=cmd_cache_get)
    x=sp.add_parser('cache-put');x.add_argument('run');x.add_argument('node');x.add_argument('result');x.add_argument('--files',nargs='*');x.add_argument('--version',default='1');x.set_defaults(fn=cmd_cache_put)
    x=sp.add_parser('retry-plan');x.add_argument('run');x.add_argument('--include-dependents',action='store_true');x.set_defaults(fn=cmd_retry_plan)
    x=sp.add_parser('resume');x.set_defaults(fn=cmd_resume)
    x=sp.add_parser('commit');x.add_argument('run');x.add_argument('--message');x.add_argument('--yes',action='store_true');x.set_defaults(fn=cmd_commit)
    x=sp.add_parser('render');x.add_argument('run');x.set_defaults(fn=cmd_render)
    x=sp.add_parser('finish');x.add_argument('run');x.add_argument('--status',default='complete');x.set_defaults(fn=cmd_finish)
    x=sp.add_parser('status');x.add_argument('run');x.set_defaults(fn=cmd_status)
    a=ap.parse_args();a.fn(a)
if __name__=='__main__':main()
