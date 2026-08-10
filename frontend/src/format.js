const STATUS_LABELS = {
  queued: "等待解析",
  resolving: "解析中",
  downloading: "下载中",
  merging: "合并中",
  transcoding: "兼容转换中",
  completed: "已完成",
  failed: "失败",
  skipped: "已跳过",
};

export function formatBytes(value) {
  const bytes = Number(value || 0);
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let size = bytes / 1024;
  let unit = units[0];
  for (let index = 1; size >= 1024 && index < units.length; index += 1) {
    size /= 1024;
    unit = units[index];
  }
  return `${Number(size.toFixed(1))} ${unit}`;
}

export function formatSpeed(value) {
  if (!value) return "—";
  return `${formatBytes(value)}/s`;
}

export function formatEta(value) {
  if (value === null || value === undefined) return "—";
  const seconds = Math.max(0, Math.floor(Number(value)));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const remainder = seconds % 60;
  if (hours) return `${hours}时${String(minutes).padStart(2, "0")}分${String(remainder).padStart(2, "0")}秒`;
  if (minutes) return `${minutes}分${String(remainder).padStart(2, "0")}秒`;
  return `${remainder}秒`;
}

export function statusLabel(status) {
  return STATUS_LABELS[status] || status || "未知";
}
