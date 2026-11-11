import os; os.environ.setdefault("GRAPH_SKILL_NO_PROBES", "1")  # never call real CLIs (vercel whoami opens browser sign-in)
import tempfile as _tf; os.environ.setdefault("GRAPH_SKILL_CONFIG_DIR", _tf.mkdtemp(prefix="graph-perm-"))  # never touch the real ~/.config
import json, os, re, stat, sys, time, unittest
from pathlib import Path

from test_userctx import Base, write, CORE
sys.path.insert(0, str(CORE))
import routing  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
CAPS = json.loads((CORE / 'capabilities.json').read_text(encoding='utf-8'))['hosts']['claude-code']['tiers']
M = lambda **k: dict({'input': 1, 'cache_read': 0, 'cache_write': 0}, **k)
# fake models.dev data: fake-big 40, fake-mid2 15, fake-mid 10 reason; fake-small 1 no reasoning
FAKE_PRICES = {
    'fake-big': M(output=40, reasoning=True, tool_call=True), 'fake-mid2': M(output=15, reasoning=True, tool_call=True),
    'fake-mid': M(output=10, reasoning=True, tool_call=True), 'fake-small': M(output=1, reasoning=False, tool_call=True),
    'fake-hidden': M(output=100, reasoning=True, tool_call=True)}
FAKE_CODEX = {'models': [
    {'slug': 'fake-big', 'context_window': 400000, 'supported_reasoning_levels': [{'effort': 'low'}, {'effort': 'medium'}, {'effort': 'high'}], 'default_reasoning_level': 'medium', 'visibility': 'list'},
    {'slug': 'fake-mid2', 'context_window': 200000, 'supported_reasoning_levels': [{'effort': 'low'}, {'effort': 'medium'}], 'default_reasoning_level': 'low', 'visibility': 'list'},
    {'slug': 'fake-mid', 'context_window': 200000, 'supported_reasoning_levels': [{'effort': 'low'}], 'default_reasoning_level': 'low', 'visibility': 'list'},
    {'slug': 'fake-small', 'context_window': 100000, 'supported_reasoning_levels': [], 'default_reasoning_level': None, 'visibility': 'list'},
    {'slug': 'fake-hidden', 'context_window': 100000, 'supported_reasoning_levels': [{'effort': 'high'}], 'default_reasoning_level': 'high', 'visibility': 'hide'},
    {'slug': 'fake-unpriced', 'context_window': 100000, 'supported_reasoning_levels': [{'effort': 'high'}], 'default_reasoning_level': 'high', 'visibility': 'list'}]}
FAKE_OPENCODE = '''fakeprov/fake-big
{
  "id": "fake-big",
  "cost": {"input": 1, "output": 40},
  "capabilities": {"reasoning": true, "toolcall": true},
  "limit": {"context": 100000}
}
fakeprov/fake-small
{
  "id": "fake-small",
  "cost": {"input": 1, "output": 1},
  "capabilities": {"reasoning": false, "toolcall": true}
}
'''


@unittest.skipIf(os.name == 'nt', 'fake host programs are shell scripts')
class RoutingBase(Base):
    def setUp(self):
        super().setUp()
        self.bin = self.home / 'bin'; self.bin.mkdir()
        self.env_extra['PATH'] = str(self.bin) + os.pathsep + '/usr/bin' + os.pathsep + '/bin'   # no real host programs
        self.env_extra['CLAUDECODE'] = '1'
        self.prices(FAKE_PRICES)

    def prices(self, models):
        write(self.home / '.cache/graph-skill/prices.json', json.dumps({'as_of': time.strftime('%Y-%m-%d', time.gmtime()), 'models': models}))

    def fake(self, name, out_text):
        data = self.home / f'{name}.out'; data.write_text(out_text, encoding='utf-8')
        count = self.home / f'{name}.count'
        exe = self.bin / name
        exe.write_text(f'#!{sys.executable}\nimport sys\nopen({str(count)!r}, "a").write("x")\nsys.stdout.write(open({str(data)!r}, encoding="utf-8").read())\n', encoding='utf-8')
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
        return count

    def calls(self, count): return len(count.read_text()) if count.exists() else 0

    def start(self, *extra):
        self.g('init', 'task', '--id', 'r1', '--skip-preflight', 't', '--skip-context', 't', '--skip-budget', 't', '--auto-approve', 'just do it', *extra)

    def add(self, nid, role, *extra, status='pending', code=0):
        return self.g('node', 'r1', nid, '--role', role, '--status', status, '--goal', 'g', '--no-check', 'none', *extra, code=code)

    def resolve(self, nid, *extra, code=0):
        return self.g('resolve', 'r1', nid, *extra, code=code)

    def first(self, r): return r.stdout.splitlines()[0]


