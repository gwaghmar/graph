#!/usr/bin/env python3
"""Graph checkpoint: one short screen where the user keeps or drops each item, then presses GO.

This module only builds and shows items. It never changes run state. graph.py applies the answer.
Everything shown is plain text from the run state. Fetched text is cleaned first. No network loads in the page."""
from __future__ import annotations
import base64, html, json, os, re, socket, sys, time
from pathlib import Path

import outside

CTRL = re.compile(r'[\x00-\x1f\x7f​-‏‪-‮⁦-⁩﻿]')

def clean(text, n=60):
    """One short plain line, cut at a word edge."""
    t = CTRL.sub(' ', str(text or '')); t = ' '.join(t.split())
    if len(t) <= n: return t
    cut = t[:n].rsplit(' ', 1)[0] or t[:n]
    return cut.rstrip(' ,.;:') + '...'

def color_on(stream=None):
    s = stream or sys.stdout
    try: tty = s.isatty()
    except Exception: tty = False
    return bool(tty) and not os.environ.get('NO_COLOR')

def paint(text, code, on):
    return f'\033[{code}m{text}\033[0m' if on else text

def scan_book(run_dir):
    try: return json.loads((Path(run_dir) / 'scan.json').read_text(encoding='utf-8'))
    except (OSError, ValueError): return {}

def safety_of(verdict, res=None):
    if verdict == 'package':
        eco = str((res or {}).get('package') or 'npm').split(':')[0]
        return (f"package: installed by {eco}, not scanned", 'warn')
    if verdict == 'big': return ('too big: reference only', 'warn')
    return {'clean': ('safe', 'ok'), 'warn': ('warnings', 'warn'), 'block': ('blocked', 'bad')}.get(verdict, ('not scanned', 'warn'))

def build_items(d, run_dir):
    """Cards the user can drop. Each has id, kind, part, title, why, badge (text, level) and more."""
    items = []
    for n in d.get('nodes', []):
        dep = n.get('depends_on') or []
        why = clean(n.get('role', '')) + (' · after ' + ', '.join(dep) if dep else '') + ('' if n.get('status') == 'pending' else f" · {n.get('status')}")
        items.append({'id': 'node:' + n['id'], 'kind': 'step', 'part': 'Step', 'title': clean(n.get('goal') or n['id']), 'why': why, 'badge': None, 'more': 0,
                      'locked': n.get('status') != 'pending'})
    o = d.get('outside') or {}
    book = scan_book(run_dir)
    picks = {p['part']: p for p in o.get('picks', [])}
    for part in o.get('parts', []):
        doc = outside.load_doc(Path(run_dir), part) or {}
        top = doc.get('top') or []
        pk = picks.get(part)
        repo = pk['use'] if pk else doc.get('recommended')
        if not repo: continue
        c = next((x for x in top if x.get('repo') == repo), {})
        res = book.get(repo) or {}
        badge = safety_of(res.get('verdict') or (pk or {}).get('verdict'), res or pk)
        ment = outside.total_mentions(c) if c else 0
        bits = [f'{ment} mentions' if ment else '', f"{c['stars']} stars" if c.get('stars') else '']
        items.append({'id': 'pick:' + part, 'kind': 'pick', 'part': clean(part, 30), 'title': clean(repo, 50),
                      'why': ' · '.join(b for b in bits if b) or ('picked' if pk else 'top find'), 'badge': badge, 'more': max(len(top) - 1, 0), 'locked': False})
    for f in d.get('features', []):
        items.append({'id': 'feat:' + f['id'], 'kind': 'feature', 'part': 'Feature people want', 'title': clean(f.get('name')), 'why': clean(f.get('why'), 70), 'badge': None, 'more': 0, 'locked': False})
    if d.get('campaign'):
        import growth
        try: items += growth.checkpoint_cards(d['campaign'])
        except growth.Refuse: pass
    return items

def cost_line(d):
    u = d.get('usage') or {}
    t = u.get('totals') if u.get('status') == 'ok' else None
    if t and not t.get('unknown') and t.get('cost') is not None: return f"${t['cost']:,.2f} so far"
    return '$0 / unknown'

