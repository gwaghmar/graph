#!/usr/bin/env python3
"""Graph Skill runtime: a plan gate and run recorder. Rules are enforced here, not in prompts."""
from __future__ import annotations
import argparse, contextlib, hashlib, html, io, json, os, re, shlex, shutil, subprocess, sys, time, uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from preflight import run_proc
import userctx, usage, routing, budget, outside, checkpoint

ROOT = Path('.graph/runs')
STATUSES = ('pending', 'running', 'retrying', 'complete', 'failed', 'skipped', 'cached')
DONE = ('complete', 'cached', 'skipped')
RUN_ID = re.compile(r'^[A-Za-z0-9_-]+$')

# What the current command did, collected for the run log (see write_log).
CTX = {'msg': None, 'checks': [], 'run': None, 'preflight': None, 'route': None}

SIZES = ('small', 'medium', 'large')

def die(code, msg):
    msg = tilde(msg)
    CTX['msg'] = msg
    if code == 3:
        on = checkpoint.color_on(sys.stderr)
        print(checkpoint.paint('✖ STOPPED', '1;31', on), file=sys.stderr)
    print(msg, file=sys.stderr); raise SystemExit(code)

def size_of(d): return d.get('size') if d.get('size') in SIZES else 'medium'


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

GO_AHEAD = ('go ahead', 'just do it', 'no need to ask', 'approve', 'proceed', 'ship it', "don't ask", 'do not ask', 'without asking', 'without review', 'skip approval')
AUTO_MSG = "Auto-approve needs the user's own go-ahead words, not the task. Show the plan and ask."

def _norm_words(text): return re.findall(r'[a-z0-9]+', (text or '').lower())

def auto_approve_problem(words_text, task):
    """Why --auto-approve text is not the user's own go-ahead, or None when it looks fine."""
    w, t = _norm_words(words_text), _norm_words(task)
    if not w: return AUTO_MSG
    ws, ts = ' '.join(w), ' '.join(t)
    if ws == ts or ws in ts: return AUTO_MSG
    if len(set(w) & set(t)) / len(set(w)) > 0.6: return AUTO_MSG
    if not any(' '.join(_norm_words(p)) in ws for p in GO_AHEAD): return AUTO_MSG + ' No explicit go-ahead phrase found.'
    return None

def cmd_init(a):
    if a.max_retries < 0: die(2, '--max-retries must be 0 or more.')
    if a.skip_preflight is not None and not a.skip_preflight.strip(): die(2, '--skip-preflight needs a reason, for example --skip-preflight "user declined".')
    if a.skip_budget is not None and not a.skip_budget.strip(): die(2, '--skip-budget needs a reason, for example --skip-budget "user declined".')
    for flag, val in (('--skip-context', a.skip_context), ('--auto-approve', a.auto_approve)):
        if val is not None and not val.strip():
            die(2, f'{flag} needs text. For --skip-context give a reason. For --auto-approve give the exact words the user said that allow going ahead without review.')
    rid = a.id or uuid.uuid4().hex[:10]
    check_run_id(rid); CTX['run'] = rid
    import preflight
    detected = preflight.safe(lambda: preflight.detect_host(os.environ, Path.cwd(), Path.home()), 'unknown')
    host = a.host or detected
    refuse_auto = auto_approve_problem(a.auto_approve, a.task) if a.auto_approve else None
    if (ROOT / rid).exists(): die(3, f'Refused: run {rid} already exists. Pick a new --id, or continue it with "resume".')
    ensure_ignore()
    p = ROOT / rid / 'state.json'
    data = {
        'schema_version': 5, 'id': rid, 'run_date': userctx.today_utc(), 'task': a.task, 'host': host, 'host_detected': detected, 'max_retries': a.max_retries,
        'quality_commands': a.quality_command or [], 'status': 'planning', 'created_at': now(), 'updated_at': now(),
        'git': git_info(), 'nodes': [], 'events': [], 'quality': [], 'size': a.size
    }
    if a.skip_preflight: data['skip_preflight'] = a.skip_preflight.strip()
    if a.skip_context: data['skip_context'] = a.skip_context.strip()
    if a.skip_budget: data['skip_budget'] = a.skip_budget.strip()
    if a.auto_approve and not refuse_auto: data['auto_approve'] = a.auto_approve.strip()
    save(p, data); render_files(p, data); print(rid)
    if a.host and detected not in ('unknown',) and a.host.lower() != detected.split(' ')[0].lower():
        print(f"Note: --host {a.host} differs from the detected host {detected}. Both are recorded.", file=sys.stderr)
    if refuse_auto:
        print(f"Run {rid} was created without auto-approve.", file=sys.stderr)
        die(3, 'Refused: ' + refuse_auto)

def unmet(d, n):
    return [dep for dep in n.get('depends_on', []) if (get_node(d, dep) or {}).get('status') not in DONE]

NO_CHECK_ROLES = ('research', 'docs', 'review', 'design')
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
    if n.get('needs') is not None and n['needs'] not in routing.TIERS:
        out.append(f"{n['id']}: needs must be one of {', '.join(reversed(routing.TIERS))}")
    if n.get('effort') is not None and n['effort'] not in routing.EFFORTS:
        out.append(f"{n['id']}: effort must be one of {', '.join(routing.EFFORTS)}")
    return out

def tail_lines(text, n=40):
    return '\n'.join((text or '').splitlines()[-n:])

def run_check(command, timeout):
    """Run a plan check command. shell=True is used ONLY for plan and quality commands: they may contain
    pipes or &&, and are never built from untrusted input by this script."""
    started = epoch()
    code, out, _, timed_out = run_proc(command, timeout, shell=True, merge=True)
    CTX['checks'].append({'command': command, 'exit_code': code, 'duration_ms': round((epoch() - started) * 1000), 'timed_out': bool(timed_out)})
    if timed_out: code, out = None, out + f'\n[check timed out after {timeout}s]'
    return {'check_exit': code, 'check_output': tail_lines(out), 'check_ms': round((epoch() - started) * 1000)}

def launcher():
    """The command that starts python on this machine, as SKILL.md says: python3, or py -3 / python on Windows."""
    if os.name == 'nt': return 'py -3' if shutil.which('py') else 'python'
    return 'python3' if shutil.which('python3') else shlex.quote(Path(sys.executable or 'python3').as_posix())

def script_ref():
    """Path of this script: relative to the project root when installed in the project, else absolute."""
    me = Path(__file__).resolve()
    with contextlib.suppress(ValueError, OSError):
        rel = me.relative_to(Path.cwd().resolve()).as_posix()
        if rel.startswith(('.claude/skills/graph/', '.agents/skills/graph/')): return rel
    return me.as_posix()

def quote_path(ref):
    if os.name != 'nt': return shlex.quote(ref)
    return '"' + ref + '"' if ' ' in ref else ref

def graph_cmd():
    return launcher() + ' ' + quote_path(script_ref())

def preflight_done(d):
    return bool(d.get('skip_preflight')) or any(e.get('cmd') == 'preflight' and e.get('outcome') == 'ok' for e in read_log(d['id']))

def plan_hash(d):
    """Fingerprint of the plan: node ids, dependencies, goals and checks."""
    rows = sorted((n['id'], sorted(n.get('depends_on', [])), (n.get('goal') or '').strip(), n.get('check') or '') + ((n['needs'],) if n.get('needs') else ()) for n in d.get('nodes', []))
    return hashlib.sha256(json.dumps(rows, ensure_ascii=True).encode('utf-8')).hexdigest()[:16]

def budget_missing(d):
    return d.get('schema_version', 3) >= 5 and size_of(d) != 'small' and not d.get('skip_budget') and not d.get('budget')

def start_gaps(d):
    """Steps still missing before any node may start, as (key, text). Runs made before schema 4 only need preflight."""
    c, rid, gaps = graph_cmd(), d['id'], []
    if not preflight_done(d):
        gaps.append(('preflight', f"preflight: run {c} preflight --run {rid} --needs <comma list>  "
                                  f"(or create the run with init --skip-preflight \"<reason>\" if the user declined it)"))
    if d.get('schema_version', 3) < 4: return gaps
    small = size_of(d) == 'small'
    if not d.get('skip_context') and not small:
        if not d.get('profile'):
            gaps.append(('profile', f"profile: read the user with {c} context --run {rid}, then run {c} profile --run {rid} --text \"<how this user works, 10 lines max>\"  "
                                    f"(or create the run with init --skip-context \"<reason>\" if this host has no user files)"))
        if not d.get('toolkit'):
            gaps.append(('choose', f"choose: run {c} tools --run {rid} --task \"<task>\", then {c} choose --run {rid} --skills a,b --clis gh --why \"<one line>\""))
    if budget_missing(d):
        gaps.append(('budget', f"budget: run {c} budget --run {rid}. If it prints unknown, ask the user how much usage is left and when it resets, then add --left-pct N --resets HH:MM  "
                               f"(or create the run with init --skip-budget \"<reason>\" if the user declined it)"))
    ap = d.get('approval')
    if ap and not d.get('auto_approve') and ap.get('plan') and ap['plan'] != plan_hash(d):
        gaps.append(('approve', f"approve: the plan changed after approval. Re-show the plan to the user, and only after they say go run {graph_cmd()} approve --run {d['id']} --note \"<the user's words>\""))
    elif not (ap or d.get('auto_approve')):
        gaps.append(('approve', f"approve: show the plan to the user, and only after they say go run {c} approve --run {rid} --note \"<the user's words>\"  "
                                f"(or create the run with init --auto-approve \"<the user's words>\" if they already said to go ahead without review)"))
    gaps += [('outside', g) for g in outside_gaps(d)]
    if size_of(d) == 'large' and not d.get('checkpoint_built'):
        gaps.append(('checkpoint', f"checkpoint: this is a large job. Run {c} checkpoint --run {rid} --serve (or --terminal) so the user can keep or drop items"))
    return gaps

def tilde(text):
    """Show the home folder as ~ so no OS user name reaches the screen or the log."""
    for h in {str(Path.home()), Path.home().as_posix()}:
        if h and h not in ('/', '.'): text = text.replace(h, '~')
    return text

def gap_message(d, gaps, what):
    return tilde(f"Refused: cannot {what} for {d['id']}. Missing steps:\n👉 Do this:\n" + '\n'.join(f'  {i}. {t}' for i, (_, t) in enumerate(gaps, 1)) + '\nThen try again.')

# ---- file auto-record: git snapshot at start, diff at end ----
def _git(args, top=None):
    try: r = subprocess.run(['git', *args], text=True, capture_output=True, encoding='utf-8', errors='replace', cwd=top, timeout=20)
    except (OSError, subprocess.SubprocessError): return None
    return r.stdout if r.returncode == 0 else None

