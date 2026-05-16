#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Application des jumeaux d'amendements sur les fiches de séance.

Source : data/jumeaux_seance.json, généré à partir de l'analyse
d'amendements identiques (dispositif + exposé des motifs identiques).
Couvre les groupes de jumeaux entre amendements de séance (AN) et
amendements de commission (CD, CE), avec leur sort.

Champ ajouté sur les amendements AN concernés :
- jumeaux : liste de dicts {num, kind, sort, auteur?, article?}
- jumeau_groupe : identifiant du groupe (G001, G002, ...)
"""

from __future__ import annotations
import json
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
JUMEAUX_FILE = DATA_DIR / "jumeaux_seance.json"


def load_jumeaux() -> dict:
    if not JUMEAUX_FILE.exists():
        return {}
    raw = json.loads(JUMEAUX_FILE.read_text(encoding="utf-8"))
    return raw.get("jumeaux", {})


def apply_jumeaux_seance(amendments: list) -> dict:
    jumeaux = load_jumeaux()
    stats = {
        "total_in_index": len(jumeaux),
        "applied": 0,
        "with_tranche": 0,   # combien ont au moins un jumeau au sort tranché
        "missing_in_data": [],
    }
    if not jumeaux:
        print(f"⚠ {JUMEAUX_FILE} introuvable ou vide")
        return stats

    by_num = {a.get("num", ""): a for a in amendments}

    for an_num, info in jumeaux.items():
        if an_num not in by_num:
            stats["missing_in_data"].append(an_num)
            continue
        a = by_num[an_num]
        a["jumeaux"] = info["jumeaux"]
        a["jumeau_groupe"] = info.get("groupe", "")
        stats["applied"] += 1
        # Compte ceux qui ont un sort tranché
        tranche = any(
            j.get("sort") not in (None, "", "Non renseigné")
            for j in info["jumeaux"]
        )
        if tranche:
            stats["with_tranche"] += 1

    print(f"  ✓ Jumeaux appliqués sur {stats['applied']} amendements AN")
    print(f"    Dont {stats['with_tranche']} avec ≥1 jumeau au sort déjà tranché")
    if stats["missing_in_data"]:
        print(f"  ⚠ {len(stats['missing_in_data'])} jumeaux du fichier introuvables dans amendments.json")
    return stats


if __name__ == "__main__":
    import sys
    DATA_FILE = DATA_DIR / "amendments.json"
    if not DATA_FILE.exists():
        print(f"⚠ {DATA_FILE} introuvable", file=sys.stderr)
        sys.exit(1)
    data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    print(f"Application des jumeaux de séance...")
    apply_jumeaux_seance(data["amendments"])
    DATA_FILE.write_text(
        json.dumps(data, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    print(f"✓ Sauvegardé dans {DATA_FILE}")