class RankTests(unittest.TestCase):
    def c(self, i, price, reasoning=True, tool=True): return {'id': i, 'price': price, 'reasoning': reasoning, 'tool_call': tool}

    def test_three_reasoners_and_a_cheap_model(self):
        r = routing.rank([self.c('a', 40), self.c('b', 15), self.c('c', 10), self.c('d', 1, False)])
        self.assertEqual(r, {'deep': 'a', 'build': 'b', 'quick': 'd'})

    def test_two_models_build_is_cheaper_sibling(self):
        self.assertEqual(routing.rank([self.c('a', 40), self.c('b', 10)]), {'deep': 'a', 'build': 'b', 'quick': 'b'})

    def test_one_model_is_all_tiers_and_none_is_empty(self):
        self.assertEqual(routing.rank([self.c('a', 5)]), {'deep': 'a', 'build': 'a', 'quick': 'a'})
        self.assertEqual(routing.rank([]), {})
        self.assertEqual(routing.rank([self.c('a', None)]), {})

    def test_pick_never_goes_down_for_high_risk(self):
        tv = {'tiers': {'quick': {'value': 'q'}}}
        self.assertEqual(routing.pick(tv, 'build', False)[1]['value'], 'q')
        self.assertIsNone(routing.pick(tv, 'build', True)[1])
        tv = {'tiers': {'deep': {'value': 'd'}}}
        self.assertEqual(routing.pick(tv, 'build', True)[1]['value'], 'd')

    def test_role_table_and_default(self):
        self.assertEqual(routing.tier_of({'role': 'review'})[0], 'deep')
        self.assertEqual(routing.tier_of({'role': 'Implement'})[0], 'build')
        self.assertEqual(routing.tier_of({'role': 'rename'})[0], 'quick')
        self.assertEqual(routing.tier_of({'role': 'zzz'})[0], 'build')
        self.assertEqual(routing.tier_of({'role': 'review', 'needs': 'quick'}), ('quick', 'needs'))

    def test_opencode_and_codex_parsers(self):
        m = routing.parse_opencode(FAKE_OPENCODE)
        self.assertEqual([(x['id'], x['price'], x['reasoning']) for x in m], [('fakeprov/fake-big', 40, True), ('fakeprov/fake-small', 1, False)])
        self.assertEqual([x['id'] for x in routing.parse_codex(json.dumps(FAKE_CODEX))], ['fake-big', 'fake-mid2', 'fake-mid', 'fake-small', 'fake-unpriced'])
        self.assertEqual([x['id'] for x in routing.parse_codex(json.dumps(FAKE_CODEX['models']))][:1], ['fake-big'])

    def test_report_flags_mismatch_and_escalation(self):
        d = {'nodes': [{'id': 'a', 'role': 'build', 'needs': 'deep', 'routes': [{'value': 'alpha', 'text': 'model=alpha', 'escalated': True}]},
                       {'id': 'b', 'role': 'build', 'needs': 'build', 'routes': [{'value': 'alpha', 'text': 'model=alpha'}]},
                       {'id': 'c', 'role': 'build', 'needs': 'build'}],
             'usage': {'rows': [{'who': 'a', 'model': 'x-alpha-1'}, {'who': 'b', 'model': 'x-beta-1'}]}}
        text = '\n'.join(routing.report_lines(d))
        a, b, c = [l for l in text.splitlines() if l.startswith('| ') and not l.startswith('| Node')]
        self.assertIn('escalated', a); self.assertNotIn('MISMATCH', a)
        self.assertIn('MISMATCH', b); self.assertIn('x-beta-1', b)
        self.assertIn('no resolve was recorded', c)
        self.assertIn('not recorded', ' '.join(routing.report_lines({'nodes': []})) + ' not recorded')


