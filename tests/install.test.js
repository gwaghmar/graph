const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');
const {spawnSync} = require('child_process');
const bin = path.resolve(__dirname, '../bin/graph-skill.js');

function cleanEnv(home) {
  return {...process.env, HOME: home, PATH: '/usr/bin:/bin', TERM_PROGRAM: '', CLAUDECODE: '', CODEX_HOME: '', OPENCODE: ''};
}

test('explicit all installs all adapters and runtime', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'graph-skill-'));
  const r = spawnSync(process.execPath, [bin, 'install', '--target', 'all'], {cwd: dir, encoding:'utf8', env: cleanEnv(dir)});
  assert.equal(r.status, 0, r.stderr);
  for (const rel of ['.claude/commands/graph.md','.codex/skills/graph/SKILL.md','.opencode/commands/graph.md','.cursor/rules/graph.mdc','.graph/graph.py']) {
    assert.equal(fs.existsSync(path.join(dir, rel)), true, rel);
  }
});

test('auto-detects Codex and installs only Codex', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'graph-codex-'));
  fs.mkdirSync(path.join(dir, '.codex'));
  const r = spawnSync(process.execPath, [bin, 'install'], {cwd: dir, encoding:'utf8', env: cleanEnv(dir)});
  assert.equal(r.status, 0, r.stderr);
  assert.equal(fs.existsSync(path.join(dir, '.codex/skills/graph/SKILL.md')), true);
  assert.equal(fs.existsSync(path.join(dir, '.claude/commands/graph.md')), false);
  assert.equal(fs.existsSync(path.join(dir, '.cursor/rules/graph.mdc')), false);
  const host = JSON.parse(fs.readFileSync(path.join(dir, '.graph/host.json'), 'utf8'));
  assert.deepEqual(host.hosts, ['codex']);
});

test('stops instead of guessing when no host is detected', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'graph-none-'));
  const r = spawnSync(process.execPath, [bin, 'install'], {cwd: dir, encoding:'utf8', env: cleanEnv(dir)});
  assert.notEqual(r.status, 0);
  assert.match(r.stderr, /Could not detect/);
});

test('runtime supports cache, smart retry, resume, quality, and interactive report', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'graph-runtime-'));
  let r = spawnSync(process.execPath, [bin, 'install', '--target', 'codex'], {cwd: dir, encoding:'utf8', env: cleanEnv(dir)});
  assert.equal(r.status, 0, r.stderr);
  fs.writeFileSync(path.join(dir, 'source.txt'), 'one');
  const py = path.join(dir, '.graph/graph.py');
  r = spawnSync('python3', [py, 'init', 'demo', '--host', 'codex', '--id', 'testrun'], {cwd: dir, encoding:'utf8'});
  assert.equal(r.status, 0, r.stderr);
  spawnSync('python3', [py, 'node', 'testrun', 'worker', '--role', 'worker', '--status', 'running', '--files', 'source.txt'], {cwd: dir});
  spawnSync('python3', [py, 'node', 'testrun', 'worker', '--role', 'worker', '--status', 'failed', '--files', 'source.txt'], {cwd: dir});
  spawnSync('python3', [py, 'node', 'testrun', 'review', '--role', 'reviewer', '--status', 'pending', '--depends-on', 'worker'], {cwd: dir});
  r = spawnSync('python3', [py, 'retry-plan', 'testrun', '--include-dependents'], {cwd: dir, encoding:'utf8'});
  assert.equal(r.status, 0, r.stderr);
  assert.deepEqual(JSON.parse(r.stdout).retry, ['worker', 'review']);
  r = spawnSync('python3', [py, 'cache-put', 'testrun', 'worker', 'done', '--files', 'source.txt'], {cwd: dir, encoding:'utf8'});
  assert.equal(r.status, 0, r.stderr);
  r = spawnSync('python3', [py, 'cache-get', 'testrun', 'worker', '--files', 'source.txt'], {cwd: dir, encoding:'utf8'});
  assert.equal(r.status, 0, r.stderr);
  assert.match(r.stdout, /"result": "done"/);
  r = spawnSync('python3', [py, 'quality', 'testrun', '--command', 'true'], {cwd: dir, encoding:'utf8'});
  assert.equal(r.status, 0, r.stderr);
  r = spawnSync('python3', [py, 'resume'], {cwd: dir, encoding:'utf8'});
  assert.equal(JSON.parse(r.stdout).run, 'testrun');
  const report = fs.readFileSync(path.join(dir, '.graph/runs/testrun/graph.html'), 'utf8');
  assert.match(report, /Execution graph/);
  assert.match(report, /Node details/);
  assert.match(report, /Cache Hits/);
});
