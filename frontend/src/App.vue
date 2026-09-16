<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from "vue";

import { formatBytes, formatEta, formatSpeed, statusLabel } from "./format.js";
import { pageAfterDeletion } from "./history.js";
import { resetNewBatchState } from "./new-batch.js";

const inputText = ref("");
const inputElement = ref(null);
const activeView = ref(window.location.hash === "#/pages" ? "pages" : "links");
const pageUrl = ref("");
const pageMaxItems = ref(50);
const downloadDirectory = ref("");
const youtubeNetworkMode = ref("system");
const youtubeProxyUrl = ref("");
const youtubeNetworkBusy = ref(false);
const youtubeNetworkMessage = ref("");
const youtubeNetworkOk = ref(false);
const youtubeAuth = ref({ status: "idle", message: "尚未进行 YouTube 登录验证", running: false, has_saved_state: false });
const youtubeAuthBusy = ref(false);
const preview = ref(null);
const batch = ref(null);
const busy = ref(false);
const pauseBusy = ref(false);
const notice = ref("");
const error = ref("");
const history = ref({ items: [], page: 1, page_size: 20, total: 0, total_pages: 0 });
const historyBusy = ref(false);
const deleteAllBusy = ref(false);
const historyOpen = ref(false);
const appVersion = ref({ current_version: "…", packaged: false, can_self_update: false, prepared_version: null });
const updateOpen = ref(false);
const updateBusy = ref(false);
const updateInfo = ref(null);
const updateError = ref("");
let eventSource = null;
let pageSession = null;
let youtubeAuthTimer = null;

const counts = computed(() => batch.value?.counts || {});
const completedCount = computed(() => (counts.value.completed || 0) + (counts.value.skipped || 0));
const overallProgress = computed(() => {
  if (!batch.value?.total) return 0;
  return Math.round((completedCount.value * 100) / batch.value.total);
});
const isPageView = computed(() => activeView.value === "pages");

const collectionStatusText = computed(() => {
  if (batch.value?.source_mode !== "page") return "";
  const labels = {
    pending: "等待开始采集",
    preflight: "正在检查 YouTube 运行环境与网络",
    waiting_login: "等待网页登录",
    waiting_verification: "等待完成页面验证",
    collecting: "正在采集",
    target_reached: "已达到设定数量",
    insufficient: "未达到设定数量",
    page_ended: "页面已结束",
    login_expired: "登录状态已失效",
    risk_controlled: "页面触发风控",
    no_videos: "没有发现视频",
    stopped: "采集已停止",
  };
  return labels[batch.value.collection_status] || batch.value.collection_status || "等待开始采集";
});

function platformLabel(platform) {
  return {
    douyin: "抖音",
    kuaishou: "快手",
    bilibili: "B站",
    youtube: "YouTube",
    direct: "视频直链",
    vipshop: "唯品会",
  }[platform] || platform || "视频";
}

async function request(url, options = {}) {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.detail || "请求失败");
  return payload;
}

async function loadSettings() {
  const settings = await request("/api/settings");
  downloadDirectory.value = settings.download_directory;
  youtubeNetworkMode.value = settings.youtube_network_mode || "system";
  youtubeProxyUrl.value = settings.youtube_proxy_url || "";
}

async function loadAppVersion() {
  appVersion.value = await request("/api/app/version");
}

function closeUpdate() {
  updateOpen.value = false;
  if (!historyOpen.value) document.body.classList.remove("history-drawer-open");
}

async function checkAppUpdate() {
  updateOpen.value = true;
  updateBusy.value = true;
  updateError.value = "";
  document.body.classList.add("history-drawer-open");
  try {
    updateInfo.value = await request("/api/app/update/check", { method: "POST" });
    appVersion.value = { ...appVersion.value, ...updateInfo.value };
  } catch (reason) {
    updateError.value = reason.message;
  } finally {
    updateBusy.value = false;
  }
}

async function prepareAppUpdate() {
  updateBusy.value = true;
  updateError.value = "";
  try {
    updateInfo.value = await request("/api/app/update/prepare", { method: "POST" });
    appVersion.value = { ...appVersion.value, ...updateInfo.value };
  } catch (reason) {
    updateError.value = reason.message;
  } finally {
    updateBusy.value = false;
  }
}

