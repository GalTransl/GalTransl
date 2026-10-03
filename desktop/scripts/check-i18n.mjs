import { existsSync, readFileSync, readdirSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import ts from 'typescript';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const catalogRoot = resolve(root, 'src/i18n/locales');
export const interpolationNames = (value) => [...new Set([...value.matchAll(/{{-?\s*([\w.]+)(?:\s*,[^}]*)?\s*}}/g)].map((match) => match[1]))].sort();
const richTags = (value) => [...value.matchAll(/<\/?\d+\s*\/?>/g)].map((match) => match[0]).sort();

export function flattenCatalog(catalog, prefix = '', errors = []) {
  const leaves = new Map();
  if (!catalog || typeof catalog !== 'object' || Array.isArray(catalog)) {
    errors.push(`${prefix || 'root'}: expected an object`);
    return leaves;
  }
  for (const [key, value] of Object.entries(catalog)) {
    const name = prefix ? `${prefix}.${key}` : key;
    if (typeof value === 'string') leaves.set(name, value);
    else if (value && typeof value === 'object' && !Array.isArray(value)) {
      for (const entry of flattenCatalog(value, name, errors)) leaves.set(...entry);
    } else errors.push(`${name}: expected a string or object`);
  }
  return leaves;
}

export function validateCatalog(source, translation) {
  const errors = [];
  const zh = flattenCatalog(source, '', errors);
  const en = flattenCatalog(translation, '', errors);
  const pending = [];
  const missing = [];
  for (const [key, value] of en) {
    if (!zh.has(key)) errors.push(`${key}: unknown translation key`);
    else if (value.trim()) {
      if (JSON.stringify(interpolationNames(zh.get(key))) !== JSON.stringify(interpolationNames(value))) errors.push(`${key}: interpolation names differ`);
      if (JSON.stringify(richTags(zh.get(key))) !== JSON.stringify(richTags(value))) errors.push(`${key}: rich text placeholders differ`);
    }
  }
  for (const [key, value] of zh) {
    if (!value.trim()) errors.push(`${key}: Chinese fallback must not be empty`);
    if (!en.has(key)) missing.push(key);
    else if (!en.get(key).trim()) pending.push(key);
  }
  return { errors, pending, missing, sourceKeys: zh };
}

function sourceFiles(directory) {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const file = resolve(directory, entry.name);
    return entry.isDirectory() ? sourceFiles(file) : /\.tsx?$/.test(file) ? [file] : [];
  });
}

export function checkResources() {
  const errors = [];
  const allKeys = new Set();
  const report = [];
  const namespaces = readdirSync(resolve(catalogRoot, 'zh-CN')).filter((file) => file.endsWith('.json'));
  for (const name of readdirSync(resolve(catalogRoot, 'en')).filter((file) => file.endsWith('.json'))) {
    if (!namespaces.includes(name)) errors.push(`${name}: unknown English namespace`);
  }
  for (const name of namespaces) {
    const ns = name.slice(0, -5);
    const zh = JSON.parse(readFileSync(resolve(catalogRoot, 'zh-CN', name), 'utf8'));
    const englishFile = resolve(catalogRoot, 'en', name);
    const en = existsSync(englishFile) ? JSON.parse(readFileSync(englishFile, 'utf8')) : {};
    const result = validateCatalog(zh, en);
    errors.push(...result.errors.map((error) => `${ns}:${error}`));
    for (const key of result.sourceKeys.keys()) allKeys.add(`${ns}:${key}`);
    report.push({ namespace: ns, total: result.sourceKeys.size, pending: result.pending, missing: result.missing });
  }
  const exclusions = JSON.parse(readFileSync(resolve(root, 'src/i18n/audit-exclusions.json'), 'utf8'));
  const referenced = new Set();
  const inspectKey = (key, where) => {
    if (!/^(common|settings|projects|agent|config|plugins|errors):/.test(key)) return;
    referenced.add(key);
    if (!allKeys.has(key)) errors.push(`${where}: unknown resource key ${key}`);
  };
  for (const file of sourceFiles(resolve(root, 'src'))) {
    const relative = file.slice(root.length + 1).replaceAll('\\', '/');
    const source = readFileSync(file, 'utf8');
    const ast = ts.createSourceFile(file, source, ts.ScriptTarget.Latest, true, file.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS);
    function visit(node) {
      const where = `${relative}:${ast.getLineAndCharacterOfPosition(node.getStart(ast)).line + 1}`;
      if (ts.isStringLiteralLike(node)) {
        inspectKey(node.text, where);
        if (/[\u3400-\u9fff]/.test(node.text) && !relative.startsWith('src/i18n/')) {
          if (!exclusions.some((item) => item.file === relative && item.text === node.text)) errors.push(`${where}: unreviewed hardcoded Chinese text`);
        }
      }
      if (ts.isJsxText(node) && /[a-zA-Z\u3400-\u9fff]/.test(node.text)) errors.push(`${where}: hardcoded JSX text`);
      if (ts.isTemplateExpression(node) && /[\u3400-\u9fff]/.test(node.getText(ast))) errors.push(`${where}: unreviewed Chinese template`);
      if (ts.isCallExpression(node) && ['t', 'translate', 'message', 'uiMessage'].includes(node.expression.getText(ast)) && ts.isStringLiteral(node.arguments[0])) {
        // Object literals expose the named parameters at call sites without evaluating code.
        const options = node.arguments[1];
        if (options && ts.isObjectLiteralExpression(options)) {
          const names = new Set(options.properties.map((property) => property.name?.getText(ast)));
          const key = node.arguments[0].text;
          const [ns, ...parts] = key.split(/[:.]/);
          const resource = JSON.parse(readFileSync(resolve(catalogRoot, 'zh-CN', `${ns}.json`), 'utf8'));
          const value = parts.reduce((object, part) => object?.[part], resource);
          if (typeof value === 'string') for (const name of interpolationNames(value)) if (!names.has(name)) errors.push(`${where}: missing interpolation parameter ${name}`);
        }
      }
      ts.forEachChild(node, visit);
    }
    visit(ast);
  }
  const pluginMap = JSON.parse(readFileSync(resolve(root, 'src/i18n/plugin-map.json'), 'utf8'));
  function inspectMap(object) {
    for (const value of Object.values(object)) {
      if (value && typeof value === 'object') inspectMap(value);
      else if (typeof value === 'string') inspectKey(value, 'plugin-map.json');
    }
  }
  inspectMap(pluginMap);
  return { errors, report, referenced };
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const { errors, report, referenced } = checkResources();
  for (const item of report) {
    console.log(`${item.namespace}: ${item.total} keys, ${item.pending.length} pending, ${item.missing.length} missing`);
    if (process.argv.includes('--pending')) for (const key of [...item.pending, ...item.missing]) console.log(`  ${item.namespace}:${key}`);
  }
  for (const error of errors) console.error(error);
  console.log(`${referenced.size} source keys checked; ${errors.length} errors. Empty English entries fall back to Chinese.`);
  process.exitCode = errors.length ? 1 : 0;
}
