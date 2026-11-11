import json, os, subprocess, sys, unittest, urllib.error, urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_graph import GraphCase, GRAPH  # noqa: E402

CORE = Path(GRAPH).parent
sys.path.insert(0, str(CORE))
import checkpoint  # noqa: E402


class CP(GraphCase):
    def start(self, size='medium', *extra):
        self.g('init', 'Build the thing', '--id', 'r1', '--size', size, '--skip-preflight', 't', '--skip-context', 't', '--skip-budget', 't', *extra)

    def plan(self):
        self.node('a', 'pending', '--goal', 'Write the login page')
        self.node('b', 'pending', '--goal', 'Add tests for login', '--depends-on', 'a')
        self.node('c', 'pending', '--goal', 'Polish the footer')

    def path(self, name): return Path(self.dir) / '.graph/runs/r1' / name

    def edit(self, fn):
        p = self.path('state.json'); d = json.loads(p.read_text(encoding='utf-8')); fn(d); p.write_text(json.dumps(d), encoding='utf-8')

    def add_pick(self):
        def f(d):
            d['outside'] = {'parts': ['ui'], 'none': {}, 'picks': [{'part': 'ui', 'use': 'acme/ui-kit', 'kind': 'reference', 'verdict': 'clean', 'status': 'ready'}]}
        self.edit(f)

    def log(self): return [json.loads(l) for l in self.path('log.jsonl').read_text(encoding='utf-8').splitlines()]

    def popen(self, *args, stdin=False, env=None):
        e = dict(os.environ, **(env or {}))
        return subprocess.Popen([sys.executable, GRAPH, 'checkpoint', '--run', 'r1', *args], cwd=self.dir, text=True, encoding='utf-8', env=e,
                                stdin=subprocess.PIPE if stdin else None, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def reap(self, p):
        if p.poll() is None: p.kill()
        p.wait()
        for s in (p.stdout, p.stderr):
            if s: s.close()

    def serve(self):
        p = self.popen('--serve', '--no-open'); self.addCleanup(self.reap, p)
        line = p.stdout.readline()
        self.assertIn('http://127.0.0.1:', line)
        url = line.split('Open this page: ')[1].strip()
        return p, url

    def post(self, url, drops, token_ok=True):
        u = url if token_ok else url.split('?t=')[0] + '?t=wrong'
        u = u.replace('/?t=', '/go?t=')
        req = urllib.request.Request(u, data=json.dumps({'dropped': drops}).encode(), method='POST', headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=10) as r: return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e: return e.code, json.loads(e.read() or b'{}')


class PageTests(CP):
    def test_page_content_and_style(self):
        self.start(); self.plan(); self.add_pick()
        self.g('feature', '--run', 'r1', '--name', 'Total price upfront', '--why', 'top complaint')
        out = self.g('checkpoint', '--run', 'r1').stdout
        self.assertIn('Write the login page', out); self.assertIn('acme/ui-kit', out); self.assertIn('$0 / unknown', out)
        h = self.path('checkpoint.html').read_text(encoding='utf-8')
        for want in ("Tap what you DON'T need.", 'press GO', 'role="switch"', 'Write the login page', 'acme/ui-kit', 'Total price upfront', 'prefers-color-scheme:dark', 'viewport', 'safe', '$0 / unknown'):
            self.assertIn(want, h)
        self.assertNotIn('https://', h); self.assertNotIn('http://', h); self.assertNotIn('<link', h)
        self.assertNotIn('src=', h)

    def test_html_escapes_text(self):
        self.start(); self.node('a', 'pending', '--goal', '<script>alert(1)</script> x')
        self.g('checkpoint', '--run', 'r1')
        h = self.path('checkpoint.html').read_text(encoding='utf-8')
        self.assertNotIn('<script>alert', h); self.assertIn('&lt;script&gt;', h)

    def test_size_shown_and_logged(self):
        self.start('large'); self.plan(); self.g('checkpoint', '--run', 'r1')
        self.assertIn('large job', self.path('checkpoint.html').read_text(encoding='utf-8'))
        self.assertIn('checkpoint', [e['cmd'] for e in self.log()])


class ServeTests(CP):
    def test_token_and_post_flow(self):
        self.start(); self.plan(); self.add_pick()
        p, url = self.serve()
        base = url.split('?t=')[0]
        with self.assertRaises(urllib.error.HTTPError) as cm: urllib.request.urlopen(base, timeout=10)
        self.assertEqual(cm.exception.code, 403)
        self.assertEqual(self.post(url, [], token_ok=False)[0], 403)
        with urllib.request.urlopen(url, timeout=10) as r: self.assertIn('GO', r.read().decode())
        code, body = self.post(url, ['node:a'])                      # b needs a: refused, server stays up
        self.assertEqual(code, 409); self.assertIn('kept step b needs dropped step a', body['error'])
        self.assertEqual(self.post(url, ['node:zzz'])[0], 409)
        self.assertNotIn('approval', self.state())
        self.assertEqual(len(self.state()['nodes']), 3)
        code, body = self.post(url, ['node:c', 'pick:ui'])
        self.assertEqual(code, 200)
        p.wait(timeout=10); self.assertEqual(p.returncode, 0)
        d = self.state()
        self.assertEqual([n['id'] for n in d['nodes']], ['a', 'b'])
        self.assertEqual(d['approval']['source'], 'checkpoint'); self.assertEqual(d['approval']['drops'], ['node:c', 'pick:ui'])
        self.assertEqual(d['outside']['picks'], []); self.assertIn('ui', d['outside']['none'])
        msgs = [e.get('message', '') for e in self.log() if e['cmd'] == 'checkpoint' and e['outcome'] == 'refused']
        self.assertTrue(any('token' in m for m in msgs)); self.assertTrue(any('needs dropped step' in m for m in msgs))
        self.assertTrue(self.log()[-1]['outcome'] == 'ok')
        self.node('a', 'running')                                     # approval is real: nodes can start

    def test_started_node_cannot_be_dropped(self):
        self.start(); self.plan(); self.g('approve', '--run', 'r1', '--note', 'go'); self.node('c', 'running')
        p, url = self.serve()
        code, body = self.post(url, ['node:c'])
        self.assertEqual(code, 409); self.assertIn('already started', body['error'])
        self.assertEqual(self.post(url, [])[0], 200); p.wait(timeout=10)

    def test_times_out(self):
        self.start(); self.plan()
        p = self.popen('--serve', '--no-open', '--timeout', '1')
        out, err = p.communicate(timeout=20)
        self.assertEqual(p.returncode, 1); self.assertIn('timed out', err); self.assertNotIn('approval', self.state())


class TerminalTests(CP):
    def test_parse(self):
        self.assertEqual(checkpoint.parse_terminal('go', 3), set())
        self.assertEqual(checkpoint.parse_terminal(' Drop 2, 4 ', 4), {2, 4})
        self.assertEqual(checkpoint.parse_terminal('drop 1 3', 3), {1, 3})
        for bad in ('', 'drop', 'drop x', 'drop 9', 'drop 0', 'yes'):
            with self.assertRaises(ValueError): checkpoint.parse_terminal(bad, 4)

    def test_no_color_and_not_tty(self):
        self.start(); self.plan()
        os.environ['NO_COLOR'] = '1'
        try: self.assertFalse(checkpoint.color_on())
        finally: del os.environ['NO_COLOR']
        m = {'size': 'medium', 'task': 't', 'cost': '$0 / unknown', 'security': 's', 'hidden': 0,
             'items': [{'id': 'node:a', 'part': 'Step', 'title': 'x', 'why': 'w', 'badge': ('safe', 'ok'), 'more': 2}]}
        self.assertNotIn('\x1b', checkpoint.render_terminal(m, color=False))
        self.assertIn('\x1b[', checkpoint.render_terminal(m, color=True)); self.assertIn('+2 more', checkpoint.render_terminal(m, color=False))

    def test_terminal_flow(self):
        self.start(); self.plan()
        p = self.popen('--terminal', stdin=True, env={'NO_COLOR': '1'})
        out, err = p.communicate('drop 9\nnope\ndrop 3\n', timeout=20)
        self.assertEqual(p.returncode, 0, err)
        self.assertNotIn('\x1b', out); self.assertIn("Type what you DON'T need", out); self.assertIn('1. KEEP', out); self.assertIn('No item 9', out)
        d = self.state()
        self.assertEqual([n['id'] for n in d['nodes']], ['a', 'b']); self.assertEqual(d['approval']['source'], 'checkpoint')

    def test_terminal_dependency_refusal_and_eof(self):
        self.start(); self.plan()
        p = self.popen('--terminal', stdin=True, env={'NO_COLOR': '1'})
        out, err = p.communicate('drop 1\n', timeout=20)
        self.assertEqual(p.returncode, 3); self.assertIn('kept step b needs dropped step a', err)
        self.assertNotIn('approval', self.state()); self.assertEqual(len(self.state()['nodes']), 3)
        p = self.popen('--terminal', stdin=True)
        p.communicate('', timeout=20); self.assertEqual(p.returncode, 1); self.assertNotIn('approval', self.state())
        self.assertTrue(any(e['outcome'] == 'refused' for e in self.log() if e['cmd'] == 'checkpoint'))


class SizeTests(CP):
    def test_small_needs_only_preflight_and_approve(self):
        self.g('init', 'tiny', '--id', 'r1', '--size', 'small', '--skip-preflight', 't')
        self.node('a', 'pending', '--goal', 'one line fix', '--no-check', 'none')
        r = self.node('a', 'running', code=3)
        self.assertIn('approve', r.stderr); self.assertNotIn('profile', r.stderr); self.assertNotIn('budget', r.stderr); self.assertNotIn('choose', r.stderr)
        self.g('approve', '--run', 'r1', '--note', 'ok')
        self.node('a', 'running')
        self.assertEqual(self.state()['approval']['source'], 'model')

    def test_small_still_needs_preflight(self):
        self.g('init', 'tiny', '--id', 'r1', '--size', 'small')
        self.node('a', 'pending', '--goal', 'g')
        self.assertIn('preflight', self.node('a', 'running', code=3).stderr)

    def test_size_is_required(self):
        r = subprocess.run([sys.executable, GRAPH, 'init', 't', '--id', 'r9'], cwd=self.dir, capture_output=True, text=True)
        self.assertEqual(r.returncode, 2); self.assertIn('--size', r.stderr)

    def test_medium_keeps_current_gates_and_main_parts(self):
        self.g('init', 'feat', '--id', 'r1', '--size', 'medium', '--skip-preflight', 't')
        self.node('a', 'pending', '--goal', 'g')
        err = self.node('a', 'running', code=3).stderr
        self.assertIn('profile', err); self.assertIn('budget', err)
        self.g('parts', '--run', 'r1', '--set', 'design,deploy', '--main', 'design')
        self.g('parts', '--run', 'r1', '--set', 'design', '--main', 'deploy', code=2)
        self.g('parts', '--run', 'r1', '--set', 'design,deploy', '--main', 'design')
        self.g('context', '--run', 'r1'); self.g('profile', '--run', 'r1', '--text', 'No user rules found [none]')
        self.g('choose', '--run', 'r1', '--why', 'none needed'); self.g('budget', '--run', 'r1', '--left-pct', '50', '--resets', '23:59')
        out = self.g('approve', '--run', 'r1', '--note', 'go', code=3).stderr
        self.assertIn('design:', out); self.assertNotIn('deploy:', out)
        self.g('part-none', '--run', 'r1', '--part', 'design', '--note', 'nothing fits')
        self.g('approve', '--run', 'r1', '--note', 'go')

    def test_large_needs_every_part_and_checkpoint(self):
        self.start('large'); self.plan()
        self.g('approve', '--run', 'r1', '--note', 'go', code=3)
        self.g('parts', '--run', 'r1', '--set', 'design,deploy')
        self.g('part-none', '--run', 'r1', '--part', 'design', '--note', 'n')
        out = self.g('approve', '--run', 'r1', '--note', 'go', code=3).stderr; self.assertIn('deploy:', out)
        self.g('part-none', '--run', 'r1', '--part', 'deploy', '--note', 'n')
        self.assertIn('checkpoint', self.g('approve', '--run', 'r1', '--note', 'go', code=3).stderr)
        self.g('checkpoint', '--run', 'r1'); self.g('approve', '--run', 'r1', '--note', 'go')

    def test_size_change_is_logged_and_rechecks(self):
        self.start('small'); self.plan()
        self.g('approve', '--run', 'r1', '--note', 'go'); self.node('c', 'running'); self.node('c', 'complete')
        out = self.g('size', '--run', 'r1', '--to', 'large').stdout
        self.assertIn('small -> large', out); self.assertIn('Do this', out); self.assertIn('parts', out)
        self.assertEqual(self.node('a', 'running', code=3).stderr.count('STOPPED'), 1)
        self.assertEqual(self.state()['size'], 'large')
        e = [x for x in self.log() if x['cmd'] == 'size'][-1]; self.assertEqual(e['args']['size'], 'large')


class StyleTests(CP):
    def test_summary_top_and_approval_source(self):
        self.start(); self.plan()
        self.g('checkpoint', '--run', 'r1')
        p = self.popen('--terminal', stdin=True, env={'NO_COLOR': '1'}); p.communicate('go\n', timeout=20)
        for n in 'abc': self.ran(n)
        self.g('finish', 'r1', '--status', 'complete')
        s = self.path('summary.md').read_text(encoding='utf-8')
        self.assertTrue(s.startswith('# '))
        self.assertLess(s.index('Do this'), s.index('\n---\n')); self.assertLess(s.index('\n---\n'), s.index('## Run summary'))
        self.assertIn('pressed GO at the checkpoint', s)
        self.assertLessEqual(len([l for l in s.split('\n---\n')[0].splitlines() if l[:2] in ('1.', '2.', '3.', '4.')]), 3)

    def test_model_approval_is_marked(self):
        self.start(); self.plan(); self.g('approve', '--run', 'r1', '--note', 'go')
        for n in 'abc': self.ran(n)
        self.g('finish', 'r1', '--status', 'failed')
        self.assertIn('recorded by the model', self.path('summary.md').read_text(encoding='utf-8'))

    def test_refusal_has_heading_and_do_this(self):
        self.g('init', 't', '--id', 'r1', '--size', 'medium', '--skip-preflight', 't')
        self.node('a', 'pending', '--goal', 'g')
        err = self.node('a', 'running', code=3).stderr
        self.assertTrue(err.startswith('✖ STOPPED')); self.assertIn('👉 Do this', err)
        self.assertNotIn(str(Path.home()) + '/', err)


if __name__ == '__main__':
    unittest.main()
