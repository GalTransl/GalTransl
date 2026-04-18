import { useEffect, useMemo, useRef, useState } from 'react';
import { Panel } from '../../components/Panel';

interface RetranslKeySectionProps {
  config: Record<string, unknown> | null;
  onChange: (keys: string[]) => void;
  onDirty: () => void;
}

function readKeys(config: Record<string, unknown> | null): string[] {
  const common = (config?.common as Record<string, unknown>) || {};
  const raw = common.retranslKey;
  if (Array.isArray(raw)) {
    return raw.map((k) => String(k ?? '').trim()).filter(Boolean);
  }
  if (typeof raw === 'string') {
    return raw.split(/\r?\n/).map((k) => k.trim()).filter(Boolean);
  }
  return [];
}

export function RetranslKeySection({ config, onChange, onDirty }: RetranslKeySectionProps) {
  const keys = useMemo(() => readKeys(config), [config]);

  const [draft, setDraft] = useState('');
  const [editingIndex, setEditingIndex] = useState<number | null>(null);
  const [editingDraft, setEditingDraft] = useState('');
  const editInputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    if (editingIndex !== null) {
      editInputRef.current?.focus();
      editInputRef.current?.select();
    }
  }, [editingIndex]);

  const commit = (next: string[]) => {
    onChange(next);
    onDirty();
  };

  const handleAdd = () => {
    const value = draft.trim();
    if (!value) return;
    if (keys.includes(value)) {
      setDraft('');
      return;
    }
    commit([...keys, value]);
    setDraft('');
  };

  const handleDelete = (idx: number) => {
    const next = keys.filter((_, i) => i !== idx);
    commit(next);
    if (editingIndex === idx) {
      setEditingIndex(null);
      setEditingDraft('');
    }
  };

  const handleStartEdit = (idx: number) => {
    setEditingIndex(idx);
    setEditingDraft(keys[idx] ?? '');
  };

  const handleCancelEdit = () => {
    setEditingIndex(null);
    setEditingDraft('');
  };

  const handleSaveEdit = () => {
    if (editingIndex === null) return;
    const value = editingDraft.trim();
    if (!value) {
      handleDelete(editingIndex);
      return;
    }
    // Deduplicate: if another entry already has this value, just drop the current one
    const duplicateIdx = keys.findIndex((k, i) => i !== editingIndex && k === value);
    let next: string[];
    if (duplicateIdx >= 0) {
      next = keys.filter((_, i) => i !== editingIndex);
    } else {
      next = keys.map((k, i) => (i === editingIndex ? value : k));
    }
    commit(next);
    setEditingIndex(null);
    setEditingDraft('');
  };

  return (
    <Panel
      title="重翻关键字"
      description="原文、译文、问题中命中这些关键字的句子会在下次启动时被重翻。"
    >
      <div className="retransl-key-section">
        <div className="retransl-key-section__add">
          <input
            type="text"
            className="retransl-key-section__input"
            placeholder="输入关键字后按回车或点击添加"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                e.preventDefault();
                handleAdd();
              }
            }}
          />
          <button
            type="button"
            className="retransl-key-section__btn retransl-key-section__btn--primary"
            onClick={handleAdd}
            disabled={!draft.trim()}
          >
            添加
          </button>
        </div>

        {keys.length === 0 ? (
          <div className="retransl-key-section__empty">
            暂无重翻关键字。添加后，下次启动时命中这些关键字的句子会被重新翻译。
          </div>
        ) : (
          <ul className="retransl-key-section__list">
            {keys.map((key, idx) => {
              const isEditing = editingIndex === idx;
              return (
                <li
                  key={`${idx}-${key}`}
                  className={`retransl-key-section__item${isEditing ? ' retransl-key-section__item--editing' : ''}`}
                >
                  <span className="retransl-key-section__index">{idx + 1}</span>
                  {isEditing ? (
                    <input
                      ref={editInputRef}
                      type="text"
                      className="retransl-key-section__input retransl-key-section__input--inline"
                      value={editingDraft}
                      onChange={(e) => setEditingDraft(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter') {
                          e.preventDefault();
                          handleSaveEdit();
                        } else if (e.key === 'Escape') {
                          e.preventDefault();
                          handleCancelEdit();
                        }
                      }}
                    />
                  ) : (
                    <span className="retransl-key-section__text" title={key}>{key}</span>
                  )}
                  <div className="retransl-key-section__actions">
                    {isEditing ? (
                      <>
                        <button
                          type="button"
                          className="retransl-key-section__btn retransl-key-section__btn--primary"
                          onClick={handleSaveEdit}
                          disabled={!editingDraft.trim()}
                        >
                          保存
                        </button>
                        <button
                          type="button"
                          className="retransl-key-section__btn retransl-key-section__btn--ghost"
                          onClick={handleCancelEdit}
                        >
                          取消
                        </button>
                      </>
                    ) : (
                      <>
                        <button
                          type="button"
                          className="retransl-key-section__btn retransl-key-section__btn--ghost"
                          onClick={() => handleStartEdit(idx)}
                          title="编辑"
                        >
                          编辑
                        </button>
                        <button
                          type="button"
                          className="retransl-key-section__btn retransl-key-section__btn--danger"
                          onClick={() => handleDelete(idx)}
                          title="删除"
                        >
                          删除
                        </button>
                      </>
                    )}
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </Panel>
  );
}
