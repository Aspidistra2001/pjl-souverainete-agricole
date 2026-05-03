#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Synchronise data/amendments.json avec le flux RSS Aspidistra et l'API OpenData
de l'Assemblée nationale.

Pour chaque entrée du flux RSS :
  - si l'amendement existe déjà dans la baseline, on met à jour son état/sort
    en fetchant son XML OpenData
  - s'il n'existe pas, on l'ajoute avec un résumé "à rédiger" extrait de
    l'exposé sommaire officiel

Le script est exécuté périodiquement par le workflow .github/workflows/refresh.yml.
Le code est volontairement défensif : aucun crash ne doit jamais bloquer une
exécution, mieux vaut écrire une mise à jour partielle qu'aucune.

Usage : python scripts/refresh_amendments.py
"""

from __future__ import annotations
import csv
import io
import json
import os
import re
import html
import sys
import time
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

# Permettre l'import du module summarize_amendments situé dans le même dossier
sys.path.insert(0, str(Path(__file__).resolve().parent))

# ----- Configuration -----

ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = ROOT / "data" / "amendments.json"
SYNC_STATUS_FILE = ROOT / "data" / "sync_status.json"

FEED_URL = "https://aspidistra2001.github.io/AN/feed"
TEXT_NUMBER = "2632"  # PJL souveraineté agricoles

# CSV OpenData officiel listant TOUS les amendements du dossier législatif.
# Beaucoup plus fiable que de découvrir les amendements via le flux RSS,
# qui ne signale que les changements récents.
# Le numéro 54085 est l'identifiant du dossier législatif PJL 2632 sur OpenData.
DOSSIER_LEG_ID = "54085"
DOSSIER_CSV_URL = f"http://data.assemblee-nationale.fr/static/openData/repository/17/dossiers_legislatifs_opendata/{DOSSIER_LEG_ID}/excel.csv"

USER_AGENT = "Mozilla/5.0 (compatible; veille-pjl-2632/1.0; +https://aspidistra2001.github.io/pjl-souverainete-agricole/)"
TIMEOUT = 15
MAX_WORKERS = 10

# Mapping des codes <groupePolitiqueRef> XML → code de groupe utilisé dans le bulletin
GROUP_REF_MAP = {
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
}

# Préfixes des organes de commission (le 2e bloc dans l'URI XML OpenData)
ORGANE_PREFIXES = {
    "CD": "PO419865",  # CION-DVP : Développement durable
    "CE": "PO419610",  # CION-ECO : Affaires économiques
    "AS": "PO420120",  # CION-SOC : Affaires sociales
}

XML_NS = "{http://schemas.assemblee-nationale.fr/referentiel}"


# ----- Helpers -----

def http_get(url: str) -> str | None:
    """GET texte avec User-Agent et timeout. Retourne None en cas d'échec."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            return resp.read().decode(charset, errors="replace")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        print(f"  GET {url} → {e}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"  GET {url} → erreur inattendue : {e}", file=sys.stderr)
        return None


def amendment_xml_url(num: str) -> str | None:
    """Construit l'URL du XML OpenData pour un numéro CD/CE/AS."""
    m = re.match(r"^([A-Z]+)(\d+)$", num)
    if not m:
        return None
    letters, digits = m.group(1), m.group(2).zfill(6)
    org = ORGANE_PREFIXES.get(letters)
    if not org:
        return None
    return f"https://www.assemblee-nationale.fr/dyn/opendata/AMANR5L17{org}B{TEXT_NUMBER}P0D1N{digits}.xml"


def find_text(elem: ET.Element, path: str) -> str | None:
    sub = elem.find(path)
    if sub is None or sub.text is None:
        return None
    return sub.text.strip() or None


def clean_text(s: str | None) -> str:
    """Décode entités HTML et normalise espaces."""
    if not s:
        return ""
    t = html.unescape(s)
    t = t.replace("\xa0", " ")  # espace insécable
    t = re.sub(r"<[^>]+>", " ", t)  # balises HTML résiduelles
    return re.sub(r"\s+", " ", t).strip()


def truncate(s: str, n: int = 500) -> str:
    if len(s) <= n:
        return s
    return s[:n].rsplit(" ", 1)[0] + "…"