async function applyAppUpdate() {
  const version = updateInfo.value?.prepared_version || appVersion.value.prepared_version;
  if (!window.confirm(`程序将关闭并更新到 ${version}，完成后自动重新启动。是否继续？`)) return;
  updateBusy.value = true;
  updateError.value = "";
  try {
    updateInfo.value = { ...updateInfo.value, ...(await request("/api/app/update/apply", { method: "POST" })) };
  } catch (reason) {
    updateError.value = reason.message;
    updateBusy.value = false;
  }
}

function youtubeNetworkPayload() {
  return {
    mode: youtubeNetworkMode.value,
    proxy_url: youtubeNetworkMode.value === "manual" ? youtubeProxyUrl.value.trim() : "",
  };
}

async function saveYoutubeNetwork() {
  youtubeNetworkBusy.value = true;
  youtubeNetworkMessage.value = "";
  try {
    const result = await request("/api/settings/youtube-network", {
      method: "POST",
      body: JSON.stringify(youtubeNetworkPayload()),
    });
    youtubeNetworkMode.value = result.youtube_network_mode;
    youtubeProxyUrl.value = result.youtube_proxy_url;
    youtubeNetworkOk.value = true;
    youtubeNetworkMessage.value = result.message;
  } catch (reason) {
    youtubeNetworkOk.value = false;
    youtubeNetworkMessage.value = reason.message;
  } finally {
    youtubeNetworkBusy.value = false;
  }
}

async function testYoutubeNetwork() {
  youtubeNetworkBusy.value = true;
  youtubeNetworkMessage.value = "正在测试 YouTube 连接…";
  try {
    const result = await request("/api/settings/youtube-network/test", {
      method: "POST",
      body: JSON.stringify(youtubeNetworkPayload()),
    });
    youtubeNetworkOk.value = result.ok;
    youtubeNetworkMessage.value = result.message;
  } catch (reason) {
    youtubeNetworkOk.value = false;
    youtubeNetworkMessage.value = reason.message;
  } finally {
    youtubeNetworkBusy.value = false;
  }
}

async function loadYoutubeAuth() {
  youtubeAuth.value = await request("/api/settings/youtube-auth");
  if (!youtubeAuth.value.running && youtubeAuthTimer) {
    window.clearInterval(youtubeAuthTimer);
    youtubeAuthTimer = null;
  }
}

function currentYoutubeTarget() {
  const source = isPageView.value ? pageUrl.value.trim() : inputText.value;
  const match = source.match(/https:\/\/(?:www\.|m\.)?(?:youtube\.com|youtu\.be)\/[^\s]+/i);
  return match?.[0] || null;
}

function startYoutubeAuthPolling() {
  if (youtubeAuthTimer) return;
  youtubeAuthTimer = window.setInterval(() => {
    loadYoutubeAuth().catch((reason) => {
      youtubeAuth.value = { ...youtubeAuth.value, message: reason.message };
    });
  }, 1000);
}

async function startYoutubeAuth() {
  youtubeAuthBusy.value = true;
  try {
    youtubeAuth.value = await request("/api/settings/youtube-auth/start", {
      method: "POST",
      body: JSON.stringify({ target_url: currentYoutubeTarget() }),
    });
    startYoutubeAuthPolling();
  } catch (reason) {
    youtubeAuth.value = { ...youtubeAuth.value, status: "error", message: reason.message };
  } finally {
    youtubeAuthBusy.value = false;
  }
}

async function completeYoutubeAuth() {
  youtubeAuthBusy.value = true;
  try {
    youtubeAuth.value = await request("/api/settings/youtube-auth/complete", { method: "POST" });
  } catch (reason) {
    youtubeAuth.value = { ...youtubeAuth.value, status: "error", message: reason.message };
  } finally {
    youtubeAuthBusy.value = false;
  }
}

async function loadHistory(page = 1) {
  historyBusy.value = true;
  try {
    const payload = await request(`/api/batches?page=${page}&page_size=20`);
    history.value = payload;
    return payload;
  } finally {
    historyBusy.value = false;
  }
}

async function changeHistoryPage(page) {
  error.value = "";
  try {
    await loadHistory(page);
  } catch (reason) {
    error.value = reason.message;
  }
}

async function previewInput() {
  error.value = "";
  if (!inputText.value.trim()) {
    preview.value = null;
    return;
  }
  try {
    preview.value = await request("/api/batches/preview", {
      method: "POST",
      body: JSON.stringify({ text: inputText.value }),
    });
  } catch (reason) {
    error.value = reason.message;
  }
}

