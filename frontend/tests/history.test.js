import { describe, expect, it } from "vitest";

import { pageAfterDeletion } from "../src/history.js";

describe("批次历史分页", () => {
  it("删除当前页最后一项后回退一页", () => {
    expect(pageAfterDeletion(3, 0)).toBe(2);
  });

  it("第一页或当前页仍有记录时保持页码", () => {
    expect(pageAfterDeletion(1, 0)).toBe(1);
    expect(pageAfterDeletion(3, 2)).toBe(3);
  });
});
