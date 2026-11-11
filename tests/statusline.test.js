const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawnSync } = require('child_process');
const bin = path.resolve(__dirname, '../bin/graph-skill.js');
const script = path.resolve(__dirname, '../skills/graph/scripts/statusline.py');

function setup() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'graph-skill-sl-'));
  const home = path.join(root, 'home'); const proj = path.join(root, 'proj');
  fs.mkdirSync(path.join(home, '.claude'), { recursive: true }); fs.mkdirSync(proj);
  return { home, proj, settings: path.join(home, '.claude', 'settings.json') };
}
function run(ctx, args) {
  const env = { ...process.env, HOME: ctx.home, USERPROFILE: ctx.home, GRAPH_SKILL_TEST_HOSTS: 'claude' };
  return spawnSync(process.execPath, [bin, 'statusline', ...args], { cwd: ctx.proj, encoding: 'utf8', env });
}
const backups = (c) => fs.readdirSync(path.dirname(c.settings)).filter((f) => f.includes('graph-skill-backup'));

test('statusline without --enable only explains', () => {
  const c = setup();
  const r = run(c, []);
  assert.equal(r.status, 0); assert.match(r.stdout, /opt-in/);
  assert.ok(!fs.existsSync(c.settings));
});

test('--enable prints the snippet and writes nothing without --yes', () => {
  const c = setup();
  fs.writeFileSync(c.settings, '{"theme":"dark"}\n');
  const r = run(c, ['--enable', '--script', script]);
  assert.equal(r.status, 0, r.stderr);
  assert.match(r.stdout, /"statusLine"/); assert.match(r.stdout, /statusline\.py/); assert.match(r.stdout, /Nothing was written/);
  assert.equal(fs.readFileSync(c.settings, 'utf8'), '{"theme":"dark"}\n');
  assert.equal(backups(c).length, 0);
  assert.ok(!r.stdout.includes(c.home) || r.stdout.includes('~'), 'home shows as ~');
});

test('--enable --yes creates settings when none exist', () => {
  const c = setup();
  const r = run(c, ['--enable', '--yes', '--script', script]);
  assert.equal(r.status, 0, r.stderr);
  const s = JSON.parse(fs.readFileSync(c.settings, 'utf8'));
  assert.equal(s.statusLine.type, 'command'); assert.match(s.statusLine.command, /statusline\.py/);
  assert.equal(backups(c).length, 0);
});

test('--yes backs up, keeps other keys and wraps an existing statusLine command', () => {
  const c = setup();
  const before = { theme: 'dark', hooks: { a: 1 }, statusLine: { type: 'command', command: "echo it's mine", padding: 2 } };
  fs.writeFileSync(c.settings, JSON.stringify(before));
  const r = run(c, ['--enable', '--yes', '--script', script]);
  assert.equal(r.status, 0, r.stderr);
  const s = JSON.parse(fs.readFileSync(c.settings, 'utf8'));
  assert.equal(s.theme, 'dark'); assert.deepEqual(s.hooks, { a: 1 }); assert.equal(s.statusLine.padding, 2);
  assert.match(s.statusLine.command, /statusline\.py/); assert.ok(s.statusLine.command.includes('echo it'), s.statusLine.command);
  const b = backups(c);
  assert.equal(b.length, 1);
  assert.deepEqual(JSON.parse(fs.readFileSync(path.join(path.dirname(c.settings), b[0]), 'utf8')), before);
  // running it again does not wrap twice
  const again = run(c, ['--enable', '--yes', '--script', script]);
  assert.match(again.stdout, /Already enabled/);
  assert.deepEqual(JSON.parse(fs.readFileSync(c.settings, 'utf8')), s);
});

test('wrapped command really draws the old status line', { skip: process.platform === 'win32' }, () => {
  const c = setup();
  fs.writeFileSync(c.settings, JSON.stringify({ statusLine: { type: 'command', command: 'echo OLD-LINE' } }));
  run(c, ['--enable', '--yes', '--script', script]);
  const cmd = JSON.parse(fs.readFileSync(c.settings, 'utf8')).statusLine.command.replace(/^~/, c.home).replace(' ~/', ' ' + c.home + '/');
  const r = spawnSync(cmd, { shell: true, input: '{}', encoding: 'utf8', env: { ...process.env, HOME: c.home } });
  assert.equal(r.stdout.trim(), 'OLD-LINE');
});

test('settings that are not plain JSON are never touched', () => {
  const c = setup();
  fs.writeFileSync(c.settings, '{ // comment\n "a": 1 }');
  const r = run(c, ['--enable', '--yes', '--script', script]);
  assert.equal(r.status, 1); assert.match(r.stderr, /not plain JSON/);
  assert.equal(fs.readFileSync(c.settings, 'utf8'), '{ // comment\n "a": 1 }');
});

test('a non-command statusLine and a missing script are refused', () => {
  const c = setup();
  fs.writeFileSync(c.settings, JSON.stringify({ statusLine: { type: 'text', text: 'x' } }));
  assert.equal(run(c, ['--enable', '--yes', '--script', script]).status, 1);
  const c2 = setup();
  const r = run(c2, ['--enable', '--yes']);
  assert.equal(r.status, 1); assert.match(r.stderr, /statusline\.py not found/);
  assert.ok(!fs.existsSync(c2.settings));
});
