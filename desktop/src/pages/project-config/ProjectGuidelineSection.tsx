import { UiTrans, message as uiMessage, t as translate, useMessageState, useUiLanguage } from "../../i18n";
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
  useUiLanguage();
  const [content, setContent] = useState('');
  const [savedContent, setSavedContent] = useState('');
  const [filename, setFilename] = useState('translation_guideline.md');
  const [exists, setExists] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useMessageState<string | null>(null);
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
      setError(normalizeError(err, uiMessage("config:projectGuidelineSection.load_normalizeError_readProjectGuidelineFailed")));
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
      setError(normalizeError(err, uiMessage("config:projectGuidelineSection.handleSave_normalizeError_saveProjectGuidelineFailed")));
    } finally {
      setSaving(false);
    }
  }, [content, projectId]);

  return (
    <Panel
      className="project-guideline-section"
      title={translate("config:projectGuidelineSection.projectGuidelineSection_title_projectTranslationGuideline")}
      description={translate("config:projectGuidelineSection.projectGuidelineSection_description_countProjectTranslationTranslationGuidelineGuideline")}
      actions={
        <>
          <Button
            variant="secondary"
            onClick={() => void load()}
            disabled={loading || saving || !dirty}
            title={dirty ? translate("config:projectGuidelineSection.projectGuidelineSection_title_notSaveChangeFileRead") : translate("config:projectGuidelineSection.projectGuidelineSection_title_currentEmptyNotSaveChange")}
          >{translate("config:projectGuidelineSection.projectGuidelineSection_actions_read")}</Button>
          <Button onClick={() => void handleSave()} disabled={loading || saving || !dirty}>
            {saving ? translate("common:actions.saving") : translate("config:projectGuidelineSection.projectGuidelineSection_message_saveGuideline")}
          </Button>
        </>
      }
    >
      <div className="project-guideline-section__body">
        {error ? <InlineFeedback tone="error" title={translate("config:projectGuidelineSection.projectGuidelineSectionBody_title_projectGuideline")} description={error} /> : null}

        <div className="project-guideline-section__hint">
          <div>{translate("config:projectGuidelineSection.projectGuidelineSectionHint_div_file")}<code>{filename}</code>
            <span className="project-guideline-section__path" title={projectDir}>{translate("config:projectGuidelineSection.projectGuidelineSectionHint_message_projectDirectoryProject")}</span>
            {!exists ? <span className="project-guideline-section__badge">{translate("config:projectGuidelineSection.projectGuidelineSectionHint_message_notCreateSave")}</span> : null}
          </div>
          <div><UiTrans k="config:projectGuidelineSection.projectGuidelineSectionHint_message_save0Translation0EffectivePendingTranslation" components={[<strong />]} /></div>
        </div>

        {loading ? (
          <LoadingState title={translate("config:projectGuidelineSection.projectGuidelineSectionBody_title_pendingReadProjectGuideline")} />
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
            {dirty ? translate("config:projectGuidelineSection.projectGuidelineSectionFoot_message_notSaveChange") : savedFlash ? translate("config:projectGuidelineSection.projectGuidelineSectionFoot_message_doneSave") : translate("config:projectGuidelineSection.projectGuidelineSectionFoot_message_done")}
          </span>
          <span className="project-guideline-section__count">{translate("config:projectGuidelineSection.projectGuidelineSectionFoot_message_text", { count: content.length })}</span>
          <span className="project-guideline-section__tip">{translate("config:projectGuidelineSection.projectGuidelineSectionFoot_message_ctrlCmdSSave")}</span>
        </div>
      </div>
    </Panel>
  );
}
