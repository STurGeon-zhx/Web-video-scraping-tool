import { describe, expect, it } from "vitest";

import { formatBytes, formatEta, formatSpeed, statusLabel } from "../src/format.js";

describe("任务显示格式", () => {
  it("格式化字节和速度", () => {
    expect(formatBytes(0)).toBe("0 B");
    expect(formatBytes(1536)).toBe("1.5 KB");
    expect(formatSpeed(2 * 1024 * 1024)).toBe("2 MB/s");
  });

  it("格式化预计剩余时间", () => {
    expect(formatEta(null)).toBe("—");
    expect(formatEta(65)).toBe("1分05秒");
    expect(formatEta(3661)).toBe("1时01分01秒");
  });

  it("显示中文任务状态", () => {
    expect(statusLabel("queued")).toBe("等待解析");
    expect(statusLabel("downloading")).toBe("下载中");
    expect(statusLabel("transcoding")).toBe("兼容转换中");
    expect(statusLabel("completed")).toBe("已完成");
    expect(statusLabel("failed")).toBe("失败");
  });
});
