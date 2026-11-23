/* 侧栏文件夹拖动排序的纯逻辑，供 components/useDragReorder.ts 和两个 Agent 侧栏的顺序持久化使用。 */

export type DragAxis = 'x' | 'y';

export type DragReorderState = {
  key: string;
  fromIndex: number;
  toIndex: number;
  /** 被拖项相对按下时的位移（含拖动期间容器自身的滚动） */
  offset: number;
  /** 其余项让位时移动的距离：被拖项的尺寸加上间距 */
  shift: number;
  axis: DragAxis;
};

/** 被拖项盖住某一项的长度超过两者中较短那个的尺寸，就越过它。starts、sizes 是按下时各项的
 *  起点坐标和尺寸，offset 是被拖项的位移。
 *  各项等高时这等价于"中心越过中心"；不能直接比中心：展开的侧栏文件夹有几百像素高，
 *  而手抓的是它顶部的标题行，要让它的中心越过第一项的中心，指针得拖到列表外面去。 */
export function dragTargetIndex(starts: readonly number[], sizes: readonly number[], fromIndex: number, offset: number): number {
  const size = sizes[fromIndex];
  const start = starts[fromIndex] + offset;
  let next = fromIndex;
  for (let i = fromIndex + 1; i < starts.length; i++) if (start + size - starts[i] > Math.min(size, sizes[i])) next = i;
  for (let i = fromIndex - 1; i >= 0; i--) if (starts[i] + sizes[i] - start > Math.min(size, sizes[i])) next = i;
  return next;
}

/** 拖动中各项的位移：被拖项跟手，介于起止位置之间的项让出一个身位。 */
export function dragItemTransform(drag: DragReorderState | null, index: number): string | undefined {
  if (!drag) return undefined;
  const translate = (value: number) => `translate${drag.axis === 'x' ? 'X' : 'Y'}(${value}px)`;
  if (index === drag.fromIndex) return translate(drag.offset);
  if (drag.fromIndex < drag.toIndex && index > drag.fromIndex && index <= drag.toIndex) return translate(-drag.shift);
  if (drag.toIndex < drag.fromIndex && index >= drag.toIndex && index < drag.fromIndex) return translate(drag.shift);
  return undefined;
}

/** 把 key 移到 toIndex，返回新数组；key 不在列表里或位置没变时原样返回。 */
export function moveKey<T>(keys: readonly T[], key: T, toIndex: number): T[] {
  const fromIndex = keys.indexOf(key);
  if (fromIndex < 0 || fromIndex === toIndex) return [...keys];
  const next = keys.filter((item) => item !== key);
  next.splice(Math.max(0, Math.min(toIndex, next.length)), 0, key);
  return next;
}

/** 按用户拖出来的顺序排列当前分组。没排过的新分组（新打开的项目、新游戏）排在最前，
 *  和"最新的在上面"的习惯一致；已排过的按保存的相对顺序跟在后面。 */
export function applySavedOrder(keys: readonly string[], saved: readonly string[]): string[] {
  const rank = new Map(saved.map((key, index) => [key, index]));
  const fresh = keys.filter((key) => !rank.has(key));
  const known = keys.filter((key) => rank.has(key)).sort((a, b) => rank.get(a)! - rank.get(b)!);
  return [...fresh, ...known];
}

/** 读取保存的分组顺序；存储坏了就当没排过。 */
export function parseSavedOrder(raw: string | null): string[] {
  try {
    const value = JSON.parse(raw || '[]');
    return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : [];
  } catch {
    return [];
  }
}
