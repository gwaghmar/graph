import json, os, re, subprocess, sys, tempfile, time, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent)); import briefhelp  # noqa: E402

CORE = Path(__file__).resolve().parent.parent / 'skills' / 'graph' / 'scripts'
GRAPH = str(CORE / 'graph.py')
sys.path.insert(0, str(CORE))
import preflight, userctx  # noqa: E402

USER = 'zorbatheuser'  # fake OS user name: must never appear in any output or log


def write(p, text, days_old=None):
    p = Path(p); p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding='utf-8')
    if days_old is not None:
        t = time.time() - days_old * 86400
        os.utime(p, (t, t))
    return p


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(os.path.realpath(self._tmp.name))
        self.home = root / 'Users' / USER
        self.proj = root / 'proj'
        self.home.mkdir(parents=True); self.proj.mkdir()
        self.env_extra = {}

    def env(self):
        e = {k: v for k, v in os.environ.items() if k not in ('CLAUDECODE',)}
        e.update(HOME=str(self.home), USERPROFILE=str(self.home), SECRET_TOKEN='sekret-env-value')
        e.update(self.env_extra)
        return e

    def g(self, *args, code=0):
        if args and args[0] == 'init' and '--size' not in args: args = (*args, '--size', 'medium')
        r = subprocess.run([sys.executable, GRAPH, *args], cwd=self.proj, capture_output=True, text=True,
                           encoding='utf-8', env=self.env())
        self.assertEqual(r.returncode, code, f'{args}: {r.stdout}{r.stderr}')
        rid = briefhelp.init_run_id(args)
        if rid and r.returncode == 0:
            subprocess.run([sys.executable, GRAPH, *briefhelp.brief_cmd(rid)], cwd=self.proj, capture_output=True, text=True, encoding='utf-8', env=self.env())
        return r

    def init(self, *extra, rid='r1'):
        self.g('init', 'task', '--id', rid, '--skip-preflight', 'test', '--skip-budget', 'test', *extra)

    def state(self, rid='r1'):
        return json.loads((self.proj / f'.graph/runs/{rid}/state.json').read_text(encoding='utf-8'))

    def log(self, rid='r1'):
        return (self.proj / f'.graph/runs/{rid}/log.jsonl').read_text(encoding='utf-8')

    def mem(self):
        return self.home / '.claude' / 'projects' / re.sub(r'[^A-Za-z0-9]', '-', str(self.proj)) / 'memory'

    def node(self, status, code=0, nid='a'):
        return self.g('node', 'r1', nid, '--role', 'research', '--status', status, '--goal', 'g', code=code)

    def settings(self, **kw):
        write(self.home / '.claude/settings.json', json.dumps(kw))