def _dirty(top):
    """{path: mtime_ns or None} for every changed or untracked file, paths relative to the repo root."""
    out = _git(['status', '--porcelain', '-z', '-uall'], top)
    if out is None: return None
    items, res, i = out.split('\0'), {}, 0
    while i < len(items):
        e = items[i]; i += 1
        if len(e) < 4: continue
        if e[0] in 'RC': i += 1
        path = e[3:]
        if path.startswith('.graph/'): continue
        try: res[path] = (Path(top) / path).stat().st_mtime_ns
        except OSError: res[path] = None
    return res

def take_snapshot():
    top = (_git(['rev-parse', '--show-toplevel']) or '').strip()
    if not top: return None
    dirty = _dirty(top)
    if dirty is None: return None
    return {'top': top, 'head': (_git(['rev-parse', 'HEAD'], top) or '').strip() or None, 'dirty': dirty}

def changed_since(snap):
    top = snap['top']; now_dirty = _dirty(top)
    if now_dirty is None: return []
    out = {p for p, m in now_dirty.items() if p not in snap['dirty'] or snap['dirty'][p] != m}
    head = (_git(['rev-parse', 'HEAD'], top) or '').strip() or None
    if snap.get('head') and head and head != snap['head']:
        out |= {p for p in (_git(['diff', '--name-only', snap['head'], head], top) or '').splitlines() if p and not p.startswith('.graph/')}
    return sorted(out)

FILES_NOTE = 'Files were recorded from git changes between start and end of this node. Parallel nodes may share attributions.'

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
    if a.status in ('running', 'retrying'):
        gaps = start_gaps(d)
        if gaps: die(3, gap_message(d, gaps, 'start a node'))
        wait = deferral(d, a.node, a.depends_on if a.depends_on is not None else (n or {}).get('depends_on', []))
        if wait: die(3, f"Refused: {a.node} is on level {wait['level']}, which is deferred until {budget.local_time(wait['not_before'])} (local time, {wait['iso']}) because the usage budget does not cover it now. Do not start it before then. Run resume after that time.")
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
    for fld in ('needs', 'effort'):
        if n is not None and getattr(a, fld) is not None and getattr(a, fld) != n.get(fld) and not planning_call:
            die(3, f"Refused: --{fld} of {a.node} is locked once planning ends. Set it with --status pending before the first node runs.")
    if n is not None and a.no_check is not None and a.no_check.strip() != (n.get('no_check') or '') and not planning_call:
        die(3, f"Refused: --no-check of {a.node} is locked once planning ends.")
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
    new = {'id': a.node, 'check': a.check, 'target': a.target, 'risk': a.risk, 'needs': a.needs, 'effort': a.effort}
    if a.no_check is not None and not a.no_check.strip(): die(2, '--no-check needs a reason.')
    bad = field_problems(new)
    if bad: die(2, f"Bad plan field: {bad[0]}.")
    _, d0 = load(a.run)
    n0 = get_node(d0, a.node)
    risk, needs = a.risk or (n0 or {}).get('risk'), a.needs or (n0 or {}).get('needs')
    if risk == 'high' and needs:
        floor = routing.ROLE_TIER.get(a.role.lower(), routing.DEFAULT_TIER)
        if routing.TIERS.index(needs) < routing.TIERS.index(floor):
            die(3, f"Refused: {a.node} is high risk, so it cannot route below {floor} (its role {a.role!r}). Use --needs {floor} or deep, or lower the risk with a reason.")
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
    n.update({k: v for k, v in {'role': a.role, 'status': a.status, 'model': a.model, 'check': a.check, 'no_check': (a.no_check or '').strip() or None,
                                'target': a.target, 'risk': a.risk, 'needs': a.needs, 'effort': a.effort, 'goal': (a.goal or '').strip() or None, 'skip_reason': a.reason}.items() if v is not None})
    n['depends_on'] = deps
    n['tier'], n['tier_by'] = routing.tier_of(n)
    if a.files: n['files'] = sorted(set(n.get('files', []) + a.files))
    if a.status in ('running', 'retrying') and previous not in ('running', 'retrying'):
        n['attempts'] = n.get('attempts', 0) + 1
        n['started_epoch'] = epoch(); n['started_at'] = now()
        n['snapshot'] = take_snapshot()
        if d.get('status') == 'planning': d['status'] = 'running'
    refused = None
    if result:
        n.update(result)
        if result['check_exit'] != 0:
            n['status'] = 'failed'
            refused = (f"Refused: check failed for {a.node} (exit {result['check_exit']}), node set to failed. "
                       f"Run \"brief {a.run} {a.node}\" and hand it to a fresh subagent to retry.")
    if n['status'] in ('complete', 'failed') and 'snapshot' in n:
        snap = n.pop('snapshot')
        if snap:
            auto = changed_since(snap)
            t0, t1 = n.get('started_epoch') or 0, epoch()
            others = [o['id'] for o in d['nodes'] if o is not n and (o.get('status') in ('running', 'retrying') or
                      any(s[0] < t1 and s[1] > t0 for s in o.get('spans', [])))]
            own = set(n.get('files', []))
            if auto and others:
                n['shared_files'] = sorted(set(n.get('shared_files', [])) | (set(auto) - own))
                n['shared_with'] = sorted(set(n.get('shared_with', [])) | set(others))
            elif auto: n['files'] = sorted(own | set(auto))
            n['files_note'] = FILES_NOTE
    if n['status'] in ('complete', 'failed', 'skipped', 'cached'):
        n['ended_epoch'] = epoch(); n['ended_at'] = now()
        if n.get('started_epoch'): n.setdefault('spans', []).append([n['started_epoch'], n['ended_epoch']])
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
    tier, _ = routing.tier_of(n); cur, lim, last = routing.attempt_info(d, dict(n, status='failed'))
    out.append(f"Tier: {tier}" + (f", effort {n['effort']}" if n.get('effort') else ''))
    if last: out.append(f"ESCALATION: this is the last allowed attempt, so route it one tier up ({tier} -> {routing.up(tier)}). Run: {graph_cmd()} resolve {a.run} {n['id']} and pass the value to the host.")
    else: out.append(f"Route it with: {graph_cmd()} resolve {a.run} {n['id']}")
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

def deferral(d, node_id, deps):
    """{level, not_before, iso} when this node sits in a deferred level that cannot start yet, else None."""
    df = d.get('deferred')
    if not isinstance(df, dict) or epoch() >= df.get('not_before', 0): return None
    lv = node_levels(d)
    level = lv[node_id] + 1 if node_id in lv else 1 + max([lv.get(x, 0) + 1 for x in deps] or [0])
    return {'level': level, 'not_before': df['not_before'], 'iso': df.get('iso', '')} if level >= df.get('from_level', 10**9) else None

def cmd_next(a):
    _, d = load(a.run); limit = d.get('max_retries', 2) + 1
    ready = [n['id'] for n in d.get('nodes', []) if not unmet(d, n) and (
        n.get('status') == 'pending' or (n.get('status') == 'failed' and n.get('attempts', 0) < limit))]
    def show(i):
        w = deferral(d, i, (get_node(d, i) or {}).get('depends_on', []))
        return f"{i}  (waiting until {budget.local_time(w['not_before'])})" if w else i
    print('\n'.join(show(i) for i in ready) if ready else 'none')

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

def parallel_info(d):
    """(levels with 2+ nodes, whether any two nodes overlapped in time). Overlap comes from recorded start and end times only."""
    levels=node_levels(d); per={}
    for n in d.get('nodes',[]): per[levels[n['id']]]=per.get(levels[n['id']],0)+1
    wide=sum(1 for c in per.values() if c>=2)
    span=[]  # (node index, start, end): every attempt counts, so a retry does not hide an overlap
    for k,n in enumerate(d.get('nodes',[])):
        done=[tuple(x) for x in n.get('spans',[])]
        if n.get('status') in ('running','retrying') and n.get('started_epoch'): done.append((n['started_epoch'],epoch()))
        elif not done and n.get('started_epoch'): done.append((n['started_epoch'],n.get('ended_epoch') or epoch()))
        span+=[(k,a,b) for a,b in done]
    over=any(x[0]!=y[0] and x[1]<y[2] and y[1]<x[2] for i,x in enumerate(span) for y in span[i+1:])
    return wide,over

def parallel_line(d):
    wide,over=parallel_info(d)
    if not wide: return 'Parallel: no plan level had 2 or more nodes, so nothing could run in parallel.'
    word='level' if wide==1 else 'levels'
    return f"Parallel: {wide} {word} had 2+ nodes; nodes overlapped in time: {'yes' if over else 'no'}."+('' if over else ' They ran one after another.')

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
    lines.append(parallel_line(d))
    lines.append(f"Stats: retries {s['retries']} · wall {s['wall_ms']/1000:.1f}s · compute {s['duration_ms']/1000:.1f}s")
    lines.append(f"Report: {ROOT/d['id']/'graph.html'}")
    lines.append(rule)
    return '\n'.join(lines)

def short(text,n=110):
    text=' '.join(str(text or '').split())
    return text if len(text)<=n else text[:n-3]+'...'

def failure_history(d):
    """Lines for every failed check and refused action in the full log, each with how it was resolved."""
    log=read_log(d['id']) if d.get('id') else []; out=[]
    checks={}  # node -> list of exit codes, in order
    for i,e in enumerate(log):
        if e.get('cmd')=='node' and e.get('node') and e.get('checks'):
            c=e['checks'][0]; ok=c.get('exit_code')==0 and not c.get('timed_out')
            checks.setdefault(e['node'],[]).append((ok,c.get('exit_code'),c.get('timed_out')))
    done_nodes={}
    for node,runs in checks.items():
        bad=[r for r in runs if not r[0]]
        if not bad: continue
        codes=', '.join('timeout' if r[2] else f'exit {r[1]}' for r in bad)
        first_ok=next((k for k,r in enumerate(runs,1) if r[0]),None)
        n_bad=len(bad); times='once' if n_bad==1 else f'{n_bad} times'
        end=f'passed on attempt {first_ok}.' if first_ok else 'did not pass. The node is still failed.'
        out.append(f"- Node {node}: check failed {times} ({codes}), {end}")
    for i,e in enumerate(log):
        if e.get('outcome')!='refused': continue
        if e.get('cmd')=='node' and e.get('checks') and (e['checks'][0].get('exit_code')!=0 or e['checks'][0].get('timed_out')): continue
        later=[x for x in log[i+1:] if x.get('outcome')=='ok' and x.get('cmd')==e.get('cmd') and x.get('node')==e.get('node')
               and ((x.get('args') or {}).get('status')==(e.get('args') or {}).get('status'))]
        if e.get('cmd')=='finish' and not later and d.get('finished_at') and d.get('status') in ('complete','complete-unverified'): later=[e]
        who=f"Node {e['node']}" if e.get('node') else e.get('cmd','action').capitalize()
        what=f"{e.get('cmd')} refused" if e.get('cmd')!='node' else f"move to {(e.get('args') or {}).get('status','?')} refused"
        out.append(f"- {who}: {what} ({short(e.get('message'))}) "+('Resolved: the same action succeeded later.' if later else 'Not resolved in the log.'))
    byq={}
    for x in d.get('quality',[]): byq.setdefault(x['command'],[]).append(x.get('status'))
    for cmd,sts in byq.items():
        nb=sum(1 for t in sts if t in ('failed','timeout'))
        if not nb: continue
        ok=next((k for k,t in enumerate(sts,1) if t in ('passed','no-tests')),None)
        later_ok=sts[-1] in ('passed','no-tests')
        times='once' if nb==1 else f'{nb} times'
        out.append(f"- Quality check `{cmd}`: failed {times}, "+(f'passed on a later run (run {max(k for k,t in enumerate(sts,1) if t in ("passed","no-tests"))}).' if later_ok else 'latest run did not pass.'))
    return out

