# Veille parlementaire — PJL souveraineté agricole

Page web statique pour suivre les amendements déposés sur le projet de loi
d'urgence pour la protection et la souveraineté agricoles (texte n°&nbsp;2632,
17ᵉ législature).

La page est entièrement statique et se met à jour côté navigateur en lisant
le flux RSS [aspidistra2001.github.io/AN/feed](https://aspidistra2001.github.io/AN/feed)
toutes les 10 minutes.

## Contenu

- `index.html` — structure de la page
- `styles.css` — feuille de style (esthétique éditoriale, papier crème)
- `app.js` — moteur (chargement de la baseline, parsing du flux RSS, filtres et rendu)
- `data/amendments.json` — baseline locale figée au 28 avril 2026, 6 h, contenant
  les 545 amendements avec leurs résumés synthétisés rédigés à la main
- `data/authors.json` — index des 57 députés ayant déposé un amendement, avec
  leur groupe parlementaire, pour résoudre le bon groupe quand un amendement
  est détecté via le flux RSS (matching par nom de famille avec gestion des
  particules « Le Feur », « de Pélichy », etc.)
- `.github/workflows/deploy.yml` — workflow GitHub Actions qui publie sur GitHub Pages

## Comment fonctionne la mise à jour automatique

1. Au chargement, `app.js` charge la baseline locale (`data/amendments.json`) et
   l'index auteurs (`data/authors.json`) en parallèle, et affiche les 545 amendements.
2. Il interroge ensuite le flux RSS `aspidistra2001.github.io/AN/feed` (avec deux
   proxys CORS de secours : `api.allorigins.win` et `corsproxy.io`).
3. Le parseur supporte à la fois le format **RSS 2.0** et **Atom** : il détecte
   automatiquement le format et extrait pour chaque entrée le numéro d'amendement
   (motif `[A-Z]{2,4}\d+`), le titre, le contenu, le lien et un éventuel sort.
4. **Pour les nouveaux amendements détectés via le flux**, le moteur :
   - tente de résoudre l'auteur en croisant le fragment extrait du flux
     (« M. Potier », « Mme PANTEL Sophie »…) avec `authors.json` ; le bon
     groupe parlementaire est attribué si le matching réussit, sinon une
     pastille « groupe ? » est affichée pour signaler le doute ;
   - affiche un bandeau « Synthèse à rédiger — extrait brut du flux RSS »
     au-dessus du résumé, qui est un extrait brut tronqué à 600 caractères ;
     ces résumés-brouillons sont stylisés en italique sur fond grisé pour
     indiquer qu'ils nécessitent un travail de reformulation.
5. Les amendements dont le sort change (par ex. « En traitement » → « Adopté »)
   sont signalés en **orange** avec badge `MODIFIÉ`.
6. La page rafraîchit le flux toutes les 10 minutes ou sur clic du bouton
   « Actualiser » en haut à droite.

## Workflow recommandé pour les nouveaux amendements

Quand un nouvel amendement apparaît via RSS, il est marqué « FLUX » avec un
résumé-extrait à reformuler. Pour produire une vraie synthèse :

1. Cliquez sur le numéro d'amendement → ouverture de la fiche officielle
   sur assemblee-nationale.fr ;
2. Lisez le dispositif et l'exposé des motifs ;
3. Éditez `data/amendments.json` : ajoutez une entrée pour cet amendement
   (en copiant la structure d'une entrée existante) avec un `summary` rédigé
   à la main, en retirant le flag `is_rss_new` et `summary_pending` ;
4. Poussez sur `main` : le workflow GitHub Actions redéploie automatiquement
   et le bandeau « à rédiger » disparaît.

## Déploiement sur GitHub Pages

1. Créer un dépôt GitHub (par exemple `veille-pjl-2632`).
2. Y pousser tous les fichiers de ce dossier (en conservant la structure).
3. Dans les paramètres du dépôt → **Pages** → **Source** : sélectionner
   **GitHub Actions** (et non « Deploy from a branch »).
4. À chaque push sur la branche `main`, le workflow `.github/workflows/deploy.yml`
   se déclenche et publie la page à l'adresse
   `https://<votre-pseudo>.github.io/<nom-du-depot>/`.

## Note sur le CORS

GitHub Pages renvoie par défaut l'en-tête `Access-Control-Allow-Origin: *`
sur les fichiers statiques, donc le `fetch` direct du flux RSS doit
fonctionner sans avoir besoin de proxy. Les deux proxys CORS de secours
sont prévus pour les cas de défaillance réseau.

## Maintenance des résumés

Si vous souhaitez modifier ou compléter les résumés synthétisés, éditez le
fichier `data/amendments.json` (champ `summary` de chaque amendement). Pour
un nouvel amendement détecté via le flux, le résumé affiché est un extrait
du flux lui-même tronqué à 600 caractères ; ajoutez-le manuellement à
`data/amendments.json` pour fournir une vraie synthèse.

## Licence

Code sous licence MIT. Données : Assemblée nationale, sous licence Etalab 2.0.