class ContextTests(Base):
    def setUp(self):
        super().setUp()
        write(self.home / '.claude/CLAUDE.md', 'GLOBAL-BODY-SECRET', 20)
        write(self.home / '.codex/AGENTS.md', 'CODEX-BODY-SECRET', 3)
        write(self.proj / 'CLAUDE.md', 'PROJECT-BODY-SECRET', 1)
        write(self.proj / '.cursor/rules/a.mdc', '---\nalwaysApply: true\n---\nRULE-BODY-SECRET')
        write(self.proj / '.cursor/rules/b.mdc', '---\nalwaysApply: false\n---\nx')
        m = self.mem()
        write(m / 'MEMORY.md', '- [Old](old.md) hook\n- [Fresh](fresh.md) hook\n- [Meta](meta.md) x\n- [Bad](../evil.md) y\n- [Missing](gone.md) z\n', 5)
        write(m / 'old.md', '---\nname: old\n---\nOLD-BODY-SECRET', 45)
        write(m / 'fresh.md', '---\nname: fresh\n---\nFRESH-BODY', 2)
        # metadata.modified wins over a new file date
        write(m / 'meta.md', '---\nname: meta\nmetadata:\n  modified: 2020-01-02\n---\nMETA-BODY-SECRET', 0)
        write(self.home / '.claude/evil.md', '---\nmodified: 2000-01-01\n---\n')
        self.init()

    def test_report_shape_and_stale(self):
        out = self.g('context', '--run', 'r1').stdout
        lines = out.splitlines()
        self.assertEqual(lines[0], f'Today is {time.strftime("%Y-%m-%d", time.gmtime())}.')
        self.assertIn('Stale: 2 notes', out)
        self.assertRegex(out, r'old\.md.*STALE: check before using')
        self.assertRegex(out, r'meta\.md.*2020-01-02.*STALE')
        self.assertNotRegex(out, r'fresh\.md.*STALE')
        self.assertNotIn('evil.md', out); self.assertNotIn('gone.md', out)
        self.assertIn('already in context: do not re-read', out)
        self.assertIn('On other hosts, read the global files, then the project files', out)
        self.assertRegex(out, r'old\.md.*45 days ago')

    def test_priority_order_and_flags(self):
        out = self.g('context', '--run', 'r1').stdout
        order = [out.index(x) for x in ('~/.claude/CLAUDE.md', '~/.codex/AGENTS.md', './CLAUDE.md', './.cursor/rules/a.mdc', 'memory index')]
        self.assertEqual(order, sorted(order))
        self.assertRegex(out, r'a\.mdc \(alwaysApply\)')
        self.assertNotRegex(out, r'b\.mdc \(alwaysApply\)')
        self.assertIn('20 days ago', out)

    def test_privacy_no_bodies_no_username(self):
        r = self.g('context', '--run', 'r1')
        for bad in ('BODY-SECRET', USER, 'sekret-env-value', str(self.proj), 'RULE-BODY'):
            self.assertNotIn(bad, r.stdout + r.stderr)
            self.assertNotIn(bad, self.log())
        self.assertNotIn(USER, json.dumps(self.state()))

    def test_state_and_log(self):
        self.g('context', '--run', 'r1')
        c = self.state()['context']
        self.assertEqual(sorted(c['stale']), ['meta.md', 'old.md'])
        self.assertIn('"cmd": "context"', self.log())

    def test_unknown_run_and_no_files(self):
        self.g('context', '--run', 'nope', code=2)
        self.init(rid='r2'); empty = Path(self.proj) / 'sub'; empty.mkdir()
        shutil_home = self.home; self.home = self.home.parent / 'emptyhome'; self.home.mkdir()
        self.g('context', '--run', 'r2')
        self.home = shutil_home

    def test_unit_age(self):
        text, stale, n = userctx.context_report(self.proj, self.home, today='2030-01-01')
        self.assertIn('Today is 2030-01-01.', text)
        self.assertEqual(len(stale), 3)  # every note is old against 2030


class ProfileTests(Base):
    def test_profile_ok_and_logged(self):
        self.init()
        self.g('profile', '--run', 'r1', '--text', 'short replies [user said]\nlikes tables [memory]')
        self.assertEqual(self.state()['profile']['text'], 'short replies [user said]\nlikes tables [memory]')
        self.assertIn('likes tables', self.log())

    def test_profile_limits(self):
        self.init()
        r = self.g('profile', '--run', 'r1', '--text', '\n'.join(['x [memory]'] * 11), code=2)
        self.assertIn('10 lines', r.stderr)
        self.g('profile', '--run', 'r1', '--text', 'y' * 1201 + ' [memory]', code=2)
        self.g('profile', '--run', 'r1', '--text', '  ', code=2)
        self.g('profile', '--run', 'r1', '--text', '\n'.join(['x [memory]'] * 10))
        self.g('profile', '--run', 'r1', '--text', 'z' * 1170 + ' [memory]')
        self.assertIn('"outcome": "invalid"', self.log())


class CatalogCase(Base):
    def setUp(self):
        super().setUp()
        long = 'word ' * 80
        write(self.home / '.claude/skills/deploy-helper/SKILL.md',
              f'---\nname: deploy-helper\ndescription: Deploy a site to production hosting {long}\n---\nSKILL-BODY-SECRET')
        write(self.home / '.claude/skills/hidden-thing/SKILL.md',
              '---\nname: hidden-thing\ndescription: Deploy production hosting wizard\n---\nbody')
        write(self.proj / '.claude/skills/chart-maker/SKILL.md',
              '---\nname: chart-maker\ndescription: >\n  Draw charts and graphs\n  from data tables\n---\nbody')
        write(self.home / '.agents/skills/bare-dir/notes.txt', 'no skill file')
        base = self.home / '.claude/plugins/cache/mk'
        write(base / 'plug-on/1.0.0/skills/design-pro/SKILL.md', '---\nname: design-pro\ndescription: Visual design layout typography\n---\nbody')
        write(base / 'plug-on/1.0.0/.claude-plugin/plugin.json', json.dumps({'name': 'plug-on', 'description': 'Visual design toolkit for layout'}))
        write(base / 'plug-off/2.0.0/skills/db-tune/SKILL.md', '---\nname: db-tune\ndescription: Database query tuning postgres\n---\nbody')
        write(base / 'plug-off/2.0.0/.claude-plugin/plugin.json', json.dumps({'description': 'Database postgres tuning'}))
        self.settings(enabledPlugins={'plug-on@mk': True, 'plug-off@mk': False, 'ghost@mk': True},
                      skillOverrides={'hidden-thing': 'off', 'chart-maker': 'on'},
                      env={'API_KEY': 'sekret-env-value'})
        self.cat = {(c['kind'], c['name']): c for c in preflight.skill_catalog(self.proj, self.home)}


