const state = {
  papers: [],
  remotePaperSearchResults: [],
  wikiPages: [],
  wikiOpenFolders: new Set(),
  wikiFilter: "",
  activePaper: null,
  chatPaper: null,
  chatContextKey: "general",
  currentWikiPath: null,
  chatModel: "",
  applyingHistory: false,
  obsidianExportDir: "",
  uploadProgressTimer: null,
  uploadJobId: "",
  remoteBuildProgressTimer: null,
  remoteBuildJobId: "",
  remotePaperGraphZoom: 1,
  remotePaperGraphSelected: 0,
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));
const bootControlSelector = "button, input, select, textarea";
const bootFocusableSelector = "a[href], summary, [tabindex]";

function setBootMessage(message) {
  const node = $("#bootMessage");
  if (node) node.textContent = message;
}

function setBootControlsDisabled(disabled) {
  $$(bootControlSelector).forEach((control) => {
    if (disabled) {
      if (!control.disabled) {
        control.dataset.bootDisabled = "1";
        control.disabled = true;
      }
      return;
    }
    if (control.dataset.bootDisabled === "1") {
      control.disabled = false;
      delete control.dataset.bootDisabled;
    }
  });

  $$(bootFocusableSelector).forEach((node) => {
    if (disabled) {
      if (node.dataset.bootTabindex === undefined) {
        node.dataset.bootTabindex = node.getAttribute("tabindex") ?? "";
        node.setAttribute("tabindex", "-1");
        node.setAttribute("aria-disabled", "true");
      }
      return;
    }
    if (node.dataset.bootTabindex !== undefined) {
      const previous = node.dataset.bootTabindex;
      if (previous) node.setAttribute("tabindex", previous);
      else node.removeAttribute("tabindex");
      node.removeAttribute("aria-disabled");
      delete node.dataset.bootTabindex;
    }
  });
}

function setAppLoading(isLoading, message = "Astro-Note AI is loading...") {
  document.body.classList.toggle("app-loading", isLoading);
  document.body.setAttribute("aria-busy", isLoading ? "true" : "false");
  const overlay = $("#bootOverlay");
  if (overlay) overlay.hidden = !isLoading;
  if (isLoading) setBootMessage(message);
  setBootControlsDisabled(isLoading);
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    "\"": "&quot;",
    "'": "&#039;",
  }[char]));
}

function formatDate(value) {
  return value ? String(value).slice(0, 10) : "";
}

function isArxivPaperId(value) {
  return /^\d{4}\.\d{4,5}(v\d+)?$/.test(String(value || "")) || /^[a-z-]+\/\d{7}(v\d+)?$/.test(String(value || ""));
}

function sourceLinkLabel(paper) {
  const absUrl = String(paper?.abs_url || "");
  if (!absUrl.startsWith("http")) return "Local upload";
  return isArxivPaperId(paper?.arxiv_id) || absUrl.includes("arxiv.org") ? "arXiv" : "Source";
}

async function api(path, options = {}) {
  const timeoutMs = options.timeoutMs ?? 20000;
  const controller = typeof AbortController !== "undefined" ? new AbortController() : null;
  const timeout = controller ? window.setTimeout(() => controller.abort(), timeoutMs) : null;
  const { timeoutMs: _timeoutMs, headers, ...fetchOptions } = options;
  const isFormData = typeof FormData !== "undefined" && fetchOptions.body instanceof FormData;
  const requestHeaders = isFormData ? (headers || {}) : { "Content-Type": "application/json", ...(headers || {}) };
  let response;
  try {
    response = await fetch(path, {
      ...fetchOptions,
      headers: requestHeaders,
      signal: fetchOptions.signal || controller?.signal,
    });
  } catch (error) {
    if (error.name === "AbortError") throw new Error(`Request timed out: ${path}`);
    throw error;
  } finally {
    if (timeout) window.clearTimeout(timeout);
  }
  const text = await response.text();
  let data;
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    data = { error: text };
  }
  if (!response.ok) throw new Error(data.error || response.statusText);
  return data;
}

function showLoadError(target, label, error) {
  const node = $(target);
  if (!node) return;
  node.innerHTML = `<div class="item empty">${escapeHtml(label)} failed: ${escapeHtml(error.message || error)}</div>`;
}

const pendingMathRoots = new Set();

function runMathTypeset(roots) {
  if (!window.MathJax?.typesetPromise) return;
  window.MathJax.typesetPromise(roots).catch((error) => {
    console.warn("MathJax typeset failed", error);
  });
}

function typesetMath(root = document.body) {
  if (!root) return;
  if (window.MathJax?.typesetPromise) {
    runMathTypeset([root]);
    return;
  }
  pendingMathRoots.add(root);
}

window.addEventListener("mathjax-ready", () => {
  const roots = pendingMathRoots.size ? Array.from(pendingMathRoots) : [document.body];
  pendingMathRoots.clear();
  runMathTypeset(roots);
});

function routeFromLocation() {
  const params = new URLSearchParams(window.location.search);
  const anchor = window.location.hash ? decodeURIComponent(window.location.hash.slice(1)) : "";
  if (params.get("chatPaper")) return { view: "chat", chatPaper: params.get("chatPaper") };
  if (params.get("paper")) return { view: "papers", paper: params.get("paper") };
  if (params.get("wiki")) return { view: "wiki", wiki: params.get("wiki"), anchor };
  return { view: params.get("view") || "dashboard" };
}

function normalizeRoute(route = {}) {
  const validViews = new Set(["dashboard", "upload", "papers", "wiki", "chat", "graph", "review", "runs"]);
  const view = validViews.has(route.view) ? route.view : "dashboard";
  if (route.chatPaper) return { view: "chat", chatPaper: String(route.chatPaper) };
  if (route.paper) return { view: "papers", paper: String(route.paper) };
  if (route.wiki) return { view: "wiki", wiki: String(route.wiki), anchor: route.anchor ? String(route.anchor) : "" };
  return { view };
}

function routeUrl(route) {
  const normalized = normalizeRoute(route);
  const params = new URLSearchParams();
  if (normalized.view !== "dashboard") params.set("view", normalized.view);
  if (normalized.wiki) params.set("wiki", normalized.wiki);
  if (normalized.paper) params.set("paper", normalized.paper);
  if (normalized.chatPaper) params.set("chatPaper", normalized.chatPaper);
  const query = params.toString();
  const hash = normalized.anchor ? `#${encodeURIComponent(normalized.anchor)}` : "";
  return `${window.location.pathname}${query ? `?${query}` : ""}${hash}`;
}

function updateHistory(route, { replace = false } = {}) {
  if (state.applyingHistory) return;
  const normalized = normalizeRoute(route);
  const url = routeUrl(normalized);
  const current = `${window.location.pathname}${window.location.search}`;
  const method = replace || current === url ? "replaceState" : "pushState";
  window.history[method](normalized, "", url);
}

function navigateToView(name) {
  setView(name);
  const route = name === "chat" && state.chatPaper
    ? { view: "chat", chatPaper: state.chatPaper.arxiv_id }
    : { view: name };
  updateHistory(route);
}

async function applyHistoryRoute(route) {
  state.applyingHistory = true;
  closeCitationTrace();
  closeWikiSubReader();
  const normalized = normalizeRoute(route);
  try {
    if (normalized.wiki) {
      setView("wiki");
      await loadWikiPage(normalized.wiki, { anchor: normalized.anchor, updateHistory: false });
      return;
    }
    if (normalized.paper) {
      setView("papers");
      await loadPaperDetail(normalized.paper, { updateHistory: false });
      return;
    }
    if (normalized.chatPaper) {
      await openPaperChatById(normalized.chatPaper, { updateHistory: false });
      return;
    }
    setView(normalized.view);
  } finally {
    state.applyingHistory = false;
  }
}

function setView(name) {
  $$(".tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.view === name));
  $$(".view").forEach((view) => view.classList.toggle("active", view.id === name));
  if (name === "graph") $("#graphFrame").src = `/graph.html?ts=${Date.now()}`;
  if (name === "review") loadReviewQueue();
}

async function loadDashboard() {
  $("#metrics").innerHTML = `<div class="metric"><div class="label">Loading dashboard...</div></div>`;
  const data = await api("/api/summary", { timeoutMs: 15000 });
  $("#subtitle").textContent = "Powered by LLM Wiki";
  const metrics = [
    ["Papers", data.total_papers],
    ["Graphed", data.status_counts.graphed || 0],
    ["Rejected", data.status_counts.rejected || 0],
    ["Semantic topics", (data.semantic_pages || []).length],
    ["Graph edges", data.graph.edges || 0],
  ];
  $("#metrics").innerHTML = metrics.map(([label, value]) => `
    <div class="metric"><div class="value">${value}</div><div class="label">${label}</div></div>
  `).join("");
  const semanticPages = data.semantic_pages || [];
  $("#semanticTopicList").innerHTML = semanticPages.map((page) => `
    <div class="item" data-wiki="${escapeHtml(page.path)}">
      <div class="item-title">${escapeHtml(page.title)}</div>
      <div class="item-meta">${escapeHtml(page.path)}</div>
    </div>
  `).join("") || `<div class="item empty">No semantic topics.</div>`;
  $("#recentPapers").innerHTML = data.recent_papers.map((paper) => paperItem(paper)).join("");
  $("#semanticTopicList").querySelectorAll("[data-wiki]").forEach((row) => {
    row.addEventListener("click", () => {
      setView("wiki");
      loadWikiPage(row.dataset.wiki);
    });
  });
  $("#recentPapers").querySelectorAll("[data-paper]").forEach((row) => {
    row.addEventListener("click", () => {
      openDashboardPaperChat(row.dataset.paper);
    });
  });
}

async function openDashboardPaperChat(arxivId) {
  const data = await api(`/api/paper?id=${encodeURIComponent(arxivId)}`);
  startPaperChat(data.paper);
}

async function openPaperChatById(arxivId, options = {}) {
  if (!arxivId) return;
  const data = await api(`/api/paper?id=${encodeURIComponent(arxivId)}`);
  startPaperChat(data.paper, options);
}

async function loadModels() {
  const select = $("#chatModel");
  try {
    const data = await api("/api/models", { timeoutMs: 15000 });
    const options = data.options || (data.models || []).map((model) => ({
      value: model,
      label: model,
      available: true,
      message: "",
    }));
    const available = options.filter((option) => option.available);
    state.chatModel = data.default || available[0]?.value || "";
    select.innerHTML = options.map((option) => {
      const disabled = option.available ? "" : " disabled";
      const title = option.message ? ` title="${escapeHtml(option.message)}"` : "";
      const suffix = option.available ? "" : " (unavailable)";
      return `<option value="${escapeHtml(option.value)}"${disabled}${title}>${escapeHtml(option.label + suffix)}</option>`;
    }).join("") || `<option value="">No chat models</option>`;
    select.value = state.chatModel;
    select.title = options.find((option) => option.value === select.value)?.message || "";
  } catch (error) {
    select.innerHTML = `<option value="">Model load failed</option>`;
    select.title = error.message;
  }
}

function paperItem(paper) {
  const topics = paper.topics?.length ? paper.topics.slice(0, 3).join(", ") : (paper.topic || "Unclassified");
  return `
    <div class="item" data-paper="${escapeHtml(paper.arxiv_id)}">
      <div class="item-title">${escapeHtml(paper.title)}</div>
      <div class="item-meta">${escapeHtml(paper.arxiv_id)} · ${escapeHtml(paper.status)} · ${escapeHtml(topics)}</div>
    </div>
  `;
}

function paperWikiPath(arxivId) {
  return `wiki/papers/${String(arxivId || "").replace(/\//g, "_")}.md`;
}

async function loadPapers() {
  $("#paperList").innerHTML = `<div class="item empty">Loading papers...</div>`;
  const params = new URLSearchParams();
  const query = $("#paperSearch").value.trim();
  const status = $("#statusFilter").value;
  if (query) params.set("q", query);
  if (status) params.set("status", status);
  const data = await api(`/api/papers?${params.toString()}`, { timeoutMs: 15000 });
  const papers = Array.isArray(data.papers) ? data.papers : [];
  state.papers = papers;
  renderStatusOptions(data.statuses || []);
  const groups = data.paper_groups?.length
    ? data.paper_groups
    : [{ topic: "Papers", count: papers.length, papers }];
  if (!papers.length) {
    const detail = [status ? `status: ${status}` : "", query ? `search: ${query}` : ""].filter(Boolean).join(" · ");
    $("#paperList").innerHTML = `<div class="item empty">No papers${detail ? ` (${escapeHtml(detail)})` : ""}.</div>`;
    return;
  }
  $("#paperList").innerHTML = groups.map((group) => `
    <section class="paper-topic-group">
      <div class="paper-topic-head">
        <span>${escapeHtml(group.topic)}</span>
        <span>${escapeHtml(String(group.count))}</span>
      </div>
      ${group.papers.map((paper) => {
        const topics = paper.topics?.length ? paper.topics.slice(0, 4).join(", ") : (paper.topic || "Unclassified");
        const wikiButton = paper.wiki_exists
          ? `<button class="paper-wiki-button" type="button" data-wiki="${escapeHtml(paper.wiki_path || paperWikiPath(paper.arxiv_id))}">Wiki</button>`
          : `<button class="paper-wiki-button" type="button" disabled title="Wiki has not been generated yet.">No Wiki</button>`;
        return `
          <div class="paper-row" data-paper="${escapeHtml(paper.arxiv_id)}">
            <div>${escapeHtml(paper.arxiv_id)}</div>
            <div><span class="badge ${badgeClass(paper.status)}">${escapeHtml(paper.status)}</span></div>
            <div>
              <div class="item-title">${escapeHtml(paper.title)}</div>
              <div class="item-meta">${escapeHtml(topics)}</div>
            </div>
            ${wikiButton}
          </div>
        `;
      }).join("")}
    </section>
  `).join("") || `<div class="item empty">No papers.</div>`;
  $("#paperList").querySelectorAll("[data-paper]").forEach((row) => {
    row.addEventListener("click", () => loadPaperDetail(row.dataset.paper));
  });
  $("#paperList").querySelectorAll("[data-wiki]").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      setView("wiki");
      loadWikiPage(button.dataset.wiki);
    });
  });
}

