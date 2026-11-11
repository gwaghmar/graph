#!/usr/bin/env python3
"""Graph statusline helper. OPT-IN: nothing runs it until the user adds it to Claude Code settings
(npx graph-skill statusline --enable). It reads the statusline JSON on stdin and writes only usage numbers
(rate limit percentages, reset times, model id, context window size) to ~/.cache/graph-skill/claude-rate-limits.json.
It never stores the session id, paths, prompts or any message content.
Usage: statusline.py [existing statusline command]. With a command, that command still draws the status line."""
from __future__ import annotations
import contextlib, datetime, json, os, subprocess, sys, time
from pathlib import Path

WINDOWS = (('five_hour', '5h'), ('seven_day', '7d'))

def cache_path(): return Path.home() / '.cache' / 'graph-skill' / 'claude-rate-limits.json'

def num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v else None

def epoch(v):
    if num(v) is not None: return v / 1000 if v > 1e12 else v
    with contextlib.suppress(ValueError, TypeError):
        return datetime.datetime.fromisoformat(str(v).replace('Z', '+00:00')).timestamp()
    return None

def extract(data, now=None):
    """The small dict that is stored. Numbers and ids only."""
    if not isinstance(data, dict): return None
    out = {'written_at': now if now is not None else time.time(), 'rate_limits': {}}
    m = data.get('model')
    mid = m.get('id') if isinstance(m, dict) else m if isinstance(m, str) else None
    if isinstance(mid, str) and len(mid) <= 100: out['model'] = mid
    cw = data.get('context_window')
    size = num(cw.get('context_window_size')) if isinstance(cw, dict) else None
    if size is not None: out['context_window'] = int(size)
    rl = data.get('rate_limits')
    for key, _ in WINDOWS:
        w = rl.get(key) if isinstance(rl, dict) else None
        used = num(w.get('used_percentage')) if isinstance(w, dict) else None
        if used is not None: out['rate_limits'][key] = {'used_percentage': used, 'resets_at': epoch(w.get('resets_at'))}
    return out

def write_cache(out, path=None):
    p = path or cache_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + f'.{os.getpid()}.tmp')
    tmp.write_text(json.dumps(out) + '\n', encoding='utf-8')
    os.replace(tmp, p)

def status_line(data):
    parts = []
    m = data.get('model') if isinstance(data, dict) else None
    name = m.get('display_name') if isinstance(m, dict) else None
    if isinstance(name, str) and name: parts.append(name[:30])
    rl = (data.get('rate_limits') if isinstance(data, dict) else None) or {}
    for key, label in WINDOWS:
        w = rl.get(key) if isinstance(rl, dict) else None
        used = num(w.get('used_percentage')) if isinstance(w, dict) else None
        if used is not None: parts.append(f'{label} {round(used)}%')
    return ' | '.join(parts) if parts else 'graph'

def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try: sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception: pass
    raw = sys.stdin.read()
    try: data = json.loads(raw)
    except ValueError: data = None
    with contextlib.suppress(Exception):
        out = extract(data)
        if out: write_cache(out)
    wrapped = ' '.join(argv).strip()
    if wrapped:
        try:
            r = subprocess.run(wrapped, shell=True, input=raw, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5)
            if r.stdout.strip(): print(r.stdout.rstrip('\n')); return 0
        except (OSError, subprocess.SubprocessError): pass
    print(status_line(data))
    return 0

if __name__ == '__main__': sys.exit(main())
