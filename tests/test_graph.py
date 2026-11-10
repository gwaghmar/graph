import json, os, subprocess, sys, tempfile, unittest
from pathlib import Path

GRAPH = str(Path(__file__).resolve().parent.parent / 'skills' / 'graph' / 'scripts' / 'graph.py')


class GraphCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def g(self, *args, code=0):
        r = subprocess.run([sys.executable, GRAPH, *args], cwd=self.dir, capture_output=True,
                           text=True, encoding='utf-8')
        self.assertEqual(r.returncode, code, f'{args}: {r.stdout}{r.stderr}')
        return r

    def init(self, *extra):
        self.g('init', 'task', '--id', 'r1', *extra)

    def node(self, nid, status, *extra, code=0):
        return self.g('node', 'r1', nid, '--role', 'worker', '--status', status, *extra, code=code)

    def state(self):
        return json.loads((Path(self.dir) / '.graph/runs/r1/state.json').read_text(encoding='utf-8'))

    def quality(self, cmd, code):
        self.g('quality', 'r1', '--command', cmd, code=code)


class Rules(GraphCase):
    def test_bad_status_exits_2(self):
        self.init()
        r = self.node('a', 'bogus', code=2)
        self.assertIn('pending', r.stderr)

    def test_all_valid_statuses_accepted(self):
        self.init()
        for st in ('pending', 'skipped', 'cached', 'failed'):
            self.node('n_' + st, st)

    def test_unknown_run_exits_2(self):
        self.g('next', 'nope', code=2)

    def test_next_lists_ready_nodes(self):
        self.init()
        self.node('a', 'pending')
        self.node('b', 'pending', '--depends-on', 'a')
        self.assertEqual(self.g('next', 'r1').stdout.split(), ['a'])
        self.node('a', 'running'); self.node('a', 'complete')
        self.assertEqual(self.g('next', 'r1').stdout.split(), ['b'])
        self.node('b', 'complete')
        self.assertEqual(self.g('next', 'r1').stdout.strip(), 'none')

    def test_next_includes_failed_with_retries_left_only(self):
        self.init('--max-retries', '1')
        self.node('a', 'running'); self.node('a', 'failed')
        self.assertEqual(self.g('next', 'r1').stdout.split(), ['a'])
        self.node('a', 'retrying'); self.node('a', 'failed')
        self.assertEqual(self.g('next', 'r1').stdout.strip(), 'none')

    def test_cannot_run_before_dependencies_done(self):
        self.init()
        self.node('a', 'pending')
        self.node('b', 'pending', '--depends-on', 'a')
        r = self.node('b', 'running', code=3)
        self.assertIn('waiting on a', r.stderr)
        self.node('b', 'retrying', code=3)
        self.node('a', 'skipped')
        self.node('b', 'running')

    def test_retry_cap_default_is_two(self):
        self.init()
        self.node('a', 'running'); self.node('a', 'failed')
        self.node('a', 'retrying'); self.node('a', 'failed')
        self.node('a', 'retrying'); self.node('a', 'failed')
        r = self.node('a', 'retrying', code=3)
        self.assertIn('attempts', r.stderr)
        self.assertEqual(self.state()['nodes'][0]['attempts'], 3)

    def test_retry_cap_zero(self):
        self.init('--max-retries', '0')
        self.node('a', 'running'); self.node('a', 'failed')
        self.node('a', 'retrying', code=3)

    def test_finish_refused_with_open_nodes(self):
        self.init()
        self.node('a', 'pending')
        r = self.g('finish', 'r1', code=3)
        self.assertIn('a', r.stderr)
        self.assertNotIn('finished_at', self.state())

    def test_finish_refused_when_quality_failed(self):
        self.init()
        self.node('a', 'complete')
        self.quality(f'"{sys.executable}" -c "raise SystemExit(1)"', 1)
        self.g('finish', 'r1', code=3)

    def test_finish_ok_when_latest_quality_passed(self):
        self.init()
        self.node('a', 'complete')
        self.quality(f'"{sys.executable}" -c "raise SystemExit(1)"', 1)
        self.quality(f'"{sys.executable}" -c "pass"', 0)
        self.g('finish', 'r1')
        self.assertEqual(self.state()['status'], 'complete')

    def test_finish_without_checks_is_unverified(self):
        self.init()
        self.node('a', 'complete')
        r = self.g('finish', 'r1')
        self.assertIn('complete-unverified', r.stdout)
        self.assertEqual(self.state()['status'], 'complete-unverified')

    def test_finish_failed_always_allowed_and_resumable(self):
        self.init()
        self.node('a', 'running'); self.node('a', 'failed')
        self.g('finish', 'r1', '--status', 'failed')
        self.assertEqual(self.state()['status'], 'failed')
        out = json.loads(self.g('resume').stdout)
        self.assertEqual(out['run'], 'r1')
        s = self.state()
        self.assertEqual(s['status'], 'running')
        self.assertNotIn('finished_at', s)

    def test_completed_run_not_resumable(self):
        self.init()
        self.node('a', 'complete')
        self.g('finish', 'r1')
        self.g('resume', code=2)

    def test_pytest_no_tests_is_not_failure(self):
        try:
            import pytest  # noqa: F401
        except ImportError:
            self.skipTest('pytest not installed')
        self.init()
        Path(self.dir, 'tests').mkdir()
        Path(self.dir, 'tests', 'test_x.py').write_text('# no tests here\n', encoding='utf-8')
        self.g('quality', 'r1')
        self.assertEqual(self.state()['quality'][0]['status'], 'no-tests')

    def test_pytest_not_detected_without_tests(self):
        self.init()
        r = self.g('quality', 'r1')
        self.assertIn('No local quality command', r.stdout)

    def test_removed_commands_are_gone(self):
        self.init()
        for cmd in (['cache-get', 'r1', 'a'], ['cache-put', 'r1', 'a', 'x'], ['usage', 'r1'], ['commit', 'r1', '--yes']):
            self.g(*cmd, code=2)

    def test_report_has_no_timeline(self):
        self.init()
        self.node('a', 'complete')
        html = (Path(self.dir) / '.graph/runs/r1/graph.html').read_text(encoding='utf-8')
        self.assertNotIn('Timeline', html)
        self.assertNotIn('Critical', html)
        self.assertTrue((Path(self.dir) / '.graph/runs/r1/graph.mmd').exists())



