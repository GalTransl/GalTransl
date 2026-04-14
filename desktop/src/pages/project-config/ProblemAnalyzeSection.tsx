import { Panel } from '../../components/Panel';

interface ProblemAnalyzeSectionProps {
  config: Record<string, unknown> | null;
  onProblemListChange: (lines: string[]) => void;
  onDirty: () => void;
}

export function ProblemAnalyzeSection({ config, onProblemListChange, onDirty }: ProblemAnalyzeSectionProps) {
  return (
    <Panel title="问题分析" description="翻译质量检测配置。">
      <div className="config-form">
        <label className="field">
          <span>问题检测列表</span>
          <textarea
            rows={8}
            value={Array.isArray((config?.problemAnalyze as Record<string, unknown>)?.problemList)
              ? ((config?.problemAnalyze as Record<string, unknown>).problemList as string[]).join('\n')
              : String((config?.problemAnalyze as Record<string, unknown>)?.problemList ?? '')}
            onChange={(e) => {
              const lines = e.target.value.split('\n').filter(Boolean);
              onProblemListChange(lines);
              onDirty();
            }}
          />
          <span className="field__hint">每行一个问题类型，如：词频过高、残留日文等</span>
        </label>
      </div>
    </Panel>
  );
}
