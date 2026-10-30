import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';
import ts from 'typescript';
import { createTypeScriptLoader } from './helpers/load-typescript.mjs';

const source = readFileSync(new URL('../src/lib/markdown.ts', import.meta.url), 'utf8');
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
});
const exports = {};
vm.runInNewContext(outputText, { exports, require: (name) => {
  assert.equal(name, '../i18n/core');
  return createTypeScriptLoader()(new URL('../src/i18n/core.ts', import.meta.url));
} });
const { renderMarkdown, renderMarkdownBlocks, splitMarkdownBlocks, splitMarkdownSegments } = exports;

test('tool tables render escaped pipes and keep br tags as literal text', () => {
  const html = renderMarkdown([
    '| index | message<br>原文 |',
    '| --- | --- |',
    '| 1 | 第一行\\|译文<br>第二行<BR />第三行<br/>第四行 |',
    '| 2 | `<br>` <br onclick="alert(1)"> <img src=x onerror=alert(1)> |',
    '| 3 | &lt;br&gt; |',
  ].join('\n'));
  assert.match(html, /<table>/);
  assert.ok(html.includes('<th>message&lt;br&gt;原文</th>'));
  assert.ok(html.includes('<td>第一行|译文&lt;br&gt;第二行&lt;BR /&gt;第三行&lt;br/&gt;第四行</td>'));
  assert.ok(html.includes('<code>&lt;br&gt;</code>'));
  assert.ok(html.includes('&lt;br onclick=&quot;alert(1)&quot;&gt;'));
  assert.ok(html.includes('&lt;img src=x onerror=alert(1)&gt;'));
  assert.ok(html.includes('&amp;lt;br&amp;gt;'));
  assert.doesNotMatch(html, /<img|<br\b/i);
});

test('dictionary fences preserve nested fences, tabs, blank lines and trailing spaces', () => {
  const raw = ['// 注释', '', '  アリス\t爱丽丝\t人名  ', '```', '````text', '\\n\t<br>|', ''];
  const html = renderMarkdown(['字典 GPT.txt', '', '`````text', ...raw, '`````', '', '**结束**'].join('\n'));
  assert.equal((html.match(/<pre>/g) || []).length, 1);
  assert.ok(html.includes(`<pre><code class="language-text">${raw.join('\n').replace('<br>', '&lt;br&gt;')}</code></pre>`));
  assert.ok(html.endsWith('<p><strong>结束</strong></p>'));
});

test('fences require a standalone closing line at least as long as the opening fence', () => {
  assert.equal(
    renderMarkdown('```text\n```not a closing fence\nvalue\n````'),
    '<pre><code class="language-text">```not a closing fence\nvalue</code></pre>',
  );
  assert.equal(renderMarkdown('```\n```'), '<pre><code></code></pre>');
});

