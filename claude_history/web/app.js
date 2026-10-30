"use strict";
// Claude History — vanilla JS front end. All data comes from the local API.

// When embedded in the VS Code extension, window.cchHost bridges API calls,
// clipboard, links and resume to the extension host. In a browser it's absent.
const HOST = window.cchHost || null;
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const ROLE_LABEL = { user: "user", assistant: "claude", thinking: "thinking", tool_use: "tool call",
  tool_result: "tool output", summary: "summary", memory: "memory" };

const state = {
  results: [], sel: -1, page: 1, total: 0, lastQuery: "",
  sessions: [], spage: 1, stotal: 0,
  pack: loadPack(), lastPack: null, selected: new Set(),
  view: "sessions", viewPinned: false,  // History list shows sessions or matching messages
};

// ------------------------------------------------------------------ utils --
function toolLabel(name) {
  // "mcp__ccd_turn__update_plan" -> "ccd_turn › update_plan"
  const m = /^mcp__(.+?)__(.+)$/.exec(name || "");
  return m ? `${m[1]} › ${m[2]}` : (name || "");
}
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function snippetHtml(s) {
  return esc(s).replace(/\x02/g, "<mark>").replace(/\x03/g, "</mark>");
}
function fmtDate(ts, withTime = true) {
  if (!ts) return "";
  const d = new Date(ts);
  if (isNaN(d)) return ts;
  const opts = { year: "numeric", month: "short", day: "numeric" };
  if (withTime) Object.assign(opts, { hour: "2-digit", minute: "2-digit" });
  return d.toLocaleString(undefined, opts);
}
function ago(ts) {
  if (!ts) return "never";
  const s = (Date.now() - new Date(ts)) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}