def run_info(d):
    c=d.get('context'); extra=''
    if d.get('skip_context'): st=f"0 (context step waived: {short(d['skip_context'],80)})"
    elif not c: st='0 (the context command was not run)'
    elif c.get('no_memory'): st='unknown (memory index not found for this folder)'
    else: st=f"{len(c.get('stale',[]))}"+(f" ({', '.join(c['stale'][:10])})" if c.get('stale') else '')
    kit=d.get('toolkit'); ap=d.get('approval')
    toolkit='- Toolkit: '+('; '.join(f"{k} {', '.join(kit[k])}" for k in ('skills','plugins','clis','mcp') if kit.get(k)) or 'no extra tools')+f". Why: {short(kit['why'],80)}" if kit else '- Toolkit: not recorded.'
    approval=f"- Approval: {short(ap['note'],120)}" if ap else f"- Approval: skipped, the user said: {short(d['auto_approve'],120)}" if d.get('auto_approve') else '- Approval: not recorded.'
    src=(ap or {}).get('source')
    how=('- Approval source: pressed GO at the checkpoint'+(f" (dropped: {short(', '.join(ap['drops']),100)})" if ap.get('drops') else '')+'.') if src=='checkpoint' else '- Approval source: recorded by the model. The runtime cannot prove a human wrote the note.' if ap else None
    return [f"- Run date: {d.get('run_date') or 'unknown'}", f"- Size: {size_of(d)}", f"- Stale context flagged: {st}", toolkit, approval]+([how] if how else [])+(
            ['- The runtime records the approval text. It cannot prove a human wrote it.'] if src!='checkpoint' else [])

ABS_PATH = re.compile(r'''(?<![\w.~$:/\\-])((?:/|[A-Za-z]:[\\/])[^\s'"`;|&<>()=,]+)''')
SYSTEM_PREFIXES = ('/dev/', '/bin/', '/usr/bin/', '/usr/sbin/', '/sbin/', '/proc/')
INSTALL_RE = re.compile(r'\b(pip3?|brew|apt(-get)?) install\b|\bnpm (install|i) (-g|--global)\b')

def outside_paths(d):
    """(node id or 'quality', path) for absolute paths in check commands that lie outside the project, and installs outside it."""
    try: root = ROOT.resolve().parent.parent.parent
    except Exception: return []
    rows = [(n['id'], n.get('check')) for n in d.get('nodes', [])] + [('quality', x.get('command')) for x in d.get('quality', [])]
    out = []
    for nid, cmd in rows:
        if not cmd: continue
        for m in ABS_PATH.findall(cmd):
            q = m.rstrip('.')
            if q in ('/',) or any((q + '/').startswith(s) for s in SYSTEM_PREFIXES): continue
            try:
                inside = Path(q).resolve() == root or root in Path(q).resolve().parents
            except Exception: inside = False
            if not inside and (nid, q) not in out: out.append((nid, q))
        if INSTALL_RE.search(cmd) and (nid, 'a tool installed during the run') not in out: out.append((nid, 'a tool installed during the run'))
    return out

def summary_top(d,status,said,todo):
    """Big one-line status, then at most 3 things to do, then a divider. The details follow."""
    if 'open problems' in said: head='⚠ DO NOT TRUST THIS'
    else: head={'complete':'✔ DONE','complete-unverified':'⚠ DONE, NOT PROVEN','failed':'✖ FAILED'}.get(status,'… NOT FINISHED')
    items=[t[2:] if t.startswith('- ') else t for t in todo if t not in ('- Nothing to do.',)]
    items=[i for i in items if not i.startswith('Nothing')] if status!='complete' or items else items
    L=[f'# {head}','',short(said.split('. ')[0].rstrip('.')+'.',100),'','## 👉 Do this:','']
    if not items: L.append('Nothing. You are done.' if status in ('complete','complete-unverified') else 'Read the details below.')
    else:
        L+=[f'{i}. {short(t,140)}' for i,t in enumerate(items[:3],1)]
        if len(items)>3: L.append(f'+{len(items)-3} more in "What you must do next" below.')
    return '\n'.join(L+['','---',''])+'\n'

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
    nref,rnodes=refusal_count(d['id']) if d.get('id') else (0,[])
    said+=(f" The rules refused {nref} action(s)"+(f" on node(s): {', '.join(rnodes)}" if rnodes else '')+'.') if nref else ' The rules refused no action.'
    L=['## Run summary (details)','',said,'','## Run info','']+run_info(d)+['','## What you asked','',d.get('task','') or 'No task recorded.','','## What changed','']
    if nodes:
        for n in nodes:
            f=n.get('files',[]); sh=n.get('shared_files',[]); w=', '.join(n.get('shared_with',[]))
            txt=(f" Files: {', '.join(f)}." if f else '')
            if sh: txt+=(f" Shared files (with {w}): {', '.join(sh)}." if f else f" Files shared with {w}: {', '.join(sh)}.")
            L.append(f"- Node {n['id']} ({n.get('role','')}) is {n.get('status')}." + (txt or ' No files recorded.'))
        grp={}
        for n in nodes:
            if n.get('shared_files'): grp.setdefault(tuple(sorted(set([n['id']]+n.get('shared_with',[])))),set()).update(n['shared_files'])
        for k,v in grp.items(): L.append(f"- shared: {', '.join(k)}: {', '.join(sorted(v))}.")
    else: L.append('- No nodes were recorded.')
    L+=['','## What was checked','']
    checks=0
    for n in nodes:
        if n.get('check_exit') is not None or n.get('check_output'):
            checks+=1; ok=n.get('check_exit')==0
            L.append(f"- Node {n['id']}: check `{n.get('check','')}` {'passed' if ok else 'failed'} (exit {n.get('check_exit')}).")
        elif n.get('status') in DONE and n.get('status')!='skipped' and not n.get('check'):
            L.append(f"- Node {n['id']}: no check ran. Nothing proves this node." + (f" Reason given: {short(n['no_check'],100)}." if n.get('no_check') else ''))
    for x in q:
        checks+=1; L.append(f"- Quality check `{x.get('command','')}`: {x.get('status','unknown')}.")
    if not checks and not any(l.startswith('- Node') for l in L[L.index('## What was checked'):]): L.append('- No check ran.')
    L+=['','## How it ran','',parallel_line(d)]
    L+=['','## Tokens and cost','']+usage.report_lines(d['usage'] if 'usage' in d else None)
    L+=['','## Usage budget','']+budget.report_lines(d)
    L+=['','## Model routing','']+routing.report_lines(d)
    L+=['','## Outside skills and repos','']+outside_lines(d)
    L+=['','## What failed','']
    out=failure_history(d)
    out+=[f"- Node {n['id']} is failed now, after {n.get('attempts',0)} attempt(s)." + (f" Check exit was {n['check_exit']}." if n.get('check_exit') not in (None,0) else '') for n in failed
          if not any(l.startswith(f"- Node {n['id']}: check failed") for l in out)]
    out+=[f"- Quality check `{x.get('command','')}` {x.get('status')} in its latest run." for x in badq]
    L+=out or ['- Nothing failed. The log shows no failed check and no refused action.']
    L+=['','## What you must do next','']
    todo=[f"- Fix node {n['id']}. Run the brief command for it and retry with a fresh agent." for n in failed]
    todo+=[f"- Fix the failing check `{x.get('command','')}`, then run quality again." for x in badq]
    todo+=[f"- Finish node {n['id']}. It is {n.get('status')}." for n in open_]
    if status=='complete-unverified': todo.append('- Test the result yourself. No check proved it.')
    todo+=[f"- {g}" for g in d.get('access_gaps',[])]
    todo+=[f"- Node {nid}: check depends on {tilde(pth)} outside the project; it may fail elsewhere." for nid,pth in outside_paths(d)]
    passed=[n['id'] for n in nodes if n.get('status')=='complete' and n.get('check_exit')==0]
    unchecked=[n['id'] for n in nodes if n.get('status') in ('complete','cached') and n.get('check_exit')!=0]
    if skipped: todo.append(f"- Skipped or cached, not run: {', '.join(skipped)}. Confirm that is fine.")
    if unchecked: todo.append(f"- Nothing is required by the runtime, but these nodes had no passing check, so nothing proves them: {', '.join(unchecked)}. Look at them yourself."+(f" Checked and passed: {', '.join(passed)}." if passed else ''))
    elif status=='complete' and nodes and not todo: todo.append('- Nothing. Every node check ran and passed.')
    L+=todo or ['- Nothing to do.']
    return summary_top(d,status,said,todo)+'\n'.join(L)+'\n'

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
text{fill:var(--fg);font-size:12px}svg{width:100%;height:auto}line{stroke:var(--mute)}pre{white-space:pre-wrap;word-break:break-word;overflow-x:auto}code{word-break:break-all}.bad{color:var(--bad)}.ev.failed{border-color:var(--bad)}.ev.complete{border-color:var(--ok)}
.act{border-collapse:collapse;width:100%;font-size:.85rem}.act td,.act th{border-bottom:1px solid var(--line);padding:4px 8px;text-align:left;vertical-align:top}tr.bad td{color:var(--bad);font-weight:600}"""

def activity_html(run_id):
    rows=read_log(run_id)
    if not rows: return '<p class="muted">No activity logged yet.</p>'
    e=html.escape; out=[]
    for r in rows:
        cls=' class="bad"' if r.get('outcome') in ('refused','invalid') else ''
        out.append(f"<tr{cls}><td>{e(str(r.get('at','')))}</td><td>{e(str(r.get('node') or '-'))}</td><td>{e(describe(r))}</td><td>{e(str(r.get('outcome','')))}</td><td>{e(str(r.get('message') or ''))}</td></tr>")
    return '<div style="overflow-x:auto"><table class="act"><tr><th>Time</th><th>Node</th><th>Action</th><th>Outcome</th><th>Why</th></tr>'+''.join(out)+'</table></div>'

def render_files(p,d):
    out=p.parent; (out/'graph.mmd').write_text(mermaid(d), encoding='utf-8')
    running=not d.get('finished_at') and d.get('status') not in ('complete','complete-unverified','failed')
    refresh='<meta http-equiv="refresh" content="2">' if running else ''
    live=' · live, refreshes every 2s' if running else ''
    payload=json.dumps({'nodes':d.get('nodes',[])}).replace('<','\\u003c')
    steps=d.get('verify_steps') or {}
    ev=''.join(node_evidence(n,steps.get(n['id'])) for n in d.get('nodes',[]))
    page=f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">{refresh}<title>Graph run {html.escape(d['id'])}</title><style>{CSS}</style></head><body><main>
<h1 style="font-size:2rem">Graph run {html.escape(d['id'])} <small>{html.escape(str(d.get('status','')))}</small></h1><p class="muted">Updated {html.escape(d.get('updated_at',''))}{live}</p>
<section class="card">{md_html(build_summary(d))}<p><b>Rule refusals: {refusal_count(d['id'])[0]}</b></p></section>
<section class="card"><h2>Diagram</h2><svg id="graph" viewBox="0 0 900 300" role="img" aria-label="Execution graph"></svg></section>
<h2>Evidence per node</h2>{ev or '<p class="muted">No nodes yet.</p>'}
<h2>Activity</h2>{activity_html(d['id'])}
<script>const nodes={payload}.nodes;const svg=document.getElementById('graph'),NS='http://www.w3.org/2000/svg',L={{}};
function lv(n,s=new Set()){{if(L[n.id]!=null)return L[n.id];if(s.has(n.id))return 0;s.add(n.id);const d=n.depends_on||[];return L[n.id]=d.length?1+Math.max(...d.map(i=>{{const x=nodes.find(y=>y.id===i);return x?lv(x,new Set(s)):0}})):0}}
nodes.forEach(n=>lv(n));const by={{}},pos={{}};nodes.forEach(n=>(by[L[n.id]]??=[]).push(n));
Object.keys(by).forEach(l=>by[l].forEach((n,i)=>pos[n.id]={{x:20+l*190,y:20+i*80}}));const ps=Object.values(pos);
if(ps.length)svg.setAttribute('viewBox',`0 0 ${{Math.max(400,...ps.map(q=>q.x+170))}} ${{Math.max(120,...ps.map(q=>q.y+76))}}`);
function el(t,a,x){{const e=document.createElementNS(NS,t);for(const k in a)e.setAttribute(k,a[k]);if(x!=null)e.textContent=x;return e}}
nodes.forEach(n=>(n.depends_on||[]).forEach(d=>{{if(pos[d]&&pos[n.id])svg.appendChild(el('line',{{x1:pos[d].x+150,y1:pos[d].y+28,x2:pos[n.id].x,y2:pos[n.id].y+28,'stroke-width':2}}))}}));
nodes.forEach(n=>{{const p=pos[n.id],g=el('g',{{class:'node '+n.status}});g.append(el('rect',{{x:p.x,y:p.y,width:150,height:56,rx:9}}),el('text',{{x:p.x+10,y:p.y+22}},n.id),el('text',{{x:p.x+10,y:p.y+42}},n.status));svg.appendChild(g)}});</script></main></body></html>"""
    tmp=out/f'graph.html.{os.getpid()}.tmp'; tmp.write_text(page, encoding='utf-8'); os.replace(tmp, out/'graph.html')

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
        CTX['checks'].append({'command':command,'exit_code':code,'duration_ms':round((epoch()-started)*1000),'timed_out':bool(timed_out)})
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
    if failed: CTX['msg']='A quality check failed or timed out.'; raise SystemExit(1)

