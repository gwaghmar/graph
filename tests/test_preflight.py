import json, subprocess, sys, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

CORE = Path(__file__).resolve().parent.parent / 'skills' / 'graph' / 'scripts'
sys.path.insert(0, str(CORE))
import preflight  # noqa: E402

AUTH_WORDS = ('whoami', 'auth', 'projects')


def fake_run(cmd, **kw):
    out = 'alice-account\n' if any(w in cmd for w in AUTH_WORDS) else 'v1.2.3\n'
    return SimpleNamespace(returncode=0, stdout=out if kw.get('stdout') == subprocess.PIPE else None, stderr=None)


class PreflightCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cwd = Path(self._tmp.name) / 'proj'
        self.home = Path(self._tmp.name) / 'home'
        self.cwd.mkdir(); self.home.mkdir()

    def scan(self, **kw):
        return preflight.scan(self.cwd, self.home, kw.pop('env', {}), **kw)

    def with_tools(self, which=lambda n: '/bin/' + n):
        return mock.patch.multiple('preflight.shutil', which=mock.Mock(side_effect=which)), \
               mock.patch('preflight.subprocess.run', side_effect=fake_run)

    def test_privacy_no_account_names_by_default(self):
        w, r = self.with_tools()
        with w, r as run:
            res = self.scan()
        self.assertNotIn('alice', json.dumps(res))
        self.assertNotIn('accounts', res)
        self.assertNotIn('alice', (self.cwd / '.graph' / 'preflight.json').read_text(encoding='utf-8'))
        auth = [c for c in run.call_args_list if any(x in c.args[0] for x in AUTH_WORDS)]
        self.assertTrue(auth)
        for c in auth:
            self.assertEqual(c.kwargs['stdout'], subprocess.DEVNULL)
            self.assertEqual(c.kwargs['stderr'], subprocess.DEVNULL)

    def test_account_names_only_with_flag(self):
        w, r = self.with_tools()
        with w, r:
            res = self.scan(with_accounts=True)
        self.assertEqual(res['accounts']['gh'], 'alice-account')

    def test_fail_soft_when_everything_breaks(self):
        with mock.patch('preflight.shutil.which', side_effect=OSError('boom')), \
             mock.patch('preflight.subprocess.run', side_effect=subprocess.TimeoutExpired('x', 2)):
            res = self.scan(env={'CLAUDECODE': '1'})
        self.assertEqual(res['access'], {'gh': 'unknown', 'vercel': 'unknown', 'supabase': 'unknown'})
        self.assertIsNone(res['clis']['git']['present'])
        self.assertEqual(res['verify']['ios_simulator'], 'unknown')
        self.assertIn('env:', preflight.summary(res))

    def test_timeout_on_auth_is_unknown(self):
        def run(cmd, **kw):
            if 'whoami' in cmd: raise subprocess.TimeoutExpired(cmd, 2)
            return fake_run(cmd, **kw)
        with mock.patch('preflight.shutil.which', side_effect=lambda n: '/bin/' + n), \
             mock.patch('preflight.subprocess.run', side_effect=run):
            res = self.scan()
        self.assertEqual(res['access']['vercel'], 'unknown')
        self.assertEqual(res['access']['gh'], 'ok')

    def test_cache_and_refresh(self):
        w, r = self.with_tools()
        with w, r as run:
            self.scan(); first = run.call_count
            self.scan(); self.assertEqual(run.call_count, first)
            self.scan(refresh=True); self.assertEqual(run.call_count, first * 2)

    def test_mcp_and_skill_names_only(self):
        (self.cwd / '.mcp.json').write_text(json.dumps(
            {'mcpServers': {'github': {'env': {'TOKEN': 'sekret-value'}}}}), encoding='utf-8')
        (self.home / '.claude.json').write_text(json.dumps(
            {'mcpServers': {'g1': {}}, 'projects': {str(self.cwd): {'mcpServers': {'p1': {'env': {'K': 'sekret-value'}}}}}}),
            encoding='utf-8')
        (self.home / '.codex').mkdir()
        (self.home / '.codex' / 'config.toml').write_text(
            '[mcp_servers.codexsrv]\ncommand="x"\n[mcp_servers.codexsrv.env]\nK="sekret-value"\n', encoding='utf-8')
        (self.home / '.claude' / 'skills' / 'my-skill').mkdir(parents=True)
        (self.cwd / '.cursor' / 'rules').mkdir(parents=True)
        (self.cwd / '.cursor' / 'rules' / 'a.mdc').write_text('---\nalwaysApply: true\n---\nbody secret', encoding='utf-8')
        (self.cwd / 'CLAUDE.md').write_text('private', encoding='utf-8')
        with mock.patch('preflight.shutil.which', return_value=None), \
             mock.patch('preflight.subprocess.run', side_effect=fake_run):
            res = self.scan()
        self.assertEqual(res['mcp'], ['codexsrv', 'g1', 'github', 'p1'])
        self.assertEqual(res['skills'], ['my-skill'])
        paths = {c['path']: c.get('alwaysApply') for c in res['context']}
        self.assertTrue(paths['.cursor/rules/a.mdc'])
        self.assertIn('./CLAUDE.md', paths)
        self.assertNotIn('sekret', json.dumps(res))
        self.assertNotIn('private', json.dumps(res))

    def base(self, **over):
        r = {'clis': {'gh': {'present': True}, 'vercel': {'present': True}, 'npx': {'present': True}},
             'access': {'gh': 'ok', 'vercel': 'no'}, 'mcp': ['vercel-mcp'], 'verify': {'headless_browser': 'no'}}
        r.update(over)
        return r

    def test_job_resolution(self):
        jobs = preflight.load_jobs()
        out = preflight.resolve(jobs, ['github', 'deploy', 'phone', 'database'], self.base())
        self.assertEqual(out['github']['use'], 'cli:gh')
        self.assertEqual(out['deploy']['use'], 'mcp:vercel')  # CLI refused, MCP present
        self.assertIsNone(out['phone']['use'])
        self.assertIsNone(out['database']['use'])
        r = self.base(mcp=[])
        out = preflight.resolve(jobs, ['deploy'], r)
        self.assertIn('vercel login', out['deploy']['workaround'])
        self.assertIn('ask the user', out['deploy']['workaround'])

    def test_summary_is_short(self):
        w, r = self.with_tools()
        with w, r:
            res = self.scan()
        res['needs'] = preflight.resolve(preflight.load_jobs(), list(preflight.load_jobs()), res)
        self.assertLessEqual(len(preflight.summary(res).splitlines()), 8)

    def test_cli_unknown_need_exits_2(self):
        r = subprocess.run([sys.executable, str(CORE / 'graph.py'), 'preflight', '--needs', 'nope'],
                           cwd=self.cwd, capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(r.returncode, 2)
        self.assertIn('Known needs', r.stderr)


if __name__ == '__main__':
    unittest.main()
