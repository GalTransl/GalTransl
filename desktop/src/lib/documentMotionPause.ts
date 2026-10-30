/* 文档不可见（窗口最小化 / 切到别的应用）时停掉全部 CSS 动画与过渡。

   Chromium 在页面不可见时不再产出动画帧，但 DocumentTimeline 仍然持有被动画引用的
   元素子树，无限动画（进度扫光、旋转、心跳）会一直挂着合成层；从最小化恢复时这些
   积累下来的工作一次性回灌，就是"回到窗口那一下发顿"。

   用 0.01ms + iteration-count: 1，而不是 animation: none —— 与「关闭所有动画效果」
   那条规则同一套做法：动画立刻走完终态，animationend / transitionend 仍会派发，
   页面转场、折叠卸载这些依赖事件的逻辑不会被卡住。 */

const HIDDEN_ATTRIBUTE = 'data-document-hidden';

/** 安装可见性开关，返回卸载函数。首帧之前调用一次即可。 */
export function installDocumentMotionPause(): () => void {
  if (typeof document === 'undefined') return () => {};
  const root = document.documentElement;
  const sync = () => {
    if (document.hidden) root.setAttribute(HIDDEN_ATTRIBUTE, '');
    else root.removeAttribute(HIDDEN_ATTRIBUTE);
  };
  sync();
  document.addEventListener('visibilitychange', sync);
  return () => document.removeEventListener('visibilitychange', sync);
}
