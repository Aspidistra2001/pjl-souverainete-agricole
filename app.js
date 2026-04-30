/* ============================================================
 * Bulletin de veille — moteur (version "serveur first")
 * ============================================================
 *
 * Le travail d'enrichissement (lecture RSS + fetch XML OpenData) est
 * désormais fait côté serveur par .github/workflows/sync.yml qui exécute
 * scripts/refresh_amendments.py toutes les 30 minutes et committe les
 * changements directement dans data/amendments.json.
 *
 * Côté navigateur, on se contente donc :
 *   - de charger data/amendments.json
 *   - de l'afficher
 *   - de le rafraîchir toutes les 5 minutes pour récupérer les
 *     éventuels commits récents (rapport entre les rafraîchissements
 *     navigateur (5 min) et la fréquence de sync serveur (30 min))
 *
 * Plus aucun fetch externe n'est fait depuis le navigateur, ce qui
 * élimine tous les problèmes CORS et la dépendance à des proxys
 * gratuits instables.
 * ============================================================ */

const DATA_URL = "data/amendments.json";
const AUTHORS_URL = "data/authors.json";
const REFRESH_INTERVAL_MS = 5 * 60 * 1000; // 5 minutes

const state = {
  meta: null,
  amendments: [],
  byNum: new Map(),
  authors: null,
  filters: {
    search: "",
    states: new Set(),
    groups: new Set(),
    articles: new Set(),
    commission: "all",   // 'all' | 'Développement durable' | 'Affaires économiques'
  },
  previouslySeen: new Set(),
  previousStates: new Map(),
  detectedChanges: {
    addedSinceOpen: [],
    statesChanged: [],
  },
  lastRefresh: null,
};

const dom = {};

document.addEventListener("DOMContentLoaded", async () => {
  cacheDom();
  bindEvents();
  setStatus("loading", "Chargement…");

  try {
    await loadData();
    initFilters();
    render();
    state.lastRefresh = new Date();
    updateLastRefreshLabel();
    setStatus("ok", "Données à jour");
    setInterval(refreshData, REFRESH_INTERVAL_MS);
  } catch (err) {
    console.error("Erreur de chargement :", err);
    setStatus("error", "Échec du chargement");
    dom.amendments.innerHTML =
      `<p class="no-results">Erreur de chargement des données. Vérifiez la console.</p>`;
  }
});

function cacheDom() {
  dom.statusPill = document.getElementById("status-pill");
  dom.statusText = document.getElementById("status-text");
  dom.lastUpdate = document.getElementById("last-update");
  dom.refreshBtn = document.getElementById("refresh-btn");
  dom.search = document.getElementById("search");
  dom.stats = document.getElementById("stats");
  dom.stateFilters = document.getElementById("state-filters");
  dom.groupFilters = document.getElementById("group-filters");
  dom.articleFilters = document.getElementById("article-filters");
  dom.changelog = document.getElementById("changelog");
  dom.changelogBody = document.getElementById("changelog-body");
  dom.amendments = document.getElementById("amendments");
}

function bindEvents() {
  dom.refreshBtn.addEventListener("click", () => refreshData(true));
  dom.search.addEventListener("input", e => {
    state.filters.search = e.target.value.trim().toLowerCase();
    render();
  });
  // Sélecteur de commission (boutons radio en haut)
  document.querySelectorAll('input[name="commission"]').forEach(input => {
    input.addEventListener("change", e => {
      if (e.target.checked) {
        state.filters.commission = e.target.value;
        render();
      }
    });
  });
}

// ------------------------------------------------------------
// Chargement et rafraîchissement
// ------------------------------------------------------------

async function fetchJson(url) {
  const sep = url.includes("?") ? "&" : "?";
  const res = await fetch(`${url}${sep}t=${Date.now()}`, { cache: "no-cache" });
  if (!res.ok) throw new Error(`HTTP ${res.status} sur ${url}`);
  return res.json();
}

async function fetchJsonSafe(url) {
  // Comme fetchJson, mais retourne null silencieusement en cas d'échec
  try {
    return await fetchJson(url);
  } catch (e) {
    return null;
  }
}

async function loadData() {
  const [data, authors, syncStatus] = await Promise.all([
    fetchJson(DATA_URL),
    fetchJson(AUTHORS_URL),
    fetchJsonSafe("data/sync_status.json"),
  ]);
  state.meta = data.meta;
  state.amendments = data.amendments;
  state.byNum = new Map(state.amendments.map(a => [a.num, a]));
  state.authors = authors;
  state.syncStatus = syncStatus;

  state.previouslySeen = new Set(state.byNum.keys());
  state.previousStates = new Map(state.amendments.map(a => [a.num, a.state]));
  state.detectedChanges.addedSinceOpen = [];
  state.detectedChanges.statesChanged = [];
}