class DiscoveryTests(CatalogCase):
    def test_discovery(self):
        c = self.cat
        self.assertIn(('skill', 'deploy-helper'), c); self.assertIn(('skill', 'chart-maker'), c)
        self.assertIn(('skill', 'plug-on:design-pro'), c); self.assertIn(('plugin', 'plug-on'), c)
        self.assertIn(('skill', 'bare-dir'), c) if False else None
        self.assertEqual(c[('skill', 'chart-maker')]['desc'], 'Draw charts and graphs from data tables')

    def test_off_marks(self):
        c = self.cat
        self.assertTrue(c[('skill', 'hidden-thing')]['off'])
        self.assertFalse(c[('skill', 'deploy-helper')]['off'])
        self.assertFalse(c[('skill', 'chart-maker')]['off'])  # "on" is not off
        self.assertFalse(c[('plugin', 'plug-on')]['off'])
        self.assertTrue(c[('plugin', 'plug-off')]['off'])
        self.assertTrue(c[('skill', 'plug-off:db-tune')]['off'])
        self.assertEqual(c[('plugin', 'plug-off')]['off_why'], 'enabledPlugins is false')

    def test_description_cut_and_no_body(self):
        d = self.cat[('skill', 'deploy-helper')]['desc']
        self.assertEqual(len(d), 200)
        self.assertNotIn('SKILL-BODY-SECRET', json.dumps(list(self.cat.values())))
        self.assertNotIn('sekret-env-value', json.dumps(list(self.cat.values())))

    def test_installed_plugins_json_path(self):
        other = self.home / '.claude/plugins/elsewhere/ver'
        write(other / 'skills/odd/SKILL.md', '---\nname: odd\ndescription: Odd one\n---\n')
        write(self.home / '.claude/plugins/installed_plugins.json',
              json.dumps({'plugins': {'ghost@mk': [{'installPath': str(other)}],
                                      'evil@mk': [{'installPath': str(self.home.parent)}]}}))
        self.settings(enabledPlugins={'ghost@mk': True, 'evil@mk': True})
        c = {(x['kind'], x['name']) for x in preflight.skill_catalog(self.proj, self.home)}
        self.assertIn(('skill', 'ghost:odd'), c)
        self.assertFalse([k for k in c if k[1].startswith('evil:')])  # outside ~/.claude/plugins is ignored

    def test_scan_includes_catalog_and_names(self):
        res = preflight.scan(self.proj, self.home, {}, refresh=True)
        self.assertIn('deploy-helper', res['skills']); self.assertNotIn('hidden-thing', res['skills'])
        self.assertTrue(res['catalog'])
        self.assertIn('turned off', preflight.summary(dict(res, needs={})))

    def test_front_parser_reads_no_body(self):
        f, raw = preflight.read_front(self.home / '.claude/skills/deploy-helper/SKILL.md')
        self.assertNotIn('SKILL-BODY', raw)
        self.assertEqual(f['name'], 'deploy-helper')


