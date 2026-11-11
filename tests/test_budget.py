import os; os.environ.setdefault("GRAPH_SKILL_NO_PROBES", "1")  # never call real CLIs (vercel whoami opens browser sign-in)
import tempfile as _tf; os.environ.setdefault("GRAPH_SKILL_CONFIG_DIR", _tf.mkdtemp(prefix="graph-perm-"))  # never touch the real ~/.config
import json, subprocess, sys, time, unittest
from pathlib import Path

from test_userctx import Base, write, CORE
from test_usage import UsageCase, line, MA, iso
sys.path.insert(0, str(CORE))
import budget, statusline  # noqa: E402

SECRET = 'SECRET-MESSAGE-BODY'


def hm(ts): return time.strftime('%H:%M', time.localtime(ts))


def rollout(home, rates, name='rollout-2026-11-10-aaa.jsonl', ts=None, days_old=0):
    """Fake Codex session file. rates is a list of (primary, secondary) pairs of (used, minutes, resets_at); the last one counts."""
    ts = ts or time.time()
    rows = [{'timestamp': iso(ts - 100), 'type': 'event_msg', 'payload': {'type': 'user_message', 'message': SECRET}}]
    for i, (p, s) in enumerate(rates):
        rl = {k: {'used_percent': v[0], 'window_minutes': v[1], 'resets_at': v[2]} for k, v in (('primary', p), ('secondary', s)) if v}
        rows.append({'timestamp': iso(ts - 50 + i), 'type': 'event_msg', 'payload': {'type': 'token_count', 'info': None, 'rate_limits': rl}})
    write(home / '.codex' / 'sessions' / '2026' / '11' / '10' / name, '\n'.join(json.dumps(r) for r in rows) + '\n', days_old)


def claude_cache(home, five=None, seven=None, written=None):
    rl = {}
    if five: rl['five_hour'] = {'used_percentage': five[0], 'resets_at': five[1]}
    if seven: rl['seven_day'] = {'used_percentage': seven[0], 'resets_at': seven[1]}
    write(home / '.cache/graph-skill/claude-rate-limits.json', json.dumps({'written_at': written or time.time(), 'model': 'fake-model-a', 'context_window': 200000, 'rate_limits': rl}))


def hist(home, rows):
    write(home / '.config/graph-skill/history.jsonl', '\n'.join(json.dumps(r) for r in rows) + '\n')


def h(tier, role, tokens, run='h'):
    return {'date': '2026-11-01', 'host': 'codex', 'role': role, 'tier': tier, 'run': run, 'node': 'n', 'tokens': {'input': tokens, 'output': 0, 'cache_read': 99999999, 'cache_write': 0, 'reasoning': 0}}


def calib(home, host, window, n, per_pct=1000):
    write(home / '.config/graph-skill/budget-calib.jsonl', ''.join(json.dumps({'host': host, 'window': window, 'run': f'c{i}', 'pct_before': 10, 'pct_after': 20, 'tokens': per_pct * 10}) + '\n' for i in range(n)))


class BudgetBase(Base):
    def setUp(self):
        super().setUp()
        self.g('init', 'task', '--id', 'r1', '--skip-preflight', 't', '--skip-context', 't', '--auto-approve', 'just do it')

    def plan(self, *specs):
        """specs: (id, dep or None, role). Build nodes in a chain or tree, pending."""
        for nid, dep, role in specs:
            extra = ['--depends-on', dep] if dep else []
            self.g('node', 'r1', nid, '--role', role, '--status', 'pending', '--goal', 'g', *extra)

    def budget(self, *extra, code=0):
        return self.g('budget', '--run', 'r1', '--host', 'codex', *extra, code=code)