# ----- Parsing du flux RSS -----

def parse_feed(feed_xml: str) -> list[dict]:
    """Parse le flux RSS d'Aspidistra et renvoie la liste des items.

    Chaque item : { num, link, xml_url, pub_date }.
    Filtre sur le texte 2632.
    """
    try:
        root = ET.fromstring(feed_xml)
    except ET.ParseError as e:
        print(f"Flux RSS invalide : {e}", file=sys.stderr)
        return []

    items = root.findall(".//item")
    if not items:
        # Atom
        items = root.findall(".//{http://www.w3.org/2005/Atom}entry")

    result = []
    for it in items:
        title = (it.findtext("title") or it.findtext("{http://www.w3.org/2005/Atom}title") or "").strip()
        link = (it.findtext("link") or "").strip()
        if not link:
            link_el = it.find("{http://www.w3.org/2005/Atom}link")
            if link_el is not None:
                link = link_el.get("href", "").strip()
        guid = (it.findtext("guid") or it.findtext("{http://www.w3.org/2005/Atom}id") or "").strip()
        descr = (it.findtext("description") or it.findtext("{http://www.w3.org/2005/Atom}content") or "").strip()
        pub_date = (it.findtext("pubDate") or it.findtext("{http://www.w3.org/2005/Atom}updated") or "").strip()

        blob = f"{title} {link} {guid} {descr}"
        m = re.search(r"\b([A-Z]{2,4}\d+)\b", blob)
        if not m:
            continue
        num = m.group(1)

        # Filtre sur le texte 2632
        if not (re.search(rf"\b{TEXT_NUMBER}\b", blob)
                or f"/{TEXT_NUMBER}/" in link
                or f"B{TEXT_NUMBER}P" in guid):
            continue

        # URL du XML OpenData : présent dans le guid (sans .xml) ou dans la description
        xml_url = None
        m2 = re.search(r"https?://[^\s\"'<>]+AMANR[A-Z0-9]+\.xml", blob)
        if m2:
            xml_url = m2.group(0)
        elif "AMANR" in guid:
            xml_url = guid.rstrip("/") + ".xml" if not guid.endswith(".xml") else guid
        if xml_url and xml_url.startswith("/"):
            xml_url = "https://www.assemblee-nationale.fr" + xml_url

        result.append({
            "num": num,
            "link": link,
            "xml_url": xml_url,
            "pub_date": pub_date,
        })

    return result


# ----- Parsing du XML OpenData officiel -----

