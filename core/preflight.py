#!/usr/bin/env python3
"""Graph preflight: scan the environment and pick the best tool for each job.
Records names and yes/no answers only. Never reads secrets, tokens or env values."""
from __future__ import annotations
import json, os, platform, re, shutil, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

TIMEOUT = 2
TOOL_TTL, ACCESS_TTL = 24 * 3600, 3600
CLIS = {'git': ['--version'], 'gh': ['--version'], 'vercel': ['--version'], 'supabase': ['--version'],
        'node': ['--version'], 'npx': ['--version'], 'python3': ['--version'], 'python': ['--version'],
        'py': ['--version'], 'docker': ['--version'], 'xcrun': ['--version'], 'adb': ['version'],
        'emulator': ['-version'], 'playwright': ['--version']}
ACCESS = {'gh': ['gh', 'auth', 'status'], 'vercel': ['vercel', 'whoami'],
          'supabase': ['supabase', 'projects', 'list']}
LOGIN = {'gh': 'gh auth login', 'vercel': 'vercel login', 'supabase': 'supabase login'}
BROWSERS = ('google-chrome', 'chromium', 'chromium-browser', 'chrome', 'msedge', 'microsoft-edge')
MCP_BROWSER_HINTS = ('chrome', 'browser', 'playwright', 'puppeteer')


def first_line(text):
    return next((l.strip() for l in (text or '').splitlines() if l.strip()), '')

def safe(fn, default='unknown'):
    try: return fn()
    except Exception: return default

def read_json(p):
    try:
        d = json.loads(Path(p).read_text(encoding='utf-8'))
        return d if isinstance(d, dict) else {}
    except Exception: return {}

def run(cmd, capture=True):
    """Run a list command with a 2s timeout. Return (code, stdout, stderr), or None on any failure."""
    try:
        exe = shutil.which(cmd[0])
        if not exe: return None
        out = subprocess.PIPE if capture else subprocess.DEVNULL
        p = subprocess.run([exe] + cmd[1:], stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                           timeout=TIMEOUT, text=True, encoding='utf-8', errors='replace')
        return p.returncode, p.stdout or '', p.stderr or ''
    except Exception: return None

# ---- probes (each fails soft) ----
def probe_cli(name):
    if not shutil.which(name): return {'present': False}
    r = run([name] + CLIS[name])
    ver = first_line(r[1] or r[2])[:60] if r and r[0] == 0 else ''
    return {'present': True, 'version': ver or 'unknown'}

def probe_access(name, with_accounts):
    """Exit code only. Output is discarded unless the caller asked for account names."""
    if not shutil.which(name): return {'status': 'unknown'}
    r = run(ACCESS[name], capture=with_accounts)
    if r is None: return {'status': 'unknown'}
    d = {'status': 'ok' if r[0] == 0 else 'no'}
    if with_accounts and r[0] == 0:
        line = first_line(r[1]) or first_line(r[2])
        if line: d['account'] = line[:80]
    return d

def v_headless():
    if shutil.which('playwright') or any(shutil.which(b) for b in BROWSERS): return 'yes'
    env = os.environ
    roots = [env.get(k) for k in ('ProgramFiles', 'ProgramFiles(x86)', 'LOCALAPPDATA') if env.get(k)]
    paths = ['/Applications/Google Chrome.app', '/Applications/Microsoft Edge.app']
    paths += [str(Path(r) / s) for r in roots for s in
              ('Google/Chrome/Application/chrome.exe', 'Microsoft/Edge/Application/msedge.exe')]
    return 'yes' if any(Path(p).exists() for p in paths) else 'no'

def v_ios():
    if sys.platform != 'darwin' or not shutil.which('xcrun'): return 'no'
    r = run(['xcrun', 'simctl', 'help'])
    return 'unknown' if r is None else ('yes' if r[0] == 0 else 'no')

def v_android():
    if shutil.which('emulator'):
        r = run(['emulator', '-list-avds'])
        if r is None: return 'unknown'
        if r[0] == 0 and r[1].strip(): return 'yes'
    if shutil.which('adb'):
        r = run(['adb', 'devices'])
        if r is None: return 'unknown'
        return 'yes' if any(l.endswith('device') for l in r[1].splitlines()[1:]) else 'no'
    return 'no'

def v_mirroring():
    return 'yes' if sys.platform == 'darwin' and Path('/System/Applications/iPhone Mirroring.app').exists() else 'no'

# ---- static scans (names only) ----
def detect_host(env, cwd, home):
    if env.get('CLAUDECODE'): return 'claude-code'
    if env.get('CURSOR_TRACE_ID') or env.get('CURSOR_AGENT'): return 'cursor'
    for name, prefix in (('codex', 'CODEX_'), ('opencode', 'OPENCODE')):
        if any(k.startswith(prefix) for k in env): return name
    for name, p in (('claude-code', home / '.claude'), ('cursor', cwd / '.cursor'),
                    ('codex', home / '.codex'), ('opencode', home / '.config' / 'opencode')):
        if p.is_dir(): return name + ' (guessed from config dir)'
    return 'unknown'

