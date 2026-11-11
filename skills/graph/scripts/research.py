"""Field brief and research mode. Pure rules over the run state; graph.py loads, saves and logs.

No per-field code or lists live here. The model writes the brief from today's sources.
The runtime only enforces what the brief says: windows per source type, the landmark rule,
undated sources, and the close rules. Nothing here uses the network.
"""
from __future__ import annotations
import calendar, datetime, re, time

MIN_SOURCES = 5
LEVELS = ('strong', 'medium', 'weak')
DATE_SOURCES = ('api', 'page', 'fetched', 'none')
SLUG = re.compile(r'^[a-z0-9][a-z0-9_-]{0,31}$')
DATE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
SECRET_PARAM = re.compile(r'((?:key|token|secret|password|passwd|auth|sig|signature|session|code)[a-z_]*=)[^&#\s]*', re.I)

_clock = time.time


class Refuse(Exception):
    def __init__(self, msg, code=3, todo=()):
        super().__init__(msg); self.code = code; self.todo = list(todo)

    def text(self):
        t = self.args[0]
        if self.todo: t += '\n👉 Do this:\n' + '\n'.join(f'  {i}. {x}' for i, x in enumerate(self.todo[:3], 1))
        return t


def refuse(msg, *todo): raise Refuse('Refused: ' + msg, 3, todo)
def usage_err(msg): raise Refuse(msg, 2)
def today(): return time.strftime('%Y-%m-%d', time.gmtime(_clock()))
def now(): return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(_clock()))
def clean(text, n=300): return ' '.join(str(text or '').split())[:n]


def valid_date(s):
    if not DATE.match(s or ''): return False
    try: datetime.date.fromisoformat(s)
    except ValueError: return False
    return True


def days_between(older, newer):
    def ts(s): return calendar.timegm(tuple(int(x) for x in s.split('-')) + (0, 0, 0))
    return (ts(newer) - ts(older)) // 86400


def R(d): return d.setdefault('research', {'sources': [], 'gaps': [], 'claims': []})
def brief_of(d): return d.get('field_brief')


def parse_windows(text):
    out = {}
    for part in (text or '').split(','):
        part = part.strip()
        if not part: continue
        if '=' not in part: usage_err(f'Bad --windows item "{clean(part, 40)}". Use type=days, for example paper=730,news=30. Use type=any for no age limit.')
        k, v = (x.strip().lower() for x in part.split('=', 1))
        if not SLUG.match(k): usage_err(f'Bad source type "{clean(k, 40)}" in --windows. Use letters, digits, "-" or "_".')
        if v == 'any': out[k] = 'any'
        elif v.isdigit() and 1 <= int(v) <= 36500: out[k] = int(v)
        else: usage_err(f'Bad window "{clean(part, 40)}". Days must be a whole number from 1 to 36500, or "any".')
    if not out: usage_err('--windows needs at least one item, for example paper=730,news=30.')
    return out


def split_lines(text):
    return [clean(x, 160) for x in re.split(r'[\n|]', text or '') if x.strip()]


def set_brief(d, field, sources, fresh, windows, landmark_ok, evidence, safety, review):
    vals = {'field': field, 'sources': sources, 'fresh': fresh, 'evidence': evidence, 'review': review, 'safety': safety, 'windows': windows, 'landmark-ok': landmark_ok}
    for k, v in vals.items():
        if not (v or '').strip(): usage_err(f'brief needs --{k} "<words>". Every part of the brief is required.')
    if landmark_ok not in ('yes', 'no'): usage_err('--landmark-ok must be yes or no.')
    win = parse_windows(windows)
    new = {'field': clean(field, 200), 'sources': clean(sources), 'fresh': clean(fresh), 'windows': win, 'landmark_ok': landmark_ok == 'yes',
           'evidence': clean(evidence), 'safety': split_lines(safety), 'review': clean(review), 'at': now()}
    old = brief_of(d)
    if (d.get('research') or {}).get('started') and old and (old['windows'] != win or old['landmark_ok'] != new['landmark_ok']):
        refuse('sources were already checked against this brief. Windows and the landmark rule cannot change after that.',
               'Keep the windows and the landmark rule. Or start a new run for the new brief.')
    d['field_brief'] = new
    return new


def need_brief(d):
    b = brief_of(d)
    if not b:
        refuse('no field brief yet.', 'Write the brief: brief --run <id> --field "<words>" --sources "<best sources today>" --fresh "<rule>" --windows type=days,... '
                                     '--landmark-ok yes|no --evidence "<standard>" --safety "<lines>" --review "<who checks>"')
    return b


