#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Application des avis (rapporteure, gouvernement, sort) sur les amendements CD
à partir du JSON `data/avis_cd.json`, lui-même extrait du compte-rendu de la
commission Développement durable.

Champs ajoutés sur chaque amendement CD concerné :
- avis_rapporteur : "F" | "D" | "S" | "R" | "Sat" | "" (vide si pas d'avis)
- avis_gouvernement : "F" | "D" | "S" | "R" | "Sat" | "" (vide si pas d'avis)
- sort_commission : "Adopté" | "Rejeté" | "Retiré" | "Tombé" | "Non défendu" | etc.
- sort_note : note libre (ex : "12 voix contre 11", "Adopté contre l'avis de la rapporteure")

Codes courts standardisés : F=Favorable, D=Défavorable, S=Sagesse,
R=Demande de retrait, Sat=Satisfait.

Comportement :
- Si l'amendement est dans avis_cd.json, les champs sont mis à jour (force).
- Si non, les champs existants sont préservés.
- Pour les amendements CE (Affaires éco), aucune action.
"""

from __future__ import annotations
import json
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
AVIS_FILE = DATA_DIR / "avis_cd.json"


def load_avis() -> dict:
    """Charge le fichier avis_cd.json et renvoie le dict {num: {r, g, sort, note}}."""
    if not AVIS_FILE.exists():
        return {}
    raw = json.loads(AVIS_FILE.read_text(encoding="utf-8"))
    return raw.get("amendments", {})


def apply_avis_cd(amendments: list) -> dict:
    """Applique les avis sur les amendements CD (modification en place).

    Renvoie un dict de stats.
    """
    avis = load_avis()
    stats = {
        "total_avis_in_index": len(avis),
        "applied": 0,
        "with_r": 0,
        "with_g": 0,
        "with_sort": 0,
        "missing_in_data": [],   # amendements présents dans avis_cd mais pas dans amendments.json
    }

    if not avis:
        print(f"⚠ {AVIS_FILE} introuvable ou vide — aucun avis appliqué")
        return stats

    # Index des amendements existants pour un lookup rapide
    amend_by_num = {a.get("num", ""): a for a in amendments}

    for num, info in avis.items():
        if num not in amend_by_num:
            stats["missing_in_data"].append(num)
            continue

        a = amend_by_num[num]
        if "r" in info:
            a["avis_rapporteur"] = info["r"]
            stats["with_r"] += 1
        if "g" in info:
            a["avis_gouvernement"] = info["g"]
            stats["with_g"] += 1
        if "sort" in info:
            a["sort_commission"] = info["sort"]
            stats["with_sort"] += 1
        if "note" in info:
            a["sort_note"] = info["note"]
        stats["applied"] += 1

    print(f"  ✓ Avis appliqués sur {stats['applied']} amendements CD")
    print(f"    avec avis rapporteure : {stats['with_r']}")
    print(f"    avec avis gouvernement : {stats['with_g']}")
    print(f"    avec sort en commission : {stats['with_sort']}")
    if stats["missing_in_data"]:
        print(f"  ⚠ {len(stats['missing_in_data'])} amendements du fichier d'avis introuvables dans les données :")
        for n in stats["missing_in_data"][:10]:
            print(f"      {n}")
        if len(stats["missing_in_data"]) > 10:
            print(f"      ... et {len(stats['missing_in_data']) - 10} autres")

    return stats


# CLI standalone
if __name__ == "__main__":
    import sys
    DATA_FILE = DATA_DIR / "amendments.json"
    if not DATA_FILE.exists():
        print(f"⚠ {DATA_FILE} introuvable", file=sys.stderr)
        sys.exit(1)

    data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    print(f"Application des avis CD...")
    stats = apply_avis_cd(data["amendments"])

    DATA_FILE.write_text(
        json.dumps(data, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8"
    )
    print(f"✓ Sauvegardé dans {DATA_FILE}")