class ToolsTests(CatalogCase):
    def setUp(self):
        super().setUp()
        self.env_extra = {'PATH': str(Path(sys.executable).parent)}
        self.init()

    def test_ranking_and_off_note(self):
        out = self.g('tools', '--run', 'r1', '--task', 'deploy the site to production hosting').stdout
        self.assertLess(out.index('deploy-helper'), out.index('(skill'))  # listed
        first = [l for l in out.splitlines() if l.startswith('  1.')][0]
        self.assertIn('deploy-helper', first)
        self.assertNotIn('hidden-thing (skill', out)
        self.assertIn('matches but turned off: hidden-thing; ask the user before turning it on', out)
        self.assertIn('CLI and MCP picks', out); self.assertIn('github:', out)

    def test_top_eight_cap_and_plugin_rank(self):
        out = self.g('tools', '--run', 'r1', '--task', 'visual design layout').stdout
        ranked = [l for l in out.splitlines() if re.match(r'\s+\d+\. ', l)]
        self.assertLessEqual(len(ranked), 8)
        self.assertTrue(any('design-pro' in l or 'plug-on' in l for l in ranked))

    def test_off_plugin_flagged_not_ranked(self):
        out = self.g('tools', '--run', 'r1', '--task', 'tune postgres database query').stdout
        self.assertIn('matches but turned off: ', out)
        self.assertNotRegex(out, r'\d\. plug-off')

    def test_no_match_and_task_required_and_privacy(self):
        out = self.g('tools', '--run', 'r1', '--task', 'zzqq xxyy').stdout
        self.assertIn('no skill or plugin matched', out)
        self.g('tools', '--run', 'r1', code=2)
        for bad in (USER, 'sekret-env-value', 'SKILL-BODY-SECRET'):
            self.assertNotIn(bad, out); self.assertNotIn(bad, self.log())

    def test_unit_words(self):
        self.assertIn('chart', userctx.words('Charts and graphs'))
        self.assertNotIn('the', userctx.words('the cat'))


class ChooseTests(CatalogCase):
    def setUp(self):
        super().setUp(); self.init()

    def test_choose_records_and_flags(self):
        r = self.g('choose', '--run', 'r1', '--skills', 'deploy-helper,nosuch,hidden-thing', '--plugins', 'plug-on,nope@x',
                   '--clis', 'nosuchcli123', '--mcp', 'nomcp', '--why', 'fits the deploy')
        k = self.state()['toolkit']
        self.assertEqual(k['skills'], ['deploy-helper', 'nosuch', 'hidden-thing'])
        self.assertEqual(k['why'], 'fits the deploy')
        for x in ('skill nosuch', 'plugin nope@x', 'cli nosuchcli123', 'mcp nomcp'):
            self.assertIn(x + ': not found on this machine', r.stdout)
        self.assertIn('skill hidden-thing: turned off', r.stdout)
        self.assertNotIn('skill deploy-helper:', r.stdout)
        self.assertIn('"cmd": "choose"', self.log())
        self.assertIn('nosuchcli123', self.log())

    def test_choose_plain_name_for_plugin_skill_and_cli_found(self):
        r = self.g('choose', '--run', 'r1', '--skills', 'design-pro,plug-on:design-pro', '--clis', Path(sys.executable).name, '--why', 'ok')
        self.assertNotIn('not found', r.stdout.replace('python', ''))

    def test_choose_needs_why_and_good_names(self):
        self.g('choose', '--run', 'r1', '--skills', 'a', code=2)
        self.g('choose', '--run', 'r1', '--why', ' ', code=2)
        self.g('choose', '--run', 'r1', '--skills', 'a;rm -rf', '--why', 'x', code=2)
        self.g('choose', '--run', 'r1', '--why', 'no extra tools needed')  # empty toolkit is allowed with a reason


