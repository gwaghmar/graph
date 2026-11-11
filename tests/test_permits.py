import os; os.environ.setdefault("GRAPH_SKILL_NO_PROBES", "1")  # never call real CLIs (vercel whoami opens browser sign-in)
import tempfile as _tf; os.environ.setdefault("GRAPH_SKILL_CONFIG_DIR", _tf.mkdtemp(prefix="graph-perm-"))  # never touch the real ~/.config
import contextlib, io, json, subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

CORE = Path(__file__).resolve().parent.parent / 'skills' / 'graph' / 'scripts'
sys.path.insert(0, str(CORE))
import graph, permits, preflight  # noqa: E402

GRAPH = str(CORE / 'graph.py')


class CliCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(); self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name); self.cfg = self.dir / 'cfg'
        self.env = dict(os.environ, GRAPH_SKILL_CONFIG_DIR=str(self.cfg), GRAPH_SKILL_NO_PROBES='1')

    def g(self, *args, code=0):
        r = subprocess.run([sys.executable, GRAPH, *args], cwd=self.dir, capture_output=True, text=True, encoding='utf-8', env=self.env)
        self.assertEqual(r.returncode, code, f'{args}: {r.stdout}{r.stderr}')
        return r.stdout + r.stderr

    def run_id(self):
        self.g('init', 'task', '--id', 'r1', '--size', 'small', '--skip-preflight', 'x', '--skip-context', 'x', '--skip-budget', 'x')
        return 'r1'


class PermitCommands(CliCase):
    def test_record_list_forget(self):
        self.g('permit', '--key', 'login:vercel', '--skip', '--note', 'no thanks')
        self.g('permit', '--key', 'spend:acme', '--allow', '--note', 'ok up to $5')
        out = self.g('permits')
        self.assertIn('login:vercel', out); self.assertIn('skip', out); self.assertIn('no thanks', out); self.assertIn('allowed', out)
        data = json.loads((self.cfg / 'permissions.json').read_text(encoding='utf-8'))
        self.assertEqual(data['spend:acme']['decision'], 'allowed'); self.assertEqual(data['spend:acme']['note'], 'ok up to $5'); self.assertIn('at', data['spend:acme'])
        self.g('permit', '--key', 'login:vercel', '--deny', '--note', 'never')
        self.assertEqual(json.loads(self.g('permits', '--json'))['login:vercel']['decision'], 'denied')
        self.g('permit', '--key', 'login:vercel', '--forget')
        self.assertNotIn('login:vercel', self.g('permits'))

    def test_needs_the_users_words_and_one_mode(self):
        self.g('permit', '--key', 'login:gh', '--allow', code=2)
        self.g('permit', '--key', 'login:gh', '--allow', '--deny', '--note', 'x', code=2)
        self.g('permit', '--key', 'Bad Key!', '--allow', '--note', 'x', code=2)
        self.assertFalse((self.cfg / 'permissions.json').exists())

    def test_check_asks_once_then_remembers_and_logs(self):
        rid = self.run_id()
        out = self.g('permit', '--run', rid, '--key', 'plugin-on:vercel', '--check', code=3)
        self.assertEqual(sum('Ask the user once' in l for l in out.splitlines()), 1)
        self.assertIn('permit --key plugin-on:vercel --allow|--deny|--skip --note', out)
        self.g('permit', '--run', rid, '--key', 'plugin-on:vercel', '--allow', '--note', 'yes')
        self.assertIn('allowed', self.g('permit', '--run', rid, '--key', 'plugin-on:vercel', '--check'))
        self.g('permit', '--run', rid, '--key', 'spend:p', '--skip', '--note', 'free only')
        self.assertIn('remembered: skip', self.g('permit', '--run', rid, '--key', 'spend:p', '--check'))
        log = [json.loads(l) for l in (self.dir / '.graph/runs/r1/log.jsonl').read_text(encoding='utf-8').splitlines()]
        outcomes = [(e['cmd'], e['outcome']) for e in log]
        self.assertIn(('permit', 'refused'), outcomes)
        self.assertTrue(any('remembered: allowed' in ' '.join(e.get('permits', [])) for e in log))
        self.assertTrue(any('remembered: skip' in ' '.join(e.get('permits', [])) for e in log))

    def test_permissions_never_approve_posts(self):
        for key in ('post:x', 'posts:twitter', 'grow:post', 'publish:blog', 'spend:post', 'reply:reddit', 'growth:approve'):
            out = self.g('permit', '--key', key, '--allow', '--note', 'yes', code=3)
            self.assertIn('own tap', out)
        self.assertFalse((self.cfg / 'permissions.json').exists())
        # a stored permission, even written by hand, is never read by the growth code
        src = (CORE / 'growth.py').read_text(encoding='utf-8')
        self.assertNotIn('permits', src)
        self.assertNotIn('permissions.json', src)

    def test_wrong_file_content_is_ignored(self):
        self.cfg.mkdir(); (self.cfg / 'permissions.json').write_text('{"a": {"decision": "yes please"}, "b": 5}', encoding='utf-8')
        self.assertEqual(self.g('permits').strip(), 'No permissions recorded.')