def security_line(d, run_dir):
    book = scan_book(run_dir); picks = (d.get('outside') or {}).get('picks', [])
    if not picks: return 'No outside code used.'
    v = [(book.get(p['use']) or {}).get('verdict') or p.get('verdict') for p in picks]
    extra = (f", {v.count('package')} package(s) installed by npm or pip, not scanned" if v.count('package') else '') + (f", {v.count('big')} too big, reference only" if v.count('big') else '')
    return f"{len(picks)} outside pick(s): {v.count('clean')} safe, {v.count('warn')} with warnings, {v.count('block')} blocked{extra}. Nothing was run."

def hidden_count(d, run_dir):
    return sum((outside.load_doc(Path(run_dir), p) or {}).get('hidden', 0) for p in (d.get('outside') or {}).get('parts', []))

def model(d, run_dir):
    items = build_items(d, run_dir)
    return {'run': d['id'], 'task': clean(d.get('task'), 70), 'size': d.get('size', 'medium'), 'items': items, 'cost': cost_line(d),
            'security': security_line(d, run_dir), 'hidden': hidden_count(d, run_dir)}

# ---------- terminal ----------
def render_terminal(m, color=None, dropped=()):
    on = color_on() if color is None else color
    L = [paint(f"YOUR PLAN  ({m['size'].upper()})", '1', on), paint(m['task'], '2', on), '',
         paint("👉 Type what you DON'T need. Then GO.", '1;30;43', on), '']
    for i, it in enumerate(m['items'], 1):
        off = it['id'] in dropped
        tag = paint('DROP', '1;31', on) if off else paint('KEEP', '1;32', on)
        extra = ''
        if it['badge']: extra += '  ' + paint(('✔ ' if it['badge'][1] == 'ok' else '! ' if it['badge'][1] == 'warn' else '✖ ') + it['badge'][0], {'ok': '32', 'warn': '33', 'bad': '31'}[it['badge'][1]], on)
        if it['more']: extra += f"  +{it['more']} more"
        L.append(f"{paint(str(i) + '.', '1', on)} {tag}  {paint(it['part'].upper(), '36', on)}  {paint(it['title'], '1', on)}{extra}")
        if it['why']: L.append(f"        {it['why']}")
        if it.get('thumb'): L.append(f"        thumbnail: {it['thumb']}")
    L += ['', f"Cost: {m['cost']}", f"Security: {m['security']}"]
    if m['hidden']: L.append(f"hidden: {m['hidden']} below bar")
    L += ['', paint('Type  go   or   drop 2,4   (drops those, then goes)', '1;36', on)]
    return '\n'.join(L)

def parse_terminal(text, count):
    """'go' -> set(); 'drop 2,4' -> {2, 4} (1-based). Raises ValueError with a short message."""
    t = (text or '').strip().lower()
    if t in ('go', 'g'): return set()
    m = re.match(r'^drop\s+([\d,\s]+)$', t)
    if not m: raise ValueError('Type  go  or  drop 2,4')
    nums = {int(x) for x in re.findall(r'\d+', m.group(1))}
    if not nums: raise ValueError('Type  go  or  drop 2,4')
    bad = sorted(n for n in nums if not 1 <= n <= count)
    if bad: raise ValueError(f"No item {', '.join(map(str, bad))}. Use 1 to {count}.")
    return nums

