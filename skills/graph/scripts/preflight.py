#!/usr/bin/env python3
"""Graph preflight: scan the environment and pick the best tool for each job.
Records names and yes/no answers only. It parses MCP config files but keeps only server names,
and never stores or prints env values, tokens or other config values."""
from __future__ import annotations
import contextlib, json, os, platform, re, shutil, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import permits

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

def run_proc(cmd, timeout, shell=False, merge=False, capture=True, env=None, cwd=None):
    """Run a command and kill its whole process tree on timeout. Return (code, stdout, stderr, timed_out).
    shell=True is for plan commands only. On timeout the code is None."""
    env = permits.quiet_env(env)  # CI=1 and a no-op BROWSER: no CLI can open a sign-in page. stdin is DEVNULL below.
    kw = {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {'start_new_session': True}
    pipe = subprocess.PIPE if capture else subprocess.DEVNULL
    p = subprocess.Popen(cmd, shell=shell, stdin=subprocess.DEVNULL, stdout=pipe,
                         stderr=subprocess.STDOUT if merge and capture else pipe,
                         text=True, encoding='utf-8', errors='replace', env=env, cwd=cwd, **kw)
    try:
        out, err = p.communicate(timeout=timeout)
        return p.returncode, out or '', err or '', False
    except subprocess.TimeoutExpired:
        try:
            if os.name == 'nt':
                subprocess.run(['taskkill', '/T', '/F', '/PID', str(p.pid)], capture_output=True, timeout=10)
            else:
                os.killpg(p.pid, 9)
        except Exception: p.kill()
        try: out, err = p.communicate(timeout=5)
        except Exception: out, err = '', ''
        return None, out or '', err or '', True

def run(cmd, capture=True):
    """Run a list command with a 2s timeout. Return (code, stdout, stderr), or None on any failure."""
    try:
        exe = shutil.which(cmd[0])
        if not exe: return None
        code, out, err, timed_out = run_proc([exe] + cmd[1:], TIMEOUT, capture=capture)
        if timed_out: return None
        return code, out, err
    except Exception: return None

# ---- probes (each fails soft) ----
def probe_cli(name):
    if not shutil.which(name): return {'present': False}
    r = run([name] + CLIS[name])
    ver = first_line(r[1] or r[2])[:60] if r and r[0] == 0 else ''
    return {'present': True, 'version': ver or 'unknown'}

def parse_account(name, out, err=''):
    """Pull the account name from CLI output. supabase has no reliable account line, so it stores nothing."""
    text = (out or '') + '\n' + (err or '')
    if name == 'gh':
        m = re.search(r'account\s+([A-Za-z0-9][A-Za-z0-9_.-]*)', text)
        return m.group(1) if m else ''
    if name == 'vercel':
        lines = [l.strip() for l in (out or '').splitlines() if l.strip()]
        return lines[-1] if lines else ''
    return ''

def login_files(name, home=None, env=None):
    """Where a CLI keeps its login. Existence is checked only; the files are never opened."""
    h, env = Path(home) if home else Path.home(), os.environ if env is None else env
    if name == 'vercel':
        bases = [h / 'Library' / 'Application Support', Path(env.get('XDG_DATA_HOME') or h / '.local' / 'share')]
        if env.get('APPDATA'): bases.append(Path(env['APPDATA']))
        return [b / 'com.vercel.cli' / 'auth.json' for b in bases] + [h / '.vercel' / 'auth.json']
    if name == 'supabase':
        return [h / '.supabase' / 'access-token']
    return []

def probe_access(name, with_accounts, home=None, env=None):
    """Exit code only. Output is discarded unless the caller asked for account names.
    vercel and supabase are checked OFFLINE by login-file presence: calling them can start a browser
    sign-in (OAuth) prompt on every run. GRAPH_SKILL_NO_PROBES=1 turns all access calls off."""
    if os.environ.get('GRAPH_SKILL_NO_PROBES') == '1': return {'status': 'unknown'}
    if not shutil.which(name): return {'status': 'unknown'}
    files = login_files(name, home, env)
    if files and not with_accounts:
        return {'status': 'ok' if any(f.exists() for f in files) else 'no', 'method': 'login file'}
    r = run(ACCESS[name], capture=with_accounts)
    if r is None: return {'status': 'unknown'}
    d = {'status': 'ok' if r[0] == 0 else 'no'}
    if with_accounts and r[0] == 0:
        acct = parse_account(name, r[1], r[2])
        if acct: d['account'] = acct[:80]
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
    try: has_xcrun = shutil.which('xcrun')
    except Exception: return 'unknown'  # the lookup itself broke: say so, not "no"
    if sys.platform != 'darwin' or not has_xcrun: return 'no'
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
    # One function for preflight and init. Codex variables win over a leaked CLAUDECODE.
    if any(k.startswith('CODEX_') for k in env): return 'codex'
    if env.get('CLAUDECODE'): return 'claude-code'
    if env.get('CURSOR_TRACE_ID') or env.get('CURSOR_AGENT'): return 'cursor'
    if any(k.startswith('OPENCODE') for k in env): return 'opencode'
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

def read_front(p, max_lines=80):
    """Read only the frontmatter of a file. Return (top-level keys with values, raw frontmatter text).
    Indented lines and block scalars (> or |) are joined onto their key. The body is never read."""
    lines = []
    try:
        with open(p, encoding='utf-8', errors='replace') as f:
            if f.readline().strip() != '---': return {}, ''
            for _ in range(max_lines):
                line = f.readline()
                if not line or line.strip() == '---': break
                lines.append(line.rstrip('\n'))
    except OSError: return {}, ''
    out, key = {}, None
    for line in lines:
        m = re.match(r'^([A-Za-z_][\w-]*):\s*(.*)$', line)
        if m:
            key = m.group(1); out[key] = '' if m.group(2).strip() in ('>', '|', '>-', '|-') else m.group(2).strip()
        elif key and line.strip(): out[key] = (out[key] + ' ' + line.strip()).strip()
    return {k: v.strip().strip('"\'') for k, v in out.items()}, '\n'.join(lines)

DESC_MAX = 200
OFF_WORDS = ('off', 'disabled', 'disable', 'false', 'hidden')

def is_off_value(v):
    return v is False or (isinstance(v, str) and v.strip().lower() in OFF_WORDS)

def skill_item(skill_md, src, settings, plugin=None, plugin_off=False):
    front, _ = read_front(skill_md)
    base = front.get('name') or skill_md.parent.name
    name = f'{plugin}:{base}' if plugin else base
    ov = settings.get('skillOverrides') if isinstance(settings.get('skillOverrides'), dict) else {}
    off = plugin_off or is_off_value(ov.get(name)) or is_off_value(ov.get(base))
    return {'name': name, 'kind': 'skill', 'src': src, 'desc': ' '.join(front.get('description', '').split())[:DESC_MAX],
            'off': bool(off), 'off_why': ('plugin is disabled' if plugin_off else 'skillOverrides') if off else ''}

def plugin_dirs(home, key):
    """Install folders of one plugin: installed_plugins.json first, else the cache layout cache/<market>/<name>/<version>.
    Only folders under ~/.claude/plugins are used."""
    root = (home / '.claude' / 'plugins').resolve()
    name, _, market = key.partition('@')
    found = []
    entries = read_json(home / '.claude' / 'plugins' / 'installed_plugins.json').get('plugins')
    for e in (entries.get(key) if isinstance(entries, dict) and isinstance(entries.get(key), list) else []):
        try:
            p = Path(e['installPath']).resolve()
            if p.is_dir() and root in p.parents: found.append(p)
        except Exception: pass
    if not found and market and not any(s in part for part in (market, name) for s in ('/', '\\', '..')):
        try:
            cands = sorted((d.resolve() for d in (root / 'cache' / market / name).iterdir() if d.is_dir()), key=lambda d: d.stat().st_mtime)
            found = [c for c in cands if root in c.parents][-1:]
        except Exception: pass
    return found

def find_skill_files(base, manifest=None, cap=300):
    """SKILL.md files Claude Code loads from a plugin: skills/<name>/SKILL.md, plus skill folders that plugin.json declares.
    No recursive walk, so deprecated or sample folders are not listed. Only paths under the plugin folder are used."""
    base = Path(base).resolve()
    out, dirs = [], []
    try: dirs += sorted(p for p in (base / 'skills').iterdir() if p.is_dir())
    except OSError: pass
    decl = (manifest or {}).get('skills')
    for s in ([decl] if isinstance(decl, str) else decl if isinstance(decl, list) else []):
        if not isinstance(s, str): continue
        try: q = (base / s).resolve()
        except Exception: continue
        if q != base and base in q.parents and q.is_dir():
            if (q / 'SKILL.md').is_file(): dirs.append(q)
            else:
                try: dirs += sorted(x for x in q.iterdir() if x.is_dir())
                except OSError: pass
    for d in dirs:
        f = d / 'SKILL.md'
        try: ok = f.is_file() and base in f.resolve().parents
        except OSError: ok = False
        if ok and f not in out: out.append(f)
        if len(out) >= cap: break
    return out

def merged_settings(cwd, home):
    """enabledPlugins and skillOverrides merged in Claude Code order: user, project settings.json, project settings.local.json. Later wins."""
    out = {'enabledPlugins': {}, 'skillOverrides': {}}
    for p in (home / '.claude' / 'settings.json', cwd / '.claude' / 'settings.json', cwd / '.claude' / 'settings.local.json'):
        s = read_json(p)
        for k in out:
            if isinstance(s.get(k), dict): out[k].update(s[k])
    return out

NAME_RE = re.compile(r'^[\w.-]+$')

def skill_catalog(cwd, home):
    """Skills and plugins with name and a description cut to 200 chars. Never the body of any file.
    off is true for skills in skillOverrides set to off, and for plugins (and their skills) switched off in enabledPlugins."""
    settings = merged_settings(cwd, home)
    items, seen = [], set()
    def add(it):
        k = (it['kind'], it['name'], it['src'])
        if k not in seen: seen.add(k); items.append(it)
    for src, d in (('project', cwd / '.claude/skills'), ('user', home / '.claude/skills'), ('project-agents', cwd / '.agents/skills'),
                   ('user-agents', home / '.agents/skills'), ('codex', home / '.codex/skills')):
        try: subs = sorted(p for p in d.iterdir() if p.is_dir())
        except Exception: continue
        for p in subs:
            if (p / 'SKILL.md').is_file(): add(skill_item(p / 'SKILL.md', src, settings))
            else: add({'name': p.name, 'kind': 'skill', 'src': src, 'desc': '', 'off': is_off_value((settings.get('skillOverrides') or {}).get(p.name)) if isinstance(settings.get('skillOverrides'), dict) else False, 'off_why': ''})
    enabled = settings.get('enabledPlugins') if isinstance(settings.get('enabledPlugins'), dict) else {}
    for key in sorted(enabled):
        name = key.partition('@')[0]; off = enabled[key] is not True
        dirs = safe(lambda: plugin_dirs(home, key), [])
        desc, real, manifest = '', '', {}
        for d in dirs:
            try:
                m = read_json(d / '.claude-plugin' / 'plugin.json')
                if isinstance(m, dict):
                    desc = desc or str(m.get('description') or '')
                    if not real and isinstance(m.get('name'), str) and NAME_RE.match(m['name']): real, manifest = m['name'], m
            except Exception: pass
        prefix = real or name
        add({'name': name, 'kind': 'plugin', 'src': key.partition('@')[2] or 'plugin', 'desc': ' '.join(desc.split())[:DESC_MAX],
             'off': off, 'off_why': 'enabledPlugins is false' if off else ''})
        for d in dirs:
            for f in find_skill_files(d, manifest):
                add(skill_item(f, 'plugin ' + name, settings, plugin=prefix, plugin_off=off))
    return items

def skill_names(cwd, home):
    return sorted({it['name'] for it in skill_catalog(cwd, home) if it['kind'] == 'skill' and not it['off']})

def always_apply(p):
    """Read only the frontmatter of a Cursor rule."""
    try:
        text = p.read_text(encoding='utf-8')
        if not text.startswith('---'): return False
        front = text.split('---', 2)[1]
        return bool(re.search(r'^alwaysApply:\s*true\s*$', front, re.M))
    except Exception: return False

def project_root(cwd, env=None, override=None):
    """Folder Claude Code keys its memory by: --project-dir, else CLAUDE_PROJECT_DIR, else the git top-level, else cwd."""
    env = os.environ if env is None else env
    for v in (override, env.get('CLAUDE_PROJECT_DIR')):
        if v:
            with contextlib.suppress(Exception):
                q = Path(v).expanduser()
                if q.is_dir(): return q.resolve()
    with contextlib.suppress(Exception):
        r = subprocess.run(['git', 'rev-parse', '--show-toplevel'], text=True, capture_output=True, encoding='utf-8', errors='replace', cwd=str(cwd), timeout=10)
        if r.returncode == 0 and r.stdout.strip(): return Path(r.stdout.strip()).resolve()
    return Path(cwd)

def memory_index(cwd, home, env=None, override=None):
    """MEMORY.md path for this project, trying the project root first, then cwd. Returns the first one that exists, else the project-root path."""
    roots = [project_root(cwd, env, override), Path(cwd)]
    paths = [home / '.claude' / 'projects' / re.sub(r'[^A-Za-z0-9]', '-', str(r)) / 'memory' / 'MEMORY.md' for r in roots]
    return next((p for p in paths if p.is_file()), paths[0])

def context_files(cwd, home):
    found = []
    mem = memory_index(cwd, home)
    for label, p in (('./CLAUDE.md', cwd / 'CLAUDE.md'), ('~/.claude/CLAUDE.md', home / '.claude/CLAUDE.md'),
                     ('./AGENTS.md', cwd / 'AGENTS.md'), ('~/.codex/AGENTS.md', home / '.codex/AGENTS.md'),
                     ('claude memory (MEMORY.md)', mem)):
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
                futures[('access', n)] = pool.submit(safe, lambda n=n: probe_access(n, with_accounts, home, env), {'status': 'unknown'})
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
    result['catalog'] = safe(lambda: skill_catalog(cwd, home), [])  # fresh each call: OFF switches change often
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

def login_gate(tool, questions, notes):
    """A needed login: use the remembered answer, or add ONE question. Never asks twice."""
    key, cmd = f'login:{tool}', LOGIN[tool]
    e = permits.get(key)
    if e and e['decision'] == 'allowed':
        notes.append(f"remembered: allowed ({e['at'][:10]}) {key}")
        return f'{tool} is not logged in. The user allowed login (remembered {e["at"][:10]}): run `{cmd}` yourself'
    if e:
        notes.append(f"remembered: {e['decision']} ({e['at'][:10]}) {key}")
        return f'{tool} login was {e["decision"]} by the user (remembered {e["at"][:10]}). Use another option'
    name = 'GitHub' if tool == 'gh' else tool.capitalize()
    questions.append(f'\U0001f511 {name} is not logged in. Ask the user once: log in (run `{cmd}` yourself) or skip? Then: permit --key {key} --allow|--skip --note "<the user\'s words>"')
    return f'{tool} is not logged in. Ask the user once: log in (run `{cmd}` yourself) or skip'

def resolve(jobs, needs, r, questions=None, notes=None):
    questions = [] if questions is None else questions; notes = [] if notes is None else notes
    out = {}
    for need in needs:
        res = [(c, *check(c, r)) for c in jobs[need]]
        pick = next((x for s in ('ok', 'unsure') for x in res if x[1] == s), None)
        if pick:
            out[need] = {'use': pick[0], 'note': pick[2]}
        else:
            why = next((x[2] for x in res if 'not logged in' in x[2]), res[0][2] if res else 'no options')
            m = re.match(r'(\w+) CLI not logged in', why)
            if m and m.group(1) in LOGIN:
                out[need] = {'use': None, 'workaround': login_gate(m.group(1), questions, notes)}
            else: out[need] = {'use': None, 'workaround': f'{why}, or ask the user'}
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
             f"MCP: {short(r['mcp'])} | skills: {len(r['skills'])} ({short(r['skills'], 5)})" + (f", {sum(1 for c in r.get('catalog', []) if c['off'])} turned off" if any(c['off'] for c in r.get('catalog', [])) else ''),
             f"context files: {short(ctx)} (* = alwaysApply)",
             f"verify: headless-browser {v.get('headless_browser')}, ios-sim {v.get('ios_simulator')}, "
             f"android {v.get('android_emulator')}, iphone-mirroring {v.get('iphone_mirroring')}"]
    needs = [f"need {k}: " + (f"{d['use']}" + (f" ({d['note']})" if d['note'] else '') if d['use'] else d['workaround'])
             for k, d in r.get('needs', {}).items()]
    if len(needs) > 2: needs = [needs[0], ' ; '.join(needs[1:])]
    needs += [f'no mapping for {n}, use your judgment' for n in r.get('unmapped', [])]
    return '\n'.join(lines + needs)