def cmd_validate(a):
    # exit 1: bad plan structure. exit 3: a node has no goal (refused by a rule).
    _,d=load(a.run); nodes=d.get('nodes',[]); ids={n['id'] for n in nodes}; problems=[]
    for n in nodes:
        for dep in n.get('depends_on',[]):
            if dep==n['id']: problems.append(f"self-dependency: {n['id']}")
            elif dep not in ids: problems.append(f"unknown dependency: {n['id']} ← {dep}")
    for n in nodes: problems += field_problems(n)
    nogoal=[n['id'] for n in nodes if not (n.get('goal') or '').strip()]
    nocheck=[n['id'] for n in nodes if n.get('role') not in NO_CHECK_ROLES and not (n.get('check') or '').strip() and not (n.get('no_check') or '').strip()]
    if nocheck: problems.append('build node(s) without a check: '+', '.join(nocheck)+'. Add --check "<command>" or --no-check "<reason>"')
    if nogoal: problems.append('missing goal on node(s): '+', '.join(nogoal)+'. Set one with: node <run> <id> --role <role> --status pending --goal "<what this node must achieve>"')
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
        CTX['msg']='; '.join(problems)[:500]; print('\n'.join(f'✖ {x}' for x in problems)); raise SystemExit(3 if (nogoal or nocheck) else 1)
    print(f'✔ plan valid: {len(nodes)} nodes, all dependencies known, no cycles')

def js(x): return json.dumps(x, ensure_ascii=True)

def cmd_export_workflow(a):
    _, d = load(a.run); nodes = d.get('nodes', [])
    if not nodes: die(2, f'Run {a.run} has no nodes. Add the plan first, then export.')
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf): cmd_validate(argparse.Namespace(run=a.run))
    except SystemExit:
        die(3, f'Refused: plan for {a.run} is not valid. Fix it until "validate {a.run}" passes, then export.\n' + buf.getvalue().rstrip())
    gaps = [x for x in start_gaps(d) if x[0] != 'preflight']
    if gaps: die(3, gap_message(d, gaps, 'export the workflow'))
    levels = node_levels(d); by = {}
    for n in nodes: by.setdefault(levels[n['id']], []).append(n)
    df = d.get('deferred'); held = []
    if isinstance(df, dict) and epoch() < df.get('not_before', 0):
        held = [k for k in by if k + 1 >= df['from_level']]
        for k in held: del by[k]
        if not by: die(3, f"Refused: every level is deferred until {budget.local_time(df['not_before'])}. Nothing can run now.")
    g = graph_cmd()
    def mark(n, status): return f"{g} node {shlex.quote(a.run)} {shlex.quote(n['id'])} --role {shlex.quote(n.get('role') or 'worker')} --status {status}"
    def prompt(n):
        lines = [f"You are one node of a graph run. Node: {n['id']}. Goal: {n['goal']}."]
        if n.get('files'): lines.append('Files: ' + ', '.join(n['files']) + '.')
        lines += ['1. Before work, run: ' + mark(n, 'running'), '2. Do the work.']
        if n.get('check'): lines.append(f"3. Check (the runtime runs it for you on complete): {n['check']}")
        lines += [f"{'4' if n.get('check') else '3'}. When done, run: {mark(n, 'complete')}",
                  'If a command exits 3, it is a refusal. Read the line and follow it. Do not work around it.',
                  f"If you cannot finish, run: {mark(n, 'failed')}",
                  'Reply with ok true only if the complete command succeeded, else ok false with a short summary.']
        return '\n'.join(lines)
    routes = {}
    for n in nodes:
        r = routing.route(d, n, 'claude-code', Path.home())
        routes[n['id']] = r
    def opts(n):
        r = routes[n['id']]; extra = ''
        if r['value']: extra += f", model: {js(r['value'])}"
        if r['effort']: extra += f", effort: {js(r['effort'])}"
        return f"{{ label: {js(n['id'])}, schema{extra} }}"
    title = lambda i: f'Level {i + 1}'
    phases = ',\n'.join(f'    {{ title: {js(title(i))}, detail: {js(", ".join(n["id"] for n in by[k]))} }}' for i, k in enumerate(sorted(by)))
    out = [f'// Graph run {a.run}: this script belongs to that run only. For a new task, create a new run and export again.',
           '// Paths are relative to the project root: run the Workflow tool from there.',
           'export const meta = {', f'  name: {js("graph-" + a.run)},',
           f'  description: {js("Graph run " + a.run + ": " + str(d.get("task", ""))[:120])},',
           '  phases: [', phases, '  ],', '}', '',
           "const schema = { type: 'object', required: ['ok', 'summary'], properties: { ok: { type: 'boolean' }, summary: { type: 'string' } } }",
           'let stopped = false', '']
    if not preflight_done(d):
        out += [f"await agent({js('Run: ' + g + ' preflight --run ' + shlex.quote(a.run) + ' --needs <what this task needs, for example github,deploy>. Read the summary it prints. Reply ok true when it ran.')}, {{ label: 'preflight', schema }})", '']
    for i in sorted(by):
        out += [f'if (!stopped) {{', f'  phase({js(title(i))})', '  const results = await parallel([']
        out += [f"    () => agent({js(prompt(n))}, {opts(n)})," for n in by[i]]
        out += ['  ])', '  const bad = results.findIndex(r => r === null || !r.ok)', '  if (bad !== -1) {',
                f'    log("Node failed or returned nothing at {title(i)}. Stopping so dependents do not start.")',
                f"    await agent({js('Run: ' + g + ' finish ' + shlex.quote(a.run) + ' --status failed')}, {{ label: 'mark-failed' }})",
                '    stopped = true', '  }', '}', '']
    if held: out += [f"log({js('Levels ' + str(df['from_level']) + ' and higher wait until ' + budget.local_time(df['not_before']) + ' (usage budget). Run resume then, and export again.')})"]
    else: out += ['if (!stopped) {', f"  await agent({js('Run: ' + g + ' quality ' + shlex.quote(a.run) + ' then ' + g + ' finish ' + shlex.quote(a.run) + ' --status complete')}, {{ label: 'finish' }})", '}', '']
    path = Path(a.out) if a.out else Path('.claude/workflows') / f'graph-{a.run}.js'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('\n'.join(out), encoding='utf-8')
    CTX['route'] = {k: v['text'] for k, v in routes.items()}
    with run_lock(a.run):
        p, d2 = load(a.run)
        for n2 in d2['nodes']:
            if n2['id'] in routes: n2.setdefault('routes', []).append(routing.keep(routes[n2['id']])); n2['routes'] = n2['routes'][-10:]
        d2['updated_at'] = now(); save(p, d2)
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
    CTX['run']=d['id']
    out={'run':d['id'],'task':d['task'],'status':d['status'],'graph':str(ROOT/d['id']/'graph.html')}
    if d.get('deferred') and not d.get('skip_budget'):
        df=d['deferred']
        out['deferred']=(f"Levels {df['from_level']}+ wait until {budget.local_time(df['not_before'])}." if epoch()<df['not_before'] else f"The deferral time has passed. Levels {df['from_level']}+ can start.")
        with run_lock(d['id']):
            p,d=load(d['id']); d['budget']=fresh_budget(d); save(p,d)
        out['budget']=[tilde(x) for x in budget.lines(d['budget'])]
    print(json.dumps(out,indent=2))

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
    d['usage']=usage.collect(d, Path.home(), Path.cwd())
    with contextlib.suppress(Exception):
        tot=(d['usage'].get('totals') or {}) if d['usage'].get('status')=='ok' else {}
        budget.finish(d, Path.home(), epoch(), budget_host(d), budget.fresh(tot))
    with contextlib.suppress(Exception): usage.write_history(Path.home(), usage.history_entries(d, d['usage']))
    d['status']=status; d['finished_at']=now(); d['updated_at']=now()
    finish_outside(d)
    d['verify_steps']=verify_plans(d)
    d['access_gaps']=[f"Node {i} could not be fully verified: {a} is missing. Workaround: {c}." for i,st in d['verify_steps'].items() for a,b,c in st if b!='ok']
    save(p, d); render_files(p, d)
    text=build_summary(d); (p.parent/'summary.md').write_text(text, encoding='utf-8')
    print(text); print(f"Report: {p.parent/'graph.html'}")