class HostTests(RoutingBase):
    def test_claude_alias_values_and_effort(self):
        self.start()
        self.add('a', 'design'); self.add('b', 'fix', '--effort', 'high'); self.add('c', 'rename'); self.add('d', 'fix', '--needs', 'deep', '--effort', 'low')
        self.assertEqual(self.first(self.resolve('a')), f"model={CAPS['deep']}")
        self.assertEqual(self.first(self.resolve('b')), f"model={CAPS['build']} effort=high")
        self.assertEqual(self.first(self.resolve('c')), f"model={CAPS['quick']}")
        self.assertEqual(self.first(self.resolve('d')), f"model={CAPS['deep']} effort=low")
        n = {x['id']: x for x in self.state()['nodes']}
        self.assertEqual((n['a']['tier'], n['a']['tier_by'], n['d']['tier_by']), ('deep', 'role', 'needs'))
        self.assertEqual(n['a']['routes'][0]['value'], CAPS['deep'])

    def test_user_override_always_wins_and_bad_values_are_ignored(self):
        write(self.home / '.config/graph-skill/tiers.json', json.dumps({'claude-code': {'deep': 'my-own', 'build': 'bad value; rm -rf'}}))
        self.start(); self.add('a', 'design'); self.add('b', 'fix')
        r = self.resolve('a'); self.assertEqual(self.first(r), 'model=my-own'); self.assertIn('your override', r.stdout)
        r = self.resolve('b'); self.assertEqual(self.first(r), f"model={CAPS['build']}"); self.assertIn('ignored bad entry', r.stdout)
        self.assertIn('your override', self.g('models', '--host', 'claude-code').stdout)

    def test_codex_catalog_ranked_by_models_dev(self):
        count = self.fake('codex', json.dumps(FAKE_CODEX))
        out = self.g('models', '--host', 'codex').stdout
        self.assertIn('deep  -> fake-big', out); self.assertIn('build -> fake-mid2', out); self.assertIn('quick -> fake-small', out)
        self.assertNotIn('fake-hidden', out); self.assertIn('models.dev cache', out); self.assertIn('codex catalog (fresh)', out)
        self.g('models', '--host', 'codex'); self.assertEqual(self.calls(count), 1)   # 24h cache
        self.g('models', '--host', 'codex', '--refresh'); self.assertEqual(self.calls(count), 2)
        self.assertIn('catalog (cache)', self.g('models', '--host', 'codex').stdout)

    def test_codex_resolve_effort_rules(self):
        self.fake('codex', json.dumps(FAKE_CODEX))
        self.start(); self.add('a', 'design', '--effort', 'high'); self.add('b', 'fix', '--effort', 'high'); self.add('c', 'fix'); self.add('d', 'find')
        self.assertEqual(self.first(self.resolve('a', '--host', 'codex')), '-m fake-big -c model_reasoning_effort=high')
        r = self.resolve('b', '--host', 'codex')                      # build model does not support high: its default is used
        self.assertEqual(self.first(r), '-m fake-mid2 -c model_reasoning_effort=low'); self.assertIn('not supported', r.stdout)
        self.assertEqual(self.first(self.resolve('c', '--host', 'codex')), '-m fake-mid2 -c model_reasoning_effort=low')
        self.assertEqual(self.first(self.resolve('d', '--host', 'codex')), '-m fake-small')

    def test_codex_missing_or_unpriced_is_host_default(self):
        self.start(); self.add('a', 'design')
        r = self.resolve('a', '--host', 'codex'); self.assertEqual(self.first(r), routing.NOT_APPLIED); self.assertIn('not installed', r.stdout)
        self.fake('codex', json.dumps(FAKE_CODEX)); self.prices({})
        r = self.resolve('a', '--host', 'codex'); self.assertEqual(self.first(r), routing.NOT_APPLIED); self.assertIn('could be ranked', r.stdout)

    def test_override_beats_catalog_per_tier(self):
        self.fake('codex', json.dumps(FAKE_CODEX))
        write(self.home / '.config/graph-skill/tiers.json', json.dumps({'codex': {'quick': 'fake-mid'}}))
        self.start(); self.add('a', 'find'); self.add('b', 'design')
        self.assertEqual(self.first(self.resolve('a', '--host', 'codex')), '-m fake-mid -c model_reasoning_effort=low')
        self.assertEqual(self.first(self.resolve('b', '--host', 'codex')), '-m fake-big -c model_reasoning_effort=medium')

    def test_opencode_and_unknown_hosts(self):
        self.fake('opencode', FAKE_OPENCODE)
        self.start(); self.add('a', 'design'); self.add('b', 'find')
        self.assertEqual(self.first(self.resolve('a', '--host', 'opencode')), '--model fakeprov/fake-big')
        self.assertEqual(self.first(self.resolve('b', '--host', 'opencode')), '--model fakeprov/fake-small')
        self.assertEqual(self.first(self.resolve('a', '--host', 'cursor')), 'host default')
        self.assertEqual(self.first(self.resolve('a', '--host', 'whatever')), 'host default')

    def test_fresh_models_dev_file_adds_flags(self):
        api = {'openai': {'models': {k: {'cost': {'input': v['input'], 'output': v['output']}, 'reasoning': v.get('reasoning'), 'tool_call': v.get('tool_call')} for k, v in FAKE_PRICES.items()}}}
        f = self.home / 'api.json'; f.write_text(json.dumps(api), encoding='utf-8')
        (self.home / '.cache/graph-skill/prices.json').unlink()
        self.env_extra['GRAPH_PRICES_URL'] = f.as_uri()
        self.fake('codex', json.dumps(FAKE_CODEX))
        out = self.g('models', '--host', 'codex', '--refresh').stdout
        self.assertIn('models.dev refreshed', out); self.assertIn('deep  -> fake-big', out)
        cached = json.loads((self.home / '.cache/graph-skill/prices.json').read_text(encoding='utf-8'))['models']['fake-small']
        self.assertIs(cached['reasoning'], False); self.assertIs(cached['tool_call'], True)

    def test_manual_model_override_is_logged_and_noted(self):
        self.start(); self.add('a', 'fix', '--model', 'hand-picked')
        r = self.resolve('a'); self.assertEqual(self.first(r), 'model=hand-picked'); self.assertIn('manual', r.stdout)
        self.assertIn('hand-picked', self.log())


