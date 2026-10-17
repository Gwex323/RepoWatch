export function bound(value: number, maximum: number): number {
  return Math.min(value, maximum - 1);
}