def parse_amendment_xml(xml_text: str) -> dict | None:
    """Extrait les champs utiles d'un XML d'amendement OpenData."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None

    out: dict = {}

    # Numéro long
    out["num"] = find_text(root, f".//{XML_NS}numeroLong")

    # Article (division)
    div = root.find(f".//{XML_NS}division")
    if div is not None:
        titre = find_text(div, f"{XML_NS}titre") or ""
        avant_apres = find_text(div, f"{XML_NS}avant_A_Apres") or ""
        additionnel = (find_text(div, f"{XML_NS}articleAdditionnel") == "true")
        out["article"] = format_article(titre, avant_apres, additionnel)
    else:
        out["article"] = "À classer"

    # Auteur principal et groupe
    auteur = root.find(f".//{XML_NS}auteur")
    if auteur is not None:
        ref = find_text(auteur, f"{XML_NS}groupePolitiqueRef")
        out["group_ref"] = ref
        out["group"] = GROUP_REF_MAP.get(ref) if ref else None
        rapp_el = auteur.find(f"{XML_NS}auteurRapporteurOrganeRef")
        out["rapporteur"] = bool(rapp_el is not None and rapp_el.text and rapp_el.text.strip())

    # Libellé en clair (auteur principal + cosignataires)
    sig_libelle = root.find(f".//{XML_NS}signataires/{XML_NS}libelle")
    libelle = clean_text(sig_libelle.text if sig_libelle is not None else None)
    out["libelle"] = libelle
    if libelle:
        first = libelle.split(",")[0].strip()
        first_clean = re.sub(r"^(M\.?|Mme\.?|Mlle\.?)\s+", "", first).strip()
        out["author"] = first_clean

    # État et sort (priorité au sort si rempli)
    sort = find_text(root, f".//{XML_NS}cycleDeVie/{XML_NS}sort")
    etat_lib = find_text(root, f".//{XML_NS}etatDesTraitements/{XML_NS}etat/{XML_NS}libelle")
    out["state"] = sort or etat_lib or "En traitement"

    # Dispositif et exposé sommaire (texte propre)
    disp_el = root.find(f".//{XML_NS}dispositif")
    out["dispositif_text"] = clean_text(disp_el.text if disp_el is not None else "")
    expo_el = root.find(f".//{XML_NS}exposeSommaire")
    out["expose_text"] = clean_text(expo_el.text if expo_el is not None else "")

    return out


def format_article(titre: str, avant_apres: str, additionnel: bool) -> str:
    if not titre:
        return "À classer"
    if not additionnel:
        return titre
    m = re.search(r"Article\s+(\d+|PREMIER)", titre, re.IGNORECASE)
    if not m:
        return titre
    n = m.group(1)
    if avant_apres == "B":
        return "Avant l'article 1ᵉʳ" if n.upper() == "PREMIER" else f"Avant l'article {n}"
    return "Après l'article PREMIER" if n.upper() == "PREMIER" else f"Après l'article {n}"


# ----- Logique principale de synchronisation -----

def fetch_and_parse_xml(num: str, xml_url: str | None) -> tuple[str, dict | None]:
    """Worker thread : fetch + parse, renvoie (num, parsed_data | None)."""
    url = xml_url or amendment_xml_url(num)
    if not url:
        return num, None
    text = http_get(url)
    if not text:
        return num, None
    parsed = parse_amendment_xml(text)
    return num, parsed


def fetch_dossier_csv() -> list[dict] | None:
    """Récupère le CSV OpenData listant tous les amendements du dossier législatif.

    C'est la source de vérité officielle de l'Assemblée nationale.
    Beaucoup plus fiable que la découverte via flux RSS qui ne signale que
    les changements récents.

    Retourne une liste de dicts (un par amendement) ou None en cas d'échec.
    """
    try:
        req = urllib.request.Request(DOSSIER_CSV_URL, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=TIMEOUT * 2) as resp:
            raw = resp.read()
    except Exception as e:
        print(f"  CSV OpenData injoignable : {e}", file=sys.stderr)
        return None
    # Le fichier est encodé en cp1252 (Windows-1252)
    try:
        text = raw.decode("cp1252")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")
    rows = list(csv.DictReader(io.StringIO(text), delimiter=";"))
    return rows


def map_state_from_csv(sort_value: str) -> str:
    """Convertit la valeur 'Sort de l'amendement' du CSV en état affiché."""
    if not sort_value or sort_value == "Non renseigné":
        return "En traitement"
    return sort_value.strip()


