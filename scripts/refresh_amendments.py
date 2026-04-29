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
import json
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

# ----- Configuration -----

ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = ROOT / "data" / "amendments.json"

FEED_URL = "https://aspidistra2001.github.io/AN/feed"
TEXT_NUMBER = "2632"  # PJL souveraineté agricoles

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


def synchronize() -> dict:
    """Met à jour data/amendments.json à partir du flux RSS et de l'API OpenData.

    Stratégie :
      1. Charger la baseline existante.
      2. Lire le flux RSS pour découvrir les NOUVEAUX amendements (non présents dans la baseline).
      3. Faire un balayage COMPLET de tous les amendements connus (baseline + nouveaux du flux)
         pour rapatrier leur état/sort réel depuis l'XML OpenData.
         Cela rattrape les votes qui ont eu lieu et ne sont plus dans le flux RSS.

    Retourne un dict avec un résumé des changements pour le commit message.
    """
    print(f"=== Synchronisation lancée à {datetime.now(timezone.utc).isoformat()} ===")
    summary = {
        "added": [],
        "state_changes": [],
        "errors": [],
        "feed_items": 0,
        "fetched": 0,
    }

    # 1. Charger la baseline
    if not DATA_FILE.exists():
        print(f"Erreur : {DATA_FILE} introuvable", file=sys.stderr)
        return summary
    with DATA_FILE.open(encoding="utf-8") as f:
        data = json.load(f)

    by_num = {a["num"]: a for a in data["amendments"]}
    print(f"Baseline : {len(by_num)} amendements")

    # 2. Fetch le flux RSS pour découvrir les nouveaux amendements
    feed_xml = http_get(FEED_URL)
    if not feed_xml:
        print("Flux RSS injoignable, on continue avec la baseline existante.", file=sys.stderr)
        summary["errors"].append("flux RSS injoignable")
        feed_items = []
    else:
        feed_items = parse_feed(feed_xml)
        summary["feed_items"] = len(feed_items)
        print(f"Flux RSS : {len(feed_items)} items pertinents (texte {TEXT_NUMBER})")

    # 3. Identifier les nouveaux amendements du flux à fetcher en plus
    new_from_feed = [it for it in feed_items if it["num"] not in by_num]
    print(f"Nouveaux à ajouter depuis le flux : {len(new_from_feed)}")

    # 4. Construire la liste complète des amendements à fetcher :
    #    tous les amendements connus + les nouveaux du flux
    fetch_list = []
    for a in data["amendments"]:
        fetch_list.append({"num": a["num"], "xml_url": None, "link": a.get("url", "")})
    for it in new_from_feed:
        fetch_list.append({"num": it["num"], "xml_url": it["xml_url"], "link": it["link"]})

    print(f"Total à fetcher : {len(fetch_list)} amendements (balayage complet)")

    # 5. Fetch en parallèle
    enriched: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(fetch_and_parse_xml, it["num"], it["xml_url"]): it for it in fetch_list}
        completed = 0
        for fut in as_completed(futures):
            num, parsed = fut.result()
            completed += 1
            if parsed:
                enriched[num] = parsed
                summary["fetched"] += 1
            if completed % 100 == 0:
                print(f"  Fetché {completed}/{len(fetch_list)} (succès : {summary['fetched']})")

    print(f"XML enrichis : {summary['fetched']}/{len(fetch_list)}")

    # 6. Mettre à jour les amendements existants avec leurs états réels
    for num, existing in by_num.items():
        parsed = enriched.get(num)
        if not parsed:
            continue
        new_state = parsed.get("state")
        if new_state and new_state != existing["state"]:
            old_state = existing["state"]
            existing["previous_state"] = old_state
            existing["state"] = new_state
            existing["state_changed_at"] = datetime.now(timezone.utc).isoformat()
            summary["state_changes"].append({
                "num": num, "old": old_state, "new": new_state
            })
            print(f"  [~] {num} : {old_state} → {new_state}")
        # Au passage on corrige aussi le groupe si on a une donnée plus précise
        xml_group = parsed.get("group")
        if xml_group and existing.get("group") != xml_group:
            existing["group"] = xml_group

    # 7. Ajouter les nouveaux amendements détectés via le flux RSS
    for item in new_from_feed:
        num = item["num"]
        parsed = enriched.get(num)
        if not parsed:
            continue
        instance = "Affaires économiques" if num.startswith("CE") else \
                   "Affaires sociales" if num.startswith("AS") else \
                   "Développement durable" if num.startswith("CD") else ""
        summary_text = parsed.get("expose_text") or parsed.get("dispositif_text") or "Détails non disponibles."
        new_amend = {
            "num": num,
            "article": parsed.get("article", "À classer"),
            "author": parsed.get("author", "Auteur non identifié"),
            "group": parsed.get("group") or "EPR",
            "group_resolved": bool(parsed.get("group")),
            "rapporteur": parsed.get("rapporteur", False),
            "state": parsed.get("state", "En traitement"),
            "url": item["link"],
            "instance": instance,
            "summary": truncate(summary_text, 500),
            "summary_pending": True,
            "is_new": True,
            "is_rss_new": True,
            "added_via_rss_at": datetime.now(timezone.utc).isoformat(),
        }
        data["amendments"].append(new_amend)
        by_num[num] = new_amend
        summary["added"].append(num)
        print(f"  [+] {num} ajouté ({parsed.get('group') or '?'} – {parsed.get('author', '?')})")

    # 8. Mettre à jour la métadonnée de fraîcheur
    data["meta"]["last_sync"] = datetime.now(timezone.utc).isoformat()
    data["meta"]["total"] = len(data["amendments"])

    # 9. Écrire le fichier
    with DATA_FILE.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))

    print(f"\nRésumé : +{len(summary['added'])} ajout(s), {len(summary['state_changes'])} changement(s) d'état")
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
