import { useEffect, useMemo, useState } from 'react';
import { Panel } from '../../components/Panel';
import { ConfigFieldRow, ConfigFieldGroup, type ConfigFieldDef } from './ConfigFieldRow';
import { fetchTranslationGuidelines } from '../../lib/api';

// ── Primary (high-frequency) fields ──
const PRIMARY_FIELDS: ConfigFieldDef[] = [
  { key: 'workersPerProject', label: '并发文件数', description: '项目级并行文件数；单文件并行需配合"文件分割"。', type: 'number', placeholder: '16' },
  { key: 'gpt.numPerRequestTranslate', label: '单次翻译句数', description: '每次请求打包的句子数，建议不超过 16。', type: 'number', placeholder: '16' },
  { key: 'language', label: '目标语言', description: '翻译输出语言。', type: 'select', options: ['zh-cn', 'zh-tw', 'en', 'ja', 'ko', 'ru', 'fr'] },
  { key: 'sortBy', label: '翻译顺序', description: 'name 按文件名，size 优先大文件（并行时通常更快）。', type: 'select', options: ['name', 'size'] },
  { key: 'splitFile', label: '文件分割', description: '单文件分片模式：no 关闭，Num 按句数切片，Equal 按份数均分。', type: 'select', options: ['no', 'Num', 'Equal'] },
  { key: 'splitFileNum', label: '分割数量', description: 'Num 模式下表示每片句数；Equal 模式下表示分片总数。', type: 'number', placeholder: '2048' },
  { key: 'gpt.contextNum', label: '上下文句数', description: '每次请求附带的前文句数，常用 8。', type: 'number', placeholder: '8' },
  { key: 'gpt.translation_guideline', label: '翻译规范', description: '使用的翻译规范文件（位于 translation_guidelines 文件夹）。', type: 'select', options: [] },
];

// ── Advanced (low-frequency) fields ──
const ADVANCED_FIELDS: ConfigFieldDef[] = [
  { key: 'splitFileCrossNum', label: '分割交叉句数', description: '分片间重叠句数，可提升片段衔接质量（常用 0 或 10）。', type: 'number', placeholder: '0' },
  { key: 'save_steps', label: '缓存保存频率', description: '每处理 N 个批次保存一次缓存。', type: 'number', placeholder: '1' },
  { key: 'start_time', label: '定时启动', description: '24 小时制时间（如 00:30）；留空则立即启动。', type: 'text', placeholder: '留空则立即启动' },
  { key: 'linebreakSymbol', label: '换行符', description: 'JSON 内换行符类型，供问题检测/自动修复使用。', type: 'text', placeholder: 'auto' },
  { key: 'skipH', label: '跳过敏感句', description: '是否跳过可能触发敏感词检测的句子。', type: 'select', options: ['true', 'false'] },
  { key: 'smartRetry', label: '智能重试', description: '解析失败时自动缩小批次并重置上下文，减少无效重试。', type: 'select', options: ['true', 'false'] },
  { key: 'retranslFail', label: '重翻失败句', description: '启动时是否自动重翻标记为 (Failed) 的句子。', type: 'select', options: ['true', 'false'] },
  { key: 'gpt.enhance_jailbreak', label: '改善拒答', description: '启用后可降低模型拒答概率。', type: 'select', options: ['true', 'false'] },
  { key: 'gpt.change_prompt', label: '修改Prompt', description: 'no 不改；AdditionalPrompt 追加；OverwritePrompt 覆盖默认提示词。', type: 'select', options: ['no', 'AdditionalPrompt', 'OverwritePrompt'] },
  { key: 'gpt.prompt_content', label: '额外Prompt内容', description: '仅在"修改Prompt"非 no 时生效。', type: 'text' },
  { key: 'gpt.token_limit', label: 'Token限制(Sakura)', description: 'Sakura 场景下单轮 token 上限；0 表示不限制。', type: 'number', placeholder: '0' },
  { key: 'loggingLevel', label: '日志级别', description: 'debug 详细，info 常规，warning 仅警告。', type: 'select', options: ['debug', 'info', 'warning'] },
  { key: 'saveLog', label: '保存日志到文件', description: '是否将运行日志写入文件。', type: 'select', options: ['true', 'false'] },
];

interface CommonSettingsSectionProps {
  commonConfig: Record<string, unknown>;
  onFieldChange: (path: string, value: string) => void;
  onListFieldChange?: (path: string, value: string[]) => void;
}

export function CommonSettingsSection({ commonConfig, onFieldChange, onListFieldChange }: CommonSettingsSectionProps) {
  const [guidelines, setGuidelines] = useState<string[]>([]);

  useEffect(() => {
    let cancelled = false;
    fetchTranslationGuidelines()
      .then((list) => { if (!cancelled) setGuidelines(list); })
      .catch(() => { /* optional; fallback to current value only */ });
    return () => { cancelled = true; };
  }, []);

  const primaryFields = useMemo<ConfigFieldDef[]>(() => {
    const currentGuideline = String(getFieldValue(commonConfig, 'gpt.translation_guideline') ?? '');
    const merged = [...guidelines];
    if (currentGuideline && !merged.includes(currentGuideline)) {
      merged.unshift(currentGuideline);
    }
    return PRIMARY_FIELDS.map((field) =>
      field.key === 'gpt.translation_guideline'
        ? { ...field, options: merged }
        : field,
    );
  }, [commonConfig, guidelines]);

  return (
    <Panel title="通用设置" description="翻译核心参数配置（说明已与 sampleProject/config.inc.yaml 同步）。">
      <ConfigFieldGroup title="常用设置" tier="primary">
        {primaryFields.map((field) => (
          <ConfigFieldRow
            key={field.key}
            field={field}
            value={getFieldValue(commonConfig, field.key)}
            onChange={onFieldChange}
            pathPrefix="common"
            tier="primary"
          />
        ))}
      </ConfigFieldGroup>

      <details className="config-advanced-details">
        <summary className="config-advanced-details__summary">高级设置</summary>
        <ConfigFieldGroup title="高级设置" tier="advanced">
          {ADVANCED_FIELDS.map((field) => (
            <ConfigFieldRow
              key={field.key}
              field={field}
              value={getFieldValue(commonConfig, field.key)}
              onChange={onFieldChange}
              onListChange={onListFieldChange}
              pathPrefix="common"
              tier="advanced"
            />
          ))}
        </ConfigFieldGroup>
      </details>
    </Panel>
  );
}

/**
 * Get a value from an object by dot-separated path. Prefers literal flat keys
 * (YAML under `common:` uses flat dotted keys like `gpt.translation_guideline`).
 */
function getFieldValue(obj: Record<string, unknown>, path: string): unknown {
  const keys = path.split('.');
  let current: unknown = obj;
  for (let i = 0; i < keys.length; i++) {
    if (current == null || typeof current !== 'object') return undefined;
    const remaining = keys.slice(i).join('.');
    const cur = current as Record<string, unknown>;
    if (Object.prototype.hasOwnProperty.call(cur, remaining)) {
      return cur[remaining];
    }
    current = cur[keys[i]];
  }
  return current;
}