function renderRemotePaperSearchResults(data) {
  const target = $("#remotePaperSearchResults");
  const status = $("#remotePaperSearchStatus");
  const papers = Array.isArray(data?.papers) ? data.papers : [];
  state.remotePaperSearchResults = papers;
  if (status) {
    const sourceLabels = { ads: "NASA ADS", arxiv: "arXiv", "arxiv-web": "arXiv web", local: "local-library" };
    const sourceLabel = sourceLabels[data?.source] || "paper-search";
    const warning = data?.warning ? `${data.warning} ` : "";
    status.textContent = papers.length
      ? `${warning}${papers.length} ${sourceLabel} candidate(s). Top matches are emphasized; additional candidates remain selectable in the graph. ${data.ranking_note || ""}`
      : `${warning}No candidates for this search.`;
  }
  if (!target) return;
  renderRemotePaperGraph(papers);
  target.innerHTML = papers.map((paper, index) => {
    const authors = Array.isArray(paper.authors) ? paper.authors.slice(0, 4).join(", ") : "";
    const authorSuffix = Array.isArray(paper.authors) && paper.authors.length > 4 ? ", et al." : "";
    const score = Number(paper.search_score || 0).toFixed(1);
    const localStatus = paper.local_status || "";
    const isBuilt = ["graphed", "curated", "ingested"].includes(localStatus);
    const canBuild = paper.can_build !== false && Boolean(paper.pdf_url);
    const tier = index < 10 ? "Top match" : "Candidate";
    const linkLabel = paper.source === "ads" ? "ADS" : "Open";
    return `
      <article class="paper-search-result ${index < 10 ? "primary" : "candidate"}" data-result-index="${index}" tabindex="0">
        <div class="paper-search-result-main">
          <div class="item-title">${escapeHtml(paper.title)}</div>
          <div class="item-meta">
            ${escapeHtml(String(index + 1))} · ${escapeHtml(tier)} · ${escapeHtml(paper.arxiv_id)} · ${escapeHtml(paper.primary_category || paper.source || "paper")} · score ${escapeHtml(score)}
          </div>
          <div class="item-meta">${escapeHtml(authors + authorSuffix)}</div>
          <p>${escapeHtml(paper.abstract_snippet || paper.abstract || "")}</p>
        </div>
        <div class="paper-search-actions">
          <a href="${escapeHtml(paper.abs_url || `https://arxiv.org/abs/${paper.arxiv_id}`)}" target="_blank" rel="noreferrer">${escapeHtml(linkLabel)}</a>
          <button type="button" data-build-paper="${index}" ${isBuilt || !canBuild ? "disabled" : ""}>${isBuilt ? "Built" : (canBuild ? "Build Wiki" : "No PDF")}</button>
        </div>
      </article>
    `;
  }).join("") || `<div class="item empty">No paper-search results yet.</div>`;
  target.querySelectorAll("[data-result-index]").forEach((row) => {
    row.addEventListener("click", () => selectRemotePaperResult(Number(row.dataset.resultIndex)));
    row.addEventListener("keydown", (event) => {
      if (event.key === "Enter") selectRemotePaperResult(Number(row.dataset.resultIndex));
    });
  });
  target.querySelectorAll("[data-build-paper]").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      processRemotePaperSearchResult(Number(button.dataset.buildPaper));
    });
  });
  selectRemotePaperResult(0, { scroll: false });
}

function paperGraphTokens(paper) {
  const text = `${paper.title || ""} ${paper.abstract || ""} ${paper.categories || ""}`.toLowerCase();
  const stop = new Set(["the", "and", "for", "with", "from", "this", "that", "are", "into", "using", "paper", "study", "result", "results", "data", "astro", "arxiv"]);
  return new Set((text.match(/[a-z0-9][a-z0-9.+_-]{2,}/g) || []).filter((token) => !stop.has(token)));
}

function paperSimilarity(left, right) {
  const a = paperGraphTokens(left);
  const b = paperGraphTokens(right);
  if (!a.size || !b.size) return 0;
  let overlap = 0;
  a.forEach((token) => {
    if (b.has(token)) overlap += 1;
  });
  return overlap / Math.sqrt(a.size * b.size);
}

function remotePaperGraphLayout(papers) {
  const width = 760;
  const height = 460;
  const centerX = width / 2;
  const centerY = height / 2;
  const ranked = papers.map((paper, index) => ({ paper, index, score: Number(paper.search_score || 0) }));
  const maxScore = Math.max(1, ...ranked.map((item) => item.score));
  return ranked.map((item, order) => {
    const isPrimary = order < 10;
    const groupIndex = isPrimary ? order : order - 10;
    const groupSize = isPrimary ? Math.min(10, ranked.length) : Math.max(1, ranked.length - 10);
    const angle = (Math.PI * 2 * groupIndex) / Math.max(1, groupSize);
    const candidateRing = Math.floor(groupIndex / 24);
    const radius = isPrimary
      ? 92 + (order % 2) * 38
      : Math.min(208 + candidateRing * 42, 322);
    return {
      ...item,
      isPrimary,
      x: centerX + Math.cos(angle) * radius,
      y: centerY + Math.sin(angle) * radius,
      r: isPrimary ? 10 + Math.min(12, item.score / maxScore * 12) : 6 + Math.min(5, item.score / maxScore * 5),
    };
  });
}

function renderRemotePaperGraph(papers) {
  const target = $("#remotePaperGraph");
  if (!target) return;
  if (!papers.length) {
    target.innerHTML = `<div class="item empty">No discovery graph yet.</div>`;
    return;
  }
  const nodes = remotePaperGraphLayout(papers);
  const zoom = Math.max(0.6, Math.min(2, Number(state.remotePaperGraphZoom || 1)));
  const viewWidth = 760 / zoom;
  const viewHeight = 460 / zoom;
  const viewX = 380 - viewWidth / 2;
  const viewY = 230 - viewHeight / 2;
  const links = [];
  for (let i = 0; i < papers.length; i += 1) {
    for (let j = i + 1; j < papers.length; j += 1) {
      const similarity = paperSimilarity(papers[i], papers[j]);
      const threshold = nodes[i].isPrimary || nodes[j].isPrimary ? 0.12 : 0.18;
      if (similarity >= threshold) links.push({ source: nodes[i], target: nodes[j], similarity });
    }
  }
  links.sort((left, right) => right.similarity - left.similarity);
  const visibleLinks = links.slice(0, 280);
  target.innerHTML = `
    <div class="paper-graph-toolbar">
      <button type="button" data-graph-zoom="-1" ${zoom <= 0.6 ? "disabled" : ""}>-</button>
      <button type="button" data-graph-reset>Reset</button>
      <button type="button" data-graph-zoom="1" ${zoom >= 2 ? "disabled" : ""}>+</button>
      <span>${Math.round(zoom * 100)}%</span>
    </div>
    <svg viewBox="${viewX.toFixed(1)} ${viewY.toFixed(1)} ${viewWidth.toFixed(1)} ${viewHeight.toFixed(1)}" role="img" aria-label="Similarity graph of arXiv search results">
      <rect x="0" y="0" width="760" height="460" rx="8"></rect>
      <g class="paper-graph-ring">
        <circle cx="380" cy="230" r="150"></circle>
        <circle cx="380" cy="230" r="330"></circle>
      </g>
      <g class="paper-graph-legend">
        <circle cx="24" cy="24" r="7" class="legend-primary"></circle>
        <text x="38" y="28">Top matches</text>
        <circle cx="128" cy="24" r="5" class="legend-candidate"></circle>
        <text x="140" y="28">More candidates</text>
      </g>
      ${visibleLinks.map((link) => `
        <line
          x1="${link.source.x.toFixed(1)}"
          y1="${link.source.y.toFixed(1)}"
          x2="${link.target.x.toFixed(1)}"
          y2="${link.target.y.toFixed(1)}"
          stroke-width="${Math.max(1, link.similarity * 7).toFixed(2)}"
        />
      `).join("")}
      ${nodes.map((node) => `
        <g class="paper-node ${node.isPrimary ? "primary" : "candidate"}" data-node-index="${node.index}" tabindex="0" transform="translate(${node.x.toFixed(1)} ${node.y.toFixed(1)})">
          <circle r="${node.r.toFixed(1)}"></circle>
          ${node.isPrimary ? `<text x="${(node.r + 8).toFixed(1)}" y="4">${escapeHtml(String(node.index + 1))}</text>` : ""}
          <title>${escapeHtml(node.paper.title || node.paper.arxiv_id)}</title>
        </g>
      `).join("")}
    </svg>
  `;
  target.querySelectorAll("[data-graph-zoom]").forEach((button) => {
    button.addEventListener("click", () => {
      const direction = Number(button.dataset.graphZoom || "0");
      state.remotePaperGraphZoom = Math.max(0.6, Math.min(2, zoom + direction * 0.2));
      renderRemotePaperGraph(state.remotePaperSearchResults);
      selectRemotePaperResult(state.remotePaperGraphSelected, { scroll: false });
    });
  });
  target.querySelector("[data-graph-reset]")?.addEventListener("click", () => {
    state.remotePaperGraphZoom = 1;
    renderRemotePaperGraph(state.remotePaperSearchResults);
    selectRemotePaperResult(state.remotePaperGraphSelected, { scroll: false });
  });
  target.querySelectorAll("[data-node-index]").forEach((node) => {
    node.addEventListener("click", () => selectRemotePaperResult(Number(node.dataset.nodeIndex)));
    node.addEventListener("keydown", (event) => {
      if (event.key === "Enter") selectRemotePaperResult(Number(node.dataset.nodeIndex));
    });
  });
}

function selectRemotePaperResult(index, options = {}) {
  const paper = state.remotePaperSearchResults[index];
  if (!paper) return;
  state.remotePaperGraphSelected = index;
  $$("#remotePaperGraph [data-node-index]").forEach((node) => {
    node.classList.toggle("selected", Number(node.dataset.nodeIndex) === index);
  });
  $$("#remotePaperSearchResults [data-result-index]").forEach((row) => {
    row.classList.toggle("selected", Number(row.dataset.resultIndex) === index);
  });
  if (options.scroll !== false) {
    $(`#remotePaperSearchResults [data-result-index="${index}"]`)?.scrollIntoView({ block: "nearest" });
  }
}

async function searchRemotePapers(event) {
  event?.preventDefault();
  const query = $("#remotePaperSearch").value.trim();
  const limit = $("#remotePaperSearchLimit").value || "10";
  const provider = $("#remotePaperSearchProvider")?.value || "auto";
  const useLlm = $("#remotePaperUseLlm")?.checked ? "1" : "0";
  if (!query) {
    $("#remotePaperSearchStatus").textContent = "Enter a paper topic or arXiv search query.";
    return;
  }
  $("#remotePaperSearchStatus").textContent = useLlm === "1"
    ? "Interpreting goal with LLM, then searching ADS/arXiv..."
    : "Searching ADS/arXiv...";
  $("#remotePaperSearchResults").innerHTML = `<div class="item empty">Searching...</div>`;
  const params = new URLSearchParams({ q: query, limit, provider, use_llm: useLlm });
  try {
    const data = await api(`/api/paper-search?${params.toString()}`, { timeoutMs: 70000 });
    state.remotePaperGraphZoom = 1;
    state.remotePaperGraphSelected = 0;
    renderRemotePaperSearchResults(data);
  } catch (error) {
    $("#remotePaperSearchStatus").textContent = `Search failed: ${error.message}`;
    $("#remotePaperSearchResults").innerHTML = "";
  }
}

async function processRemotePaperSearchResult(index) {
  const paper = state.remotePaperSearchResults[index];
  if (!paper) return;
  if (paper.can_build === false || !paper.pdf_url) {
    $("#remotePaperSearchStatus").textContent = "This candidate has no downloadable arXiv PDF. Open the ADS record or choose another candidate.";
    return;
  }
  selectRemotePaperResult(index);
  const button = $(`[data-build-paper="${index}"]`);
  if (button) {
    button.disabled = true;
    button.textContent = "Building...";
  }
  const jobId = createUploadJobId();
  renderRemoteBuildProgress({
    status: "running",
    stage: "Preparing Build Wiki",
    message: `Preparing ${paper.arxiv_id} for download, extraction, wiki generation, and graph rebuild.`,
    percent: 0,
    file_index: 1,
    total_files: 1,
    filename: paper.arxiv_id,
    log: [],
  });
  startRemoteBuildProgressPolling(jobId);
  $("#remotePaperSearchStatus").textContent = `Selected ${paper.arxiv_id}. Downloading PDF and building wiki artifacts...`;
  try {
    await api("/api/paper-search-import", {
      method: "POST",
      body: JSON.stringify({ paper, progress_job_id: jobId }),
      timeoutMs: 20000,
    });
    const data = await api("/api/paper-action", {
      method: "POST",
      body: JSON.stringify({ arxiv_id: paper.arxiv_id, action: "process_to_graph", progress_job_id: jobId }),
      timeoutMs: 1800000,
    });
    stopRemoteBuildProgressPolling();
    paper.already_added = true;
    paper.local_status = data.status || "graphed";
    renderRemoteBuildProgress({
      status: data.ok ? "completed" : "failed",
      stage: data.ok ? "Build Wiki complete" : "Build Wiki failed",
      message: data.ok ? `Built wiki for ${paper.arxiv_id}.` : `Build failed for ${paper.arxiv_id}.`,
      percent: 100,
      file_index: 1,
      total_files: 1,
      filename: paper.arxiv_id,
    });
    $("#remotePaperSearchStatus").textContent = data.ok
      ? `Built wiki for ${paper.arxiv_id}.`
      : `Build failed for ${paper.arxiv_id}. ${data.output || ""}`;
    renderRemotePaperSearchResults({ papers: state.remotePaperSearchResults, ranking_note: "Selecting a paper runs download, extraction, wiki generation, and graph rebuild." });
    await loadPapers();
    await loadDashboard();
    await loadWikiList();
    await loadPaperDetail(paper.arxiv_id);
  } catch (error) {
    stopRemoteBuildProgressPolling();
    renderRemoteBuildProgress({
      status: "failed",
      stage: "Build Wiki failed",
      message: error.message,
      percent: 100,
      file_index: 1,
      total_files: 1,
      filename: paper.arxiv_id,
      log: [{ time: new Date().toLocaleTimeString(), stage: "Build Wiki failed", message: error.message }],
    });
    if (button) {
      button.disabled = false;
      button.textContent = "Build Wiki";
    }
    $("#remotePaperSearchStatus").textContent = `Build failed: ${error.message}`;
  }
}

function clearRemotePaperSearch() {
  state.remotePaperSearchResults = [];
  state.remotePaperGraphSelected = 0;
  state.remotePaperGraphZoom = 1;
  $("#remotePaperSearch").value = "";
  $("#remotePaperSearchStatus").textContent = "Search ADS/arXiv astro-ph. Similar papers appear as a graph; choose one to download and build a wiki page.";
  $("#remotePaperSearchResults").innerHTML = "";
  $("#remotePaperGraph").innerHTML = `<div class="item empty">No discovery graph yet.</div>`;
  const progress = $("#remoteBuildProgress");
  if (progress) progress.hidden = true;
}

function renderStatusOptions(statuses) {
  const select = $("#statusFilter");
  const current = select.value;
  select.innerHTML = `<option value="">All statuses</option>` + statuses.map((status) => (
    `<option value="${escapeHtml(status)}">${escapeHtml(status)}</option>`
  )).join("");
  select.value = statuses.includes(current) ? current : "";
}

function badgeClass(status) {
  if (status === "graphed") return "graphed";
  if (status === "rejected") return "rejected";
  if (String(status).startsWith("failed")) return "failed";
  return "";
}

function pdfViewerMarkup(viewerId, paper) {
  if (!paper?.pdf_path) {
    return `<div class="paper-notice">이 문서는 PDF 뷰어로 표시할 로컬 PDF 파일이 없습니다. Wiki와 Chat은 생성된 Markdown source를 사용합니다.</div>`;
  }
  return `
    <div id="${escapeHtml(viewerId)}" class="pdf-viewer" data-pdf-path="${escapeHtml(paper.pdf_path)}" data-page="1" data-pages="1" data-zoom="1.35" data-fit="width">
      <div class="pdf-toolbar">
        <button type="button" data-pdf-prev>Prev</button>
        <span><strong data-pdf-current>1</strong> / <span data-pdf-total>...</span></span>
        <button type="button" data-pdf-next>Next</button>
        <span class="pdf-toolbar-divider"></span>
        <button type="button" data-pdf-zoom-out>-</button>
        <button type="button" data-pdf-fit>Fit width</button>
        <button type="button" data-pdf-zoom-in>+</button>
        <span data-pdf-zoom-label>Fit</span>
      </div>
      <div class="pdf-stage">
        <div class="pdf-pages" data-pdf-pages></div>
      </div>
    </div>
  `;
}

async function mountPdfViewer(selector) {
  const viewer = $(selector);
  if (!viewer) return;
  const pdfPath = viewer.dataset.pdfPath;
  if (!pdfPath) return;
  const image = viewer.querySelector("[data-pdf-image]");
  const current = viewer.querySelector("[data-pdf-current]");
  const total = viewer.querySelector("[data-pdf-total]");
  const prev = viewer.querySelector("[data-pdf-prev]");
  const next = viewer.querySelector("[data-pdf-next]");
  const zoomOut = viewer.querySelector("[data-pdf-zoom-out]");
  const zoomIn = viewer.querySelector("[data-pdf-zoom-in]");
  const fit = viewer.querySelector("[data-pdf-fit]");
  const zoomLabel = viewer.querySelector("[data-pdf-zoom-label]");
  const stage = viewer.querySelector(".pdf-stage");
  let pageWidth = 0;
  let resizeTimer = null;
  function clampZoom(value) {
    return Math.max(0.45, Math.min(3, Number(value) || 1));
  }
  function fitZoom() {
    if (!stage || !pageWidth) return 1.2;
    return clampZoom((stage.clientWidth - 32) / pageWidth);
  }
  function currentZoom() {
    return viewer.dataset.fit === "width" ? fitZoom() : clampZoom(viewer.dataset.zoom || "1.35");
  }
  async function loadInfo() {
    const info = await api(`/api/pdf-info?path=${encodeURIComponent(pdfPath)}`, { timeoutMs: 15000 });
    viewer.dataset.pages = String(info.pages || 1);
    pageWidth = Number(info.page_width || 0);
    total.textContent = viewer.dataset.pages;
  }
  function renderPage(page) {
    const pages = Number(viewer.dataset.pages || "1");
    const nextPage = Math.min(Math.max(1, page), pages);
    const zoom = currentZoom();
    viewer.dataset.page = String(nextPage);
    current.textContent = String(nextPage);
    prev.disabled = nextPage <= 1;
    next.disabled = nextPage >= pages;
    zoomOut.disabled = zoom <= 0.45;
    zoomIn.disabled = zoom >= 3;
    zoomLabel.textContent = viewer.dataset.fit === "width" ? "Fit" : `${Math.round(zoom * 100)}%`;
    image.src = `/pdf-page?path=${encodeURIComponent(pdfPath)}&page=${nextPage}&zoom=${encodeURIComponent(zoom)}`;
  }
  prev.addEventListener("click", () => renderPage(Number(viewer.dataset.page || "1") - 1));
  next.addEventListener("click", () => renderPage(Number(viewer.dataset.page || "1") + 1));
  zoomOut.addEventListener("click", () => {
    viewer.dataset.fit = "custom";
    viewer.dataset.zoom = String(clampZoom(currentZoom() - 0.15));
    renderPage(Number(viewer.dataset.page || "1"));
  });
  zoomIn.addEventListener("click", () => {
    viewer.dataset.fit = "custom";
    viewer.dataset.zoom = String(clampZoom(currentZoom() + 0.15));
    renderPage(Number(viewer.dataset.page || "1"));
  });
  fit.addEventListener("click", () => {
    viewer.dataset.fit = "width";
    renderPage(Number(viewer.dataset.page || "1"));
  });
  if (typeof ResizeObserver !== "undefined" && stage) {
    const observer = new ResizeObserver(() => {
      if (viewer.dataset.fit !== "width") return;
      window.clearTimeout(resizeTimer);
      resizeTimer = window.setTimeout(() => renderPage(Number(viewer.dataset.page || "1")), 120);
    });
    observer.observe(stage);
  }
  image.addEventListener("error", () => {
    image.replaceWith(Object.assign(document.createElement("div"), {
      className: "paper-notice",
      textContent: "PDF page를 앱 내부에서 렌더링하지 못했습니다.",
    }));
  }, { once: true });
  try {
    await loadInfo();
    renderPage(1);
  } catch (error) {
    viewer.innerHTML = `<div class="paper-notice">${escapeHtml(error.message)}</div>`;
  }
}

async function mountContinuousPdfViewer(selector) {
  const viewer = $(selector);
  if (!viewer) return;
  const pdfPath = viewer.dataset.pdfPath;
  if (!pdfPath) return;
  const current = viewer.querySelector("[data-pdf-current]");
  const total = viewer.querySelector("[data-pdf-total]");
  const prev = viewer.querySelector("[data-pdf-prev]");
  const next = viewer.querySelector("[data-pdf-next]");
  const zoomOut = viewer.querySelector("[data-pdf-zoom-out]");
  const zoomIn = viewer.querySelector("[data-pdf-zoom-in]");
  const fit = viewer.querySelector("[data-pdf-fit]");
  const zoomLabel = viewer.querySelector("[data-pdf-zoom-label]");
  const stage = viewer.querySelector(".pdf-stage");
  const pagesEl = viewer.querySelector("[data-pdf-pages]");
  let pageWidth = 0;
  let pageHeight = 0;
  let resizeTimer = null;
  let scrollFrame = null;
  let loadObserver = null;

  function clampZoom(value) {
    return Math.max(0.45, Math.min(3, Number(value) || 1));
  }
  function fitZoom() {
    if (!stage || !pageWidth) return 1.2;
    return clampZoom((stage.clientWidth - 52) / pageWidth);
  }
  function currentZoom() {
    return viewer.dataset.fit === "width" ? fitZoom() : clampZoom(viewer.dataset.zoom || "1.35");
  }
  async function loadInfo() {
    const info = await api(`/api/pdf-info?path=${encodeURIComponent(pdfPath)}`, { timeoutMs: 15000 });
    viewer.dataset.pages = String(info.pages || 1);
    pageWidth = Number(info.page_width || 0);
    pageHeight = Number(info.page_height || 0);
    total.textContent = viewer.dataset.pages;
  }
  function pageImageUrl(page, zoom) {
    return `/pdf-page?path=${encodeURIComponent(pdfPath)}&page=${page}&zoom=${encodeURIComponent(zoom)}`;
  }
  function placeholderHeight(zoom) {
    if (!pageWidth || !pageHeight) return 760;
    return Math.max(240, Math.round(pageHeight * zoom));
  }
  function updateToolbar(page) {
    const pages = Number(viewer.dataset.pages || "1");
    const nextPage = Math.min(Math.max(1, Number(page) || 1), pages);
    const zoom = currentZoom();
    viewer.dataset.page = String(nextPage);
    current.textContent = String(nextPage);
    prev.disabled = nextPage <= 1;
    next.disabled = nextPage >= pages;
    zoomOut.disabled = zoom <= 0.45;
    zoomIn.disabled = zoom >= 3;
    zoomLabel.textContent = viewer.dataset.fit === "width" ? "Fit" : `${Math.round(zoom * 100)}%`;
  }
  function loadPage(pageEl) {
    if (!pageEl || pageEl.dataset.loaded === "1") return;
    const page = Number(pageEl.dataset.page || "1");
    const zoom = currentZoom();
    pageEl.dataset.loaded = "1";
    pageEl.innerHTML = `
      <div class="pdf-page-label">Page ${page}</div>
      <img alt="PDF page ${page}" src="${pageImageUrl(page, zoom)}">
    `;
    pageEl.querySelector("img").addEventListener("error", () => {
      pageEl.dataset.loaded = "0";
      pageEl.innerHTML = `<div class="paper-notice">PDF page ${page} could not be rendered.</div>`;
    }, { once: true });
  }
  function observePages() {
    if (loadObserver) loadObserver.disconnect();
    const pageItems = Array.from(pagesEl.querySelectorAll("[data-pdf-page-item]"));
    if (typeof IntersectionObserver === "undefined") {
      pageItems.slice(0, 4).forEach(loadPage);
      return;
    }
    loadObserver = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        if (entry.isIntersecting) loadPage(entry.target);
      }
    }, { root: stage, rootMargin: "900px 0px" });
    pageItems.forEach((pageEl) => loadObserver.observe(pageEl));
  }
  function updateCurrentPageFromScroll() {
    scrollFrame = null;
    const items = Array.from(pagesEl.querySelectorAll("[data-pdf-page-item]"));
    if (!items.length) return;
    const stageRect = stage.getBoundingClientRect();
    const center = stageRect.top + stageRect.height * 0.45;
    let bestPage = Number(viewer.dataset.page || "1");
    let bestDistance = Infinity;
    for (const item of items) {
      const rect = item.getBoundingClientRect();
      const distance = Math.abs((rect.top + rect.height / 2) - center);
      if (distance < bestDistance) {
        bestDistance = distance;
        bestPage = Number(item.dataset.page || "1");
      }
    }
    updateToolbar(bestPage);
  }
  function scheduleScrollUpdate() {
    if (scrollFrame) return;
    scrollFrame = window.requestAnimationFrame(updateCurrentPageFromScroll);
  }
  function scrollToPage(page, options = {}) {
    const pages = Number(viewer.dataset.pages || "1");
    const nextPage = Math.min(Math.max(1, Number(page) || 1), pages);
    updateToolbar(nextPage);
    const pageEl = pagesEl.querySelector(`[data-page="${nextPage}"]`);
    if (pageEl) {
      pageEl.scrollIntoView({ block: "start", behavior: options.behavior || "smooth" });
      loadPage(pageEl);
    }
  }
  function buildPages(targetPage = 1) {
    const pages = Number(viewer.dataset.pages || "1");
    const minHeight = placeholderHeight(currentZoom());
    pagesEl.innerHTML = Array.from({ length: pages }, (_, index) => {
      const page = index + 1;
      return `
        <section class="pdf-page" data-pdf-page-item data-page="${page}" data-loaded="0" style="min-height: ${minHeight}px">
          <div class="pdf-page-label">Page ${page}</div>
          <div class="pdf-page-placeholder">Loading page ${page}...</div>
        </section>
      `;
    }).join("");
    observePages();
    scrollToPage(targetPage, { behavior: "auto" });
  }
  function refreshLoadedPages(targetPage = Number(viewer.dataset.page || "1")) {
    const minHeight = placeholderHeight(currentZoom());
    pagesEl.querySelectorAll("[data-pdf-page-item]").forEach((pageEl) => {
      const page = Number(pageEl.dataset.page || "1");
      pageEl.style.minHeight = `${minHeight}px`;
      pageEl.dataset.loaded = "0";
      pageEl.innerHTML = `
        <div class="pdf-page-label">Page ${page}</div>
        <div class="pdf-page-placeholder">Loading page ${page}...</div>
      `;
    });
    observePages();
    scrollToPage(targetPage, { behavior: "auto" });
  }

  prev.addEventListener("click", () => scrollToPage(Number(viewer.dataset.page || "1") - 1));
  next.addEventListener("click", () => scrollToPage(Number(viewer.dataset.page || "1") + 1));
  zoomOut.addEventListener("click", () => {
    viewer.dataset.fit = "custom";
    viewer.dataset.zoom = String(clampZoom(currentZoom() - 0.15));
    refreshLoadedPages();
  });
  zoomIn.addEventListener("click", () => {
    viewer.dataset.fit = "custom";
    viewer.dataset.zoom = String(clampZoom(currentZoom() + 0.15));
    refreshLoadedPages();
  });
  fit.addEventListener("click", () => {
    viewer.dataset.fit = "width";
    refreshLoadedPages();
  });
  stage.addEventListener("scroll", scheduleScrollUpdate, { passive: true });
  if (typeof ResizeObserver !== "undefined") {
    const observer = new ResizeObserver(() => {
      if (viewer.dataset.fit !== "width") return;
      window.clearTimeout(resizeTimer);
      resizeTimer = window.setTimeout(() => refreshLoadedPages(), 120);
    });
    observer.observe(stage);
  }
  try {
    await loadInfo();
    buildPages(1);
  } catch (error) {
    viewer.innerHTML = `<div class="paper-notice">${escapeHtml(error.message)}</div>`;
  }
}

