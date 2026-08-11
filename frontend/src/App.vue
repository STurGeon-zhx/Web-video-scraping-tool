<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from "vue";

import { formatBytes, formatEta, formatSpeed, statusLabel } from "./format.js";
import { pageAfterDeletion } from "./history.js";
import { resetNewBatchState } from "./new-batch.js";

const inputText = ref("");
const inputElement = ref(null);
const downloadDirectory = ref("");
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
let eventSource = null;
let pageSession = null;

const counts = computed(() => batch.value?.counts || {});
const completedCount = computed(() => (counts.value.completed || 0) + (counts.value.skipped || 0));
const overallProgress = computed(() => {
  if (!batch.value?.total) return 0;
  return Math.round((completedCount.value * 100) / batch.value.total);
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
  document.body.classList.remove("history-drawer-open");
}

function handleKeydown(event) {
  if (event.key === "Escape" && historyOpen.value) closeHistory();
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
  await nextTick();
  window.scrollTo({ top: 0, behavior: "smooth" });
  inputElement.value?.focus();
}

async function viewBatch(batchId) {
  error.value = "";
  try {
    batch.value = await request(`/api/batches/${batchId}`);
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
  pageSession = new EventSource("/api/app/session");
  window.addEventListener("keydown", handleKeydown);
  try {
    await Promise.all([loadSettings(), loadHistory(1)]);
  } catch (reason) {
    error.value = reason.message;
  }
});

onBeforeUnmount(() => {
  closeCurrentEvents();
  pageSession?.close();
  pageSession = null;
  window.removeEventListener("keydown", handleKeydown);
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
        <button class="history-trigger" type="button" @click="openHistory">
          历史批次 <strong>{{ history.total }}</strong>
        </button>
      </div>
    </header>

    <main>
      <section class="hero">
        <p class="eyebrow">简单三步</p>
        <h2>粘贴链接，剩下的交给队列</h2>
        <p>每行一条链接，也可以直接粘贴含文案的平台分享文本。</p>
      </section>

      <section class="panel import-panel">
        <div class="panel-heading">
          <span class="step-number">1</span>
          <div>
            <h3>导入视频链接</h3>
            <p>支持抖音、快手、B站、唯品会商品主视频、视频直链及 yt-dlp 明确支持的视频平台</p>
          </div>
        </div>
        <textarea
          ref="inputElement"
          v-model="inputText"
          aria-label="视频链接列表"
          placeholder="https://www.douyin.com/video/1234567890123456789&#10;https://www.kuaishou.com/f/xxxxxx&#10;https://www.bilibili.com/video/BVxxxxxxxxxx/&#10;https://detail.vip.com/detail-品牌ID-商品ID.html&#10;http://cdn.example.com/video.mp4"
          @blur="previewInput"
        ></textarea>
        <div v-if="preview" class="preview-strip">
          <span class="ok">有效 {{ preview.valid_count }}</span>
          <span>重复 {{ preview.duplicate_count }}</span>
          <span :class="{ warn: preview.invalid_count }">无效 {{ preview.invalid_count }}</span>
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
              <strong>批次 #{{ item.id }}</strong>
              <span>{{ formatCreatedAt(item.created_at) }} · 共 {{ item.total }} 项</span>
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
