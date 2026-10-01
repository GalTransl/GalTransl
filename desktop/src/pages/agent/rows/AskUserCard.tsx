import { useState } from 'react';
import { Icon } from '../../../components/Icon';
import type { ActivityItem } from '../timeline';
import { asArgs, str } from '../toolMeta';

/* ── 询问用户卡片（ask_user）──
   卡片**就在转录里**、紧跟在那个 ask_user 工具行下面（由外层按 group 渲染），
   一题一步、选项按钮 + 固定的「自己填」入口，可以跳过单题或全部跳过。
   **没有倒计时**：后端那个工具不设超时，会一直等着；不想答就点全部跳过，
   或者干脆点停止让 Agent 自己判断。 */

type AskDraft = { values: string[]; custom: boolean; text: string; skipped: boolean };

type AskQuestion = {
  question: string;
  options: string[];
  multiSelect: boolean;
  /** 模型推荐的选项（后端「全自动-零打断」档位下会直接采用它代答） */
  recommended: string;
};

const emptyAskDraft = (): AskDraft => ({ values: [], custom: false, text: '', skipped: false });

export function AskUserCard({
  item,
  submitting,
  error,
  onSubmit,
}: {
  item: ActivityItem;
  submitting: boolean;
  error: string | null;
  onSubmit: (answers: Array<string[] | null>) => void;
}) {
  const argQuestions = asArgs(item.arguments)?.questions;
  const questions: AskQuestion[] = (Array.isArray(argQuestions) ? argQuestions : [])
    .filter((q): q is Record<string, unknown> => Boolean(q) && typeof q === 'object' && !Array.isArray(q))
    .map((q) => ({
      question: str(q.question),
      options: Array.isArray(q.options) ? q.options.map((o) => str(o)).filter(Boolean) : [],
      multiSelect: q.multiSelect === true,
      recommended: str(q.recommended),
    }));

  const [index, setIndex] = useState(0);
  // 草稿不能在 useState 初始化器里按 questions 一次成型：ask_user 的参数是**流式到达**的，
  // 首渲染时 questions 往往还空着，参数到齐后 questions 变长、初始化好的旧数组却不会跟着长
  // ——drafts[i] 一越界就是读取 undefined 的白屏（整棵 React 树崩掉）。这里只存"用户
  // 碰过的部分"，渲染时按 questions 长度补齐空草稿。
  const [touchedDrafts, setTouchedDrafts] = useState<AskDraft[]>([]);
  const drafts: AskDraft[] = questions.map((_, i) => touchedDrafts[i] ?? emptyAskDraft());

  if (!questions.length) return null;
  const current = questions[Math.min(index, questions.length - 1)];
  const draft = drafts[index] ?? emptyAskDraft();
  const last = index === questions.length - 1;

  // 一题的答案 = 勾选的选项 + 自己填的内容（都为空 = 跳过）
  const answerOf = (d: AskDraft): string[] => {
    const values = [...d.values];
    const text = d.text.trim();
    if (d.custom && text && !values.includes(text)) values.push(text);
    return values;
  };
  const update = (patch: Partial<AskDraft>) =>
    setTouchedDrafts((prev) => {
      const next = questions.map((_, i) => prev[i] ?? emptyAskDraft());
      next[index] = { ...next[index], ...patch };
      return next;
    });
  const goNext = (list: AskDraft[]) => {
    if (last) onSubmit(list.map(answerOf));
    else setIndex((v) => v + 1);
  };
  const toggleOption = (option: string) => {
    // 多选：点几个勾几个，改完自己点「下一题」（点了就走就没法多选了）
    if (current.multiSelect) {
      update({
        values: draft.values.includes(option)
          ? draft.values.filter((v) => v !== option)
          : [...draft.values, option],
      });
      return;
    }
    // 单选：点一下就选中并直接进下一题（最后一题即提交），不必再点「下一题」。
    // 「自己填…」不走这里——它是那一行就地变成输入框，填完回车或点下一题才走。
    const list = drafts.map((d, i) => (i === index ? { ...d, values: [option], custom: false } : d));
    setTouchedDrafts(list);
    goNext(list);
  };
  const skipCurrent = () => {
    const list = drafts.map((d, i) =>
      i === index ? { values: [], custom: false, text: '', skipped: true } : d,
    );
    setTouchedDrafts(list);
    goNext(list);
  };

  return (
    <div className="agent-ask" role="form" aria-label="Agent 提问">
      <div className="agent-ask__head">
        <span className="agent-ask__icon" aria-hidden><Icon name="help" /></span>
        <span className="agent-ask__title">Agent 想先问你</span>
        {questions.length > 1 ? (
          <span className="agent-ask__progress">
            第 {index + 1} / {questions.length} 题
          </span>
        ) : null}
        <button
          type="button"
          className="agent-ask__decline"
          onClick={() => onSubmit(questions.map(() => null))}
          disabled={submitting}
          title="全部跳过，让 Agent 按自己的判断继续"
        >
          全部跳过
        </button>
      </div>

      {questions.length > 1 ? (
        <div className="agent-ask__dots">
          {questions.map((q, i) => {
            const d = drafts[i];
            const state = answerOf(d).length ? 'is-answered' : d.skipped ? 'is-skipped' : '';
            return (
              <button
                key={`${i}-${q.question}`}
                type="button"
                className={`agent-ask__dot ${state}${i === index ? ' is-current' : ''}`}
                onClick={() => setIndex(i)}
                title={`第 ${i + 1} 题：${q.question}`}
                aria-label={`第 ${i + 1} 题`}
              />
            );
          })}
        </div>
      ) : null}

      <h4 className="agent-ask__question">{current.question}</h4>

      <div className="agent-ask__options" role={current.multiSelect ? 'group' : 'radiogroup'}>
        {current.options.map((option) => {
          const selected = draft.values.includes(option);
          return (
            <button
              key={option}
              type="button"
              role={current.multiSelect ? 'checkbox' : 'radio'}
              aria-checked={selected}
              className={`agent-ask__option${selected ? ' is-selected' : ''}`}
              onClick={() => toggleOption(option)}
              disabled={submitting}
            >
              <span className="agent-ask__mark" aria-hidden>{selected ? <Icon name="check" /> : null}</span>
              <span>{option}</span>
              {option === current.recommended ? (
                <span className="agent-ask__tag" title="Agent 推荐这一项（零打断档位会直接选它）">
                  推荐
                </span>
              ) : null}
            </button>
          );
        })}
        {draft.custom ? (
          /* 「自己填」就地变输入框：点开的是这一行本身，不再在下面另起一个浮出的
             输入框。整行包在 label 里，点行的空白处也能聚焦到输入。 */
          <label className="agent-ask__option agent-ask__option--editing is-selected">
            <span className="agent-ask__mark" aria-hidden><Icon name="check" /></span>
            <input
              className="agent-ask__option-input"
              autoFocus
              value={draft.text}
              placeholder="自己填…"
              aria-label="自己填"
              onChange={(e) => update({ text: e.target.value })}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.nativeEvent.isComposing && draft.text.trim()) {
                  e.preventDefault();
                  goNext(drafts);
                }
                // Esc 退出编辑：与再点一下「自己填…」对称，填的内容一并丢掉
                if (e.key === 'Escape') {
                  e.preventDefault();
                  update({ custom: false, text: '' });
                }
              }}
              // 空着离开就退回未选中的「自己填…」，不留一个空输入框挂在列表里
              onBlur={() => { if (!draft.text.trim()) update({ custom: false }); }}
              disabled={submitting}
            />
          </label>
        ) : (
          <button
            type="button"
            role={current.multiSelect ? 'checkbox' : 'radio'}
            aria-checked={false}
            className="agent-ask__option"
            onClick={() =>
              update({
                custom: true,
                // 单选选中「自己填」要把已选选项让开；多选则各自独立
                ...(current.multiSelect ? {} : { values: [] }),
              })
            }
            disabled={submitting}
          >
            <span className="agent-ask__mark" aria-hidden />
            <span>自己填…</span>
          </button>
        )}
      </div>

      {error ? <div className="agent-ask__error">{error}</div> : null}

      <div className="agent-ask__foot">
        <button type="button" className="agent-ask__btn" onClick={skipCurrent} disabled={submitting}>
          跳过
        </button>
        <button
          type="button"
          className="agent-ask__btn is-primary"
          onClick={() => goNext(drafts)}
          disabled={submitting}
        >
          {submitting ? '提交中…' : last ? '提交' : '下一题'}
        </button>
        <span className="agent-ask__note">Agent 正等着这条回答；不想答就跳过或点停止。</span>
      </div>
    </div>
  );
}