async function loadPaperDetail(arxivId, options = {}) {
  const data = await api(`/api/paper?id=${encodeURIComponent(arxivId)}`);
  state.activePaper = data.paper;
  const paper = data.paper;
  $("#paperDetail").classList.remove("empty");
  const absUrl = String(paper.abs_url || "");
  const sourceLink = absUrl.startsWith("http")
    ? `<a href="${escapeHtml(absUrl)}" target="_blank" rel="noreferrer">${escapeHtml(sourceLinkLabel(paper))}</a>`
    : `<span class="badge">Local upload</span>`;
  const canReject = paper.status !== "rejected";
  const topics = paper.topics?.length ? paper.topics.join(", ") : (paper.topic || "Unclassified");
  const pdfBlock = pdfViewerMarkup("paperPdfViewer", paper);
  const hasWiki = Boolean(data.wiki_exists || paper.wiki_exists || data.wiki_html);
  const wikiAction = hasWiki
    ? `<button type="button" id="openPaperWiki">Wiki</button>`
    : `<button type="button" id="openPaperWiki" disabled title="Wiki generation failed or has not completed.">No Wiki</button>`;
  $("#paperDetail").innerHTML = `
    <div class="paper-summary">
      <h1>${escapeHtml(paper.title)}</h1>
      <div>
        <span class="badge ${badgeClass(paper.status)}">${escapeHtml(paper.status)}</span>
        <span class="badge">${escapeHtml(paper.topic || "Unclassified")}</span>
        <span class="badge">score ${paper.relevance_score ?? "-"}</span>
      </div>
      <div class="item-meta">Paper ID: ${escapeHtml(paper.arxiv_id)} · ${escapeHtml(paper.categories)} · ${escapeHtml(topics)}</div>
      <div class="paper-actions">
        ${sourceLink}
        ${wikiAction}
        <button type="button" id="togglePaperInfo">Info</button>
        <button type="button" id="chatAboutPaper">Chat</button>
        ${canReject ? `<button type="button" id="deletePaper" class="danger">Delete</button>` : ""}
      </div>
      <div id="paperActionStatus" class="paper-action-status" hidden></div>
    </div>
    <div>${pdfBlock}</div>
    <div id="paperExtra" class="paper-extra" hidden>
      <section class="korean-summary">
        <div class="summary-head">
          <h2>한글 요약</h2>
          <div class="summary-actions">
            <button type="button" id="refreshKoreanSummary">다시 생성</button>
          </div>
        </div>
        <div id="koreanSummary" class="reader summary-reader">Info를 누르면 한글 요약을 불러옵니다.</div>
      </section>
      <details>
        <summary>Classification</summary>
        <p>${escapeHtml(paper.rationale || "No classification.")}</p>
      </details>
      <details>
        <summary>Abstract</summary>
        <p>${escapeHtml(paper.abstract)}</p>
      </details>
      <details>
        <summary>Wiki Page</summary>
        <div class="reader">${data.wiki_html || "<p>No wiki page. Check the paper status and rebuild after the LLM quota recovers.</p>"}</div>
      </details>
    </div>
  `;
  mountContinuousPdfViewer("#paperPdfViewer");
  $("#togglePaperInfo").addEventListener("click", () => {
    const extra = $("#paperExtra");
    extra.hidden = !extra.hidden;
    if (!extra.hidden) loadKoreanSummary(arxivId);
  });
  $("#refreshKoreanSummary").addEventListener("click", () => loadKoreanSummary(arxivId, true));
  if (hasWiki) {
    $("#openPaperWiki").addEventListener("click", () => {
      setView("wiki");
      loadWikiPage(paper.wiki_path || paperWikiPath(arxivId));
    });
  }
  $("#chatAboutPaper").addEventListener("click", () => startPaperChat(paper));
  if (canReject) $("#deletePaper").addEventListener("click", () => paperAction(arxivId, "delete"));
  wireReaderLinks($("#paperDetail"), paperWikiPath(arxivId));
  typesetMath($("#paperDetail"));
  if (options.updateHistory !== false) updateHistory({ view: "papers", paper: arxivId });
}