function toast(msg, ms = 1800) {
  const t = $("#toast"); t.textContent = msg; t.classList.add("show");
  clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.remove("show"), ms);
}
async function copy(text, what = "Copied") {
  if (HOST) { await HOST.copy(text); toast(what); return; }
  try { await navigator.clipboard.writeText(text); toast(what); }
  catch {
    const ta = document.createElement("textarea"); ta.value = text; document.body.appendChild(ta);
    ta.select(); document.execCommand("copy"); ta.remove(); toast(what);
  }
}
async function api(path, body) {
  if (HOST) return HOST.api(path, body);
  const opts = body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {};
  const r = await fetch(path, opts);
  const data = await r.json().catch(() => ({ error: `HTTP ${r.status}` }));
  if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
  return data;
}
function vscodeLink(p) {
  if (!p) return null;
  return "vscode://file/" + encodeURI(p.replace(/\\/g, "/")).replace(/#/g, "%23").replace(/\?/g, "%3F");
}
function queryTerms(q) {
  return (q.match(/"[^"]+"|[^\s()]+/g) || [])
    .filter(t => !/^(AND|OR|NOT)$/.test(t) && !t.startsWith("-"))
    .map(t => t.replace(/^"|"$/g, "").replace(/\*$/, ""))
    .filter(t => t.length > 1);
}
function highlightTerms(html, q) {
  const terms = queryTerms(q);
  if (!terms.length) return html;
  const re = new RegExp("(" + terms.map(t => esc(t).replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|") + ")", "gi");
  // Only replace inside text, never inside tags.
  return html.replace(/(^|>)([^<]+)/g, (m, a, txt) => a + txt.replace(re, "<mark>$1</mark>"));
}
function renderBody(text, role) {
  if (role === "tool_use" || role === "tool_result") return esc(text);
  // Light Markdown: fenced code and inline code only — enough to keep code readable.
  const parts = String(text).split(/```[^\n]*\n?/);
  return parts.map((p, i) => i % 2 ? `<pre>${esc(p.replace(/\n$/, ""))}</pre>`
    : esc(p).replace(/`([^`\n]+)`/g, "<code>$1</code>")).join("");
}
const narrowMQ = window.matchMedia("(max-width: 760px)");
const isNarrow = () => narrowMQ.matches;
function lsGet(k, d = null) { try { const v = localStorage.getItem(k); return v === null ? d : v; } catch { return d; } }
function lsSet(k, v) { try { localStorage.setItem(k, v); } catch { /* storage unavailable */ } }
// Narrow layouts show list OR detail; this flips a split section to its detail pane.
function revealDetail(root, on = true) { root.closest(".split")?.classList.toggle("show-detail", on); }

function loadPack() { try { return JSON.parse(localStorage.getItem("cch.pack") || "[]"); } catch { return []; } }
function savePack() {
  try { localStorage.setItem("cch.pack", JSON.stringify(state.pack)); } catch { /* storage unavailable */ }
  $("#packCount").textContent = state.pack.length;
  $("#packCount").hidden = !state.pack.length;
}

// ---------------------------------------------------------------- filters --
function filterParams() {
  const roles = $$("#fRoles input:checked").map(i => i.value);
  const p = new URLSearchParams();
  if ($("#fProject").value) p.set("project", $("#fProject").value);
  if (roles.length !== $$("#fRoles input").length) p.set("roles", roles.join(","));
  if ($("#fFrom").value) p.set("from", $("#fFrom").value);
  if ($("#fTo").value) p.set("to", $("#fTo").value);
  if ($("#fFile").value.trim()) p.set("file", $("#fFile").value.trim());
  if (!$("#fSide").checked) p.set("sidechain", "0");
  if ($("#fArch").checked) p.set("archived", "1");
  return p;
}

// ----------------------------------------------------------------- search --
async function runSearch(page = 1) {
  const q = $("#q").value.trim();
  state.lastQuery = q;
  if (!q) { onQueryChange(); return; }
  const p = filterParams();
  p.set("q", q); p.set("mode", $("#mode").value); p.set("sort", $("#sort").value);
  p.set("page", page); p.set("page_size", 40);
  try { localStorage.setItem("cch.lastq", q); } catch {}
  $("#results").innerHTML = `<div class="empty muted">Searching…</div>`;
  try {
    const res = await api("/api/search?" + p);
    state.results = res.results; state.page = res.page; state.total = res.total; state.sel = -1;
    renderResults(res);
    if (res.results.length && !isNarrow()) select(0, false);
    $("#results").scrollTop = 0;
  } catch (e) {
    $("#results").innerHTML = `<div class="empty">Search failed: ${esc(e.message)}</div>`;
  }
}

function renderResults(res) {
  const el = $("#results");
  if (!res) { el.innerHTML = `<div class="empty muted">Type a query to search all projects.</div>`; return; }
  if (!res.total) {
    el.innerHTML = `<div class="empty">No matches for <b>${esc(res.query)}</b>.<br><br><span class="muted">
      Try fewer words, a prefix like <code>migrat*</code>, <b>Literal</b> mode, or clearing filters.</span></div>`;
    return;
  }
  const first = (res.page - 1) * res.page_size + 1, last = first + res.results.length - 1;
  const pages = Math.ceil(res.total / res.page_size);
  el.innerHTML = `<div class="summaryline"><span>${res.total.toLocaleString()} match${res.total === 1 ? "" : "es"}${res.total > res.results.length ? ` · showing ${first}–${last}` : ""}</span>
      ${res.mode !== "fts" && res.mode !== "substring" ? `<span class="spacer"></span><span title="${esc(res.mode)}">literal fallback</span>` : ""}</div>` +
    res.results.map((r, i) => `
      <div class="item" data-i="${i}">
        <div class="meta"><span class="role ${r.role}">${ROLE_LABEL[r.role] || r.role}${r.tool_name ? " · " + esc(toolLabel(r.tool_name)) : ""}</span>
          <span class="proj">${esc(r.project || "?")}</span><span class="title">${esc(r.title || r.session_id)}</span>
          <span>${fmtDate(r.ts, false)}</span></div>
        <div class="snip ${r.role.startsWith("tool") ? "code" : ""}">${snippetHtml(r.snippet)}</div>
      </div>`).join("") +
    (pages > 1 ? `<div class="pager"><button data-pg="${res.page - 1}" ${res.page <= 1 ? "disabled" : ""}>‹ Prev</button>
      <span class="muted">Page ${res.page} / ${pages}</span>
      <button data-pg="${res.page + 1}" ${res.page >= pages ? "disabled" : ""}>Next ›</button></div>` : "");
  $$(".item", el).forEach(n => n.addEventListener("click", () => select(+n.dataset.i)));
  $$(".pager button", el).forEach(b => b.addEventListener("click", () => runSearch(+b.dataset.pg)));
}

function select(i, reveal = true) {
  if (i < 0 || i >= state.results.length) return;
  if (reveal) revealDetail($("#detail"));
  state.sel = i;
  $$("#results .item").forEach(n => n.classList.toggle("sel", +n.dataset.i === i));
  $(`#results .item[data-i="${i}"]`)?.scrollIntoView({ block: "nearest" });
  showContext(state.results[i].id, $("#detail"));
}

// ---------------------------------------------------------------- detail ---
function sessionHeader(s, extra = "") {
  const title = s.title || s.id;
  const open = lsGet("cch.details") === "1";
  const cwdLink = s.cwd && s.cwd_exists ? `<a class="btn" href="${vscodeLink(s.cwd)}" title="${esc(s.cwd)}">Open folder in VS Code</a>` : "";
  const pills = [
    s.cwd && !s.cwd_exists ? `<span class="pill warn">directory gone</span>` : "",
    s.source_missing ? `<span class="pill warn" title="Claude Code deleted the original transcript; the index preserved it">transcript deleted</span>` : "",
    s.archived_at ? `<span class="pill" title="Archived ${esc(fmtDate(s.archived_at))}">archived</span>` : "",
    !s.resumable && s.resume_blocker && !s.id.startsWith("memory:")
      ? `<span class="pill warn" title="${esc(s.resume_blocker)}. Continue via the claude-history tools or a context pack.">can't resume</span>` : "",
  ].join("");
  const isMemory = s.id.startsWith("memory:");
  const resumeBtns = HOST
    ? (s.resumable ? `<button data-act="chat" class="primary" title="Reopen this conversation in Claude Code's chat panel. If it belongs to another folder, that folder opens in a new window first.">Continue in chat</button>` : "")
      + (isMemory ? "" : `<button data-act="bring" ${s.resumable ? "" : 'class="primary"'} title="Start a new Claude chat in this workspace that loads this session through claude-history and carries on">Bring into this chat</button>`)
      + (isMemory ? "" : `<button data-act="whole" title="Put the complete conversation (your messages and Claude's replies) into a new Claude chat. Works even if Claude Code deleted the transcript.">Send whole session</button>`)
      + (s.resumable ? `<button data-act="term" class="ghost" title="claude --resume in a terminal in this window, running in ${esc(s.cwd)}">Terminal</button>` : "")
    : !s.resumable ? "" : `<button data-act="vscode" class="primary" title="Open ${esc(s.cwd)} in VS Code and copy the resume command">Continue in VS Code</button>
       <button data-act="term" title="Resume in a new terminal window">Resume in terminal</button>`;
  const wholeBrowser = HOST || isMemory ? "" : `<button data-act="copywhole" title="Copy the complete conversation as Markdown">Copy whole session</button>`;
  const files = (s.files || []).slice(0, 40);
  return `<div class="shead">
    <div class="sbar">
      <button class="back ghost" data-act="back" aria-label="Back to list">&larr;</button>
      <div class="stitle"><h2 title="${esc(title)}">${esc(title)}</h2>
        <div class="ssub"><b>${esc(s.project || "?")}</b><span>${fmtDate(s.started, false)} → ${fmtDate(s.last_activity, false)}</span>
          <span>${s.message_count} msgs</span>${pills}</div></div>
      <div class="sacts">${resumeBtns}${wholeBrowser}${extra}
        ${`<button data-act="archive" class="ghost" title="${s.archived_at ? "Show this session in search and lists again" : "Hide from search and lists (reversible)"}">${s.archived_at ? "Unarchive" : "Archive"}</button>`}
        <button data-act="more" class="ghost" aria-expanded="${open}" title="Directory, id, summary, files">Details ${open ? "▴" : "▾"}</button></div>
    </div>
    <div class="sdetails" ${open ? "" : "hidden"}>
      <dl class="kv">
        <dt>Directory</dt><dd><code>${esc(s.cwd || "unknown")}</code></dd>
        <dt>Session</dt><dd><code>${esc(s.id)}</code></dd>
        <dt>Active</dt><dd>${fmtDate(s.started)} → ${fmtDate(s.last_activity)}${s.git_branch ? ` · branch <code>${esc(s.git_branch)}</code>` : ""}${s.entrypoint ? ` · via ${esc(s.entrypoint)}` : ""}${s.repo_url ? ` · ${esc(s.repo_url)}` : ""}</dd>
        ${s.resume_blocker && !s.id.startsWith("memory:") ? `<dt>Resume</dt><dd>${esc(s.resume_blocker)}</dd>` : ""}
        ${s.summary ? `<dt>Summary</dt><dd>${esc(s.summary).replace(/\n/g, "<br>")}</dd>` : ""}
      </dl>
      <div class="actions">
        ${s.resume_command ? `<button data-act="resume" title="${esc(s.resume_command)}">Copy resume command</button>` : ""}
        <button data-act="sid">Copy session id</button>
        ${isMemory ? "" : HOST
          ? `<button data-act="whole-tools" title="Save everything, including tool calls and output, to a file and start a Claude chat that reads it">Send with tool output</button>`
          : `<button data-act="savewhole" title="Save everything, including tool calls and output, as a Markdown file">Save full transcript</button>`}
        ${cwdLink}
        <button data-act="delete" class="danger" title="Remove from this index and keep it out of future syncs. Claude Code's own transcript is not touched.">Delete from index</button>
        ${files.length ? `<button data-act="files">Files (${s.files.length})</button>` : ""}
      </div>
      ${files.length ? `<div class="files" hidden>${files.map(f => `<div><a href="${vscodeLink(f)}" title="Open">${esc(f)}</a></div>`).join("")}</div>` : ""}
    </div>
  </div>`;
}

function wireHeader(root, s) {
  root.querySelector('[data-act="back"]')?.addEventListener("click", () => revealDetail(root, false));
  root.querySelector('[data-act="more"]')?.addEventListener("click", e => {
    const d = root.querySelector(".sdetails"), open = d.hidden;
    d.hidden = !open; e.currentTarget.setAttribute("aria-expanded", open);
    e.currentTarget.textContent = `Details ${open ? "▴" : "▾"}`;
    lsSet("cch.details", open ? "1" : "0");
  });
  root.querySelector('[data-act="resume"]')?.addEventListener("click", () => copy(s.resume_command, "Resume command copied"));
  root.querySelector('[data-act="vscode"]')?.addEventListener("click", () => resumeSession(s, "vscode"));
  root.querySelector('[data-act="term"]')?.addEventListener("click", () => resumeSession(s, "terminal"));
  root.querySelector('[data-act="chat"]')?.addEventListener("click", () => resumeSession(s, "chat"));
  root.querySelector('[data-act="bring"]')?.addEventListener("click", () => resumeSession(s, "bring"));
  root.querySelector('[data-act="whole"]')?.addEventListener("click", () => resumeSession(s, "whole"));
  root.querySelector('[data-act="whole-tools"]')?.addEventListener("click", () => resumeSession(s, "whole-tools"));
  root.querySelector('[data-act="copywhole"]')?.addEventListener("click", async () => {
    try {
      const r = await api("/api/session-export", { session_id: s.id, include_tools: false, want_markdown: true });
      await copy(r.markdown, `Whole conversation copied (${r.messages} messages, ≈${r.tokens.toLocaleString()} tokens)`);
    } catch (e) { toast("Failed: " + e.message, 5000); }
  });
  root.querySelector('[data-act="savewhole"]')?.addEventListener("click", async () => {
    try {
      const r = await api("/api/session-export", { session_id: s.id, include_tools: true });
      toast(`Saved ${r.messages} messages (≈${r.tokens.toLocaleString()} tokens) to ${r.path}`, 7000);
    } catch (e) { toast("Failed: " + e.message, 5000); }
  });
  root.querySelector('[data-act="sid"]')?.addEventListener("click", () => copy(s.id, "Session id copied"));
  root.querySelector('[data-act="archive"]')?.addEventListener("click", async () => {
    if (await archiveSessions([s.id], !s.archived_at)) {
      const cur = root._current;
      if (cur && cur.hit && !cur.full) showContext(cur.hit.id, root); else showSession(s.id, root);
    }
  });
  root.querySelector('[data-act="delete"]')?.addEventListener("click", e => confirmTwice(e.currentTarget, () => deleteSessions([s.id])));
  root.querySelector('[data-act="files"]')?.addEventListener("click", () => { const f = root.querySelector(".files"); f.hidden = !f.hidden; });
}

async function resumeSession(s, action) {
  if (HOST) {
    const mode = action === "terminal" ? "term" : action;
    const said = {
      chat: "Opened in Claude's chat panel",
      window: "Opening its folder in a new window. The conversation opens in Claude's chat there",
      bring: "New Claude chat started with a request to load this session. Review and send it",
      whole: "The whole conversation is in a new Claude chat. Review and send it",
      "whole-file": "Full transcript saved; a new Claude chat asks Claude to read it. Review and send it",
      term: "Resuming in the terminal panel",
    };
    try { toast(said[await HOST.resume(s.id, mode)] || "Done", 5000); }
    catch (e) { toast("Can't continue: " + e.message, 7000); }
    return;
  }
  try {
    const r = await api("/api/resume", { session_id: s.id, action });
    if (action === "vscode") {
      await navigator.clipboard.writeText(r.command).catch(() => {});
      toast("Folder opening in VS Code — paste in its terminal (Ctrl+`) to resume, or pick it in Claude Code's history list", 6000);
    } else {
      toast("Resuming in a new terminal window");
    }
  } catch (e) { toast("Can't resume: " + e.message, 6000); }
}

const CLAMP_LINES = 6;
function msgHtml(m, q) {
  const tool = m.role === "tool_use" || m.role === "tool_result";
  let body = renderBody(m.text, m.role);
  if (q) body = highlightTerms(body, q);
  const lines = m.text.split("\n").length;
  // Tool I/O and long messages start collapsed so the conversation stays scannable.
  const clamp = (tool || m.role === "thinking") ? lines > CLAMP_LINES || m.text.length > 600
    : lines > 30 || m.text.length > 3000;
  const more = clamp ? `<button class="expand ghost" data-a="expand">${lines > CLAMP_LINES ? `Show all ${lines} lines` : "Show more"}</button>` : "";
  return `<article class="msg ${tool ? "tool" : ""} ${m.is_hit ? "hit" : ""} ${m.is_sidechain ? "side" : ""} ${clamp ? "clamp" : ""}" data-id="${m.id}">
    <header><span class="role ${m.role}">${ROLE_LABEL[m.role] || m.role}${m.tool_name ? " · " + esc(toolLabel(m.tool_name)) : ""}</span>
      ${m.is_sidechain ? `<span class="pill">subagent</span>` : ""}${m.is_error ? `<span class="pill warn">error</span>` : ""}
      <span>${fmtDate(m.ts)}</span><span class="muted">#${m.id}</span>
      <span class="acts"><button data-a="copy" title="Copy excerpt with attribution (c)">Copy</button>
        <button data-a="pack" title="Add this message ±2 to the context pack (a)">+ Pack</button></span></header>
    <div class="body">${body}</div>${more}</article>`;
}

function excerpt(s, m) {
  return `[From Claude Code session "${s.title || s.id}" — project ${s.project}, ${s.cwd || ""}, ${(m.ts || "").slice(0, 16).replace("T", " ")} UTC, session ${s.id}, msg ${m.id}. Historical context, not the current repo.]\n\n${m.text}`;
}

function wireMessages(root, s, msgs) {
  const byId = Object.fromEntries(msgs.map(m => [m.id, m]));
  $$(".msg", root).forEach(n => {
    const m = byId[n.dataset.id];
    n.querySelector('[data-a="copy"]').addEventListener("click", () => copy(excerpt(s, m), "Excerpt copied with attribution"));
    n.querySelector('[data-a="pack"]').addEventListener("click", () => addToPack(m, s));
    n.querySelector('[data-a="expand"]')?.addEventListener("click", e => {
      const collapsed = n.classList.toggle("clamp");
      const n = m.text.split("\n").length;
      e.currentTarget.textContent = collapsed ? (n > CLAMP_LINES ? `Show all ${n} lines` : "Show more") : "Show less";
    });
  });
}

async function showContext(id, root, before = 4, after = 4) {
  root.innerHTML = `<div class="empty muted">Loading…</div>`;
  try {
    const ctx = await api(`/api/context/${id}?before=${before}&after=${after}`);
    const s = ctx.session;
    root.innerHTML = sessionHeader(s, `<button data-act="full" title="Show the whole conversation">Full session</button>`) +
      `<div class="msgs"><div class="more"><button data-more="before">↑ More before</button></div>
        ${ctx.messages.map(m => msgHtml(m, state.lastQuery)).join("")}
        <div class="more"><button data-more="after">↓ More after</button></div></div>`;
    wireHeader(root, s);
    wireMessages(root, s, ctx.messages);
    root.querySelector('[data-act="full"]').addEventListener("click", () => showSession(s.id, root, id));
    root.querySelector('[data-more="before"]').addEventListener("click", () => showContext(id, root, before + 10, after));
    root.querySelector('[data-more="after"]').addEventListener("click", () => showContext(id, root, before, after + 10));
    root._current = { session: s, hit: ctx.messages.find(m => m.is_hit) };
    root.querySelector(".msg.hit")?.scrollIntoView({ block: "center" });
  } catch (e) { root.innerHTML = `<div class="empty">Could not load context: ${esc(e.message)}</div>`; }
}

async function showSession(sid, root, focusId = null, offset = 0) {
  root.innerHTML = `<div class="empty muted">Loading session…</div>`;
  try {
    const side = $("#fSide").checked ? 1 : 0;
    const res = await api(`/api/session/${encodeURIComponent(sid)}?offset=${offset}&limit=400&sidechain=${side}`);
    const s = res.session;
    res.messages.forEach(m => m.is_hit = m.id === focusId);
    const next = offset + res.messages.length < res.total;
    root.innerHTML = sessionHeader(s) + `<div class="msgs">
      ${offset ? `<div class="more"><button data-pg="${Math.max(0, offset - 400)}">↑ Earlier messages</button></div>` : ""}
      ${res.messages.map(m => msgHtml(m, focusId ? state.lastQuery : "")).join("")}
      ${next ? `<div class="more"><button data-pg="${offset + 400}">↓ Later messages (${res.total - offset - res.messages.length} more)</button></div>` : ""}
      </div>`;
    wireHeader(root, s);
    wireMessages(root, s, res.messages);
    $$("[data-pg]", root).forEach(b => b.addEventListener("click", () => showSession(sid, root, focusId, +b.dataset.pg)));
    root._current = { session: s, hit: res.messages.find(m => m.is_hit) || res.messages[0], full: true };
    (root.querySelector(".msg.hit") || root).scrollIntoView({ block: "center" });
  } catch (e) { root.innerHTML = `<div class="empty">Could not load session: ${esc(e.message)}</div>`; }
}

// -------------------------------------------------------------- curation --
// Archive hides sessions (reversible); delete removes them from the index and keeps
// them out of future syncs. Claude Code's own transcript files are never touched.
function confirmTwice(btn, fn, label = "Confirm delete") {
  if (btn.classList.contains("confirm")) { btn.classList.remove("confirm"); fn(); return; }
  const old = btn.textContent;
  btn.classList.add("confirm"); btn.textContent = label;
  setTimeout(() => { if (btn.isConnected && btn.classList.contains("confirm")) { btn.classList.remove("confirm"); btn.textContent = old; } }, 4000);
}
async function afterCuration() {
  state.selected.clear();
  if (state.view === "sessions") await loadSessions(state.spage || 1);
  else runSearch(state.page || 1);
  refreshStatus(); loadProjects().catch(() => {});
}
async function archiveSessions(ids, archived = true) {
  try {
    const r = await api("/api/sessions/archive", { ids, archived });
    toast(`${archived ? "Archived" : "Unarchived"} ${r.changed} session${r.changed === 1 ? "" : "s"}`);
    await afterCuration();
    return true;
  } catch (e) { toast("Failed: " + e.message, 5000); return false; }
}
async function deleteSessions(ids) {
  try {
    const r = await api("/api/sessions/delete", { ids });
    toast(`Deleted ${r.deleted} session${r.deleted === 1 ? "" : "s"} from the index. Restore them under Health`, 5000);
    for (const root of [$("#detail")]) {
      if (root._current && ids.includes(root._current.session.id)) {
        root.innerHTML = `<div class="empty muted">Session deleted from the index.</div>`; root._current = null; revealDetail(root, false);
      }
    }
    await afterCuration();
  } catch (e) { toast("Failed: " + e.message, 5000); }
}
function updateBulkBar() {
  const n = state.selected.size;
  document.body.classList.toggle("selecting", n > 0);
  $("#bulkbar").hidden = n === 0 || state.view !== "sessions";
  $("#selCount").textContent = `${n} selected`;
  const boxes = $$("#sessions .pick");
  $("#selAll").checked = boxes.length > 0 && boxes.every(b => b.checked);
  const chosen = state.sessions.filter(s => state.selected.has(s.id));
  $("#bulkArchive").hidden = !chosen.some(s => !s.archived_at);
  $("#bulkUnarchive").hidden = !chosen.some(s => s.archived_at);
  $("#bulkDelete").classList.remove("confirm"); $("#bulkDelete").textContent = `Delete`;
}

// ---------------------------------------------------------------- history --
// One list for both: sessions when the search box is empty, matching messages
// when it isn't. "Sessions" with a query searches session titles and prompts.
function setView(v) {
  state.view = v;
  document.body.classList.toggle("view-sessions", v === "sessions");
  document.body.classList.toggle("view-messages", v === "messages");
  $$(".seg [data-view]").forEach(b => b.classList.toggle("on", b.dataset.view === v));
  $(".seg [data-view=messages]").disabled = !$("#q").value.trim();
  $("#sessions").hidden = v !== "sessions";
  $("#results").hidden = v !== "messages";
  if (v === "messages") $("#bulkbar").hidden = true; else updateBulkBar();
}
function refreshList(page = 1) {
  if (state.view === "sessions") loadSessions(page); else runSearch(page);
}
function syncSessionSort() {
  const q = $("#q").value.trim(), s = $("#ssort");
  s.querySelector("[value=relevance]").hidden = !q;
  if (q && s.value === "newest" && !s.dataset.touched) s.value = "relevance";
  if (!q && s.value === "relevance") s.value = "newest";
}
function onQueryChange() {
  syncSessionSort();
  const q = $("#q").value.trim();
  if (!q) { state.viewPinned = false; state.lastQuery = ""; setView("sessions"); loadSessions(1); return; }
  if (state.view === "sessions" && state.viewPinned) { setView("sessions"); loadSessions(1); return; }
  setView("messages"); runSearch(1);
}

// --------------------------------------------------------------- sessions --
async function loadSessions(page = 1) {
  const p = filterParams();
  const q = $("#q").value.trim();
  p.delete("archived");
  p.set("archived", $("#sarch").value);
  if (q) p.set("match", q); else { p.delete("roles"); p.delete("sidechain"); }
  p.set("sort", $("#ssort").value); p.set("page", page); p.set("page_size", 60);
  const el = $("#sessions");
  try {
    const res = await api("/api/sessions?" + p);
    state.sessions = res.sessions; state.spage = res.page; state.stotal = res.total;
    const pages = Math.ceil(res.total / res.page_size);
    const label = { only: "archived sessions", all: "sessions (active and archived)" }[$("#sarch").value] || "sessions";
    const summary = !res.total ? "" : `<div class="summaryline">${res.total.toLocaleString()} ${label}${q ? " with matches" : ""}</div>`;
    el.innerHTML = summary + (res.sessions.map(s => `
      <div class="item ${s.archived_at ? "archived" : ""}" data-sid="${esc(s.id)}">
        <input type="checkbox" class="pick" data-sid="${esc(s.id)}" ${state.selected.has(s.id) ? "checked" : ""} aria-label="Select">
        <div class="stitle">${s.archived_at ? `<span class="pill">archived</span> ` : ""}${esc(s.title || s.id)}</div>
        <div class="meta"><span class="proj">${esc(s.project || "?")}</span><span class="title" title="${esc(s.cwd || "")}">${esc(s.cwd || "")}</span>
          <span>${fmtDate(s.last_activity, false)}</span></div>
        <div class="meta">${s.hits ? `<span class="pill hits">${s.hits} match${s.hits === 1 ? "" : "es"}</span>` : ""}<span>${s.message_count} msgs</span><span>started ${fmtDate(s.started, false)}</span>
          ${s.source_missing ? `<span class="pill warn">source deleted</span>` : ""}
          ${s.git_branch && s.git_branch !== "HEAD" ? `<span>⎇ ${esc(s.git_branch)}</span>` : ""}<span class="muted">${esc(s.id.slice(0, 8))}</span></div>
      </div>`).join("") || `<div class="empty muted">${$("#sarch").value === "only" ? "Nothing archived." : q ? `No sessions contain <b>${esc(q)}</b>.` : "No sessions match."}</div>`) +
      (pages > 1 ? `<div class="pager"><button data-pg="${res.page - 1}" ${res.page <= 1 ? "disabled" : ""}>‹ Prev</button>
        <span class="muted">Page ${res.page} / ${pages}</span><button data-pg="${res.page + 1}" ${res.page >= pages ? "disabled" : ""}>Next ›</button></div>` : "");
    $$(".pick", el).forEach(b => {
      b.addEventListener("click", e => e.stopPropagation());
      b.addEventListener("change", () => { b.checked ? state.selected.add(b.dataset.sid) : state.selected.delete(b.dataset.sid); updateBulkBar(); });
    });
    updateBulkBar();
    $$(".item", el).forEach(n => n.addEventListener("click", () => {
      $$(".item", el).forEach(x => x.classList.toggle("sel", x === n));
      revealDetail($("#detail"));
      state.lastQuery = q;  // highlight the search terms in the opened session
      showSession(n.dataset.sid, $("#detail"));
    }));
    $$(".pager button", el).forEach(b => b.addEventListener("click", () => loadSessions(+b.dataset.pg)));
  } catch (e) { el.innerHTML = `<div class="empty">Failed: ${esc(e.message)}</div>`; }
}

// ------------------------------------------------------------------- pack --
function addToPack(m, s, before = 2, after = 2) {
  if (state.pack.some(p => p.message_id === m.id)) { toast("Already in pack"); return; }
  state.pack.push({ message_id: m.id, before, after, role: m.role, ts: m.ts, project: s.project,
    title: s.title || s.id, preview: m.text.slice(0, 140) });
  savePack(); renderPackItems(); toast(`Added to pack (${state.pack.length})`);
}
function renderPackItems() {
  const el = $("#pItems");
  if (!state.pack.length) {
    el.innerHTML = `<div class="empty muted">Empty. Use <b>+ Pack</b> on any message, or press <kbd>a</kbd> on a search result.</div>`;
    return;
  }
  el.innerHTML = state.pack.map((p, i) => `<div class="pitem">
    <div class="t"><div><span class="role ${p.role}">${ROLE_LABEL[p.role] || p.role}</span> <b>${esc(p.project)}</b> · ${esc(p.title)}</div>
      <div class="muted">${fmtDate(p.ts)} · ${esc(p.preview)}</div></div>
    <label title="Messages before">−<input type="number" min="0" max="30" value="${p.before}" data-k="before" data-i="${i}"></label>
    <label title="Messages after">+<input type="number" min="0" max="30" value="${p.after}" data-k="after" data-i="${i}"></label>
    <button class="ghost" data-rm="${i}" title="Remove">✕</button></div>`).join("");
  $$("input[data-k]", el).forEach(inp => inp.addEventListener("change", () => {
    state.pack[+inp.dataset.i][inp.dataset.k] = Math.max(0, Math.min(30, +inp.value || 0)); savePack();
  }));
  $$("[data-rm]", el).forEach(b => b.addEventListener("click", () => { state.pack.splice(+b.dataset.rm, 1); savePack(); renderPackItems(); }));
}
async function buildPack(save = false) {
  if (!state.pack.length) { toast("Pack is empty"); return null; }
  const body = { items: state.pack.map(({ message_id, before, after }) => ({ message_id, before, after })),
    task: $("#pTask").value.trim(), max_tokens: +$("#pBudget").value || 6000,
    include_tools: $("#pTools").checked, redact: $("#pRedact").checked, save };
  try {
    const res = await api("/api/pack", body);
    state.lastPack = res;
    $("#pPreview").textContent = res.markdown; $("#pPreview").classList.remove("muted");
    $("#pMeta").textContent = `≈${res.tokens.toLocaleString()} tokens · ${res.sessions} session(s)` +
      (body.redact ? ` · ${res.redactions} redaction(s)` : " · redaction off") +
      (res.dropped_blocks ? ` · ${res.dropped_blocks} block(s) dropped for budget` : "") +
      (res.saved_to ? ` · saved to ${res.saved_to}` : "");
    ["#pCopy", "#pSave", "#pDownload"].forEach(s => $(s).disabled = false);
    return res;
  } catch (e) { toast("Pack failed: " + e.message); return null; }
}

// ----------------------------------------------------------------- status --
async function refreshStatus() {
  try {
    const s = await api("/api/status");
    const el = $("#status");
    el.classList.toggle("busy", s.sync_running);
    el.classList.toggle("bad", !!(s.sync_error || (s.sources_by_status.error || 0)));
    $("#statusText").textContent = `${s.sessions} sessions · ${s.messages.toLocaleString()} msgs · synced ${ago(s.last_sync)}`;
    el.title = `Last sync: ${s.last_sync || "never"} (UTC)` + (s.sync_error ? `\nSync error: ${s.sync_error}` : "");
    if ($("#tab-health").classList.contains("active")) renderHealth(s);
    return s;
  } catch { $("#statusText").textContent = "server unreachable"; $("#status").classList.add("bad"); }
}
function renderHealth(s) {
  const st = s.last_sync_stats || {};
  const card = (n, l) => `<div class="card"><b>${(n ?? 0).toLocaleString()}</b><span>${l}</span></div>`;
  $("#health").innerHTML = `
    <h3>Index</h3><div class="cards">${card(s.projects, "projects")}${card(s.sessions, "sessions")}${card(s.messages, "messages")}
      ${card(s.sessions_archived, "archived")}${card(s.sessions_deleted, "deleted from index")}
      ${card(s.sessions_source_missing, "sessions kept after source deleted")}${card(s.skipped_bad_lines, "malformed lines skipped")}</div>
    <h3>Sources</h3><div class="cards">${Object.entries(s.sources_by_status).map(([k, v]) => card(v, k)).join("")}
      ${Object.entries(s.sources_by_kind).map(([k, v]) => card(v, k + " files")).join("")}</div>
    <h3>Last sync</h3><p>${s.last_sync ? fmtDate(s.last_sync) + " (" + ago(s.last_sync) + ")" : "never"} ·
      ${st.indexed ?? 0} updated, ${st.unchanged ?? 0} unchanged, ${st.new_messages ?? 0} new messages, ${st.seconds ?? 0}s ·
      background sync every ${s.sync_interval}s ${s.sync_running ? "· <b>running now</b>" : ""}</p>
    ${s.sync_error ? `<p style="color:var(--err)">Sync error: ${esc(s.sync_error)}</p>` : ""}
    <div class="row"><button id="hSync">Incremental sync</button><button id="hFull">Full rescan</button>
      <span class="muted">A full rescan re-reads every transcript from the start. Originals are only ever read.</span></div>
    <h3>Files that could not be processed</h3>
    ${s.error_sources.length ? `<table><tr><th>Path</th><th>Error</th></tr>${s.error_sources.map(e =>
      `<tr><td><code>${esc(e.path)}</code></td><td>${esc(e.error)}</td></tr>`).join("")}</table>` : `<p class="muted">None.</p>`}
    <h3>Deleted from the index</h3><div id="hDeleted" class="muted">Loading…</div>
    <h3>Projects</h3><div id="hProjects" class="muted">Loading…</div>
    <h3>Locations</h3><p class="muted">History read from: ${s.claude_dirs.map(d => `<code>${esc(d)}</code>`).join(", ")}<br>
      Index database: <code>${esc(s.db_path)}</code> · schema v${s.schema_version} · parser v${s.parser_version}</p>`;
  $("#hSync").onclick = () => triggerSync(false);
  $("#hFull").onclick = () => triggerSync(true);
  api("/api/deleted").then(rows => {
    if (!rows.length) { $("#hDeleted").innerHTML = `<p class="muted">Nothing deleted. Deleted sessions appear here and can be restored while Claude Code still has their transcript.</p>`; return; }
    $("#hDeleted").innerHTML = `<div class="tablewrap"><table><tr><th>Session</th><th>Project</th><th>Deleted</th><th></th></tr>
      ${rows.map(r => `<tr><td>${esc(r.title || r.id)}<br><span class="muted">${esc(r.id.slice(0, 8))} · ${r.message_count ?? "?"} msgs</span></td>
        <td>${esc(r.project_name || r.project_key || "")}</td><td>${fmtDate(r.deleted_at)}</td>
        <td>${r.restorable ? `<button data-restore="${esc(r.id)}">Restore</button>` : `<span class="muted" title="Claude Code no longer has this transcript">not restorable</span>`}</td></tr>`).join("")}
      </table></div>`;
    $$("[data-restore]", $("#hDeleted")).forEach(b => b.addEventListener("click", async () => {
      b.disabled = true; b.textContent = "Restoring…";
      try {
        const r = await api("/api/deleted/restore", { ids: [b.dataset.restore] });
        toast(r.restored.length ? "Restored and re-indexed" : "Transcript no longer exists; nothing to re-index", 4000);
        refreshStatus(); afterCuration();
      } catch (e) { toast("Failed: " + e.message, 5000); b.disabled = false; b.textContent = "Restore"; }
    }));
  }).catch(() => {});
  api("/api/projects").then(ps => {
    $("#hProjects").innerHTML = `<div class="tablewrap"><table><tr><th>Project</th><th>Directory</th><th class="num">Sessions</th><th class="num">Messages</th><th>Last seen</th></tr>
      ${ps.map(p => `<tr><td>${esc(p.name)}</td><td><code>${esc(p.cwd || p.key)}</code></td><td class="num">${p.sessions}</td>
        <td class="num">${p.messages.toLocaleString()}</td><td>${fmtDate(p.last_seen, false)}</td></tr>`).join("")}</table></div>`;
  });
}
async function triggerSync(full) {
  await api("/api/sync", { full });
  toast(full ? "Full rescan started" : "Sync started");
  setTimeout(refreshStatus, 400); setTimeout(refreshStatus, 2500);
}
async function loadProjects() {
  const ps = await api("/api/projects");
  $("#fProject").innerHTML = `<option value="">All projects (${ps.length})</option>` +
    ps.filter(p => p.sessions || p.messages).map(p => `<option value="${esc(p.key)}" title="${esc(p.cwd || "")}">${esc(p.name)} (${p.sessions})</option>`).join("");
}

// ------------------------------------------------------------------- tabs --
function showTab(name) {
  name = { search: "history", sessions: "history" }[name] || name;  // tabs before 0.2.2
  $$(".tabs > button").forEach(b => b.classList.toggle("active", b.dataset.tab === name));
  $$(".tab").forEach(t => t.classList.toggle("active", t.id === "tab-" + name));
  if (name === "pack") renderPackItems();
  if (name === "health") refreshStatus();
  document.body.dataset.tab = name;
}

// ---------------------------------------------------------------- filters --
function activeFilterCount() {
  const rolesChanged = $$("#fRoles input").some(i => i.checked !== i.defaultChecked);
  return [rolesChanged, $("#fFrom").value, $("#fTo").value, $("#fFile").value.trim(), !$("#fSide").checked,
    $("#fArch").checked].filter(Boolean).length;
}
function updateFilterBadge() {
  const n = activeFilterCount(), b = $("#filterCount");
  b.textContent = n; b.hidden = !n;
}
function toggleFilters(open) {
  $("#filters").hidden = !open;
  $("#filtersBtn").setAttribute("aria-expanded", open);
  lsSet("cch.filters", open ? "1" : "0");
}
function clearFilters() {
  $$("#fRoles input").forEach(i => i.checked = i.defaultChecked);
  ["#fFrom", "#fTo", "#fFile"].forEach(s => $(s).value = "");
  $("#fSide").checked = true;
  $("#fArch").checked = false;
  updateFilterBadge(); refreshList();
}

// --------------------------------------------------------------- resizer --
function initResizers() {
  const saved = lsGet("cch.listw");
  if (saved) document.documentElement.style.setProperty("--list-w", saved);
  $$(".resizer").forEach(r => {
    r.addEventListener("pointerdown", e => {
      const split = r.parentElement, box = split.getBoundingClientRect();
      r.setPointerCapture(e.pointerId); r.classList.add("drag"); document.body.classList.add("resizing");
      const move = ev => {
        const w = Math.max(260, Math.min(box.width - 320, ev.clientX - box.left));
        document.documentElement.style.setProperty("--list-w", `${Math.round(w / box.width * 1000) / 10}%`);
      };
      const up = () => {
        r.removeEventListener("pointermove", move); r.classList.remove("drag"); document.body.classList.remove("resizing");
        lsSet("cch.listw", getComputedStyle(document.documentElement).getPropertyValue("--list-w").trim());
      };
      r.addEventListener("pointermove", move);
      r.addEventListener("pointerup", up, { once: true });
    });
    r.addEventListener("dblclick", () => { document.documentElement.style.setProperty("--list-w", "40%"); lsSet("cch.listw", "40%"); });
  });
}

// ------------------------------------------------------------------- wire --
let debounce;
$("#searchForm").addEventListener("submit", e => { e.preventDefault(); showTab("history"); onQueryChange(); });
$("#q").addEventListener("input", () => {
  clearTimeout(debounce);
  debounce = setTimeout(() => { showTab("history"); onQueryChange(); }, 280);
});
$$(".seg [data-view]").forEach(b => b.addEventListener("click", () => {
  const v = b.dataset.view;
  if (v === "messages" && !$("#q").value.trim()) { $("#q").focus(); return; }
  state.viewPinned = v === "sessions" && !!$("#q").value.trim();
  setView(v); refreshList();
}));
["#mode", "#sort"].forEach(s => $(s).addEventListener("change", () => runSearch(1)));
["#fProject", "#fFrom", "#fTo", "#fSide", "#fFile"].forEach(s => $(s).addEventListener("change", () => refreshList()));
$$("#fRoles input").forEach(i => i.addEventListener("change", () => { if (state.view === "messages") runSearch(1); }));
$("#filtersBtn").addEventListener("click", () => toggleFilters($("#filters").hidden));
$("#fClear").addEventListener("click", clearFilters);
$("#filters").addEventListener("change", updateFilterBadge);
$("#fFile").addEventListener("input", updateFilterBadge);
$("#ssort").addEventListener("change", e => { e.target.dataset.touched = "1"; loadSessions(1); });
$("#sarch").addEventListener("change", () => { state.selected.clear(); loadSessions(1); });
$("#selAll").addEventListener("change", e => {
  $$("#sessions .pick").forEach(b => { b.checked = e.target.checked; e.target.checked ? state.selected.add(b.dataset.sid) : state.selected.delete(b.dataset.sid); });
  updateBulkBar();
});
$("#bulkArchive").addEventListener("click", () => archiveSessions([...state.selected], true));
$("#bulkUnarchive").addEventListener("click", () => archiveSessions([...state.selected], false));
$("#bulkDelete").addEventListener("click", e => confirmTwice(e.currentTarget, () => deleteSessions([...state.selected]),
  `Confirm delete ${state.selected.size}`));
$("#fArch").addEventListener("change", () => { if (state.view === "messages") runSearch(1); });
$$(".tabs > button").forEach(b => b.addEventListener("click", () => showTab(b.dataset.tab)));
$("#syncBtn").addEventListener("click", () => triggerSync(false));
$("#pBuild").addEventListener("click", () => buildPack(false));
$("#pClear").addEventListener("click", () => { state.pack = []; savePack(); renderPackItems(); });
$("#pCopy").addEventListener("click", () => state.lastPack && copy(state.lastPack.markdown, "Context pack copied — paste it into Claude Code"));
$("#pSave").addEventListener("click", () => buildPack(true));
$("#pDownload").addEventListener("click", () => {
  if (!state.lastPack) return;
  if (HOST) { HOST.saveAs(state.lastPack.markdown); return; }
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([state.lastPack.markdown], { type: "text/markdown" }));
  a.download = `context-pack-${new Date().toISOString().slice(0, 16).replace(/[:T]/g, "")}.md`; a.click();
});

document.addEventListener("keydown", e => {
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName);
  if (e.key === "Escape" && typing) { document.activeElement.blur(); return; }
  if (typing || e.ctrlKey || e.metaKey || e.altKey) return;
  const inHistory = $("#tab-history").classList.contains("active");
  const inSearch = inHistory && state.view === "messages";
  if (e.key === "Escape" && isNarrow()) { $$(".split.show-detail").forEach(x => x.classList.remove("show-detail")); return; }
  if (e.key === "/") { e.preventDefault(); showTab("history"); $("#q").focus(); $("#q").select(); }
  else if (["1", "2", "3"].includes(e.key)) showTab(["history", "pack", "health"][+e.key - 1]);
  else if (inSearch && (e.key === "j" || e.key === "ArrowDown")) { e.preventDefault(); select(state.sel + 1); }
  else if (inSearch && (e.key === "k" || e.key === "ArrowUp")) { e.preventDefault(); select(state.sel - 1); }
  else if (inSearch && e.key === "Enter" && state.sel >= 0) showSession(state.results[state.sel].session_id, $("#detail"), state.results[state.sel].id);
  else if (e.key === "a" || e.key === "c") {
    const cur = inHistory && $("#detail")._current;
    if (!cur || !cur.hit) return;
    if (e.key === "a") addToPack(cur.hit, cur.session); else copy(excerpt(cur.session, cur.hit), "Excerpt copied with attribution");
  }
});

if (HOST) {
  // Links to files/folders open through the extension instead of the OS protocol handler.
  document.addEventListener("click", e => {
    const a = e.target.closest('a[href^="vscode://file/"]');
    if (!a) return;
    e.preventDefault();
    HOST.open(decodeURI(a.getAttribute("href").slice("vscode://file/".length)));
  });
  const toChat = document.createElement("button");
  toChat.id = "pChat"; toChat.textContent = "Send to chat"; toChat.className = "primary"; toChat.disabled = true;
  toChat.title = "Start a new Claude Code chat in this workspace with the pack as its first message (you review before sending)";
  $("#pCopy").before(toChat);
  toChat.addEventListener("click", async () => {
    if (!state.lastPack) return;
    try { await HOST.toChat(state.lastPack.markdown); toast("Pack placed in a new Claude chat. Review and send it", 4000); }
    catch (e) { toast("Can't open Claude chat: " + e.message, 6000); }
  });
  const ins = document.createElement("button");
  ins.id = "pInsert"; ins.textContent = "Insert in editor"; ins.disabled = true;
  ins.title = "Insert the pack at the cursor of the active editor";
  $("#pDownload").textContent = "Save as…";
  $("#pSave").hidden = true;  // "Save as…" covers it inside the editor
  $("#pDownload").after(ins);
  ins.addEventListener("click", () => state.lastPack && HOST.insert(state.lastPack.markdown));
  new MutationObserver(() => { ins.disabled = toChat.disabled = $("#pCopy").disabled; }).observe($("#pCopy"), { attributes: true });
  // The extension can push a query (e.g. "search selection").
  window.cchSearch = q => { showTab("history"); $("#q").value = q; state.viewPinned = false; onQueryChange(); };
}

(async function init() {
  savePack();
  initResizers();
  toggleFilters(lsGet("cch.filters") === "1");
  updateFilterBadge();
  await refreshStatus();
  loadProjects().catch(() => {});
  setInterval(refreshStatus, 10000);
  const params = new URLSearchParams(location.search);
  const q = (HOST && HOST.initialQuery != null ? HOST.initialQuery : params.get("q")) || "";
  $("#q").value = q;
  // Deep links: ?q=…&view=sessions shows sessions containing matches; ?session=<id> opens one.
  state.viewPinned = !!q && params.get("view") === "sessions";
  setView(q && !state.viewPinned ? "messages" : "sessions");
  onQueryChange();
  if (params.get("session")) {
    state.lastQuery = q;
    revealDetail($("#detail"));
    showSession(params.get("session"), $("#detail"));
  }
  showTab(params.get("tab") || "history");
  if (!q) $("#q").focus();
})();