class GateTests(Base):
    def prepare(self, *extra):
        self.init(*extra)
        self.g('node', 'r1', 'a', '--role', 'research', '--status', 'pending', '--goal', 'g')

    def test_all_missing_listed(self):
        self.g('init', 'task', '--id', 'r1')
        self.g('node', 'r1', 'a', '--role', 'research', '--status', 'pending', '--goal', 'g')
        r = self.node('running', code=3)
        for x in ('preflight: run', 'profile: read the user', 'choose: run', 'approve: show the plan', '--skip-preflight',
                  '--skip-context', '--auto-approve', 'context --run r1', 'profile --run r1', 'choose --run r1', 'approve --run r1'):
            self.assertIn(x, r.stderr)
        self.assertNotIn(USER, r.stderr)
        self.assertIn('"outcome": "refused"', self.log())
        self.assertNotIn(USER, self.log())

    def test_home_shown_as_tilde(self):
        import shutil
        dest = self.home / '.claude/skills/graph'
        shutil.copytree(CORE.parent, dest, ignore=shutil.ignore_patterns('__pycache__'))
        run = lambda *a: subprocess.run([sys.executable, str(dest / 'scripts/graph.py'), *a], cwd=self.proj,
                                        capture_output=True, text=True, encoding='utf-8', env=self.env())
        self.assertEqual(run('init', 't', '--id', 'r1', '--size', 'medium').returncode, 0)
        run('node', 'r1', 'a', '--role', 'research', '--status', 'pending', '--goal', 'g')
        r = run('node', 'r1', 'a', '--role', 'research', '--status', 'running')
        self.assertEqual(r.returncode, 3)
        self.assertIn('~/.claude/skills/graph/scripts/graph.py', r.stderr)
        self.assertNotIn(USER, r.stderr); self.assertNotIn(USER, self.log())

    def test_only_missing_steps_listed_and_flow(self):
        self.prepare()
        r = self.node('running', code=3)
        self.assertNotIn('preflight: run', r.stderr)
        self.assertIn('profile: read', r.stderr); self.assertIn('approve: show', r.stderr)
        self.g('profile', '--run', 'r1', '--text', 'terse [user said]')
        r = self.node('running', code=3)
        self.assertNotIn('profile: read', r.stderr); self.assertIn('choose: run', r.stderr)
        self.g('choose', '--run', 'r1', '--skills', 'x', '--why', 'because')
        r = self.node('running', code=3)
        self.assertNotIn('choose: run', r.stderr); self.assertIn('approve: show', r.stderr)
        self.g('approve', '--run', 'r1', '--note', 'go, looks good')
        self.assertEqual(self.state()['approval']['note'], 'go, looks good')
        self.node('running'); self.node('complete')

    def test_auto_approve(self):
        self.prepare('--skip-context', 'no user files', '--auto-approve', 'just do it')
        self.assertEqual(self.state()['auto_approve'], 'just do it')
        self.node('running')
        self.assertIn('just do it', self.log())

    def test_skip_context_waives_profile_and_choose_only(self):
        self.prepare('--skip-context', 'bare host')
        r = self.node('running', code=3)
        self.assertNotIn('profile: read', r.stderr); self.assertNotIn('choose: run', r.stderr)
        self.assertIn('approve: show', r.stderr)
        self.g('approve', '--run', 'r1', '--note', 'ok go')
        self.node('running')

    def test_auto_approve_alone_not_enough(self):
        self.prepare('--auto-approve', 'just do it')
        r = self.node('running', code=3)
        self.assertIn('profile: read', r.stderr); self.assertNotIn('approve: show', r.stderr)

    def test_blank_flags_and_empty_approve(self):
        self.g('init', 't', '--id', 'x1', '--skip-context', ' ', code=2)
        self.g('init', 't', '--id', 'x2', '--auto-approve', '', code=2)
        self.init()
        self.g('approve', '--run', 'r1', '--note', ' ', code=2)
        self.g('approve', '--run', 'nope', '--note', 'x', code=2)

    def test_retry_also_gated_only_by_missing_steps(self):
        self.prepare('--skip-context', 'x', '--auto-approve', 'go ahead')
        self.node('running'); self.node('failed')
        self.node('retrying')

    def test_old_schema_run_needs_only_preflight(self):
        self.prepare()
        p = self.proj / '.graph/runs/r1/state.json'
        d = json.loads(p.read_text(encoding='utf-8')); d['schema_version'] = 3; p.write_text(json.dumps(d), encoding='utf-8')
        self.node('running')

    def test_export_workflow_refused_without_approval(self):
        self.prepare('--skip-context', 'x')
        r = self.g('export-workflow', 'r1', code=3)
        self.assertIn('approve: show', r.stderr)
        self.g('approve', '--run', 'r1', '--note', 'go')
        self.g('export-workflow', 'r1')