async function openPaperDetailById(arxivId) {
  if (!arxivId) return;
  setView("papers");
  await loadPaperDetail(arxivId);
}

async function paperAction(arxivId, action) {
  const status = $("#paperActionStatus");
  status.hidden = false;
  if (action === "delete") {
    const confirmed = window.confirm(
      `Delete ${arxivId} and remove its PDF, extracted text, wiki page, summaries, graph links, and wiki references?`
    );
    if (!confirmed) {
      status.hidden = true;
      return;
    }
  }
  const messages = {
    select: "Selecting paper...",
    process_to_graph: "Processing this paper to graph...",
    reject: "Rejecting paper and removing local artifacts...",
    delete: "Deleting paper and removing local artifacts...",
  };
  status.textContent = messages[action] || "Running paper action...";
  try {
    const data = await api("/api/paper-action", {
      method: "POST",
      body: JSON.stringify({ arxiv_id: arxivId, action }),
    });
    if (data.ok && action === "delete") {
      const removed = data.removed?.length || 0;
      const pruned = data.pruned?.length || 0;
      $("#paperDetail").classList.add("empty");
      $("#paperDetail").innerHTML = `Deleted ${escapeHtml(arxivId)}. Removed ${removed} artifact(s), pruned ${pruned} wiki page(s).`;
      await loadPapers();
      await loadDashboard();
      await loadWikiList();
      return;
    } else {
      status.textContent = data.ok ? `Done. Current status: ${data.status || "updated"}` : data.output || "Action failed.";
    }
    await loadPapers();
    await loadDashboard();
    await loadPaperDetail(arxivId);
  } catch (error) {
    status.textContent = error.message;
  }
}

async function loadKoreanSummary(arxivId, refresh = false, targetSelector = "#koreanSummary") {
  const target = $(targetSelector);
  if (!target) return;
  const loadedKey = `ko:${arxivId}`;
  if (!refresh && target.dataset.loaded === loadedKey) return;
  target.dataset.loaded = "";
  target.hidden = false;
  const requestToken = `${loadedKey}:${Date.now()}`;
  target.dataset.requestToken = requestToken;
  target.textContent = refresh ? "한글 요약을 다시 생성하는 중입니다..." : "한글 요약을 불러오는 중입니다...";
  try {
    const query = new URLSearchParams({ id: arxivId });
    if (refresh) query.set("refresh", "1");
    const data = await api(`/api/paper-summary?${query.toString()}`, { timeoutMs: 240000 });
    if (target.dataset.requestToken !== requestToken) return;
    target.innerHTML = data.html || "<p>요약이 없습니다.</p>";
    target.dataset.loaded = loadedKey;
    wireReaderLinks(target, paperWikiPath(arxivId));
    typesetMath(target);
  } catch (error) {
    if (target.dataset.requestToken !== requestToken) return;
    target.textContent = error.message;
  }
}

async function loadWikiList() {
  $("#wikiList").innerHTML = `<div class="side-row">Loading wiki pages...</div>`;
  const data = await api("/api/wiki-list", { timeoutMs: 15000 });
  state.wikiPages = data.pages;
  $("#wikiList").innerHTML = `
    <div class="wiki-tools">
      <div>
        <h2>Wiki Files</h2>
        <div class="item-meta">${data.pages.length} markdown files</div>
      </div>
      <div class="obsidian-export-actions" aria-label="Obsidian export">
        <button id="chooseObsidianFolder" type="button">Choose Folder</button>
        <button id="exportObsidian" type="button">Export ZIP</button>
      </div>
      <div id="obsidianExportStatus" class="item-meta export-status">${state.obsidianExportDir ? `Folder: ${escapeHtml(state.obsidianExportDir)}` : "No export folder selected"}</div>
      <input id="wikiSearch" type="search" placeholder="Filter wiki files" aria-label="Filter wiki files">
    </div>
    <div id="wikiTree" class="wiki-tree"></div>
  `;
  $("#chooseObsidianFolder").addEventListener("click", chooseObsidianFolder);
  $("#exportObsidian").addEventListener("click", exportObsidianVault);
  $("#wikiSearch").addEventListener("input", (event) => {
    state.wikiFilter = event.target.value.trim().toLowerCase();
    renderWikiTree();
  });
  renderWikiTree();
}

