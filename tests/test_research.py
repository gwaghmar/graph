import os; os.environ.setdefault("GRAPH_SKILL_NO_PROBES", "1")  # never call real CLIs (vercel whoami opens browser sign-in)
import calendar, json, subprocess, sys, time, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_graph import GraphCase, GRAPH  # noqa: E402
import briefhelp  # noqa: E402


def ago(days):
    return time.strftime('%Y-%m-%d', time.gmtime(time.time() - days * 86400))


class Research(GraphCase):
    def start(self, **over):
        self.init()
        a = dict(zip(briefhelp.BRIEF[::2], briefhelp.BRIEF[1::2])); a.update(over)
        flat = [x for k, v in a.items() for x in (k, v)]
        self.g('brief', '--run', 'r1', *flat)

    def src(self, n=1, kind='news', days=5, code=0, *extra):
        return self.g('source', '--run', 'r1', '--url', f'https://example.org/{kind}/{n}/{days}', '--type', kind, '--date', ago(days),
                      '--date-source', 'api', '--title', f'T{n}', *extra, code=code)

    def raw(self, *args, code=0):
        r = subprocess.run([sys.executable, GRAPH, *args], cwd=self.dir, capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(r.returncode, code, f'{args}: {r.stdout}{r.stderr}')
        return r.stdout + r.stderr

    def log(self):
        return [json.loads(l) for l in (Path(self.dir) / '.graph/runs/r1/log.jsonl').read_text(encoding='utf-8').splitlines()]

    # ---- windows per type ----
    def test_window_per_type(self):
        self.start()
        self.src(1, 'news', 29); self.src(2, 'paper', 700)
        r = self.src(3, 'news', 31, code=3)
        self.assertIn('31 days old', r.stderr); self.assertIn('window for "news" is 30', r.stderr)
        self.src(4, 'paper', 731, code=3)
        self.assertEqual([s['id'] for s in self.state()['research']['sources']], ['s1', 's2'])

    def test_unknown_type_refused_and_default_window(self):
        self.start()
        self.assertIn('no window for type "blog"', self.src(1, 'blog', 1, code=3).stderr)

    def test_future_date_and_bad_date_exit_2(self):
        self.start()
        self.g('source', '--run', 'r1', '--url', 'https://a.org/x', '--type', 'news', '--date', ago(-3), '--date-source', 'api', '--title', 't', code=2)
        self.g('source', '--run', 'r1', '--url', 'https://a.org/x', '--type', 'news', '--date', '2026-13-40', '--date-source', 'api', '--title', 't', code=2)
        self.g('source', '--run', 'r1', '--url', 'ftp://a.org/x', '--type', 'news', '--date', ago(1), '--date-source', 'api', '--title', 't', code=2)

    # ---- landmark ----
    def test_landmark_rule(self):
        self.start()
        self.src(1, 'paper', 2000, code=3)
        self.g('source', '--run', 'r1', '--url', 'https://a.org/old', '--type', 'paper', '--date', ago(2000), '--date-source', 'page', '--title', 'Old', '--landmark', code=2)
        self.src(1, 'paper', 2000, 0, '--landmark', '--why', 'founding result')
        s = self.state()['research']['sources'][0]
        self.assertTrue(s['landmark']); self.assertEqual(s['why'], 'founding result')

    def test_landmark_refused_when_brief_says_no(self):
        self.start(**{'--landmark-ok': 'no'})
        r = self.src(1, 'paper', 2000, 3, '--landmark', '--why', 'classic')
        self.assertIn('does not allow landmark', r.stderr)
        self.src(2, 'paper', 5, 3, '--landmark', '--why', 'classic')

    # ---- undated ----
    def test_undated_rejected_unless_brief_allows(self):
        self.start()
        undated = ['--url', 'https://a.org/u', '--type', 'news', '--date-source', 'none', '--title', 'U']
        r = self.g('source', '--run', 'r1', *undated, code=3)
        self.assertIn('no date', r.stderr)
        self.g('source', '--run', 'r1', '--url', 'https://a.org/doc', '--type', 'doc', '--date-source', 'none', '--title', 'Doc')
        s = self.state()['research']['sources'][0]
        self.assertIsNone(s['date']); self.assertEqual(s['date_source'], 'none')
        self.g('source', '--run', 'r1', '--url', 'https://a.org/z', '--type', 'doc', '--date', ago(1), '--date-source', 'none', '--title', 'x', code=2)
        self.g('source', '--run', 'r1', '--url', 'https://a.org/z', '--type', 'doc', '--date-source', 'api', '--title', 'x', code=2)

    def test_duplicate_url_and_secret_query_redacted(self):
        self.start()
        self.g('source', '--run', 'r1', '--url', 'https://user:pw@a.org/p?id=3&api_key=SECRET#frag', '--type', 'doc', '--date-source', 'none', '--title', 'x')
        u = self.state()['research']['sources'][0]['url']
        self.assertNotIn('SECRET', u); self.assertNotIn('pw', u); self.assertIn('id=3', u)
        self.g('source', '--run', 'r1', '--url', u, '--type', 'doc', '--date-source', 'none', '--title', 'x', code=3)

    def test_windows_locked_after_first_source(self):
        self.start()
        self.src(1, 'news', 99, code=3)
        r = self.g('brief', '--run', 'r1', *briefhelp.BRIEF[:4], '--fresh', 'x', '--windows', 'news=365', *briefhelp.BRIEF[8:], code=3)
        self.assertIn('cannot change', r.stderr)
        self.g('brief', '--run', 'r1', *briefhelp.BRIEF)  # same windows: allowed

    # ---- gaps and claims ----
    def test_gap_needs_two_sources_else_weak(self):
        self.start(); self.src(1); self.src(2)
        self.g('gap', '--run', 'r1', '--complaint', 'too slow', '--sources', 's1')
        self.g('gap', '--run', 'r1', '--complaint', 'no export', '--sources', 's1,s2', '--feature', 'Export')
        gaps = self.state()['research']['gaps']
        self.assertTrue(gaps[0]['weak']); self.assertFalse(gaps[1]['weak'])
        self.assertEqual([f['name'] for f in self.state()['features']], ['Export'])
        self.g('gap', '--run', 'r1', '--complaint', 'x', '--sources', 's9', code=3)
        self.g('claim', '--run', 'r1', '--text', 'c', '--sources', 's1,nope', code=3)

    # ---- close ----
    def test_close_needs_five_sources_and_cited_claims(self):
        self.start(); self.node('a', 'pending', '--goal', 'Research the field')
        for i in range(1, 5): self.src(i)
        r = self.g('research', '--run', 'r1', '--close', code=3)
        self.assertIn('Only 4 valid source', r.stderr); self.assertIn('--partial', r.stderr)
        self.src(5)
        self.g('claim', '--run', 'r1', '--text', 'A fact', '--sources', 's1,s2')
        self.g('research', '--run', 'r1', '--close')
        self.assertTrue(self.state()['research']['closed'])
        self.src(6, 'news', 1, 3)  # closed: no more sources

    def test_close_partial_is_shown(self):
        self.start(); self.node('a', 'pending', '--goal', 'Research the field'); self.src(1)
        self.g('research', '--run', 'r1', '--close', '--partial', '', code=2)
        out = self.g('research', '--run', 'r1', '--close', '--partial', 'API was down').stdout
        self.assertIn('PARTIAL', out)
        self.g('finish', 'r1', '--status', 'failed')
        self.assertIn('PARTIAL: API was down', self.g('summary', 'r1').stdout)

    def test_non_research_plan_may_close_with_few_sources(self):
        self.start(); self.g('research', '--run', 'r1', '--close')

    def test_close_without_brief_refused(self):
        self.raw('init', 't', '--id', 'r2', '--size', 'medium', '--skip-preflight', 'x')
        self.assertIn('no field brief', self.raw('research', '--run', 'r2', '--close', code=3))

    # ---- brief gate ----
    def plan_raw(self, rid, size):
        self.raw('init', 't', '--id', rid, '--size', size, '--skip-preflight', 'x', '--skip-context', 'x', '--skip-budget', 'x')
        self.raw('node', rid, 'a', '--role', 'research', '--status', 'pending', '--goal', 'Look it up')

    def test_brief_gate(self):
        self.plan_raw('m1', 'medium')
        r = self.raw('approve', '--run', 'm1', '--note', 'go', code=3)
        self.assertIn('no field brief', r); self.assertIn('👉 Do this', r)
        self.raw('brief', '--run', 'm1', *briefhelp.BRIEF)
        self.raw('approve', '--run', 'm1', '--note', 'go')

    def test_small_is_exempt(self):
        self.plan_raw('s1', 'small')
        self.raw('approve', '--run', 's1', '--note', 'go')

    def test_brief_requires_every_part(self):
        self.init()
        for i in range(0, len(briefhelp.BRIEF), 2):
            args = briefhelp.BRIEF[:i] + briefhelp.BRIEF[i + 2:]
            r = self.g('brief', '--run', 'r1', *args, code=2)
            self.assertIn('Every part', r.stderr)
        self.g('brief', '--run', 'r1', *briefhelp.BRIEF[:-1], '', code=2)
        base = dict(zip(briefhelp.BRIEF[::2], briefhelp.BRIEF[1::2]))
        for bad in ('badwindow', 'news=0', 'news=abc', 'Bad Type=3'):
            flat = [x for k, v in {**base, '--windows': bad}.items() for x in (k, v)]
            self.g('brief', '--run', 'r1', *flat, code=2)

    def test_retry_brief_still_works(self):
        self.init(); self.node('a', 'pending', '--goal', 'g')
        self.assertIn('RETRY BRIEF', self.g('brief', 'r1', 'a').stdout)

    def test_checkpoint_card_cannot_be_dropped(self):
        self.plan_raw('l1', 'large')
        self.raw('brief', '--run', 'l1', *briefhelp.BRIEF)
        sys.path.insert(0, str(Path(GRAPH).parent))
        import checkpoint
        d = json.loads((Path(self.dir) / '.graph/runs/l1/state.json').read_text(encoding='utf-8'))
        items = checkpoint.build_items(d, Path(self.dir) / '.graph/runs/l1')
        card = [i for i in items if i['id'] == 'brief'][0]
        self.assertTrue(card['locked']); self.assertEqual(card['title'], 'test field'); self.assertIn('a human expert', card['why'])
        self.assertEqual(sum(1 for i in items if i['kind'] == 'brief'), 1)

    # ---- report and summary ----
    def test_summary_safety_first_and_sources_table(self):
        self.start(**{'--safety': 'Not medical advice.|Ask a doctor.'})
        self.src(1, 'news', 3, 0, '--level', 'strong')
        self.src(2, 'paper', 2000, 0, '--landmark', '--why', 'founding result')
        self.g('finish', 'r1', '--status', 'failed')
        out = self.g('summary', 'r1').stdout
        self.assertLess(out.index('Not medical advice.'), out.index('Task:'))
        md = self.g('render', 'r1').stdout
        from_state = json.loads((Path(self.dir) / '.graph/runs/r1/state.json').read_text(encoding='utf-8'))
        sys.path.insert(0, str(Path(GRAPH).parent)); import graph as G
        top = G.build_summary(from_state)
        self.assertLess(top.index('Not medical advice.'), top.index('## 👉 Do this:'))
        for text in ('| s1 | news |', 'strong', 'LANDMARK: founding result', 'Who should check this: a human expert', 'Evidence levels: 1 strong, 1 medium, 0 weak',
                     'Evidence standard: two sources per claim', 'Windows: paper 730 days'):
            self.assertIn(text, out)
        html = (Path(self.dir) / '.graph/runs/r1/graph.html').read_text(encoding='utf-8')
        self.assertIn('<table', html); self.assertIn('Not medical advice.', html)

    def test_no_safety_block_without_brief(self):
        self.raw('init', 't', '--id', 'r2', '--size', 'medium', '--skip-preflight', 'x')
        self.assertNotIn('Read first', self.raw('summary', 'r2'))

    # ---- log ----
    def test_every_command_and_refusal_logged(self):
        self.start(); self.src(1); self.src(2, 'news', 99, code=3)
        self.g('gap', '--run', 'r1', '--complaint', 'x', '--sources', 's1'); self.g('claim', '--run', 'r1', '--text', 'c', '--sources', 's1')
        self.g('research', '--run', 'r1', '--close', code=3); self.g('research', '--run', 'r1', '--close', '--partial', 'why')
        rows = self.log()
        cmds = [(e['cmd'], e['outcome']) for e in rows if e['cmd'] in ('brief', 'source', 'gap', 'claim', 'research')]
        self.assertIn(('source', 'refused'), cmds); self.assertIn(('research', 'refused'), cmds)
        for c in ('brief', 'gap', 'claim'): self.assertIn((c, 'ok'), cmds)
        self.assertEqual(sum(1 for c in cmds if c == ('research', 'ok')), 1)
        self.assertIn('field', [k for e in rows if e['cmd'] == 'brief' for k in e['args']])


if __name__ == '__main__':
    unittest.main()


class CanonUrlTests(unittest.TestCase):
    def test_same_page_same_key(self):
        import research
        k = research.canon_url('https://www.Example.com/a/b/?utm_source=x&b=2&a=1#frag')
        for u in ('http://example.com/a/b?a=1&b=2', 'https://example.com/a/b/?a=1&b=2&fbclid=z'): self.assertEqual(research.canon_url(u), k)
        self.assertNotEqual(research.canon_url('https://example.com/a/b?a=2&b=2'), k)
