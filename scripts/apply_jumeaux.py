#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Application sur les amendements CE des informations sur leurs jumeaux CD
(amendements identiques côté commission Développement durable, dont le sort
peut prédire celui de l'amendement CE en commission Affaires économiques).

Champs ajoutés sur les amendements CE concernés :
- jumeaux_cd : liste de dicts {num, sort, auteur?, kind, article?}
- jumeau_type : "substantiel" | "suppression" — type principal de jumelage

Source : data/jumeaux_cd_ce.json, lui-même extrait de l'analyse comparative
des dispositifs identiques entre CD et CE.
"""

from __future__ import annotations
import json
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
JUMEAUX_FILE = DATA_DIR / "jumeaux_cd_ce.json"


def load_jumeaux() -> dict:
    if not JUMEAUX_FILE.exists():
        return {}
    raw = json.loads(JUMEAUX_FILE.read_text(encoding="utf-8"))
    return raw.get("amendments", {})


def apply_jumeaux(amendments: list) -> dict:
    """Applique les jumeaux sur les amendements CE (modification en place)."""
    jumeaux = load_jumeaux()
    stats = {
        "total_in_index": len(jumeaux),
        "applied": 0,
        "by_type": {"substantiel": 0, "suppression": 0},
        "missing_in_data": [],
    }

    if not jumeaux:
        print(f"⚠ {JUMEAUX_FILE} introuvable ou vide — aucun jumeau appliqué")
        return stats

    amend_by_num = {a.get("num", ""): a for a in amendments}

    for ce_num, info in jumeaux.items():
        if ce_num not in amend_by_num:
            stats["missing_in_data"].append(ce_num)
            continue

        a = amend_by_num[ce_num]
        a["jumeaux_cd"] = info["jumeaux_cd"]
        a["jumeau_type"] = info.get("type", "substantiel")
        if "article" in info:
            a["jumeau_article"] = info["article"]
        stats["applied"] += 1
        if info.get("type") in stats["by_type"]:
            stats["by_type"][info["type"]] += 1

    print(f"  ✓ Jumeaux CD appliqués sur {stats['applied']} amendements CE")
    print(f"    substantiels : {stats['by_type']['substantiel']}")
    print(f"    suppressions : {stats['by_type']['suppression']}")
    if stats["missing_in_data"]:
        print(f"  ⚠ {len(stats['missing_in_data'])} CE du fichier introuvables dans les données")

    return stats


# CLI standalone
if __name__ == "__main__":
    import sys
    DATA_FILE = DATA_DIR / "amendments.json"
    if not DATA_FILE.exists():
        print(f"⚠ {DATA_FILE} introuvable", file=sys.stderr)
        sys.exit(1)

    data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    print(f"Application des jumeaux CD↔CE...")
    stats = apply_jumeaux(data["amendments"])

    DATA_FILE.write_text(
        json.dumps(data, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8"
    )
    print(f"✓ Sauvegardé dans {DATA_FILE}")