def mcp_servers(cwd, home):
    names = set()
    def add(d): names.update(d) if isinstance(d, dict) else None
    add(read_json(cwd / '.mcp.json').get('mcpServers'))
    claude = read_json(home / '.claude.json')
    add(claude.get('mcpServers'))
    projects = claude.get('projects') if isinstance(claude.get('projects'), dict) else {}
    for key in {str(cwd), cwd.as_posix()}:
        proj = projects.get(key)
        if isinstance(proj, dict): add(proj.get('mcpServers'))
    add(read_json(home / '.cursor' / 'mcp.json').get('mcpServers'))
    add(read_json(cwd / '.cursor' / 'mcp.json').get('mcpServers'))
    try:
        toml = (home / '.codex' / 'config.toml').read_text(encoding='utf-8')
        names.update(m.group(2) for m in re.finditer(r'^\s*\[mcp_servers\.(["\']?)([^\]."\'\s]+)\1', toml, re.M))
    except Exception: pass
    return sorted(names)

def skill_names(cwd, home):
    names = set()
    for d in (cwd / '.claude/skills', home / '.claude/skills', cwd / '.agents/skills',
              home / '.agents/skills', home / '.codex/skills'):
        try: names.update(p.name for p in d.iterdir() if p.is_dir())
        except Exception: pass
    return sorted(names)

def always_apply(p):
    """Read only the frontmatter of a Cursor rule."""
    try:
        text = p.read_text(encoding='utf-8')
        if not text.startswith('---'): return False
        front = text.split('---', 2)[1]
        return bool(re.search(r'^alwaysApply:\s*true\s*$', front, re.M))
    except Exception: return False

def context_files(cwd, home):
    found = []
    slug = re.sub(r'[^A-Za-z0-9]', '-', str(cwd))
    for label, p in (('./CLAUDE.md', cwd / 'CLAUDE.md'), ('~/.claude/CLAUDE.md', home / '.claude/CLAUDE.md'),
                     ('./AGENTS.md', cwd / 'AGENTS.md'), ('~/.codex/AGENTS.md', home / '.codex/AGENTS.md'),
                     (f'~/.claude/projects/{slug}/memory/MEMORY.md',
                      home / '.claude' / 'projects' / slug / 'memory' / 'MEMORY.md')):
        if p.is_file(): found.append({'path': label})
    try:
        for p in sorted((cwd / '.cursor' / 'rules').glob('*.mdc')):
            found.append({'path': f'.cursor/rules/{p.name}', 'alwaysApply': always_apply(p)})
    except Exception: pass
    return found

# ---- scan with cache ----
def fresh(entry, ttl):
    return isinstance(entry, dict) and 0 <= time.time() - entry.get('at', 0) < ttl

def scan(cwd, home, env, refresh=False, with_accounts=False):
    cache_file = cwd / '.graph' / 'preflight.json'
    cache = {} if refresh else read_json(cache_file)
    tools = cache.get('tools') if fresh(cache.get('tools'), TOOL_TTL) else None
    tools_at = tools.get('at') if tools else time.time()
    acc = cache.get('access')
    if not (fresh(acc, ACCESS_TTL) and (acc.get('accounts') or not with_accounts)): acc = None
    if tools is None:
        tools = {'os': {'darwin': 'mac', 'win32': 'windows'}.get(sys.platform, 'linux'),
                 'arch': safe(platform.machine) or 'unknown',
                 'shell': safe(lambda: Path(env.get('SHELL') or env.get('COMSPEC') or '').stem or 'unknown'),
                 'host': safe(lambda: detect_host(env, cwd, home)),
                 'mcp': safe(lambda: mcp_servers(cwd, home), []),
                 'skills': safe(lambda: skill_names(cwd, home), []),
                 'context': safe(lambda: context_files(cwd, home), [])}
        need_tools = True
    else: need_tools = False
    futures = {}
    with ThreadPoolExecutor(max_workers=16) as pool:
        if need_tools:
            for n in CLIS:
                futures[('clis', n)] = pool.submit(safe, lambda n=n: probe_cli(n), {'present': None, 'version': 'unknown'})
            for k, fn in (('headless_browser', v_headless), ('ios_simulator', v_ios),
                          ('android_emulator', v_android), ('iphone_mirroring', v_mirroring)):
                futures[('verify', k)] = pool.submit(safe, fn)
        if acc is None:
            for n in ACCESS:
                futures[('access', n)] = pool.submit(safe, lambda n=n: probe_access(n, with_accounts), {'status': 'unknown'})
    if need_tools:
        tools['clis'] = {n: f.result() for (g, n), f in futures.items() if g == 'clis'}
        tools['verify'] = {n: f.result() for (g, n), f in futures.items() if g == 'verify'}
    if acc is None:
        got = {n: f.result() for (g, n), f in futures.items() if g == 'access'}
        acc = {'at': time.time(), 'accounts': with_accounts,
               'status': {n: d['status'] for n, d in got.items()},
               'names': {n: d['account'] for n, d in got.items() if d.get('account')}}
    if futures:
        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps({'tools': dict(tools, at=tools_at), 'access': acc},
                                             indent=2) + '\n', encoding='utf-8')
        except Exception: pass
    result = {k: v for k, v in tools.items() if k != 'at'}
    result['access'] = acc['status']
    if with_accounts: result['accounts'] = acc.get('names', {})
    return result