def need_open(d):
    if (d.get('research') or {}).get('closed'): refuse('research is closed for this run.', 'Start a new run to research more.')


def safe_url(url):
    m = re.match(r'^(https?)://([^/?#\s]+)([^#\s]*)', (url or '').strip(), re.I)
    if not m: usage_err('--url must be a web link that starts with http:// or https://.')
    host = m.group(2).rsplit('@', 1)[-1]
    return SECRET_PARAM.sub(r'\1REDACTED', f'{m.group(1).lower()}://{host}{m.group(3)}')


TRACKING = re.compile(r'^(utm_[a-z]+|fbclid|gclid|mc_cid|mc_eid|ref|ref_src|igshid|si)$', re.I)


def canon_url(url):
    """Same page, same key: scheme dropped, host lowercased, no www, no fragment, no trailing slash, tracking params removed, query sorted."""
    m = re.match(r'^https?://([^/?#\s]+)([^?#\s]*)(?:\?([^#\s]*))?', (url or '').strip(), re.I)
    if not m: return (url or '').strip().lower()
    host = re.sub(r'^www\.', '', m.group(1).lower())
    q = sorted(x for x in (m.group(3) or '').split('&') if x and not TRACKING.match(x.split('=', 1)[0]))
    return host + (m.group(2) or '').rstrip('/') + ('?' + '&'.join(q) if q else '')


def add_source(d, url, kind, date, date_source, title, landmark=False, why=None, level=None):
    b = need_brief(d); need_open(d)
    kind = (kind or '').strip().lower()
    if not SLUG.match(kind): usage_err('--type needs one word for the kind, for example paper or news.')
    url = safe_url(url)
    if not clean(title): usage_err('--title is required.')
    if level is not None and level not in LEVELS: usage_err('--level must be strong, medium or weak.')
    if date_source not in DATE_SOURCES: usage_err('--date-source must be api, page, fetched or none.')
    if date and date_source == 'none': usage_err('--date was given, so --date-source cannot be none. Say where the date came from.')
    if not date and date_source != 'none': usage_err('No --date was given. Use --date-source none for an undated source.')
    if date and not valid_date(date): usage_err('--date must be a real date, YYYY-MM-DD.')
    if date and date > today(): usage_err('--date is in the future.')
    if landmark and not (why or '').strip(): usage_err('--landmark needs --why "<reason this old source still counts>".')
    r = R(d); r['started'] = True  # kept even if this call is refused: windows and the landmark rule are locked from now on
    win = b['windows'].get(kind, b['windows'].get('default'))
    if win is None:
        refuse(f'the brief has no window for type "{kind}".', f'Use a source type the brief lists: {", ".join(sorted(b["windows"]))}.',
               'Or start a new run with a brief that names this type.')
    if landmark and not b['landmark_ok']: refuse('the brief does not allow landmark sources.', 'Drop --landmark and use a newer source.')
    if any(canon_url(s['url']) == canon_url(url) for s in r['sources']): refuse('this link is already a source.')
    age = None
    if win != 'any':
        if not date:
            refuse(f'this source has no date, and the brief needs a date for type "{kind}".',
                   'Find the date (API field, page, or fetch header). Pass --date with --date-source.', 'Or use a different source.')
        age = days_between(date, today())
        if age > win and not landmark:
            refuse(f'this source is {age} days old. The window for "{kind}" is {win} days.', 'Use a newer source.',
                   'If it is a landmark and the brief allows landmarks, add --landmark --why "<reason>".')
    elif date: age = days_between(date, today())
    row = {'id': f"s{len(r['sources']) + 1}", 'url': url, 'type': kind, 'date': date or None, 'date_source': date_source,
           'title': clean(title, 160), 'level': level or 'medium', 'landmark': bool(landmark), 'at': now()}
    if age is not None: row['age_days'] = age
    if landmark: row['why'] = clean(why, 200)
    r['sources'].append(row)
    return row


def ids_of(d, text, what):
    ids = list(dict.fromkeys(x.strip() for x in (text or '').split(',') if x.strip()))
    if not ids: usage_err(f'{what} needs --sources s1,s2 (source ids from the source command).')
    known = {s['id'] for s in R(d)['sources']}
    bad = [x for x in ids if x not in known]
    if bad: refuse(f'unknown source id(s): {", ".join(clean(x, 20) for x in bad)}.', 'Add the source first with the source command.')
    return ids


def add_gap(d, complaint, sources, feature=None):
    need_brief(d); need_open(d)
    if not clean(complaint): usage_err('gap needs --complaint "<what people say is missing>".')
    ids = ids_of(d, sources, 'gap')
    r = R(d)
    row = {'id': f"g{len(r['gaps']) + 1}", 'complaint': clean(complaint, 200), 'sources': ids, 'weak': len(ids) < 2, 'feature': clean(feature, 60) or None}
    r['gaps'].append(row)
    return row


