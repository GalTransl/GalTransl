/* Agent 回复里的缓存引用卡片。

模型在输出里写 $transl_cache(文件名, 行号)（行号支持 12 / 12-15 / 12,20），
这里把它渲染成"某条缓存"的只读卡片——设计与「缓存与问题」页的 cache-card 一致
（同一套样式类 + 说话人配色 + 问题标签），只是不可编辑、去掉了删除/展开按钮。

卡片要自己去读缓存文件（接口 /cache/:filename 一次给整个文件的条目），所以做了
两层节流：同一个文件只请求一次（Promise 复用），条目按 index 取（不是数组下标）。 */

import { useEffect, useMemo, useState } from 'react';
import {
  encodeProjectDir,
  fetchCacheFile,
  type CacheEntry,
  type CacheFileResponse,
} from '../lib/api';
import { splitProblemItems } from '../lib/problemFilter';
import { speakerStyle } from '../lib/speaker';
import { parseCacheRefIndexes, renderMarkdown, splitMarkdownSegments } from '../lib/markdown';

/** 同一项目 + 文件的读取共享一个 Promise：一条回复里引用同一文件多次也只请求一次。 */
const cacheFilePromises = new Map<string, Promise<CacheFileResponse>>();

function loadCacheFile(projectId: string, filename: string): Promise<CacheFileResponse> {
  const key = `${projectId}::${filename}`;
  const hit = cacheFilePromises.get(key);
  if (hit) return hit;
  const pending = fetchCacheFile(projectId, filename);
  cacheFilePromises.set(key, pending);
  pending.catch(() => cacheFilePromises.delete(key));  // 失败不留缓存，允许重试
  return pending;
}

function srcOf(entry: CacheEntry): string {
  return entry.post_src || entry.post_jp || entry.pre_src || entry.pre_jp || '';
}

function dstOf(entry: CacheEntry): string {
  return entry.pre_dst || entry.pre_zh || entry.proofread_dst || entry.proofread_zh || '';
}

function speakerOf(entry: CacheEntry): string {
  const raw = entry.name;
  if (Array.isArray(raw)) return raw.filter(Boolean).join('/');
  return raw || '';
}

/** 只读的缓存条目卡片：沿用「缓存与问题」页的 cache-card 设计。 */
function CacheEntryCard({ entry }: { entry: CacheEntry }) {
  const problems = splitProblemItems(entry.problem);
  const speaker = speakerOf(entry);
  const engine = entry.trans_by || '';

  return (
    <article className={`cache-card${problems.length ? ' cache-card--problem' : ''}`}>
      <div className="cache-card__row">
        <span className="cache-card__field-label">#{entry.index}</span>
        {speaker ? (
          <span className="cache-card__pill cache-card__pill--speaker" style={speakerStyle(speaker)}>
            {speaker}
          </span>
        ) : null}
        {problems.length ? (
          <div className="cache-card__problem-slot">
            {problems.map((item) => (
              <span key={item} className="cache-card__problem-item">
                <span className="cache-card__pill cache-card__pill--problem" title={item}>
                  {item}
                </span>
              </span>
            ))}
          </div>
        ) : null}
        <div className="cache-card__spacer" />
        {engine ? <span className="cache-card__pill cache-card__pill--engine">{engine}</span> : null}
      </div>
      <div className="cache-card__fields">
        <div className="cache-card__field">
          <span className="cache-card__field-label">原文</span>
          <div className="cache-card__input-wrap">
            <span className="cache-card__readonly-input">{srcOf(entry)}</span>
          </div>
        </div>
        <div className="cache-card__field">
          <span className="cache-card__field-label">译文</span>
          <div className="cache-card__input-wrap">
            <span className="cache-card__readonly-input">{dstOf(entry)}</span>
          </div>
        </div>
      </div>
    </article>
  );
}

/** 头部的数据库小图标：emoji 在不同系统渲染不一致，SVG 更可控。 */
function DatabaseIcon() {
  return (
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true">
      <ellipse cx="8" cy="3.6" rx="5.4" ry="2.1" />
      <path d="M2.6 3.6v8.8c0 1.16 2.42 2.1 5.4 2.1s5.4-.94 5.4-2.1V3.6" />
      <path d="M2.6 8c0 1.16 2.42 2.1 5.4 2.1s5.4-.94 5.4-2.1" />
    </svg>
  );
}

/** 一条 $transl_cache(文件名, 行号) 引用：取数据 → 卡片列表。 */
const COLLAPSED_COUNT = 5;

/** 已展开的引用（模块级）：切页面会导致组件卸载重建，展开状态放 useState 会丢，
 *  这里按 引用key 记住，重挂时恢复。 */
const expandedRefs = new Set<string>();

