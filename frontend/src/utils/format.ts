/** 展示层格式化工具。 */

/** 字节大小 → 易读文本（B / KB / MB / GB）。 */
export function formatFileSize(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return '-'
  if (bytes < 1024) return `${bytes} B`
  const units = ['KB', 'MB', 'GB', 'TB']
  let value = bytes / 1024
  let unitIndex = 0
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024
    unitIndex += 1
  }
  return `${value.toFixed(value >= 100 ? 0 : value >= 10 ? 1 : 2)} ${units[unitIndex]}`
}

/** ISO 时间字符串 → 本地 yyyy/MM/dd HH:mm。 */
export function formatDateTime(iso: string): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  const pad = (n: number) => String(n).padStart(2, '0')
  return (
    `${date.getFullYear()}/${pad(date.getMonth() + 1)}/${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}`
  )
}

/** 0~1 的相似度 → 百分比（保留整数）。 */
export function formatScore(score: number): string {
  return `${Math.round(score * 100)}%`
}

/** 来源定位：PDF 显示页码，DOCX/XLSX 显示章节/工作表名。 */
export function formatLocation(page: number | null, section: string | null): string {
  if (page !== null && page !== undefined) return `第 ${page} 页`
  if (section) return section
  return '位置未知'
}