async function refreshData(manual = false) {
  if (manual) dom.refreshBtn.setAttribute("disabled", "");
  setStatus("loading", "Lecture des données…");

  try {
    const [data, syncStatus] = await Promise.all([
      fetchJson(DATA_URL),
      fetchJsonSafe("data/sync_status.json"),
    ]);
    state.syncStatus = syncStatus;
    const newByNum = new Map(data.amendments.map(a => [a.num, a]));

    const added = [];
    const stateChanges = [];

    for (const [num, amend] of newByNum) {
      if (!state.previouslySeen.has(num)) {
        added.push(num);
      }
      const prevState = state.previousStates.get(num);
      if (prevState !== undefined && prevState !== amend.state) {
        stateChanges.push({ num, oldState: prevState, newState: amend.state });
      }
    }

    state.amendments = data.amendments;
    state.byNum = newByNum;
    state.meta = data.meta;

    state.detectedChanges.addedSinceOpen = state.detectedChanges.addedSinceOpen.concat(added);
    state.detectedChanges.statesChanged = state.detectedChanges.statesChanged.concat(stateChanges);

    for (const num of added) {
      const a = state.byNum.get(num);
      if (a) a._addedSinceOpen = true;
    }
    for (const ch of stateChanges) {
      const a = state.byNum.get(ch.num);
      if (a) {
        a._stateChangedSinceOpen = true;
        a._previousState = ch.oldState;
      }
    }

    for (const [num, amend] of newByNum) {
      state.previousStates.set(num, amend.state);
      state.previouslySeen.add(num);
    }

    state.lastRefresh = new Date();
    setStatus("ok",
      added.length || stateChanges.length
        ? `Mise à jour reçue — ${added.length} nouveau(x), ${stateChanges.length} changement(s)`
        : "Données à jour"
    );
    updateLastRefreshLabel();
    render();
  } catch (err) {
    console.error("Échec du rafraîchissement :", err);
    setStatus("error", "Échec du rafraîchissement");
  } finally {
    dom.refreshBtn.removeAttribute("disabled");
  }
}

// ------------------------------------------------------------
// Filtres
// ------------------------------------------------------------

function initFilters() {
  const stateCounts = countBy(state.amendments, a => a.state);
  buildFilterRows(dom.stateFilters,
    Array.from(stateCounts.keys()).sort(),
    stateCounts, "state");

  const groupCounts = countBy(state.amendments, a => a.group);
  const groupOrder = Array.from(groupCounts.entries())
    .sort((a, b) => b[1] - a[1])
    .map(([k]) => k);
  buildFilterRows(dom.groupFilters, groupOrder, groupCounts, "group", true);

  const articleCounts = countBy(state.amendments, a => a.article);
  const articleOrder = state.meta.article_order.filter(x => articleCounts.has(x))
    .concat(Array.from(articleCounts.keys()).filter(x => !state.meta.article_order.includes(x)));
  buildFilterRows(dom.articleFilters, articleOrder, articleCounts, "article");
}

function buildFilterRows(container, keys, counts, kind, withSwatch = false) {
  container.innerHTML = "";
  keys.forEach(key => {
    const row = document.createElement("label");
    row.className = "filter-row";

    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.dataset.kind = kind;
    cb.dataset.key = key;
    cb.addEventListener("change", () => onFilterChange(kind, key, cb.checked));
    row.appendChild(cb);

    if (withSwatch && state.meta.groups[key]) {
      const sw = document.createElement("span");
      sw.className = "swatch";
      sw.style.background = state.meta.groups[key].bg;
      row.appendChild(sw);
    }

    const label = document.createElement("span");
    label.className = "label";
    label.textContent = withSwatch ? state.meta.groups[key]?.label || key : prettyArticle(key);
    row.appendChild(label);

    const count = document.createElement("span");
    count.className = "count";
    count.textContent = counts.get(key) || 0;
    row.appendChild(count);

    container.appendChild(row);
  });
}

function onFilterChange(kind, key, checked) {
  const set = state.filters[kind + "s"];
  if (checked) set.add(key);
  else set.delete(key);
  render();
}

function countBy(arr, fn) {
  const m = new Map();
  arr.forEach(x => {
    const k = fn(x);
    m.set(k, (m.get(k) || 0) + 1);
  });
  return m;
}

