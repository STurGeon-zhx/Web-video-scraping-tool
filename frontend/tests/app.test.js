// @vitest-environment jsdom

import { flushPromises, mount } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { nextTick } from "vue";

import App from "../src/App.vue";

const historyPayload = {
  items: [
    {
      id: 5,
      total: 2,
      output_dir: "D:\\Videos",
      created_at: "2026-08-10T08:00:00+00:00",
      counts: { completed: 1, transcoding: 1 },
    },
  ],
  page: 1,
  page_size: 20,
  total: 1,
  total_pages: 1,
};

const batchPayload = {
  id: 5,
  total: 2,
  output_dir: "D:\\Videos",
  created_at: "2026-08-10T08:00:00+00:00",
  counts: { completed: 1, transcoding: 1 },
  tasks: [
    {
      id: 1,
      platform: "direct",
      video_id: "0123456789abcdef",
      title: "测试视频",
      status: "completed",
      progress: 100,
      downloaded_bytes: 100,
      total_bytes: 100,
      speed: null,
      eta: null,
      error_message: null,
    },
  ],
  pause_reason: null,
  paused: false,
};

class FakeEventSource {
  static instances = [];

  constructor(url) {
    this.url = url;
    this.closed = false;
    FakeEventSource.instances.push(this);
  }

  addEventListener() {}

  close() {
    this.closed = true;
  }
}

function jsonResponse(payload) {
  return {
    ok: true,
    async json() {
      return payload;
    },
  };
}

async function mountApp() {
  const wrapper = mount(App);
  await flushPromises();
  return wrapper;
}

test("页面使用多平台名称和链接提示", async () => {
  const wrapper = await mountApp();

  expect(wrapper.get(".brand h1").text()).toBe("视频批量下载工具");
  expect(wrapper.get("textarea").attributes("aria-label")).toBe("视频链接列表");
  expect(wrapper.get(".import-panel").text()).toContain("抖音、快手、B站");
  expect(wrapper.get(".import-panel").text()).toContain("视频直链");
  expect(wrapper.get(".import-panel").text()).toContain("唯品会");
  expect(wrapper.get("textarea").attributes("placeholder")).toContain(".mp4");
  wrapper.unmount();
});

test("任务列表展示视频直链平台标签", async () => {
  const wrapper = await mountApp();
  await wrapper.get(".history-trigger").trigger("click");
  await wrapper.get(".history-actions .secondary-button").trigger("click");
  await flushPromises();

  expect(wrapper.get(".platform-badge").text()).toBe("视频直链");
  wrapper.unmount();
});

test("任务列表展示唯品会平台标签", async () => {
  batchPayload.tasks[0].platform = "vipshop";
  const wrapper = await mountApp();
  await wrapper.get(".history-trigger").trigger("click");
  await wrapper.get(".history-actions .secondary-button").trigger("click");
  await flushPromises();

  expect(wrapper.get(".platform-badge").text()).toBe("唯品会");
  wrapper.unmount();
  batchPayload.tasks[0].platform = "direct";
});

beforeEach(() => {
  FakeEventSource.instances = [];
  vi.stubGlobal("EventSource", FakeEventSource);
  vi.stubGlobal("fetch", vi.fn(async (url) => {
    if (url === "/api/settings") {
      return jsonResponse({ download_directory: "D:\\Videos" });
    }
    if (url === "/api/batches?page=1&page_size=20") {
      return jsonResponse(historyPayload);
    }
    if (url === "/api/batches/5") {
      return jsonResponse(batchPayload);
    }
    if (url === "/api/batches/5/pause") {
      return jsonResponse({ ...batchPayload, paused: true });
    }
    if (url === "/api/batches/5/resume") {
      return jsonResponse({ ...batchPayload, paused: false });
    }
    throw new Error(`未处理的测试请求: ${url}`);
  }));
  vi.stubGlobal("scrollTo", vi.fn());
});

afterEach(() => {
  document.body.className = "";
  vi.unstubAllGlobals();
});

