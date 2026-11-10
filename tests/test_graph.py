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

    def ran(self, nid, *extra):
        self.node(nid, 'running', *extra); self.node(nid, 'complete')

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
        self.node('n_pending', 'pending')
        self.node('n_skipped', 'skipped', '--reason', 'not needed')
        self.node('n_failed', 'running'); self.node('n_failed', 'failed')
        self.node('n_cached', 'cached', code=3)
        self.node('n_skip2', 'skipped', code=3)

    def test_unknown_run_exits_2(self):
        self.g('next', 'nope', code=2)

    def test_next_lists_ready_nodes(self):
        self.init()
        self.node('a', 'pending')
        self.node('b', 'pending', '--depends-on', 'a')
        self.assertEqual(self.g('next', 'r1').stdout.split(), ['a'])
        self.ran('a')
        self.assertEqual(self.g('next', 'r1').stdout.split(), ['b'])
        self.ran('b')
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
        self.node('a', 'skipped', '--reason', 'not needed')
        self.node('b', 'running')

    def test_cannot_drop_dependencies_while_starting(self):
        self.init()
        self.node('a', 'pending', '--check', 'false')
        self.node('b', 'pending', '--depends-on', 'a')
        self.node('b', 'running', '--depends-on', code=3)
        self.node('a', 'running', '--check', 'true', code=3)
        self.assertEqual([n['status'] for n in self.state()['nodes']], ['pending', 'pending'])

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
        self.init('--quality-command', f'"{sys.executable}" -c "raise SystemExit(1)"')
        self.ran('a')
        self.g('finish', 'r1', code=3)
        self.g('quality', 'r1', code=1)
        self.g('finish', 'r1', code=3)

    def test_finish_ok_when_latest_quality_passed(self):
        flip = f'"{sys.executable}" -c "import os,sys; sys.exit(0 if os.path.exists(\'ok\') else 1)"'
        self.init('--quality-command', flip)
        self.ran('a')
        self.g('quality', 'r1', code=1)
        Path(self.dir, 'ok').write_text('x', encoding='utf-8')
        self.g('quality', 'r1')
        self.g('finish', 'r1')
        self.assertEqual(self.state()['status'], 'complete')

    def test_extra_command_cannot_hide_failed_required(self):
        self.init('--quality-command', f'"{sys.executable}" -c "raise SystemExit(1)"')
        self.ran('a')
        self.g('quality', 'r1', code=1)
        self.quality(f'"{sys.executable}" -c "pass"', 0)
        r = self.g('finish', 'r1', code=3)
        self.assertIn('required', r.stderr)

    def test_finish_without_checks_is_unverified(self):
        self.init()
        self.ran('a')
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
        self.ran('a')
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
        self.ran('a')
        html = (Path(self.dir) / '.graph/runs/r1/graph.html').read_text(encoding='utf-8')
        self.assertNotIn('Timeline', html)
        self.assertNotIn('Critical', html)
        self.assertTrue((Path(self.dir) / '.graph/runs/r1/graph.mmd').exists())



class Review(GraphCase):
    def test_cannot_jump_to_complete_or_skip(self):
        self.init()
        self.node('a', 'pending'); self.node('b', 'pending', '--depends-on', 'a')
        self.node('b', 'complete', code=3)
        self.node('b', 'skipped', '--reason', 'x', code=3)
        self.node('a', 'complete', code=3)
        self.assertEqual(self.state()['nodes'][1]['status'], 'pending')

    def test_zero_retries_cannot_complete_directly(self):
        self.init('--max-retries', '0')
        self.node('a', 'running'); self.node('a', 'failed')
        self.node('a', 'complete', code=3)

    def test_check_is_locked(self):
        self.init()
        self.node('a', 'pending', '--check', 'exit 1')
        self.node('a', 'running')
        self.node('a', 'complete', '--check', 'true', code=3)
        self.node('a', 'complete', code=3)
        self.assertEqual(self.state()['nodes'][0]['check'], 'exit 1')

    def test_dependencies_locked_after_start(self):
        self.init()
        self.node('a', 'pending'); self.node('b', 'pending', '--depends-on', 'a')
        self.node('b', 'pending', '--depends-on')
        self.node('b', 'pending', '--depends-on', 'a')
        self.node('x', 'running')
        self.node('b', 'running', '--depends-on', code=3)
        self.node('b', 'running', code=3)

    def test_init_refuses_existing_and_bad_ids(self):
        self.init()
        self.node('a', 'pending')
        self.g('init', 'task', '--id', 'r1', code=3)
        self.assertEqual(len(self.state()['nodes']), 1)
        self.g('init', 'task', '--id', '../escaped', code=2)
        self.assertFalse((Path(self.dir).parent / 'escaped').exists())

    def test_gitignore_written(self):
        self.init()
        self.assertEqual((Path(self.dir) / '.graph/.gitignore').read_text(encoding='utf-8'), '*\n')

    def test_summary_names_skipped_nodes(self):
        self.init('--quality-command', f'"{sys.executable}" -c "pass"')
        self.node('a', 'skipped', '--reason', 'x'); self.ran('b')
        self.g('quality', 'r1')
        self.g('finish', 'r1')
        s = (Path(self.dir) / '.graph/runs/r1/summary.md').read_text(encoding='utf-8')
        self.assertIn('Skipped or cached: a', s)
        self.assertIn('No check ran for: b', s)

    def test_check_timeout_kills_process(self):
        if os.name == 'nt': self.skipTest('POSIX process group check')
        self.init()
        marker = Path(self.dir) / 'alive'
        cmd = f'"{sys.executable}" -c "import time,pathlib; time.sleep(2); pathlib.Path(\'alive\').write_text(\'x\')"'
        self.node('a', 'running', '--check', cmd)
        self.node('a', 'complete', '--check-timeout', '1', code=3)
        import time; time.sleep(2.5)
        self.assertFalse(marker.exists())

    def test_pytest_not_detected_for_js_tests_folder(self):
        self.init()
        Path(self.dir, 'tests').mkdir()
        Path(self.dir, 'tests', 'a.test.js').write_text('//', encoding='utf-8')
        self.assertIn('No local quality command', self.g('quality', 'r1').stdout)


