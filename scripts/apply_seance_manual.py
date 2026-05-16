#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Application des résumés et tags éditorialisés des amendements de séance
publique (PJL 2632, texte n° 2765, souveraineté agricole).

Sources :
- data/resumes_seance.json : résumés extraits du bulletin de veille manuel
                             (exposé des motifs, prioritaire sur Claude)
- data/tags_seance.json    : tags thématiques (coop, ab, animale, végétale)
                             extraits du bulletin focus thématiques

Comportement :
- Tous les amendements AN listés dans les JSON voient leur résumé et leurs
  tags écrasés par la version éditorialisée (logique : votre travail manuel
  est toujours plus fiable que la génération automatique).
- Les amendements AN absents des JSON gardent leur résumé/tags Claude
  (ou pending si Claude n'est pas encore passé).
- Les amendements CD/CE ne sont pas touchés.

Champs ajoutés :
- summary_source = "manual" (écrase "claude" ou "rss")
- tags_source    = "manual" (écrase "claude" ou "skipped")
"""

from __future__ import annotations
import json
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
RESUMES_FILE = DATA_DIR / "resumes_seance.json"
TAGS_FILE = DATA_DIR / "tags_seance.json"

VALID_TAGS = {"coop", "ab", "animale", "végétale"}


def load_resumes() -> dict:
    if not RESUMES_FILE.exists():
        return {}
    raw = json.loads(RESUMES_FILE.read_text(encoding="utf-8"))
    return raw.get("resumes", {})


def load_tags() -> dict:
    if not TAGS_FILE.exists():
        return {}
    raw = json.loads(TAGS_FILE.read_text(encoding="utf-8"))
    return raw.get("tags", {})


def apply_seance_manual(amendments: list) -> dict:
    """Applique les résumés et tags de séance sur les amendements AN.

    Modifie la liste en place. Renvoie un dict de statistiques.
    """
    resumes = load_resumes()
    tags_by_num = load_tags()
    stats = {
        "resumes_index": len(resumes),
        "tags_index": len(tags_by_num),
        "resumes_applied": 0,
        "tags_applied": 0,
        "missing_in_data": [],
        "tags_distribution": {t: 0 for t in VALID_TAGS},
        "amendments_with_tags": 0,
    }

    # Index pour lookup rapide
    by_num = {a.get("num", ""): a for a in amendments}

    # 1) Appliquer les résumés
    for num, expose in resumes.items():
        if num not in by_num:
            stats["missing_in_data"].append(num)
            continue
        a = by_num[num]
        a["summary"] = expose
        a["summary_source"] = "manual"
        a["summary_pending"] = False
        stats["resumes_applied"] += 1

    # 2) Appliquer les tags
    for num, tags in tags_by_num.items():
        if num not in by_num:
            if num not in stats["missing_in_data"]:
                stats["missing_in_data"].append(num)
            continue
        a = by_num[num]
        # Ne garder que les tags valides
        valid = [t for t in tags if t in VALID_TAGS]
        a["tags"] = valid
        a["tags_source"] = "manual"
        stats["tags_applied"] += 1
        if valid:
            stats["amendments_with_tags"] += 1
            for t in valid:
                stats["tags_distribution"][t] += 1

    print(f"  ✓ Résumés manuels appliqués : {stats['resumes_applied']}")
    print(f"  ✓ Tags manuels appliqués    : {stats['tags_applied']} (dont {stats['amendments_with_tags']} avec ≥1 tag)")
    if stats["amendments_with_tags"]:
        dist = ", ".join(f"{t}={n}" for t, n in stats["tags_distribution"].items() if n)
        print(f"    Distribution : {dist}")
    if stats["missing_in_data"]:
        print(f"  ⚠ {len(stats['missing_in_data'])} numéros du bulletin introuvables dans amendments.json")
        print(f"     (probablement des amendements pas encore syncs depuis OpenData)")

    return stats


# CLI standalone
if __name__ == "__main__":
    import sys
    DATA_FILE = DATA_DIR / "amendments.json"
    if not DATA_FILE.exists():
        print(f"⚠ {DATA_FILE} introuvable", file=sys.stderr)
        sys.exit(1)

    data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    print(f"Application des résumés et tags éditorialisés (séance)...")
    apply_seance_manual(data["amendments"])

    DATA_FILE.write_text(
        json.dumps(data, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    print(f"✓ Sauvegardé dans {DATA_FILE}")
