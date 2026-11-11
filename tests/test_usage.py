import os; os.environ.setdefault("GRAPH_SKILL_NO_PROBES", "1")  # never call real CLIs (vercel whoami opens browser sign-in)
import tempfile as _tf; os.environ.setdefault("GRAPH_SKILL_CONFIG_DIR", _tf.mkdtemp(prefix="graph-perm-"))  # never touch the real ~/.config
import json, re, subprocess, sys, time, unittest
from pathlib import Path

from test_userctx import Base, write, CORE
sys.path.insert(0, str(CORE))
import usage  # noqa: E402

MA, MB, MX = 'fake-model-a-20250101', 'fake-model-b', 'fake-unlisted-model'
FIX = Path(__file__).resolve().parent / 'fixtures-legacy'


def iso(t): return time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(t)) + f'.{int((t % 1) * 1000):03d}Z'


def line(mid, model, ts, inp=0, out=0, cr=0, cw=0, think=0, rid='req', agent=None, content='SECRET-MESSAGE-BODY', sid='s1'):
    o = {'type': 'assistant', 'timestamp': iso(ts), 'sessionId': sid, 'requestId': rid, 'isSidechain': bool(agent),
         'message': {'id': mid, 'model': model, 'content': content,
                     'usage': {'input_tokens': inp, 'output_tokens': out, 'cache_read_input_tokens': cr, 'cache_creation_input_tokens': cw,
                               'output_tokens_details': {'thinking_tokens': think}}}}
    if agent: o['agentId'] = agent
    return json.dumps(o)


class UsageCase(Base):
    def setUp(self):
        super().setUp()
        self.env_extra['CLAUDECODE'] = '1'
        # ids and models in this file are fake; prices go in a fresh cache file in the temp HOME
        self.prices({MA: dict(input=3, output=15, cache_read=0.3, cache_write=3.75), MB: dict(input=1, output=2, cache_read=0, cache_write=0)})
        self.init()
        self.t = time.time()  # after init: created_at is cut to whole seconds, so records at t are never earlier than it

    def init(self):
        self.g('init', 'task', '--id', 'r1', '--skip-preflight', 'test', '--skip-context', 'test', '--skip-budget', 'test', '--auto-approve', 'just do it')

    def prices(self, models, as_of=None):
        write(self.home / '.cache/graph-skill/prices.json', json.dumps({'as_of': as_of or time.strftime('%Y-%m-%d', time.gmtime()), 'models': models}))

    def cdir(self):
        return self.home / '.claude' / 'projects' / re.sub(r'[^A-Za-z0-9]', '-', str(self.proj))

    def lines(self, rel, rows):
        write(self.cdir() / rel, '\n'.join(rows) + '\n')

    def usage(self, *extra):
        return self.g('usage', '--run', 'r1', '--host', 'claude-code', *extra).stdout

    def ustate(self):
        return self.state()['usage']


