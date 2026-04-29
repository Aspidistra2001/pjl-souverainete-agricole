/* ============================================================
 * Bulletin de veille — moteur
 * ============================================================
 *
 * Parcours :
 *   1. Charge data/amendments.json (baseline locale figée).
 *   2. Tente de récupérer le flux RSS d'aspidistra2001.github.io/AN/feed.
 *   3. Détecte les amendements nouveaux (non présents dans la baseline)
 *      et les changements de "sort" pour les amendements existants.
 *   4. Met à jour l'affichage et recommence toutes les 10 minutes.
 *
 * Le flux peut être au format RSS 2.0 ou Atom : les deux sont parsés.
 * ============================================================ */

const FEED_URL = "https://aspidistra2001.github.io/AN/feed";
const REFRESH_INTERVAL_MS = 10 * 60 * 1000; // 10 minutes
const STORAGE_KEY = "veille_pjl_2632_state";

// Proxys CORS — ordre d'essai. Les services gratuits étant instables,
// on en a plusieurs en secours. Le premier qui répond gagne.
const CORS_FALLBACKS = [
  url => `https://corsproxy.io/?${encodeURIComponent(url)}`,
  url => `https://api.codetabs.com/v1/proxy?quest=${encodeURIComponent(url)}`,
  url => `https://api.allorigins.win/raw?url=${encodeURIComponent(url)}`,
  url => `https://cors-anywhere.herokuapp.com/${url}`,
];

// Cache local des XMLs déjà fetchés pour éviter de retaper les proxys en boucle
// (clé : URL du XML, valeur : { result, timestamp })
const xmlCache = new Map();
const XML_CACHE_TTL_MS = 30 * 60 * 1000; // 30 min — au-delà on refait le fetch


// État applicatif
const state = {
  meta: null,                  // métadonnées (groupes, ordre des articles)
  baseline: [],                // 545 amendements baseline
  byNum: new Map(),            // index num → amendment (comprend mises à jour RSS)
  authors: null,               // index des députés (group_map + lookup par nom de famille)
  filters: {
    search: "",
    states: new Set(),         // états actifs
    groups: new Set(),         // groupes actifs
    articles: new Set(),       // articles actifs
  },
  rssDetected: {
    newAmendments: [],         // numéros détectés via RSS et absents de la baseline
    stateChanges: [],          // {num, oldState, newState}
  },
  lastFetch: null,
  feedAvailable: false,
};

const dom = {};

// ------------------------------------------------------------
// Boot
// ------------------------------------------------------------