# ---------- page ----------
CSS = """:root{--bg:#f3f5f7;--paper:#fff;--fg:#111a22;--muted:#5b6872;--line:#d5dde3;--do:#ffd23f;--do-fg:#1a1400;--keep:#1f9d55;--keep-soft:#e3f6eb;--drop:#c0392b;--drop-soft:#fbe7e5;--warn:#b86e00;--warn-soft:#fff1d6;--accent:#0f7c8c;
--display:"Space Grotesk",ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;--body:"IBM Plex Sans",ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#0d1217;--paper:#161e25;--fg:#eef3f6;--muted:#9aa8b2;--line:#2c3843;--keep:#4ccf86;--keep-soft:#163324;--drop:#ff7b6e;--drop-soft:#3a1916;--warn:#ffb648;--warn-soft:#3a2a10;--accent:#4fc2d1;color-scheme:dark}}
:root[data-theme="dark"]{--bg:#0d1217;--paper:#161e25;--fg:#eef3f6;--muted:#9aa8b2;--line:#2c3843;--keep:#4ccf86;--keep-soft:#163324;--drop:#ff7b6e;--drop-soft:#3a1916;--warn:#ffb648;--warn-soft:#3a2a10;--accent:#4fc2d1;color-scheme:dark}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:17px/1.4 var(--body)}
.wrap{max-width:560px;margin:0 auto;padding:24px 16px 48px;display:flex;flex-direction:column;gap:14px}
h1{font:700 34px/1.1 var(--display);margin:0}.ex{font:600 12px var(--body);letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
.do{background:var(--do);color:var(--do-fg);border-radius:14px;padding:16px 18px;font-family:var(--display)}.do b{display:block;font-size:26px}.do span{font-size:17px}
.card{background:var(--paper);border:3px solid var(--keep);border-radius:14px;padding:14px 16px;display:grid;grid-template-columns:1fr auto;gap:4px 12px;align-items:center;cursor:pointer;user-select:none}
.card:focus-visible,.go:focus-visible{outline:3px solid var(--accent);outline-offset:3px}
.card .part{font:600 13px var(--body);letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}.card .pick{font:700 22px var(--display);overflow-wrap:anywhere}.card .why{color:var(--muted);font-size:15px}
.tog{grid-row:1/4;grid-column:2;font:700 15px var(--display);padding:10px 14px;border-radius:99px;background:var(--keep);color:var(--paper);min-width:86px;text-align:center}
.card.off{border-color:var(--drop);background:var(--drop-soft)}.card.off .tog{background:var(--drop)}.card.off .pick{text-decoration:line-through;opacity:.6}
.card.lock{cursor:default;opacity:.8}.card.lock .tog{background:var(--muted)}
.badge{display:inline-block;font:700 12px var(--body);padding:2px 8px;border-radius:99px;margin-right:6px;background:var(--keep-soft);color:var(--keep)}.badge.warn{background:var(--warn-soft);color:var(--warn)}.badge.bad{background:var(--drop-soft);color:var(--drop)}
.more{font-size:14px;color:var(--accent)}.thumb{grid-column:1/3;max-width:100%;border-radius:10px;margin-top:6px}
.money,.sec{background:var(--paper);border-radius:14px;padding:14px 16px;border:3px solid var(--line);display:flex;justify-content:space-between;align-items:center;gap:10px}.money b{font:700 30px var(--display)}.sec{font-size:15px}
.go{font:700 26px var(--display);padding:18px;border:0;border-radius:14px;background:var(--fg);color:var(--bg);cursor:pointer}.go[disabled]{opacity:.5;cursor:default}
.msg{font:700 20px var(--display);text-align:center}.msg.ok{color:var(--keep)}.msg.bad{color:var(--drop)}
@media (prefers-reduced-motion:no-preference){.card{transition:background .15s,border-color .15s}}"""

JS = """const T=%TOKEN%;const cards=[...document.querySelectorAll('.card:not(.lock)')];
const dropped=()=>cards.filter(c=>c.classList.contains('off')).map(c=>c.dataset.id);
cards.forEach(c=>{const flip=()=>{const off=c.classList.toggle('off');c.setAttribute('aria-checked',String(!off));c.querySelector('.tog').textContent=off?'DROP':'KEEP'};
c.addEventListener('click',flip);c.addEventListener('keydown',e=>{if(e.key===' '||e.key==='Enter'){e.preventDefault();flip()}})});
const go=document.getElementById('go'),msg=document.getElementById('msg');
go.addEventListener('click',async()=>{const d=dropped();msg.className='msg';
if(!T){msg.className='msg ok';msg.textContent='Tell your AI: GO'+(d.length?' - drop: '+d.join(', '):'');msg.hidden=false;return}
go.disabled=true;
try{const r=await fetch('/go?t='+encodeURIComponent(T),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({dropped:d})});
const j=await r.json().catch(()=>({}));
if(r.ok){msg.className='msg ok';msg.textContent='\\u2713 Approved. Building now.'}else{msg.className='msg bad';msg.textContent=j.error||'Refused.';go.disabled=false}}
catch(e){msg.className='msg bad';msg.textContent='Could not reach the runtime.';go.disabled=false}
msg.hidden=false});"""

