# Cups Joséphine

Suivi du stock de cups à emporter du Café Joséphine (Nice) et alerte de commande.
Le détail du fonctionnement et de l'installation est dans `README.md`.

## Qui et comment

- Propriétaire : Jean-Charles. Répondre en français, simplement, sans jargon technique.
- Avant toute publication sur GitHub, lui montrer ce qui change et attendre son accord.
- Quand une modification change ce qui est décrit ici, mettre cette note à jour dans le même envoi.

## Où ça tourne

- Dépôt GitHub `Josephine-Nice/cups`, **public**. Le compte `Josephine-Nice` est connecté sur ce Mac (`gh`).
- GitHub Actions (`.github/workflows/page.yml`) lance `cups_josephine.py` vers 5h-6h et vers 18h-19h (Paris).
  Les horaires GitHub ne sont pas fiables : après un inventaire ou une livraison, on lance « Run workflow » à la main.
- La page est publiée sur https://josephine-nice.github.io/cups/
- Le robot `cups-bot` met à jour `historique_cups.json` à chaque passage : faire un `git pull` avant de modifier quoi que ce soit.

## Données

- Ventes : API Square (Orders), avec un jeton Square **propre à cette appli**, différent de celui des cakes.
- Inventaires et livraisons : Google Sheet « Stocks cups », publié en CSV (colonnes de `modele_stocks_cups.csv`).
- Secrets GitHub : `SQUARE_ACCESS_TOKEN`, `SQUARE_LOCATION_ID`, `STOCK_SHEET_URL`.

## Règles métier (réglages en haut de `cups_josephine.py`)

- Transparents = boissons « Iced » + option « Version glacée » ; opaques = boissons chaudes − « Version glacée ».
- Vente à emporter : suffixe « ae » jusqu'au 01/11/2026, puis ligne taxée à 5,5 % depuis le 02/11/2026.
- Une nouvelle boisson non reconnue apparaît dans « À vérifier » sur la page : l'ajouter à `FROIDES` ou `CHAUDES`.
- Cartons de 500 opaques et de 1 000 transparents ; livraison en 5 semaines + 2 de marge (alerte sous 7 semaines de stock).

## Précautions

- Le dépôt et la page sont publics : jamais de jeton, de numéro de téléphone ni de lien de Sheet dans le code ou les fichiers publiés.
- `.env` (secrets pour les tests en local) ne doit jamais être publié. Il est exclu par `.git/info/exclude` sur ce Mac.

## Autres applis Joséphine

- Dossier `~/josephine/cakes` (dépôt `Josephine-Nice/cakes`) : Cakes (production de Paula) et Stock cakes (saisie du stock).
  Elles lisent aussi Square : leur code peut servir de modèle.