function prettyArticle(name) {
  return name.replace("Article PREMIER", "Article 1ᵉʳ");
}

// ------------------------------------------------------------
// Rendu
// ------------------------------------------------------------

function render() {
  renderCommissionCounts();
  renderStats();
  renderChangelog();
  renderAmendments();
}

function renderCommissionCounts() {
  // Met à jour les compteurs affichés à côté des boutons radio
  // d'après l'instance de chaque amendement
  const counts = countBy(state.amendments, a => a.instance || "");
  const total = state.amendments.length;
  document.querySelectorAll('[data-commission-count]').forEach(el => {
    const key = el.dataset.commissionCount;
    const n = key === "all" ? total : (counts.get(key) || 0);
    el.textContent = `(${n})`;
  });
}

function renderStats() {
  const all = state.amendments;
  const newCount = all.filter(a => a.is_new || a.is_rss_new).length;
  const actifs = all.filter(a =>
    a.state === "En traitement" || a.state === "A discuter"
  ).length;
  const votes = all.filter(a =>
    a.state === "Adopté" || a.state === "Rejeté" ||
    a.state === "Tombé" || a.state === "Non soutenu"
  ).length;

  dom.stats.innerHTML = `
    <div class="stat"><span class="stat-num">${all.length}</span><span class="stat-label">Amendements</span></div>
    <div class="stat"><span class="stat-num accent">${newCount}</span><span class="stat-label">Nouveaux</span></div>
    <div class="stat"><span class="stat-num">${actifs}</span><span class="stat-label">Actifs</span></div>
    <div class="stat"><span class="stat-num">${votes}</span><span class="stat-label">Votés</span></div>
  `;
}

function renderChangelog() {
  const baselineNew = state.amendments.filter(a => a.is_new && !a.is_rss_new);
  const rssAdded = state.amendments.filter(a => a.is_rss_new);
  const sessionAdded = state.detectedChanges.addedSinceOpen;
  const sessionStateChanges = state.detectedChanges.statesChanged;

  // Changements opérés côté serveur dans les dernières 24h, lus depuis les
  // horodatages présents dans le JSON. Permet de voir l'historique récent
  // même quand la page vient juste d'être ouverte.
  const ONE_DAY_MS = 24 * 60 * 60 * 1000;
  const now = Date.now();
  const recentServerChanges = state.amendments
    .filter(a => a.state_changed_at && (now - new Date(a.state_changed_at).getTime() < ONE_DAY_MS))
    .map(a => ({
      num: a.num,
      newState: a.state,
      oldState: a.previous_state || "?",
      at: new Date(a.state_changed_at),
    }))
    .sort((a, b) => b.at - a.at);

  const recentServerAdded = state.amendments
    .filter(a => a.added_via_rss_at && (now - new Date(a.added_via_rss_at).getTime() < ONE_DAY_MS))
    .map(a => ({
      num: a.num,
      at: new Date(a.added_via_rss_at),
    }))
    .sort((a, b) => b.at - a.at);

  const parts = [];

  if (baselineNew.length > 0) {
    const nums = baselineNew.map(a => a.num);
    parts.push(
      `<p><strong>${nums.length} amendements</strong> ont été ajoutés au tableau officiel entre le 26 et le 28 avril 2026 : ` +
      nums.map(n => `<code>${n}</code>`).join(", ") +
      `. Signalés en <em>jaune</em>.</p>`
    );
  }

  if (recentServerAdded.length > 0) {
    const nums = recentServerAdded.map(c => c.num);
    parts.push(
      `<p><strong>${nums.length} amendement${nums.length > 1 ? "s ajoutés" : " ajouté"} dans les dernières 24 h</strong> ` +
      `via le flux RSS (synchronisation automatique côté serveur) : ` +
      nums.slice(0, 15).map(n => `<code>${n}</code>`).join(", ") +
      (nums.length > 15 ? `, et ${nums.length - 15} de plus…` : "") +
      `</p>`
    );
  }

  if (recentServerChanges.length > 0) {
    parts.push(
      `<p><strong>${recentServerChanges.length} changement${recentServerChanges.length > 1 ? "s" : ""} d'état dans les dernières 24 h</strong> ` +
      `(synchronisation automatique côté serveur) :</p>` +
      `<ul class="changelog-list">` +
      recentServerChanges.slice(0, 20).map(c =>
        `<li><code>${c.num}</code> : <em>${c.oldState}</em> → <strong>${c.newState}</strong> ` +
        `<span class="changelog-time">${formatRelativeTime(c.at)}</span></li>`
      ).join("") +
      `</ul>` +
      (recentServerChanges.length > 20 ? `<p class="changelog-foot">…et ${recentServerChanges.length - 20} autre(s) plus ancien(s).</p>` : "")
    );
  }

  // Changements détectés depuis l'ouverture de cette page (en plus des historiques serveur)
  if (sessionAdded.length > 0) {
    parts.push(
      `<p><em>Pendant votre session</em> : ${sessionAdded.length} amendement${sessionAdded.length > 1 ? "s" : ""} ` +
      `${sessionAdded.length > 1 ? "ajoutés" : "ajouté"} : ` +
      sessionAdded.slice(0, 5).map(n => `<code>${n}</code>`).join(", ") +
      `.</p>`
    );
  }

  if (sessionStateChanges.length > 0) {
    parts.push(
      `<p><em>Pendant votre session</em> : ${sessionStateChanges.length} changement${sessionStateChanges.length > 1 ? "s" : ""} d'état :</p>` +
      `<ul class="changelog-list">` +
      sessionStateChanges.slice(0, 10).map(c =>
        `<li><code>${c.num}</code> : <em>${c.oldState}</em> → <strong>${c.newState}</strong></li>`
      ).join("") +
      `</ul>`
    );
  }

  if (parts.length === 0) {
    dom.changelogBody.innerHTML = `<p class="changelog-empty">Aucune évolution récente.</p>`;
  } else {
    dom.changelogBody.innerHTML = parts.join("");
  }
}

