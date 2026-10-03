import { t as translate, useUiLanguage } from "../i18n";
import { Component, type ReactNode } from 'react';
import { Button } from './Button';
import { InlineFeedback } from './page-state/InlineFeedback';

type Props = { children: ReactNode };
type State = { error: Error | null };

/** 渲染错误边界：子树里任何一处渲染抛错（比如模型给的入参形状出乎意料）时兜住它，
 *  显示一张可重试的错误卡，而不是把整页打成白屏。
 *  「重试」只是清掉错误、重建子树；页面数据在 localStorage / 后端，不受影响。 */
export class RenderErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error) {
    // 堆栈进控制台，界面上只留一句话与重试入口
    console.error('页面渲染出错', error);
  }

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;
    return <RenderErrorFallback error={error} onRetry={() => this.setState({ error: null })} />;
  }
}

function RenderErrorFallback({ error, onRetry }: { error: Error; onRetry: () => void }) {
  useUiLanguage();
  return (
    <div style={{ padding: 24, display: 'grid', gap: 12, justifyItems: 'start' }}>
      <InlineFeedback tone="error" title={translate("common:renderErrorBoundary.message_title_text")} description={error.message || translate("common:actions.unknownError")} />
      <Button onClick={onRetry}>{translate("common:actions.retry")}</Button>
    </div>
  );
}