class Adapters(BudgetBase):
    def test_codex_reads_last_event_and_never_content(self):
        t = time.time()
        rollout(self.home, [((10, 300, t + 1000), (5, 10080, t + 9000)), ((42, 300, t + 3600), (7, 10080, t + 86400))])
        r = self.budget()
        self.assertIn('5h: 58% left (42% used)', r.stdout); self.assertIn('7d: 93% left', r.stdout)
        self.assertRegex(r.stdout, rf'resets (\d{{4}}-\d\d-\d\d )?{hm(t + 3600)}')   # the date shows when it is not today
        self.assertIn('source codex session file', r.stdout)
        self.assertIn('Approximate', r.stdout)
        b = self.state()['budget']
        self.assertEqual({w['name'] for w in b['before']}, {'5h', '7d'})
        w = {x['name']: x for x in b['before']}['5h']
        self.assertEqual((w['used_pct'], w['window_minutes'], w['source']), (42, 300, 'codex session file'))
        for blob in (r.stdout, json.dumps(b), self.log()): self.assertNotIn(SECRET, blob)

    def test_codex_past_reset_means_refilled(self):
        t = time.time()
        rollout(self.home, [((90, 300, t - 60), (20, 10080, t + 86400))])
        r = self.budget()
        self.assertIn('5h: 100% left (0% used)', r.stdout); self.assertIn('source refilled', r.stdout)
        self.assertIn('7d: 80% left', r.stdout)

    def test_codex_newest_file_wins_and_no_data_is_unknown(self):
        t = time.time()
        rollout(self.home, [((90, 300, t + 99), None)], name='rollout-old.jsonl', days_old=2)
        rollout(self.home, [((30, 300, t + 99), None)], name='rollout-new.jsonl')
        self.assertIn('5h: 70% left', self.budget().stdout)

    def test_claude_cache_fresh_stale_and_expired(self):
        t = time.time()
        claude_cache(self.home, (35, t + 7200), (12, t + 400000))
        r = self.g('budget', '--run', 'r1', '--host', 'claude-code')
        self.assertIn('5h: 65% left', r.stdout); self.assertIn('source statusline cache', r.stdout)
        claude_cache(self.home, (35, t + 7200), written=t - 1200)
        r = self.g('budget', '--run', 'r1', '--host', 'claude-code')
        self.assertIn('unknown: the statusline cache is stale', r.stdout); self.assertIn('user decides', r.stdout)
        claude_cache(self.home, (35, t - 5))
        self.assertIn('unknown', self.g('budget', '--run', 'r1', '--host', 'claude-code').stdout)
        (self.home / '.cache/graph-skill/claude-rate-limits.json').unlink()
        self.assertIn('statusline --enable', self.g('budget', '--run', 'r1', '--host', 'claude-code').stdout)

    def test_unknown_host_and_user_answer(self):
        r = self.g('budget', '--run', 'r1', '--host', 'cursor')
        self.assertIn('unknown', r.stdout); self.assertIn('--left-pct', r.stdout)
        self.g('budget', '--run', 'r1', '--host', 'cursor', '--left-pct', '30', code=2)
        self.g('budget', '--run', 'r1', '--host', 'cursor', '--left-pct', '130', '--resets', '10:00', code=2)
        self.g('budget', '--run', 'r1', '--host', 'cursor', '--left-pct', '30', '--resets', '25:99', code=2)
        r = self.g('budget', '--run', 'r1', '--host', 'cursor', '--left-pct', '30', '--resets', '23:59')
        self.assertIn('user: 30% left (70% used)', r.stdout); self.assertIn('source user', r.stdout)
        self.assertEqual(self.state()['budget']['before'][0]['source'], 'user')