class Ledger(UsageCase):
    def test_every_session_and_subagent_in_the_window_counts(self):
        """A run with several headless calls: each call is its own session file, with its own subagent files."""
        t = self.t
        self.lines('sA.jsonl', [line('a1', MA, t, inp=100, out=10, rid='ra1', sid='sA'), line('shared', MA, t, inp=1, out=1, rid='rs', sid='sA')])
        self.lines('sB.jsonl', [line('b1', MA, t, inp=200, out=20, rid='rb1', sid='sB'), line('shared', MA, t, inp=1, out=1, rid='rs', sid='sB')])  # resumed copy of one message
        self.lines('sA/subagents/agent-aa1.jsonl', [line('sa1', MB, t, inp=1000, out=100, rid='rsa', agent='aa1', sid='sA')])
        self.lines('sB/subagents/agent-bb1.jsonl', [line('sb1', MB, t, inp=2000, out=200, rid='rsb', agent='bb1', sid='sB')])
        self.lines('sOld.jsonl', [line('o1', MA, t - 3600, inp=99999, out=99999, rid='ro', sid='sOld')])     # before the run window
        other = self.home / '.claude' / 'projects' / 'some-other-project'
        write(other / 'sX.jsonl', line('x1', MA, t, inp=88888, out=88888, rid='rx', sid='sX') + '\n')      # another project
        self.usage()
        u = self.ustate()
        self.assertEqual(u['totals']['input'], 100 + 200 + 1 + 1000 + 2000)    # the shared message once
        self.assertEqual(u['totals']['output'], 10 + 20 + 1 + 100 + 200)
        self.assertEqual(u['sessions'], 2); self.assertEqual(len(u['agents']), 2)
        self.assertIn('Counted 2 host sessions', self.g('usage', '--run', 'r1', '--host', 'claude-code').stdout)

    def test_sessions_from_a_subfolder_count_but_a_similar_sibling_does_not(self):
        t = self.t
        def put(dirname, cwd, mid, inp):
            o = json.loads(line(mid, MA, t, inp=inp, out=1, rid=mid, sid='s' + mid)); o['cwd'] = str(cwd)
            write(self.home / '.claude' / 'projects' / dirname / f's{mid}.jsonl', json.dumps(o) + '\n')
        enc = re.sub(r'[^A-Za-z0-9]', '-', str(self.proj))
        put(enc, self.proj, 'm0', 1)
        put(enc + '-src', self.proj / 'src', 'm1', 10)                       # headless call started in a sub folder
        put(enc + '-2', Path(str(self.proj) + '-2'), 'm2', 1000)             # a different project with a longer name
        put(enc + '-nocwd', self.proj / 'x', 'm3', 100000)
        o = json.loads(line('m4', MA, t, inp=7, out=1, rid='m4', sid='sm4')); o.pop('cwd', None)
        write(self.home / '.claude' / 'projects' / (enc + '-nocwd2') / 'sm4.jsonl', json.dumps(o) + '\n')   # no cwd on the line: not counted
        self.usage()
        self.assertEqual(self.ustate()['totals']['input'], 1 + 10 + 100000)

    def test_dedupe_and_totals(self):
        t = self.t
        self.lines('s1.jsonl', [
            line('m1', MA, t, inp=10, out=5, cr=100, cw=20, rid='r1'),
            line('m1', MA, t + .001, inp=10, out=50, cr=100, cw=20, rid='r1', think=7),  # same message again, final count wins
            line('m2', MA, t + .002, inp=1000, out=500, rid='r2'),
            line(None, MA, t + .003, inp=1, out=1, rid='r3'), line(None, MA, t + .003, inp=1, out=1, rid='r3'),  # no id: dedupe on request id
            line('old', MA, self.t - 3600, inp=9999, out=9999),  # before the run
        ])
        out = self.usage()
        tot = self.ustate()['totals']
        self.assertEqual((tot['input'], tot['output'], tot['cache_read'], tot['cache_write'], tot['reasoning']), (1011, 551, 100, 20, 7))
        want = (1011 * 3 + 551 * 15 + 100 * .3 + 20 * 3.75) / 1e6
        self.assertAlmostEqual(tot['cost'], want, places=6)
        self.assertIn(usage.LABEL, out)
        self.assertIn('| orchestrator |', out)
        self.assertNotIn('SECRET-MESSAGE-BODY', out + json.dumps(self.state()))

    def test_unknown_model_is_tokens_only(self):
        t = self.t
        self.lines('s1.jsonl', [line('m1', MX, t, inp=10, out=5), line('m2', MA, t, inp=1, out=1, rid='b')])
        out = self.usage()
        self.assertIn('n/a', out)
        rows = {r['model']: r for r in self.ustate()['rows']}
        self.assertTrue(rows[MX]['unknown']); self.assertEqual(rows[MX]['input'], 10)
        self.assertFalse(rows[MA]['unknown'])

    def test_model_id_matching(self):
        t = self.t
        # a date suffix on the transcript id still finds the base price, and the shipped id matches exactly
        self.prices({'fake-model-b': dict(input=1, output=2, cache_read=0, cache_write=0)})
        self.lines('s1.jsonl', [line('m1', 'fake-model-b-20260101', t, inp=1000000)])
        self.usage(); self.assertAlmostEqual(self.ustate()['totals']['cost'], 1.0)

    def test_price_age_cache_old_uses_shipped(self):
        old = time.strftime('%Y-%m-%d', time.gmtime(time.time() - 40 * 86400))
        self.prices({MA: dict(input=1, output=1, cache_read=0, cache_write=0)}, as_of=old)
        self.lines('s1.jsonl', [line('m1', MA, time.time(), inp=10)])
        out = self.usage()
        self.assertIn('shipped snapshot', out)
        self.assertEqual(self.ustate()['prices']['from'], 'shipped snapshot')
        self.assertIn('days old', out)
        self.prices({MA: dict(input=1, output=1, cache_read=0, cache_write=0)})
        self.usage(); self.assertEqual(self.ustate()['prices']['from'], 'cache')

    def test_subagent_attribution(self):
        self.g('node', 'r1', 'a', '--role', 'research', '--status', 'running', '--goal', 'g')
        time.sleep(1.1)
        t = time.time()
        self.lines('s1.jsonl', [line('m1', MA, t, inp=10, out=1)])
        self.lines('s1/subagents/agent-aaa111.jsonl', [line('s1', MB, t, inp=100, out=1, rid='x1', agent='aaa111')])
        self.usage()
        w = {(r['who'], r['model']): r for r in self.ustate()['rows']}
        self.assertEqual(w[('a', MB)]['input'], 100)
        self.assertEqual(w[('orchestrator', MA)]['input'], 10)
        self.assertEqual([a['who'] for a in self.ustate()['agents']], ['a'])

    def test_overlapping_nodes_are_shared_and_none_is_unattributed(self):
        for nid in ('a', 'b'): self.g('node', 'r1', nid, '--role', 'research', '--status', 'running', '--goal', 'g')
        time.sleep(1.1)
        t = time.time()
        self.lines('s1/subagents/agent-aaa111.jsonl', [line('s1', MB, t, inp=100, rid='x1', agent='aaa111')])
        self.usage(); self.assertEqual({r['who'] for r in self.ustate()['rows']}, {'shared'})
        for nid in ('a', 'b'): self.g('node', 'r1', nid, '--role', 'research', '--status', 'complete')
        self.lines('s1/subagents/agent-aaa111.jsonl', [line('s1', MB, self.t + .001, inp=100, rid='x1', agent='aaa111')])
        self.usage(); self.assertEqual({r['who'] for r in self.ustate()['rows']}, {'unattributed'})

    def test_finish_adds_summary_and_history(self):
        self.g('node', 'r1', 'a', '--role', 'research', '--status', 'running', '--goal', 'g')
        time.sleep(1.1)
        t = time.time()
        self.lines('s1/subagents/agent-aaa111.jsonl', [line('s1', MA, t, inp=100, out=10, rid='x1', agent='aaa111')])
        self.node('complete')
        self.g('finish', 'r1', '--status', 'complete')
        text = (self.proj / '.graph/runs/r1/summary.md').read_text(encoding='utf-8')
        self.assertIn('## Tokens and cost', text); self.assertIn(usage.LABEL, text)
        h = [json.loads(x) for x in (self.home / '.config/graph-skill/history.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual(len(h), 1)
        self.assertEqual((h[0]['role'], h[0]['tokens']['input'], h[0]['tokens']['output']), ('research', 100, 10))
        self.assertNotIn(str(self.proj), json.dumps(h))
        self.g('finish', 'r1', '--status', 'complete')
        self.assertEqual(len((self.home / '.config/graph-skill/history.jsonl').read_text(encoding='utf-8').splitlines()), 1)

    def test_finish_fails_soft_when_nothing_found(self):
        self.g('finish', 'r1', '--status', 'failed')
        self.assertIn('## Tokens and cost', (self.proj / '.graph/runs/r1/summary.md').read_text(encoding='utf-8'))

    def test_unsupported_host_is_unknown(self):
        out = self.g('usage', '--run', 'r1', '--host', 'opencode').stdout
        self.assertIn('unknown', out); self.assertEqual(self.ustate()['status'], 'unknown')

    def test_usage_is_logged(self):
        self.usage()
        self.assertIn('"cmd": "usage"', self.log())


class CodexAndPrices(UsageCase):
    def test_codex_deltas_and_model(self):
        t = self.t
        rows = [
            {'type': 'session_meta', 'timestamp': iso(t), 'payload': {'id': 'c1', 'cwd': str(self.proj)}},
            {'type': 'turn_context', 'timestamp': iso(t), 'payload': {'model': MA}},
        ]
        def tc(ts, i, c, o, r):
            return {'type': 'event_msg', 'timestamp': iso(ts), 'payload': {'type': 'token_count', 'info': {'total_token_usage': {
                'input_tokens': i, 'cached_input_tokens': c, 'output_tokens': o, 'reasoning_output_tokens': r}}}}
        rows += [tc(t + .001, 1000, 400, 50, 10), tc(t + .002, 1000, 400, 50, 10), tc(t + .003, 3000, 1400, 150, 30)]  # repeat event adds nothing
        write(self.home / '.codex/sessions/2026/11/10/rollout-1.jsonl', '\n'.join(json.dumps(r) for r in rows))
        write(self.home / '.codex/sessions/2026/11/10/rollout-2.jsonl', json.dumps({'type': 'session_meta', 'payload': {'id': 'c2', 'cwd': '/elsewhere'}}))
        self.g('usage', '--run', 'r1', '--host', 'codex')
        tot = self.ustate()['totals']
        self.assertEqual((tot['input'], tot['cache_read'], tot['output'], tot['reasoning']), (1600, 1400, 150, 30))

    def test_prices_refresh_from_local_source(self):
        api = {'anthropic': {'models': {'fake-x': {'cost': {'input': 2, 'output': 4, 'cache_read': 0.2}}, 'no-cost': {}}}, 'other': {'models': {'y': {'cost': {'input': 1, 'output': 1}}}}}
        src = write(self.home / 'api.json', json.dumps(api))
        self.env_extra['GRAPH_PRICES_URL'] = src.as_uri()
        out = self.g('prices', '--refresh', '--run', 'r1').stdout
        self.assertIn('1 models', out)
        c = json.loads((self.home / '.cache/graph-skill/prices.json').read_text(encoding='utf-8'))
        self.assertEqual(list(c['models']), ['fake-x']); self.assertEqual(c['as_of'], time.strftime('%Y-%m-%d', time.gmtime()))
        self.assertIn('"cmd": "prices"', self.log())

    def test_prices_refresh_failure_keeps_old(self):
        self.env_extra['GRAPH_PRICES_URL'] = (self.home / 'missing.json').as_uri()
        r = self.g('prices', '--refresh', code=1)
        self.assertIn('old prices stay', r.stderr)

    def test_shipped_snapshot_shape_and_no_model_names_in_code(self):
        d = json.loads((CORE / 'prices.json').read_text(encoding='utf-8'))
        self.assertTrue(d['as_of'] and d['source_url'] and d['models'])
        for m in d['models'].values(): self.assertEqual(set(m), {'input', 'output', 'cache_read', 'cache_write'})
        names = set(d['models'])
        for f in list(CORE.glob('*.py')) + [CORE.parent / 'SKILL.md', CORE.parent.parent.parent / 'README.md']:
            text = f.read_text(encoding='utf-8')
            for m in names: self.assertNotIn(m, text, f'{m} in {f.name}')


if __name__ == '__main__':
    unittest.main()