def thumb_html(it):
    """A small thumbnail for a video card, inlined so the page needs no file server. Only png, jpg or gif under 400 KB."""
    p = it.get('thumb')
    try:
        f = Path(p) if p else None
        if not f or not f.is_file() or f.stat().st_size > 400_000 or f.suffix.lower() not in ('.png', '.jpg', '.jpeg', '.gif'): return ''
        mime = 'image/png' if f.suffix.lower() == '.png' else 'image/gif' if f.suffix.lower() == '.gif' else 'image/jpeg'
        return f'<img class="thumb" alt="video thumbnail" src="data:{mime};base64,{base64.b64encode(f.read_bytes()).decode()}">'
    except OSError: return ''

def render_html(m, token=None):
    e = html.escape
    cards = []
    for it in m['items']:
        lock = it.get('locked')
        b = ''
        if it['badge']: b += f'<span class="badge {it["badge"][1]}">{"✓" if it["badge"][1] == "ok" else "!" if it["badge"][1] == "warn" else "✖"} {e(it["badge"][0])}</span>'
        more = f'<span class="more"> +{it["more"]} more</span>' if it['more'] else ''
        dis = ' aria-disabled="true"' if lock else ''
        cards.append(f'<div class="card{" lock" if lock else ""}" data-id="{e(it["id"])}" tabindex="{-1 if lock else 0}" role="switch" aria-checked="true"{dis}>'
                     f'<span class="part">{e(it["part"])}</span><span class="tog">{"LOCKED" if lock else "KEEP"}</span>'
                     f'<span class="pick">{e(it["title"])}</span><span class="why">{b}{e(it["why"])}{more}</span>{thumb_html(it)}</div>')
    hid = f'<p class="ex">hidden: {m["hidden"]} below bar</p>' if m['hidden'] else ''
    js = JS.replace('%TOKEN%', json.dumps(token).replace('<', '\\u003c'))
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><title>Graph checkpoint</title><style>{CSS}</style></head><body><main class="wrap">
<span class="ex">{e(m['size'])} job · {e(m['task'])}</span><h1>Your plan. 30 seconds.</h1>
<div class="do"><b>👉 Tap what you DON'T need.</b><span>Then press GO.</span></div>
{''.join(cards) or '<p class="ex">No items yet.</p>'}
<div class="money"><span>Cost to test</span><b>{e(m['cost'])}</b></div><div class="sec"><span>Security</span><span>{e(m['security'])}</span></div>{hid}
<button class="go" id="go" type="button">GO ▶</button><p class="msg" id="msg" role="alert" hidden></p></main><script>{js}</script></body></html>"""


# ---------- background server state: .graph/runs/<id>/checkpoint.json ----------
BROWSER_UA = re.compile(r'^Mozilla/\d.*\b(Chrome|Chromium|Safari|Firefox|Edg|OPR|Gecko)\b', re.I)

def ua_class(ua):
    """'browser' for a browser-looking User-Agent, else 'script' (curl, python, empty). A script can fake this header,
    so it is evidence only. It is not proof that a human pressed GO."""
    return 'browser' if BROWSER_UA.search(str(ua or '')) else 'script'

def pressed_text(cls):
    if cls == 'browser': return 'pressed GO from a browser'
    if cls == 'script': return 'pressed GO from a script (not proof of a human)'
    return 'pressed GO in the terminal'

def state_path(run_dir): return Path(run_dir) / 'checkpoint.json'

def read_state(run_dir):
    try:
        d = json.loads(state_path(run_dir).read_text(encoding='utf-8'))
        return d if isinstance(d, dict) else None
    except (OSError, ValueError): return None

def write_state(run_dir, **fields):
    """Merge fields into checkpoint.json (mode 0600: it holds the one-time link)."""
    p = state_path(run_dir); cur = read_state(run_dir) or {}
    cur.update(fields)
    tmp = p.with_suffix('.tmp')
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as fh: fh.write(json.dumps(cur) + '\n')
    os.replace(tmp, p)

def port_open(port):
    """True if something listens on 127.0.0.1:port. Portable: no signals, so no risk of killing a reused pid."""
    try:
        with socket.create_connection(('127.0.0.1', int(port)), timeout=1): return True
    except (OSError, ValueError, TypeError): return False

def live_state(run_dir):
    """The saved state, with 'waiting' turned into 'dead' if the server is gone."""
    st = read_state(run_dir)
    if st and st.get('status') == 'waiting':
        if time.time() > (st.get('expires') or 0): st['status'] = 'timeout'
        elif not port_open(st.get('port')): st['status'] = 'dead'
    return st
