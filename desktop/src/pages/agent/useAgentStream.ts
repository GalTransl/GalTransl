import { useCallback, useEffect, useRef } from 'react';
import { subscribeAgentStream, type AgentEvent } from '../../lib/api';
import { appendStreamEvents } from './timeline';

// 事件流意外断开后的自动重连次数（退避 1s、2s、4s… 封顶 10s）
const STREAM_MAX_RECONNECTS = 5;
// 流事件批量进 state 的间隔（ms）
const EVENT_FLUSH_MS = 50;

type StreamHandlers = {
  /** 每条事件（close 除外）。返回 true 表示已自行处理（或丢弃），不进转录。 */
  onEvent: (ev: AgentEvent) => boolean;
  /** 后端发来 close 帧：本条流正常结束。 */
  onClose: () => void;
  /** 正在重连（attempt 从 1 开始）；重连成功后 attempt 为 0。 */
  onReconnecting: (attempt: number, max: number) => void;
  /** 重连次数用尽，放弃。 */
  onGiveUp: (err: Error) => void;
};

/**
 * Agent 事件流的连接管理：订阅/关闭、断线自动重连、事件批量进 state。
 *
 * 事件怎么解释（状态、队列、去重、乐观消息……）由页面通过 handlers 决定；
 * 这里只保证三件事：
 * - 旧流迟到的回调、排着的重连定时器在切走后作废（按「流代号」判断）；
 * - 流结束（close 或断线）时释放 abort 引用，isOpen() 如实反映有没有流在收事件；
 * - 进转录的事件先攒 EVENT_FLUSH_MS 再批量 setEvents，相邻增量合并成一条，
 *   流式输出不会按 token 重建整段时间线。
 */
export function useAgentStream(
  setEvents: (update: (prev: AgentEvent[]) => AgentEvent[]) => void,
  handlers: StreamHandlers,
) {
  const handlersRef = useRef(handlers);
  handlersRef.current = handlers;
  const abortRef = useRef<(() => void) | null>(null);
  // 流的代号：每次订阅/关闭都 +1
  const genRef = useRef(0);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const attemptsRef = useRef(0);
  const pendingRef = useRef<AgentEvent[]>([]);
  const flushTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  /** 把攒着的事件立刻落进 state（要按顺序插入别的事件前先调它）。 */
  const flush = useCallback(() => {
    if (flushTimerRef.current) {
      clearTimeout(flushTimerRef.current);
      flushTimerRef.current = null;
    }
    const batch = pendingRef.current;
    if (!batch.length) return;
    pendingRef.current = [];
    setEvents((prev) => appendStreamEvents(prev, batch));
  }, [setEvents]);

  const close = useCallback(() => {
    // 已收到但还没进 state 的事件先落进去：重订阅从已见 step 之后续拉，这批不会再来
    flush();
    genRef.current += 1;
    if (reconnectTimerRef.current) {
      clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }
    attemptsRef.current = 0;
    abortRef.current?.();
    abortRef.current = null;
  }, [flush]);

  const subscribe = useCallback(
    (dir: string, sessionId: string | undefined, afterStep: () => number) => {
      close();
      const gen = genRef.current;
      let abort: (() => void) | null = null;
      // 本条流结束了：只清自己的引用（abortRef 可能已被新订阅占用）
      const release = () => {
        if (abortRef.current === abort) abortRef.current = null;
      };
      abort = subscribeAgentStream(
        dir,
        (ev) => {
          if (genRef.current !== gen) return;
          if (attemptsRef.current > 0) {
            attemptsRef.current = 0;
            handlersRef.current.onReconnecting(0, STREAM_MAX_RECONNECTS);
          }
          if (ev.type === 'close') {
            release();
            handlersRef.current.onClose();
            return;
          }
          if (handlersRef.current.onEvent(ev)) return;
          pendingRef.current.push(ev);
          if (!flushTimerRef.current) flushTimerRef.current = setTimeout(flush, EVENT_FLUSH_MS);
        },
        (err) => {
          if (genRef.current !== gen) return;
          release();
          // 连接断了（后端重启、代理掐线…）：按指数退避重连，从已见的 step 之后续拉。
          // 后端那边回合早就结束的话，重连拿到的首帧就是终态 + close，自然收尾。
          const attempt = attemptsRef.current;
          if (attempt >= STREAM_MAX_RECONNECTS) {
            handlersRef.current.onGiveUp(err);
            return;
          }
          handlersRef.current.onReconnecting(attempt + 1, STREAM_MAX_RECONNECTS);
          reconnectTimerRef.current = setTimeout(() => {
            reconnectTimerRef.current = null;
            if (genRef.current !== gen) return;
            subscribe(dir, sessionId, afterStep);
            // subscribe 开头的 close 会把计数清零，这里接回去
            attemptsRef.current = attempt + 1;
          }, Math.min(1000 * 2 ** attempt, 10000));
        },
        afterStep(),
        sessionId,
      );
      abortRef.current = abort;
    },
    [close, flush],
  );

  /** 当前是否有流在收事件（close 帧或断线后为 false）。 */
  const isOpen = useCallback(() => abortRef.current !== null, []);

  useEffect(() => close, [close]);

  return { subscribe, close, flush, isOpen };
}