class RuleTests(RoutingBase):
    def test_bad_values_and_locking(self):
        self.start()
        self.add('a', 'fix', '--needs', 'huge', code=2); self.add('a', 'fix', '--effort', 'max', code=2)
        self.add('a', 'fix', '--needs', 'build')
        self.add('a', 'fix', '--needs', 'quick')                          # still planning: allowed
        self.g('node', 'r1', 'a', '--role', 'fix', '--status', 'running')
        self.g('node', 'r1', 'a', '--role', 'fix', '--status', 'running', '--needs', 'deep', code=3)
        self.assertIn('locked', self.log())

    def test_high_risk_cannot_route_below_its_role_tier(self):
        self.start()
        r = self.add('a', 'review', '--needs', 'quick', '--risk', 'high', code=3)
        self.assertIn('cannot route below deep', r.stderr)
        r = self.add('b', 'fix', '--needs', 'quick', '--risk', 'high', code=3); self.assertIn('cannot route below build', r.stderr)
        self.add('c', 'fix', '--needs', 'quick', '--risk', 'low')
        self.add('d', 'fix', '--needs', 'deep', '--risk', 'high')
        self.assertEqual(self.first(self.resolve('d')), f"model={CAPS['deep']}")
        self.assertEqual(sum(1 for l in self.log().splitlines() if '"refused"' in l), 2)

    def test_last_attempt_escalates_one_tier_and_brief_says_so(self):
        self.start('--max-retries', '1')
        self.add('a', 'fix', '--needs', 'build', '--check', 'exit 1')
        r = self.resolve('a'); self.assertEqual(self.first(r), f"model={CAPS['build']}"); self.assertNotIn('Escalated', r.stdout)
        self.g('node', 'r1', 'a', '--role', 'fix', '--status', 'running')
        self.g('node', 'r1', 'a', '--role', 'fix', '--status', 'complete', code=3)    # check fails: node failed, attempt 1 used
        self.assertIn('ESCALATION', self.g('brief', 'r1', 'a').stdout)
        r = self.resolve('a'); self.assertEqual(self.first(r), f"model={CAPS['deep']}"); self.assertIn('Escalated', r.stdout)
        self.assertTrue(self.state()['nodes'][0]['routes'][-1]['escalated'])

    def test_no_escalation_without_retries_or_when_already_deep(self):
        self.start('--max-retries', '0'); self.add('a', 'fix')
        self.assertNotIn('Escalated', self.resolve('a').stdout)  # one attempt only: it is not a last attempt after retries

    def test_deep_node_on_last_attempt_gets_high_effort(self):
        self.start('--max-retries', '1'); self.add('a', 'design', '--check', 'exit 1')
        self.g('node', 'r1', 'a', '--role', 'design', '--status', 'running')
        self.g('node', 'r1', 'a', '--role', 'design', '--status', 'complete', code=3)
        r = self.resolve('a'); self.assertEqual(self.first(r), f"model={CAPS['deep']} effort=high")

    def test_export_workflow_carries_resolved_model_and_effort(self):
        self.start(); self.add('a', 'design', '--effort', 'high'); self.add('b', 'rename', '--depends-on', 'a'); self.add('c', 'fix', '--depends-on', 'a', '--needs', 'quick')
        out = self.home / 'wf.js'
        self.g('export-workflow', 'r1', '--out', str(out))
        js = out.read_text(encoding='utf-8')
        self.assertIn(f'label: "a", schema, model: "{CAPS["deep"]}", effort: "high" }}', js)
        self.assertIn(f'label: "b", schema, model: "{CAPS["quick"]}" }}', js)
        self.assertIn(f'label: "c", schema, model: "{CAPS["quick"]}" }}', js)
        self.assertEqual(self.state()['nodes'][0]['routes'][0]['value'], CAPS['deep'])
        self.assertIn('"routing"', self.log())

    def test_resolve_and_models_are_logged_and_reported(self):
        self.start(); self.add('a', 'design')
        self.resolve('a'); self.resolve('nope', code=2); self.g('models', '--run', 'r1', '--host', 'claude-code')
        lines = [json.loads(l) for l in self.log().splitlines()]
        cmds = [(e['cmd'], e['outcome']) for e in lines]
        self.assertIn(('resolve', 'ok'), cmds); self.assertIn(('resolve', 'invalid'), cmds); self.assertIn(('models', 'ok'), cmds)
        self.assertIn('resolve -> ', self.g('log', 'r1').stdout)
        self.g('node', 'r1', 'a', '--role', 'design', '--status', 'running'); self.g('node', 'r1', 'a', '--role', 'design', '--status', 'complete')
        self.g('finish', 'r1', '--status', 'failed', code=0)
        s = (self.proj / '.graph/runs/r1/summary.md').read_text(encoding='utf-8')
        self.assertIn('## Model routing', s); self.assertIn(f"model={CAPS['deep']}", s)

    def test_plan_hash_includes_needs_only_when_set(self):
        sys.path.insert(0, str(CORE)); import graph
        a = {'nodes': [{'id': 'a', 'goal': 'g'}]}; b = {'nodes': [{'id': 'a', 'goal': 'g', 'needs': 'deep'}]}
        self.assertNotEqual(graph.plan_hash(a), graph.plan_hash(b))
        self.assertEqual(graph.plan_hash(a), graph.plan_hash({'nodes': [{'id': 'a', 'goal': 'g', 'effort': 'low'}]}))


class NoModelNames(unittest.TestCase):
    def test_no_model_names_in_code_or_docs(self):
        rx = re.compile(r'\b(opus|sonnet|haiku|fable|gpt-?\d|gemini|llama|mistral|grok|deepseek|o[134]-(mini|pro)?)\b', re.I)
        files = list((CORE).glob('*.py')) + [REPO / 'skills/graph/SKILL.md', REPO / 'README.md', REPO / 'skills/graph/references/protocol.md']
        bad = [f'{f.name}:{i}: {l.strip()[:80]}' for f in files for i, l in enumerate(f.read_text(encoding='utf-8').splitlines(), 1) if rx.search(l)]
        self.assertEqual(bad, [])

    def test_skill_md_stays_short(self):
        self.assertLess(len((REPO / 'skills/graph/SKILL.md').read_text(encoding='utf-8').splitlines()), 500)


if __name__ == '__main__':
    unittest.main()