# ---- jobs ----
def check(c, r):
    """Return (state, why) for one candidate. state is ok, unsure or no."""
    if '|' in c:
        res = [check(x, r) for x in c.split('|')]
        for want in ('ok', 'unsure'):
            for x in res:
                if x[0] == want: return x
        return res[0]
    clis, v = r['clis'], r['verify']
    mcp = [m.lower() for m in r['mcp']]
    if c.startswith('cli:'):
        words = c[4:].split(); exe, tool = words[0], words[-1]
        if not clis.get(exe, {}).get('present'): return 'no', f'{exe} CLI not installed: install it'
        if tool in ACCESS:
            s = r['access'].get(tool, 'unknown')
            if s == 'no': return 'no', f'{tool} CLI not logged in: run {LOGIN[tool]}'
            if s != 'ok': return 'unsure', f'{tool} access unknown'
        return 'ok', ''
    if c.startswith('mcp:'):
        n = c[4:].lower()
        return ('ok', '') if any(n in m for m in mcp) else ('no', f'no {n} MCP server: add one')
    if c in ('browser', 'browser-emulation'):
        if v.get('headless_browser') == 'yes' or any(h in m for m in mcp for h in MCP_BROWSER_HINTS): return 'ok', ''
        if v.get('headless_browser') == 'unknown': return 'unsure', 'browser unknown'
        return 'no', 'no browser found: install Chrome or Edge'
    flags = {'ios-simulator': 'ios_simulator', 'android-emulator': 'android_emulator',
             'iphone-mirroring': 'iphone_mirroring'}
    if c in flags:
        s = v.get(flags[c])
        return ('ok', '') if s == 'yes' else (('unsure', f'{c} unknown') if s == 'unknown' else ('no', f'{c} not available'))
    if c == 'computer-use':
        return ('ok', '') if any('computer' in m for m in mcp) else ('no', 'no computer-use MCP server')
    return 'no', f'unknown option {c}'

def load_jobs():
    return read_json(Path(__file__).resolve().parent / 'jobs.json')

def resolve(jobs, needs, r):
    out = {}
    for need in needs:
        res = [(c, *check(c, r)) for c in jobs[need]]
        pick = next((x for s in ('ok', 'unsure') for x in res if x[1] == s), None)
        if pick:
            out[need] = {'use': pick[0], 'note': pick[2]}
        else:
            why = next((x[2] for x in res if 'not logged in' in x[2]), res[0][2] if res else 'no options')
            out[need] = {'use': None, 'workaround': f'{why}, or ask the user'}
    return out

# ---- output ----
def short(items, n=6):
    return ', '.join(items[:n]) + (f' +{len(items) - n}' if len(items) > n else '') if items else 'none'

def summary(r):
    clis = r['clis']
    have = [n for n, d in clis.items() if d.get('present')]
    miss = [n for n, d in clis.items() if d.get('present') is False]
    ctx = [c['path'] + ('*' if c.get('alwaysApply') else '') for c in r['context']]
    v = r['verify']
    lines = [f"env: {r['os']} {r['arch']} shell={r['shell']} host={r['host']}",
             f"CLIs: {' '.join(have) or 'none'} | missing: {' '.join(miss) or 'none'}",
             'access: ' + ', '.join(f'{k} {s}' for k, s in r['access'].items()),
             f"MCP: {short(r['mcp'])} | skills: {len(r['skills'])} ({short(r['skills'], 5)})",
             f"context files: {short(ctx)} (* = alwaysApply)",
             f"verify: headless-browser {v.get('headless_browser')}, ios-sim {v.get('ios_simulator')}, "
             f"android {v.get('android_emulator')}, iphone-mirroring {v.get('iphone_mirroring')}"]
    needs = [f"need {k}: " + (f"{d['use']}" + (f" ({d['note']})" if d['note'] else '') if d['use'] else d['workaround'])
             for k, d in r.get('needs', {}).items()]
    if len(needs) > 2: needs = [needs[0], ' ; '.join(needs[1:])]
    return '\n'.join(lines + needs)

def run_preflight(needs, refresh=False, with_accounts=False, as_json=False):
    """Return the text to print. Raise ValueError for an unknown need."""
    jobs = load_jobs()
    bad = [n for n in needs if n not in jobs]
    if bad: raise ValueError(f"Unknown need: {', '.join(bad)}. Known needs: {', '.join(sorted(jobs))}.")
    r = scan(Path.cwd(), Path.home(), os.environ, refresh, with_accounts)
    if needs: r['needs'] = resolve(jobs, needs, r)
    return json.dumps(r, indent=2) if as_json else summary(r)