def run_preflight(needs, refresh=False, with_accounts=False, as_json=False, info=None):
    """Return the text to print. Needs with no mapping are reported, not refused. 'none' means no needs."""
    jobs = load_jobs()
    needs = [n for n in needs if n.lower() != 'none']
    unmapped = [n for n in needs if n not in jobs]
    needs = [n for n in needs if n in jobs]
    r = scan(Path.cwd(), Path.home(), os.environ, refresh, with_accounts)
    questions, notes = [], []
    if needs: r['needs'] = resolve(jobs, needs, r, questions, notes)
    if unmapped: r['unmapped'] = unmapped
    if info is not None and unmapped: info['unmapped'] = unmapped
    if info is not None: info['questions'] = list(dict.fromkeys(questions)); info['permits'] = list(dict.fromkeys(notes))
    if info is not None:  # names and statuses only, for the run log
        info['needs'] = {k: {'tool': d['use'], 'note': d.get('note', '')} for k, d in r.get('needs', {}).items()}
        info['workarounds'] = {k: d['workaround'] for k, d in r.get('needs', {}).items() if not d['use']}
        info['access'] = dict(r['access'])
        if with_accounts: info['accounts'] = dict(r.get('accounts', {}))
    if as_json: return json.dumps(r, indent=2)
    return summary(r)