def cmd_usage(a):
    p,d=load(a.run)
    d['usage']=usage.collect(d, Path.home(), Path.cwd(), host=a.host if a.host and a.host!='auto' else None, session=a.session, project=a.project_dir)
    d['updated_at']=now(); save(p,d); print('\n'.join(usage.report_lines(d['usage'])))

def cmd_prices(a):
    home=Path.home()
    if a.refresh:
        count,err=usage.refresh_prices(home)
        if err: die(1,f'Could not refresh prices ({err}). The old prices stay in use.')
        print(f'Prices refreshed: {count} models, saved to ~/.cache/graph-skill/prices.json.')
    table,as_of,where,age=usage.load_prices(home)
    print(f"Prices in use: {where}, as of {as_of}"+(f' ({age} days old)' if age is not None else '')+f", {len(table)} models. {usage.LABEL}")

def resolve_host(a, d):
    h = a.host if a.host and a.host != 'auto' else (d.get('host_detected') if d else None) or (d.get('host') if d else None)
    return routing.canon_host(h) if h else 'unknown'

def cmd_resolve(a):
    p, d = load(a.run)
    n = get_node(d, a.node) or die(2, f'Unknown node: {a.node}. Check the node id.')
    host = resolve_host(a, d)
    r = routing.route(d, n, host, Path.home())
    kept = routing.keep(r)
    n.setdefault('routes', []).append(kept); n['routes'] = n['routes'][-10:]
    n['tier'], n['tier_by'] = routing.tier_of(n)
    d['updated_at'] = now(); save(p, d)
    CTX['route'] = kept
    print(tilde(routing.describe(r)))

def cmd_models(a):
    home = Path.home()
    d = None
    if a.run: _, d = load(a.run)
    if a.refresh:
        count, err = usage.refresh_prices(home)
        print(f'models.dev refreshed: {count} models.' if not err else f'Could not refresh models.dev ({err}). The old data stays in use.')
    if a.host == 'all': hosts = list(routing.HOSTS)
    else:
        h = resolve_host(a, d) if a.host in (None, 'auto') else routing.canon_host(a.host)
        if a.host in (None, 'auto') and h == 'unknown':
            import preflight
            h = routing.canon_host(preflight.safe(lambda: preflight.detect_host(os.environ, Path.cwd(), home), 'unknown'))
        hosts = [h] if h != 'unknown' else list(routing.HOSTS)
    for h in hosts:
        tv = routing.tier_values(h, home, refresh=a.refresh)
        print(f'{h}:')
        for t in reversed(routing.TIERS):
            e = tv['tiers'].get(t)
            print(f"  {t:<5} -> " + (f"{e['value']}  [{e['source']}]" if e else routing.NOT_APPLIED if h not in ('cursor', 'unknown') else 'host default'))
        for x in tv['data']: print(f'  data: {x}')
        for x in tv['notes']: print(f'  note: {tilde(x)}')
    CTX['route'] = {'hosts': hosts}

def cmd_tree(a):
    _,d=load(a.run); print(ascii_graph(d))

def cmd_summary(a):
    _,d=load(a.run); print(summary_text(d))

def cmd_status(a):
    _,d=load(a.run); print(json.dumps({'run':d,'stats':stats(d)},indent=2))

def cmd_context(a):
    _, d = load(a.run)
    text, stale, total = userctx.context_report(Path.cwd(), Path.home(), project=a.project_dir)
    d['context'] = {'at': now(), 'files': total, 'stale': stale, 'no_memory': 'memory index not found' in text}
    d['updated_at'] = now(); save(ROOT / a.run / 'state.json', d); print(text)

NO_RULES = 'No user rules found [none]'

def cmd_profile(a):
    _, d = load(a.run)
    text = (a.text or '').strip()
    if not text: die(2, 'profile needs --text: a short note on how this user works.')
    n = len(text.splitlines())
    if n > 10: die(2, f'Refused: the profile has {n} lines. Keep it to 10 lines or fewer.')
    if len(text) > 1200: die(2, f'Refused: the profile has {len(text)} characters. Keep it to 1200 or fewer.')
    bad = [l for l in text.splitlines() if l.strip() and not re.search(r'\[[^\[\]]+\]\s*$', l)]
    if bad: die(2, f'Refused: every profile line must end with its source in brackets, for example [~/.claude/CLAUDE.md], [memory] or [user said]. Missing on: {bad[0][:60]!r}.')
    if (d.get('context') or {}).get('files') == 0 and text != NO_RULES:
        die(2, f'Refused: context found no user files, so the profile must be exactly "{NO_RULES}". Or create the run with init --skip-context "<reason>".')
    d['profile'] = {'at': now(), 'text': text}
    d['updated_at'] = now(); save(ROOT / a.run / 'state.json', d); print('Profile recorded.')

def cmd_tools(a):
    _, d = load(a.run)
    if not (a.task_text or '').strip(): die(2, 'tools needs --task "<text>".')
    import preflight
    r = preflight.safe(lambda: preflight.scan(Path.cwd(), Path.home(), os.environ), None)
    needs = None
    if r:
        jobs = preflight.load_jobs()
        needs = preflight.safe(lambda: preflight.resolve(jobs, sorted(jobs), r), None)
    catalog = (r or {}).get('catalog') or preflight.safe(lambda: preflight.skill_catalog(Path.cwd(), Path.home()), [])
    print(userctx.tools_report(catalog, a.task_text, needs))

def cmd_choose(a):
    _, d = load(a.run)
    if not (a.why or '').strip(): die(2, 'choose needs --why "<one line>": say why these tools fit.')
    if '\n' in a.why.strip(): die(2, '--why must be one line.')
    kit = {k: userctx.split_names(getattr(a, k)) for k in ('skills', 'plugins', 'clis', 'mcp')}
    bad = userctx.bad_names([n for v in kit.values() for n in v])
    if bad: die(2, f"Bad tool name: {bad[0]!r}. Use letters, digits and . : @ / + - _ only.")
    import preflight
    catalog = preflight.safe(lambda: preflight.skill_catalog(Path.cwd(), Path.home()), [])
    miss, off = userctx.check_toolkit(Path.cwd(), Path.home(), kit['skills'], kit['plugins'], kit['clis'], kit['mcp'], catalog)
    d['toolkit'] = dict(kit, why=a.why.strip(), at=now(), not_found=miss, turned_off=off)
    d['updated_at'] = now(); save(ROOT / a.run / 'state.json', d)
    print('Toolkit recorded: ' + '; '.join(f"{k} {', '.join(v)}" for k, v in kit.items() if v) + f". Why: {a.why.strip()}")
    for m in miss: print(f'  {m}: not found on this machine')
    for m in off: print(f'  {m}: turned off. Ask the user before turning it on.')

def budget_host(d, host=None):
    h = usage.host_key(host if host and host != 'auto' else None)
    if h != 'unknown': return h
    for src in (d.get('host_detected'), d.get('host')):
        h = usage.host_key(src)
        if h != 'unknown': return h
    return 'unknown'

def fresh_budget(d, a=None):
    """Recompute the budget record from the current plan. A before-run answer from the user is kept."""
    b = budget.build(d, {k: v + 1 for k, v in node_levels(d).items()}, Path.home(), epoch(), budget_host(d, getattr(a, 'host', None)), d.get('budget_user'))
    return b

def cmd_budget(a):
    p, d = load(a.run)
    if d.get('skip_budget'): print(f"The budget step was skipped for this run: {d['skip_budget']}"); return
    if (a.left_pct is None) != (a.resets is None): die(2, 'Give both --left-pct N and --resets HH:MM, or neither.')
    if a.left_pct is not None:
        if not 0 <= a.left_pct <= 100: die(2, '--left-pct must be between 0 and 100.')
        at = budget.parse_hhmm(a.resets, epoch())
        if at is None: die(2, f'--resets must be a local time like 14:30, not {a.resets!r}.')
        d['budget_user'] = {'left_pct': a.left_pct, 'resets_at': at, 'at_epoch': epoch(), 'at': now()}
    d['budget'] = fresh_budget(d, a)
    if a.left_pct is not None and not any(w['source'] == 'user' for w in d['budget']['before']):
        print('Note: the host source works, so the user answer was recorded but not used.')
    d['updated_at'] = now(); save(p, d)
    print('\n'.join(tilde(x) for x in budget.lines(d['budget'])))

def cmd_defer(a):
    p, d = load(a.run)
    if not d.get('nodes'): die(2, f'Run {a.run} has no nodes. Add the plan first.')
    top = max(node_levels(d).values()) + 1
    if not 1 <= a.from_level <= top: die(2, f'--from-level must be between 1 and {top} (the plan has {top} levels).')
    at = usage.epoch_of(a.not_before)
    if at is None: die(2, f'--not-before must be an ISO time like 2026-11-10T14:30 (local) or 2026-11-10T14:30Z, not {a.not_before!r}.')
    started = [n['id'] for n in d['nodes'] if n.get('status') != 'pending' and node_levels(d)[n['id']] + 1 >= a.from_level]
    if started: die(3, f"Refused: nodes {', '.join(started)} on level {a.from_level} or higher already started, so they cannot be deferred.")
    d['deferred'] = {'from_level': a.from_level, 'not_before': at, 'iso': budget.local_iso(at), 'at': now()}
    d['updated_at'] = now(); save(p, d)
    print(f"Deferred: nodes on level {a.from_level} and higher will not start before {budget.local_time(at)} (local time). Levels 1-{a.from_level - 1} can run now.")
    print(f"This is a suggestion only. Nothing is scheduled: run {graph_cmd()} resume at that time to re-read the usage.")