describe("历史批次抽屉", () => {
  test("右上角入口打开包含本机批次的抽屉，并移除底部历史面板", async () => {
    const wrapper = await mountApp();

    expect(wrapper.find(".history-panel").exists()).toBe(false);
    expect(wrapper.get(".history-trigger").text()).toContain("历史批次 1");
    await wrapper.get(".history-trigger").trigger("click");

    expect(wrapper.get(".history-drawer").attributes("aria-modal")).toBe("true");
    expect(wrapper.get(".history-drawer").text()).toContain("批次 #5");
    expect(document.body.classList.contains("history-drawer-open")).toBe(true);
    wrapper.unmount();
  });

  test("历史批次处理中数量包含兼容转换任务", async () => {
    const wrapper = await mountApp();
    await wrapper.get(".history-trigger").trigger("click");

    expect(wrapper.get(".history-counts").text()).toContain("处理中 1");
    wrapper.unmount();
  });

  test("按 Escape 关闭抽屉并恢复页面滚动", async () => {
    const wrapper = await mountApp();
    await wrapper.get(".history-trigger").trigger("click");

    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    await nextTick();

    expect(wrapper.find(".history-drawer").exists()).toBe(false);
    expect(document.body.classList.contains("history-drawer-open")).toBe(false);
    wrapper.unmount();
  });

  test("查看批次后关闭抽屉并显示批次详情", async () => {
    const wrapper = await mountApp();
    await wrapper.get(".history-trigger").trigger("click");

    await wrapper.get(".history-actions .secondary-button").trigger("click");
    await flushPromises();

    expect(wrapper.find(".history-drawer").exists()).toBe(false);
    expect(wrapper.get(".task-panel").text()).toContain("批次 #5");
    wrapper.unmount();
  });

  test("取消确认时不删除全部批次", async () => {
    vi.stubGlobal("confirm", vi.fn(() => false));
    const wrapper = await mountApp();
    await wrapper.get(".history-trigger").trigger("click");

    await wrapper.get(".delete-all-batches-button").trigger("click");

    expect(confirm).toHaveBeenCalledWith(
      "确定删除全部 1 个批次吗？未完成下载将取消并清理分片；已完成视频文件和保存目录会保留。此操作无法撤销。",
    );
    expect(fetch).not.toHaveBeenCalledWith(
      "/api/batches",
      expect.objectContaining({ method: "DELETE" }),
    );
    wrapper.unmount();
  });

  test("确认后一次删除全部批次并清空当前详情", async () => {
    vi.stubGlobal("confirm", vi.fn(() => true));
    const wrapper = await mountApp();
    await wrapper.get(".history-trigger").trigger("click");
    await wrapper.get(".history-actions .secondary-button").trigger("click");
    await flushPromises();
    const batchEvents = FakeEventSource.instances.find(
      (item) => item.url === "/api/events?batchId=5",
    );
    await wrapper.get(".history-trigger").trigger("click");
    fetch.mockImplementation(async (url, options = {}) => {
      if (url === "/api/batches" && options.method === "DELETE") {
        return jsonResponse({ deleted: true, count: 1 });
      }
      if (url === "/api/batches?page=1&page_size=20") {
        return jsonResponse({
          items: [],
          page: 1,
          page_size: 20,
          total: 0,
          total_pages: 0,
        });
      }
      throw new Error(`未处理的测试请求: ${url}`);
    });

    await wrapper.get(".delete-all-batches-button").trigger("click");
    await flushPromises();

    expect(fetch).toHaveBeenCalledWith(
      "/api/batches",
      expect.objectContaining({ method: "DELETE" }),
    );
    expect(batchEvents.closed).toBe(true);
    expect(wrapper.find(".task-panel").exists()).toBe(false);
    expect(wrapper.get(".history-empty").text()).toBe("暂无批次记录");
    expect(wrapper.text()).toContain("已删除 1 个批次，已完成视频文件仍保留在磁盘中");
    wrapper.unmount();
  });

  test("删除全部批次期间禁用按钮并显示忙碌状态", async () => {
    vi.stubGlobal("confirm", vi.fn(() => true));
    const wrapper = await mountApp();
    await wrapper.get(".history-trigger").trigger("click");
    let resolveDelete;
    fetch.mockImplementation((url, options = {}) => {
      if (url === "/api/batches" && options.method === "DELETE") {
        return new Promise((resolve) => {
          resolveDelete = resolve;
        });
      }
      if (url === "/api/batches?page=1&page_size=20") {
        return Promise.resolve(jsonResponse({
          items: [],
          page: 1,
          page_size: 20,
          total: 0,
          total_pages: 0,
        }));
      }
      throw new Error(`未处理的测试请求: ${url}`);
    });

    await wrapper.get(".delete-all-batches-button").trigger("click");

    const button = wrapper.get(".delete-all-batches-button");
    expect(button.attributes("disabled")).toBeDefined();
    expect(button.text()).toBe("正在删除…");

    resolveDelete(jsonResponse({ deleted: true, count: 1 }));
    await flushPromises();
    wrapper.unmount();
  });
});

test("页面挂载时建立生命周期连接并在卸载时关闭", async () => {
  const wrapper = await mountApp();
  const session = FakeEventSource.instances.find((item) => item.url === "/api/app/session");

  expect(session).toBeDefined();
  expect(session.closed).toBe(false);

  wrapper.unmount();

  expect(session.closed).toBe(true);
});

test("当前批次可暂停并继续下载", async () => {
  const wrapper = await mountApp();
  await wrapper.get(".history-trigger").trigger("click");
  await wrapper.get(".history-actions .secondary-button").trigger("click");
  await flushPromises();

  const pauseButton = wrapper.get(".pause-button");
  expect(pauseButton.text()).toBe("暂停下载");

  await pauseButton.trigger("click");
  await flushPromises();
  expect(wrapper.get(".pause-button").text()).toBe("继续下载");
  expect(wrapper.get(".batch-paused-alert").text()).toContain("已暂停");

  await wrapper.get(".pause-button").trigger("click");
  await flushPromises();
  expect(wrapper.get(".pause-button").text()).toBe("暂停下载");
});

test("当前批次处理中数量包含兼容转换任务", async () => {
  const wrapper = await mountApp();
  await wrapper.get(".history-trigger").trigger("click");
  await wrapper.get(".history-actions .secondary-button").trigger("click");
  await flushPromises();

  expect(wrapper.get(".metrics").text()).toContain("处理中1");
  wrapper.unmount();
});
