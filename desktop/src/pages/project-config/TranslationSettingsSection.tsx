import { useEffect, useMemo, useState } from 'react';
import { Panel } from '../../components/Panel';
import { ConfigFieldRow, ConfigFieldGroup, type ConfigFieldDef } from './ConfigFieldRow';
import { fetchTranslationGuidelines } from '../../lib/api';

// ── Primary (high-frequency) fields ──
const PRIMARY_FIELDS: ConfigFieldDef[] = [
  { key: 'workersPerProject', label: '并发文件数', description: '项目级并行文件数；单文件并行需配合「文件读写」中的「文件分割」。', type: 'number', placeholder: '16' },
  { key: 'gpt.numPerRequestTranslate', label: '单次翻译句数', description: '每次请求打包的句子数，建议不超过 16。', type: 'number', placeholder: '16' },
  { key: 'gpt.dynamicNumPerRequestTranslate', label: '动态句数调整', description: '开启后根据模型解析错误自动降低句数，稳定后逐步提升。', type: 'select', options: ['true', 'false'] },
  { key: 'gpt.dynamicNumPerRequestTranslate.min', label: '动态最小句数', description: '动态调整时允许降到的最小单次翻译句数。', type: 'number', placeholder: '8' },
  { key: 'gpt.dynamicNumPerRequestTranslate.max', label: '动态最大句数', description: '动态调整时允许升到的最大单次翻译句数。', type: 'number', placeholder: '64' },
  { key: 'language', label: '目标语言', description: '翻译输出语言。', type: 'select', options: ['zh-cn', 'zh-tw', 'en', 'ja', 'ko', 'ru', 'fr'] },
  { key: 'sortBy', label: '翻译顺序', description: 'name 按文件名，size 优先大文件（并行时通常更快）。', type: 'select', options: ['name', 'size'] },
  { key: 'gpt.multiTurn', label: '多轮对话翻译', description: 'ForGal / ForNovel 默认开启，连续批次共享对话，保持人物称呼和文风一致。', type: 'select', options: ['true', 'false'] },
  { key: 'gpt.contextNum', label: '上下文句数', description: '多轮会话开始或重置时补充的前文句数；关闭多轮时每次携带。0 表示不补充前文，常用 8。', type: 'number', placeholder: '8' },
  { key: 'gpt.translation_guideline', label: '翻译规范', description: '全局翻译规范文件（位于 translation_guidelines 文件夹）。本项目的专属规范在左侧「项目规范」页，翻译时会拼在这份之后、冲突时以项目规范为准。', type: 'select', options: [] },
];

// 只在「动态句数调整」开启时才有意义的两个字段：关闭时从常用设置里隐去
const DYNAMIC_RANGE_KEYS = new Set([
  'gpt.dynamicNumPerRequestTranslate.min',
  'gpt.dynamicNumPerRequestTranslate.max',
]);

// ── Advanced (low-frequency) fields ──
const ADVANCED_FIELDS: ConfigFieldDef[] = [
  { key: 'gpt.multiTurn.maxTurns', label: '会话最多轮数', description: 'ForGal / ForNovel 每个会话的请求轮数，默认 8；达到后新开会话。', type: 'number', placeholder: '8' },
  { key: 'gpt.multiTurn.maxChars', label: '会话字符预算', description: '历史消息（含思考）与新请求的字符预算，默认 24000；超限后新开会话，单个批次不截断。', type: 'number', placeholder: '24000' },
  { key: 'start_time', label: '定时启动', description: '24 小时制时间（如 00:30）；留空则立即启动。', type: 'text', placeholder: '留空则立即启动' },
  { key: 'skipH', label: '跳过敏感句', description: '是否跳过可能触发敏感词检测的句子。', type: 'select', options: ['true', 'false'] },
  { key: 'smartRetry', label: '智能重试', description: '解析失败时自动缩小批次并重置上下文，减少无效重试。', type: 'select', options: ['true', 'false'] },
  { key: 'retranslFail', label: '重翻失败句', description: '启动时是否自动重翻标记为 (Failed) 的句子。', type: 'select', options: ['true', 'false'] },
  { key: 'gpt.enhance_jailbreak', label: '改善拒答', description: '启用后可降低模型拒答概率。', type: 'select', options: ['true', 'false'] },
  { key: 'gpt.token_limit', label: 'Token限制(Sakura)', description: 'Sakura 场景下单轮 token 上限；0 表示不限制。', type: 'number', placeholder: '0' },
];

interface TranslationSettingsSectionProps {
  commonConfig: Record<string, unknown>;
  onFieldChange: (path: string, value: string) => void;
  onListFieldChange?: (path: string, value: string[]) => void;
}

export function TranslationSettingsSection({ commonConfig, onFieldChange, onListFieldChange }: TranslationSettingsSectionProps) {
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
    // 动态句数调整关闭时，上下限没人用，不显示
    const dynamicRaw = getFieldValue(commonConfig, 'gpt.dynamicNumPerRequestTranslate');
    const dynamicEnabled = dynamicRaw === true || String(dynamicRaw ?? '').trim().toLowerCase() === 'true';
    return PRIMARY_FIELDS
      .filter((field) => dynamicEnabled || !DYNAMIC_RANGE_KEYS.has(field.key))
      .map((field) =>
        field.key === 'gpt.translation_guideline'
          ? { ...field, options: merged }
          : field,
      );
  }, [commonConfig, guidelines]);

  return (
    <Panel title="翻译设置" description="配置目标语言、翻译并发、单次句数、上下文和重试策略。">
      <ConfigFieldGroup title="常用设置" tier="primary">
        {primaryFields.map((field) => (
          <ConfigFieldRow
            key={field.key}
            field={field}
            value={getFieldValue(commonConfig, field.key) ?? (field.key === 'gpt.multiTurn' ? true : undefined)}
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