class Estimates(BudgetBase):
    def test_defaults_are_labelled_and_history_needs_five(self):
        self.plan(('a', None, 'build'), ('b', 'a', 'build'))
        rollout(self.home, [((10, 300, time.time() + 999), None)])
        hist(self.home, [h('build', 'build', 1000 * (i + 1)) for i in range(4)])
        r = self.budget()
        self.assertIn('default, not your history', r.stdout); self.assertIn('level 1: 80,000-180,000', r.stdout)
        hist(self.home, [h('build', 'build', 1000 * (i + 1)) for i in range(5)])  # cache reads are not counted
        r = self.budget()
        self.assertIn('your history, 5 samples', r.stdout); self.assertNotIn('default', r.stdout.split('basis:')[1])
        self.assertIn('level 1: 3,000-4,000', r.stdout)  # median 3000, p80 4000
        self.assertIn('total: 6,000-8,000', r.stdout)

    def test_cannot_convert_below_three_runs(self):
        self.plan(('a', None, 'build'))
        rollout(self.home, [((10, 300, time.time() + 999), None)]); calib(self.home, 'codex', '5h', 2)
        r = self.budget()
        self.assertIn('cannot convert to % yet', r.stdout); self.assertIn('unknown: user decides', r.stdout)
        calib(self.home, 'codex', '5h', 3)
        r = self.budget()
        self.assertIn('about 1,000 tokens per 1% (from 3 runs)', r.stdout)

    def recommend(self, used, build_tokens, quick_tokens=None, nodes=3):
        t = time.time()
        rollout(self.home, [((used, 300, t + 3600), None)]); calib(self.home, 'codex', '5h', 3)
        rows = [h('build', 'build', build_tokens, f'b{i}') for i in range(5)]
        if quick_tokens: rows += [h('quick', 'build', quick_tokens, f'q{i}') for i in range(5)]
        hist(self.home, rows)
        return self.budget().stdout

    def chain(self):
        self.plan(('a', None, 'build'), ('b', 'a', 'build'), ('c', 'b', 'build'))

    def test_all_now(self):
        self.chain(); out = self.recommend(50, 10000)  # 50 percent left at 1000 tokens per percent: 50,000
        self.assertIn('Recommendation: all now', out)

    def test_lighter_tier(self):
        self.chain(); out = self.recommend(50, 30000, 10000)
        self.assertIn('Recommendation: now with lighter tier for non-critical low-risk nodes (a, b, c)', out)

    def test_levels_now_and_rest_after_reset(self):
        self.chain(); out = self.recommend(80, 10000)  # 20,000 left: two levels
        rec = out.split('Recommendation: ')[1]
        self.assertIn('levels 1-2 now and the rest after the reset at', rec)
        self.assertIn('--from-level 3', rec); self.assertIn('Nothing is scheduled', rec)

    def test_nothing_fits(self):
        self.chain(); out = self.recommend(98, 10000)
        self.assertIn('nothing fits now: wait', out)

    def test_high_risk_and_deep_roles_never_lighten(self):
        self.g('node', 'r1', 'a', '--role', 'build', '--status', 'pending', '--goal', 'g', '--risk', 'high')
        self.plan(('r', None, 'review'))
        out = self.recommend(50, 400000, 10000, nodes=2)
        self.assertNotIn('now with lighter tier', out)

    def test_done_nodes_are_not_estimated(self):
        self.plan(('a', None, 'build'), ('b', 'a', 'build'))
        rollout(self.home, [((10, 300, time.time() + 999), None)])
        self.g('budget', '--run', 'r1', '--host', 'codex')  # records the step so nodes may start
        self.g('node', 'r1', 'a', '--role', 'build', '--status', 'skipped', '--reason', 'x')
        self.assertIn('level 2: 80,000', self.budget().stdout); self.assertNotIn('level 1:', self.budget().stdout)


class Gates(Base):
    def init(self, *extra):
        self.g('init', 'task', '--id', 'r1', '--skip-preflight', 't', '--skip-context', 't', *extra)

    def test_node_start_refused_until_budget_recorded(self):
        self.init('--auto-approve', 'just do it')
        r = self.g('node', 'r1', 'a', '--role', 'research', '--status', 'running', '--goal', 'g', code=3)
        self.assertIn('budget:', r.stderr); self.assertIn('--skip-budget', r.stderr)
        self.g('budget', '--run', 'r1', '--host', 'cursor')  # unknown usage still records the step
        self.g('node', 'r1', 'a', '--role', 'research', '--status', 'running', '--goal', 'g')
        self.assertIn('"outcome": "refused"', self.log()); self.assertIn('"cmd": "budget"', self.log())

    def test_skip_budget_waives_and_needs_a_reason(self):
        self.g('init', 'task', '--id', 'x', '--skip-budget', ' ', code=2)
        self.init('--auto-approve', 'just do it', '--skip-budget', 'user declined')
        self.g('node', 'r1', 'a', '--role', 'research', '--status', 'running', '--goal', 'g')
        self.assertIn('skipped', self.g('budget', '--run', 'r1').stdout)
        self.g('node', 'r1', 'a', '--role', 'research', '--status', 'complete')
        self.g('finish', 'r1', '--status', 'complete')
        self.assertIn('The budget step was skipped: user declined', (self.proj / '.graph/runs/r1/summary.md').read_text(encoding='utf-8'))

    def test_approve_needs_budget_and_prints_it(self):
        self.init()
        self.g('tools', '--run', 'r1', '--task', 'x')
        self.g('node', 'r1', 'a', '--role', 'research', '--status', 'pending', '--goal', 'g')
        self.g('approve', '--run', 'r1', '--note', 'go', code=3)
        self.g('budget', '--run', 'r1', '--host', 'cursor')
        r = self.g('approve', '--run', 'r1', '--note', 'go')
        self.assertIn('Approval recorded.', r.stdout); self.assertIn('Usage budget', r.stdout); self.assertIn('level 1:', r.stdout)
        self.assertEqual(self.state()['budget']['estimate'][0]['level'], 1)