def synchronize() -> dict:
    """Met à jour data/amendments.json à partir du CSV OpenData officiel.

    Stratégie (refonte du 30 avril) :
      1. Charger la baseline existante.
      2. Télécharger le CSV OpenData officiel listant TOUS les amendements
         du dossier législatif (source de vérité).
      3. Comparer baseline ↔ CSV :
         - Amendements en plus dans le CSV : à ajouter (et fetcher leur XML
           pour récupérer le groupe parlementaire et l'exposé sommaire).
         - Amendements dont le sort a changé : à mettre à jour avec horodatage.
      4. Le flux RSS reste comme système d'alerte rapide (mais redondant).

    Retourne un dict avec un résumé des changements pour le commit message.
    """
    print(f"=== Synchronisation lancée à {datetime.now(timezone.utc).isoformat()} ===")
    summary = {
        "added": [],
        "state_changes": [],
        "errors": [],
        "feed_items": 0,
        "fetched": 0,
        "csv_total": 0,
    }

    # 1. Charger la baseline
    if not DATA_FILE.exists():
        print(f"Erreur : {DATA_FILE} introuvable", file=sys.stderr)
        return summary
    with DATA_FILE.open(encoding="utf-8") as f:
        data = json.load(f)

    by_num = {a["num"]: a for a in data["amendments"]}
    print(f"Baseline locale : {len(by_num)} amendements")

    # 2. Télécharger le CSV OpenData officiel (source de vérité)
    csv_rows = fetch_dossier_csv()
    if csv_rows is None:
        print("Pas de CSV → fallback sur le flux RSS uniquement", file=sys.stderr)
        summary["errors"].append("CSV OpenData injoignable")
        csv_rows = []
    else:
        summary["csv_total"] = len(csv_rows)
        print(f"CSV OpenData : {len(csv_rows)} amendements officiels")

    # Indexer le CSV par numéro
    csv_by_num = {}
    for row in csv_rows:
        num = (row.get("Numéro de l'amendement") or "").strip()
        if num:
            csv_by_num[num] = row

    # 3. Comparer : changements d'état
    for num, csv_row in csv_by_num.items():
        existing = by_num.get(num)
        if not existing:
            continue
        new_state = map_state_from_csv(csv_row.get("Sort de l'amendement", ""))
        if new_state != existing["state"]:
            old_state = existing["state"]
            existing["previous_state"] = old_state
            existing["state"] = new_state
            existing["state_changed_at"] = datetime.now(timezone.utc).isoformat()
            summary["state_changes"].append({
                "num": num, "old": old_state, "new": new_state
            })
            print(f"  [~] {num} : {old_state} → {new_state}")

    # 4. Identifier les amendements présents dans le CSV mais pas dans la baseline
    csv_only = sorted(set(csv_by_num) - set(by_num))
    print(f"Amendements à ajouter (CSV mais pas en local) : {len(csv_only)}")

    # 5. Pour chaque amendement à ajouter, fetcher son XML pour récupérer
    #    le groupe parlementaire et l'exposé sommaire (le CSV ne donne pas
    #    ces informations détaillées)
    if csv_only:
        print(f"Fetch en parallèle des XML pour les {len(csv_only)} nouveaux amendements...")
        enriched: dict[str, dict] = {}
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futures = {ex.submit(fetch_and_parse_xml, num, None): num for num in csv_only}
            for fut in as_completed(futures):
                num, parsed = fut.result()
                if parsed:
                    enriched[num] = parsed
                    summary["fetched"] += 1

        for num in csv_only:
            csv_row = csv_by_num[num]
            parsed = enriched.get(num, {})

            instance = (csv_row.get("Instance") or "").strip()
            article = (csv_row.get("Désignation de l'article") or "À classer").strip()
            url = (csv_row.get("URL Amendement") or "").strip()
            state = map_state_from_csv(csv_row.get("Sort de l'amendement", ""))
            author = (csv_row.get("Auteur") or "Auteur non identifié").strip()
            # Retirer les marqueurs "rapporteur" du nom (toutes formes)
            is_rapporteur = bool(re.search(r"rapporteur", author, re.IGNORECASE))
            # Formes longues à retirer en premier (mention complète)
            author = re.sub(
                r"\s+rapporteur(e)?\s+(pour\s+avis\s+)?(au\s+nom\s+de\s+la\s+commission.*)?$",
                "", author, flags=re.IGNORECASE
            ).strip()
            # Forme courte résiduelle
            author = re.sub(r"\s+rapporteur(e)?\s*$", "", author, flags=re.IGNORECASE).strip()

            # Données fines via XML (groupe, résumé)
            group = parsed.get("group") or "EPR"
            summary_text = parsed.get("expose_text") or parsed.get("dispositif_text") or "Détails non disponibles."

            new_amend = {
                "num": num,
                "article": article,
                "author": author,
                "group": group,
                "rapporteur": is_rapporteur,
                "state": state,
                "url": url,
                "instance": instance,
                "summary": truncate(summary_text, 500),
                "summary_pending": True,
                "is_new": True,
                "is_rss_new": True,
                "added_via_csv_at": datetime.now(timezone.utc).isoformat(),
            }
            data["amendments"].append(new_amend)
            by_num[num] = new_amend
            summary["added"].append(num)
            print(f"  [+] {num} ajouté ({group} – {author})")

    # 6. Vérification : amendements dans la baseline mais absents du CSV
    #    (= amendements supprimés / fusionnés / non publiés sur OpenData)
    local_only = sorted(set(by_num) - set(csv_by_num))
    if local_only:
        print(f"⚠ {len(local_only)} amendements locaux non présents dans le CSV : {local_only[:5]}…", file=sys.stderr)
        summary["errors"].append(f"{len(local_only)} amendements locaux manquants au CSV")

    # 7. Flux RSS — reste utilisé comme signal complémentaire
    feed_xml = http_get(FEED_URL)
    if feed_xml:
        feed_items = parse_feed(feed_xml)
        summary["feed_items"] = len(feed_items)
        print(f"Flux RSS (info) : {len(feed_items)} items")

    # 8. Génération des résumés via l'API Claude (Haiku 4.5).
    #    On ne traite que les amendements avec summary_pending=True
    #    (les autres ont déjà un résumé rédigé manuellement ou par un appel API précédent).
    pending = [a for a in data["amendments"] if a.get("summary_pending")]
    if pending:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if api_key:
            try:
                # Import local pour éviter une erreur si le module n'est pas là
                from summarize_amendments import summarize_amendments
                # max_per_run=50 pour limiter les coûts par sync
                # (~50 × 0,003 USD = 0,15 USD max par sync, environ 1500 amendements/mois max)
                api_stats = summarize_amendments(pending, api_key, max_per_run=50)
                summary["summaries_generated"] = api_stats.get("generated", 0)
                summary["summaries_cost_usd"] = api_stats.get("estimated_cost_usd", 0)
            except Exception as e:
                print(f"⚠ Génération de résumés échouée : {e}", file=sys.stderr)
                summary.setdefault("errors", []).append(f"Résumés API : {e}")
        else:
            print(f"  {len(pending)} amendements en attente de résumé (ANTHROPIC_API_KEY non définie)")

    # 8 bis. Tagging thématique (coop, ab, animale, végétale) — système hybride.
    #
    #    Étape 1 : tags depuis les bulletins manuels (data/tags_manual.json).
    #              Tous les amendements CE listés y reçoivent leurs tags + tags_source="manual".
    #              Instantané, gratuit, prioritaire.
    #
    #    Étape 2 : fallback Claude pour les amendements CE qui n'ont AUCUN tags_source.
    #              Cela couvre les nouveaux amendements déposés depuis la publication des
    #              derniers bulletins. ~0,001 USD par amendement, max 50 par sync.
    try:
        from tag_from_bulletins import apply_manual_tags
        # force=True pour rattraper si bulletins ont changé. Les amendements taggés Claude
        # antérieurement sont préservés par la logique interne (cf. tag_from_bulletins.py).
        manual_stats = apply_manual_tags(data["amendments"], force=True)
        summary["tags_manual_applied"] = manual_stats.get("tagged", 0)
    except Exception as e:
        print(f"⚠ Tagging manuel échoué : {e}", file=sys.stderr)
        summary.setdefault("errors", []).append(f"Tagging manuel : {e}")
        manual_stats = {}

    # Étape 2 : Claude en fallback sur les CE non couverts par les bulletins
    #          ET pas encore tagués par Claude (pas de tags_source du tout)
    #          ET ayant un résumé non-pending (sinon le tagging serait peu fiable)
    needs_claude = [
        a for a in data["amendments"]
        if a.get("num", "").startswith("CE")
        and "tags_source" not in a
        and not a.get("summary_pending")
    ]
    if needs_claude:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if api_key:
            try:
                from tag_amendments import tag_amendments
                # max_per_run=50 : généralement très peu de nouveaux amendements à taguer
                # (ceux non couverts par les bulletins). Coût plafonné à ~0,05 USD par sync.
                api_stats = tag_amendments(needs_claude, api_key, max_per_run=50)
                summary["tags_claude_applied"] = api_stats.get("tagged", 0)
                summary["tags_cost_usd"] = api_stats.get("estimated_cost_usd", 0)
            except Exception as e:
                print(f"⚠ Fallback Claude échoué : {e}", file=sys.stderr)
                summary.setdefault("errors", []).append(f"Tagging Claude : {e}")
        else:
            print(f"  {len(needs_claude)} amendements CE non couverts par bulletins en attente de tagging Claude (ANTHROPIC_API_KEY non définie)")

    # 8 ter. Application des avis CD (rapporteure, gouvernement, sort en commission)
    #    à partir de data/avis_cd.json, extrait du compte-rendu officiel de la
    #    commission Développement durable. Instantané, gratuit, mise à jour
    #    chaque fois que avis_cd.json est mis à jour.
    try:
        from apply_avis_cd import apply_avis_cd
        avis_stats = apply_avis_cd(data["amendments"])
        summary["avis_cd_applied"] = avis_stats.get("applied", 0)
    except Exception as e:
        print(f"⚠ Application avis CD échouée : {e}", file=sys.stderr)
        summary.setdefault("errors", []).append(f"Avis CD : {e}")

    # 8 quater. Application des jumeaux CD↔CE (sort des amendements similaires
    #    déjà examinés en commission Développement durable). Ces données permettent
    #    d'anticiper le sort probable des amendements CE qui ont un jumeau CD.
    try:
        from apply_jumeaux import apply_jumeaux
        jumeaux_stats = apply_jumeaux(data["amendments"])
        summary["jumeaux_applied"] = jumeaux_stats.get("applied", 0)
    except Exception as e:
        print(f"⚠ Application jumeaux échouée : {e}", file=sys.stderr)
        summary.setdefault("errors", []).append(f"Jumeaux CD↔CE : {e}")

    # 9. Mettre à jour la métadonnée
    data["meta"]["last_sync"] = datetime.now(timezone.utc).isoformat()
    data["meta"]["total"] = len(data["amendments"])

    # 10. Écrire le fichier
    with DATA_FILE.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))

    # 10 bis. Régénérer l'export Excel téléchargeable depuis le site.
    # Ce fichier est mis à jour à chaque sync pour rester en cohérence
    # avec amendments.json. Si openpyxl n'est pas installé, on saute
    # silencieusement (le site continue de marcher sans l'export).
    try:
        from generate_excel import main as generate_xlsx
        generate_xlsx()
    except ImportError:
        print("  (openpyxl non disponible, export Excel sauté)", file=sys.stderr)
    except Exception as e:
        print(f"  ⚠ Génération Excel échouée : {e}", file=sys.stderr)
        summary.setdefault("errors", []).append(f"Excel : {e}")

    # 11. TOUJOURS écrire le fichier de statut de synchro (même si rien n'a changé)
    # Ce fichier est committé indépendamment d'amendments.json pour prouver
    # que le script tourne, même quand il n'y a aucune modification de fond.
    sync_status = {
        "last_sync_utc": datetime.now(timezone.utc).isoformat(),
        "last_sync_human": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "feed_items": summary["feed_items"],
        "fetched": summary["fetched"],
        "added": len(summary["added"]),
        "state_changes": len(summary["state_changes"]),
        "summaries_generated": summary.get("summaries_generated", 0),
        "summaries_cost_usd": summary.get("summaries_cost_usd", 0),
        "errors": summary.get("errors", []),
    }
    with SYNC_STATUS_FILE.open("w", encoding="utf-8") as f:
        json.dump(sync_status, f, ensure_ascii=False, indent=2)

    print(f"\nRésumé : +{len(summary['added'])} ajout(s), {len(summary['state_changes'])} changement(s) d'état, {summary.get('summaries_generated', 0)} résumés générés")
    return summary


def has_changes(summary: dict) -> bool:
    return bool(summary["added"] or summary["state_changes"])


def write_commit_message(summary: dict) -> str:
    parts = ["Sync amendements PJL 2632"]
    if summary["added"]:
        parts.append(f"+{len(summary['added'])} nouveau(x): {', '.join(summary['added'][:5])}{'…' if len(summary['added']) > 5 else ''}")
    if summary["state_changes"]:
        parts.append(f"{len(summary['state_changes'])} changement(s) d'état")
    return " — ".join(parts)


# ----- Point d'entrée -----

def main():
    summary = synchronize()
    # Écrire un résumé pour le workflow
    with open(ROOT / ".sync_summary.txt", "w", encoding="utf-8") as f:
        f.write(write_commit_message(summary) + "\n")
        f.write(f"changes={'1' if has_changes(summary) else '0'}\n")

    print()
    print("Commit message :", write_commit_message(summary))
    sys.exit(0)


if __name__ == "__main__":
    main()