async function chooseDirectory() {
  error.value = "";
  try {
    const settings = await request("/api/settings/pick-directory", { method: "POST" });
    downloadDirectory.value = settings.download_directory;
  } catch (reason) {
    error.value = reason.message;
  }
}

async function startBatch() {
  if (!inputText.value.trim()) {
    error.value = "请先粘贴至少一条视频链接";
    return;
  }
  busy.value = true;
  error.value = "";
  notice.value = "";
  try {
    const created = await request("/api/batches", {
      method: "POST",
      body: JSON.stringify({ text: inputText.value, output_dir: downloadDirectory.value }),
    });
    batch.value = created;
    if (created.import_summary) {
      const platforms = Object.entries(created.import_summary.platform_counts || {})
        .map(([platform, count]) => `${platformLabel(platform)} ${count}`)
        .join("、");
      notice.value = `已创建 ${created.import_summary.expanded_count} 个视频任务${platforms ? `（${platforms}）` : ""}`;
    }
    inputText.value = "";
    preview.value = null;
    connectEvents(created.id);
    await loadHistory(1);
  } catch (reason) {
    error.value = reason.message;
  } finally {
    busy.value = false;
  }
}

async function startPageBatch() {
  if (!pageUrl.value.trim()) {
    error.value = "请先粘贴一个包含视频的页面链接";
    return;
  }
  busy.value = true;
  error.value = "";
  notice.value = "";
  try {
    const created = await request("/api/batches", {
      method: "POST",
      body: JSON.stringify({
        text: pageUrl.value.trim(),
        output_dir: downloadDirectory.value,
        source_mode: "page",
        max_items: Number(pageMaxItems.value),
      }),
    });
    batch.value = created;
    notice.value = `已创建页面采集批次，计划采集 ${created.requested_count} 条视频`;
    connectEvents(created.id);
    await loadHistory(1);
  } catch (reason) {
    error.value = reason.message;
  } finally {
    busy.value = false;
  }
}

