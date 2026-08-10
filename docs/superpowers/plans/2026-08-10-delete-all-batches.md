# Delete All Batches Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在历史批次抽屉提供一次确认即可安全删除当前全部批次记录的按钮，同时保留成品视频。

**Architecture:** 后端通过批次 ID 快照复用现有单批次取消和删除逻辑，前端只发起一次批量删除请求并重置当前批次与历史状态。SQLite 外键级联继续负责任务记录删除，队列继续负责取消任务和清理分片。

**Tech Stack:** FastAPI、SQLite、Vue 3、pytest、Vitest、Vite

## Global Constraints

- 不修改数据库结构。
- 不删除已完成视频和保存目录。
- 不增加新的前端依赖。
- 只删除点击时已经存在的批次。

---

### Task 1: 批量删除后端接口

**Files:**
- Modify: `backend/douyin_downloader/store.py`
- Modify: `backend/douyin_downloader/api.py`
- Test: `tests/test_store.py`
- Test: `tests/test_api.py`

**Interfaces:**
- Produces: `Database.list_batch_ids() -> list[int]`
- Produces: `DELETE /api/batches -> {"deleted": bool, "count": int}`

- [x] **Step 1: Write the failing store and API tests**

```python
def test_lists_all_batch_ids_newest_first(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    ids = [database.create_batch([url], tmp_path) for url in urls]
    assert database.list_batch_ids() == list(reversed(ids))

def test_delete_all_batches_cancels_snapshot_before_deleting_records(tmp_path: Path) -> None:
    response = client.delete("/api/batches")
    assert response.json() == {"deleted": True, "count": 2}
    assert queue.cancel_calls == list(reversed(batch_ids))
```

- [x] **Step 2: Run focused tests and verify they fail because the API and method do not exist**

Run: `python -m pytest tests/test_store.py tests/test_api.py -k "list_batch_ids or delete_all_batches" -q`

- [x] **Step 3: Add the minimal ID query and route**

```python
def list_batch_ids(self) -> list[int]:
    with self._connect() as connection:
        return [int(row["id"]) for row in connection.execute("SELECT id FROM batches ORDER BY id DESC")]

@app.delete("/api/batches")
async def delete_all_batches() -> dict:
    batch_ids = database.list_batch_ids()
    for batch_id in batch_ids:
        queue.cancel_batch(batch_id)
    deleted = sum(database.delete_batch(batch_id) for batch_id in batch_ids)
    return {"deleted": True, "count": deleted}
```

- [x] **Step 4: Run focused tests and verify they pass**

Run: `python -m pytest tests/test_store.py tests/test_api.py -k "list_batch_ids or delete_all_batches" -q`

### Task 2: 历史抽屉批量删除交互

**Files:**
- Modify: `frontend/src/App.vue`
- Modify: `frontend/src/styles.css`
- Test: `frontend/tests/app.test.js`

**Interfaces:**
- Consumes: `DELETE /api/batches -> {"deleted": true, "count": N}`
- Produces: `.delete-all-batches-button` 按钮和 `deleteAllBatches()` 交互

- [x] **Step 1: Write the failing Vue interaction test**

```javascript
test("确认后一次删除全部批次并保留成品提示", async () => {
  vi.stubGlobal("confirm", vi.fn(() => true));
  await wrapper.get(".delete-all-batches-button").trigger("click");
  expect(fetch).toHaveBeenCalledWith("/api/batches", expect.objectContaining({ method: "DELETE" }));
  expect(wrapper.text()).toContain("已完成视频文件仍保留");
});
```

- [x] **Step 2: Run the focused test and verify it fails because the button is absent**

Run: `npm test -- --run frontend/tests/app.test.js` from `frontend`

- [x] **Step 3: Implement the button, confirmation, busy state, request, SSE cleanup and history refresh**

```javascript
const deleteAllBusy = ref(false);
async function deleteAllBatches() {
  if (!window.confirm(confirmText)) return;
  deleteAllBusy.value = true;
  try {
    const result = await request("/api/batches", { method: "DELETE" });
    closeCurrentEvents();
    batch.value = null;
    await loadHistory(1);
    notice.value = `已删除 ${result.count} 个批次，已完成视频文件仍保留在磁盘中`;
  } finally {
    deleteAllBusy.value = false;
  }
}
```

- [x] **Step 4: Run the focused test and verify it passes**

Run: `npm test -- --run frontend/tests/app.test.js` from `frontend`

### Task 3: 完整回归验证

**Files:**
- Verify only: backend and frontend test suites

**Interfaces:**
- Consumes: Tasks 1-2 completed behavior
- Produces: passing regression evidence

- [x] **Step 1: Run all Python tests**

Run: `python -m pytest -q`

- [x] **Step 2: Run all frontend tests**

Run: `npm test` from `frontend`

- [x] **Step 3: Run the Vue production build**

Run: `npm run build` from `frontend`

- [x] **Step 4: Review the final diff for scope and preserved-file semantics**

Run: `git diff -- backend/douyin_downloader/store.py backend/douyin_downloader/api.py tests/test_store.py tests/test_api.py frontend/src/App.vue frontend/src/styles.css frontend/tests/app.test.js`
