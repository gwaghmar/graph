const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');
const {spawnSync} = require('child_process');
const bin = path.resolve(__dirname, '../bin/graph-skill.js');
const PY = process.platform === 'win32' ? 'python' : 'python3';

function cleanEnv(home) {
  return {...process.env, HOME: home, PATH: '/usr/bin:/bin', TERM_PROGRAM: '', CLAUDECODE: '', CODEX_HOME: '', OPENCODE: '', CURSOR_TRACE_ID: ''};
}

test('explicit all installs all adapters and runtime', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'graph-skill-'));
  const r = spawnSync(process.execPath, [bin, 'install', '--target', 'all'], {cwd: dir, encoding:'utf8', env: cleanEnv(dir)});
  assert.equal(r.status, 0, r.stderr);
  for (const rel of ['.claude/commands/graph.md','.codex/skills/graph/SKILL.md','.opencode/commands/graph.md','.cursor/rules/graph.mdc','.agents/skills/graph/SKILL.md','.graph/graph.py']) {
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

test('auto-detects OpenClaw and installs the project-agent skill', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'graph-openclaw-'));
  fs.mkdirSync(path.join(dir, '.openclaw'));
  const r = spawnSync(process.execPath, [bin, 'install'], {cwd: dir, encoding:'utf8', env: cleanEnv(dir)});
  assert.equal(r.status, 0, r.stderr);
  assert.equal(fs.existsSync(path.join(dir, '.agents/skills/graph/SKILL.md')), true);
  assert.equal(fs.existsSync(path.join(dir, '.claude/commands/graph.md')), false);
  const host = JSON.parse(fs.readFileSync(path.join(dir, '.graph/host.json'), 'utf8'));
  assert.deepEqual(host.hosts, ['openclaw']);
});

test('uninstalling one host keeps the shared runtime for remaining hosts', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'graph-multi-'));
  let r = spawnSync(process.execPath, [bin, 'install', '--target', 'all'], {cwd: dir, encoding:'utf8', env: cleanEnv(dir)});
  assert.equal(r.status, 0, r.stderr);
  r = spawnSync(process.execPath, [bin, 'uninstall', '--target', 'codex'], {cwd: dir, encoding:'utf8', env: cleanEnv(dir)});
  assert.equal(r.status, 0, r.stderr);
  assert.equal(fs.existsSync(path.join(dir, '.codex/skills/graph/SKILL.md')), false);
  assert.equal(fs.existsSync(path.join(dir, '.graph/graph.py')), true, 'shared runtime must survive');
  assert.equal(fs.existsSync(path.join(dir, '.claude/commands/graph.md')), true);
  const host = JSON.parse(fs.readFileSync(path.join(dir, '.graph/host.json'), 'utf8'));
  assert.deepEqual(host.hosts.sort(), ['claude', 'cursor', 'openclaw', 'opencode']);
  r = spawnSync(process.execPath, [bin, 'uninstall', '--target', 'all'], {cwd: dir, encoding:'utf8', env: cleanEnv(dir)});
  assert.equal(r.status, 0, r.stderr);
  assert.equal(fs.existsSync(path.join(dir, '.graph')), false, 'runtime removed once no hosts remain');
});

