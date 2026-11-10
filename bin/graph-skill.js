#!/usr/bin/env node
'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');

const SRC = path.resolve(__dirname, '..', 'skills', 'graph');
const OWNER_FILE = '.graph-skill-owner';
// Text that only files written by graph-skill 0.x contain.
const LEGACY_MARKER = /\.graph\/graph\.py|graph-skill/;
const LEGACY_PATHS = [
  '.claude/commands/graph.md', '.claude/agents/graph-planner.md', '.claude/agents/graph-reviewer.md',
  '.claude/agents/graph-visualizer.md', '.claude/agents/graph-worker.md',
  '.codex/skills/graph', '.cursor/rules/graph.mdc', '.opencode/commands/graph.md'
];
const MARKETPLACE_CMDS = ['claude plugin marketplace add gwaghmar/graph', 'claude plugin install graph@graph-skill'];

function commandExists(name) {
  // Test hook: GRAPH_SKILL_TEST_HOSTS="claude,openclaw" replaces the PATH lookup.
  if (process.env.GRAPH_SKILL_TEST_HOSTS !== undefined) {
    return process.env.GRAPH_SKILL_TEST_HOSTS.split(',').map((x) => x.trim()).includes(name);
  }
  const exts = process.platform === 'win32' ? (process.env.PATHEXT || '.EXE;.CMD;.BAT').split(';') : [''];
  for (const dir of (process.env.PATH || '').split(path.delimiter)) {
    if (!dir) continue;
    for (const ext of exts) {
      try { if (fs.statSync(path.join(dir, name + ext)).isFile()) return true; } catch (e) { /* not here */ }
    }
  }
  return false;
}

function detectHosts(cwd, home) {
  const has = (p) => fs.existsSync(path.join(cwd, p)) || fs.existsSync(path.join(home, p));
  const hosts = [];
  if (has('.claude') || commandExists('claude')) hosts.push('claude');
  if (has('.codex') || commandExists('codex')) hosts.push('codex');
  if (has('.opencode') || commandExists('opencode')) hosts.push('opencode');
  if (has('.cursor') || commandExists('cursor')) hosts.push('cursor');
  if (has('.openclaw') || commandExists('openclaw')) hosts.push('openclaw');
  return hosts;
}

// Skill folders for this scope. Codex, Cursor, OpenCode and OpenClaw read .agents/skills.
function targets(base) {
  return [path.join(base, '.claude', 'skills', 'graph'), path.join(base, '.agents', 'skills', 'graph')];
}

function listFiles(dir, rel = '') {
  const out = [];
  for (const e of fs.readdirSync(path.join(dir, rel), { withFileTypes: true })) {
    if (e.name === '__pycache__') continue;
    const r = path.join(rel, e.name);
    if (e.isDirectory()) out.push(...listFiles(dir, r)); else out.push(r);
  }
  return out;
}

function installTo(dest) {
  if (fs.existsSync(dest) && !fs.existsSync(path.join(dest, OWNER_FILE))) {
    console.log(`Skipped ${dest}: a folder not made by graph-skill is already there. Move it, then run install again.`);
    return false;
  }
  for (const rel of listFiles(SRC)) {
    fs.mkdirSync(path.dirname(path.join(dest, rel)), { recursive: true });
    fs.copyFileSync(path.join(SRC, rel), path.join(dest, rel));
  }
  fs.writeFileSync(path.join(dest, OWNER_FILE), JSON.stringify({ installedAt: new Date().toISOString() }) + '\n', 'utf8');
  console.log(`Installed Graph -> ${dest}`);
  return true;
}

function ensureGitignore(cwd) {
  const gi = path.join(cwd, '.gitignore');
  if (!fs.existsSync(path.join(cwd, '.git')) && !fs.existsSync(gi)) return;
  let text = '';
  try { text = fs.readFileSync(gi, 'utf8'); } catch (e) { /* new file */ }
  const have = new Set(text.split(/\r?\n/).map((l) => l.trim()));
  const add = ['.graph/runs/', '.graph/preflight.json'].filter((l) => !have.has(l));
  if (!add.length) return;
  fs.writeFileSync(gi, text + (text && !text.endsWith('\n') ? '\n' : '') + add.join('\n') + '\n', 'utf8');
  console.log(`Added ${add.join(', ')} to .gitignore`);
}

function migrateLegacy(cwd) {
  for (const rel of LEGACY_PATHS) {
    const p = path.join(cwd, rel);
    if (!fs.existsSync(p)) continue;
    const file = fs.statSync(p).isDirectory() ? path.join(p, 'SKILL.md') : p;
    let text = '';
    try { text = fs.readFileSync(file, 'utf8'); } catch (e) { /* unreadable */ }
    if (LEGACY_MARKER.test(text)) {
      fs.rmSync(p, { recursive: true, force: true });
      console.log(`Removed old 0.x file ${rel}`);
    } else {
      console.log(`Note: ${rel} was not made by graph-skill, so it was left in place.`);
    }
  }
}

function install(flags, cwd, home) {
  const hosts = detectHosts(cwd, home);
  const base = flags.global ? home : cwd;
  console.log(`Hosts detected: ${hosts.length ? hosts.join(', ') : 'none (installing the standard folders anyway)'}`);
  targets(base).forEach(installTo);
  if (!flags.global) { ensureGitignore(cwd); migrateLegacy(cwd); }
  console.log('\nOn Claude Code you can also install it as a plugin:');
  MARKETPLACE_CMDS.forEach((c) => console.log(`  ${c}`));
}

function update(flags, cwd, home) {
  const base = flags.global ? home : cwd;
  let n = 0;
  for (const dest of targets(base)) {
    if (!fs.existsSync(path.join(dest, OWNER_FILE))) continue;
    installTo(dest); n++;
  }
  if (!n) console.log('Nothing to update. Run "graph-skill install" first.');
}

function uninstall(flags, cwd, home) {
  const base = flags.global ? home : cwd;
  for (const dest of targets(base)) {
    if (!fs.existsSync(dest)) continue;
    if (!fs.existsSync(path.join(dest, OWNER_FILE))) { console.log(`Left ${dest}: not made by graph-skill.`); continue; }
    fs.rmSync(dest, { recursive: true, force: true });
    console.log(`Removed ${dest}`);
  }
  console.log('Run history in .graph/ was kept.');
}

function doctor(flags, cwd, home) {
  const hosts = detectHosts(cwd, home);
  console.log(`Hosts detected: ${hosts.length ? hosts.join(', ') : 'none'}`);
  for (const base of [cwd, home]) {
    for (const dest of targets(base)) {
      console.log(`${fs.existsSync(path.join(dest, OWNER_FILE)) ? 'ok     ' : 'missing'} ${dest}`);
    }
  }
  const py = ['python3', 'python', 'py'].find(commandExists);
  console.log(py ? `ok      Python found (${py})` : 'missing Python 3: install it so the runtime can run.');
}

function help() {
  console.log('graph-skill\n\nUsage:\n  graph-skill install [--global]\n  graph-skill update [--global]\n  graph-skill uninstall [--global]\n  graph-skill doctor');
}

function main(argv) {
  const command = argv[0] || 'help';
  const flags = { global: argv.includes('--global') };
  const cwd = process.cwd();
  const home = os.homedir();
  const fns = { install, update, uninstall, doctor };
  if (fns[command]) fns[command](flags, cwd, home); else help();
}

try { main(process.argv.slice(2)); } catch (err) {
  console.error(`graph-skill: ${err.message}`);
  process.exit(1);
}