function CacheRefCard({
  projectDir,
  filename,
  indexSpec,
}: {
  projectDir: string;
  filename: string;
  indexSpec: string;
}) {
  const [entries, setEntries] = useState<CacheEntry[] | null>(null);
  const [error, setError] = useState<string>('');
  const refKey = `${projectDir}::${filename}::${indexSpec}`;
  const [expanded, setExpandedState] = useState(() => expandedRefs.has(refKey));
  const setExpanded = (next: boolean) => {
    if (next) expandedRefs.add(refKey);
    else expandedRefs.delete(refKey);
    setExpandedState(next);
  };
  const wanted = useMemo(() => parseCacheRefIndexes(indexSpec), [indexSpec]);

  useEffect(() => {
    let alive = true;
    setEntries(null);
    setError('');
    loadCacheFile(encodeProjectDir(projectDir), filename)
      .then((res) => {
        if (!alive) return;
        const list = Array.isArray(res?.entries) ? res.entries : [];
        const byIndex = new Map<number, CacheEntry>();
        for (const e of list) {
          const idx = Number((e as CacheEntry)?.index);
          if (Number.isFinite(idx)) byIndex.set(idx, e as CacheEntry);
        }
        if (!alive) return;
        setEntries(wanted.map((i) => byIndex.get(i)).filter((e): e is CacheEntry => Boolean(e)));
      })
      .catch((err) => {
        if (!alive) return;
        setError(err instanceof Error ? err.message : String(err));
      });
    return () => {
      alive = false;
    };
  }, [projectDir, filename, wanted.join(',')]);

  const found = entries ?? [];
  const missing = wanted.length - found.length;
  // 条目多时默认折叠，避免一条回复被引用刷满整屏
  const collapsed = found.length > COLLAPSED_COUNT && !expanded;
  const visible = collapsed ? found.slice(0, COLLAPSED_COUNT) : found;
  const hidden = found.length - visible.length;

  return (
    <section className="agent-cache-ref" aria-label={`缓存引用 ${filename} ${indexSpec}`}>
      <header className="agent-cache-ref__head">
        <span className="agent-cache-ref__icon">
          <DatabaseIcon />
        </span>
        <span className="agent-cache-ref__file" title={filename}>{filename}</span>
        <span className="agent-cache-ref__lines">第 {indexSpec} 行</span>
        {found.length ? <span className="agent-cache-ref__count">{found.length} 条</span> : null}
      </header>
      {error ? (
        <div className="agent-cache-ref__state agent-cache-ref__state--error">
          读不到「{filename}」：{error}
        </div>
      ) : entries === null ? (
        <div className="agent-cache-ref__state agent-cache-ref__state--loading" aria-busy="true">
          <span className="agent-cache-ref__skeleton" style={{ width: '78%' }} />
          <span className="agent-cache-ref__skeleton" style={{ width: '92%' }} />
          <span className="agent-cache-ref__skeleton" style={{ width: '56%' }} />
        </div>
      ) : found.length === 0 ? (
        <div className="agent-cache-ref__state">
          「{filename}」里没有第 {indexSpec} 行（行号来自缓存条目的 index）
        </div>
      ) : (
        <>
          <div className="agent-cache-ref__cards">
            {visible.map((entry) => (
              <CacheEntryCard key={`${filename}#${entry.index}`} entry={entry} />
            ))}
          </div>
          {hidden > 0 || missing > 0 ? (
            <div className="agent-cache-ref__foot">
              {hidden > 0 ? (
                <button
                  type="button"
                  className="agent-cache-ref__more"
                  onClick={() => setExpanded(true)}
                >
                  展开其余 {hidden} 条
                </button>
              ) : null}
              {missing > 0 ? (
                <span className="agent-cache-ref__missing">另有 {missing} 条未找到</span>
              ) : null}
            </div>
          ) : null}
        </>
      )}
    </section>
  );
}

/** 渲染一段 Agent 文本：普通 markdown + 其中的缓存引用卡片。

    没有引用时走原来的一条 innerHTML（行为与样式完全不变）；有引用时按片段切开，
    文本片段各自渲染 markdown，引用片段渲染卡片。流式输出时行末光标只加在最后一段。 */
export function AgentMarkdown({
  text,
  projectDir,
  cursor,
  className,
}: {
  text: string;
  projectDir: string;
  cursor?: boolean;
  className?: string;
}) {
  const segments = useMemo(() => splitMarkdownSegments(text), [text]);
  const hasRef = segments.some((s) => s.kind === 'cacheRef');
  const cls = ['agent-md', className].filter(Boolean).join(' ');
  if (!hasRef) {
    return <div className={cls} dangerouslySetInnerHTML={{ __html: renderMarkdown(text, { cursor }) }} />;
  }
  return (
    <div className={`${cls} agent-md--segmented`}>
      {segments.map((seg, i) =>
        seg.kind === 'text' ? (
          <div
            key={`t${i}`}
            className="agent-md__chunk"
            dangerouslySetInnerHTML={{
              __html: renderMarkdown(seg.text, { cursor: cursor && i === segments.length - 1 }),
            }}
          />
        ) : (
          <CacheRefCard
            key={`${seg.filename}:${seg.indexSpec}:${i}`}
            projectDir={projectDir}
            filename={seg.filename}
            indexSpec={seg.indexSpec}
          />
        ),
      )}
    </div>
  );
}