class LoginGate(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(); self.addCleanup(self._tmp.cleanup)
        p = mock.patch.dict(os.environ, {'GRAPH_SKILL_CONFIG_DIR': self._tmp.name}); p.start(); self.addCleanup(p.stop)
        self.r = {'clis': {'gh': {'present': True}, 'vercel': {'present': True}, 'npx': {'present': True}},
                  'access': {'gh': 'ok', 'vercel': 'no'}, 'mcp': [], 'verify': {'headless_browser': 'no'},
                  'os': 'x', 'arch': 'y', 'shell': 'z', 'host': 'h', 'skills': [], 'context': []}

    def resolve(self):
        q, n = [], []
        out = preflight.resolve(preflight.load_jobs(), ['deploy'], self.r, q, n)
        return out, q, n

    def test_unknown_login_gives_one_question(self):
        out, q, n = self.resolve()
        self.assertEqual(len(q), 1)
        self.assertIn('\U0001f511 Vercel is not logged in. Ask the user once: log in (run `vercel login` yourself) or skip? Then: permit --key login:vercel --allow|--skip --note', q[0])
        self.assertIsNone(out['deploy']['use'])

    def test_allowed_skip_and_denied_do_not_ask(self):
        permits.record('login:vercel', 'allowed', 'yes log in')
        out, q, n = self.resolve()
        self.assertEqual(q, []); self.assertTrue(n[0].startswith('remembered: allowed (')); self.assertIn('vercel login', out['deploy']['workaround'])
        for d in ('skip', 'denied'):
            permits.record('login:vercel', d, 'no')
            out, q, n = self.resolve()
            self.assertEqual(q, []); self.assertIn(f'remembered: {d}', n[0]); self.assertIn('Use another option', out['deploy']['workaround'])

    def test_preflight_command_exits_3_with_the_question_then_not_again(self):
        def go():
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(sys, 'argv', ['graph.py', 'preflight', '--needs', 'deploy']), mock.patch.object(preflight, 'scan', return_value=self.r), \
                 contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                graph.CTX.update(msg=None, checks=[], run=None, preflight=None, route=None, permits=[])
                try: graph.main(); return 0, out.getvalue() + err.getvalue()
                except SystemExit as e: return e.code, out.getvalue() + err.getvalue()
        code, text = go()
        self.assertEqual(code, 3); self.assertIn('Ask the user once', text); self.assertIn('permit --key login:vercel', text)
        permits.record('login:vercel', 'skip', 'skip it')
        code, text = go()
        self.assertEqual(code, 0); self.assertNotIn('Ask the user once', text)


class QuietEnv(unittest.TestCase):
    def test_every_subprocess_gets_ci_and_a_noop_browser(self):
        env = permits.quiet_env({'PATH': '/bin'})
        self.assertEqual(env['CI'], '1'); self.assertEqual(env['BROWSER'], '' if os.name == 'nt' else (permits.noop_browser()))
        if os.name != 'nt': self.assertTrue(Path(env['BROWSER']).name == 'true')
        proc = mock.MagicMock(); proc.communicate.return_value = ('', ''); proc.returncode = 0
        with mock.patch('preflight.subprocess.Popen', return_value=proc) as po:
            preflight.run_proc(['gh', 'auth', 'status'], 2)
            preflight.run_proc('echo hi', 2, shell=True, env={'PATH': '/bin', 'CI': '0', 'BROWSER': 'firefox'})
        for call in po.call_args_list:
            kw = call.kwargs
            self.assertEqual(kw['stdin'], subprocess.DEVNULL)
            self.assertEqual(kw['env']['CI'], '1'); self.assertEqual(kw['env']['BROWSER'], permits.noop_browser())


if __name__ == '__main__':
    unittest.main()
