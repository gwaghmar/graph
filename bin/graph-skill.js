#!/usr/bin/env node
'use strict';

const fs = require('fs');
const path = require('path');
const { spawnSync } = require('child_process');

const ROOT = path.resolve(__dirname, '..');
const args = process.argv.slice(2);
const command = args[0] || 'help';
const cwd = process.cwd();

const TARGETS = {
  claude: ['templates/claude-code', '.claude'],
  codex: ['templates/codex', '.codex'],
  opencode: ['templates/opencode', '.opencode'],
  cursor: ['templates/cursor', '.cursor']
};

function copyDir(src, dest) {
  fs.mkdirSync(dest, { recursive: true });
  for (const entry of fs.readdirSync(src, { withFileTypes: true })) {
    if (entry.name === '__pycache__') continue;
    const from = path.join(src, entry.name);
    const to = path.join(dest, entry.name);
    if (entry.isDirectory()) copyDir(from, to);
    else fs.copyFileSync(from, to);
  }
}

function commandExists(name) {
  const probe = process.platform === 'win32' ? 'where' : 'sh';
  const probeArgs = process.platform === 'win32' ? [name] : ['-lc', `command -v ${name} >/dev/null 2>&1`];
  return spawnSync(probe, probeArgs, { stdio: 'ignore' }).status === 0;
}

function hasAny(paths) {
  return paths.some((p) => fs.existsSync(path.resolve(cwd, p)) || fs.existsSync(path.resolve(process.env.HOME || '', p)));
}

function detectHosts() {
  const env = process.env;
  // Session env vars are set only inside a live host session — trust them alone.
  if (env.CLAUDECODE) return ['claude'];
  if (env.CODEX_HOME) return ['codex'];
  if (env.OPENCODE) return ['opencode'];
  if ((env.TERM_PROGRAM || '').toLowerCase().includes('cursor') || env.CURSOR_TRACE_ID) return ['cursor'];

  const detected = [];
  if (hasAny(['.claude', '.claude.json']) || commandExists('claude')) detected.push('claude');
  if (hasAny(['.codex']) || commandExists('codex')) detected.push('codex');
  if (hasAny(['.opencode']) || commandExists('opencode')) detected.push('opencode');
  if (hasAny(['.cursor']) || commandExists('cursor')) detected.push('cursor');

  return detected;
}

function resolveTarget(requested) {
  if (requested && requested !== 'auto') {
    if (!TARGETS[requested] && requested !== 'all') throw new Error(`Unknown target: ${requested}`);
    return requested === 'all' ? Object.keys(TARGETS) : [requested];
  }

  const detected = detectHosts();
  if (detected.length === 1) return detected;
  if (detected.length === 0) {
    throw new Error('Could not detect Claude Code, Codex, OpenCode, or Cursor. Use --target <host>.');
  }
  throw new Error(`Multiple hosts detected (${detected.join(', ')}). Run inside the intended host or use --target <host>.`);
}

function getFlag(name) {
  const index = args.indexOf(name);
  return index >= 0 ? args[index + 1] : undefined;
}

function install(requested) {
  const selected = resolveTarget(getFlag('--target') || requested || 'auto');
  for (const name of selected) {
    const [src, dest] = TARGETS[name];
    copyDir(path.join(ROOT, src), path.join(cwd, dest));
    console.log(`Installed Graph for ${name} -> ${path.join(cwd, dest)}`);
  }
  copyDir(path.join(ROOT, 'core'), path.join(cwd, '.graph'));
  fs.writeFileSync(path.join(cwd, '.graph', 'host.json'), JSON.stringify({ hosts: selected, installedAt: new Date().toISOString() }, null, 2) + '\n');
  console.log(`Installed shared runtime -> ${path.join(cwd, '.graph')}`);
}

function uninstall(requested) {
  const selected = resolveTarget(getFlag('--target') || requested || 'auto');
  const dirs = {
    claude: ['.claude/commands/graph.md', '.claude/agents/graph-planner.md', '.claude/agents/graph-reviewer.md', '.claude/agents/graph-worker.md', '.claude/agents/graph-visualizer.md'],
    codex: ['.codex/skills/graph'],
    opencode: ['.opencode/commands/graph.md'],
    cursor: ['.cursor/rules/graph.mdc']
  };
  for (const target of selected) {
    for (const rel of dirs[target]) fs.rmSync(path.join(cwd, rel), { recursive: true, force: true });
    console.log(`Removed Graph files for ${target}`);
  }
  fs.rmSync(path.join(cwd, '.graph'), { recursive: true, force: true });
}

function detect() {
  const detected = detectHosts();
  console.log(JSON.stringify({ detected, cwd }, null, 2));
}

function help() {
  console.log(`graph-skill\n\nUsage:\n  npx graph-skill install              # auto-detect one host\n  npx graph-skill install --target codex\n  npx graph-skill uninstall            # auto-detect one host\n  npx graph-skill detect\n\nTargets: claude, codex, opencode, cursor, all\n`);
}

try {
  if (command === 'install') install(args[1] && !args[1].startsWith('--') ? args[1] : 'auto');
  else if (command === 'uninstall') uninstall(args[1] && !args[1].startsWith('--') ? args[1] : 'auto');
  else if (command === 'detect') detect();
  else help();
} catch (err) {
  console.error(`graph-skill: ${err.message}`);
  process.exit(1);
}
