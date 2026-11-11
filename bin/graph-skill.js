#!/usr/bin/env node
'use strict';

const crypto = require('crypto');
const fs = require('fs');
const os = require('os');
const path = require('path');

const SRC = path.resolve(__dirname, '..', 'skills', 'graph');
const OWNER_FILE = '.graph-skill-owner';
// Text that only files written by graph-skill 0.x contain.
const LEGACY_MARKER = /\.graph\/graph\.py|graph-skill/;
// SHA-256 of the 0.x agent files (LF line endings). They have no marker text, so match their exact content.
const LEGACY_HASHES = new Set([
  '31f5c02a506e02e453a742ffa19d93fd42ad050e0108c32411851fa94fe2fded', '42c245261d7959c100b3d09be81cc19d98cd21dfab9c8e343fa420494a03a609',
  '3f3b9ee860ee94d6fce67df2a9d3c74c769e6a4db051655490a41e74154d1dd6', 'a8c8a53e6f95ae72799af33909b226143a8ffd6dadc85e82f1c5db43dc6d05d4',
  'b108e26aa7e2b0a55533db28d73ead7620a0bd475928c03d53300b685e3d793a', '08bb40f4b3169d70b4fa24e5cb93fa3d3d09eec5c7ea180396dcb99ac9185d51'
]);
const LEGACY_PATHS = [
  '.claude/commands/graph.md', '.claude/agents/graph-planner.md', '.claude/agents/graph-reviewer.md',
  '.claude/agents/graph-visualizer.md', '.claude/agents/graph-worker.md',
  '.codex/skills/graph', '.agents/skills/graph', '.cursor/rules/graph.mdc', '.opencode/commands/graph.md'
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
  const files = listFiles(SRC).map((r) => r.split(path.sep).join('/'));
  fs.writeFileSync(path.join(dest, OWNER_FILE), JSON.stringify({ installedAt: new Date().toISOString(), files }) + '\n', 'utf8');
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
    if (fs.existsSync(path.join(p, OWNER_FILE))) continue; // made by 1.x, not legacy
    const file = fs.statSync(p).isDirectory() ? path.join(p, 'SKILL.md') : p;
    let text = '';
    try { text = fs.readFileSync(file, 'utf8'); } catch (e) { /* unreadable */ }
    const sha = crypto.createHash('sha256').update(text.replace(/\r\n/g, '\n')).digest('hex');
    if (LEGACY_MARKER.test(text) || LEGACY_HASHES.has(sha)) {
      fs.rmSync(file, { force: true }); // only the matching file, not other files in a folder
      if (file !== p) { try { fs.rmdirSync(p); } catch (e) { /* other files remain */ } }
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
  if (!flags.global) migrateLegacy(cwd); // first, so an old .agents/skills/graph does not block the new install
  targets(base).forEach(installTo);
  if (!flags.global) ensureGitignore(cwd);
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

// Remove only files the installer listed, then empty folders. Keep the folder if the user added files.
function removeOwned(dest) {
  let files;
  try { files = JSON.parse(fs.readFileSync(path.join(dest, OWNER_FILE), 'utf8')).files; } catch (e) { /* old marker */ }
  if (!Array.isArray(files)) files = listFiles(SRC); // older installs: the current skill file list
  for (const rel of files) {
    const p = path.resolve(dest, rel);
    if (!p.startsWith(path.resolve(dest) + path.sep)) continue;
    try { fs.unlinkSync(p); } catch (e) { /* already gone */ }
  }
  fs.rmSync(path.join(dest, OWNER_FILE), { force: true });
  fs.rmSync(path.join(dest, 'scripts', '__pycache__'), { recursive: true, force: true });
  const prune = (d) => {
    for (const e of fs.readdirSync(d, { withFileTypes: true })) if (e.isDirectory()) prune(path.join(d, e.name));
    if (!fs.readdirSync(d).length) fs.rmdirSync(d);
  };
  prune(dest);
  if (fs.existsSync(dest)) console.log(`Removed graph-skill files from ${dest}. The folder was kept because it holds other files.`);
  else console.log(`Removed ${dest}`);
}

function uninstall(flags, cwd, home) {
  const base = flags.global ? home : cwd;
  for (const dest of targets(base)) {
    if (!fs.existsSync(dest)) continue;
    if (!fs.existsSync(path.join(dest, OWNER_FILE))) { console.log(`Left ${dest}: not made by graph-skill.`); continue; }
    removeOwned(dest);
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

// ---- statusline: opt-in helper that lets Graph read Claude Code usage left ----
function tilde(text, home) {
  return home ? text.split(home).join('~') : text;
}

function shellQuote(text) {
  if (process.platform === 'win32') return '"' + text.replace(/"/g, '\\"') + '"';
  return "'" + text.replace(/'/g, "'\\''") + "'";
}

function pythonLauncher() {
  if (process.platform === 'win32') return commandExists('py') ? 'py -3' : 'python';
  return commandExists('python3') || !commandExists('python') ? 'python3' : 'python';
}

// Path as written into settings.json. Under the home folder on Mac/Linux it is ~/..., which the shell expands.
function commandPath(script, home) {
  const safe = /^[A-Za-z0-9_./-]+$/;
  if (process.platform !== 'win32' && script.startsWith(home + path.sep)) {
    const rel = script.slice(home.length + 1).split(path.sep).join('/');
    if (safe.test(rel)) return '~/' + rel;
  }
  const out = process.platform === 'win32' ? script.split(path.sep).join('/') : script;  // forward slashes also work under bash
  return /[\s"'&|<>^()]/.test(out) ? shellQuote(out) : out;
}

function findStatuslineScript(flags, cwd, home) {
  if (flags.script) return path.resolve(flags.script);
  for (const base of [home, cwd]) {
    const p = path.join(base, '.claude', 'skills', 'graph', 'scripts', 'statusline.py');
    if (fs.existsSync(p)) return p;
  }
  return null;
}

function statusline(flags, cwd, home) {
  if (!flags.enable) {
    console.log('graph-skill statusline --enable [--yes]\n\nThis is opt-in. It lets Graph read how much Claude Code usage is left.\n' +
      'Without --yes it only prints the change to ~/.claude/settings.json. With --yes it writes it, after a backup.\n' +
      'The helper stores only usage percentages, reset times, the model id and the context size. No message content.');
    return;
  }
  const script = findStatuslineScript(flags, cwd, home);
  if (!script || !fs.existsSync(script)) {
    console.error('graph-skill: statusline.py not found. Run "graph-skill install --global" first, or pass --script <path>.');
    process.exitCode = 1; return;
  }
  let settingsPath = path.join(home, '.claude', 'settings.json');
  try { settingsPath = fs.realpathSync(settingsPath); } catch (e) { /* no file yet: write at the plain path */ }  // dotfile managers symlink this file
  let settings = {};
  if (fs.existsSync(settingsPath)) {
    try { settings = JSON.parse(fs.readFileSync(settingsPath, 'utf8').replace(/^﻿/, '')); } catch (e) {
      console.error(`graph-skill: ${tilde(settingsPath, home)} is not plain JSON, so nothing was changed. Add the statusLine entry by hand.`);
      process.exitCode = 1; return;
    }
    if (!settings || typeof settings !== 'object' || Array.isArray(settings)) {
      console.error(`graph-skill: ${tilde(settingsPath, home)} is not a JSON object, so nothing was changed.`);
      process.exitCode = 1; return;
    }
  }
  const base = `${pythonLauncher()} ${commandPath(script, home)}`;
  const old = settings.statusLine;
  let entry;
  if (old !== undefined && old !== null) {
    if (typeof old !== 'object' || old.type !== 'command' || typeof old.command !== 'string' || !old.command.trim()) {
      console.error('graph-skill: the existing statusLine is not a plain command, so it was left alone. Wrap it by hand: pass its command as the argument of statusline.py.');
      process.exitCode = 1; return;
    }
    if (/skills[\\/]graph[\\/]scripts[\\/]statusline\.py/.test(old.command)) {
      console.log('Already enabled: the statusLine already runs the Graph helper. Nothing to do.');
      return;
    }
    entry = Object.assign({}, old, { type: 'command', command: `${base} ${shellQuote(old.command)}` });
  } else {
    entry = { type: 'command', command: base };
  }
  console.log(`Settings change for ${tilde(settingsPath, home)}:`);
  console.log(JSON.stringify({ statusLine: entry }, null, 2));
  if (old) console.log('Your existing statusLine command is kept: the helper runs it and shows its output.');
  if (!flags.yes) {
    console.log('\nNothing was written. Run again with --yes to apply it. A backup is made first.');
    return;
  }
  if (fs.existsSync(settingsPath)) {
    const stamp = new Date().toISOString().replace(/[-:T]/g, '').slice(0, 14);
    const backup = `${settingsPath}.graph-skill-backup-${stamp}`;
    fs.copyFileSync(settingsPath, backup);
    console.log(`Backup: ${tilde(backup, home)}`);
  } else {
    fs.mkdirSync(path.dirname(settingsPath), { recursive: true });
  }
  settings.statusLine = entry;
  const tmp = `${settingsPath}.graph-skill-tmp`;
  fs.writeFileSync(tmp, JSON.stringify(settings, null, 2) + '\n', 'utf8');
  try { fs.chmodSync(tmp, fs.statSync(settingsPath).mode & 0o7777); } catch (e) { /* new file: keep the default mode */ }
  fs.renameSync(tmp, settingsPath);
  console.log(`Enabled. Restart Claude Code. To undo, restore the backup or remove the statusLine entry.`);
}

function help() {
  console.log('graph-skill\n\nUsage:\n  graph-skill install [--global]\n  graph-skill update [--global]\n  graph-skill uninstall [--global]\n  graph-skill doctor\n  graph-skill statusline --enable [--yes] [--script path]');
}

function main(argv) {
  const command = argv[0] || 'help';
  const flags = { global: argv.includes('--global'), enable: argv.includes('--enable'), yes: argv.includes('--yes') };
  const si = argv.indexOf('--script');
  if (si !== -1) {
    if (!argv[si + 1] || argv[si + 1].startsWith('--')) throw new Error('--script needs a path.');
    flags.script = argv[si + 1];
  }
  const cwd = process.cwd();
  const home = os.homedir();
  const fns = { install, update, uninstall, doctor, statusline };
  if (fns[command]) fns[command](flags, cwd, home); else help();
}

try { main(process.argv.slice(2)); } catch (err) {
  console.error(`graph-skill: ${err.message}`);
  process.exit(1);
}
