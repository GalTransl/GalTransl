// Development-only reference capture. Never imported by the application or packaging.
// node scripts/audit_extraction_tools.mjs <archived-runtime> <python> <backend> <adapter>
import { createHash } from 'node:crypto';
import { mkdtemp, mkdir, readFile, realpath, rm, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

if (process.argv.length !== 6) throw new Error('Expected legacy runtime, Python, backend and adapter paths');
const [legacy, python, backend, adapter] = process.argv.slice(2).map(value => path.resolve(value));
const { extractionTools } = await import(pathToFileURL(path.join(legacy, 'dist/tools.js')).href);
const nativeDir = 'node_modules/@earendil-works/pi-coding-agent/dist/core/tools';
const { createAllToolDefinitions } = await import(pathToFileURL(path.join(legacy, nativeDir, 'index.js')).href);
const tempRoot = await realpath(os.tmpdir());
const root = await mkdtemp(path.join(tempRoot, 'galtransl-tool-audit-'));
const workspace = path.join(root, 'workspace'), game = path.join(root, 'game'), skill = path.join(root, 'skill');
const cases = [];
try {
  for (const dir of [workspace, game, skill, ...['gt_input', 'gt_output', 'original', 'metadata', 'reports', 'scripts'].map(name => path.join(workspace, name))]) await mkdir(dir);
  await mkdir(path.join(workspace, 'reports/paginated'));
  for (let index = 0; index < 105; index++) await writeFile(path.join(workspace, 'reports/paginated', `file-${String(index).padStart(3, '0')}`), '');
  await writeFile(path.join(skill, 'SKILL.md'), 'Synthetic audit Skill');
  await writeFile(path.join(skill, 'audit_module.py'), 'import sys; print("module", sys.argv[1])');
  let onApprove = async () => {};
  const tools = extractionTools({ workspace, gameDir: game, skillDir: skill, pythonPath: python,
    pythonArgs: [backend, '--extraction-python'], pythonRunnerScript: adapter,
    systemPythons: [{ executable: python, args: [] }], emit() {},
    interaction: { approve: (...args) => onApprove(...args), ask: async () => 'fixture A\n中文' } });
  function normalize(value) {
    if (typeof value === 'string') return value.replaceAll(root, '<ROOT>').replaceAll(python, '<PYTHON>');
    if (Array.isArray(value)) return value.map(normalize);
    if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([key, entry]) =>
      [key, key === 'path' || key === 'log_path' ? normalize(entry).split(path.sep).join('/') : normalize(entry)]));
    return value;
  }
  async function capture(name, toolName, args, files = {}, options = {}) {
    for (const [file, content] of Object.entries(files)) {
      await writeFile(path.join(workspace, file), content);
    }
    let approvals = 0;
    onApprove = async () => {
      approvals++;
      for (const [file, content] of Object.entries(options.duringApproval || {})) await writeFile(path.join(workspace, file), content);
    };
    const tool = tools.find(tool => tool.name === toolName);
    let value, error;
    try {
      const result = await tool.execute(`audit-${cases.length}`, args);
      const text = result.content.filter(block => block.type === 'text').map(block => block.text).join('');
      try { value = JSON.parse(text); } catch { value = text; }
    } catch (failure) {
      error = String(failure.message);
      if (options.command) {
        try { error = JSON.parse(error); } catch {}
      }
    }
    if (options.command) {
      for (const result of [value, error]) if (result && typeof result === 'object') {
        delete result.wall_time_seconds;
        delete result.log_path;
      }
    }
    const after = {};
    for (const file of options.observe || []) after[file] = (await readFile(path.join(workspace, file))).toString('hex');
    cases.push(normalize({ name, tool: toolName, args, files, ...options, value, error, approvals, after }));
  }
  await capture('text-lines', 'read_file', { path: 'scripts/text.txt' }, { 'scripts/text.txt': 'one\n二\nthree\n' });
  await capture('text-offset', 'read_file', { path: 'scripts/text.txt', offset: 2, limit: 1 });
  await capture('text-past-end', 'read_file', { path: 'scripts/text.txt', offset: 99 });
  await capture('text-crlf-bom', 'read_file', { path: 'scripts/windows.txt' }, { 'scripts/windows.txt': '\ufeffone\r\n二\r\n' });
  await capture('text-emoji', 'read_file', { path: 'scripts/emoji.txt' }, { 'scripts/emoji.txt': 'abc😀日本語\n' });
  await capture('binary-start', 'read_file', { path: 'scripts/text.txt', hex: true, limit: 1 });
  await capture('binary-eof', 'read_file', { path: 'scripts/text.txt', hex: true, offset: 999 });
  await capture('list-default', 'list_files', {});
  await capture('list-past-end', 'list_files', { offset: 999 });
  await capture('list-page-one', 'list_files', { path: 'reports/paginated' });
  await capture('list-page-two', 'list_files', { path: 'reports/paginated', offset: 100 });
  await capture('write-unicode', 'write_file', { path: 'scripts/write.txt', content: '中文😀\r\n' }, {}, { observe: ['scripts/write.txt'] });
  await capture('edit-unique', 'edit_file', { path: 'scripts/edit.txt', old_text: 'two', new_text: '二' }, { 'scripts/edit.txt': 'one\r\ntwo\r\n' }, { observe: ['scripts/edit.txt'] });
  await capture('edit-ambiguous', 'edit_file', { path: 'scripts/edit.txt', old_text: 'one', new_text: 'x' }, { 'scripts/edit.txt': 'one one' });
  await capture('edit-missing', 'edit_file', { path: 'scripts/edit.txt', old_text: 'missing', new_text: 'x' });
  await capture('edit-conflict', 'edit_file', { path: 'scripts/edit.txt', old_text: 'one', new_text: 'x' }, { 'scripts/edit.txt': 'one' },
    { duringApproval: { 'scripts/edit.txt': 'one external' }, observe: ['scripts/edit.txt'] });
  await capture('edit-literal-dollar', 'edit_file', { path: 'scripts/edit.txt', old_text: 'one', new_text: '$&' }, { 'scripts/edit.txt': 'one' }, { observe: ['scripts/edit.txt'] });
  await capture('python-code', 'run_python', { code: 'import sys; print("中文", sys.argv[1])', args: ['fixture'] }, {}, { command: true });
  await capture('python-system', 'run_python', { runtime: 'system', code: 'print("system fixture")' }, {}, { command: true });
  await capture('python-script', 'run_python', { script: 'scripts/adapter.py', args: ['fixture'] },
    { 'scripts/adapter.py': 'import sys; print("script", sys.argv[1])' }, { command: true });
  await capture('python-module', 'run_python', { module: 'audit_module', args: ['fixture'] }, {}, { command: true });
  await capture('python-nonzero', 'run_python', { code: 'print("nonzero fixture"); raise SystemExit(7)' }, {}, { command: true });
  await capture('shell-command', 'run_shell', { command: process.platform === 'win32' ? "Write-Output 'shell fixture'" : "printf 'shell fixture\\n'" }, {}, { command: true });
  await capture('python-invalid-choice', 'run_python', { code: '', module: 'json' });
  await capture('python-invalid-runtime', 'run_python', { code: '', python_path: python });
  await capture('ask-text', 'ask_user', { question: 'Which fixture?' });
  const native = createAllToolDefinitions(workspace);
  const nativeProbes = [];
  for (const [name, before, edits] of [
    ['edit-batch', 'one\ntwo\n', [{ oldText: 'one', newText: 'ONE' }, { oldText: 'two', newText: 'TWO' }]],
    ['edit-fuzzy', 'quote “value”\n', [{ oldText: 'quote "value"', newText: 'replaced' }]],
    ['edit-crlf-bom', '\ufeffone\r\ntwo\r\n', [{ oldText: 'one\ntwo', newText: 'ONE\nTWO' }]],
  ]) {
    const file = `scripts/native-${name}.txt`;
    await writeFile(path.join(workspace, file), before);
    const result = await native.edit.execute(name, { path: file, edits });
    nativeProbes.push(normalize({ name, before, edits, result, after: (await readFile(path.join(workspace, file))).toString('hex') }));
  }
  const hashes = {};
  for (const file of ['src/tools.ts', 'src/commands.ts', 'src/protocol.ts', 'src/session.ts', 'src/paths.ts', 'dist/tools.js',
    ...['index', 'read', 'edit', 'edit-diff', 'write', 'bash', 'powershell', 'grep', 'find', 'ls'].map(name => `${nativeDir}/${name}.js`)]) {
    hashes[file] = createHash('sha256').update(await readFile(path.join(legacy, file))).digest('hex');
  }
  const pkg = JSON.parse(await readFile(path.join(legacy, 'node_modules/@earendil-works/pi-coding-agent/package.json'), 'utf8'));
  process.stdout.write(JSON.stringify({ reference: { package: pkg.name, version: pkg.version, hashes },
    schemas: tools.map(({ name, parameters }) => ({ name, parameters })),
    native_schemas: Object.values(native).map(({ name, parameters }) => ({ name, parameters })),
    native_probes: nativeProbes,
    cases }, null, 2));
} finally {
  if (path.dirname(root) !== tempRoot || !path.basename(root).startsWith('galtransl-tool-audit-')) throw new Error('Unsafe cleanup path');
  await rm(root, { recursive: true, force: true });
}
