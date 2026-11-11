#!/usr/bin/env python3
"""Graph: read the user, pick tools. Helpers for the context and tools commands.
Prints file names, dates and ages only. It never prints file bodies, env values or the OS user name:
home paths are shown as ~ and the memory folder name is never shown (the context and tools commands, and preflight --json)."""
from __future__ import annotations
import datetime, os, re, time
from pathlib import Path

import preflight

STALE_DAYS = 30
MAX_NOTES = 300

def today_utc():
    return time.strftime('%Y-%m-%d', time.gmtime())

def _day_from_mtime(p):
    return datetime.datetime.fromtimestamp(p.stat().st_mtime, datetime.timezone.utc).date()

def _age(day, today):
    return (datetime.date.fromisoformat(today) - day).days

def memory_dir(cwd, home, project=None):
    """Memory folder of this project (see preflight.memory_index for how the project folder is chosen)."""
    return preflight.memory_index(cwd, home, override=project).parent

def note_day(p):
    """Date of a memory note: metadata.modified from its frontmatter if present, else the file mtime."""
    _, raw = preflight.read_front(p)
    m = re.search(r'^\s*modified:\s*[\'"]?(\d{4}-\d{2}-\d{2})', raw, re.M)
    if m:
        try: return datetime.date.fromisoformat(m.group(1)), 'modified'
        except ValueError: pass
    return _day_from_mtime(p), 'file date'

def linked_notes(index):
    """Names of .md files linked from MEMORY.md. Only plain file names in the same folder are accepted."""
    try: text = index.read_text(encoding='utf-8', errors='replace')
    except OSError: return []
    names = []
    for m in re.finditer(r'\]\(([^)\s#]+\.md)\)', text):
        n = m.group(1)
        if '/' in n or '\\' in n or n.startswith('.') or n == 'MEMORY.md' or n in names: continue
        names.append(n)
    return names

def context_entries(cwd, home, today, project=None):
    """List of dicts: group, label, day, age, flag, stale. Highest priority first."""
    out = []
    def add(group, label, p, flag=''):
        try: day = _day_from_mtime(p)
        except OSError: return
        out.append({'group': group, 'label': label, 'day': day.isoformat(), 'age': _age(day, today), 'flag': flag, 'stale': False})
    def rules(group, base, prefix):
        try: files = sorted((base / 'rules').glob('*.mdc'))
        except OSError: files = []
        for p in files: add(group, f'{prefix}rules/{p.name}', p, 'alwaysApply' if preflight.always_apply(p) else '')
    for label, p in (('~/.claude/CLAUDE.md', home / '.claude/CLAUDE.md'), ('~/.codex/AGENTS.md', home / '.codex/AGENTS.md')):
        if p.is_file(): add('global', label, p)
    rules('global', home / '.cursor', '~/.cursor/')
    for label, p in (('./CLAUDE.md', cwd / 'CLAUDE.md'), ('./AGENTS.md', cwd / 'AGENTS.md')):
        if p.is_file(): add('project', label, p)
    rules('project', cwd / '.cursor', './.cursor/')
    mem = memory_dir(cwd, home, project)
    if (mem / 'MEMORY.md').is_file():
        add('memory', 'memory index (MEMORY.md)', mem / 'MEMORY.md')
        for n in linked_notes(mem / 'MEMORY.md')[:MAX_NOTES]:
            p = mem / n
            if not p.is_file(): continue
            try: day, how = note_day(p)
            except OSError: continue
            age = _age(day, today); stale = age > STALE_DAYS
            out.append({'group': 'memory', 'label': f'memory note {n}', 'day': day.isoformat(), 'age': age,
                        'flag': how, 'stale': stale})
    return out

def context_report(cwd, home, today=None, project=None):
    """Return (text, stale note names, number of files)."""
    today = today or today_utc()
    entries = context_entries(cwd, home, today, project)
    no_mem = not any(e['group'] == 'memory' for e in entries)
    lines = [f'Today is {today}.', 'User context, highest priority first (global, then project, then memory):']
    if not entries: lines.append('  none found')
    for i, e in enumerate(entries, 1):
        ago = f"{e['age']} day{'' if e['age'] == 1 else 's'} ago" if e['age'] >= 0 else 'in the future'
        flag = f" ({e['flag']})" if e['flag'] else ''
        mark = '  STALE: check before using' if e['stale'] else ''
        lines.append(f"  {i}. {e['group']:<7} {e['label']}{flag}  {e['day']} ({ago}){mark}")
    stale = [e['label'].replace('memory note ', '') for e in entries if e['stale']]
    if no_mem: lines.append('Stale: unknown, memory index not found for this folder (pass --project-dir <folder the session started in> to point at it)')
    else: lines.append(f'Stale: {len(stale)} notes' + (f" ({', '.join(stale[:20])})" if stale else ''))
    lines.append('On Claude Code these files are already in context: do not re-read them. '
                 'On other hosts, read the global files, then the project files, now.')
    lines.append(f'Rules: a STALE note is a hint to verify, not a fact. If two rules conflict, the newer and more specific one wins: say so. '
                 f'Stale means older than {STALE_DAYS} days.')
    return '\n'.join(lines), stale, len(entries)