test('streaming unclosed fences still render escaped content without a typing cursor', () => {
  const html = renderMarkdown('`````text\n```\n<script>alert(1)</script>');
  assert.match(html, /^<pre><code class="language-text">```\n&lt;script&gt;alert\(1\)&lt;\/script&gt;/);
  assert.doesNotMatch(html, /agent-typing-cursor/);
  assert.ok(html.endsWith('</code></pre>'));
  assert.doesNotMatch(html, /<script>/);
});

test('fence info string only keeps safe characters in the class attribute', () => {
  assert.equal(
    renderMarkdown('```js"onload="x\nv\n```'),
    '<pre><code class="language-js">v</code></pre>',
  );
  assert.equal(renderMarkdown('```\nv\n```'), '<pre><code>v</code></pre>');
});

test('inline code is never re-parsed by emphasis or link rules', () => {
  const code = (text) => `<code>${text}</code>`;
  assert.equal(renderMarkdown('a `x*y*z` b'), `<p>a ${code('x*y*z')} b</p>`);
  assert.equal(renderMarkdown('a `[x](y)` b'), `<p>a ${code('[x](y)')} b</p>`);
  assert.equal(renderMarkdown('a `~~s~~` b'), `<p>a ${code('~~s~~')} b</p>`);
  assert.equal(renderMarkdown('a `**k**` b'), `<p>a ${code('**k**')} b</p>`);
  // 代码外的标记仍然照常生效
  assert.equal(renderMarkdown('**b** `c` *i*'), '<p><strong>b</strong> <code>c</code> <em>i</em></p>');
});

test('strikethrough and task list items render without extra DOM', () => {
  assert.equal(renderMarkdown('a ~~gone~~ b'), '<p>a <del>gone</del> b</p>');
  assert.equal(
    renderMarkdown(['- [ ] todo', '- [x] done', '- plain'].join('\n')),
    '<ul>\n<li class="is-task">todo</li>\n<li class="is-task is-done">done</li>\n<li>plain</li>\n</ul>',
  );
});

test('block splitting mirrors the parser block boundaries and round-trips', () => {
  const text = [
    '# 标题',
    '',
    '段落一行',
    '段落第二行',
    '',
    '```js',
    'const a = 1;',
    '',
    'const b = 2;',
    '```',
    '',
    '- 列表',
    '',
    '| a | b |',
    '| --- | --- |',
    '| 1 | 2 |',
  ].join('\n');
  // 围栏内的空行不是块边界
  const blocks = splitMarkdownBlocks(text);
  assert.ok(blocks.some((block) => block.includes('const a = 1;\n\nconst b = 2;')));
  assert.equal(renderMarkdownBlocks(text), renderMarkdown(text));
});

test('block rendering equals whole-document rendering for streamed prefixes', () => {
  const full = [
    '先说结论。',
    '',
    '**要点**：',
    '',
    '1. 一',
    '2. 二',
    '',
    '> 引用一段',
    '',
    '```python',
    'def f():',
    '    return 1',
    '```',
    '',
    '| h |',
    '| --- |',
    '| v |',
    '',
    '结尾 `x*y` 。',
  ].join('\n');
  for (let i = 0; i <= full.length; i += 3) {
    const prefix = full.slice(0, i);
    assert.equal(renderMarkdownBlocks(prefix), renderMarkdown(prefix), `prefix length ${i}`);
  }
});

test('cache references inside fenced code do not split the fence', () => {
  const text = ['```json', '$transl_cache("a.json", 1)', '```'].join('\n');
  const segments = splitMarkdownSegments(text);
  assert.equal(segments.length, 1);
  assert.equal(segments[0].kind, 'text');
  assert.equal(segments[0].text, text);
  assert.equal(renderMarkdown(segments[0].text).startsWith('<pre><code class="language-json">'), true);
});

test('fence tracking keeps working across several references in one text', () => {
  const text = [
    '先看 $transl_cache("a.json", 1) 这条',
    '',
    '```json',
    '$transl_cache("b.json", 2)',
    '```',
    '',
    '再看 $transl_cache("c.json", 3)',
  ].join('\n');
  // 数组来自 vm 上下文，先拷到本域再比较（原型不同会让 deepStrictEqual 失败）。
  const segments = Array.from(splitMarkdownSegments(text));
  const kinds = segments.map((seg) => (seg.kind === 'cacheRef' ? seg.filename : 'text'));
  assert.deepEqual(kinds, ['text', 'a.json', 'text', 'c.json']);
  // 围栏作为一整段留在文本片段里，不被劈开
  assert.ok(segments[2].text.includes('```json\n$transl_cache("b.json", 2)\n```'));
  assert.equal(renderMarkdown(segments[2].text).includes('<pre><code class="language-json">'), true);
});

test('cache references outside fences still split into cards', () => {
  // vm 里造出来的对象原型不同，直接深比较会失败，转成普通 JSON 再比。
  const segments = JSON.parse(JSON.stringify(splitMarkdownSegments('见 $transl_cache("a.json", 3) 一节')));
  assert.deepEqual(segments, [
    { kind: 'text', text: '见 ' },
    { kind: 'cacheRef', filename: 'a.json', indexSpec: '3' },
    { kind: 'text', text: ' 一节' },
  ]);
});