def approval_checks(d, run):
    """Refuse unless the plan is valid and the size gates are met. Shared by approve and the checkpoint."""
    if not d.get('nodes'): die(2, f'Refused: run {run} has no nodes. Add the plan, show it to the user, then approve.')
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf): cmd_validate(argparse.Namespace(run=run))
    except SystemExit:
        die(3, f'Refused: plan for {run} is not valid. Fix it until "validate {run}" passes, show it again, then approve.\n' + buf.getvalue().rstrip())
    if budget_missing(d): die(3, f'Refused: the budget step is missing. Run {graph_cmd()} budget --run {run} first, or create the run with init --skip-budget "<reason>".')
    gaps = outside_gaps(d)
    if gaps: die(3, f"Refused: each part needs a pick or a \"none found\" note first:\n" + '\n'.join('  - ' + g for g in gaps))
    if size_of(d) == 'large' and not d.get('checkpoint_built'):
        die(3, f"Refused: this is a large job. Show the checkpoint first: {graph_cmd()} checkpoint --run {run} --serve (or --terminal). On Claude Code you may use the host's multi-select question with the same list.")

def record_approval(d, run, note, source, drops=(), a=None):
    d['approval'] = {'at': now(), 'note': note[:500], 'plan': plan_hash(d), 'source': source}
    if drops: d['approval']['drops'] = list(drops)
    if not d.get('skip_budget') and d.get('schema_version', 3) >= 5 and size_of(d) != 'small': d['budget'] = fresh_budget(d, a)
    lines = install_staged(d, run)
    d['updated_at'] = now(); save(ROOT / run / 'state.json', d)
    return lines

def cmd_approve(a):
    _, d = load(a.run)
    note = (a.note or '').strip()
    if not note: die(2, 'approve needs --note "<what the user said>". Quote the user. Never approve for them.')
    approval_checks(d, a.run)
    for line in record_approval(d, a.run, note, 'model', (), a): print(line)
    print('Approval recorded.')
    if d.get('budget'): print('\n'.join(tilde(x) for x in budget.lines(d['budget'])))

def cmd_preflight(a):
    ensure_ignore()
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import preflight
        needs=[n.strip() for n in (a.needs or '').split(',') if n.strip() and n.strip().lower()!='none']
        info={}
        print(preflight.run_preflight(needs, a.refresh, a.with_accounts, a.json, info))
        CTX['preflight']=info
    except ValueError as e: die(2, str(e))
    except Exception as e: print(f'preflight scan incomplete ({type(e).__name__}). Continue without it or retry with --refresh.')

# ---- outside skills and repos: find, candidate, scan, pick, keep, vendor-run ----
def O(d): return d.setdefault('outside', {'parts': [], 'none': {}, 'picks': []})

def approved(d): return bool(d.get('approval') or d.get('auto_approve'))

def check_part(part):
    if not outside.PART_RE.match(part or ''): die(2, f'Bad part name: {part!r}. Use letters, digits, "_" and "-" only, 40 characters at most.')

def check_repo(repo):
    if not outside.valid_repo(repo): die(2, f'Bad repo: {repo!r}. Use owner/repo.')

def outside_gaps(d):
    """Parts with neither a pick nor a none-found note. Large: every part. Medium: only the parts marked main. Small: none."""
    size = size_of(d)
    if size == 'small': return []
    o, c = O(d), graph_cmd()
    if size == 'large' and not o['parts']: return [f'no parts listed. Run {c} parts --run {d["id"]} --set a,b (the parts of this job, for example design,ui,security)']
    need = o['parts'] if size == 'large' else [p for p in o['parts'] if p in o.get('main', [])]
    done = {p['part'] for p in o['picks']} | set(o['none'])
    return [f'{p}: run {c} find --run {d["id"]} --part {p} --query "<words>" then scan and pick, or {c} part-none --run {d["id"]} --part {p} --note "<why>"' for p in need if p not in done]

def scan_book(run):
    try: return json.loads((ROOT / run / 'scan.json').read_text(encoding='utf-8'))
    except (OSError, ValueError): return {}

def install_staged(d, run):
    lines = []
    for pk in O(d)['picks']:
        if pk['status'] != 'staged': continue
        if pk['kind'] == 'skill':
            done, err = outside.install_skill(run, pk['use'])
            if err: pk['status'] = 'failed'; lines.append(f"Could not install {pk['use']}: {err}")
            else: pk['status'] = 'ready'; pk['installed'] = done; lines.append(f"Installed skill from {pk['use']} in this project only: {', '.join(done)}")
        else: pk['status'] = 'ready'
    return lines

def finish_outside(d):
    o = O(d)
    with contextlib.suppress(Exception):
        removed, kept = outside.cleanup(d['id'], set(o.get('keep', [])))
        o['cleanup'] = {'removed': removed, 'kept': kept}

def outside_lines(d):
    o = d.get('outside') or {}
    if not (o.get('picks') or o.get('none') or o.get('parts') or (ROOT / d['id'] / 'finds').is_dir()): return ['- No outside skills or repos were looked for.']
    L = []
    for pk in o.get('picks', []): L.append(f"- Part {pk['part']}: used {pk['use']} as {pk['kind']} (scan {pk.get('verdict', '?')}, {pk['status']}).")
    for p, why in (o.get('none') or {}).items(): L.append(f'- Part {p}: none found or not needed. {short(why, 100)}')
    missing = [p for p in o.get('parts', []) if p not in (o.get('none') or {}) and not any(x['part'] == p for x in o.get('picks', []))]
    if missing: L.append(f"- No pick and no note for: {', '.join(missing)}.")
    c = o.get('cleanup')
    if c is None: L.append('- Cleanup has not run yet. Finish removes outside skills and the vendor folder.')
    else:
        L.append(f"- Removed at finish: {len(c['removed'])} item(s)." + (f" Kept: {', '.join(c['kept'])}." if c['kept'] else ' Nothing kept.'))
    return L or ['- Searches ran, nothing was picked.']

def cmd_parts(a):
    p, d = load(a.run)
    names = [x.strip() for x in (a.set or '').split(',') if x.strip()]
    if not names: die(2, 'parts needs --set a,b with at least one part name.')
    for n in names: check_part(n)
    main = [x.strip() for x in (a.main or '').split(',') if x.strip()]
    for n in main:
        check_part(n)
        if n not in names: die(2, f'--main {n} is not in --set. Main parts must be parts of the job.')
    O(d)['parts'] = list(dict.fromkeys(names)); O(d)['main'] = list(dict.fromkeys(main)); d['updated_at'] = now(); save(p, d)
    print('Parts: ' + ', '.join(O(d)['parts']) + (f". Main: {', '.join(O(d)['main'])}" if main else ''))

def cmd_part_none(a):
    p, d = load(a.run); check_part(a.part)
    if not (a.note or '').strip(): die(2, 'part-none needs --note "<why nothing fits or none is needed>".')
    O(d)['none'][a.part] = outside.clean(a.note, 300); d['updated_at'] = now(); save(p, d)
    print(f'Part {a.part}: logged as none found or not needed.')

def cmd_find(a):
    p, d = load(a.run); check_part(a.part)
    if not (a.query or '').strip(): die(2, 'find needs --query "<words>". Use generic words, nothing personal.')
    if len(a.query) > 200: die(2, '--query is too long. Use 200 characters or fewer.')
    doc = outside.do_find(ROOT / a.run, a.part, a.query.strip())
    print(tilde(outside.show_doc(doc, a.run, graph_cmd())))

def cmd_candidate(a):
    p, d = load(a.run); check_part(a.part)
    try: doc = outside.do_candidate(ROOT / a.run, a.part, a.url, a.mentions, a.note or '')
    except ValueError as e: die(2, str(e))
    print(tilde(outside.show_doc(doc, a.run, graph_cmd())))

def cmd_scan(a):
    p, d = load(a.run); check_part(a.part); check_repo(a.pick)
    doc = outside.load_doc(ROOT / a.run, a.part)
    c = next((x for x in (doc or {}).get('candidates', []) if (x.get('repo') or '').lower() == a.pick.lower()), None)
    if not c: die(3, f'Refused: {a.pick} is not in the finds for part {a.part}. Run find (or candidate) first.')
    if not c.get('passes'): die(3, f"Refused: {a.pick} is below the trust bar ({c.get('why')}). It is hidden. Scan one of the top 3.")
    res, err = outside.do_scan(c['repo'])
    if err: die(1, f'Scan failed for {a.pick}: {err}. Nothing was installed. Try another pick.')
    res['part'] = a.part
    book = scan_book(a.run); book[c['repo']] = res; save(ROOT / a.run / 'scan.json', book)
    print(tilde(outside.show_scan(res, a.run, a.part, graph_cmd())))

def cmd_pick(a):
    p, d = load(a.run); check_part(a.part); check_repo(a.use)
    doc = outside.load_doc(ROOT / a.run, a.part)
    c = next((x for x in (doc or {}).get('candidates', []) if (x.get('repo') or '').lower() == a.use.lower()), None)
    if not c or not c.get('passes'): die(3, f'Refused: {a.use} is not a shown find for part {a.part}.')
    repo = c['repo']; r = scan_book(a.run).get(repo)
    if not r: die(3, f'Refused: {repo} was not scanned. Run {graph_cmd()} scan --run {a.run} --part {a.part} --pick {repo} first.')
    if r['verdict'] == 'block': die(3, f'Refused: the scan of {repo} says BLOCK. It cannot be picked. Pick another.')
    if not outside.vendor_dir(repo).is_dir(): die(3, f'Refused: the scanned copy of {repo} is gone. Scan it again.')
    if a.kind == 'skill' and not outside.skill_dirs(outside.vendor_dir(repo)): die(3, f'Refused: {repo} has no SKILL.md, so it cannot be picked as a skill. Use --as reference or tool.')
    if r['verdict'] == 'warn' and a.kind != 'reference' and approved(d) and not (a.ok or '').strip():
        die(3, f'Refused: the scan of {repo} has warnings. Show them to the user and pass --ok "<the user\'s words>".')
    o = O(d)
    o['picks'] = [x for x in o['picks'] if not (x['part'] == a.part and x['use'] == repo)]
    pk = {'part': a.part, 'use': repo, 'kind': a.kind, 'verdict': r['verdict'], 'at': now(), 'status': 'ready' if a.kind == 'reference' else 'staged'}
    if (a.ok or '').strip(): pk['ok'] = outside.clean(a.ok, 300)
    o['picks'].append(pk)
    lines = install_staged(d, a.run) if approved(d) else []
    d['updated_at'] = now(); save(p, d)
    if pk['status'] == 'staged' and not approved(d): print(f'Picked {repo} as {a.kind} for part {a.part}. It installs when the plan is approved. Nothing was copied yet.')
    else: print(f'Picked {repo} as {a.kind} for part {a.part}.')
    for l in lines: print(tilde(l))
    if a.kind == 'reference': print(f'Read it in {outside.vendor_dir(repo).as_posix()}. It is removed at finish.')