def add_claim(d, text, sources):
    need_brief(d); need_open(d)
    if not clean(text): usage_err('claim needs --text "<what the research says>".')
    ids = ids_of(d, sources, 'claim')
    r = R(d); row = {'id': f"c{len(r['claims']) + 1}", 'text': clean(text, 300), 'sources': ids}
    r['claims'].append(row)
    return row


def is_research(d):
    """A research-type plan has a node with role research, or has started recording sources."""
    return any(n.get('role') == 'research' for n in d.get('nodes', [])) or bool((d.get('research') or {}).get('started'))


def close(d, partial=None):
    need_brief(d); r = R(d)
    if r.get('closed'): refuse('research is already closed.')
    if partial is not None and not partial.strip(): usage_err('--partial needs a reason.')
    if is_research(d) and not partial:
        known = {s['id'] for s in r['sources']}
        problems = []
        if len(r['sources']) < MIN_SOURCES: problems.append(f"Only {len(r['sources'])} valid source(s). {MIN_SOURCES} are needed.")
        for row in r['gaps'] + r['claims']:
            if not row.get('sources') or any(x not in known for x in row['sources']):
                problems.append(f"{'Gap' if row['id'].startswith('g') else 'Claim'} {row['id']} does not cite valid source ids.")
        if problems:
            refuse('research cannot close yet. ' + ' '.join(problems), 'Add valid sources. Every gap and claim must cite source ids.',
                   'Or close what you have with --partial "<why it is not complete>". The report will say so.')
    r['closed'] = {'at': now(), 'partial': clean(partial, 200) if partial else None, 'sources': len(r['sources'])}
    return r['closed']


def status_lines(d):
    r = d.get('research') or {}; c = r.get('closed')
    return [f"Sources: {len(r.get('sources', []))} valid (need {MIN_SOURCES}). Gaps: {len(r.get('gaps', []))}. Claims: {len(r.get('claims', []))}.",
            ('Closed' + (f" (partial: {c['partial']})" if c.get('partial') else '') + '.') if c else 'Not closed.']


def safety_block(d):
    b = brief_of(d)
    return ['## ⚠ Read first'] + [f'- {x}' for x in b['safety']] + [''] if b and b.get('safety') else []


def card(d):
    """Checkpoint card text: (title, why)."""
    b = brief_of(d)
    return clean(b['field'], 60), clean(f"sources: {b['sources']} · fresh: {b['fresh']} · check: {b['review']}", 140)


def report_lines(d):
    b = brief_of(d); r = d.get('research') or {}
    if not b: return ['- No field brief was written.']
    L = [f"- Field: {b['field']}", f"- Best sources today: {b['sources']}", f"- Fresh rule: {b['fresh']}",
         '- Windows: ' + ', '.join(f"{k} {v if v == 'any' else str(v) + ' days'}" for k, v in b['windows'].items()),
         f"- Landmarks allowed: {'yes' if b['landmark_ok'] else 'no'}", f"- Evidence standard: {b['evidence']}",
         f"- Who should check this: {b['review']}"]
    srcs = r.get('sources', [])
    if srcs:
        L += ['', '| Source | Type | Date | Level | Landmark | Title |', '| --- | --- | --- | --- | --- | --- |']
        for s in srcs:
            date = (s['date'] + f" ({s['date_source']})") if s.get('date') else 'undated'
            lm = ('LANDMARK: ' + s.get('why', '')) if s['landmark'] else 'no'
            L.append(f"| {s['id']} | {s['type']} | {date} | {s['level']} | {lm.replace('|', '/')} | {s['title'].replace('|', '/')} |")
        L.append('')
        L.append('- Evidence levels: ' + ', '.join(f"{sum(1 for s in srcs if s['level'] == lv)} {lv}" for lv in LEVELS) + '.')
    else: L.append('- No sources recorded.')
    for g in r.get('gaps', []):
        L.append(f"- Gap {g['id']}: {g['complaint']} (sources {', '.join(g['sources'])})" + (' WEAK: fewer than 2 sources.' if g['weak'] else ''))
    for c in r.get('claims', []):
        L.append(f"- Claim {c['id']}: {c['text']} (sources {', '.join(c['sources'])})")
    c = r.get('closed')
    if c: L.append('- Research closed' + (f" PARTIAL: {c['partial']}" if c.get('partial') else '') + '.')
    elif srcs or r.get('started'): L.append('- Research is not closed.')
    return L
