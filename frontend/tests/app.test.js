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
      counts: { completed: 1, failed: 1 },
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
  counts: { completed: 1, failed: 1 },
  tasks: [],
  pause_reason: null,
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
});

test("页面挂载时建立生命周期连接并在卸载时关闭", async () => {
  const wrapper = await mountApp();
  const session = FakeEventSource.instances.find((item) => item.url === "/api/app/session");

  expect(session).toBeDefined();
  expect(session.closed).toBe(false);

  wrapper.unmount();

  expect(session.closed).toBe(true);
});