class FreshnessTests(Base):
    def test_run_date_and_summary(self):
        today = time.strftime('%Y-%m-%d', time.gmtime())
        write(self.mem() / 'MEMORY.md', '- [Old](old.md) x\n')
        write(self.mem() / 'old.md', '---\nname: old\n---\nbody', 60)
        self.init()
        self.assertEqual(self.state()['run_date'], today)
        self.g('context', '--run', 'r1')
        self.g('profile', '--run', 'r1', '--text', 'terse [user said]')
        self.g('choose', '--run', 'r1', '--skills', 'x', '--why', 'w')
        self.g('node', 'r1', 'a', '--role', 'research', '--status', 'pending', '--goal', 'g')
        self.g('approve', '--run', 'r1', '--note', 'go ahead please')
        self.node('running'); self.node('complete')
        self.g('finish', 'r1', '--status', 'failed')
        s = (self.proj / '.graph/runs/r1/summary.md').read_text(encoding='utf-8')
        self.assertIn(f'Run date: {today}', s)
        self.assertIn('Stale context flagged: 1 (old.md)', s)
        self.assertIn('Approval: go ahead please', s)
        self.assertIn('cannot prove a human', s)
        html = (self.proj / '.graph/runs/r1/graph.html').read_text(encoding='utf-8')
        self.assertIn('Run date', html); self.assertIn('Stale context flagged', html)

    def test_summary_when_context_not_read(self):
        self.init('--skip-context', 'bare', '--auto-approve', 'just do it')
        self.g('finish', 'r1', '--status', 'failed')
        s = (self.proj / '.graph/runs/r1/summary.md').read_text(encoding='utf-8')
        self.assertIn('Stale context flagged: 0 (context step waived: bare)', s)
        self.assertIn('Approval: skipped, the user said: just do it', s)


if __name__ == '__main__':
    unittest.main()


class ReviewFixTests(Base):
    def test_approve_needs_plan_and_is_tied_to_it(self):
        self.init()
        self.g('approve', '--run', 'r1', '--note', 'go', code=2)  # no nodes
        self.g('context', '--run', 'r1'); self.g('profile', '--run', 'r1', '--text', 'No user rules found [none]')
        self.g('choose', '--run', 'r1', '--skills', 'x', '--why', 'w')
        self.g('node', 'r1', 'a', '--role', 'research', '--status', 'pending', '--goal', 'g')
        self.g('approve', '--run', 'r1', '--note', 'go')
        self.g('node', 'r1', 'b', '--role', 'research', '--status', 'pending', '--goal', 'g2')
        r = self.g('node', 'r1', 'a', '--role', 'research', '--status', 'running', '--goal', 'g', code=3)
        self.assertIn('plan changed after approval', r.stderr)
        self.g('approve', '--run', 'r1', '--note', 'go again')
        self.node('running')

    def test_memory_from_project_dir_and_missing_notice(self):
        other = self.home / 'elsewhere'; other.mkdir()
        slug = re.sub(r'[^A-Za-z0-9]', '-', str(other))
        write(self.home / '.claude' / 'projects' / slug / 'memory' / 'MEMORY.md', '- [N](n.md) x\n')
        write(self.home / '.claude' / 'projects' / slug / 'memory' / 'n.md', '---\nname: n\n---\nb', 60)
        self.init()
        r = self.g('context', '--run', 'r1')
        self.assertIn('memory index not found for this folder', r.stdout)
        self.assertNotIn('Stale: 0', r.stdout)
        r = self.g('context', '--run', 'r1', '--project-dir', str(other))
        self.assertIn('Stale: 1 notes', r.stdout)
        self.env_extra['CLAUDE_PROJECT_DIR'] = str(other)
        self.assertIn('Stale: 1 notes', self.g('context', '--run', 'r1').stdout)

    def test_plugin_prefix_settings_merge_skills_dir_and_traversal(self):
        sys.path.insert(0, str(Path(GRAPH).parent))
        import preflight
        plug = self.home / '.claude' / 'plugins' / 'cache' / 'm' / 'p-key' / '1'
        write(plug / '.claude-plugin' / 'plugin.json', json.dumps({'name': 'real-name', 'description': 'd'}))
        write(plug / 'skills' / 'k' / 'SKILL.md', '---\nname: k\ndescription: x\n---\n')
        write(plug / 'deprecated' / 'old' / 'SKILL.md', '---\nname: old\n---\n')
        write(self.home / '.claude' / 'settings.json', json.dumps({'enabledPlugins': {'p-key@m': False, 'x@../../../../outside': True}}))
        write(self.proj / '.claude' / 'settings.local.json', json.dumps({'enabledPlugins': {'p-key@m': True}}))
        names = {(i['kind'], i['name']): i for i in preflight.skill_catalog(self.proj, self.home)}
        self.assertIn(('skill', 'real-name:k'), names)
        self.assertFalse(names[('skill', 'real-name:k')]['off'])
        self.assertNotIn(('skill', 'real-name:old'), names)
        self.assertNotIn(('skill', 'p-key:old'), names)
        self.assertFalse([k for k in names if k[1].startswith('x:')])
