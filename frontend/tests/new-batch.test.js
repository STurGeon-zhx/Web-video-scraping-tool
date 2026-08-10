import { describe, expect, test } from "vitest";

import { resetNewBatchState } from "../src/new-batch.js";

describe("resetNewBatchState", () => {
  test("只清空当前导入与批次视图，并关闭当前进度连接", () => {
    const inputText = { value: "https://www.douyin.com/video/123" };
    const preview = { value: { valid_count: 1 } };
    const batch = { value: { id: 7 } };
    const notice = { value: "下载已完成" };
    const error = { value: "网络错误" };
    const history = { items: [{ id: 7 }], total: 1 };
    let connectionClosed = false;

    resetNewBatchState({
      inputText,
      preview,
      batch,
      notice,
      error,
      closeCurrentEvents() {
        connectionClosed = true;
      },
    });

    expect(inputText.value).toBe("");
    expect(preview.value).toBeNull();
    expect(batch.value).toBeNull();
    expect(notice.value).toBe("");
    expect(error.value).toBe("");
    expect(connectionClosed).toBe(true);
    expect(history).toEqual({ items: [{ id: 7 }], total: 1 });
  });
});
