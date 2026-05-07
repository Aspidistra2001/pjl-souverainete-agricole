#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tagging thématique des amendements via l'API Claude (Haiku 4.5).

Pour chaque amendement, Claude lit le résumé et le dispositif, et attribue
0 à 4 tags parmi : coop, ab, animale, végétale.

Optimisations :
  - Prompt caching sur les instructions communes
  - Dédoublonnage : seuls les amendements sans champ 'tags' sont traités
  - Modèle Haiku 4.5 : ~0,001$ par amendement

Configuration :
  - Variable d'environnement ANTHROPIC_API_KEY requise
  - Si non définie, le script renvoie sans erreur (les amendements
    restent sans tag jusqu'à la prochaine sync)
"""

from __future__ import annotations
import os
import json
import re
import sys
import urllib.request
import urllib.error
import time

API_URL = "https://api.anthropic.com/v1/messages"
MODEL = "claude-haiku-4-5-20251001"

# Tags possibles (ordre fixe, libellés fixes)
VALID_TAGS = {"coop", "ab", "animale", "végétale"}

SYSTEM_PROMPT = """Vous classez des amendements parlementaires français selon leur portée thématique.

CONTEXTE : Le PJL n° 2632 est le projet de loi d'urgence pour la protection et la souveraineté agricoles, examiné en commission du Développement durable puis en commission des Affaires économiques.

VOTRE TÂCHE : Pour chaque amendement, analysez son contenu et attribuez 0 à 4 tags parmi :

- **coop** : touche aux coopératives agricoles (SCA, SCL, CUMA, OP), à leur statut juridique, leur fiscalité, leurs missions, leur gouvernance, ou aux organisations de producteurs (OPA). Exemples : transparence des coopératives, élargissement de leurs prérogatives, soutien fiscal aux GAEC.

- **ab** : touche à l'agriculture biologique au sens du règlement UE (label AB, conversion bio, exigences spécifiques). Aussi : restauration collective bio, soutien aux filières bio, distinction bio/conventionnel. Ne PAS inclure les pratiques "durables" non biologiques (HVE, agroécologie générique, raisonné).

- **animale** : touche à la production animale (élevage bovin, porcin, ovin, caprin, volaille, équin, apicole, aquaculture). Aussi : conditions d'élevage, abattage, transport d'animaux, alimentation animale, prix de la viande, ICPE élevage, bien-être animal. Ne PAS inclure : production de fourrage destiné à l'élevage (= végétale), médicaments vétérinaires sans cible animale spécifique.

- **végétale** : touche à la production végétale (grandes cultures, fruits, légumes, viticulture, arboriculture, semences, plantes). Aussi : phytosanitaires, intrants végétaux, jachères, haies, rotations, irrigation, AOP/IGP végétales. Ne PAS inclure : alimentation animale d'origine végétale (= animale, c'est l'élevage qui est ciblé).

RÈGLES IMPORTANTES :
- Cumul possible : un amendement peut avoir plusieurs tags (ex : ab + végétale, coop + animale).
- Seulement si pertinent : si l'amendement n'évoque AUCUN de ces 4 thèmes (ex : amendement de procédure pure, sur la gouvernance générale, sur la fiscalité agricole sans précision filière), répondre avec une liste vide [].
- Précision : ne PAS taguer "animale" parce qu'un amendement sur les coopératives MENTIONNE l'élevage en passant. Le tag doit refléter la portée réelle de l'amendement.
- Pour le tag bio : exiger une mention explicite de l'agriculture biologique ou du règlement européen bio. Ne PAS confondre avec "écologique" ou "durable".

FORMAT DE SORTIE OBLIGATOIRE :
Répondez UNIQUEMENT par une liste JSON de tags, sans explication, sans préambule.

Exemples de sortie valides :
["coop"]
["ab", "végétale"]
["animale"]
["coop", "ab", "animale"]
[]

Exemples de sorties INVALIDES :
- "Tags : coop, ab"  (pas un JSON)
- ["agriculture"]    (tag inconnu)
- ["AB"]             (casse incorrecte, doit être "ab" minuscule)"""


def call_claude_api(api_key: str, system_blocks: list, user_content: str,
                    max_tokens: int = 50, retries: int = 2) -> tuple[str | None, dict | None]:
    """Appelle l'API Claude. Renvoie (texte, usage) ou (None, None) en cas d'échec."""
    payload = {
        "model": MODEL,
        "max_tokens": max_tokens,
        "system": system_blocks,
        "messages": [{"role": "user", "content": user_content}],
    }
    data = json.dumps(payload).encode("utf-8")
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(API_URL, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                text = ""
                for block in result.get("content", []):
                    if block.get("type") == "text":
                        text += block.get("text", "")
                return text.strip(), result.get("usage")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(2 ** attempt)
                continue
            print(f"  API erreur {e.code} : {body[:150]}", file=sys.stderr)
            return None, None
        except Exception as e:
            if attempt < retries:
                time.sleep(2 ** attempt)
                continue
            print(f"  API erreur réseau : {e}", file=sys.stderr)
            return None, None
    return None, None


def parse_tags_response(text: str) -> list[str]:
    """Extrait les tags valides de la réponse JSON de Claude."""
    if not text:
        return []
    # Tolérer les wrappers markdown éventuels (```json ... ```)
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        # Fallback : extraire les tags reconnus dans le texte
        found = []
        for tag in VALID_TAGS:
            if tag.lower() in cleaned.lower():
                found.append(tag)
        return found
    if not isinstance(parsed, list):
        return []
    # Filtrer pour ne garder que les tags valides (en normalisant la casse)
    result = []
    for item in parsed:
        if not isinstance(item, str):
            continue
        # Normaliser : "AB" -> "ab", "Coopérative" -> rejeté
        normalized = item.strip().lower()
        if normalized in VALID_TAGS:
            result.append(normalized)
    # Dédupliquer en gardant l'ordre
    seen = set()
    deduped = []
    for t in result:
        if t not in seen:
            seen.add(t)
            deduped.append(t)
    return deduped


def build_user_prompt(amendment: dict) -> str:
    """Construit le contenu user à partir d'un amendement."""
    parts = []
    parts.append(f"AMENDEMENT {amendment['num']}")
    parts.append(f"Article : {amendment.get('article', 'À classer')}")
    summary = amendment.get("summary", "")
    if summary:
        parts.append("")
        parts.append("CONTENU :")
        parts.append(summary)
    parts.append("")
    parts.append("Quels tags parmi [coop, ab, animale, végétale] ? Répondez par une liste JSON.")
    return "\n".join(parts)


def tag_amendments(amendments_to_tag: list, api_key: str,
                   max_per_run: int = 200) -> dict:
    """Génère les tags pour une liste d'amendements.

    Modifie chaque amendement en place :
        - Ajoute 'tags' (liste de strings)
        - Ajoute 'tags_generated_at' (timestamp)

    Renvoie un dict de stats.
    """
    stats = {
        "candidates": len(amendments_to_tag),
        "tagged": 0,
        "failed": 0,
        "skipped": 0,
        "tokens_in": 0,
        "tokens_in_cached": 0,
        "tokens_out": 0,
        "tags_distribution": {t: 0 for t in VALID_TAGS},
        "amendments_with_tags": 0,
    }

    if not api_key:
        print("⚠ ANTHROPIC_API_KEY non définie — skip du tagging", file=sys.stderr)
        stats["skipped"] = len(amendments_to_tag)
        return stats

    system_blocks = [
        {
            "type": "text",
            "text": SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }
    ]

    todo = amendments_to_tag[:max_per_run]
    skipped = max(0, len(amendments_to_tag) - max_per_run)
    if skipped:
        print(f"  ⚠ {skipped} amendements en file d'attente (au-delà de max_per_run={max_per_run}), reportés à la prochaine sync")
        stats["skipped"] = skipped

    print(f"  → Tagging de {len(todo)} amendements via Claude Haiku 4.5...")

    from datetime import datetime, timezone

    for i, amendment in enumerate(todo, start=1):
        user_prompt = build_user_prompt(amendment)
        text, usage = call_claude_api(api_key, system_blocks, user_prompt)

        if text is None:
            stats["failed"] += 1
            continue

        tags = parse_tags_response(text)
        amendment["tags"] = tags
        amendment["tags_source"] = "claude"
        amendment["tags_generated_at"] = datetime.now(timezone.utc).isoformat()
        stats["tagged"] += 1
        if tags:
            stats["amendments_with_tags"] += 1
            for t in tags:
                if t in stats["tags_distribution"]:
                    stats["tags_distribution"][t] += 1

        if usage:
            stats["tokens_in"] += usage.get("input_tokens", 0)
            stats["tokens_in_cached"] += usage.get("cache_read_input_tokens", 0)
            stats["tokens_out"] += usage.get("output_tokens", 0)

        if i % 20 == 0:
            print(f"  Progression : {i}/{len(todo)}")

    # Estimation de coût
    cost_in = (stats["tokens_in"] - stats["tokens_in_cached"]) * 1.0 / 1_000_000
    cost_in_cached = stats["tokens_in_cached"] * 0.10 / 1_000_000
    cost_out = stats["tokens_out"] * 5.0 / 1_000_000
    total_cost = cost_in + cost_in_cached + cost_out
    stats["estimated_cost_usd"] = round(total_cost, 4)

    print(f"  ✓ {stats['tagged']} taggés ({stats['amendments_with_tags']} avec ≥1 tag), {stats['failed']} échecs")
    print(f"  Distribution : " + ", ".join(
        f"{t}={n}" for t, n in stats["tags_distribution"].items()
    ))
    print(f"  Tokens : in={stats['tokens_in']:,} (cache hits={stats['tokens_in_cached']:,}) | out={stats['tokens_out']:,}")
    print(f"  Coût estimé : ~{total_cost:.4f} USD")

    return stats


# Test standalone
if __name__ == "__main__":
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("ANTHROPIC_API_KEY manquante.")
        sys.exit(1)

    from pathlib import Path
    DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "amendments.json"
    with DATA_FILE.open(encoding="utf-8") as f:
        data = json.load(f)

    untagged = [a for a in data["amendments"] if "tags" not in a]
    print(f"Amendements à taguer : {len(untagged)}")

    if not untagged:
        print("Rien à faire.")
        sys.exit(0)

    test_batch = untagged[:5]
    stats = tag_amendments(test_batch, api_key, max_per_run=5)

    print("\nÉchantillons :")
    for a in test_batch:
        if "tags" in a:
            print(f"  {a['num']:>8}  tags={a['tags']!r}")
            print(f"           {a.get('summary', '')[:120]}...")
