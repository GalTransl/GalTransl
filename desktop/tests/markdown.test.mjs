import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';
import ts from 'typescript';

const source = readFileSync(new URL('../src/lib/markdown.ts', import.meta.url), 'utf8');
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
});
const exports = {};
vm.runInNewContext(outputText, { exports });
const { renderMarkdown } = exports;

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
  assert.ok(html.includes(`<pre><code>${raw.join('\n').replace('<br>', '&lt;br&gt;')}</code></pre>`));
  assert.ok(html.endsWith('<p><strong>结束</strong></p>'));
});

test('fences require a standalone closing line at least as long as the opening fence', () => {
  assert.equal(
    renderMarkdown('```text\n```not a closing fence\nvalue\n````'),
    '<pre><code>```not a closing fence\nvalue</code></pre>',
  );
  assert.equal(renderMarkdown('```\n```'), '<pre><code></code></pre>');
});

test('streaming unclosed fences still render escaped content and the cursor', () => {
  const html = renderMarkdown('`````text\n```\n<script>alert(1)</script>', { cursor: true });
  assert.match(html, /^<pre><code>```\n&lt;script&gt;alert\(1\)&lt;\/script&gt;/);
  assert.match(html, /class="agent-typing-cursor"/);
  assert.ok(html.endsWith('</code></pre>'));
  assert.doesNotMatch(html, /<script>/);
});