function formatRelativeTime(date) {
  const diff = Date.now() - date.getTime();
  const minutes = Math.round(diff / 60000);
  if (minutes < 1) return "à l'instant";
  if (minutes < 60) return `il y a ${minutes} min`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `il y a ${hours} h`;
  const days = Math.round(hours / 24);
  return `il y a ${days} j`;
}

function renderAmendments() {
  const filtered = filterAmendments();

  if (filtered.length === 0) {
    dom.amendments.innerHTML = `<p class="no-results">Aucun amendement ne correspond aux filtres en cours.</p>`;
    return;
  }

  const articleOrder = state.meta.article_order;
  const byArticle = new Map();
  filtered.forEach(a => {
    if (!byArticle.has(a.article)) byArticle.set(a.article, []);
    byArticle.get(a.article).push(a);
  });

  const sortedArticles = Array.from(byArticle.keys()).sort((a, b) => {
    const ai = articleOrder.indexOf(a);
    const bi = articleOrder.indexOf(b);
    return (ai < 0 ? 9999 : ai) - (bi < 0 ? 9999 : bi);
  });

  const html = sortedArticles.map(article => {
    const list = byArticle.get(article).sort((a, b) => numOf(a.num) - numOf(b.num));
    const newInArticle = list.filter(a => a.is_new || a.is_rss_new).length;
    const groupCounts = countBy(list, a => a.group);
    const groupHtml = Array.from(groupCounts.entries())
      .sort((a, b) => b[1] - a[1])
      .map(([g, n]) => groupPill(g, n))
      .join("");

    return `
      <section class="article-section">
        <header class="article-header">
          <h2 class="article-name">${escapeHtml(prettyArticle(article))}</h2>
          <span class="article-count">${list.length} amendement${list.length > 1 ? "s" : ""}</span>
          ${newInArticle > 0 ? `<span class="article-new-pill">+ ${newInArticle} nouveau${newInArticle > 1 ? "x" : ""}</span>` : ""}
        </header>
        <div class="article-groups">${groupHtml}</div>
        ${list.map(renderAmendment).join("")}
      </section>
    `;
  }).join("");

  dom.amendments.innerHTML = html;
}

