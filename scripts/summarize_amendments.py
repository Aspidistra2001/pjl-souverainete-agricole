#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Générateur de résumés d'amendements via l'API Claude (Haiku 4.5).

Usage : appelé par refresh_amendments.py pour résumer les amendements
dont le champ summary_pending est True.

Optimisations :
  - Prompt caching : les instructions communes (rôle, style, glossaire)
    sont mises en cache pour ne pas être facturées à chaque amendement.
  - Dédoublonnage : seuls les amendements avec summary_pending=True
    sont traités (les autres ont déjà un résumé rédigé manuellement
    ou par un appel API précédent).
  - Modèle Haiku 4.5 (claude-haiku-4-5-20251001) : 1$/MTok input,
    5$/MTok output, 0.10$/MTok pour cache hits.

Configuration :
  - Variable d'environnement ANTHROPIC_API_KEY requise
  - Si non définie, le script renvoie False sans erreur (les amendements
    restent en summary_pending=True jusqu'à la prochaine sync)
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

# Prompt système commun (mis en cache pour économiser sur les appels répétés)
SYSTEM_PROMPT = """Vous êtes un veilleur parlementaire qui rédige des synthèses d'amendements pour un bulletin de veille de l'Assemblée nationale française.

CONTEXTE : Le texte n° 2632 est le projet de loi d'urgence pour la protection et la souveraineté agricoles, examiné en commission du Développement durable puis en commission des Affaires économiques.

FORMAT DE SORTIE OBLIGATOIRE :
[Type] Synthèse de 200 à 300 mots maximum.

Le [Type] doit être l'un de ces qualificatifs (au choix selon le contenu) :
- [Modification] : modifie un article existant ou ajoute un alinéa
- [Complément] : ajoute une précision, condition ou critère sans changer le fond
- [Suppression] : supprime un article, alinéa ou disposition
- [Modification du titre] : modifie le titre du chapitre, section ou article
- [Substitution] : remplace une rédaction par une autre
- [Ajout d'article] : crée un nouvel article
- [Demande de rapport] : sollicite un rapport du gouvernement
- [Encadrement] : pose des limites ou définit un cadre

EXIGENCES DE STYLE :
- Sobre, factuel, dans le ton institutionnel d'un dossier de commission
- Phrases courtes, vocabulaire précis (utilisez les termes du droit administratif et agricole)
- Aller à l'essentiel : objectif principal de l'amendement, mécanisme proposé, justification clé
- Pas de jugement de valeur, pas de prise de position
- Indiquer le mécanisme juridique quand pertinent (codification, abrogation, dérogation, etc.)
- Citer les codes et articles concernés quand c'est mentionné dans le dispositif

INTERDICTIONS :
- Ne pas commencer par "Cet amendement vise à" ou "L'amendement propose de" (formule trop scolaire)
- Ne pas reproduire littéralement de longs passages du dispositif
- Ne pas donner d'avis personnel ni faire de commentaire éditorial
- Ne pas inventer d'informations qui ne sont pas dans le dispositif ou l'exposé sommaire
- Ne pas mentionner le nom de l'auteur ni le groupe parlementaire (déjà affichés ailleurs)

GLOSSAIRE DE RÉFÉRENCE :
- PAC : politique agricole commune
- ICPE : installation classée pour la protection de l'environnement
- AOP/IGP : appellation d'origine protégée / indication géographique protégée
- ONG : organisation non gouvernementale
- Néonicotinoïdes : famille d'insecticides
- HVE : haute valeur environnementale
- AB : agriculture biologique

Vous devez produire UNIQUEMENT la synthèse, sans préambule, sans guillemets, sans commentaire."""


def call_claude_api(api_key: str, system_blocks: list, user_content: str,
                    max_tokens: int = 600, retries: int = 2) -> tuple[str | None, dict | None]:
    """Appelle l'API Claude. Renvoie (texte, usage) ou (None, None) en cas d'échec.

    Le système est passé sous forme de liste de blocs pour permettre le caching
    explicite via cache_control.
    """
    payload = {
        "model": MODEL,
        "max_tokens": max_tokens,
        "system": system_blocks,
        "messages": [
            {"role": "user", "content": user_content}
        ],
    }
    data = json.dumps(payload).encode("utf-8")
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    last_err = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(API_URL, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=60) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                # Extraire le texte
                text = ""
                for block in result.get("content", []):
                    if block.get("type") == "text":
                        text += block.get("text", "")
                return text.strip(), result.get("usage")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            last_err = f"HTTP {e.code} : {body[:200]}"
            # Backoff exponentiel sur 429 et 5xx
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                wait = 2 ** attempt
                print(f"  API erreur {e.code}, retry dans {wait}s...", file=sys.stderr)
                time.sleep(wait)
                continue
            print(f"  API erreur définitive : {last_err}", file=sys.stderr)
            return None, None
        except Exception as e:
            last_err = str(e)
            if attempt < retries:
                time.sleep(2 ** attempt)
                continue
            print(f"  API erreur réseau : {last_err}", file=sys.stderr)
            return None, None
    return None, None


def build_user_prompt(amendment: dict) -> str:
    """Construit le contenu user à partir d'un amendement.

    Inclut : numéro, article, dispositif, exposé sommaire.
    """
    parts = []
    parts.append(f"AMENDEMENT {amendment['num']}")
    parts.append(f"Article visé : {amendment.get('article', 'À classer')}")
    if amendment.get("instance"):
        parts.append(f"Commission : {amendment['instance']}")
    parts.append("")

    # On a besoin du dispositif et de l'exposé sommaire
    # Ces champs ne sont peut-être pas dans amendment lui-même : on doit
    # les fetcher depuis l'XML d'OpenData ou utiliser le summary actuel
    # comme fallback (qui contient déjà l'extrait brut)
    raw = amendment.get("_raw_dispositif") or amendment.get("_raw_expose")
    if not raw:
        # Fallback : utiliser le summary actuel (qui contient l'extrait
        # brut de l'exposé sommaire si summary_pending=True)
        raw = amendment.get("summary", "")

    parts.append("DISPOSITIF ET EXPOSÉ SOMMAIRE :")
    parts.append(raw)
    parts.append("")
    parts.append("Rédigez la synthèse pour cet amendement, en respectant le format [Type] + 200 à 300 mots.")

    return "\n".join(parts)


def summarize_amendments(amendments_pending: list, api_key: str,
                         max_per_run: int = 50,
                         dry_run: bool = False) -> dict:
    """Génère les résumés pour une liste d'amendements en attente.

    Args:
        amendments_pending : liste d'amendements avec summary_pending=True
        api_key : clé API Anthropic
        max_per_run : limite de sécurité pour ne pas vider tout le crédit
                      sur une seule sync (par défaut 50)
        dry_run : si True, ne fait rien et retourne juste un compte

    Modifie chaque amendement en place :
        - Met à jour 'summary' avec la synthèse Claude
        - Passe 'summary_pending' à False
        - Ajoute 'summary_generated_at' avec le timestamp ISO

    Renvoie un dict de stats : {generated, failed, skipped, tokens_in, tokens_out, ...}
    """
    stats = {
        "candidates": len(amendments_pending),
        "generated": 0,
        "failed": 0,
        "skipped": 0,
        "tokens_in": 0,
        "tokens_in_cached": 0,
        "tokens_out": 0,
        "cache_writes": 0,
    }

    if not api_key:
        print("⚠ ANTHROPIC_API_KEY non définie — skip de la génération de résumés", file=sys.stderr)
        stats["skipped"] = len(amendments_pending)
        return stats

    if dry_run:
        print(f"[dry-run] {len(amendments_pending)} résumés seraient générés")
        return stats

    # Système avec cache_control sur le bloc principal
    # → la première requête écrit le cache, les suivantes le lisent à 10%
    system_blocks = [
        {
            "type": "text",
            "text": SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }
    ]

    todo = amendments_pending[:max_per_run]
    skipped = max(0, len(amendments_pending) - max_per_run)
    if skipped:
        print(f"  ⚠ {skipped} amendements en file d'attente (au-delà de max_per_run={max_per_run}), reportés à la prochaine sync")
        stats["skipped"] = skipped

    print(f"  → Génération de {len(todo)} résumés via Claude Haiku 4.5...")

    from datetime import datetime, timezone

    for i, amendment in enumerate(todo, start=1):
        user_prompt = build_user_prompt(amendment)
        text, usage = call_claude_api(api_key, system_blocks, user_prompt)

        if text is None:
            stats["failed"] += 1
            continue

        # Petite validation : doit commencer par [...]
        if not re.match(r"^\[[^\]]+\]", text):
            print(f"  ⚠ {amendment['num']} : format inattendu, on garde quand même : {text[:80]}", file=sys.stderr)

        amendment["summary"] = text
        amendment.pop("summary_pending", None)
        amendment["summary_generated_at"] = datetime.now(timezone.utc).isoformat()
        amendment["summary_source"] = "claude-haiku-4-5"
        stats["generated"] += 1

        if usage:
            stats["tokens_in"] += usage.get("input_tokens", 0)
            stats["tokens_in_cached"] += usage.get("cache_read_input_tokens", 0)
            stats["tokens_out"] += usage.get("output_tokens", 0)
            stats["cache_writes"] += usage.get("cache_creation_input_tokens", 0)

        if i % 10 == 0:
            print(f"  Progression : {i}/{len(todo)}")

    # Estimation de coût
    cost_in = (stats["tokens_in"] - stats["tokens_in_cached"]) * 1.0 / 1_000_000
    cost_in_cached = stats["tokens_in_cached"] * 0.10 / 1_000_000
    cost_cache_writes = stats["cache_writes"] * 1.25 / 1_000_000
    cost_out = stats["tokens_out"] * 5.0 / 1_000_000
    total_cost = cost_in + cost_in_cached + cost_cache_writes + cost_out
    stats["estimated_cost_usd"] = round(total_cost, 4)

    print(f"  ✓ {stats['generated']} générés, {stats['failed']} échecs")
    print(f"  Tokens : in={stats['tokens_in']:,} (cache hits={stats['tokens_in_cached']:,}) | out={stats['tokens_out']:,}")
    print(f"  Coût estimé : ~{total_cost:.3f} USD")

    return stats


# Test standalone (utile pour debugger sans toucher amendments.json)
if __name__ == "__main__":
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("ANTHROPIC_API_KEY manquante. Exemple d'usage :")
        print('  export ANTHROPIC_API_KEY="sk-ant-..."')
        print("  python3 scripts/summarize_amendments.py")
        sys.exit(1)

    # Charger amendments.json et lister ceux à résumer
    from pathlib import Path
    DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "amendments.json"
    with DATA_FILE.open(encoding="utf-8") as f:
        data = json.load(f)

    pending = [a for a in data["amendments"] if a.get("summary_pending")]
    print(f"Amendements en attente de résumé : {len(pending)}")

    if not pending:
        print("Rien à faire.")
        sys.exit(0)

    # Test avec 3 amendements
    test_batch = pending[:3]
    stats = summarize_amendments(test_batch, api_key, max_per_run=3)

    print("\nÉchantillons générés :")
    for a in test_batch:
        if not a.get("summary_pending"):
            print(f"\n--- {a['num']} ---")
            print(a["summary"])
