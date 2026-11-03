# Cups Joséphine

Suivi du stock de cups à emporter du Café Joséphine (Nice) et alerte de commande.
Appli **séparée** de celle des cakes : son propre dépôt, ses propres secrets, son propre jeton Square.

## Fonctionnement

1. Deux fois par jour, GitHub Actions lance `cups_josephine.py`.
2. Le script lit les ventes dans Square (API Orders) et compte les cups à emporter, jour par jour :
   - transparents = boissons Iced + « Version glacée » ;
   - opaques = boissons chaudes − « Version glacée ».
   - **Ventes à emporter :** les articles suffixés « ae » (ancien catalogue, dans le nom de l'article ou de sa variante ; « sp » = sur place), sinon les lignes taxées à 5,5 %, à toutes les dates. L'option « Sur place / À emporter » fixe le taux de TVA (5,5 % / 10 %). Elle apparaît aussi dans les « fulfillments » de l'API (PICKUP = à emporter, IN_STORE = sur place), méthode de secours si un taux changeait. Une ligne sans taxe (boisson offerte à 0 €) prend le mode des autres lignes de sa commande.
3. Les comptes sont gardés dans `historique_cups.json`, mis à jour par le robot. Le premier lancement relit environ 14 mois. Ensuite, seuls les 14 derniers jours sont relus. Quand la règle de comptage change (`VERSION_COMPTAGE` en haut du script), tout l'historique est relu au passage suivant.
4. Le script lit le Google Sheet « Stocks cups » (inventaires et livraisons) et estime le stock :
   dernier inventaire + livraisons depuis − cups vendus depuis.
5. Il prévoit la consommation : rythme des 4 dernières semaines × variation de la même période l'an dernier.
6. Il publie la page sur GitHub Pages.

Réglages, en haut du script : cartons de 500 opaques et de 1 000 transparents, délai de livraison de 5 semaines, marge de 2 semaines (alerte sous 7 semaines de stock), 7 semaines de stock minimum à la réception.

## Google Sheet « Stocks cups »

Un seul onglet, avec ces colonnes en ligne 1 (voir `modele_stocks_cups.csv`) :

| Date | Type | Opaques | Transparents | Remarque |
|---|---|---|---|---|
| 02/11/2026 | Inventaire | 3200 | 2500 | |
| 20/11/2026 | Livraison | 5000 | | commande du 15/10 |

- **Inventaire** : cups comptés, faits **le soir après la fermeture**. Si vous comptez le matin avant l'ouverture, mettez la date de la veille. Une seule colonne remplie = inventaire de ce type seulement.
- **Livraison** : cups reçus (en cups, pas en cartons). Laissez vide le type non livré.
- Une livraison reçue le même jour qu'un inventaire est considérée comme déjà comptée dans cet inventaire.

## Installation (une seule fois)

1. **Jeton Square propre à cette appli.** Dans le Square Developer Dashboard, créez une nouvelle application « Cups Joséphine ». Dans ses identifiants de **production**, copiez le jeton d'accès. Il doit au minimum avoir le droit de lire les commandes (ORDERS_READ). Copiez aussi l'identifiant du lieu de Nice (onglet Locations).
2. **Dépôt GitHub** : créez le dépôt `cups` dans le compte Josephine-Nice, en **public** (nécessaire pour la page gratuite). Déposez-y les fichiers :
   - avec « Add file → Upload files » : `cups_josephine.py`, `icon.png`, `README.md`, `.gitignore`, `modele_stocks_cups.csv`, `env.exemple` ;
   - avec « Add file → Create new file » : tapez le nom `.github/workflows/page.yml`, puis collez le contenu du fichier `page.yml`.
3. **Secrets** : dépôt → Settings → Secrets and variables → Actions → New repository secret :
   - `SQUARE_ACCESS_TOKEN` : le jeton de l'étape 1 ;
   - `SQUARE_LOCATION_ID` : l'identifiant du lieu ;
   - `STOCK_SHEET_URL` : le lien CSV du Sheet (étape 5).
4. **Pages** : Settings → Pages → Source : **GitHub Actions**. Puis Settings → Actions → General → Workflow permissions : **Read and write permissions**.
5. **Sheet** : créez « Stocks cups », collez la ligne d'en-têtes, saisissez un premier inventaire. Ensuite : Fichier → Partager → Publier sur le web → l'onglet, format **CSV** → copiez le lien dans le secret `STOCK_SHEET_URL`.
6. **Premier lancement** : Actions → « Page cups Joséphine » → Run workflow. Mettez `02/11/2026` dans le champ de contrôle (date à la française). Le journal de l'étape « Contrôle d'un jour » doit afficher **Opaques : 24, Transparents : 17**.
7. La page est à l'adresse `https://josephine-nice.github.io/cups/`. Ajoutez-la à l'écran d'accueil (Partager → Sur l'écran d'accueil).

## À savoir

- La page est publique (non référencée) : rien de confidentiel n'y figure. Le Sheet publié en CSV est lisible par toute personne qui a le lien.
- Les horaires GitHub ne sont pas fiables. Après un inventaire ou une livraison, lancez « Run workflow » pour mettre la page à jour tout de suite.
- En cas d'erreur 500 de GitHub Pages, le workflow relance la publication une fois tout seul, 90 secondes plus tard.
- La partie « À vérifier » de la page liste les articles vendus à emporter que le programme ne reconnaît pas, par exemple une nouvelle boisson. Il faut alors l'ajouter dans `FROIDES` ou `CHAUDES`, en haut du script.
- Test local : `python cups_josephine.py --csv export.csv` compte les cups d'un export Square « détails des articles ».
