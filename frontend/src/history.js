export function pageAfterDeletion(currentPage, remainingItems) {
  return remainingItems === 0 && currentPage > 1 ? currentPage - 1 : currentPage;
}
