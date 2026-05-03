#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tagging thématique des amendements à partir des bulletins de veille manuels.

Source de vérité : data/tags_manual.json

Le JSON contient une liste d'amendements pour chaque tag, extraite éditorialement
des bulletins de veille publiés (un PDF par thématique). C'est plus précis et
gratuit qu'un classement par IA.

Utilisation :
    from tag_from_bulletins import apply_manual_tags
    stats = apply_manual_tags(data['amendments'])
"""

from __future__ import annotations
import json
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
TAGS_FILE = DATA_DIR / "tags_manual.json"

VALID_TAGS = {"coop", "ab", "animale", "végétale"}


def load_tags_index() -> dict[str, set[str]]:
    """Charge le fichier tags_manual.json et renvoie un dict tag -> set de numéros."""
    if not TAGS_FILE.exists():
        return {}
    raw = json.loads(TAGS_FILE.read_text(encoding="utf-8"))
    tags = raw.get("tags", {})
    return {tag: set(nums) for tag, nums in tags.items() if tag in VALID_TAGS}


def build_reverse_index(tags: dict[str, set[str]]) -> dict[str, list[str]]:
    """Construit l'index inverse : numéro d'amendement -> liste de tags (ordre fixe)."""
    TAG_ORDER = ["coop", "ab", "animale", "végétale"]
    index = {}
    for tag in TAG_ORDER:
        for num in tags.get(tag, []):
            index.setdefault(num, []).append(tag)
    return index


def apply_manual_tags(amendments: list, force: bool = False) -> dict:
    """Applique les tags manuels sur la liste des amendements (modification en place).

    PORTÉE : seuls les amendements de la commission des Affaires économiques
    (préfixe 'CE') sont traités. Les amendements CD (Développement durable),
    AS (Affaires sociales) ou autres ne sont JAMAIS taggés. Pour ces derniers,
    si un champ 'tags' a été appliqué par erreur dans une exécution antérieure,
    il est retiré.

    Args:
        amendments: liste de dicts (data['amendments'])
        force: si True, écrase les tags existants sur les CE. Sinon, n'écrit que
               sur les CE sans champ 'tags'.

    Returns:
        dict de statistiques
    """
    tags_index = load_tags_index()
    reverse = build_reverse_index(tags_index)

    stats = {
        "total_amendments": len(amendments),
        "ce_amendments": 0,
        "non_ce_amendments": 0,
        "non_ce_cleaned": 0,       # CD/AS qui avaient un champ 'tags' parasite
        "in_index": 0,             # CE dont le numéro figure dans le JSON
        "tagged": 0,               # CE effectivement (re)taggés
        "skipped_existing": 0,     # CE déjà taggés (et force=False)
        "no_tag": 0,               # CE en dehors du périmètre des bulletins
        "tags_distribution": {t: 0 for t in VALID_TAGS},
        "amendments_with_tags": 0,
    }

    if not tags_index:
        print(f"⚠ {TAGS_FILE} introuvable ou vide — aucun tag appliqué")
        return stats

    print(f"  Index : {sum(len(v) for v in tags_index.values())} entrées au total ; {len(reverse)} amendements distincts")

    for a in amendments:
        num = a.get("num", "")
        is_ce = num.startswith("CE")

        # Pour les non-CE : nettoyage et passage au suivant
        if not is_ce:
            stats["non_ce_amendments"] += 1
            if "tags" in a:
                del a["tags"]
                if "tags_generated_at" in a:
                    del a["tags_generated_at"]
                stats["non_ce_cleaned"] += 1
            continue

        # À partir d'ici, uniquement les CE
        stats["ce_amendments"] += 1
        existing = a.get("tags")

        # Cas 1 : déjà taggé et force=False -> on saute
        if existing is not None and not force:
            stats["skipped_existing"] += 1
            if existing:
                stats["amendments_with_tags"] += 1
                for t in existing:
                    if t in stats["tags_distribution"]:
                        stats["tags_distribution"][t] += 1
            continue

        # Cas 2 : non encore taggé OU force=True
        new_tags = reverse.get(num, [])
        a["tags"] = new_tags
        stats["tagged"] += 1
        if num in reverse:
            stats["in_index"] += 1
        if new_tags:
            stats["amendments_with_tags"] += 1
            for t in new_tags:
                stats["tags_distribution"][t] += 1
        else:
            stats["no_tag"] += 1

    print(f"  ✓ {stats['ce_amendments']} amendements CE traités, dont {stats['amendments_with_tags']} avec ≥1 tag")
    print(f"  ({stats['non_ce_amendments']} amendements non-CE ignorés" + (
        f", dont {stats['non_ce_cleaned']} nettoyés d'anciens tags" if stats['non_ce_cleaned'] else ""
    ) + ")")
    print(f"  Distribution : " + ", ".join(
        f"{t}={n}" for t, n in stats["tags_distribution"].items()
    ))
    if stats["skipped_existing"]:
        print(f"  ({stats['skipped_existing']} amendements CE déjà taggés, conservés tels quels)")

    return stats


# CLI standalone : applique les tags sur amendments.json
if __name__ == "__main__":
    import sys
    DATA_FILE = DATA_DIR / "amendments.json"
    if not DATA_FILE.exists():
        print(f"⚠ {DATA_FILE} introuvable", file=sys.stderr)
        sys.exit(1)

    force = "--force" in sys.argv

    data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    print(f"Application des tags manuels (force={force})...")
    stats = apply_manual_tags(data["amendments"], force=force)

    DATA_FILE.write_text(
        json.dumps(data, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8"
    )
    print(f"✓ Sauvegardé dans {DATA_FILE}")