async function chooseObsidianFolder() {
  const button = $("#chooseObsidianFolder");
  const status = $("#obsidianExportStatus");
  button.disabled = true;
  status.textContent = "Opening folder picker...";
  try {
    if (window.pywebview?.api?.choose_folder) {
      const selected = await window.pywebview.api.choose_folder();
      if (!selected) {
        status.textContent = state.obsidianExportDir ? `Folder: ${state.obsidianExportDir}` : "Folder selection cancelled.";
        return;
      }
      state.obsidianExportDir = selected;
      status.textContent = `Folder: ${selected}`;
      return;
    }
    const selected = window.prompt("Export folder path. Leave blank to use the default app export folder.", state.obsidianExportDir || "");
    if (selected === null) {
      status.textContent = state.obsidianExportDir ? `Folder: ${state.obsidianExportDir}` : "Folder selection cancelled.";
      return;
    }
    state.obsidianExportDir = selected.trim();
    status.textContent = state.obsidianExportDir ? `Folder: ${state.obsidianExportDir}` : "Default export folder selected.";
  } catch (error) {
    status.textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

async function exportObsidianVault() {
  const button = $("#exportObsidian");
  const status = $("#obsidianExportStatus");
  const outputDir = state.obsidianExportDir;
  button.disabled = true;
  status.textContent = outputDir ? "Saving Obsidian vault ZIP to selected folder..." : "Creating Obsidian vault ZIP in the default folder...";
  try {
    const data = await api("/api/obsidian-export", {
      method: "POST",
      body: JSON.stringify({ output_dir: outputDir }),
      timeoutMs: 120000,
    });
    status.innerHTML = `
      Exported ${escapeHtml(data.count)} wiki file${data.count === 1 ? "" : "s"}.<br>
      Saved: ${escapeHtml(data.absolute_path || data.path)}
    `;
  } catch (error) {
    status.textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

function wikiTreeParts(pageOrPath) {
  const page = typeof pageOrPath === "string" ? { path: pageOrPath } : pageOrPath;
  const path = page.path;
  const parts = path.split("/");
  if (parts[0] === "wiki") parts.shift();
  const fileName = parts.pop() || "";
  if (parts[0] === "papers") parts.splice(1, 0, page.topic || "Unclassified");
  if (parts[0] === "daily") {
    const match = fileName.match(/^(\d{4})-\d{2}-\d{2}/);
    if (match) parts.splice(1, 0, match[1]);
  }
  return { folders: parts, fileName };
}

function emptyWikiNode(name, path) {
  return { name, path, folders: new Map(), pages: [], count: 0 };
}

function buildWikiTree(pages) {
  const root = emptyWikiNode("wiki", "wiki");
  pages.forEach((page) => {
    const { folders } = wikiTreeParts(page);
    let node = root;
    node.count += 1;
    folders.forEach((folder) => {
      const folderPath = `${node.path}/${folder}`;
      if (!node.folders.has(folder)) node.folders.set(folder, emptyWikiNode(folder, folderPath));
      node = node.folders.get(folder);
      node.count += 1;
    });
    node.pages.push(page);
  });
  return root;
}

function sortWikiFolders(node) {
  const folders = Array.from(node.folders.values());
  if (node.path === "wiki") {
    const order = ["papers", "document", "daily", "topics", "interests", "methods", "surveys", "simulations", "concepts", "entities", "proposals"];
    return folders.sort((a, b) => {
      const ai = order.indexOf(a.name);
      const bi = order.indexOf(b.name);
      if (ai !== -1 || bi !== -1) return (ai === -1 ? 999 : ai) - (bi === -1 ? 999 : bi);
      return a.name.localeCompare(b.name);
    });
  }
  if (node.path === "wiki/daily") {
    return folders.sort((a, b) => b.name.localeCompare(a.name));
  }
  return folders.sort((a, b) => a.name.localeCompare(b.name));
}

function wikiFolderLabel(node) {
  const labels = {
    daily: "Daily",
    papers: "Papers",
    document: "Document",
    topics: "Topics",
    interests: "Interests",
    methods: "Methods",
    surveys: "Surveys",
    simulations: "Simulations",
    concepts: "Concepts",
    entities: "Entities",
    proposals: "Proposals",
  };
  return labels[node.name] || node.name;
}

function wikiPageLabel(page) {
  return page.path.split("/").pop().replace(/\.md$/, "");
}

function numericParts(value) {
  return (value.match(/\d+/g) || []).map((part) => Number(part));
}

function compareNumericPartsDesc(aParts, bParts) {
  const size = Math.max(aParts.length, bParts.length);
  for (let index = 0; index < size; index += 1) {
    const diff = (bParts[index] || 0) - (aParts[index] || 0);
    if (diff !== 0) return diff;
  }
  return 0;
}

function compareWikiPages(a, b, nodePath) {
  if (nodePath.startsWith("wiki/daily")) {
    const dateCompare = compareNumericPartsDesc(numericParts(a.path), numericParts(b.path));
    if (dateCompare !== 0) return dateCompare;
  }
  if (nodePath.startsWith("wiki/papers")) {
    const arxivCompare = compareNumericPartsDesc(numericParts(wikiPageLabel(a)), numericParts(wikiPageLabel(b)));
    if (arxivCompare !== 0) return arxivCompare;
  }
  return a.path.localeCompare(b.path);
}

function isWikiFolderOpen(node) {
  if (state.wikiFilter) return true;
  return state.wikiOpenFolders.has(node.path);
}

function openWikiAncestors(path) {
  const page = state.wikiPages.find((item) => item.path === path);
  const { folders } = wikiTreeParts(page || path);
  let current = "wiki";
  folders.forEach((folder) => {
    current = `${current}/${folder}`;
    state.wikiOpenFolders.add(current);
  });
}

function renderWikiNode(node, depth = 0) {
  const pages = node.pages
    .slice()
    .sort((a, b) => compareWikiPages(a, b, node.path))
    .map((page) => `
      <button class="wiki-page-row${page.path === state.currentWikiPath ? " active" : ""}" style="--depth: ${depth}" data-wiki="${escapeHtml(page.path)}" title="${escapeHtml(page.path)}">
        <span class="wiki-page-name">${escapeHtml(wikiPageLabel(page))}</span>
      </button>
    `)
    .join("");
  const folders = sortWikiFolders(node).map((folder) => {
    const open = isWikiFolderOpen(folder) ? " open" : "";
    return `
      <details class="wiki-folder" data-folder="${escapeHtml(folder.path)}"${open}>
        <summary class="wiki-folder-row" style="--depth: ${depth}">
          <span class="wiki-folder-name">${escapeHtml(wikiFolderLabel(folder))}</span>
          <span class="wiki-count">${folder.count}</span>
        </summary>
        <div class="wiki-folder-children">
          ${renderWikiNode(folder, depth + 1)}
        </div>
      </details>
    `;
  }).join("");
  return pages + folders;
}

function renderWikiTree() {
  const tree = $("#wikiTree");
  if (!tree) return;
  const query = state.wikiFilter;
  const pages = query
    ? state.wikiPages.filter((page) => page.path.toLowerCase().includes(query) || (page.title || "").toLowerCase().includes(query))
    : state.wikiPages;
  if (!pages.length) {
    tree.innerHTML = `<div class="wiki-empty">No wiki files match.</div>`;
    return;
  }
  tree.innerHTML = renderWikiNode(buildWikiTree(pages), 0);
  tree.querySelectorAll("[data-wiki]").forEach((row) => {
    row.addEventListener("click", () => loadWikiPage(row.dataset.wiki));
  });
  tree.querySelectorAll("details[data-folder]").forEach((folder) => {
    folder.addEventListener("toggle", () => {
      if (state.wikiFilter) return;
      if (folder.open) state.wikiOpenFolders.add(folder.dataset.folder);
      else state.wikiOpenFolders.delete(folder.dataset.folder);
    });
  });
  const active = tree.querySelector(".wiki-page-row.active");
  if (active) active.scrollIntoView({ block: "nearest" });
}

async function loadWikiPage(path, options = {}) {
  const anchor = options.anchor || linkAnchor(path);
  path = stripLinkAnchor(path);
  let data;
  try {
    data = await api(`/api/wiki?path=${encodeURIComponent(path)}`);
  } catch (error) {
    const isPaperWiki = /^wiki\/papers\/[^/]+\.md$/.test(path) && !path.endsWith("-deep-summary.md");
    if (isPaperWiki && path !== "wiki/log.md") {
      window.alert(`Paper wiki page가 없습니다. Wiki log로 이동합니다.\n\n${path}`);
      return loadWikiPage("wiki/log.md", options);
    }
    throw error;
  }
  if (options.keepSub !== true) closeWikiSubReader();
  state.currentWikiPath = path;
  openWikiAncestors(path);
  renderWikiTree();
  $("#wikiReader").classList.remove("empty");
  const proposalActions = path.startsWith("wiki/proposals/")
    ? `
      <div class="proposal-actions">
        <div>
          <div class="context-label">Wiki Update Proposal</div>
          <div class="item-meta">검토 후 승인하면 제안 내용이 대상 위키 페이지에 추가됩니다.</div>
        </div>
        <button type="button" id="applyWikiProposal">Apply Proposal</button>
        <div id="proposalApplyStatus" class="proposal-status" hidden></div>
      </div>
    `
    : "";
  const paperActions = /^wiki\/papers\/[^/]+\.md$/.test(path) && !path.endsWith("-deep-summary.md")
    ? `
      <div class="proposal-actions">
        <div>
          <div class="context-label">Paper Wiki</div>
          <div class="item-meta">${escapeHtml(path)}</div>
        </div>
        <button type="button" id="deleteWikiPaper" class="danger">Delete</button>
        <div id="wikiPaperDeleteStatus" class="proposal-status" hidden></div>
      </div>
    `
    : "";
  $("#wikiReader").innerHTML = `${proposalActions}${paperActions}${data.html}`;
  if (path.startsWith("wiki/proposals/")) {
    $("#applyWikiProposal").addEventListener("click", () => applyWikiProposal(path));
  }
  if ($("#deleteWikiPaper")) {
    $("#deleteWikiPaper").addEventListener("click", () => deletePaperFromWiki(path));
  }
  wireReaderLinks($("#wikiReader"), path);
  typesetMath($("#wikiReader"));
  if (anchor) scrollReaderToAnchor(anchor, $("#wikiReader"));
  if (options.updateHistory !== false) updateHistory({ view: "wiki", wiki: path });
}

async function deletePaperFromWiki(path) {
  const status = $("#wikiPaperDeleteStatus");
  const confirmed = window.confirm(`Delete this paper and remove its PDF, extracted text, summaries, graph links, and wiki references?\n\n${path}`);
  if (!confirmed) return;
  status.hidden = false;
  status.textContent = "Deleting paper...";
  try {
    const data = await api("/api/paper-action", {
      method: "POST",
      body: JSON.stringify({ action: "delete", wiki_path: path }),
    });
    const removed = data.removed?.length || 0;
    const pruned = data.pruned?.length || 0;
    $("#wikiReader").classList.add("empty");
    $("#wikiReader").textContent = `Paper deleted. Removed ${removed} artifact(s), pruned ${pruned} wiki page(s).`;
    await loadWikiList();
    await loadPapers();
    await loadDashboard();
  } catch (error) {
    status.textContent = error.message;
  }
}

async function applyWikiProposal(path) {
  const button = $("#applyWikiProposal");
  const status = $("#proposalApplyStatus");
  button.disabled = true;
  button.textContent = "Applying...";
  status.hidden = false;
  status.textContent = "Applying proposal to wiki...";
  try {
    const data = await api("/api/apply-wiki-proposal", {
      method: "POST",
      body: JSON.stringify({ path }),
    });
    status.textContent = data.already_applied
      ? `Already applied to ${data.target_path}.`
      : `Applied to ${data.target_path}${data.graph_refreshed ? " and graph refreshed" : ""}.`;
    await loadWikiList();
    await loadReviewQueue();
    loadWikiPage(data.target_path);
  } catch (error) {
    button.disabled = false;
    button.textContent = "Apply Proposal";
    status.textContent = error.message;
  }
}

async function loadReviewQueue() {
  $("#proposalQueue").innerHTML = `<div class="item empty">Loading review queue...</div>`;
  const data = await api("/api/review-queue", { timeoutMs: 15000 });
  $("#proposalQueue").innerHTML = data.proposals.map((proposal) => `
    <div class="item proposal-row" data-wiki="${escapeHtml(proposal.path)}">
      <div class="item-title">${escapeHtml(proposal.title)}</div>
      <div class="item-meta">
        <span class="badge ${proposal.status === "applied" ? "graphed" : ""}">${escapeHtml(proposal.status)}</span>
        ${escapeHtml(proposal.path)}
      </div>
      <div class="item-meta">Target: ${escapeHtml(proposal.target_path || "manual review required")}</div>
    </div>
  `).join("") || `<div class="item empty">No proposals.</div>`;
  $("#proposalQueue").querySelectorAll("[data-wiki]").forEach((row) => {
    row.addEventListener("click", () => {
      setView("wiki");
      loadWikiPage(row.dataset.wiki);
    });
  });
  renderLintReport(data.lint);
}

function renderLintReport(lint) {
  const target = $("#lintReport");
  if (!target) return;
  target.classList.remove("empty");
  target.innerHTML = lint?.html || "<p>No lint report has been generated.</p>";
  if (lint?.path) {
    target.insertAdjacentHTML("afterbegin", `<div class="item-meta">Report: ${escapeHtml(lint.path)}</div>`);
  }
  wireReaderLinks(target, lint?.path || "reports/wiki-lint.md");
  typesetMath(target);
}

function stripLinkAnchor(path) {
  return String(path || "").split("#", 1)[0];
}

function linkAnchor(path) {
  const parts = String(path || "").split("#");
  return parts.length > 1 ? decodeURIComponent(parts.slice(1).join("#")) : "";
}

function cssEscape(value) {
  if (window.CSS?.escape) return CSS.escape(value);
  return String(value || "").replace(/["\\]/g, "\\$&");
}

function scrollReaderToAnchor(anchor, root = document) {
  requestAnimationFrame(() => {
    const target = root === document
      ? document.getElementById(anchor) || document.querySelector(`#${cssEscape(anchor)}`)
      : root.querySelector(`#${cssEscape(anchor)}`);
    if (!target) return;
    target.scrollIntoView({ block: "start", behavior: "smooth" });
    target.classList.add("source-anchor-hit");
    setTimeout(() => target.classList.remove("source-anchor-hit"), 1800);
  });
}

function isPaperWikiPath(path) {
  const clean = stripLinkAnchor(path);
  return /^wiki\/papers\/[^/]+\.md$/.test(clean) && !clean.endsWith("-deep-summary.md");
}

function examplePaperUrlToPath(target) {
  const embedded = String(target || "").match(/(?:^|\/|\.\.\/)papers\/([^/\s?#]+)\.md/);
  if (embedded && !embedded[1].endsWith("-deep-summary")) return `wiki/papers/${embedded[1]}.md`;
  try {
    const url = new URL(target);
    const match = url.hostname === "example.com" ? url.pathname.match(/^\/papers\/([^/]+)\.md$/) : null;
    return match ? `wiki/papers/${match[1]}.md` : "";
  } catch {
    return "";
  }
}

function hasLinkModifier(event) {
  return event.metaKey || event.ctrlKey || event.shiftKey || event.altKey;
}

function citationTargetForLink(basePath, target) {
  if (!target) return "";
  if (target.startsWith("http")) return examplePaperUrlToPath(target);
  if (target.startsWith("/") || target.startsWith("?")) return "";
  const resolved = resolveWikiLink(basePath, target);
  return isPaperWikiPath(resolved) ? stripLinkAnchor(resolved) : "";
}

function isInternalMarkdownPath(path) {
  const clean = stripLinkAnchor(path);
  return /^(wiki|data\/markdown)\//.test(clean) && /\.md$/.test(clean);
}

function nearestHeadingText(node) {
  let current = node;
  while (current && current !== document.body) {
    let sibling = current.previousElementSibling;
    while (sibling) {
      if (/^H[1-3]$/.test(sibling.tagName)) return sibling.textContent.trim();
      const nested = sibling.querySelector?.("h1, h2, h3:last-of-type");
      if (nested) return nested.textContent.trim();
      sibling = sibling.previousElementSibling;
    }
    current = current.parentElement;
  }
  return "";
}

function citationContextForLink(link) {
  const block = link.closest("li, p, blockquote, tr, .item") || link.parentElement;
  const heading = nearestHeadingText(block || link);
  const text = block?.textContent?.trim() || link.textContent || "";
  return [heading, text].filter(Boolean).join("\n").slice(0, 1600);
}

function wikiLinkTitle(path) {
  const clean = stripLinkAnchor(path);
  return clean.split("/").pop() || clean || "Source";
}

function closeWikiSubReader() {
  const panel = $("#wikiSubReader");
  if (!panel) return;
  panel.hidden = true;
  panel.innerHTML = "";
  panel.closest(".wiki-layout")?.classList.remove("sub-open");
}

function renderWikiSubShell(path, content) {
  let panel = $("#wikiSubReader");
  if (!panel) {
    const layout = document.querySelector(".wiki-layout");
    if (!layout) return null;
    panel = document.createElement("aside");
    panel.id = "wikiSubReader";
    panel.className = "wiki-sub-reader";
    layout.appendChild(panel);
  }
  panel.hidden = false;
  panel.closest(".wiki-layout")?.classList.add("sub-open");
  panel.innerHTML = `
    <div class="wiki-sub-head">
      <div>
        <div class="context-label">Source Preview</div>
        <div class="wiki-sub-title">${escapeHtml(wikiLinkTitle(path))}</div>
        <div class="wiki-sub-path">${escapeHtml(path)}</div>
      </div>
      <div class="wiki-sub-actions">
        <button type="button" id="wikiSubClose" aria-label="Close source preview">&times;</button>
      </div>
    </div>
    <div class="wiki-sub-body">${content}</div>
  `;
  $("#wikiSubClose")?.addEventListener("click", closeWikiSubReader);
  return panel.querySelector(".wiki-sub-body");
}

async function openWikiSubReader(path, anchor = "") {
  const targetPath = stripLinkAnchor(path);
  const targetAnchor = anchor || linkAnchor(path);
  const body = renderWikiSubShell(targetPath, `<div class="trace-muted">Loading source...</div>`);
  try {
    const data = await api(`/api/wiki?path=${encodeURIComponent(targetPath)}`);
    const content = renderWikiSubShell(targetPath, data.html || "<p>No content.</p>");
    if (!content) return;
    wireReaderLinks(content, targetPath);
    typesetMath(content);
    if (targetAnchor) scrollReaderToAnchor(targetAnchor, content);
  } catch (error) {
    if (body) {
      body.innerHTML = `<div class="trace-error">${escapeHtml(error.message || String(error))}</div>`;
    } else {
      renderWikiSubShell(targetPath, `<div class="trace-error">${escapeHtml(error.message || String(error))}</div>`);
    }
  }
}

function closeCitationTrace() {
  const panel = $("#citationTrace");
  if (!panel) return;
  panel.hidden = true;
  panel.classList.remove("open");
  panel.innerHTML = "";
}

function renderCitationTraceShell(content) {
  const panel = $("#citationTrace");
  if (!panel) return;
  panel.hidden = false;
  panel.classList.add("open");
  panel.innerHTML = content;
  panel.querySelector("[data-close-trace]")?.addEventListener("click", closeCitationTrace);
  typesetMath(panel);
}

function renderCitationTraceLoading() {
  renderCitationTraceShell(`
    <div class="citation-head">
      <div>
        <div class="context-label">Citation Trace</div>
        <h2>Loading source</h2>
      </div>
      <button type="button" class="trace-close" data-close-trace aria-label="Close">&times;</button>
    </div>
    <div class="citation-body">
      <div class="trace-muted">Resolving paper evidence...</div>
    </div>
  `);
}

function renderCitationTraceError(error) {
  const message = error.message || String(error);
  const hint = message === "Not found"
    ? "The UI server is older than the loaded app.js. Restart the UI server and refresh this browser tab."
    : message;
  renderCitationTraceShell(`
    <div class="citation-head">
      <div>
        <div class="context-label">Citation Trace</div>
        <h2>Trace failed</h2>
      </div>
      <button type="button" class="trace-close" data-close-trace aria-label="Close">&times;</button>
    </div>
    <div class="citation-body">
      <div class="trace-error">${escapeHtml(hint)}</div>
    </div>
  `);
}

function renderCitationTrace(data) {
  const pdfUrl = data.pdf_path ? `/pdf?path=${encodeURIComponent(data.pdf_path)}` : "";
  const evidence = (data.evidence_items || []).map((item) => `
    <li>
      <span>${escapeHtml(item.label)}</span>
      <p>${escapeHtml(item.text)}</p>
    </li>
  `).join("");
  renderCitationTraceShell(`
    <div class="citation-head">
      <div>
        <div class="context-label">Citation Trace</div>
        <h2>${escapeHtml(data.arxiv_id)}</h2>
      </div>
      <button type="button" class="trace-close" data-close-trace aria-label="Close">&times;</button>
    </div>
    <div class="citation-body">
      <section class="trace-paper">
        <h3>${escapeHtml(data.title || data.arxiv_id)}</h3>
        <div class="trace-meta">
          ${escapeHtml([data.authors, data.topic].filter(Boolean).join(" · "))}
        </div>
      </section>
      <section class="trace-section">
        <div class="trace-section-head">
          <span class="context-label">${escapeHtml(data.section_source === "ingest_cache" ? "Section Evidence" : "Wiki Evidence")}</span>
          <span class="trace-chip">${escapeHtml(data.section_label || "paper wiki")}</span>
        </div>
        <blockquote>${escapeHtml(data.excerpt || "No excerpt available.")}</blockquote>
      </section>
      ${evidence ? `<section class="trace-section"><ul class="trace-evidence">${evidence}</ul></section>` : ""}
      ${data.source_context ? `
        <details class="trace-context">
          <summary>Source context</summary>
          <p>${escapeHtml(data.source_context)}</p>
        </details>
      ` : ""}
      <div class="trace-actions">
        <button type="button" id="traceOpenWiki">Open paper wiki</button>
        <button type="button" id="traceChatPaper">Chat</button>
        ${String(data.abs_url || "").startsWith("http") ? `<a href="${escapeHtml(data.abs_url)}" target="_blank" rel="noreferrer">${escapeHtml(sourceLinkLabel(data))}</a>` : ""}
        ${pdfUrl ? `<a href="${escapeHtml(pdfUrl)}" target="_blank" rel="noreferrer">PDF</a>` : ""}
      </div>
    </div>
  `);
  $("#traceOpenWiki")?.addEventListener("click", () => {
    closeCitationTrace();
    setView("wiki");
    loadWikiPage(data.target_path);
  });
  $("#traceChatPaper")?.addEventListener("click", () => openPaperChatById(data.arxiv_id));
}

async function openCitationTrace(basePath, target, link) {
  renderCitationTraceLoading();
  try {
    const data = await api("/api/citation-trace", {
      method: "POST",
      body: JSON.stringify({
        base_path: basePath,
        target,
        context: citationContextForLink(link),
      }),
      timeoutMs: 15000,
    });
    renderCitationTrace(data);
  } catch (error) {
    renderCitationTraceError(error);
  }
}

async function runLintNow() {
  const target = $("#lintReport");
  target.textContent = "Running wiki lint...";
  try {
    const data = await api("/api/run-lint", { method: "POST", body: JSON.stringify({}) });
    renderLintReport(data.lint);
  } catch (error) {
    target.textContent = error.message;
  }
}

function wireReaderLinks(container, basePath) {
  container.querySelectorAll("[data-link]").forEach((link) => {
    const target = link.dataset.link;
    const traceTarget = citationTargetForLink(basePath, target);
    if (traceTarget) {
      link.classList.add("citation-link");
      link.title = "Open citation trace";
    } else {
      const resolved = target.startsWith("http") || target.startsWith("/") || target.startsWith("?")
        ? ""
        : resolveWikiLink(basePath, target);
      if (isInternalMarkdownPath(resolved)) {
        link.classList.add("source-preview-link");
        link.title = "Open source preview";
      }
    }
    link.addEventListener("click", (event) => {
      event.preventDefault();
      if (!target) return;
      if (target.startsWith("http")) {
        const paperTarget = examplePaperUrlToPath(target);
        if (paperTarget && !hasLinkModifier(event)) {
          openCitationTrace(basePath, paperTarget, link);
          return;
        }
        window.open(target, "_blank", "noreferrer");
        return;
      }
      if (target.startsWith("/") || target.startsWith("?")) {
        const url = new URL(target, window.location.origin);
        window.open(url.href, "_blank", "noreferrer");
        return;
      }
      const resolved = resolveWikiLink(basePath, target);
      if (isPaperWikiPath(resolved) && !hasLinkModifier(event)) {
        openCitationTrace(basePath, stripLinkAnchor(resolved), link);
        return;
      }
      openWikiSubReader(resolved, linkAnchor(resolved) || linkAnchor(target));
    });
  });
}

function resolveWikiLink(basePath, link) {
  const clean = stripLinkAnchor(link);
  if (clean.startsWith("wiki/") || clean.startsWith("data/")) return clean;
  const stack = (basePath || "wiki/index.md").split("/").slice(0, -1);
  for (const part of clean.split("/")) {
    if (!part || part === ".") continue;
    if (part === "..") stack.pop();
    else stack.push(part);
  }
  return stack.join("/");
}

function ensureChatComposerVisible() {
  const form = $("#chatForm");
  const log = $("#chatLog");
  if (!form || !log) return;
  log.scrollTop = log.scrollHeight;
}

async function askChat(question) {
  appendMessage("user", question);
  const pending = appendMessage("assistant", "Working...");
  ensureChatComposerVisible();
  const body = pending.querySelector(".message-body");
  const paperId = state.chatPaper?.arxiv_id || "";
  const model = $("#chatModel").value || state.chatModel || "";
  try {
    const data = await api("/api/chat", {
      method: "POST",
      body: JSON.stringify({ question, paper_id: paperId, model }),
      timeoutMs: 600000,
    });
    const modelLabel = data.model ? `<div class="chat-model-label">${escapeHtml(data.model)}</div>` : "";
    body.innerHTML = `${modelLabel}${data.answer_html || escapeHtml(data.answer || "")}`;
    wireReaderLinks(body, paperId ? paperWikiPath(paperId) : "wiki/index.md");
    typesetMath(body);
    addChatTurnActions(pending, question, data.answer, data.sources || []);
  } catch (error) {
    body.textContent = error.message;
  } finally {
    ensureChatComposerVisible();
    $("#chatQuestion").focus();
  }
}

function chatContextKeyForPaper(paper) {
  return paper?.arxiv_id ? `paper:${paper.arxiv_id}` : "general";
}

function resetChatLog(message = "") {
  const log = $("#chatLog");
  if (!log) return;
  log.innerHTML = "";
  if (message) appendMessage("assistant", message);
}

function switchChatContext(nextKey, message = "") {
  if (state.chatContextKey !== nextKey) {
    state.chatContextKey = nextKey;
    resetChatLog(message);
  }
}

function addChatTurnActions(messageNode, question, answer, sources) {
  const actions = document.createElement("div");
  actions.className = "message-actions";
  const saveButton = document.createElement("button");
  saveButton.type = "button";
  saveButton.textContent = "Save Q&A";
  saveButton.addEventListener("click", () => saveChatTurn(question, answer, sources, saveButton));
  const proposeButton = document.createElement("button");
  proposeButton.type = "button";
  proposeButton.textContent = "Propose Wiki Update";
  proposeButton.addEventListener("click", () => proposeWikiUpdate(question, answer, sources, proposeButton));
  actions.append(saveButton, proposeButton);
  messageNode.appendChild(actions);
}

async function saveChatTurn(question, answer, sources, button) {
  button.disabled = true;
  button.textContent = "Saving...";
  try {
    await api("/api/save-chat", {
      method: "POST",
      body: JSON.stringify({ question, answer, sources }),
    });
    button.textContent = "Saved";
  } catch (error) {
    button.disabled = false;
    button.textContent = "Save failed";
    button.title = error.message;
  }
}

async function proposeWikiUpdate(question, answer, sources, button) {
  button.disabled = true;
  button.textContent = "Drafting...";
  try {
    const data = await api("/api/propose-wiki-update", {
      method: "POST",
      body: JSON.stringify({ question, answer, sources }),
    });
    button.textContent = "Proposal created";
    await loadWikiList();
    setView("wiki");
    loadWikiPage(data.path);
  } catch (error) {
    button.disabled = false;
    button.textContent = "Proposal failed";
    button.title = error.message;
  }
}

function startPaperChat(paper, options = {}) {
  switchChatContext(
    chatContextKeyForPaper(paper),
    `${paper.arxiv_id} paper chat을 시작합니다.`
  );
  state.chatPaper = paper;
  setView("chat");
  renderChatContext();
  renderChatPaperWorkspace();
  loadKoreanSummary(paper.arxiv_id, false, "#chatKoreanSummary");
  const placeholder = `${paper.arxiv_id} 논문에 대해 질문하세요`;
  $("#chatQuestion").placeholder = placeholder;
  $("#chatQuestion").focus();
  if (options.updateHistory !== false) updateHistory({ view: "chat", chatPaper: paper.arxiv_id });
}

function clearPaperChat() {
  switchChatContext("general", "General wiki chat을 시작합니다.");
  state.chatPaper = null;
  renderChatContext();
  renderChatPaperWorkspace();
  $("#chatQuestion").placeholder = "Ask the wiki";
  updateHistory({ view: "chat" });
}

function renderChatPaperWorkspace() {
  const paper = state.chatPaper;
  const workspace = $("#chatWorkspace");
  const pane = $("#chatPaperPane");
  const pdfSlot = $("#chatPdfSlot");
  const summary = $("#chatKoreanSummary");
  workspace.classList.toggle("paper-active", Boolean(paper));
  if (!paper) {
    pane.hidden = true;
    pdfSlot.innerHTML = "";
    summary.hidden = true;
    summary.innerHTML = "";
    summary.dataset.loaded = "";
    return;
  }
  pane.hidden = false;
  pdfSlot.innerHTML = pdfViewerMarkup("chatPdfViewer", paper);
  mountContinuousPdfViewer("#chatPdfViewer");
}

function renderChatContext() {
  const box = $("#chatContext");
  const paper = state.chatPaper;
  if (!paper) {
    box.hidden = true;
    box.innerHTML = "";
    renderChatPaperWorkspace();
    return;
  }
  box.hidden = false;
  box.innerHTML = `
    <div>
      <div class="context-label">Selected Paper Chat</div>
      <div class="context-title">${escapeHtml(paper.title)}</div>
      <div class="item-meta">Paper ID: ${escapeHtml(paper.arxiv_id)} · ${escapeHtml(paper.topic || "unclassified")} · graph-linked papers included</div>
    </div>
    <div class="chat-context-actions">
      <button type="button" id="loadChatKoreanSummary">한글 요약</button>
      <button type="button" id="clearChatPaper">Clear</button>
    </div>
  `;
  $("#loadChatKoreanSummary").addEventListener("click", () => loadKoreanSummary(paper.arxiv_id, false, "#chatKoreanSummary"));
  $("#clearChatPaper").addEventListener("click", clearPaperChat);
}

function appendMessage(role, text) {
  const node = document.createElement("div");
  node.className = `message ${role}`;
  const body = document.createElement("div");
  body.className = "message-body";
  body.textContent = text;
  node.appendChild(body);
  $("#chatLog").appendChild(node);
  $("#chatLog").scrollTop = $("#chatLog").scrollHeight;
  typesetMath(body);
  ensureChatComposerVisible();
  return node;
}

async function runCommand() {
  const body = {
    command: $("#runCommand").value,
    date: $("#runDate").value,
    limit: $("#runLimit").value,
    search_query: $("#runSearchQuery").value,
    no_llm: $("#runNoLlm").checked,
  };
  $("#runOutput").textContent = "Starting...\n";
  try {
    const data = await api("/api/run", { method: "POST", body: JSON.stringify(body) });
    $("#runOutput").textContent = data.output || "Done.";
    await loadDashboard();
    await loadPapers();
  } catch (error) {
    $("#runOutput").textContent = error.message;
  }
}

function uploadFileKind(file) {
  const name = String(file?.name || "").toLowerCase();
  if (name.endsWith(".md") || name.endsWith(".markdown")) return "markdown";
  if (name.endsWith(".pdf")) return "pdf";
  return "other";
}

function updateUploadCategoryForFiles() {
  const input = $("#uploadPdf");
  const category = $("#uploadCategories");
  const files = Array.from(input?.files || []);
  if (!files.length) return;
  const kinds = new Set(files.map(uploadFileKind));
  if (kinds.size === 1 && kinds.has("markdown")) {
    category.value = "document";
    category.placeholder = "";
  } else if (kinds.size === 1 && kinds.has("pdf")) {
    category.value = "paper";
    category.placeholder = "";
  } else {
    category.value = "";
    category.placeholder = "PDF: paper, Markdown: document";
  }
}

function createUploadJobId() {
  if (window.crypto?.randomUUID) return window.crypto.randomUUID();
  return `upload-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function setUploadOutput(text) {
  const output = $("#uploadOutput");
  if (output) output.textContent = text;
}

function renderProgressPanel(ids, progress = {}) {
  const panel = $(ids.panel);
  if (!panel) return;
  panel.hidden = false;
  const percent = Math.max(0, Math.min(100, Number(progress.percent || 0)));
  $(ids.stage).textContent = progress.stage || "Running";
  $(ids.message).textContent = progress.message || "Preparing...";
  $(ids.percent).textContent = `${Math.round(percent)}%`;
  $(ids.bar).style.width = `${percent}%`;
  const fileBits = [];
  if (progress.file_index && progress.total_files) fileBits.push(`File ${progress.file_index} of ${progress.total_files}`);
  if (progress.filename) fileBits.push(progress.filename);
  $(ids.file).textContent = fileBits.join(" - ");
  const log = Array.isArray(progress.log) ? progress.log.slice(-10) : [];
  $(ids.log).innerHTML = log.map((item) => `
    <div>
      <span>${escapeHtml(item.time || "")}</span>
      <span>${escapeHtml(item.stage || "")}</span>
      <span>${escapeHtml(item.message || "")}</span>
    </div>
  `).join("");
}

const uploadProgressIds = {
  panel: "#uploadProgress",
  stage: "#uploadProgressStage",
  message: "#uploadProgressMessage",
  percent: "#uploadProgressPercent",
  bar: "#uploadProgressBar",
  file: "#uploadProgressFile",
  log: "#uploadProgressLog",
};

const remoteBuildProgressIds = {
  panel: "#remoteBuildProgress",
  stage: "#remoteBuildProgressStage",
  message: "#remoteBuildProgressMessage",
  percent: "#remoteBuildProgressPercent",
  bar: "#remoteBuildProgressBar",
  file: "#remoteBuildProgressFile",
  log: "#remoteBuildProgressLog",
};

function renderUploadProgress(progress = {}) {
  renderProgressPanel(uploadProgressIds, progress);
}

function renderRemoteBuildProgress(progress = {}) {
  renderProgressPanel(remoteBuildProgressIds, progress);
}

function stopUploadProgressPolling() {
  if (state.uploadProgressTimer) {
    window.clearInterval(state.uploadProgressTimer);
    state.uploadProgressTimer = null;
  }
}

function startUploadProgressPolling(jobId) {
  stopUploadProgressPolling();
  state.uploadJobId = jobId;
  const poll = async () => {
    try {
      const progress = await api(`/api/upload-progress?id=${encodeURIComponent(jobId)}`, { timeoutMs: 5000 });
      if (state.uploadJobId === jobId) renderUploadProgress(progress);
      if (["completed", "completed_with_errors", "failed"].includes(progress.status)) stopUploadProgressPolling();
    } catch {
      // The upload request itself reports terminal errors. Polling can briefly miss while the server is busy.
    }
  };
  poll();
  state.uploadProgressTimer = window.setInterval(poll, 1000);
}

function stopRemoteBuildProgressPolling() {
  if (state.remoteBuildProgressTimer) {
    window.clearInterval(state.remoteBuildProgressTimer);
    state.remoteBuildProgressTimer = null;
  }
}

function startRemoteBuildProgressPolling(jobId) {
  stopRemoteBuildProgressPolling();
  state.remoteBuildJobId = jobId;
  const poll = async () => {
    try {
      const progress = await api(`/api/upload-progress?id=${encodeURIComponent(jobId)}`, { timeoutMs: 5000 });
      if (state.remoteBuildJobId === jobId) renderRemoteBuildProgress(progress);
      if (["completed", "completed_with_errors", "failed"].includes(progress.status)) stopRemoteBuildProgressPolling();
    } catch {
      // The long-running build request reports terminal errors. Polling may briefly miss while a stage starts.
    }
  };
  poll();
  state.remoteBuildProgressTimer = window.setInterval(poll, 1000);
}

async function uploadPaper(event) {
  event.preventDefault();
  const files = Array.from($("#uploadPdf").files || []);
  if (!files.length) {
    setUploadOutput("Select one or more PDF or Markdown files first.");
    return;
  }
  const form = new FormData($("#uploadForm"));
  const jobId = createUploadJobId();
  form.set("upload_job_id", jobId);
  form.set("ingest", "true");
  form.set("no_llm", "false");
  const submitButton = $("#uploadForm button[type='submit']");
  submitButton.disabled = true;
  renderUploadProgress({
    status: "running",
    stage: "Preparing upload",
    message: `Preparing ${files.length} file${files.length === 1 ? "" : "s"} for processing.`,
    percent: 0,
    total_files: files.length,
    log: [],
  });
  startUploadProgressPolling(jobId);
  setUploadOutput(`Processing ${files.length} upload${files.length === 1 ? "" : "s"}...\n`);
  try {
    const data = await api("/api/upload-paper", {
      method: "POST",
      body: form,
      timeoutMs: 3600000,
    });
    stopUploadProgressPolling();
    renderUploadProgress(data.upload_progress || {
      status: "completed",
      stage: "Complete",
      message: `Uploaded ${data.count || 1} file${(data.count || 1) === 1 ? "" : "s"}.`,
      percent: 100,
    });
    const items = data.items || data.papers || [data];
    const papers = data.papers || items.filter((item) => item.source_type !== "document");
    const documents = data.documents || items.filter((item) => item.source_type === "document");
    const lines = [`Uploaded ${data.count || items.length} file${(data.count || items.length) === 1 ? "" : "s"}.`];
    for (const item of items) {
      if (item.source_type === "document") {
        lines.push(
          "",
          `File: ${item.filename || "-"}`,
          `Document: ${item.title || "-"}`,
          `Categories: ${item.categories || "document"}`,
          `Wiki: ${item.wiki_path || "-"}`,
          item.graph_output || "Document wiki uploaded.",
        );
      } else {
        lines.push(
          "",
          `File: ${item.filename || "-"}`,
          `Paper ID: ${item.arxiv_id}`,
          `Title: ${item.title}`,
          `Authors: ${(item.authors || []).join(", ") || "-"}`,
          `Abstract: ${item.abstract || "-"}`,
          `Categories: ${item.categories || "-"}`,
          `PDF: ${item.pdf_path || "-"}`,
          `Markdown: ${item.markdown_path || "-"}`,
          `Text: ${item.text_path}`,
          `Wiki: ${item.wiki_path || "-"}`,
          `Korean summary: ${item.korean_summary_path || "-"}`,
          item.ingest_output || "Wiki page and Korean summary generated.",
        );
      }
    }
    if ((data.errors || []).length) {
      lines.push("", `Failed ${data.errors.length} file${data.errors.length === 1 ? "" : "s"}:`);
      for (const item of data.errors) lines.push(`- ${item.filename}: ${item.error}`);
      window.alert(`${data.errors.length} file${data.errors.length === 1 ? "" : "s"} failed. See progress details.`);
    }
    setUploadOutput(lines.join("\n"));
    await loadDashboard();
    await loadPapers();
    await loadWikiList();
    if (documents.length && documents[0].wiki_path) {
      setView("wiki");
      await loadWikiPage(documents[0].wiki_path);
    } else if (papers.length && papers[0].arxiv_id) {
      setView("papers");
      await loadPaperDetail(papers[0].arxiv_id);
    }
  } catch (error) {
    stopUploadProgressPolling();
    renderUploadProgress({
      status: "failed",
      stage: "Upload failed",
      message: error.message,
      percent: 100,
      log: [{ time: new Date().toLocaleTimeString(), stage: "Upload failed", message: error.message }],
    });
    setUploadOutput(error.message);
    window.alert(error.message);
  } finally {
    submitButton.disabled = false;
  }
}

async function chooseBatchFolder() {
  const button = $("#chooseBatchFolder");
  button.disabled = true;
  try {
    if (window.pywebview?.api?.choose_folder) {
      const selected = await window.pywebview.api.choose_folder();
      if (selected) $("#batchFolder").value = selected;
      return;
    }
    const selected = window.prompt("Batch PDF folder path", $("#batchFolder").value || "");
    if (selected !== null) $("#batchFolder").value = selected.trim();
  } catch (error) {
    setUploadOutput(error.message);
  } finally {
    button.disabled = false;
  }
}

function uploadBatchResultLines(data) {
  const items = data.items || data.papers || [];
  const skipped = data.skipped || [];
  const errors = data.errors || [];
  const lines = [
    `Batch folder: ${data.folder_path || "-"}`,
    `Mode: ${data.batch_mode || "-"}`,
    `Processed: ${data.count || items.length}`,
    `Skipped: ${data.skipped_count || skipped.length}`,
    `Failed: ${data.failed_count || errors.length}`,
  ];
  for (const item of items) {
    lines.push(
      "",
      `File: ${item.filename || "-"}`,
      `Paper ID: ${item.arxiv_id || "-"}`,
      `Title: ${item.title || "-"}`,
      `Wiki: ${item.wiki_path || "-"}`,
      item.ingest_output || "Wiki page and Korean summary generated.",
    );
  }
  if (skipped.length) {
    lines.push("", `Skipped ${skipped.length} unchanged file${skipped.length === 1 ? "" : "s"}:`);
    for (const item of skipped) lines.push(`- ${item.filename}${item.wiki_path ? ` -> ${item.wiki_path}` : ""}`);
  }
  if (errors.length) {
    lines.push("", `Failed ${errors.length} file${errors.length === 1 ? "" : "s"}:`);
    for (const item of errors) lines.push(`- ${item.filename}: ${item.error}`);
  }
  return lines;
}

async function runBatchUpload(event) {
  event.preventDefault();
  const folderPath = $("#batchFolder").value.trim();
  if (!folderPath) {
    setUploadOutput("Select a PDF folder first.");
    return;
  }
  const mode = $("#batchMode").value;
  if (mode === "reprocess") {
    const confirmed = window.confirm("Delete existing wiki artifacts for matching papers and reprocess every PDF in this folder?");
    if (!confirmed) return;
  }
  const jobId = createUploadJobId();
  const button = $("#batchUploadForm button[type='submit']");
  button.disabled = true;
  renderUploadProgress({
    status: "running",
    stage: "Preparing batch",
    message: "Preparing PDF batch processing.",
    percent: 0,
    log: [],
  });
  startUploadProgressPolling(jobId);
  setUploadOutput(`Batch processing folder:\n${folderPath}\n`);
  try {
    const data = await api("/api/upload-batch", {
      method: "POST",
      body: JSON.stringify({
        upload_job_id: jobId,
        folder_path: folderPath,
        mode,
        categories: $("#batchCategories").value.trim() || "paper",
      }),
      timeoutMs: 3600000,
    });
    stopUploadProgressPolling();
    renderUploadProgress(data.upload_progress || {
      status: data.failed_count ? "completed_with_errors" : "completed",
      stage: "Batch complete",
      message: `Processed ${data.count || 0}, skipped ${data.skipped_count || 0}, failed ${data.failed_count || 0}.`,
      percent: 100,
    });
    setUploadOutput(uploadBatchResultLines(data).join("\n"));
    if ((data.errors || []).length) {
      window.alert(`${data.errors.length} file${data.errors.length === 1 ? "" : "s"} failed. See progress details.`);
    }
    await loadDashboard();
    await loadPapers();
    await loadWikiList();
    const papers = data.papers || data.items || [];
    if (papers.length && papers[0].arxiv_id) {
      setView("papers");
      await loadPaperDetail(papers[0].arxiv_id);
    }
  } catch (error) {
    stopUploadProgressPolling();
    renderUploadProgress({
      status: "failed",
      stage: "Batch failed",
      message: error.message,
      percent: 100,
      log: [{ time: new Date().toLocaleTimeString(), stage: "Batch failed", message: error.message }],
    });
    setUploadOutput(error.message);
    window.alert(error.message);
  } finally {
    button.disabled = false;
  }
}

async function loadUploadPrompt() {
  const editor = $("#uploadPromptEditor");
  const status = $("#uploadPromptStatus");
  if (!editor || !status) return;
  try {
    const data = await api("/api/upload-prompt", { timeoutMs: 10000 });
    editor.value = data.current_prompt || data.default_prompt || "";
    status.textContent = data.saved ? "Custom prompt loaded." : "Default prompt loaded.";
  } catch (error) {
    status.textContent = error.message;
  }
}

async function saveUploadPrompt() {
  const button = $("#saveUploadPrompt");
  const status = $("#uploadPromptStatus");
  button.disabled = true;
  status.textContent = "Saving prompt...";
  try {
    const data = await api("/api/upload-prompt", {
      method: "POST",
      body: JSON.stringify({ prompt: $("#uploadPromptEditor").value }),
      timeoutMs: 10000,
    });
    $("#uploadPromptEditor").value = data.current_prompt || "";
    status.textContent = "Prompt saved.";
  } catch (error) {
    status.textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

async function resetUploadPrompt() {
  const confirmed = window.confirm("Reset the upload work prompt to the default prompt?");
  if (!confirmed) return;
  const button = $("#resetUploadPrompt");
  const status = $("#uploadPromptStatus");
  button.disabled = true;
  status.textContent = "Resetting prompt...";
  try {
    const data = await api("/api/upload-prompt", {
      method: "POST",
      body: JSON.stringify({ reset: true }),
      timeoutMs: 10000,
    });
    $("#uploadPromptEditor").value = data.current_prompt || data.default_prompt || "";
    status.textContent = "Default prompt restored.";
  } catch (error) {
    status.textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

async function loadSettings() {
  try {
    const data = await api("/api/settings", { timeoutMs: 15000 });
    if (Array.isArray(data.providers) && data.providers.length) {
      $("#settingsProvider").innerHTML = data.providers.map((provider) => (
        `<option value="${escapeHtml(provider.value)}">${escapeHtml(provider.label)}</option>`
      )).join("");
    }
    $("#settingsProvider").value = data.provider || "ollama";
    $("#settingsOpenaiBaseUrl").value = data.openai_base_url || "";
    $("#settingsOpenaiApiKey").value = data.openai_api_key || "";
    $("#settingsNasaAdsApiKey").value = data.nasa_ads_api_key || "";
    $("#settingsOllamaBaseUrl").value = data.ollama_base_url || "";
    $("#settingsContext").value = data.context_window || "";
    const apiProvider = data.provider !== "ollama";
    setModelSelectOptions($("#settingsChatModel"), [], data.chat_model || "", {
      allowBlank: apiProvider,
      blankLabel: "Server default / no model",
    });
    setModelSelectOptions($("#settingsRetrievalModel"), [], data.retrieval_model || data.chat_model || "", {
      allowBlank: apiProvider,
      blankLabel: "Server default / no model",
    });
    updateSettingsVisibility();
    await loadSettingsModelOptions({
      announce: false,
      chatModel: data.chat_model || "",
      retrievalModel: data.retrieval_model || data.chat_model || "",
    });
    const modelLabel = apiProvider ? (data.chat_model || "server default") : data.chat_model;
    window.setTimeout(() => {
      $("#settingsOutput").textContent = `Current setting: ${data.provider_label || data.provider} - ${modelLabel}\nSaved at: ${data.settings_path}`;
    }, 0);
    $("#settingsOutput").textContent = `현재 설정: ${data.provider} · ${modelLabel}\n저장 위치: ${data.settings_path}`;
  } catch (error) {
    $("#settingsOutput").textContent = error.message;
  }
}

function setSettingsFieldVisible(fieldId, visible) {
  const field = $(`#${fieldId}`);
  if (!field) return;
  field.hidden = !visible;
  field.querySelectorAll("input, select, textarea, button").forEach((control) => {
    control.disabled = !visible;
  });
}

function updateSettingsVisibility() {
  const provider = $("#settingsProvider").value;
  const isOllama = provider === "ollama";
  const isApiProvider = !isOllama;

  setSettingsFieldVisible("settingsContextField", false);
  setSettingsFieldVisible("settingsOpenaiBaseUrlField", isApiProvider);
  setSettingsFieldVisible("settingsOpenaiApiKeyField", isApiProvider);
  setSettingsFieldVisible("settingsNasaAdsApiKeyField", true);
  setSettingsFieldVisible("settingsOllamaBaseUrlField", isOllama);
  setSettingsFieldVisible("settingsChatModelField", true);
  setSettingsFieldVisible("settingsRetrievalModelField", true);
}

function setModelSelectOptions(select, models, selected, options = {}) {
  const unique = Array.from(new Set([selected, ...models].filter(Boolean)));
  const blankOption = options.allowBlank ? `<option value="">${escapeHtml(options.blankLabel || "Server default")}</option>` : "";
  if (select.tagName === "INPUT") {
    const list = document.getElementById(select.getAttribute("list"));
    if (list) {
      list.innerHTML = unique.map((model) => (
        `<option value="${escapeHtml(model)}"></option>`
      )).join("");
    }
    select.value = selected || (options.allowBlank ? "" : (unique[0] || ""));
    return;
  }
  select.innerHTML = blankOption + unique.map((model) => (
    `<option value="${escapeHtml(model)}">${escapeHtml(model)}</option>`
  )).join("") || `<option value="">No models found</option>`;
  select.value = selected && unique.includes(selected) ? selected : (unique[0] || "");
  if (options.allowBlank && !selected) select.value = "";
}

async function loadSettingsModelOptions(options = {}) {
  const provider = $("#settingsProvider").value;
  if (false && provider !== "ollama") {
    setModelSelectOptions($("#settingsChatModel"), [], "", {
      allowBlank: true,
      blankLabel: "Server default / no model",
    });
    setModelSelectOptions($("#settingsRetrievalModel"), [], "", {
      allowBlank: true,
      blankLabel: "Server default / no model",
    });
    updateSettingsVisibility();
    if (options.announce !== false) {
      $("#settingsOutput").textContent = "API compatible mode: API base URL/API key만 설정하면 됩니다.\n모델명과 context window는 서버 기본 설정을 사용합니다.";
    }
    return;
  }
  const params = new URLSearchParams({
    provider,
    openai_base_url: $("#settingsOpenaiBaseUrl").value,
    openai_api_key: $("#settingsOpenaiApiKey").value,
    ollama_base_url: $("#settingsOllamaBaseUrl").value,
    chat_model: options.chatModel || $("#settingsChatModel").value,
    retrieval_model: options.retrievalModel || $("#settingsRetrievalModel").value,
  });
  const chatSelect = $("#settingsChatModel");
  const retrievalSelect = $("#settingsRetrievalModel");
  chatSelect.innerHTML = `<option value="">Loading models...</option>`;
  retrievalSelect.innerHTML = `<option value="">Loading models...</option>`;
  try {
    const data = await api(`/api/settings-models?${params.toString()}`, { timeoutMs: 15000 });
    if (provider === "ollama" && data.base_url) $("#settingsOllamaBaseUrl").value = data.base_url;
    if (provider !== "ollama" && data.base_url) $("#settingsOpenaiBaseUrl").value = data.base_url;
    const models = data.models || [];
    const blankLabel = "Server default / no model";
    setModelSelectOptions(chatSelect, models, data.chat_default || options.chatModel || data.default || "", {
      allowBlank: provider !== "ollama",
      blankLabel,
    });
    setModelSelectOptions(retrievalSelect, models, data.retrieval_default || options.retrievalModel || data.default || "", {
      allowBlank: provider !== "ollama",
      blankLabel,
    });
    if (options.announce !== false) {
      $("#settingsOutput").textContent = data.available
        ? `${data.message || "Models loaded"}\n${models.length} model(s) available.`
        : `${data.message || "Model listing failed"}\n모델 이름을 직접 확인하거나 Ollama가 실행 중인지 확인하세요.`;
    }
  } catch (error) {
    chatSelect.innerHTML = `<option value="">Model load failed</option>`;
    retrievalSelect.innerHTML = `<option value="">Model load failed</option>`;
    $("#settingsOutput").textContent = `모델 목록을 불러오지 못했습니다.\n${error.message}`;
  }
}

async function saveSettings(event) {
  event.preventDefault();
  const payload = {
    provider: $("#settingsProvider").value,
    openai_base_url: $("#settingsOpenaiBaseUrl").value,
    openai_api_key: $("#settingsOpenaiApiKey").value,
    nasa_ads_api_key: $("#settingsNasaAdsApiKey").value,
    ollama_base_url: $("#settingsOllamaBaseUrl").value,
    chat_model: $("#settingsChatModel").value,
    retrieval_model: $("#settingsRetrievalModel").value,
    context_window: $("#settingsContext").value,
  };
  const saveButton = $("#settingsForm button[type='submit']");
  saveButton.disabled = true;
  $("#settingsOutput").textContent = "Testing the LLM connection...\nSettings are saved only after the test succeeds.";
  $("#settingsOutput").textContent = "LLM 연결을 확인하는 중입니다...\n설정은 테스트가 성공한 뒤 저장됩니다.";
  try {
    $("#settingsOutput").textContent = "Testing the LLM connection...\nSettings are saved only after the test succeeds.";
    const data = await api("/api/settings", { method: "POST", body: JSON.stringify(payload), timeoutMs: 45000 });
    const connection = data.connection || {};
    $("#settingsOutput").textContent = [
      "저장 완료: LLM 사용 가능",
      `Provider: ${data.settings.provider}`,
      `Model: ${connection.model || data.settings.chat_model || "server default"}`,
      connection.latency_ms !== undefined ? `Connection test: ${connection.latency_ms} ms` : "",
      connection.response_excerpt ? `Test response: ${connection.response_excerpt}` : "",
      `Saved in ${data.settings.settings_path}`,
    ].filter(Boolean).join("\n");
    const limited = connection.status === "limited" || connection.ok === false;
    $("#settingsOutput").textContent = [
      limited ? "Saved with warning: API quota or rate limit is currently exhausted." : "Saved: LLM is available.",
      `Provider: ${data.settings.provider_label || data.settings.provider}`,
      `Model: ${connection.model || data.settings.chat_model || "server default"}`,
      connection.latency_ms !== undefined ? `Connection test: ${connection.latency_ms} ms` : "",
      connection.message ? `Status: ${connection.message}` : "",
      connection.response_excerpt ? `Test response: ${connection.response_excerpt}` : "",
      connection.error_excerpt ? `Test error: ${connection.error_excerpt}` : "",
      `Saved in ${data.settings.settings_path}`,
    ].filter(Boolean).join("\n");
    await loadModels();
    await loadDashboard();
  } catch (error) {
    window.setTimeout(() => {
      $("#settingsOutput").textContent = `Save failed: the LLM connection could not be verified.\n${error.message}\n\nExisting settings were kept.`;
    }, 0);
    $("#settingsOutput").textContent = `저장 실패: LLM 연결을 확인할 수 없습니다.\n${error.message}\n\n기존 설정은 유지됩니다.`;
  } finally {
    saveButton.disabled = false;
  }
}

async function openSettingsDialog() {
  const dialog = $("#settingsDialog");
  if (typeof dialog.showModal === "function") {
    if (!dialog.open) dialog.showModal();
  } else {
    dialog.classList.add("open");
  }
  loadSettings().catch((error) => {
    $("#settingsOutput").textContent = error.message;
  });
}

function closeSettingsDialog() {
  const dialog = $("#settingsDialog");
  if (typeof dialog.close === "function" && dialog.open) {
    dialog.close();
  }
  dialog.classList.remove("open");
}

function openAboutDialog() {
  const dialog = $("#aboutDialog");
  if (typeof dialog.showModal === "function") {
    if (!dialog.open) dialog.showModal();
  } else {
    dialog.classList.add("open");
  }
}

function closeAboutDialog() {
  const dialog = $("#aboutDialog");
  if (typeof dialog.close === "function" && dialog.open) {
    dialog.close();
  }
  dialog.classList.remove("open");
}

function wireEvents() {
  $$(".tab").forEach((tab) => tab.addEventListener("click", () => navigateToView(tab.dataset.view)));
  $("#openAbout").addEventListener("click", (event) => {
    event.preventDefault();
    openAboutDialog();
  });
  $("#closeAbout").addEventListener("click", closeAboutDialog);
  $("#aboutDialog").addEventListener("click", (event) => {
    if (event.target === $("#aboutDialog")) closeAboutDialog();
  });
  $("#openSettings").addEventListener("click", () => {
    openSettingsDialog().catch((error) => {
      $("#settingsOutput").textContent = error.message;
    });
  });
  $("#closeSettings").addEventListener("click", closeSettingsDialog);
  $("#settingsDialog").addEventListener("click", (event) => {
    if (event.target === $("#settingsDialog")) closeSettingsDialog();
  });
  $("#settingsProvider").addEventListener("change", () => {
    $("#settingsOpenaiBaseUrl").value = "";
    $("#settingsOpenaiApiKey").value = "";
    $("#settingsChatModel").value = "";
    $("#settingsRetrievalModel").value = "";
    updateSettingsVisibility();
    loadSettingsModelOptions({ announce: true });
  });
  $("#settingsOllamaBaseUrl").addEventListener("change", () => {
    if ($("#settingsProvider").value === "ollama") loadSettingsModelOptions({ announce: true });
  });
  $("#settingsOpenaiBaseUrl").addEventListener("change", () => {
    if ($("#settingsProvider").value !== "ollama") loadSettingsModelOptions({ announce: true });
  });
  $("#refreshDashboard").addEventListener("click", loadDashboard);
  $("#reloadPapers").addEventListener("click", loadPapers);
  $("#remotePaperSearchForm").addEventListener("submit", searchRemotePapers);
  $("#clearRemotePaperSearch").addEventListener("click", clearRemotePaperSearch);
  $("#paperSearch").addEventListener("keydown", (event) => {
    if (event.key === "Enter") loadPapers();
  });
  $("#statusFilter").addEventListener("change", loadPapers);
  $("#chatModel").addEventListener("change", () => {
    const selected = $("#chatModel").selectedOptions[0];
    state.chatModel = $("#chatModel").value;
    $("#chatModel").title = selected?.title || "";
    if (selected?.disabled) {
      appendMessage("assistant", selected.title || "Selected model is unavailable.");
    }
  });
  $("#chatForm").addEventListener("submit", (event) => {
    event.preventDefault();
    const question = $("#chatQuestion").value.trim();
    if (!question) return;
    $("#chatQuestion").value = "";
    askChat(question);
  });
  $("#startRun").addEventListener("click", runCommand);
  $("#uploadPdf").addEventListener("change", updateUploadCategoryForFiles);
  $("#uploadForm").addEventListener("submit", uploadPaper);
  $("#chooseBatchFolder").addEventListener("click", chooseBatchFolder);
  $("#batchUploadForm").addEventListener("submit", runBatchUpload);
  $("#saveUploadPrompt").addEventListener("click", saveUploadPrompt);
  $("#resetUploadPrompt").addEventListener("click", resetUploadPrompt);
  $("#settingsForm").addEventListener("submit", saveSettings);
  $("#refreshReview").addEventListener("click", loadReviewQueue);
  $("#runLintNow").addEventListener("click", runLintNow);
  window.addEventListener("message", (event) => {
    if (event.origin !== window.location.origin) return;
    if (event.data?.type === "open-paper-chat") openPaperChatById(event.data.arxiv_id);
  });
  window.addEventListener("popstate", (event) => {
    applyHistoryRoute(event.state || routeFromLocation()).catch((error) => {
      showLoadError("#metrics", "Navigation", error);
    });
  });
  renderChatContext();
}

async function init() {
  setAppLoading(true, "Starting Astro-Note AI...");
  wireEvents();
  const today = new Date().toISOString().slice(0, 10);
  $("#runDate").value = today;
  setBootMessage("Loading local models and workspace...");
  const loaders = [
    loadModels().catch((error) => {
      const select = $("#chatModel");
      select.innerHTML = `<option value="">Model load failed</option>`;
      select.title = error.message;
    }),
    loadDashboard().catch((error) => showLoadError("#metrics", "Dashboard", error)),
    loadPapers().catch((error) => showLoadError("#paperList", "Papers", error)),
    loadWikiList().catch((error) => showLoadError("#wikiList", "Wiki list", error)),
    loadSettings(),
    loadUploadPrompt(),
    loadReviewQueue().catch((error) => showLoadError("#proposalQueue", "Review queue", error)),
  ];
  await Promise.allSettled(loaders);
  setBootMessage("Opening workspace...");
  const initialRoute = normalizeRoute(routeFromLocation());
  updateHistory(initialRoute, { replace: true });
  await applyHistoryRoute(initialRoute);
  setAppLoading(false);
}

init().catch((error) => {
  setAppLoading(false);
  document.body.innerHTML = `<main class="view active"><pre class="output">${escapeHtml(error.stack || error.message)}</pre></main>`;
});
