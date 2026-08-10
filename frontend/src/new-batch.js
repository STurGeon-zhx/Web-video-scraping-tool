export function resetNewBatchState({
  inputText,
  preview,
  batch,
  notice,
  error,
  closeCurrentEvents,
}) {
  inputText.value = "";
  preview.value = null;
  batch.value = null;
  notice.value = "";
  error.value = "";
  closeCurrentEvents();
}
