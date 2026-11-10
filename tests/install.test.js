const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawnSync } = require('child_process');
const bin = path.resolve(__dirname, '../bin/graph-skill.js');

function setup() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'graph-skill-'));
  const home = path.join(root, 'home'); const proj = path.join(root, 'proj');
  fs.mkdirSync(home); fs.mkdirSync(proj);
  return { home, proj };
}
function run(ctx, args, hosts = '') {
  const env = { ...process.env, HOME: ctx.home, USERPROFILE: ctx.home, GRAPH_SKILL_TEST_HOSTS: hosts };
  return spawnSync(process.execPath, [bin, ...args], { cwd: ctx.proj, encoding: 'utf8', env });
}
const exists = (...p) => fs.existsSync(path.join(...p));

test('install copies the skill to .claude and .agents, with all hosts detected', () => {
  const c = setup();
  const r = run(c, ['install'], 'claude,codex,cursor,opencode,openclaw');
  assert.equal(r.status, 0, r.stderr);
  for (const d of ['.claude', '.agents']) {
    assert.ok(exists(c.proj, d, 'skills/graph/SKILL.md'));
    assert.ok(exists(c.proj, d, 'skills/graph/scripts/graph.py'));
    assert.ok(exists(c.proj, d, 'skills/graph/scripts/preflight.py'));
  }
  assert.match(r.stdout, /claude plugin marketplace add gwaghmar\/graph/);
  assert.match(r.stdout, /claude plugin install graph@graph-skill/);
});

test('install --global writes to the home folder only', () => {
  const c = setup();
  assert.equal(run(c, ['install', '--global']).status, 0);
  assert.ok(exists(c.home, '.claude/skills/graph/SKILL.md'));
  assert.ok(exists(c.home, '.agents/skills/graph/SKILL.md'));
  assert.equal(exists(c.proj, '.claude'), false);
});

test('gitignore ignores only runs and preflight cache', () => {
  const c = setup();
  fs.mkdirSync(path.join(c.proj, '.git'));
  run(c, ['install']);
  const gi = fs.readFileSync(path.join(c.proj, '.gitignore'), 'utf8').split('\n');
  assert.ok(gi.includes('.graph/runs/') && gi.includes('.graph/preflight.json'));
  assert.equal(gi.includes('.graph/'), false);
});

test('uninstall removes skill folders but keeps .graph history', () => {
  const c = setup();
  run(c, ['install']);
  fs.mkdirSync(path.join(c.proj, '.graph/runs/x'), { recursive: true });
  const r = run(c, ['uninstall']);
  assert.equal(r.status, 0, r.stderr);
  assert.equal(exists(c.proj, '.claude/skills/graph'), false);
  assert.equal(exists(c.proj, '.agents/skills/graph'), false);
  assert.ok(exists(c.proj, '.graph/runs/x'));
});

test('a graph folder not made by us is never overwritten or removed', () => {
  const c = setup();
  const own = path.join(c.proj, '.claude/skills/graph');
  fs.mkdirSync(own, { recursive: true });
  fs.writeFileSync(path.join(own, 'SKILL.md'), 'mine');
  run(c, ['install']);
  run(c, ['uninstall']);
  assert.equal(fs.readFileSync(path.join(own, 'SKILL.md'), 'utf8'), 'mine');
});

test('update re-copies owned files and keeps user extras', () => {
  const c = setup();
  run(c, ['install']);
  const dir = path.join(c.proj, '.agents/skills/graph');
  fs.writeFileSync(path.join(dir, 'SKILL.md'), 'stale');
  fs.writeFileSync(path.join(dir, 'extra.txt'), 'keep');
  assert.equal(run(c, ['update']).status, 0);
  assert.notEqual(fs.readFileSync(path.join(dir, 'SKILL.md'), 'utf8'), 'stale');
  assert.ok(exists(dir, 'extra.txt'));
});

test('legacy 0.x files are removed only when they carry the marker', () => {
  const c = setup();
  const w = (rel, text) => { const p = path.join(c.proj, rel); fs.mkdirSync(path.dirname(p), { recursive: true }); fs.writeFileSync(p, text); };
  w('.claude/commands/graph.md', 'run python3 .graph/graph.py init');
  w('.cursor/rules/graph.mdc', 'my own graph rule');
  const r = run(c, ['install']);
  assert.equal(exists(c.proj, '.claude/commands/graph.md'), false);
  assert.ok(exists(c.proj, '.cursor/rules/graph.mdc'));
  assert.match(r.stdout, /\.cursor\/rules\/graph\.mdc was not made by graph-skill/);
});

test('doctor runs and reports hosts', () => {
  const c = setup();
  const r = run(c, ['doctor'], 'claude');
  assert.equal(r.status, 0);
  assert.match(r.stdout, /Hosts detected: claude/);
});