class Checks(GraphCase):
    def test_complete_runs_check_and_stores_result(self):
        self.init()
        self.node('a', 'running', '--check', 'echo hi'); self.node('a', 'complete')
        n = self.state()['nodes'][0]
        self.assertEqual((n['status'], n['check_exit']), ('complete', 0))
        self.assertIn('hi', n['check_output'])

    def test_failing_check_refuses_and_marks_failed(self):
        self.init()
        self.node('a', 'running', '--check', 'echo boom; exit 4')
        r = self.node('a', 'complete', code=3)
        self.assertIn('check failed', r.stderr)
        n = self.state()['nodes'][0]
        self.assertEqual((n['status'], n['check_exit']), ('failed', 4))
        self.assertIn('boom', n['check_output'])

    def test_check_timeout_fails(self):
        self.init()
        self.node('a', 'running', '--check', f'"{sys.executable}" -c "import time; time.sleep(5)"')
        self.node('a', 'complete', '--check-timeout', '1', code=3)
        self.assertIsNone(self.state()['nodes'][0]['check_exit'])

    def test_output_tail_is_40_lines(self):
        self.init()
        self.node('a', 'running', '--check', f'"{sys.executable}" -c "print(*range(100), sep=chr(10)); raise SystemExit(1)"')
        self.node('a', 'complete', code=3)
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


class Summary(GraphCase):
    def summary(self):
        return (Path(self.dir) / '.graph/runs/r1/summary.md').read_text(encoding='utf-8')

    def test_complete_with_passing_check(self):
        self.init('--quality-command', f'"{sys.executable}" -c "pass"')
        self.node('a', 'running', '--check', 'exit 0', '--files', 'x.py'); self.node('a', 'complete')
        self.g('quality', 'r1')
        r = self.g('finish', 'r1')
        s = self.summary()
        self.assertIn('The run is complete.', s)
        self.assertIn('x.py', s); self.assertIn('passed (exit 0)', s)
        self.assertIn('Nothing failed.', s)
        self.assertIn('summary', r.stdout.lower()); self.assertIn('graph.html', r.stdout)

    def test_unverified_never_claims_success(self):
        self.init()
        self.ran('a')
        self.g('finish', 'r1')
        s = self.summary()
        self.assertIn('unverified', s); self.assertNotIn('The run is complete.', s)
        self.assertIn('no check ran', s.lower()); self.assertIn('Test the result yourself', s)

    def test_failed_node_summary(self):
        self.init()
        self.node('a', 'running', '--check', 'echo boom; exit 3'); self.node('a', 'complete', code=3)
        self.g('finish', 'r1', '--status', 'failed')
        s = self.summary()
        self.assertIn('The run failed.', s)
        self.assertIn('Node a failed', s); self.assertIn('exit was 3', s); self.assertIn('Fix node a', s)

    def test_html_has_summary_and_evidence(self):
        self.init()
        self.node('a', 'running', '--check', 'echo hello_out; exit 0'); self.node('a', 'complete')
        self.g('finish', 'r1')
        h = (Path(self.dir) / '.graph/runs/r1/graph.html').read_text(encoding='utf-8')
        self.assertLess(h.index('Run summary'), h.index('Diagram'))
        self.assertIn('hello_out', h); self.assertIn('<details>', h)
        self.assertIn('prefers-color-scheme', h); self.assertNotIn('http://', h.replace('http://www.w3.org/2000/svg', ''))


