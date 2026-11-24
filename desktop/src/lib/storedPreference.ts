/**
 * 偏好只在和默认值不同时才落到 localStorage，等于默认值就删掉这一项。
 * 读取时缺项按当前默认值补，以后调整默认值，没改过这一项的老用户会自动跟上；
 * 要是把默认值也存下来，就分不清「用户选了它」还是「当时的默认就是它」。
 *
 * localStorage 不可用时照常抛出，调用方各自决定是否忽略。
 */
export function storePreference(key: string, value: string, defaultValue: string): void {
  if (value === defaultValue) {
    localStorage.removeItem(key);
  } else {
    localStorage.setItem(key, value);
  }
}