# ---- tools ----
STOP = {'the', 'and', 'for', 'with', 'that', 'this', 'from', 'use', 'when', 'you', 'your', 'are', 'any', 'all', 'can', 'not',
        'into', 'was', 'has', 'have', 'its', 'new', 'add', 'make', 'build', 'task', 'user', 'users', 'will', 'also', 'via', 'etc'}

def words(text):
    out = set()
    for w in re.findall(r'[a-z0-9]+', (text or '').lower()):
        if len(w) > 4 and w.endswith('s'): w = w[:-1]
        if len(w) > 2 and w not in STOP: out.add(w)
    return out

def score(task_words, item):
    name_w = words(item['name'].replace(':', ' ').replace('-', ' ').replace('_', ' '))
    desc_w = words(item['desc'])
    return 2 * len(task_words & name_w) + len(task_words & (desc_w - name_w))

def rank(catalog, task):
    tw = words(task)
    scored = [(score(tw, it), it) for it in catalog]
    scored = sorted(((s, it) for s, it in scored if s > 0), key=lambda x: (-x[0], x[1]['name']))
    return [x for x in scored if not x[1]['off']], [x for x in scored if x[1]['off']]

def one_line(it, s, width=110):
    d = it['desc'] or 'no description'
    d = d if len(d) <= width else d[:width - 3] + '...'
    return f"{it['name']} ({it['kind']}, {it['src']}) score {s}: {d}"

def tools_report(catalog, task, needs):
    """needs: {need: {'use': str|None, 'workaround': str}} or None when the scan failed."""
    on, off = rank(catalog, task)
    lines = [f"Matched the task against {len(catalog)} skills and plugins ({sum(1 for c in catalog if c['off'])} turned off). Best first:"]
    if on:
        lines += [f'  {i}. {one_line(it, s)}' for i, (s, it) in enumerate(on[:8], 1)]
    else:
        lines.append('  no skill or plugin matched the task words.')
    for s, it in off[:5]:
        if s >= 2: lines.append(f"matches but turned off: {it['name']}; ask the user before turning it on")
    lines.append('CLI and MCP picks (from jobs.json):')
    if needs is None: lines.append('  scan failed: run preflight --refresh')
    for k, d in (needs or {}).items():
        lines.append(f"  {k}: " + (d['use'] if d.get('use') else 'none (' + d.get('workaround', '') + ')'))
    lines.append('Next: tell the user your picks, then record them with choose --run <id> --skills a,b --plugins p --clis gh --mcp x --why "<one line>".')
    return '\n'.join(lines)

NAME_OK = re.compile(r'^[\w.:@/+-]+$')

def split_names(text):
    return [x.strip() for x in (text or '').split(',') if x.strip()]

def bad_names(names):
    return [n for n in names if not NAME_OK.match(n)]

def check_toolkit(cwd, home, skills, plugins, clis, mcp, catalog):
    """Return (not_found, off) lists of 'kind name'. Unknown names are allowed but flagged."""
    import shutil
    miss, off = [], []
    by = {}
    for it in catalog: by.setdefault((it['kind'], it['name']), it)
    for n in skills:
        it = by.get(('skill', n)) or next((v for (k, nm), v in by.items() if k == 'skill' and nm.split(':')[-1] == n), None)
        if not it: miss.append(f'skill {n}')
        elif it['off']: off.append(f'skill {n}')
    for n in plugins:
        it = by.get(('plugin', n.partition('@')[0]))
        if not it: miss.append(f'plugin {n}')
        elif it['off']: off.append(f'plugin {n}')
    for n in clis:
        if not shutil.which(n): miss.append(f'cli {n}')
    have = [m.lower() for m in preflight.safe(lambda: preflight.mcp_servers(cwd, home), [])]
    for n in mcp:
        if n.lower() not in have: miss.append(f'mcp {n}')
    return miss, off
