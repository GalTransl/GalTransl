import type { IconName } from './Icon';
import { Icon } from './Icon';

export type SectionNavItem<K extends string = string> = {
  key: K;
  label: string;
  icon: IconName;
};

type SectionNavProps<K extends string> = {
  items: readonly SectionNavItem<K>[];
  active: K;
  onSelect: (key: K) => void;
  /** 一组页签的整体名称，读屏会念成「<名字> 导航」 */
  ariaLabel: string;
  className?: string;
};

/**
 * 页面内的二级菜单（左侧一列按钮）。
 *
 * 项目配置页先有的这套交互（见 project-config/ConfigSectionNav），设置页这次也按同样的样子
 * 拆分区，就把它抽成组件；两边都保留自己的外层容器，所以组件只管按钮本身。
 */
export function SectionNav<K extends string>({ items, active, onSelect, ariaLabel, className }: SectionNavProps<K>) {
  return (
    <nav className={className ? `section-nav ${className}` : 'section-nav'} aria-label={ariaLabel}>
      {items.map((item) => {
        const current = item.key === active;
        return (
          <button
            type="button"
            key={item.key}
            className={`section-nav__btn${current ? ' section-nav__btn--active' : ''}`}
            aria-current={current ? 'page' : undefined}
            onClick={() => onSelect(item.key)}
          >
            <span className="section-nav__icon"><Icon name={item.icon} /></span>
            <span className="section-nav__label">{item.label}</span>
          </button>
        );
      })}
    </nav>
  );
}