test('accepts --target=host equals form and merges host.json across installs', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'graph-eq-'));
  let r = spawnSync(process.execPath, [bin, 'install', '--target=codex'], {cwd: dir, encoding:'utf8', env: cleanEnv(dir)});
  assert.equal(r.status, 0, r.stderr);
  assert.equal(fs.existsSync(path.join(dir, '.codex/skills/graph/SKILL.md')), true);
  r = spawnSync(process.execPath, [bin, 'install', '--target=claude'], {cwd: dir, encoding:'utf8', env: cleanEnv(dir)});
  assert.equal(r.status, 0, r.stderr);
  const host = JSON.parse(fs.readFileSync(path.join(dir, '.graph/host.json'), 'utf8'));
  assert.deepEqual(host.hosts.sort(), ['claude', 'codex']);
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
  r = spawnSync(PY, [py, 'init', 'demo', '--host', 'codex', '--id', 'testrun'], {cwd: dir, encoding:'utf8'});
  assert.equal(r.status, 0, r.stderr);
  spawnSync(PY, [py, 'node', 'testrun', 'worker', '--role', 'worker', '--status', 'running', '--files', 'source.txt'], {cwd: dir});
  spawnSync(PY, [py, 'node', 'testrun', 'worker', '--role', 'worker', '--status', 'failed', '--files', 'source.txt'], {cwd: dir});
  spawnSync(PY, [py, 'node', 'testrun', 'review', '--role', 'reviewer', '--status', 'pending', '--depends-on', 'worker'], {cwd: dir});
  r = spawnSync(PY, [py, 'validate', 'testrun'], {cwd: dir, encoding:'utf8'});
  assert.equal(r.status, 0, r.stderr);
  assert.match(r.stdout, /plan valid/);
  r = spawnSync(PY, [py, 'retry-plan', 'testrun', '--include-dependents'], {cwd: dir, encoding:'utf8'});
  assert.equal(r.status, 0, r.stderr);
  assert.deepEqual(JSON.parse(r.stdout).retry, ['worker', 'review']);
  r = spawnSync(PY, [py, 'cache-put', 'testrun', 'worker', 'done', '--files', 'source.txt'], {cwd: dir, encoding:'utf8'});
  assert.equal(r.status, 0, r.stderr);
  r = spawnSync(PY, [py, 'cache-get', 'testrun', 'worker', '--files', 'source.txt'], {cwd: dir, encoding:'utf8'});
  assert.equal(r.status, 0, r.stderr);
  assert.match(r.stdout, /"result": "done"/);
  r = spawnSync(PY, [py, 'cache-get', 'testrun', 'worker', '--files', 'missing.txt'], {cwd: dir, encoding:'utf8'});
  assert.equal(r.status, 2, 'cache miss exits 2');
  assert.match(r.stdout, /MISS/);
  r = spawnSync(PY, [py, 'quality', 'testrun', '--command', 'exit 0'], {cwd: dir, encoding:'utf8'});
  assert.equal(r.status, 0, r.stderr);
  r = spawnSync(PY, [py, 'quality', 'testrun', '--command', 'exit 1'], {cwd: dir, encoding:'utf8'});
  assert.equal(r.status, 1, 'failing check exits 1');
  r = spawnSync(PY, [py, 'resume'], {cwd: dir, encoding:'utf8'});
  assert.equal(JSON.parse(r.stdout).run, 'testrun');
  r = spawnSync(PY, [py, 'tree', 'testrun'], {cwd: dir, encoding:'utf8'});
  assert.equal(r.status, 0, r.stderr);
  assert.match(r.stdout, /◉ testrun/);
  assert.match(r.stdout, /review .*← worker/);
  const liveReport = fs.readFileSync(path.join(dir, '.graph/runs/testrun/graph.html'), 'utf8');
  assert.match(liveReport, /http-equiv="refresh"/, 'report auto-refreshes while the run is live');
  r = spawnSync(PY, [py, 'node', 'testrun', 'review', '--role', 'reviewer', '--status', 'passed'], {cwd: dir, encoding:'utf8'});
  assert.equal(r.status, 0, r.stderr);
  assert.match(r.stdout, /review/, 'node transition prints the live graph');
  r = spawnSync(PY, [py, 'node', 'testrun', '<img src=x onerror=alert(1)>', '--role', 'worker', '--status', 'pending'], {cwd: dir, encoding:'utf8'});
  assert.equal(r.status, 0, r.stderr);
  const hostileReport = fs.readFileSync(path.join(dir, '.graph/runs/testrun/graph.html'), 'utf8');
  assert.doesNotMatch(hostileReport, /<img/, 'node ids must not inject markup into the report');
  assert.match(hostileReport, /u003cimg/, 'hostile node was recorded, escaped');
  spawnSync(PY, [py, 'node', 'testrun', 'ghost', '--role', 'worker', '--status', 'pending', '--depends-on', 'missing-node'], {cwd: dir});
  r = spawnSync(PY, [py, 'validate', 'testrun'], {cwd: dir, encoding:'utf8'});
  assert.equal(r.status, 1, 'unknown dependency fails validation');
  assert.match(r.stdout, /unknown dependency/);
  r = spawnSync(PY, [py, 'finish', 'testrun', '--status', 'complete'], {cwd: dir, encoding:'utf8'});
  assert.equal(r.status, 0, r.stderr);
  assert.match(r.stdout, /Graph summary/);
  assert.match(r.stdout, /Critical path: worker → review/);
  assert.match(r.stdout, /Report: .*graph\.html/);
  r = spawnSync(PY, [py, 'resume'], {cwd: dir, encoding:'utf8'});
  assert.notEqual(r.status, 0, 'finished runs are not resumable');
  const report = fs.readFileSync(path.join(dir, '.graph/runs/testrun/graph.html'), 'utf8');
  assert.match(report, /Execution graph/);
  assert.match(report, /Node details/);
  assert.match(report, /Cache Hits/);
  assert.match(report, /Timeline/);
  assert.doesNotMatch(report, /http-equiv="refresh"/, 'finished reports stop refreshing');
  r = spawnSync(PY, [py, 'cache-prune', '--days', '0'], {cwd: dir, encoding:'utf8'});
  assert.equal(r.status, 0, r.stderr);
  assert.match(r.stdout, /Removed 1 cache entries/);
});
