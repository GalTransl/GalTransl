import { useEffect, useRef, useState } from 'react';

interface KeyListEditorProps {
  keys: string[];
  onChange: (keys: string[]) => void;
  onDirty: () => void;
  placeholder?: string;
  emptyText?: string;
  /** 输入框下方的固定说明。 */
  hint?: string;
  /** 校验新增/编辑的值；返回错误文案则拒绝提交（不调用 onChange）。 */
  validate?: (value: string) => string | null;
}

/**
 * 字符串列表编辑器：添加/编辑/删除 + 去重，供「重翻关键字」「问题过滤关键字」
 * 「问题过滤白名单」共用。样式沿用 retransl-key-section__*（同一套观感）。
 */
export function KeyListEditor({
  keys,
  onChange,
  onDirty,
  placeholder = '输入关键字后按回车或点击添加',
  emptyText = '暂无条目',
  hint,
  validate,
}: KeyListEditorProps) {
  const [draft, setDraft] = useState('');
  const [addError, setAddError] = useState<string | null>(null);
  const [editingIndex, setEditingIndex] = useState<number | null>(null);
  const [editingDraft, setEditingDraft] = useState('');
  const [editError, setEditError] = useState<string | null>(null);
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

  const check = (value: string): string | null => (validate ? validate(value) : null);

  const handleAdd = () => {
    const value = draft.trim();
    if (!value) return;
    const error = check(value);
    if (error) {
      setAddError(error);
      return;
    }
    setAddError(null);
    if (keys.includes(value)) {
      setDraft('');
      return;
    }
    commit([...keys, value]);
    setDraft('');
  };

  const handleDelete = (idx: number) => {
    commit(keys.filter((_, i) => i !== idx));
    if (editingIndex === idx) {
      setEditingIndex(null);
      setEditingDraft('');
      setEditError(null);
    }
  };

  const handleStartEdit = (idx: number) => {
    setEditingIndex(idx);
    setEditingDraft(keys[idx] ?? '');
    setEditError(null);
  };

  const handleCancelEdit = () => {
    setEditingIndex(null);
    setEditingDraft('');
    setEditError(null);
  };

  const handleSaveEdit = () => {
    if (editingIndex === null) return;
    const value = editingDraft.trim();
    if (!value) {
      handleDelete(editingIndex);
      return;
    }
    const error = check(value);
    if (error) {
      setEditError(error);
      return;
    }
    // 去重：另一条已经是这个值时，直接丢掉当前这条
    const duplicateIdx = keys.findIndex((k, i) => i !== editingIndex && k === value);
    const next = duplicateIdx >= 0
      ? keys.filter((_, i) => i !== editingIndex)
      : keys.map((k, i) => (i === editingIndex ? value : k));
    commit(next);
    setEditingIndex(null);
    setEditingDraft('');
    setEditError(null);
  };

  return (
    <div className="retransl-key-section">
      <div className="retransl-key-section__add">
        <input
          type="text"
          className="retransl-key-section__input"
          placeholder={placeholder}
          value={draft}
          onChange={(e) => {
            setDraft(e.target.value);
            if (addError) setAddError(null);
          }}
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
      {addError ? <div className="retransl-key-section__error">{addError}</div> : null}
      {hint ? <div className="retransl-key-section__hint">{hint}</div> : null}

      {keys.length === 0 ? (
        <div className="retransl-key-section__empty">{emptyText}</div>
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
                  <div className="retransl-key-section__edit">
                    <input
                      ref={editInputRef}
                      type="text"
                      className="retransl-key-section__input retransl-key-section__input--inline"
                      value={editingDraft}
                      onChange={(e) => {
                        setEditingDraft(e.target.value);
                        if (editError) setEditError(null);
                      }}
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
                    {editError ? <div className="retransl-key-section__error">{editError}</div> : null}
                  </div>
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
  );
}
