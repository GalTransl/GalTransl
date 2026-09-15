/* Agent 回复里的缓存引用卡片。

模型在输出里写 $transl_cache(文件名, 行号)（行号支持 12 / 12-15 / 12,20），
这里把它渲染成"某条缓存"的只读卡片——设计与「缓存与问题」页的 cache-card 一致
（同一套样式类 + 说话人配色 + 问题标签 + 人名字典替换），只是不可编辑、去掉了
删除/展开按钮。

卡片要自己去读缓存文件（接口 /cache/:filename 一次给整个文件的条目），所以做了
两层节流：同一个文件只请求一次（Promise 复用），条目按 index 取（不是数组下标）。
引用一长串行号（如 20-31）时卡片区定高滚动，不撑高对话气泡。 */

import { useEffect, useMemo, useState } from 'react';
import {
  encodeProjectDir,
  fetchCacheFile,
  type CacheEntry,
  type CacheFileResponse,
} from '../lib/api';
import { splitProblemItems } from '../lib/problemFilter';
import { speakerStyle } from '../lib/speaker';
import { resolveSpeakerName, useNameDict } from '../lib/useNameDict';
import {
  parseCacheRefIndexes,
  renderMarkdown,
  splitMarkdownSegments,
  type MarkdownSegment,
} from '../lib/markdown';

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

/** 只读的缓存条目卡片：沿用「缓存与问题」页的 cache-card 设计。
   说话人 pill 跟缓存页一样过人名替换字典——显示译名，但配色仍按原名 hash
   （与缓存页同一角色同色），不然同一个角色两处颜色对不上。 */
function CacheEntryCard({
  entry,
  nameDict,
}: {
  entry: CacheEntry;
  nameDict: Map<string, string>;
}) {
  const problems = splitProblemItems(entry.problem);
  const rawSpeaker = speakerOf(entry);
  const speaker = Array.isArray(entry.name)
    ? entry.name.filter(Boolean).map((n) => resolveSpeakerName(n, nameDict)).join('/')
    : resolveSpeakerName(rawSpeaker, nameDict);
  const engine = entry.trans_by || '';

  return (
    <article className={`cache-card${problems.length ? ' cache-card--problem' : ''}`}>
      <div className="cache-card__row">
        <span className="cache-card__field-label">#{entry.index}</span>
        {speaker ? (
          <span className="cache-card__pill cache-card__pill--speaker" style={speakerStyle(rawSpeaker)}>
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

/** 一条 $transl_cache(文件名, 行号) 引用：取数据 → 卡片列表。

    条目多时不折成「展开其余 N 条」再点一下，而是把卡片区做成定高滚动区：引用
    连续区间时，直接滚比"先截断再展开"更接近翻缓存的手感，也不用记住展开态。 */
function CacheRefCard({
  projectDir,
  filename,
  indexSpec,
  nameDict,
}: {
  projectDir: string;
  filename: string;
  indexSpec: string;
  nameDict: Map<string, string>;
}) {
  const [entries, setEntries] = useState<CacheEntry[] | null>(null);
  const [error, setError] = useState<string>('');
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
          {/* 定高滚动区：条目多时在这里滚，不撑高对话气泡；tabIndex 让键盘也能滚 */}
          <div
            className="agent-cache-ref__cards"
            tabIndex={0}
            role="group"
            aria-label={`${filename} 第 ${indexSpec} 行，共 ${found.length} 条`}
          >
            {found.map((entry) => (
              <CacheEntryCard key={`${filename}#${entry.index}`} entry={entry} nameDict={nameDict} />
            ))}
          </div>
          {missing > 0 ? (
            <div className="agent-cache-ref__foot">
              <span className="agent-cache-ref__missing">另有 {missing} 条未找到</span>
            </div>
          ) : null}
        </>
      )}
    </section>
  );
}

/** 渲染一段 Agent 文本：普通 markdown + 其中的缓存引用卡片。

    没有引用时走原来的一条 innerHTML（行为与样式完全不变）；有引用时交给
    CacheRefSegments——它要多取一份人名替换字典，没引用就不必挂这份开销。 */
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
    <CacheRefSegments
      segments={segments}
      projectDir={projectDir}
      cursor={cursor}
      className={`${cls} agent-md--segmented`}
    />
  );
}

/** 带缓存引用的那段文本：按片段切开，文本片段各自渲染 markdown，引用片段渲染卡片。
    人名替换字典只在这里取——说话人 pill 要跟「缓存与问题」页显示同一个译名。
    流式输出时行末光标只加在最后一段。 */
function CacheRefSegments({
  segments,
  projectDir,
  cursor,
  className,
}: {
  segments: MarkdownSegment[];
  projectDir: string;
  cursor?: boolean;
  className: string;
}) {
  const { nameDict } = useNameDict(encodeProjectDir(projectDir));
  return (
    <div className={className}>
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
            nameDict={nameDict}
          />
        ),
      )}
    </div>
  );
}