def cmd_keep(a):
    p, d = load(a.run); check_repo(a.pick)
    o = O(d)
    if not any(x['use'].lower() == a.pick.lower() for x in o['picks']): die(3, f'Refused: {a.pick} was never picked in this run.')
    repo = next(x['use'] for x in o['picks'] if x['use'].lower() == a.pick.lower())
    o.setdefault('keep', [])
    if repo not in o['keep']: o['keep'].append(repo)
    d['updated_at'] = now(); save(p, d)
    print(f'{repo} will be kept at finish (the user asked for it).')

def cmd_vendor_run(a):
    p, d = load(a.run); check_repo(a.pick)
    argv = list(a.cmd or [])
    if argv and argv[0] == '--': argv = argv[1:]
    a.argv = ' '.join(shlex.quote(x) for x in argv)
    if not argv: die(2, 'vendor-run needs a command after "--", for example: vendor-run --run r --pick o/r -- python3 tool.py')
    if not approved(d): die(3, 'Refused: the plan is not approved yet. Tools run only after approve.')
    pk = next((x for x in O(d)['picks'] if x['use'].lower() == a.pick.lower() and x['kind'] == 'tool' and x['status'] == 'ready'), None)
    if not pk: die(3, f'Refused: {a.pick} is not a picked tool. Scan it, then pick it with --as tool.')
    vd = outside.vendor_dir(pk['use'])
    if not vd.is_dir(): die(3, f'Refused: the scanned copy of {pk["use"]} is gone. Scan it again.')
    cwd = Path.cwd() if a.where == 'project' else vd
    env = outside.scrubbed_env(outside.TMP / a.run)
    print('Running inside the project with PATH, LANG and a temporary HOME only. This is NOT a full sandbox: the tool can still use the network and any file this user can reach by full path.')
    try: code, out, err, timed = run_proc(argv, a.timeout, merge=True, env=env, cwd=str(cwd))
    except OSError as e: die(2, f'Could not start the command ({type(e).__name__}). Check the program name and PATH.')
    text = tilde(tail_lines(out + err))
    if text.strip(): print(text)
    if timed: die(3, f'The command ran longer than {a.timeout} seconds and was stopped.')
    if code != 0: die(1, f'The command exited with {code}.')

# ---- size, features, checkpoint ----
def cmd_size(a):
    p, d = load(a.run)
    old = size_of(d)
    if a.size == old: print(f'Size is already {old}.'); return
    d['size'] = a.size; d.setdefault('size_history', []).append({'at': now(), 'from': old, 'to': a.size})
    d['updated_at'] = now(); save(p, d)
    print(f'Size changed: {old} -> {a.size}. Gates were re-checked.')
    gaps = start_gaps(d)
    gaps = [g for g in gaps if g[0] != 'approve' or not approved(d)]
    if gaps:
        print('👉 Do this before any node starts:')
        for i, (_, t) in enumerate(gaps, 1): print(tilde(f'  {i}. {t}'))
    else: print('Nothing more is missing.')

def cmd_feature(a):
    p, d = load(a.run)
    name = (a.name or '').strip()
    if not name: die(2, 'feature needs --name "<a few words>".')
    fs = d.setdefault('features', [])
    fid = f'f{len(fs) + 1}'
    fs.append({'id': fid, 'name': checkpoint.clean(name, 60), 'why': checkpoint.clean(a.why or '', 80)})
    d['updated_at'] = now(); save(p, d)
    print(f'Feature {fid} added. It shows on the checkpoint.')

def log_refusal(run, msg):
    with contextlib.suppress(Exception):
        append_log(run, {'at': now(), 'cmd': 'checkpoint', 'node': None, 'args': {}, 'outcome': 'refused', 'exit_code': 3, 'message': clip(msg)})

def apply_checkpoint(run, dropped, source='checkpoint'):
    """Record the user's keep/drop answer. Under the run lock. Refuses (exit 3) with a clear message; nothing is saved then."""
    with run_lock(run):
        p, d = load(run)
        items = checkpoint.build_items(d, ROOT / run)
        valid = {i['id']: i for i in items}
        dropped = list(dict.fromkeys(dropped))
        unknown = [x for x in dropped if x not in valid]
        if unknown: die(3, f"Refused: unknown item(s) {', '.join(checkpoint.clean(x, 40) for x in unknown)}.")
        drop_nodes = {x[5:] for x in dropped if x.startswith('node:')}
        for nid in drop_nodes:
            if (get_node(d, nid) or {}).get('status') != 'pending': die(3, f'Refused: step {nid} already started. Only pending steps can be dropped.')
        for n in d.get('nodes', []):
            if n['id'] in drop_nodes: continue
            bad = [x for x in n.get('depends_on', []) if x in drop_nodes]
            if bad: die(3, f"Refused: kept step {n['id']} needs dropped step {', '.join(bad)}. Keep {', '.join(bad)}, or drop {n['id']} too.")
        d['nodes'] = [n for n in d.get('nodes', []) if n['id'] not in drop_nodes]
        o = O(d)
        for x in dropped:
            if x.startswith('pick:'):
                part = x[5:]
                o['picks'] = [k for k in o['picks'] if k['part'] != part]; o['none'][part] = 'dropped at the checkpoint'
            elif x.startswith('feat:'): d['features'] = [f for f in d.get('features', []) if 'feat:' + f['id'] != x]
        d['checkpoint_built'] = True
        approval_checks(d, run)
        names = [valid[x]['title'] for x in dropped]
        note = 'pressed GO' + (f" (dropped: {', '.join(names)})" if names else ' (kept everything)')
        lines = record_approval(d, run, note, source, dropped)
        return d, lines

def serve_checkpoint(run, timeout, open_browser):
    import http.server, secrets, urllib.parse
    token = secrets.token_urlsafe(18)
    state = {'done': False, 'ok': None}
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *x): pass
        def _send(self, code, body, ctype='application/json'):
            b = body.encode('utf-8') if isinstance(body, str) else body
            self.send_response(code); self.send_header('Content-Type', ctype + '; charset=utf-8'); self.send_header('Content-Length', str(len(b)))
            self.send_header('Cache-Control', 'no-store'); self.send_header('Referrer-Policy', 'no-referrer'); self.end_headers(); self.wfile.write(b)
        def _auth(self):
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            host = (self.headers.get('Host') or '').split(':')[0]
            ok = secrets.compare_digest((q.get('t') or [''])[0], token) and host in ('127.0.0.1', 'localhost')
            if not ok:
                log_refusal(run, 'checkpoint: wrong or missing token'); self._send(403, json.dumps({'error': 'Forbidden'}))
            return ok
        def do_GET(self):
            if not self._auth(): return
            _, d = load(run)
            self._send(200, checkpoint.render_html(checkpoint.model(d, ROOT / run), token), 'text/html')
        def do_POST(self):
            if not self._auth(): return
            if urllib.parse.urlsplit(self.path).path != '/go': return self._send(404, '{}')
            try:
                n = int(self.headers.get('Content-Length') or 0)
                if not 0 <= n <= 65536: raise ValueError
                body = json.loads(self.rfile.read(n) or b'{}'); drops = body.get('dropped', [])
                if not isinstance(drops, list) or not all(isinstance(x, str) for x in drops): raise ValueError
            except (ValueError, TypeError):
                return self._send(400, json.dumps({'error': 'Bad request.'}))
            try: apply_checkpoint(run, drops)
            except SystemExit:
                msg = CTX['msg'] or 'Refused.'; log_refusal(run, msg); return self._send(409, json.dumps({'error': msg}))
            state['done'] = True; state['ok'] = drops; self._send(200, json.dumps({'ok': True}))
    srv = http.server.HTTPServer(('127.0.0.1', 0), H); srv.timeout = 0.5
    url = f'http://127.0.0.1:{srv.server_port}/?t={token}'
    print(f'Open this page: {url}', flush=True)
    if open_browser:
        with contextlib.suppress(Exception):
            import webbrowser; webbrowser.open(url)
    end = epoch() + timeout
    try:
        while not state['done'] and epoch() < end: srv.handle_request()
    finally: srv.server_close()
    return state['ok']

def cmd_checkpoint(a):
    p, d = load(a.run)
    if not d.get('nodes'): die(2, f'Run {a.run} has no nodes. Add the plan first.')
    with run_lock(a.run):
        _, d = load(a.run); d['checkpoint_built'] = True; d['updated_at'] = now(); save(p, d)
    m = checkpoint.model(d, ROOT / a.run)
    if a.serve and a.terminal: die(2, 'Use --serve or --terminal, not both.')
    if a.terminal:
        print(tilde(checkpoint.render_terminal(m)), flush=True)
        ids = [i['id'] for i in m['items']]
        while True:
            line = sys.stdin.readline()
            if not line: die(1, 'No answer. Nothing was approved.')
            try: nums = checkpoint.parse_terminal(line, len(ids)); break
            except ValueError as e: print(str(e), flush=True)
        _, lines = apply_checkpoint(a.run, [ids[n - 1] for n in sorted(nums)])
    elif a.serve:
        drops = serve_checkpoint(a.run, a.timeout, not a.no_open)
        if drops is None: die(1, 'The checkpoint timed out. Nothing was approved.')
        lines = []
    else:
        page = ROOT / a.run / 'checkpoint.html'
        page.write_text(checkpoint.render_html(m), encoding='utf-8')
        print(tilde(checkpoint.render_terminal(m, color=False)))
        print(f'👉 Do this: show the user this list, or run {graph_cmd()} checkpoint --run {a.run} --serve  (page: {tilde(str(page))})')
        return
    for l in lines: print(tilde(l))
    print('✔ Approved. Pressed GO at the checkpoint.')

# ---- run log: .graph/runs/<id>/log.jsonl, append-only, one JSON line per command call ----
LOG_ARGS = ('project_dir', 'text', 'note', 'why', 'skills', 'plugins', 'clis', 'mcp', 'task_text', 'auto_approve', 'skip_context', 'skip_preflight', 'goal', 'no_check', 'status', 'depends_on', 'check', 'target', 'risk', 'reason', 'command', 'type', 'needs', 'refresh',
            'with_accounts', 'include_dependents', 'skip_budget', 'left_pct', 'resets', 'from_level', 'not_before', 'host', 'session', 'needs', 'effort', 'model',
            'part', 'query', 'url', 'mentions', 'pick', 'use', 'kind', 'ok', 'set', 'argv', 'where', 'size', 'main', 'name', 'serve', 'terminal', 'no_open')

def log_path(run_id): return ROOT / run_id / 'log.jsonl'