class Defer(BudgetBase):
    def setUp(self):
        super().setUp()
        self.plan(('a', None, 'research'), ('b', 'a', 'research'), ('c', 'b', 'research'))
        self.budget()
        self.later = time.time() + 7200

    def iso(self, ts): return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(ts))

    def test_defer_refuses_start_and_next_shows_wait(self):
        r = self.g('defer', 'r1', '--from-level', '2', '--not-before', self.iso(self.later))
        self.assertIn(hm(self.later), r.stdout); self.assertIn('suggestion only', r.stdout)
        self.g('node', 'r1', 'a', '--role', 'research', '--status', 'running'); self.g('node', 'r1', 'a', '--role', 'research', '--status', 'complete')
        r = self.g('node', 'r1', 'b', '--role', 'research', '--status', 'running', code=3)
        self.assertIn(hm(self.later), r.stderr); self.assertIn('level 2', r.stderr)
        self.assertRegex(self.g('next', 'r1').stdout, r'b  \(waiting until (\d{4}-\d\d-\d\d )?' + hm(self.later) + r'\)')
        self.assertIn('"outcome": "refused"', self.log()); self.assertIn('"cmd": "defer"', self.log())

    def test_deferral_ends_at_the_time(self):
        self.g('defer', 'r1', '--from-level', '1', '--not-before', self.iso(time.time() - 5))
        self.g('node', 'r1', 'a', '--role', 'research', '--status', 'running')
        self.assertNotIn('waiting', self.g('next', 'r1').stdout)

    def test_defer_validation(self):
        self.g('defer', 'r1', '--from-level', '9', '--not-before', self.iso(self.later), code=2)
        self.g('defer', 'r1', '--from-level', '2', '--not-before', 'tomorrow-ish', code=2)
        self.g('node', 'r1', 'a', '--role', 'research', '--status', 'running')
        self.g('defer', 'r1', '--from-level', '1', '--not-before', self.iso(self.later), code=3)

    def test_export_only_levels_that_can_run_now(self):
        self.g('validate', 'r1')
        self.g('defer', 'r1', '--from-level', '2', '--not-before', self.iso(self.later))
        out = self.proj / 'w.js'
        r = self.g('export-workflow', 'r1', '--out', str(out))
        text = out.read_text(encoding='utf-8')
        self.assertIn('Level 1', text); self.assertNotIn('Level 2', text); self.assertIn('wait until', text)
        self.assertNotIn(' quality ', text)
        self.g('defer', 'r1', '--from-level', '1', '--not-before', self.iso(self.later), code=0)
        self.g('export-workflow', 'r1', '--out', str(out), code=3)

    def test_resume_rereads_usage_and_reports(self):
        self.g('defer', 'r1', '--from-level', '2', '--not-before', self.iso(self.later))
        d = json.loads(self.g('resume').stdout)
        self.assertIn('wait until', d['deferred']); self.assertTrue(any('Usage left' in x for x in d['budget']))
        self.g('defer', 'r1', '--from-level', '2', '--not-before', self.iso(time.time() - 5))
        self.assertIn('passed', json.loads(self.g('resume').stdout)['deferred'])


