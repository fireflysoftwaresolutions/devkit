// The module app.js imports: the link it adds is on the page only when the import resolved under /v2/.
export function addMenu() {
  document.body.insertAdjacentHTML('beforeend', '<a href="/usage">Usage</a>');
}