def clip(v, n=500):
    return v[:n] if isinstance(v, str) else v

def append_log(run_id, entry):
    """One line, one write call, O_APPEND: safe for parallel callers without taking the run lock."""
    data = (json.dumps(entry, ensure_ascii=False) + '\n').encode('utf-8')
    fd = os.open(log_path(run_id), os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, 'O_BINARY', 0), 0o644)
    try: os.write(fd, data)
    finally: os.close(fd)

def read_log(run_id):
    out = []
    try: text = log_path(run_id).read_text(encoding='utf-8')
    except OSError: return out
    for line in text.splitlines():
        with contextlib.suppress(ValueError):
            e = json.loads(line)
            if isinstance(e, dict): out.append(e)
    return out

def write_log(a, code):
    """Log one command call. Only when the run already exists: never create a run dir to log."""
    cmd = a.fn.__name__[4:].replace('_', '-')
    if cmd == 'log': return
    rid = getattr(a, 'run', None) or CTX['run'] or getattr(a, 'id', None)
    try:
        if not rid or not RUN_ID.match(rid) or not (ROOT / rid / 'state.json').exists(): return
        args = {k: clip(getattr(a, k), 1300 if k == 'text' else 500) for k in LOG_ARGS if getattr(a, k, None) not in (None, False, [])}
        outcome = 'ok' if code == 0 else 'invalid' if code == 2 else 'refused' if code == 3 else 'error'
        e = {'at': now(), 'cmd': cmd, 'node': getattr(a, 'node', None), 'args': args, 'outcome': outcome, 'exit_code': code}
        if code != 0: e['message'] = clip(CTX['msg'] or f'exit {code}')
        if CTX['checks']: e['checks'] = CTX['checks']
        if CTX['preflight']: e['preflight'] = CTX['preflight']
        if CTX['route']: e['routing'] = CTX['route']
        append_log(rid, e)
        if outcome != 'ok' or cmd not in ('render', 'status', 'tree', 'summary', 'next', 'brief', 'validate', 'retry-plan', 'verify-plan', 'models'):
            p = ROOT / rid / 'state.json'; render_files(p, json.loads(p.read_text(encoding='utf-8')))
    except Exception: pass

def describe(e):
    cmd, a = e.get('cmd', '?'), e.get('args') or {}
    if cmd == 'node': what = 'node -> ' + str(a.get('status', '?')) + (f" (check {a['check']})" if a.get('check') else '')
    elif cmd == 'resolve': what = 'resolve -> ' + str((e.get('routing') or {}).get('text', '?'))
    elif cmd == 'preflight': what = 'preflight' + (f" needs {a['needs']}" if a.get('needs') else '')
    else: what = cmd
    for c in e.get('checks') or []:
        what += f" [ran `{c.get('command')}` exit {c.get('exit_code')} in {c.get('duration_ms')}ms" + (' TIMED OUT' if c.get('timed_out') else '') + ']'
    return what

def refusal_count(run_id):
    log = read_log(run_id); bad = [e for e in log if e.get('outcome') == 'refused']
    return len(bad), list(dict.fromkeys(e['node'] for e in bad if e.get('node')))

def cmd_log(a):
    load(a.run)
    rows = read_log(a.run)
    if a.node: rows = [e for e in rows if e.get('node') == a.node]
    if a.refusals: rows = [e for e in rows if e.get('outcome') in ('refused', 'invalid')]
    if a.json: print(json.dumps(rows, indent=2, ensure_ascii=False)); return
    if not rows: print('No log lines.'); return
    for e in rows:
        why = f" | {e['message']}" if e.get('message') else ''
        print(f"{e.get('at','')}  {e.get('node') or '-':<12} {describe(e)}  {str(e.get('outcome','')).upper()}{why}")


def main():
    ap=argparse.ArgumentParser();sp=ap.add_subparsers(required=True)
    x=sp.add_parser('init');x.add_argument('task');x.add_argument('--id');x.add_argument('--host');x.add_argument('--max-retries',type=int,default=2);x.add_argument('--quality-command',action='append');x.add_argument('--skip-preflight',metavar='REASON');x.add_argument('--skip-context',metavar='REASON');x.add_argument('--auto-approve',metavar='WORDS');x.add_argument('--skip-budget',metavar='REASON');x.add_argument('--size',choices=list(SIZES),required=True);x.set_defaults(fn=cmd_init)
    x=sp.add_parser('node');x.add_argument('run');x.add_argument('node');x.add_argument('--role',required=True);x.add_argument('--status',required=True);x.add_argument('--depends-on',nargs='*');x.add_argument('--model');x.add_argument('--needs');x.add_argument('--effort');x.add_argument('--files',nargs='*');x.add_argument('--check');x.add_argument('--no-check',dest='no_check',metavar='REASON');x.add_argument('--target');x.add_argument('--risk');x.add_argument('--goal');x.add_argument('--reason');x.add_argument('--check-timeout',type=int,default=300);x.set_defaults(fn=cmd_node,nolock=True)
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
    x=sp.add_parser('usage');x.add_argument('--run',required=True);x.add_argument('--host',default='auto');x.add_argument('--session');x.add_argument('--project-dir',dest='project_dir');x.set_defaults(fn=cmd_usage)
    x=sp.add_parser('prices');x.add_argument('--run');x.add_argument('--refresh',action='store_true');x.set_defaults(fn=cmd_prices,nolock=True)
    x=sp.add_parser('models');x.add_argument('--refresh',action='store_true');x.add_argument('--host',default='auto');x.add_argument('--run');x.set_defaults(fn=cmd_models,nolock=True)
    x=sp.add_parser('resolve');x.add_argument('run');x.add_argument('node');x.add_argument('--host',default='auto');x.set_defaults(fn=cmd_resolve)
    x=sp.add_parser('status');x.add_argument('run');x.set_defaults(fn=cmd_status,nolock=True)
    x=sp.add_parser('log');x.add_argument('run');x.add_argument('--refusals',action='store_true');x.add_argument('--node');x.add_argument('--json',action='store_true');x.set_defaults(fn=cmd_log,nolock=True)
    x=sp.add_parser('context');x.add_argument('--run',required=True);x.add_argument('--project-dir',dest='project_dir');x.set_defaults(fn=cmd_context)
    x=sp.add_parser('profile');x.add_argument('--run',required=True);x.add_argument('--text');x.set_defaults(fn=cmd_profile)
    x=sp.add_parser('tools');x.add_argument('--run',required=True);x.add_argument('--task',dest='task_text');x.set_defaults(fn=cmd_tools,nolock=True)
    x=sp.add_parser('choose');x.add_argument('--run',required=True);x.add_argument('--skills');x.add_argument('--plugins');x.add_argument('--clis');x.add_argument('--mcp');x.add_argument('--why');x.set_defaults(fn=cmd_choose)
    x=sp.add_parser('budget');x.add_argument('--run',required=True);x.add_argument('--left-pct',type=float);x.add_argument('--resets');x.add_argument('--host',default='auto');x.set_defaults(fn=cmd_budget)
    x=sp.add_parser('defer');x.add_argument('run');x.add_argument('--from-level',type=int,required=True);x.add_argument('--not-before',required=True);x.set_defaults(fn=cmd_defer)
    x=sp.add_parser('approve');x.add_argument('--run',required=True);x.add_argument('--note');x.set_defaults(fn=cmd_approve)
    x=sp.add_parser('preflight');x.add_argument('--run');x.add_argument('--needs');x.add_argument('--refresh',action='store_true');x.add_argument('--with-accounts',action='store_true');x.add_argument('--json',action='store_true');x.set_defaults(fn=cmd_preflight,nolock=True)
    x=sp.add_parser('parts');x.add_argument('--run',required=True);x.add_argument('--set');x.add_argument('--main');x.set_defaults(fn=cmd_parts)
    x=sp.add_parser('part-none');x.add_argument('--run',required=True);x.add_argument('--part',required=True);x.add_argument('--note');x.set_defaults(fn=cmd_part_none)
    x=sp.add_parser('find');x.add_argument('--run',required=True);x.add_argument('--part',required=True);x.add_argument('--query');x.set_defaults(fn=cmd_find,nolock=True)
    x=sp.add_parser('candidate');x.add_argument('--run',required=True);x.add_argument('--part',required=True);x.add_argument('--url',required=True);x.add_argument('--mentions',type=int,default=1);x.add_argument('--note');x.set_defaults(fn=cmd_candidate,nolock=True)
    x=sp.add_parser('scan');x.add_argument('--run',required=True);x.add_argument('--part',required=True);x.add_argument('--pick',required=True);x.set_defaults(fn=cmd_scan,nolock=True)
    x=sp.add_parser('pick');x.add_argument('--run',required=True);x.add_argument('--part',required=True);x.add_argument('--use',required=True);x.add_argument('--as',dest='kind',choices=['skill','reference','tool'],default='reference');x.add_argument('--ok');x.set_defaults(fn=cmd_pick)
    x=sp.add_parser('size');x.add_argument('--run',required=True);x.add_argument('--to',dest='size',choices=list(SIZES),required=True);x.set_defaults(fn=cmd_size)
    x=sp.add_parser('feature');x.add_argument('--run',required=True);x.add_argument('--name',required=True);x.add_argument('--why');x.set_defaults(fn=cmd_feature)
    x=sp.add_parser('checkpoint');x.add_argument('--run',required=True);x.add_argument('--serve',action='store_true');x.add_argument('--terminal',action='store_true');x.add_argument('--no-open',dest='no_open',action='store_true');x.add_argument('--timeout',type=int,default=1800);x.set_defaults(fn=cmd_checkpoint,nolock=True)
    x=sp.add_parser('keep');x.add_argument('--run',required=True);x.add_argument('--pick',required=True);x.set_defaults(fn=cmd_keep)
    x=sp.add_parser('vendor-run');x.add_argument('--run',required=True);x.add_argument('--pick',required=True);x.add_argument('--in',dest='where',choices=['vendor','project'],default='vendor');x.add_argument('--timeout',type=int,default=300);x.add_argument('cmd',nargs=argparse.REMAINDER);x.set_defaults(fn=cmd_vendor_run,nolock=True)
    a=ap.parse_args()
    try: sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception: pass
    run_id=getattr(a,'run',None)
    if run_id: check_run_id(run_id)
    code=0
    try:
        if run_id and not getattr(a,'nolock',False) and (ROOT/run_id).exists():
            with run_lock(run_id): a.fn(a)
        else: a.fn(a)
    except SystemExit as e:
        code=e.code if isinstance(e.code,int) else 0 if e.code is None else 1
        write_log(a,code); raise
    except Exception:
        CTX['msg']=CTX['msg'] or 'Unexpected error in the runtime.'; write_log(a,1); raise
    write_log(a,0)
if __name__=='__main__':main()
