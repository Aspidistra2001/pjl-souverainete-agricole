#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Génère un fichier Excel téléchargeable depuis le site avec tous les amendements
et leur résumé.

Le fichier produit data/export_amendements.xlsx est régénéré à chaque sync
du workflow et accessible via un lien sur la page web.

Mise en forme :
  - Une feuille par commission (Toutes / Développement durable / Affaires économiques)
  - Couleurs des groupes parlementaires (RN, EPR, etc.)
  - Couleurs des états (En traitement, A discuter, Adopté, Rejeté, etc.)
  - Hyperliens vers les fiches AN
  - Filtres automatiques + figeage de la première ligne
  - Largeurs de colonnes optimisées
"""

from __future__ import annotations
import json
import sys
from pathlib import Path
from datetime import datetime, timezone

try:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
except ImportError:
    print("⚠ openpyxl non installé. Installer avec : pip install openpyxl", file=sys.stderr)
    sys.exit(1)


ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = ROOT / "data" / "amendments.json"
# Trois fichiers générés selon le périmètre choisi par l'utilisateur :
# - export_amendements_tous.xlsx : tous les amendements (toutes commissions)
# - export_amendements_cd.xlsx   : commission Développement durable
# - export_amendements_ce.xlsx   : commission Affaires économiques


# Couleurs des groupes parlementaires (en hex sans le #)
GROUP_COLORS = {
    "RN":   {"bg": "1B3D6A", "fg": "FFFFFF"},   # bleu marine
    "EPR":  {"bg": "FBC02D", "fg": "1A1816"},   # jaune
    "LFI":  {"bg": "B71C1C", "fg": "FFFFFF"},   # rouge sombre
    "SOC":  {"bg": "EF5350", "fg": "FFFFFF"},   # rose-rouge
    "DR":   {"bg": "1565C0", "fg": "FFFFFF"},   # bleu vif
    "EcoS": {"bg": "2E7D32", "fg": "FFFFFF"},   # vert
    "DEM":  {"bg": "FF7043", "fg": "FFFFFF"},   # orange
    "HOR":  {"bg": "5C6BC0", "fg": "FFFFFF"},   # bleu lavande
    "LIOT": {"bg": "8D6E63", "fg": "FFFFFF"},   # brun
    "UDR":  {"bg": "424242", "fg": "FFFFFF"},   # gris
}

# Couleurs des états (fond, texte)
STATE_COLORS = {
    "En traitement":   {"bg": "F4F1E8", "fg": "6E6859"},
    "A discuter":      {"bg": "E3EDF6", "fg": "2C5E8A"},
    "Discuté":         {"bg": "ECE4F4", "fg": "6F4D8C"},
    "Adopté":          {"bg": "D5ECDA", "fg": "1A5D2C", "bold": True},
    "Rejeté":          {"bg": "F7D8D4", "fg": "8E2418", "bold": True},
    "Tombé":           {"bg": "ECE9E0", "fg": "5A5448", "bold": True},
    "Non soutenu":     {"bg": "ECE9E0", "fg": "5A5448"},
    "Retiré":          {"bg": "EBE8DF", "fg": "6E6859"},
    "Irrecevable":     {"bg": "F7E3E0", "fg": "8E2418"},
    "Irrecevable 40":  {"bg": "F7E3E0", "fg": "8E2418"},
}

# Couleur de l'en-tête (vert hémicycle)
HEADER_BG = "1D4D2C"
HEADER_FG = "FFFFFF"


def safe_text(s) -> str:
    """Retire les caractères de contrôle qui font crasher openpyxl."""
    if s is None:
        return ""
    s = str(s)
    return "".join(c for c in s if c == "\n" or c == "\t" or ord(c) >= 32)


def write_sheet(ws, amendments: list, title: str):
    """Écrit une feuille de tableau dans le workbook.

    Le 'title' est utilisé pour la ligne d'en-tête à l'intérieur de la feuille,
    et tronqué à 31 caractères pour le nom de l'onglet (limite Excel).
    """
    # Limite Excel : 31 caractères pour le nom de feuille
    sheet_name = title if len(title) <= 31 else title[:28] + "..."
    ws.title = sheet_name

    # Ligne de titre / contexte (fusionnée)
    ws["A1"] = f"Bulletin de veille — PJL n° 2632 — Souveraineté agricole — {title}"
    ws["A1"].font = Font(name="Calibri", size=14, bold=True, color="1A1816")
    ws["A1"].alignment = Alignment(horizontal="left", vertical="center")
    ws.merge_cells("A1:H1")
    ws.row_dimensions[1].height = 24

    ws["A2"] = f"Export généré le {datetime.now(timezone.utc).strftime('%d/%m/%Y à %H:%M UTC')} — {len(amendments)} amendements"
    ws["A2"].font = Font(name="Calibri", size=10, italic=True, color="6E6859")
    ws.merge_cells("A2:H2")
    ws.row_dimensions[2].height = 18

    # En-têtes (ligne 4)
    headers = ["N°", "Article", "Auteur", "Groupe", "État", "Thématiques", "Résumé", "Lien officiel"]
    for col_idx, h in enumerate(headers, start=1):
        cell = ws.cell(row=4, column=col_idx, value=h)
        cell.font = Font(name="Calibri", size=11, bold=True, color=HEADER_FG)
        cell.fill = PatternFill("solid", fgColor=HEADER_BG)
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.row_dimensions[4].height = 28

    # Trier par numéro (en gardant CE/CD ensemble) puis par numéro croissant
    def sort_key(a):
        num = a.get("num", "")
        # Extraire le préfixe lettres et la partie chiffres
        prefix = ""
        digits = ""
        for c in num:
            if c.isalpha():
                prefix += c
            else:
                digits += c
        return (prefix, int(digits) if digits.isdigit() else 0)

    sorted_amends = sorted(amendments, key=sort_key)

    # Bordure légère pour les cellules de données
    thin = Side(border_style="thin", color="DDDDDD")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    for row_idx, a in enumerate(sorted_amends, start=5):
        # N° (avec hyperlien)
        cell = ws.cell(row=row_idx, column=1, value=safe_text(a.get("num", "")))
        cell.font = Font(name="Calibri", size=10, bold=True, color="1565C0", underline="single")
        cell.alignment = Alignment(horizontal="left", vertical="top")
        url = a.get("url", "")
        if url:
            cell.hyperlink = url
        cell.border = border

        # Article
        article = a.get("article", "À classer")
        cell = ws.cell(row=row_idx, column=2, value=safe_text(article))
        cell.font = Font(name="Calibri", size=10)
        cell.alignment = Alignment(horizontal="left", vertical="top", wrap_text=True)
        cell.border = border

        # Auteur (ajouter (rapporteure) si rapporteur)
        author = a.get("author", "")
        if a.get("rapporteur"):
            author += " (rapporteur)"
        cell = ws.cell(row=row_idx, column=3, value=safe_text(author))
        cell.font = Font(name="Calibri", size=10)
        cell.alignment = Alignment(horizontal="left", vertical="top", wrap_text=True)
        cell.border = border

        # Groupe (avec coloration)
        group = a.get("group", "")
        cell = ws.cell(row=row_idx, column=4, value=safe_text(group))
        gcol = GROUP_COLORS.get(group, {"bg": "E0E0E0", "fg": "1A1816"})
        cell.font = Font(name="Calibri", size=10, bold=True, color=gcol["fg"])
        cell.fill = PatternFill("solid", fgColor=gcol["bg"])
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = border

        # État (avec coloration)
        state = a.get("state", "En traitement")
        cell = ws.cell(row=row_idx, column=5, value=safe_text(state))
        scol = STATE_COLORS.get(state, {"bg": "F4F1E8", "fg": "6E6859"})
        cell.font = Font(
            name="Calibri", size=10,
            color=scol["fg"],
            bold=scol.get("bold", False)
        )
        cell.fill = PatternFill("solid", fgColor=scol["bg"])
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = border

        # Thématiques (tags Claude)
        TAG_LABELS_LOCAL = {
            "coop": "Coopératives", "ab": "AB",
            "animale": "Production animale", "végétale": "Production végétale",
        }
        tags = a.get("tags", []) or []
        tag_text = ", ".join(TAG_LABELS_LOCAL.get(t, t) for t in tags)
        cell = ws.cell(row=row_idx, column=6, value=safe_text(tag_text))
        cell.font = Font(name="Calibri", size=9, italic=True, color="4a443a")
        cell.alignment = Alignment(horizontal="left", vertical="top", wrap_text=True)
        cell.border = border

        # Résumé
        summary = a.get("summary", "")
        cell = ws.cell(row=row_idx, column=7, value=safe_text(summary))
        cell.font = Font(name="Calibri", size=10)
        cell.alignment = Alignment(horizontal="left", vertical="top", wrap_text=True)
        cell.border = border

        # Lien officiel
        cell = ws.cell(row=row_idx, column=8, value="Voir sur l'AN" if url else "")
        if url:
            cell.font = Font(name="Calibri", size=10, color="1565C0", underline="single")
            cell.hyperlink = url
        else:
            cell.font = Font(name="Calibri", size=10)
        cell.alignment = Alignment(horizontal="left", vertical="top")
        cell.border = border

    # Largeurs de colonnes (en caractères) — pour 8 colonnes maintenant
    widths = [10, 24, 28, 8, 18, 22, 75, 18]
    for col_idx, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(col_idx)].width = w

    # Hauteur de ligne par défaut adaptée pour le wrap
    # Les cellules avec wrap_text s'ajusteront automatiquement
    for row in ws.iter_rows(min_row=5, max_row=ws.max_row):
        ws.row_dimensions[row[0].row].height = 60

    # Filtres automatiques sur la ligne d'en-têtes
    last_col = get_column_letter(len(headers))
    last_row = 4 + len(sorted_amends)
    ws.auto_filter.ref = f"A4:{last_col}{last_row}"

    # Figer la zone d'en-tête
    ws.freeze_panes = "A5"


def main():
    if not DATA_FILE.exists():
        print(f"⚠ {DATA_FILE} introuvable", file=sys.stderr)
        sys.exit(1)

    with DATA_FILE.open(encoding="utf-8") as f:
        data = json.load(f)

    amendments = data.get("amendments", [])
    print(f"Génération Excel pour {len(amendments)} amendements...")

    # Quatre fichiers : un par périmètre. Le site choisit lequel télécharger
    # selon le bouton radio sélectionné par l'utilisateur.
    cd_amends = [a for a in amendments if a.get("instance") == "Développement durable"]
    ce_amends = [a for a in amendments if a.get("instance") == "Affaires économiques"]
    an_amends = [a for a in amendments if a.get("instance") == "Séance publique"]

    files_to_generate = [
        ("export_amendements_tous.xlsx",
         "Tous les amendements",
         "Tous les amendements",
         amendments),
        ("export_amendements_cd.xlsx",
         "Dvp durable",
         "Commission Développement durable",
         cd_amends),
        ("export_amendements_ce.xlsx",
         "Affaires éco",
         "Commission Affaires économiques",
         ce_amends),
        ("export_amendements_an.xlsx",
         "Séance publique",
         "Séance publique (texte adopté en commission n° 2765)",
         an_amends),
    ]

    import os
    for filename, sheet_name, full_title, amends in files_to_generate:
        if not amends:
            continue
        output_file = ROOT / "data" / filename
        wb = Workbook()
        ws = wb.active
        # Pour ces fichiers à feuille unique, on adapte le nom d'onglet
        write_sheet(ws, amends, full_title)
        # Renommer l'onglet pour qu'il soit court (le titre interne reste long)
        ws.title = sheet_name
        wb.save(output_file)
        size_kb = os.path.getsize(output_file) / 1024
        print(f"  ✓ {filename} : {len(amends)} amendements ({size_kb:.0f} ko)")


if __name__ == "__main__":
    main()
