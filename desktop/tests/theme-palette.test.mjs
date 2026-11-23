import assert from 'node:assert/strict';
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

// 配色主题只改 tokens.css 里的 RGB 通道；页面样式一旦重新写死蓝色，
// 切到其他配色时那一处就会残留默认的蓝（曾经主按钮渐变、滑块都这样漏过）。
const stylesDir = fileURLToPath(new URL('../src/styles/', import.meta.url));
const tokensCss = readFileSync(join(stylesDir, 'tokens.css'), 'utf8');
const PALETTES = ['sakura'];
const CHANNEL = /(--(?:accent|line|tint)[\w-]*)\s*:/g;

function cssFiles(dir) {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return cssFiles(path);
    return name.endsWith('.css') ? [path] : [];
  });
}

/** 所有选择器恰好为 selector 的规则里声明的通道（同一选择器可以分几块写） */
function channelsOf(selector) {
  const names = new Set();
  for (const match of tokensCss.matchAll(/([^{}]+)\{([^}]*)\}/g)) {
    const sel = match[1].replace(/\/\*[\s\S]*?\*\//g, '').trim();
    if (sel !== selector) continue;
    for (const decl of match[2].matchAll(CHANNEL)) names.add(decl[1]);
  }
  return names;
}

function isSaturatedBlue([r, g, b]) {
  const [rn, gn, bn] = [r / 255, g / 255, b / 255];
  const max = Math.max(rn, gn, bn);
  const min = Math.min(rn, gn, bn);
  const light = (max + min) / 2;
  if (max === min) return false;
  const delta = max - min;
  const sat = light > 0.5 ? delta / (2 - max - min) : delta / (max + min);
  let hue;
  if (max === rn) hue = ((gn - bn) / delta) % 6;
  else if (max === gn) hue = (bn - rn) / delta + 2;
  else hue = (rn - gn) / delta + 4;
  hue = (hue * 60 + 360) % 360;
  return hue >= 191 && hue <= 252 && sat >= 0.45 && light >= 0.15 && light <= 0.92;
}

test('page styles use palette channels instead of hardcoded blue colors', () => {
  const literal = /#([0-9a-f]{6}|[0-9a-f]{3})\b|rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)/gi;
  const offenders = [];
  for (const path of cssFiles(stylesDir)) {
    if (path.endsWith('tokens.css')) continue;
    readFileSync(path, 'utf8').split(/\r?\n/).forEach((line, index) => {
      for (const m of line.matchAll(literal)) {
        let rgb;
        if (m[1]) {
          const hex = m[1].length === 3 ? [...m[1]].map((c) => c + c).join('') : m[1];
          rgb = [0, 2, 4].map((i) => parseInt(hex.slice(i, i + 2), 16));
        } else {
          rgb = [m[2], m[3], m[4]].map(Number);
        }
        if (isSaturatedBlue(rgb)) offenders.push(`${path}:${index + 1} ${m[0]}`);
      }
    });
  }
  assert.deepEqual(offenders, []);
});

test('every palette overrides all channels the default palette defines', () => {
  const defaults = channelsOf(':root');
  assert.ok(defaults.size >= 10);
  const used = new Set(cssFiles(stylesDir).flatMap((path) =>
    [...readFileSync(path, 'utf8').matchAll(/var\((--(?:accent|line|tint)[\w-]*)\)/g)].map((m) => m[1])));
  for (const channel of used) assert.ok(defaults.has(channel), `${channel} has no default value`);
  for (const palette of PALETTES) {
    assert.deepEqual([...channelsOf(`:root[data-palette='${palette}']`)].sort(), [...defaults].sort());
  }
});