function navigateTo(view) {
  activeView.value = view;
  window.location.hash = view === "pages" ? "#/pages" : "#/links";
  closeHistory();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function handleHashChange() {
  activeView.value = window.location.hash === "#/pages" ? "pages" : "links";
}

function connectEvents(batchId) {
  eventSource?.close();
  eventSource = new EventSource(`/api/events?batchId=${batchId}`);
  eventSource.addEventListener("batch", (event) => {
    const updated = JSON.parse(event.data);
    batch.value = updated;
    const summary = history.value.items.find((item) => item.id === updated.id);
    if (summary) {
      summary.counts = updated.counts;
      summary.total = updated.total;
      summary.collected_count = updated.collected_count;
      summary.collection_status = updated.collection_status;
    }
  });
  eventSource.onerror = () => {
    notice.value = "进度连接正在自动重连，后台下载不受影响";
  };
}

function closeCurrentEvents() {
  eventSource?.close();
  eventSource = null;
}

function openHistory() {
  historyOpen.value = true;
  document.body.classList.add("history-drawer-open");
}

function closeHistory() {
  historyOpen.value = false;
  if (!updateOpen.value) document.body.classList.remove("history-drawer-open");
}

function handleKeydown(event) {
  if (event.key !== "Escape") return;
  if (updateOpen.value && !updateBusy.value) closeUpdate();
  else if (historyOpen.value) closeHistory();
}

async function newBatch() {
  closeHistory();
  resetNewBatchState({
    inputText,
    preview,
    batch,
    notice,
    error,
    closeCurrentEvents,
  });
  pageUrl.value = "";
  pageMaxItems.value = 50;
  await nextTick();
  window.scrollTo({ top: 0, behavior: "smooth" });
  inputElement.value?.focus();
}

async function viewBatch(batchId) {
  error.value = "";
  try {
    batch.value = await request(`/api/batches/${batchId}`);
    navigateTo(batch.value.source_mode === "page" ? "pages" : "links");
    connectEvents(batchId);
    closeHistory();
  } catch (reason) {
    error.value = reason.message;
  }
}

async function deleteBatch(batchId) {
  const confirmed = window.confirm(
    "删除后，批次记录会移除，未完成下载会取消并清理分片；已完成的视频文件会保留。确定删除吗？",
  );
  if (!confirmed) return;
  error.value = "";
  try {
    await request(`/api/batches/${batchId}`, { method: "DELETE" });
    if (batch.value?.id === batchId) {
      eventSource?.close();
      eventSource = null;
      batch.value = null;
    }
    const currentPage = history.value.page;
    const loaded = await loadHistory(currentPage);
    const targetPage = pageAfterDeletion(currentPage, loaded.items.length);
    if (targetPage !== currentPage) await loadHistory(targetPage);
    notice.value = `已删除批次 #${batchId}，已完成视频文件仍保留在磁盘中`;
  } catch (reason) {
    error.value = reason.message;
  }
}

async function deleteAllBatches() {
  if (deleteAllBusy.value || history.value.total === 0) return;
  const confirmed = window.confirm(
    `确定删除全部 ${history.value.total} 个批次吗？未完成下载将取消并清理分片；已完成视频文件和保存目录会保留。此操作无法撤销。`,
  );
  if (!confirmed) return;
  deleteAllBusy.value = true;
  error.value = "";
  notice.value = "";
  try {
    const result = await request("/api/batches", { method: "DELETE" });
    closeCurrentEvents();
    batch.value = null;
    await loadHistory(1);
    notice.value = `已删除 ${result.count} 个批次，已完成视频文件仍保留在磁盘中`;
  } catch (reason) {
    error.value = reason.message;
  } finally {
    deleteAllBusy.value = false;
  }
}

function formatCreatedAt(value) {
  return new Date(value).toLocaleString("zh-CN", { hour12: false });
}

async function retryFailed() {
  if (!batch.value) return;
  const result = await request(`/api/batches/${batch.value.id}/retry-failed`, { method: "POST" });
  notice.value = result.retried ? `已重新加入 ${result.retried} 条失败任务` : "没有可重试的失败任务";
}

async function toggleBatchPause() {
  if (!batch.value || pauseBusy.value) return;
  pauseBusy.value = true;
  error.value = "";
  try {
    const action = batch.value.paused ? "resume" : "pause";
    batch.value = await request(`/api/batches/${batch.value.id}/${action}`, { method: "POST" });
    notice.value = batch.value.paused ? "当前批次已暂停" : "当前批次已继续下载";
  } catch (reason) {
    error.value = reason.message;
  } finally {
    pauseBusy.value = false;
  }
}

async function openDirectory() {
  try {
    await request("/api/settings/open-directory", { method: "POST" });
  } catch (reason) {
    error.value = reason.message;
  }
}

onMounted(async () => {
  if (!window.location.hash || !["#/links", "#/pages"].includes(window.location.hash)) {
    window.location.hash = "#/links";
  }
  handleHashChange();
  pageSession = new EventSource("/api/app/session");
  window.addEventListener("keydown", handleKeydown);
  window.addEventListener("hashchange", handleHashChange);
  try {
    await Promise.all([loadSettings(), loadHistory(1), loadYoutubeAuth(), loadAppVersion()]);
    if (youtubeAuth.value.running) startYoutubeAuthPolling();
  } catch (reason) {
    error.value = reason.message;
  }
});

onBeforeUnmount(() => {
  closeCurrentEvents();
  pageSession?.close();
  pageSession = null;
  if (youtubeAuthTimer) window.clearInterval(youtubeAuthTimer);
  youtubeAuthTimer = null;
  window.removeEventListener("keydown", handleKeydown);
  window.removeEventListener("hashchange", handleHashChange);
  document.body.classList.remove("history-drawer-open");
});
</script>

<template>
  <div class="app-shell">
    <header class="topbar">
      <div class="brand">
        <div class="brand-mark">⬇</div>
        <div>
          <h1>视频批量下载工具</h1>
          <p>公开视频 · 本机处理 · 批量队列</p>
        </div>
      </div>
      <div class="topbar-actions">
        <span class="privacy-badge"><i></i> 数据仅保存在本机</span>
        <button class="version-trigger" type="button" @click="checkAppUpdate">
          v{{ appVersion.current_version }}
        </button>
        <button
          v-if="!isPageView"
          class="feature-nav-button page-download-nav"
          type="button"
          @click="navigateTo('pages')"
        >页面批量下载</button>
        <button
          v-else
          class="feature-nav-button links-download-nav"
          type="button"
          @click="navigateTo('links')"
        >返回链接下载</button>
        <button class="history-trigger" type="button" @click="openHistory">
          历史批次 <strong>{{ history.total }}</strong>
        </button>
      </div>
    </header>

    <main>
      <section v-if="!isPageView" class="hero">
        <p class="eyebrow">简单三步</p>
        <h2>粘贴链接，剩下的交给队列</h2>
        <p>每行一条链接，也可以直接粘贴含文案的平台分享文本。</p>
      </section>

      <section v-if="!isPageView" class="panel import-panel">
        <div class="panel-heading">
          <span class="step-number">1</span>
          <div>
            <h3>导入视频链接</h3>
            <p>支持抖音、快手、B站、YouTube 单视频与 Shorts、唯品会商品主视频、视频直链及 yt-dlp 明确支持的视频平台</p>
          </div>
        </div>
        <textarea
          ref="inputElement"
          v-model="inputText"
          aria-label="视频链接列表"
          placeholder="https://www.douyin.com/video/1234567890123456789&#10;https://www.youtube.com/watch?v=BaW_jenozKc&#10;https://youtu.be/BaW_jenozKc&#10;https://www.youtube.com/shorts/BaW_jenozKc&#10;https://www.bilibili.com/video/BVxxxxxxxxxx/&#10;http://cdn.example.com/video.mp4"
          @blur="previewInput"
        ></textarea>
        <div v-if="preview" class="preview-strip">
          <span class="ok">有效 {{ preview.valid_count }}</span>
          <span>重复 {{ preview.duplicate_count }}</span>
          <span :class="{ warn: preview.invalid_count }">无效 {{ preview.invalid_count }}</span>
        </div>

        <div class="youtube-network-card youtube-network-card-links">
          <div class="youtube-network-heading">
            <div><strong>YouTube 网络</strong><small>仅影响 YouTube 采集和下载</small></div>
            <select v-model="youtubeNetworkMode" aria-label="YouTube 网络模式">
              <option value="system">跟随本机网络 / VPN</option>
              <option value="manual">本地代理</option>
            </select>
          </div>
          <input v-if="youtubeNetworkMode === 'manual'" v-model="youtubeProxyUrl" class="proxy-input" aria-label="YouTube 本地代理地址" placeholder="http://127.0.0.1:7890 或 socks5://127.0.0.1:7891" autocomplete="off" />
          <p class="youtube-network-help">浏览器代理插件不会影响桌面工具；使用全局/TUN VPN 时请选择“跟随本机网络”。连接测试只检查网络通道，不代表所有视频均可匿名访问。</p>
          <div class="youtube-network-actions">
            <span v-if="youtubeNetworkMessage" :class="youtubeNetworkOk ? 'network-ok' : 'network-error'">{{ youtubeNetworkMessage }}</span>
            <button class="secondary-button" type="button" :disabled="youtubeNetworkBusy" @click="testYoutubeNetwork">测试连接</button>
            <button class="secondary-button" type="button" :disabled="youtubeNetworkBusy" @click="saveYoutubeNetwork">保存设置</button>
          </div>
          <div class="youtube-auth-row">
            <div>
              <strong>YouTube 登录验证</strong>
              <small>{{ youtubeAuth.message }}</small>
              <small>使用工具独立的 Edge 配置，不读取日常浏览器 Cookie；验证窗口会一直保留到你手动完成。</small>
            </div>
            <button
              class="secondary-button youtube-auth-button"
              type="button"
              :disabled="youtubeAuthBusy"
              @click="youtubeAuth.running ? completeYoutubeAuth() : startYoutubeAuth()"
            >{{ youtubeAuth.running ? "完成验证" : "打开登录验证" }}</button>
          </div>
        </div>

        <div class="directory-row">
          <span class="step-number">2</span>
          <div class="directory-copy">
            <strong>保存到</strong>
            <span :title="downloadDirectory">{{ downloadDirectory || "正在读取默认目录…" }}</span>
          </div>
          <button class="secondary-button" type="button" @click="chooseDirectory">选择目录</button>
        </div>

        <div v-if="error" class="alert error-alert">{{ error }}</div>
        <div v-if="notice" class="alert notice-alert">{{ notice }}</div>

        <div class="start-row">
          <div class="start-note">仅下载你拥有版权或已获授权的公开内容</div>
          <button class="primary-button" type="button" :disabled="busy" @click="startBatch">
            <span class="step-number inverted">3</span>
            {{ busy ? "正在创建任务…" : "开始批量下载" }}
          </button>
        </div>
      </section>

      <section v-if="isPageView" class="hero page-hero">
        <p class="eyebrow">页面视频采集</p>
        <h2>页面内视频批量下载</h2>
        <p>粘贴一个页面链接，设定数量后自动滚动采集并加入下载队列。</p>
      </section>

      <section v-if="isPageView" class="panel import-panel page-import-panel">
        <div class="panel-heading">
          <span class="step-number">1</span>
          <div>
            <h3>导入页面链接</h3>
            <p>支持 YouTube 播放列表和频道视频页；抖音搜索页会打开工具专用 Edge 窗口供你登录。</p>
          </div>
        </div>
        <textarea
          v-model="pageUrl"
          aria-label="页面链接"
          placeholder="https://www.youtube.com/playlist?list=播放列表ID&#10;https://www.youtube.com/@频道名/videos&#10;https://www.douyin.com/search/美食?type=video"
        ></textarea>

        <div class="page-options">
          <label class="count-field">
            <span class="step-number">2</span>
            <span class="count-copy">
              <strong>下载数量</strong>
              <small>按页面当前顺序采集，范围 1–500 条</small>
            </span>
            <input v-model.number="pageMaxItems" type="number" min="1" max="500" />
          </label>
        </div>

        <div class="directory-row">
          <span class="step-number">3</span>
          <div class="directory-copy">
            <strong>保存到</strong>
            <span :title="downloadDirectory">{{ downloadDirectory || "正在读取默认目录…" }}</span>
          </div>
          <button class="secondary-button" type="button" @click="chooseDirectory">选择目录</button>
        </div>

        <div v-if="isPageView" class="youtube-network-card">
          <div class="youtube-network-heading">
            <div><strong>YouTube 网络</strong><small>仅影响 YouTube 采集和下载</small></div>
            <select v-model="youtubeNetworkMode" aria-label="YouTube 网络模式">
              <option value="system">跟随本机网络 / VPN</option>
              <option value="manual">本地代理</option>
            </select>
          </div>
          <input v-if="youtubeNetworkMode === 'manual'" v-model="youtubeProxyUrl" class="proxy-input" aria-label="YouTube 本地代理地址" placeholder="http://127.0.0.1:7890 或 socks5://127.0.0.1:7891" autocomplete="off" />
          <p class="youtube-network-help">浏览器代理插件不会影响桌面工具；使用全局/TUN VPN 时请选择“跟随本机网络”。连接测试只检查网络通道，不代表所有视频均可匿名访问。</p>
          <div class="youtube-network-actions">
            <span v-if="youtubeNetworkMessage" :class="youtubeNetworkOk ? 'network-ok' : 'network-error'">{{ youtubeNetworkMessage }}</span>
            <button class="secondary-button" type="button" :disabled="youtubeNetworkBusy" @click="testYoutubeNetwork">测试连接</button>
            <button class="secondary-button" type="button" :disabled="youtubeNetworkBusy" @click="saveYoutubeNetwork">保存设置</button>
          </div>
          <div class="youtube-auth-row">
            <div>
              <strong>YouTube 登录验证</strong>
              <small>{{ youtubeAuth.message }}</small>
              <small>使用工具独立的 Edge 配置，不读取日常浏览器 Cookie；验证窗口会一直保留到你手动完成。</small>
            </div>
            <button
              class="secondary-button youtube-auth-button"
              type="button"
              :disabled="youtubeAuthBusy"
              @click="youtubeAuth.running ? completeYoutubeAuth() : startYoutubeAuth()"
            >{{ youtubeAuth.running ? "完成验证" : "打开登录验证" }}</button>
          </div>
        </div>

        <div v-if="error" class="alert error-alert">{{ error }}</div>
        <div v-if="notice" class="alert notice-alert">{{ notice }}</div>

        <div class="start-row">
          <div class="start-note">仅采集你有权下载的公开内容；不支持 DRM、私密或付费内容</div>
          <button class="primary-button page-start-button" type="button" :disabled="busy" @click="startPageBatch">
            <span class="step-number inverted">4</span>
            {{ busy ? "正在创建采集任务…" : "开始采集并下载" }}
          </button>
        </div>
      </section>

      <section v-if="batch" class="panel task-panel">
        <div class="task-header">
          <div>
            <p class="eyebrow">批次 #{{ batch.id }}</p>
            <h3>下载进度</h3>
          </div>
          <div class="task-actions">
            <button class="secondary-button pause-button" type="button" :disabled="pauseBusy" @click="toggleBatchPause">
              {{ batch.paused ? "继续下载" : "暂停下载" }}
            </button>
            <button class="secondary-button" type="button" @click="retryFailed">重试失败项</button>
            <button class="secondary-button" type="button" @click="openDirectory">打开下载目录</button>
          </div>
        </div>

        <div v-if="batch.source_mode === 'page'" class="collection-status">
          <div>
            <strong>{{ collectionStatusText }}</strong>
            <span>已采集 {{ batch.collected_count || 0 }} / {{ batch.requested_count || 0 }} 条</span>
          </div>
          <p v-if="batch.collection_stop_reason">{{ batch.collection_stop_reason }}</p>
        </div>

        <div class="metrics">
          <div><span>总任务</span><strong>{{ batch.total }}</strong></div>
          <div><span>等待</span><strong>{{ counts.queued || 0 }}</strong></div>
          <div><span>处理中</span><strong>{{ (counts.resolving || 0) + (counts.downloading || 0) + (counts.merging || 0) + (counts.transcoding || 0) }}</strong></div>
          <div class="success"><span>完成</span><strong>{{ counts.completed || 0 }}</strong></div>
          <div class="failure"><span>失败</span><strong>{{ counts.failed || 0 }}</strong></div>
        </div>

        <div class="overall-progress">
          <div><span>整体进度</span><strong>{{ overallProgress }}%</strong></div>
          <div class="progress-track"><i :style="{ width: `${overallProgress}%` }"></i></div>
        </div>
        <div v-if="batch.paused" class="alert notice-alert batch-paused-alert">当前批次已暂停，点击“继续下载”恢复</div>
        <div v-if="batch.pause_reason" class="alert error-alert">{{ batch.pause_reason }}</div>

        <div class="table-wrap">
          <table>
            <thead><tr><th>视频</th><th>状态</th><th>进度</th><th>速度</th><th>剩余</th><th>说明</th></tr></thead>
            <tbody>
              <tr v-for="task in batch.tasks" :key="task.id">
                <td class="video-cell">
                  <strong><span class="platform-badge">{{ platformLabel(task.platform) }}</span>{{ task.title || `待解析视频 #${task.id}` }}</strong>
                  <span>{{ task.video_id || task.original_url }}</span>
                </td>
                <td><span class="status-pill" :class="task.status">{{ statusLabel(task.status) }}</span></td>
                <td class="progress-cell">
                  <div class="mini-track"><i :style="{ width: `${task.progress || 0}%` }"></i></div>
                  <span>{{ Math.round(task.progress || 0) }}%<template v-if="task.total_bytes"> · {{ formatBytes(task.downloaded_bytes) }}/{{ formatBytes(task.total_bytes) }}</template></span>
                </td>
                <td>{{ formatSpeed(task.speed) }}</td>
                <td>{{ formatEta(task.eta) }}</td>
                <td class="message-cell">{{ task.error_message || "—" }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>

      <footer>本工具不会上传链接、Cookie 或视频文件。请遵守内容版权和平台规则。</footer>
    </main>

    <div v-if="updateOpen" class="update-overlay" @click.self="!updateBusy && closeUpdate()">
      <section class="update-dialog" role="dialog" aria-modal="true" aria-labelledby="update-dialog-title">
        <div class="update-dialog-header">
          <div>
            <p class="eyebrow">软件更新</p>
            <h3 id="update-dialog-title">版本检查</h3>
          </div>
          <button class="history-drawer-close" type="button" aria-label="关闭版本检查" :disabled="updateBusy" @click="closeUpdate">×</button>
        </div>
        <div class="update-version-row">
          <span>当前版本</span><strong>v{{ appVersion.current_version }}</strong>
          <template v-if="updateInfo?.latest_version">
            <span>最新版本</span><strong>v{{ updateInfo.latest_version }}</strong>
          </template>
        </div>
        <div v-if="updateBusy" class="update-message">{{ updateInfo?.applying ? "程序正在退出并应用更新…" : "正在连接 GitHub，请稍候…" }}</div>
        <div v-else-if="updateError" class="alert error-alert">{{ updateError }}</div>
        <template v-else-if="updateInfo">
          <div class="update-message" :class="{ 'update-available': updateInfo.update_available }">{{ updateInfo.message }}</div>
          <p v-if="updateInfo.release_title" class="update-title">{{ updateInfo.release_title }}</p>
          <pre v-if="updateInfo.release_notes" class="update-notes">{{ updateInfo.release_notes }}</pre>
          <p v-if="updateInfo.download_size" class="update-size">下载大小：{{ formatBytes(updateInfo.download_size) }}</p>
          <div class="update-actions">
            <a v-if="updateInfo.release_url" class="secondary-button update-link" :href="updateInfo.release_url" target="_blank" rel="noopener">查看发布页</a>
            <button
              v-if="updateInfo.update_available && updateInfo.can_self_update && !updateInfo.prepared"
              class="primary-button"
              type="button"
              @click="prepareAppUpdate"
            >下载并校验更新</button>
            <button
              v-if="updateInfo.prepared || updateInfo.prepared_version"
              class="primary-button"
              type="button"
              @click="applyAppUpdate"
            >立即重启并更新</button>
          </div>
          <p v-if="updateInfo.update_available && !updateInfo.can_self_update" class="update-help">当前运行方式不会自动覆盖程序文件，请从发布页手动下载。</p>
        </template>
      </section>
    </div>

    <div v-if="historyOpen" class="history-overlay" @click.self="closeHistory">
      <aside class="history-drawer" role="dialog" aria-modal="true" aria-labelledby="history-drawer-title">
        <div class="history-drawer-header">
          <div>
            <p class="eyebrow">本机记录</p>
            <h3 id="history-drawer-title">批次历史</h3>
          </div>
          <div class="history-drawer-actions">
            <button class="new-batch-button" type="button" @click="newBatch">新建批次</button>
            <button class="history-drawer-close" type="button" aria-label="关闭批次历史" @click="closeHistory">×</button>
          </div>
        </div>
        <div class="history-toolbar">
          <p class="history-total">共 {{ history.total }} 个批次</p>
          <button
            class="delete-all-batches-button"
            type="button"
            :disabled="deleteAllBusy || historyBusy || history.total === 0"
            @click="deleteAllBatches"
          >{{ deleteAllBusy ? "正在删除…" : "一键删除所有批次" }}</button>
        </div>
        <div v-if="historyBusy" class="history-empty">正在加载批次历史…</div>
        <div v-else-if="!history.items.length" class="history-empty">暂无批次记录</div>
        <div v-else class="history-list">
          <article v-for="item in history.items" :key="item.id" class="history-item">
            <div class="history-copy">
              <strong><span class="history-type-badge">{{ item.source_mode === "page" ? "页面批次" : "链接批次" }}</span>批次 #{{ item.id }}</strong>
              <span>{{ formatCreatedAt(item.created_at) }} · 共 {{ item.total }} 项</span>
              <span v-if="item.source_mode === 'page'">已采集 {{ item.collected_count || 0 }} / {{ item.requested_count || 0 }} 条</span>
              <span :title="item.output_dir">{{ item.output_dir }}</span>
            </div>
            <div class="history-counts">
              <span>等待 {{ item.counts.queued || 0 }}</span>
              <span>处理中 {{ (item.counts.resolving || 0) + (item.counts.downloading || 0) + (item.counts.merging || 0) + (item.counts.transcoding || 0) }}</span>
              <span class="success-text">完成 {{ (item.counts.completed || 0) + (item.counts.skipped || 0) }}</span>
              <span class="failure-text">失败 {{ item.counts.failed || 0 }}</span>
            </div>
            <div class="history-actions">
              <button class="secondary-button" type="button" @click="viewBatch(item.id)">查看</button>
              <button class="danger-button" type="button" @click="deleteBatch(item.id)">删除</button>
            </div>
          </article>
        </div>
        <div v-if="history.total_pages > 1" class="history-pagination">
          <button class="secondary-button" type="button" :disabled="historyBusy || history.page <= 1" @click="changeHistoryPage(history.page - 1)">上一页</button>
          <span>第 {{ history.page }} / {{ history.total_pages }} 页</span>
          <button class="secondary-button" type="button" :disabled="historyBusy || history.page >= history.total_pages" @click="changeHistoryPage(history.page + 1)">下一页</button>
        </div>
      </aside>
    </div>
  </div>
</template>