function renderAmendment(a) {
  const classes = ["amendment"];
  if (a.is_new && !a.is_rss_new) classes.push("is-new");
  if (a.is_rss_new) classes.push("is-rss-new");
  if (a._stateChangedSinceOpen) classes.push("is-changed");

  const url = a.url || "#";
  const stateClass = stateCssClass(a.state);

  const summaryHtml = a.summary_pending
    ? `<div class="pending-banner" role="note">
         <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
           <path d="M12 9v4"/><path d="M12 17h.01"/>
           <path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/>
         </svg>
         Synthèse à rédiger — extrait de l'exposé sommaire officiel
       </div>
       <p class="amendment-summary summary-pending">${escapeHtml(a.summary)}</p>`
    : `<p class="amendment-summary">${escapeHtml(a.summary)}</p>`;

  return `
    <article class="${classes.join(" ")}">
      <a class="amendment-num" href="${escapeAttr(url)}" target="_blank" rel="noopener">${escapeHtml(a.num)}</a>
      <div class="amendment-body">
        <div class="amendment-meta">
          ${groupPill(a.group)}
          <span class="author">${escapeHtml(a.author)}</span>
          ${a.rapporteur ? `<span class="rapporteur-tag">(rapporteure)</span>` : ""}
          <span class="badge badge-state ${stateClass}">${escapeHtml(a.state)}</span>
          ${a.instance === "Affaires économiques" ? `<span class="badge badge-instance">Affaires éco.</span>` : ""}
          ${a.instance === "Affaires sociales" ? `<span class="badge badge-instance">Affaires soc.</span>` : ""}
          ${a.is_new && !a.is_rss_new ? `<span class="badge badge-new">NOUVEAU</span>` : ""}
          ${a.is_rss_new ? `<span class="badge badge-rss">FLUX</span>` : ""}
          ${a._stateChangedSinceOpen ? `<span class="badge badge-changed" title="Avant : ${escapeAttr(a._previousState || "?")}">MODIFIÉ</span>` : ""}
        </div>
        ${summaryHtml}
      </div>
    </article>
  `;
}

function groupPill(group, count) {
  const meta = state.meta.groups[group];
  if (!meta) return `<span class="group-pill" style="background:#ddd;color:#333">${escapeHtml(group || "?")}</span>`;
  return `<span class="group-pill" style="background:${meta.bg};color:${meta.fg}">${meta.label}${count !== undefined ? ` <span class="num">${count}</span>` : ""}</span>`;
}

function stateCssClass(s) {
  return ({
    "En traitement":   "state-en-traitement",
    "A discuter":      "state-a-discuter",
    "Discuté":         "state-discute",
    "Adopté":          "state-adopte",
    "Rejeté":          "state-rejete",
    "Tombé":           "state-tombe",
    "Non soutenu":     "state-tombe",
    "Retiré":          "state-retire",
    "Irrecevable":     "state-irrecevable",
    "Irrecevable 40":  "state-irrecevable",
  })[s] || "state-en-traitement";
}

function numOf(n) {
  const m = String(n).match(/(\d+)/);
  return m ? parseInt(m[1], 10) : 0;
}

function filterAmendments() {
  const all = state.amendments;
  const { search, states, groups, articles, commission } = state.filters;

  return all.filter(a => {
    if (commission !== "all" && a.instance !== commission) return false;
    if (states.size && !states.has(a.state)) return false;
    if (groups.size && !groups.has(a.group)) return false;
    if (articles.size && !articles.has(a.article)) return false;
    if (search) {
      const blob = `${a.num} ${a.author} ${a.summary} ${a.article}`.toLowerCase();
      if (!blob.includes(search)) return false;
    }
    return true;
  });
}

function setStatus(s, label) {
  dom.statusPill.dataset.state = s;
  dom.statusText.textContent = label;
}

function updateLastRefreshLabel() {
  if (!state.lastRefresh) return;
  const fmt = new Intl.DateTimeFormat("fr-FR", {
    hour: "2-digit", minute: "2-digit", day: "2-digit", month: "short"
  });
  let txt = `Dernière vérification : ${fmt.format(state.lastRefresh)}`;
  // Priorité 1 : sync_status.json (mis à jour à CHAQUE exécution du script)
  // Priorité 2 : meta.last_sync (mis à jour seulement quand amendments.json change)
  let serverSyncAt = null;
  if (state.syncStatus && state.syncStatus.last_sync_utc) {
    serverSyncAt = new Date(state.syncStatus.last_sync_utc);
  } else if (state.meta && state.meta.last_sync) {
    serverSyncAt = new Date(state.meta.last_sync);
  }
  if (serverSyncAt && !isNaN(serverSyncAt.getTime())) {
    const ageMin = Math.round((Date.now() - serverSyncAt.getTime()) / 60000);
    if (ageMin < 60) {
      txt += ` · synchro serveur il y a ${ageMin} min`;
    } else if (ageMin < 1440) {
      const ageH = Math.round(ageMin / 60);
      txt += ` · synchro serveur il y a ${ageH} h`;
    } else {
      const ageJ = Math.round(ageMin / 1440);
      txt += ` · synchro serveur il y a ${ageJ} j`;
    }
  }
  dom.lastUpdate.textContent = txt;
}

function escapeHtml(s) {
  if (s == null) return "";
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}
function escapeAttr(s) { return escapeHtml(s); }