document.addEventListener("DOMContentLoaded", async () => {
  cacheDom();
  bindEvents();

  setStatus("loading", "Chargement des données…");

  try {
    const [data, authors] = await Promise.all([loadBaseline(), loadAuthors()]);
    state.meta = data.meta;
    state.baseline = data.amendments;
    state.byNum = new Map(state.baseline.map(a => [a.num, { ...a }]));
    state.authors = authors;

    // Pré-remplir filtres avec tous les états/groupes/articles disponibles
    initFilters();

    render();
    setStatus("ok", "Baseline locale chargée");

    // Lancement immédiat puis répétition
    refreshFromFeed();
    setInterval(refreshFromFeed, REFRESH_INTERVAL_MS);
  } catch (err) {
    console.error("Erreur de chargement de la baseline :", err);
    setStatus("error", "Échec du chargement");
    dom.amendments.innerHTML =
      `<p class="no-results">Erreur de chargement de la baseline. Vérifiez la console.</p>`;
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
  dom.refreshBtn.addEventListener("click", () => refreshFromFeed(true));
  dom.search.addEventListener("input", e => {
    state.filters.search = e.target.value.trim().toLowerCase();
    render();
  });
}

// ------------------------------------------------------------
// Chargement baseline
// ------------------------------------------------------------

async function loadBaseline() {
  const res = await fetch("data/amendments.json", { cache: "no-cache" });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

async function loadAuthors() {
  const res = await fetch("data/authors.json", { cache: "no-cache" });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

/**
 * Cherche le groupe parlementaire d'un auteur à partir d'un fragment libre
 * extrait du flux RSS (peut être "M. POTIER", "Mme Pantel Sophie", "Potier"...).
 * Retourne { full, group } ou null si introuvable / ambigu.
 */
function lookupAuthor(rawAuthor) {
  if (!rawAuthor || !state.authors) return null;
  const norm = stripAccents(rawAuthor).toLowerCase().trim();

  // 1. Nettoyer les civilités
  let cleaned = norm
    .replace(/^(m\.?|mme\.?|mlle\.?|mr\.?|monsieur|madame|mademoiselle)\s+/i, "")
    .replace(/\s+/g, " ")
    .trim();
  if (!cleaned) return null;

  // 2. Match exact normalisé ("pantel sophie" ou "sophie pantel")
  if (state.authors.by_normalized[cleaned]) {
    return state.authors.by_normalized[cleaned];
  }

  // 3. Match par nom de famille
  // On essaie plusieurs tokens : le premier, le dernier, et après une particule
  const tokens = cleaned.split(" ");
  const PARTICLES = new Set(["de", "le", "la", "du", "des", "van", "von", "d'", "saint"]);
  const candidates = new Set();
  candidates.add(tokens[0]);
  candidates.add(tokens[tokens.length - 1]);
  // Si le premier est une particule, essayer aussi le second
  if (PARTICLES.has(tokens[0]) && tokens.length >= 2) {
    candidates.add(tokens[1]);
  }

  for (const tok of candidates) {
    if (!tok) continue;
    const matches = state.authors.by_lastname[tok];
    if (!matches) continue;
    if (matches.length === 1) {
      return matches[0];
    }
    // Plusieurs candidats : on essaie de désambiguïser par les autres tokens
    for (const cand of matches) {
      const candTokens = stripAccents(cand.full).toLowerCase().split(" ");
      // Tous les tokens significatifs (>2 lettres) du candidat doivent être dans cleaned
      const allMatch = candTokens
        .filter(w => w.length > 2 && !PARTICLES.has(w))
        .every(w => cleaned.includes(w));
      if (allMatch) return cand;
    }
  }

  return null;
}

function stripAccents(s) {
  return s.normalize("NFD").replace(/[\u0300-\u036f]/g, "");
}

// ------------------------------------------------------------
// RSS — fetch et parsing
// ------------------------------------------------------------

async function refreshFromFeed(manual = false) {
  if (manual) dom.refreshBtn.setAttribute("disabled", "");
  setStatus("loading", "Lecture du flux RSS…");

  let xmlText = null;
  let lastError = null;

  // Tentative directe
  try {
    xmlText = await fetchText(FEED_URL);
  } catch (err) {
    lastError = err;
  }

  // Fallbacks CORS
  if (!xmlText) {
    for (const proxy of CORS_FALLBACKS) {
      try {
        xmlText = await fetchText(proxy(FEED_URL));
        if (xmlText) break;
      } catch (err) {
        lastError = err;
      }
    }
  }

  dom.refreshBtn.removeAttribute("disabled");

  if (!xmlText) {
    state.feedAvailable = false;
    setStatus("offline", "Flux RSS injoignable");
    if (manual) {
      console.warn("Flux indisponible :", lastError);
    }
    return;
  }

  try {
    const items = parseFeed(xmlText);
    await applyFeedItems(items);
    state.feedAvailable = true;
    state.lastFetch = new Date();
    setStatus("ok", `Flux RSS — ${items.length} entrée${items.length > 1 ? "s" : ""}`);
    updateLastFetchLabel();
    render();
  } catch (err) {
    console.error("Parsing RSS échoué :", err);
    setStatus("error", "Flux RSS — format inattendu");
  }
}

async function fetchText(url) {
  const res = await fetch(url, { cache: "no-cache" });
  if (!res.ok) throw new Error(`HTTP ${res.status} sur ${url}`);
  return res.text();
}

/**
 * Parse un flux RSS 2.0 ou Atom et renvoie un tableau uniforme :
 *   [{ num, title, content, link, pubDate, raw }]
 */
function parseFeed(xmlText) {
  const parser = new DOMParser();
  const doc = parser.parseFromString(xmlText, "application/xml");

  const parserError = doc.querySelector("parsererror");
  if (parserError) throw new Error("XML invalide");

  const root = doc.documentElement;
  const tag = root.tagName.toLowerCase();

  let items;
  if (tag === "rss") {
    items = Array.from(doc.querySelectorAll("channel > item"));
  } else if (tag === "feed") {
    items = Array.from(doc.querySelectorAll("feed > entry"));
  } else {
    items = Array.from(doc.querySelectorAll("item, entry"));
  }

  return items.map(node => extractItem(node)).filter(Boolean);
}

function extractItem(node) {
  const title = textOf(node, "title") || "";
  const description = textOf(node, "description") || textOf(node, "summary") || "";
  const content =
    textOf(node, "content\\:encoded") ||
    textOf(node, "content") ||
    description;
  const linkText = textOf(node, "link");
  let link = linkText;
  if (!link) {
    // Atom : <link href="..."/>
    const linkEl = node.querySelector("link[href]");
    if (linkEl) link = linkEl.getAttribute("href");
  }
  const guid = textOf(node, "guid") || textOf(node, "id") || "";
  const pubDate = textOf(node, "pubDate") || textOf(node, "updated") || textOf(node, "published") || "";

  // Extraire le numéro d'amendement depuis : titre, lien, GUID ou contenu
  // Patrons typiques : CD3, CD123, CE56, AS401, SPE862…
  const blob = `${title} ${link} ${guid} ${description}`;
  const m = blob.match(/\b([A-Z]{2,4}\d+)\b/);
  if (!m) return null;

  // Filtre : ne retenir que les amendements rattachés au texte n° 2632
  // (PJL souveraineté agricoles). Le numéro de texte apparaît dans l'URL
  // du lien, dans le GUID OpenData, ou dans le titre.
  const isText2632 =
    /\b2632\b/.test(blob) ||
    /\/2632\//.test(link) ||
    /B2632P/.test(guid);
  if (!isText2632) return null;

  // Chercher l'URL du XML OpenData : présente soit dans <guid>, soit dans <description>
  // Format typique : https://www.assemblee-nationale.fr/dyn/opendata/AMANR5L17PO...XX.xml
  let xmlUrl = null;
  const xmlPatterns = [
    /https?:\/\/[^"'<>\s]+AMANR[A-Z0-9]+\.xml/i,
    /(\/dyn\/opendata\/AMANR[A-Z0-9]+\.xml)/i,
  ];
  const searchSpace = `${guid} ${description} ${content}`;
  for (const re of xmlPatterns) {
    const xm = searchSpace.match(re);
    if (xm) {
      xmlUrl = xm[0];
      if (xmlUrl.startsWith("/")) xmlUrl = "https://www.assemblee-nationale.fr" + xmlUrl;
      break;
    }
  }
  // Si le guid est exactement une URI sans .xml, ajouter .xml
  if (!xmlUrl && guid.includes("AMANR") && !guid.endsWith(".xml")) {
    xmlUrl = guid.endsWith("/") ? guid.slice(0, -1) + ".xml" : guid + ".xml";
  }

  const item = {
    num: m[1],
    title,
    description,
    content,
    link: link || "",
    pubDate,
    xmlUrl,                         // URL du XML OpenData officiel, si trouvée
    sort: extractSort(title, content, description),
    author: extractAuthor(title, content),
  };

  return item;
}

function textOf(node, tag) {
  // Gère namespaces simples (content\:encoded, dc\:creator…)
  const el = node.querySelector(tag);
  return el ? el.textContent.trim() : "";
}

/**
 * Tente d'extraire un "sort" (état d'avancement) depuis le contenu textuel.
 * Mots-clés cherchés : Adopté, Rejeté, Retiré, Tombé, Irrecevable, A discuter,
 * En traitement, Discuté…
 */
function extractSort(title, content, description) {
  const blob = `${title}\n${description}\n${content}`.toLowerCase();
  // Sort final (après vote en commission)
  if (/\badopt[ée]\b/.test(blob)) return "Adopté";
  if (/\brejet[ée]\b/.test(blob)) return "Rejeté";
  if (/\btomb[ée]\b/.test(blob)) return "Tombé";
  // États intermédiaires
  if (/\birrecevable\s*40\b/.test(blob)) return "Irrecevable 40";
  if (/\birrecevable\b/.test(blob)) return "Irrecevable";
  if (/\bretir[ée]\b/.test(blob)) return "Retiré";
  if (/\bdiscut[ée]\b/.test(blob)) return "Discuté";
  if (/\bà discuter\b|\ba discuter\b/.test(blob)) return "A discuter";
  if (/\ben traitement\b/.test(blob)) return "En traitement";
  return null;
}

function extractAuthor(title, content) {
  const blob = (title + " " + content).slice(0, 2000);
  // Patterns successifs, du plus précis au plus large
  const patterns = [
    // "Auteur(s) : M. Potier, Mme Pantel..."
    /Auteur\(?s?\)?\s*:?\s*(M(?:\.|me|lle)?\.?\s+[\wÀ-ÿ'\- ]+?)(?:[,;]|$)/i,
    // "M. POTIER" ou "Mme PANTEL Sophie"
    /\b(M(?:\.|me|lle|r)?\.?\s+[A-ZÀ-Ý][\wÀ-ÿ'\-]+(?:\s+[A-Za-zÀ-ÿ][\wÀ-ÿ'\-]+)?)/,
    // "présenté par Pantel Sophie"
    /(?:présenté|déposé)\s+par\s+([A-ZÀ-Ý][\wÀ-ÿ'\- ]+?)(?:[,.;]|$)/i,
  ];
  for (const re of patterns) {
    const m = blob.match(re);
    if (m) return m[1].trim().replace(/\s+/g, " ");
  }
  return "";
}

// ------------------------------------------------------------
// Application des données du flux à l'état
// ------------------------------------------------------------

// Mapping des codes <groupePolitiqueRef> XML → code de groupe utilisé par notre UI
const XML_GROUP_MAP = {
  "PO845401": "RN",
  "PO845407": "EPR",
  "PO845413": "LFI",
  "PO845419": "SOC",
  "PO845425": "DR",
  "PO845439": "EcoS",
  "PO845454": "DEM",
  "PO845470": "HOR",
  "PO845485": "LIOT",
  "PO872880": "UDR",
};

const XML_NS = "http://schemas.assemblee-nationale.fr/referentiel";

/**
 * Fetch et parse le XML OpenData officiel d'un amendement.
 * Renvoie { author, group, article, state, dispositifText, exposeText, libelle, rapporteur }
 * ou null en cas d'échec.
 *
 * Le serveur de l'Assemblée nationale ne renvoie pas les en-têtes CORS,
 * donc le fetch direct depuis un navigateur tiers échoue toujours.
 * On commence donc par les proxys CORS, et on met les résultats en cache
 * pour éviter de retenter les mêmes requêtes à chaque cycle de rafraîchissement.
 */
async function fetchAmendmentXml(url) {
  if (!url) return null;

  // Cache : si on a déjà fetché cette URL récemment, on retourne le résultat
  const cached = xmlCache.get(url);
  if (cached && (Date.now() - cached.timestamp) < XML_CACHE_TTL_MS) {
    return cached.result;
  }

  let xmlText = null;
  // Stratégie proxy-first (le fetch direct vers assemblee-nationale.fr est
  // toujours bloqué par CORS, inutile de le tenter)
  for (const proxy of CORS_FALLBACKS) {
    try {
      xmlText = await fetchText(proxy(url));
      if (xmlText && xmlText.includes("<amendement")) break;
      xmlText = null;
    } catch (_) {
      // proxy suivant
    }
  }

  if (!xmlText) {
    xmlCache.set(url, { result: null, timestamp: Date.now() });
    return null;
  }

  let doc;
  try {
    doc = new DOMParser().parseFromString(xmlText, "application/xml");
  } catch (e) {
    return null;
  }
  if (doc.querySelector("parsererror")) return null;

  const ns = (tag) => doc.getElementsByTagNameNS(XML_NS, tag);
  const firstText = (tag) => {
    const els = ns(tag);
    return els.length && els[0].textContent ? els[0].textContent.trim() : "";
  };
  const firstTextWithin = (parent, tag) => {
    if (!parent) return "";
    const el = parent.getElementsByTagNameNS(XML_NS, tag)[0];
    return el && el.textContent ? el.textContent.trim() : "";
  };

  // Auteur principal & groupe
  const auteur = ns("auteur")[0];
  const groupRef = firstTextWithin(auteur, "groupePolitiqueRef");
  const group = XML_GROUP_MAP[groupRef] || null;
  // Détection du statut rapporteur
  let isRapp = false;
  if (auteur) {
    const rappEl = auteur.getElementsByTagNameNS(XML_NS, "auteurRapporteurOrganeRef")[0];
    isRapp = !!(rappEl && rappEl.textContent && rappEl.textContent.trim());
  }

  // Libellé en clair (auteur principal + cosignataires)
  const sigBlock = ns("signataires")[0];
  const libelleEl = sigBlock ? sigBlock.getElementsByTagNameNS(XML_NS, "libelle")[0] : null;
  const libelle = libelleEl ? cleanXmlText(libelleEl.textContent) : "";
  // Auteur principal = premier nom du libellé
  let author = "";
  if (libelle) {
    const first = libelle.split(",")[0].trim();
    author = first.replace(/^(M\.?|Mme\.?|Mlle\.?)\s+/i, "").trim();
  }

  // Article ciblé
  const div = ns("division")[0];
  const articleTitre = firstTextWithin(div, "titre");
  const avantApres = firstTextWithin(div, "avant_A_Apres");
  const additionnel = firstTextWithin(div, "articleAdditionnel") === "true";
  const article = formatArticle(articleTitre, avantApres, additionnel);

  // État et sort : si le sort officiel est rempli, c'est lui qui prime ;
  // sinon on prend l'état du traitement administratif.
  const etatLib = (() => {
    const etats = ns("etatDesTraitements");
    if (!etats.length) return "";
    const lib = etats[0].getElementsByTagNameNS(XML_NS, "libelle")[0];
    return lib && lib.textContent ? lib.textContent.trim() : "";
  })();
  const sortLib = firstText("sort") || "";
  const state = sortLib || etatLib || "En traitement";

  // Dispositif et exposé — on récupère directement le texte (entités HTML décodées)
  const dispositifEl = ns("dispositif")[0];
  const dispositifText = dispositifEl ? cleanXmlText(dispositifEl.textContent) : "";
  const exposeEl = ns("exposeSommaire")[0];
  const exposeText = exposeEl ? cleanXmlText(exposeEl.textContent) : "";

  const result = {
    author,
    group,
    article,
    state,
    rapporteur: isRapp,
    libelle,
    dispositifText,
    exposeText,
  };
  xmlCache.set(url, { result, timestamp: Date.now() });
  return result;
}

/**
 * Nettoie un texte issu du XML officiel : décode &#160; → espace,
 * normalise les espaces, retire les balises HTML résiduelles.
 */
function cleanXmlText(s) {
  if (!s) return "";
  // Le textContent décode déjà les entités numériques sur la plupart des navigateurs,
  // mais on force pour les cas où les entités sont doublement échappées.
  let t = s
    .replace(/&#160;|&#xa0;|&nbsp;/gi, " ")
    .replace(/&#(\d+);/g, (_, n) => String.fromCharCode(parseInt(n, 10)))
    .replace(/&#x([0-9a-f]+);/gi, (_, n) => String.fromCharCode(parseInt(n, 16)))
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&apos;/g, "'");
  // Retirer les balises HTML qui ont pu rester
  t = t.replace(/<[^>]+>/g, " ");
  // Normaliser les espaces
  return t.replace(/\s+/g, " ").trim();
}

function formatArticle(titre, avantApres, additionnel) {
  if (!titre) return "À classer";
  if (!additionnel) return titre;
  // Article additionnel : titre = article de référence, avant_A_Apres = "B" (avant) ou "A" (après)
  const m = titre.match(/Article\s+(\d+|PREMIER)/i);
  if (!m) return titre;
  const n = m[1];
  if (avantApres === "B") {
    return n === "PREMIER" ? "Avant l'article 1ᵉʳ" : `Avant l'article ${n}`;
  }
  return n === "PREMIER" ? "Après l'article PREMIER" : `Après l'article ${n}`;
}

async function applyFeedItems(items) {
  state.rssDetected.newAmendments = [];
  state.rssDetected.stateChanges = [];

  // Le flux RSS d'Aspidistra ne contient pas l'état dans son XML, juste un lien
  // vers le XML OpenData officiel. Pour CHAQUE entrée du flux (qu'elle soit
  // déjà connue ou non), on fetch l'XML pour avoir l'état réel et à jour.
  // En pratique, le flux ne liste que les amendements récents/modifiés, donc
  // on enrichit aussi les amendements connus quand ils réapparaissent dans le flux.

  const toEnrich = [];

  for (const item of items) {
    const existing = state.byNum.get(item.num);

    if (!existing) {
      // Nouvel amendement → placeholder, à enrichir via XML officiel
      const placeholder = {
        num: item.num,
        article: "À classer",
        author: "Chargement…",
        group: "EPR",
        group_resolved: false,
        rapporteur: false,
        state: item.sort || "En traitement",
        url: item.link || "",
        instance: item.num.startsWith("CE") ? "Affaires économiques" :
                  item.num.startsWith("AS") ? "Affaires sociales" :
                  item.num.startsWith("CD") ? "Développement durable" : "",
        summary: "Récupération du contenu officiel en cours…",
        summary_pending: true,
        is_new: true,
        is_rss_new: true,
        rss_link: item.link,
        xml_url: item.xmlUrl,
      };
      state.byNum.set(item.num, placeholder);
      state.rssDetected.newAmendments.push(item.num);
      toEnrich.push({ item, isNew: true });
    } else {
      // Amendement connu : on l'enrichit aussi pour rafraîchir son état/sort.
      // On note l'ancien état pour détecter le changement après enrichissement.
      toEnrich.push({ item, isNew: false, oldState: existing.state });
    }
  }

  if (toEnrich.length === 0) return;

  // Render immédiat avec les placeholders
  render();

  const CONCURRENCY = 5;
  let idx = 0;
  let successCount = 0;
  let failureCount = 0;
  async function worker() {
    while (idx < toEnrich.length) {
      const job = toEnrich[idx++];
      const { item, isNew, oldState } = job;
      if (!item.xmlUrl) { failureCount++; continue; }
      try {
        const enriched = await fetchAmendmentXml(item.xmlUrl);
        if (!enriched) { failureCount++; continue; }
        successCount++;
        const a = state.byNum.get(item.num);
        if (!a) continue;

        if (isNew) {
          if (enriched.author) a.author = enriched.author;
          if (enriched.group) {
            a.group = enriched.group;
            a.group_resolved = true;
          }
          if (enriched.article) a.article = enriched.article;
          a.rapporteur = enriched.rapporteur;
          if (enriched.exposeText) {
            a.summary = enriched.exposeText.length > 500
              ? enriched.exposeText.slice(0, 500).replace(/\s+\S*$/, "") + "…"
              : enriched.exposeText;
          } else if (enriched.dispositifText) {
            a.summary = enriched.dispositifText.length > 500
              ? enriched.dispositifText.slice(0, 500).replace(/\s+\S*$/, "") + "…"
              : enriched.dispositifText;
          }
          a.libelle = enriched.libelle;
        }

        // Mise à jour de l'état dans tous les cas (nouveau ou connu)
        if (enriched.state && enriched.state !== a.state) {
          if (!isNew) {
            state.rssDetected.stateChanges.push({
              num: item.num,
              oldState: oldState || a.state,
              newState: enriched.state,
            });
            a.previous_state = a.state;
            a.is_changed = true;
          }
          a.state = enriched.state;
        }
      } catch (err) {
        failureCount++;
        console.warn(`Enrichissement XML échoué pour ${item.num} :`, err);
      }
    }
  }

  const workers = Array.from({ length: Math.min(CONCURRENCY, toEnrich.length) }, () => worker());
  await Promise.all(workers);

  // Statut détaillé pour aider au diagnostic
  if (failureCount > 0 && successCount === 0) {
    setStatus("error", `Enrichissement XML — ${failureCount} échec${failureCount > 1 ? "s" : ""} (proxys CORS indisponibles)`);
  } else if (failureCount > 0) {
    setStatus("ok", `Enrichi — ${successCount}/${successCount + failureCount} amendements`);
  }

  render();
}

function guessArticleFromContent(item) {
  const blob = `${item.title} ${item.content} ${item.description}`;
  const m = blob.match(/Article\s+(\d+|PREMIER|premier|1er)/i);
  if (!m) return null;
  const v = m[1].toLowerCase();
  if (v === "premier" || v === "1er") return "Article PREMIER";
  return `Article ${m[1]}`;
}

function stripHtml(html) {
  const tmp = document.createElement("div");
  tmp.innerHTML = html;
  return tmp.textContent.replace(/\s+/g, " ").trim();
}

// ------------------------------------------------------------
// Filtres & stats
// ------------------------------------------------------------

function initFilters() {
  // États : on prend les états présents
  const stateCounts = countBy(state.baseline, a => a.state);
  buildFilterRows(dom.stateFilters, Array.from(stateCounts.keys()), stateCounts, "state");

  // Groupes : ordre du meta (RN, EPR, …)
  const groupCounts = countBy(state.baseline, a => a.group);
  // Ordre par effectifs décroissants
  const groupOrder = Array.from(groupCounts.entries())
    .sort((a, b) => b[1] - a[1])
    .map(([k]) => k);
  buildFilterRows(dom.groupFilters, groupOrder, groupCounts, "group", true);

  // Articles : ordre canonique
  const articleCounts = countBy(state.baseline, a => a.article);
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
  renderStats();
  renderChangelog();
  renderAmendments();
}

function renderStats() {
  const all = Array.from(state.byNum.values());
  const newCount = all.filter(a => a.is_new || a.is_rss_new).length;
  const changedCount = state.rssDetected.stateChanges.length;
  // « Actifs » = en cours de procédure (pas encore retiré, irrecevable ou voté)
  const actifs = all.filter(a =>
    a.state === "En traitement" || a.state === "A discuter"
  ).length;
  // « Votés » = sort final atteint
  const votes = all.filter(a =>
    a.state === "Adopté" || a.state === "Rejeté" || a.state === "Tombé" || a.state === "Non soutenu"
  ).length;

  dom.stats.innerHTML = `
    <div class="stat"><span class="stat-num">${all.length}</span><span class="stat-label">Amendements</span></div>
    <div class="stat"><span class="stat-num accent">${newCount}</span><span class="stat-label">Nouveaux</span></div>
    <div class="stat"><span class="stat-num">${actifs}</span><span class="stat-label">Actifs</span></div>
    <div class="stat"><span class="stat-num">${votes}</span><span class="stat-label">Votés</span></div>
  `;
}

function renderChangelog() {
  const { newAmendments, stateChanges } = state.rssDetected;
  const has26AvrilNew = state.baseline.some(a => a.is_new);

  if (!state.feedAvailable && !has26AvrilNew) {
    dom.changelogBody.innerHTML = `<p class="changelog-empty">En attente d'actualisation du flux RSS…</p>`;
    return;
  }

  const parts = [];

  if (has26AvrilNew) {
    const baselineNew = state.baseline.filter(a => a.is_new).map(a => a.num);
    parts.push(
      `<p><strong>20 amendements</strong> ont été ajoutés au tableau officiel entre le 26 et le 28 avril 2026 : ` +
      baselineNew.map(n => `<code>${n}</code>`).join(", ") +
      `. Ces amendements sont signalés en <em>jaune</em> dans la liste ci-dessous.</p>`
    );
  }

  if (newAmendments.length > 0) {
    parts.push(
      `<p><strong>${newAmendments.length} nouvel${newAmendments.length > 1 ? "s amendements détectés" : " amendement détecté"} via le flux RSS</strong> ` +
      `(non présent${newAmendments.length > 1 ? "s" : ""} dans la baseline du 28 avril) : ` +
      newAmendments.map(n => `<code>${n}</code>`).join(", ") +
      `. Signalé${newAmendments.length > 1 ? "s" : ""} en <em>bleu</em>.</p>`
    );
  }

  if (stateChanges.length > 0) {
    parts.push(
      `<p><strong>${stateChanges.length} amendement${stateChanges.length > 1 ? "s" : ""}</strong> ` +
      `${stateChanges.length > 1 ? "ont changé d'état" : "a changé d'état"} depuis la baseline (signalé${stateChanges.length > 1 ? "s" : ""} en <em>orange</em>) :</p>` +
      `<ul class="changelog-list">` +
      stateChanges.map(c =>
        `<li><code>${c.num}</code> : <em>${c.oldState}</em> → <strong>${c.newState}</strong></li>`
      ).join("") +
      `</ul>`
    );
  }

  if (parts.length === 0) {
    dom.changelogBody.innerHTML = `<p class="changelog-empty">Aucune évolution détectée depuis la baseline.</p>`;
  } else {
    dom.changelogBody.innerHTML = parts.join("");
  }
}

function renderAmendments() {
  const filtered = filterAmendments();

  if (filtered.length === 0) {
    dom.amendments.innerHTML = `<p class="no-results">Aucun amendement ne correspond aux filtres en cours.</p>`;
    return;
  }

  // Regrouper par article
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
  if (a.is_new) classes.push("is-new");
  if (a.is_rss_new) classes.push("is-rss-new");
  if (a.is_changed) classes.push("is-changed");

  const url = a.url || a.rss_link || "#";
  const stateClass = stateCssClass(a.state);

  // Pour les résumés non encore synthétisés (extraits bruts du flux ou de l'exposé sommaire),
  // on affiche un bandeau d'avertissement et on stylise différemment.
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

  // Avertissement si le groupe parlementaire n'a pas pu être résolu depuis le flux
  const groupWarning = (a.is_rss_new && a.group_resolved === false)
    ? `<span class="badge badge-warning" title="Groupe non identifié — pastille indicative">groupe ?</span>`
    : "";

  return `
    <article class="${classes.join(" ")}">
      <a class="amendment-num" href="${escapeAttr(url)}" target="_blank" rel="noopener">${escapeHtml(a.num)}</a>
      <div class="amendment-body">
        <div class="amendment-meta">
          ${groupPill(a.group)}
          ${groupWarning}
          <span class="author">${escapeHtml(a.author)}</span>
          ${a.rapporteur ? `<span class="rapporteur-tag">(rapporteure)</span>` : ""}
          <span class="badge badge-state ${stateClass}">${escapeHtml(a.state)}</span>
          ${a.instance === "Affaires économiques" ? `<span class="badge badge-instance">Affaires éco.</span>` : ""}
          ${a.instance === "Affaires sociales" ? `<span class="badge badge-instance">Affaires soc.</span>` : ""}
          ${a.is_new && !a.is_rss_new ? `<span class="badge badge-new">NOUVEAU</span>` : ""}
          ${a.is_rss_new ? `<span class="badge badge-rss">FLUX</span>` : ""}
          ${a.is_changed ? `<span class="badge badge-changed">MODIFIÉ</span>` : ""}
        </div>
        ${summaryHtml}
      </div>
    </article>
  `;
}

function groupPill(group, count) {
  const meta = state.meta.groups[group];
  if (!meta) return `<span class="group-pill" style="background:#ddd;color:#333">${group}</span>`;
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

// ------------------------------------------------------------
// Filtrage
// ------------------------------------------------------------

function filterAmendments() {
  const all = Array.from(state.byNum.values());
  const { search, states, groups, articles } = state.filters;

  return all.filter(a => {
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

// ------------------------------------------------------------
// Statut & UI
// ------------------------------------------------------------

function setStatus(state, label) {
  dom.statusPill.dataset.state = state;
  dom.statusText.textContent = label;
}

function updateLastFetchLabel() {
  if (!state.lastFetch) return;
  const fmt = new Intl.DateTimeFormat("fr-FR", {
    hour: "2-digit", minute: "2-digit", day: "2-digit", month: "short"
  });
  dom.lastUpdate.textContent = `Dernière vérification : ${fmt.format(state.lastFetch)}`;
}

// ------------------------------------------------------------
// Helpers d'échappement HTML
// ------------------------------------------------------------

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
