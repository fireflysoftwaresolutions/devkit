(function boot() {
  const nav = $("#nav");
  nav.append(h("nav", {class: "views", "aria-label": "Views"}, h("div", {class: "tabs", id: "tabs"}), h("div", {class: "navmore", id: "navmore", hidden: true})),
    h("div", {class: "livew"}, h("button", {class: "live", id: "live", type: "button", "aria-controls": "livepop", "aria-expanded": "false", "data-state": LIVE.state, onclick: () => toggleLivePop()}),
      h("div", {class: "livepop", id: "livepop", role: "region", "aria-label": "Live updates", hidden: true})),
    h("div", {class: "search", role: "search"}, h("span", {class: "ico", "aria-hidden": "true"}, icon("search")), h("input", {id: "q", type: "search", placeholder: "Search projects, tasks…", "aria-label": "Search everything", autocomplete: "off"}), h("kbd", {text: "/"})),
    h("button", {class: "btn ghost", id: "palbtn", type: "button", onclick: openPalette, "aria-label": "Command palette", title: "Command palette (⌘K)"}, h("kbd", {text: "⌘K"})),
    h("button", {class: "btn icon ghost", id: "theme", type: "button", onclick: toggleTheme, "aria-label": "Toggle theme", title: "Toggle theme"}, icon("theme")));
  const q = $("#q");
  q.addEventListener("input", debounce(() => {
    const v = q.value.trim();
    if (v && !(["projects", "tasks", "search", "activity", "initiatives"].includes(S.view) && !S.id)) { store.set("devkit.prevView", S.view); set({q: v, view: "search", id: "", task: ""}, true); }
    else if (!v && S.view === "search") set({q: "", view: store.get("devkit.prevView", "now")}, true);
    else set({q: v});
  }, 140));
  q.addEventListener("keydown", e => { if (e.key === "Enter") { const first = $("[data-nav]", $("#view")); if (first) first.focus(); } });
  document.addEventListener("click", e => { $$(".facet[open],.cmenu[open]").forEach(d => { if (!d.contains(e.target)) d.open = false; });
    if (NAV.open && !e.target.closest("#navmore")) closeMore(false);
    if (LIVE.pop && !e.target.closest(".livew")) toggleLivePop(false); }, true);
  $(".livew").addEventListener("keydown", e => { if (e.key === "Escape" && LIVE.pop) { e.preventDefault(); e.stopPropagation(); toggleLivePop(false); $("#live").focus(); } });
  window.addEventListener("resize", debounce(() => fitTables($("#view")), 150));
  window.addEventListener("resize", debounce(fitNav, 100));
  document.addEventListener("toggle", e => { if (e.target && e.target.matches && e.target.matches("details")) fitTables(e.target); }, true);
  $("#scrim").addEventListener("click", () => { if (!$("#palette").hidden) closePalette(); else if (!$("#sheet").hidden) closeSheet(); });
  const dlg = $("#modal");
  dlg.addEventListener("cancel", e => { e.preventDefault(); closeModal(); });
  dlg.addEventListener("click", e => { if (e.target === dlg) closeModal(); });
  const st = decode(location.hash) || Object.assign({}, store.get("devkit.state", null) || {}, {modal: "", task: ""});
  if (st) S = Object.assign({}, DEFAULT, st, {view: ROUTES.includes(st.view) ? st.view : st.view === "portfolio" ? "initiatives" : "now"});
  if (!location.hash || location.hash !== encode(S)) history.replaceState(null, "", encode(S));
  liveIndicator(); renderBanner(); render();
  /* fonts change the widths the row and the tables were fitted to: fit again when the first fonts settle and whenever later ones finish */
  const refit = () => { fitTables($("#view")); fitNav(); };
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(refit);
  if (document.fonts && document.fonts.addEventListener) document.fonts.addEventListener("loadingdone", refit);
  try { const y = sessionStorage.getItem("devkit.scroll"); if (y) { window.scrollTo(0, +y); sessionStorage.removeItem("devkit.scroll"); } } catch (e) {}
  if (LIVE.on) { liveStart(); return; }   /* live: the event stream replaces the timed reload */
  setInterval(() => { const t = $("#ticker"); if (t) t.textContent = "Synced " + rel(D.generated); }, 30000);
  setInterval(() => { if (document.visibilityState === "visible" && $("#palette").hidden && !S.task && !S.modal && !/^(INPUT|SELECT|TEXTAREA)$/.test((document.activeElement || {}).tagName || "")) {
    try { sessionStorage.setItem("devkit.scroll", String(window.scrollY)); } catch (e) {} location.reload(); } }, 120000);
})();
})();