class Report(UsageCase):
    def init(self):
        self.g('init', 'task', '--id', 'r1', '--skip-preflight', 'test', '--skip-context', 'test', '--auto-approve', 'just do it')

    def test_before_after_estimate_vs_actual_and_learning(self):
        t = time.time()
        claude_cache(self.home, (40, t + 3600))
        self.g('node', 'r1', 'a', '--role', 'research', '--status', 'pending', '--goal', 'g')
        self.g('budget', '--run', 'r1', '--host', 'claude-code')
        self.g('node', 'r1', 'a', '--role', 'research', '--status', 'running')
        time.sleep(1.1)
        t2 = time.time()
        self.lines('s1/subagents/agent-aaa111.jsonl', [line('s1', MA, t2, inp=100, out=10, rid='x1', agent='aaa111')])
        claude_cache(self.home, (52, t + 3600))
        self.g('node', 'r1', 'a', '--role', 'research', '--status', 'complete')
        self.g('finish', 'r1', '--status', 'complete')
        text = (self.proj / '.graph/runs/r1/summary.md').read_text(encoding='utf-8')
        self.assertIn('## Usage budget', text); self.assertIn('| 5h | 40% | 52% | statusline cache |', text)
        self.assertIn('| a | build | 80,000 | 110 | -100% |', text); self.assertIn('| total |', text)
        c = [json.loads(x) for x in (self.home / '.config/graph-skill/budget-calib.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual([(x['host'], x['window'], x['pct_before'], x['pct_after']) for x in c], [('claude', '5h', 40, 52)])
        self.assertEqual(c[0]['tokens'], 110)
        self.g('finish', 'r1', '--status', 'complete')
        self.assertEqual(len((self.home / '.config/graph-skill/budget-calib.jsonl').read_text(encoding='utf-8').splitlines()), 1)

    def test_no_learning_when_the_window_refilled(self):
        t = time.time()
        claude_cache(self.home, (40, t + 3600))
        self.g('node', 'r1', 'a', '--role', 'research', '--status', 'pending', '--goal', 'g')
        self.g('budget', '--run', 'r1', '--host', 'claude-code')
        claude_cache(self.home, (3, t + 20000))
        self.g('finish', 'r1', '--status', 'failed')
        self.assertFalse((self.home / '.config/graph-skill/budget-calib.jsonl').exists())


class Statusline(unittest.TestCase):
    def test_extract_keeps_numbers_only(self):
        data = {'session_id': 'S-ID', 'cwd': '/secret/place', 'prompt': SECRET, 'transcript_path': '/x/y.jsonl',
                'model': {'id': 'fake-model-a', 'display_name': 'Fake A'}, 'context_window': {'context_window_size': 200000, 'x': SECRET},
                'rate_limits': {'five_hour': {'used_percentage': 41.5, 'resets_at': 1900000000}, 'seven_day': {'used_percentage': 9, 'resets_at': 1900500000}}}
        out = statusline.extract(data, now=5)
        self.assertEqual(out, {'written_at': 5, 'rate_limits': {'five_hour': {'used_percentage': 41.5, 'resets_at': 1900000000}, 'seven_day': {'used_percentage': 9, 'resets_at': 1900500000}},
                               'model': 'fake-model-a', 'context_window': 200000})
        self.assertEqual(statusline.status_line(data), 'Fake A | 5h 42% | 7d 9%')

    def run_script(self, home, stdin, *args):
        env = {'HOME': str(home), 'USERPROFILE': str(home), 'PATH': '', 'GRAPH_SKILL_NO_PROBES': '1'}
        import os
        env['PATH'] = os.environ.get('PATH', '')
        return subprocess.run([sys.executable, str(CORE / 'statusline.py'), *args], input=stdin, capture_output=True, text=True, encoding='utf-8', env=env)

    def test_script_writes_cache_atomically_and_prints_line(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            r = self.run_script(home, json.dumps({'session_id': 'S-ID', 'prompt': SECRET, 'rate_limits': {'five_hour': {'used_percentage': 20, 'resets_at': 1900000000}}}))
            self.assertEqual(r.returncode, 0); self.assertEqual(r.stdout.strip(), '5h 20%')
            p = home / '.cache/graph-skill/claude-rate-limits.json'
            body = p.read_text(encoding='utf-8')
            self.assertNotIn('S-ID', body); self.assertNotIn(SECRET, body)
            self.assertEqual(json.loads(body)['rate_limits']['five_hour']['used_percentage'], 20)
            self.assertEqual([x.name for x in p.parent.iterdir()], [p.name])  # no temp file left
            # bad input must not crash or break the status line
            r = self.run_script(home, 'not json'); self.assertEqual(r.returncode, 0); self.assertEqual(r.stdout.strip(), 'graph')

    def test_script_passes_through_to_wrapped_command(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            old = Path(tmp) / 'old_status.py'   # a script file: no nested quotes for cmd.exe to mangle
            old.write_text("import sys\nprint('OLD:' + str(len(sys.stdin.read()) > 5))\n", encoding='utf-8')
            wrapped = f'"{sys.executable}" "{old}"'
            r = self.run_script(Path(tmp), json.dumps({'rate_limits': {'five_hour': {'used_percentage': 20, 'resets_at': 1900000000}}}), wrapped)
            self.assertEqual(r.stdout.strip(), 'OLD:True')
            self.assertTrue((Path(tmp) / '.cache/graph-skill/claude-rate-limits.json').exists())
            r = self.run_script(Path(tmp), '{}', 'exit 1')  # wrapped command prints nothing: fall back
            self.assertEqual(r.stdout.strip(), 'graph')


if __name__ == '__main__': unittest.main()