class Checks(GraphCase):
    def test_complete_runs_check_and_stores_result(self):
        self.init()
        self.node('a', 'complete', '--check', 'echo hi')
        n = self.state()['nodes'][0]
        self.assertEqual((n['status'], n['check_exit']), ('complete', 0))
        self.assertIn('hi', n['check_output'])

    def test_failing_check_refuses_and_marks_failed(self):
        self.init()
        r = self.node('a', 'complete', '--check', 'echo boom; exit 4', code=3)
        self.assertIn('check failed', r.stderr)
        n = self.state()['nodes'][0]
        self.assertEqual((n['status'], n['check_exit']), ('failed', 4))
        self.assertIn('boom', n['check_output'])

    def test_check_timeout_fails(self):
        self.init()
        self.node('a', 'complete', '--check', f'"{sys.executable}" -c "import time; time.sleep(5)"', '--check-timeout', '1', code=3)
        self.assertIsNone(self.state()['nodes'][0]['check_exit'])

    def test_output_tail_is_40_lines(self):
        self.init()
        self.node('a', 'complete', '--check', f'"{sys.executable}" -c "print(*range(100), sep=chr(10)); raise SystemExit(1)"', code=3)
        lines = self.state()['nodes'][0]['check_output'].splitlines()
        self.assertEqual((len(lines), lines[-1]), (40, '99'))

    def test_bad_fields_exit_2(self):
        self.init()
        self.node('a', 'pending', '--target', 'toaster', code=2)
        self.node('a', 'pending', '--risk', 'medium', code=2)
        self.node('a', 'pending', '--check', ' ', code=2)

    def test_validate_catches_bad_stored_field(self):
        self.init()
        self.node('a', 'pending', '--target', 'web', '--risk', 'high')
        self.g('validate', 'r1')
        p = Path(self.dir) / '.graph/runs/r1/state.json'
        d = json.loads(p.read_text(encoding='utf-8')); d['nodes'][0]['target'] = 'x'
        p.write_text(json.dumps(d), encoding='utf-8')
        self.g('validate', 'r1', code=1)

    def test_brief_has_goal_check_output_attempt(self):
        self.init()
        self.node('a', 'running', '--check', 'echo nope; exit 1', '--goal', 'Make it work')
        self.node('a', 'complete', code=3)
        out = self.g('brief', 'r1', 'a').stdout
        for want in ('Make it work', 'echo nope', 'nope', 'attempt 2 of 3'):
            self.assertIn(want, out)
        self.g('brief', 'r1', 'zzz', code=2)

    def test_verify_plan(self):
        self.init()
        self.node('a', 'pending', '--target', 'web', '--risk', 'high')
        self.node('b', 'pending', '--target', 'none')
        out = self.g('verify-plan', 'r1', 'a').stdout
        self.assertIn('1. build, tests, http status', out)
        self.assertIn('High risk', out)
        self.assertIn('no extra verification', self.g('verify-plan', 'r1', 'b').stdout)
        self.g('verify-plan', 'r1', 'zzz', code=2)

if __name__ == '__main__':
    unittest.main()
