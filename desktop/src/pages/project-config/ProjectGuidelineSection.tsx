import { useCallback, useEffect, useState } from 'react';
import { Button } from '../../components/Button';
import { Panel } from '../../components/Panel';
import { InlineFeedback, LoadingState } from '../../components/page-state';
import { fetchProjectGuideline, saveProjectGuideline } from '../../lib/api';
import { normalizeError } from '../../lib/errors';

/** 占位示例：空文件时给个能照着改的骨架，比一句"请输入内容"有用 */
const PLACEHOLDER = `示例（按需增删）：

## 术语与称呼
- 「お兄ちゃん」统一译作「哥哥」，不要用「老哥」

## 语气与文风
- 主角内心独白用书面语，不用网络用语
- 拟声词保留原文的声音感，不意译成"啪的一声"`;

/**
 * 项目翻译规范（配置编辑页的独立区块）。
 *
 * 它**不是配置项**、是项目目录里的一个文件（translation_guideline.md），所以：
 * - 不走配置页那条「保存配置」（那套保存的是 YAML），这里自带保存按钮
 * - 也不参与配置页的 dirty 状态，避免"改了 YAML 没存 + 改了规范没存"混在一起
 *
 * 翻译时它会被拼在全局规范之后，冲突以它为准；改完**下一次启动翻译**才生效
 * （翻译器只在初始化时读一次规范）。
 */
export function ProjectGuidelineSection({
  projectId,
  projectDir,
}: {
  projectId: string;
  /** 只用于展示（文件在项目目录下），实际读写都走后端接口 */
  projectDir: string;
}) {
  const [content, setContent] = useState('');
  const [savedContent, setSavedContent] = useState('');
  const [filename, setFilename] = useState('translation_guideline.md');
  const [exists, setExists] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [savedFlash, setSavedFlash] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetchProjectGuideline(projectId);
      setContent(res.content);
      setSavedContent(res.content);
      setFilename(res.filename);
      setExists(res.exists);
    } catch (err) {
      setError(normalizeError(err, '读取项目规范失败'));
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    void load();
  }, [load]);

  const dirty = content !== savedContent;

  const handleSave = useCallback(async () => {
    setSaving(true);
    setError(null);
    try {
      const res = await saveProjectGuideline(projectId, { mode: 'overwrite', content });
      setSavedContent(content);
      setFilename(res.filename);
      setExists(true);
      setSavedFlash(true);
      window.setTimeout(() => setSavedFlash(false), 2400);
    } catch (err) {
      setError(normalizeError(err, '保存项目规范失败'));
    } finally {
      setSaving(false);
    }
  }, [content, projectId]);

  return (
    <Panel
      className="project-guideline-section"
      title="项目翻译规范"
      description="针对这个项目的翻译要求：翻译时会拼在全局规范之后，与之冲突时以本规范为准。"
      actions={
        <>
          <Button
            variant="secondary"
            onClick={() => void load()}
            disabled={loading || saving || !dirty}
            title={dirty ? '放弃未保存的修改，从文件重新读取' : '当前没有未保存的修改'}
          >
            重新读取
          </Button>
          <Button onClick={() => void handleSave()} disabled={loading || saving || !dirty}>
            {saving ? '保存中…' : '保存规范'}
          </Button>
        </>
      }
    >
      <div className="project-guideline-section__body">
        {error ? <InlineFeedback tone="error" title="项目规范" description={error} /> : null}

        <div className="project-guideline-section__hint">
          <div>
            文件：<code>{filename}</code>
            <span className="project-guideline-section__path" title={projectDir}>
              （位于项目目录内，跟项目一起走）
            </span>
            {!exists ? <span className="project-guideline-section__badge">尚未创建，保存后生成</span> : null}
          </div>
          <div>改完保存后，<strong>下一次启动翻译</strong>才生效；正在跑的翻译不受影响。</div>
        </div>

        {loading ? (
          <LoadingState title="正在读取项目规范…" />
        ) : (
          <textarea
            className="project-guideline-section__editor"
            value={content}
            placeholder={PLACEHOLDER}
            spellCheck={false}
            onChange={(e) => setContent(e.target.value)}
            onKeyDown={(e) => {
              // Ctrl/Cmd+S 直接保存：Markdown 编辑器里这是肌肉记忆
              if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
                e.preventDefault();
                if (dirty && !saving) void handleSave();
              }
            }}
          />
        )}

        <div className="project-guideline-section__foot">
          <span
            className={`project-guideline-section__status${dirty ? ' is-dirty' : ''}`}
          >
            {dirty ? '有未保存的修改' : savedFlash ? '已保存' : '已是最新'}
          </span>
          <span className="project-guideline-section__count">{content.length} 字符</span>
          <span className="project-guideline-section__tip">Ctrl / Cmd + S 保存</span>
        </div>
      </div>
    </Panel>
  );
}