class ExportWorkflow(GraphCase):
    def test_export_checks_syntax_and_levels(self):
        import shutil
        self.init()
        self.node('a', 'pending')
        self.node('b', 'pending', '--depends-on', 'a', '--check', 'echo "hi"')
        self.node('c', 'pending', '--depends-on', 'a')
        out = Path(self.dir) / 'wf.mjs'
        self.g('export-workflow', 'r1', '--out', str(out))
        js = out.read_text(encoding='utf-8')
        self.assertTrue(js.startswith('export const meta = {'))
        self.assertEqual(js.count('label: "a"'), 1)
        self.assertEqual(js.count('parallel('), 2)
        self.assertIn('--status running', js)
        self.assertIn('{ title: "Level 1", detail: "a" }', js)
        self.assertIn(Path(sys.executable).as_posix(), js)
        self.assertNotIn('Date.now', js); self.assertNotIn('Math.random', js)
        self.assertLess(js.index('label: "a"'), js.index('label: "b"'))
        node = shutil.which('node')
        if node:
            r = subprocess.run([node, '--check', str(out)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_default_path_and_invalid_plan(self):
        self.init()
        self.g('export-workflow', 'r1', code=2)
        self.node('a', 'pending', '--depends-on', 'b')
        self.g('export-workflow', 'r1', code=3)
        self.node('b', 'pending')
        self.g('export-workflow', 'r1')
        self.assertTrue((Path(self.dir) / '.claude/workflows/graph-r1.js').exists())


class RunLog(GraphCase):
    def log(self, run='r1'):
        p = Path(self.dir) / '.graph/runs' / run / 'log.jsonl'
        return [json.loads(x) for x in p.read_text(encoding='utf-8').splitlines()]

    def test_refused_move_logged(self):
        self.init()
        self.node('a', 'complete', code=3)
        e = self.log()[-1]
        self.assertEqual((e['cmd'], e['node'], e['outcome'], e['exit_code']), ('node', 'a', 'refused', 3))
        self.assertIn('Refused', e['message'])
        self.assertEqual(e['args']['status'], 'complete')

    def test_invalid_status_logged(self):
        self.init()
        self.node('a', 'bogus', code=2)
        e = self.log()[-1]
        self.assertEqual((e['outcome'], e['exit_code']), ('invalid', 2))

    def test_unknown_run_creates_nothing(self):
        self.g('next', 'nope', code=2)
        self.g('node', 'nope', 'a', '--role', 'w', '--status', 'running', code=2)
        self.assertFalse((Path(self.dir) / '.graph/runs/nope').exists())

    def test_check_logged(self):
        self.init()
        self.node('a', 'running', '--check', f'"{sys.executable}" -c "import sys; sys.exit(4)"')
        self.node('a', 'complete', code=3)
        e = self.log()[-1]
        self.assertEqual(e['outcome'], 'refused')
        c = e['checks'][0]
        self.assertEqual(c['exit_code'], 4)
        self.assertFalse(c['timed_out'])
        self.assertIsInstance(c['duration_ms'], int)

    def test_quality_logged(self):
        self.init()
        self.quality(f'"{sys.executable}" -c "pass"', 0)
        e = self.log()[-1]
        self.assertEqual(e['cmd'], 'quality')
        self.assertEqual(e['checks'][0]['exit_code'], 0)

    def test_parallel_appends_valid(self):
        self.init()
        procs = [subprocess.Popen([sys.executable, GRAPH, 'node', 'r1', f'n{i}', '--role', 'w', '--status', 'pending'],
                                  cwd=self.dir, stdout=subprocess.DEVNULL) for i in range(8)]
        for p in procs: self.assertEqual(p.wait(), 0)
        lines = [e for e in self.log() if e['cmd'] == 'node']
        self.assertEqual(len(lines), 8)

    def test_log_command_filters(self):
        self.init()
        self.node('a', 'pending')
        self.node('a', 'complete', code=3)
        out = self.g('log', 'r1', '--refusals').stdout
        self.assertIn('REFUSED', out)
        self.assertNotIn('INIT', out.upper().replace('REFUSED', ''))
        rows = json.loads(self.g('log', 'r1', '--refusals', '--json').stdout)
        self.assertTrue(rows and all(r['outcome'] in ('refused', 'invalid') for r in rows))
        self.assertIn('a', self.g('log', 'r1', '--node', 'a').stdout)

    def test_report_and_summary(self):
        self.init()
        self.node('a', 'complete', code=3)
        html = (Path(self.dir) / '.graph/runs/r1/graph.html').read_text(encoding='utf-8')
        self.assertIn('<h2>Activity</h2>', html)
        self.assertIn('Rule refusals: 1', html)
        self.assertIn('class="bad"', html)
        self.g('node', 'r1', 'a', '--role', 'w', '--status', 'skipped', '--reason', 'x')
        self.g('finish', 'r1', '--status', 'failed')
        s = (Path(self.dir) / '.graph/runs/r1/summary.md').read_text(encoding='utf-8')
        self.assertIn('The rules refused 1 action(s) on node(s): a.', s)

    def test_preflight_run_logged(self):
        self.init()
        self.g('preflight', '--run', 'r1', '--needs', 'github')
        e = self.log()[-1]
        self.assertEqual(e['cmd'], 'preflight')
        self.assertIn('github', e['preflight']['needs'])
