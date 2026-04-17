import { useEffect, useState } from 'react';
import { Button } from '../../components/Button';
import { CustomSelect } from '../../components/CustomSelect';
import { Panel } from '../../components/Panel';
import { InlineFeedback } from '../../components/page-state/InlineFeedback';
import type { SubmitJobPayload, TranslatorOption } from '../../lib/api';

type JobSubmitFormProps = {
  disabled: boolean;
  isSubmitting: boolean;
  onSubmit: (payload: SubmitJobPayload) => Promise<void>;
  submitError: string | null;
  translators: TranslatorOption[];
};

export function JobSubmitForm({ disabled, isSubmitting, onSubmit, submitError, translators }: JobSubmitFormProps) {
  const [projectDir, setProjectDir] = useState('');
  const [configFileName, setConfigFileName] = useState('config.yaml');
  const [translator, setTranslator] = useState('');
  const [localError, setLocalError] = useState<string | null>(null);
  const activeError = localError ?? submitError;

  useEffect(() => {
    if (!translator && translators.length > 0) {
      setTranslator(translators[0].name);
    }
  }, [translator, translators]);

  return (
    <Panel
      title="Submit Job"
      description="填写本地项目目录、配置文件和翻译模板，然后将任务发送到 Python 后端。"
    >
      <form
        className="form-stack"
        onSubmit={async (event) => {
          event.preventDefault();

          const normalizedProjectDir = projectDir.trim();
          const normalizedConfig = configFileName.trim() || 'config.yaml';

          if (!normalizedProjectDir) {
            setLocalError('请输入项目目录。');
            return;
          }

          if (!translator) {
            setLocalError('请选择翻译模板。');
            return;
          }

          setLocalError(null);
          await onSubmit({
            config_file_name: normalizedConfig,
            project_dir: normalizedProjectDir,
            translator,
          });
        }}
      >
        <label className="field">
          <span>项目目录</span>
          <input
            autoComplete="off"
            disabled={disabled || isSubmitting}
            onChange={(event) => setProjectDir(event.target.value)}
            placeholder="例如：E:\\GalTransl\\sampleProject"
            value={projectDir}
          />
        </label>

        <label className="field">
          <span>配置文件名</span>
          <input
            autoComplete="off"
            disabled={disabled || isSubmitting}
            onChange={(event) => setConfigFileName(event.target.value)}
            value={configFileName}
          />
        </label>

        <label className="field">
          <span>翻译模板</span>
          <CustomSelect
            disabled={disabled || isSubmitting || translators.length === 0}
            onChange={(event) => setTranslator(event.target.value)}
            value={translator}
          >
            {translators.length === 0 ? <option value="">暂无可用模板</option> : null}
            {translators.map((item) => (
              <option key={item.name} value={item.name}>
                {item.name} · {item.description}
              </option>
            ))}
          </CustomSelect>
        </label>

        {activeError ? (
          <InlineFeedback tone="error" title="启动任务失败" description={activeError} />
        ) : (
          <InlineFeedback tone="info" title="连接提示">
            后端默认地址来自 <code>VITE_BACKEND_URL</code>，未设置时回退到{' '}
            <code>http://127.0.0.1:18000</code>。
          </InlineFeedback>
        )}

        <div className="form-actions">
          <Button disabled={disabled || isSubmitting} type="submit">
            {isSubmitting ? '提交中…' : '启动任务'}
          </Button>
        </div>
      </form>
    </Panel>
  );
}
