#!/usr/bin/env python3
"""
Cups Joséphine (Nice) - suivi du stock de cups à emporter et alerte de commande.

Le script :
  1. lit les ventes Square (API Orders) et compte les cups à emporter par jour :
       - transparents = boissons Iced + « Version glacée »
       - opaques      = boissons chaudes - « Version glacée »
     Ventes à emporter : articles ou variantes suffixés « ae » (ancien catalogue, « sp » = sur place),
     sinon lignes taxées à 5,5 % (le mode de traitement « À emporter » fixe ce taux), à toutes les dates.
  2. garde ces comptes jour par jour dans historique_cups.json (seuls les derniers jours sont relus ensuite) ;
  3. lit les inventaires et livraisons du Google Sheet « Stocks cups » (publié en CSV) ;
  4. estime le stock, prévoit la consommation avec la saisonnalité (année précédente) ;
  5. écrit une page HTML : stock, semaines restantes, date de rupture, alerte et quantités.

Usage :
  python cups_josephine.py --html site/index.html            # API Square + page
  python cups_josephine.py --csv export.csv                  # compte les cups d'un export Square (test)
  python cups_josephine.py --verifier-jour 02/11/2026        # contrôle d'un jour via l'API (journal du workflow)
"""
import argparse
import csv
import io
import json
import os
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

# ----------------------------------------------------------------- réglages
TZ = ZoneInfo("Europe/Paris")
CUPS_PAR_CARTON = {"opaques": 500, "transparents": 1000}
DELAI_LIVRAISON_SEMAINES = 5
MARGE_SEMAINES = 2                  # alerte quand le stock couvre moins de délai + marge
COUVERTURE_MINI_SEMAINES = 7        # après réception, le stock doit encore couvrir au moins ça
TVA_EMPORTER = 5.5
TVA_SUR_PLACE = 10.0
JOURS_HISTORIQUE_INITIAL = 420      # 1er lancement : un peu plus d'un an pour la saisonnalité
JOURS_A_RELIRE = 14                 # ensuite : on relit seulement les 14 derniers jours
VERSION_COMPTAGE = 2                # changer ce numéro quand la règle de comptage change : tout l'historique est relu
HISTORIQUE = Path(os.environ.get("HISTORIQUE_CUPS", Path(__file__).with_name("historique_cups.json")))
SQUARE_URL = "https://connect.squareup.com/v2"
SQUARE_VERSION = "2024-10-17"
TYPES = ("opaques", "transparents")
NOMS_TYPES = {"opaques": "Opaques (chauds)", "transparents": "Transparents (froids)"}
JOURS_FR = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MOIS_FR = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."]


def sans_accents(t: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", t.lower()) if unicodedata.category(c) != "Mn")


# Boissons toujours froides et boissons chaudes (noms sans accents, sans majuscules, sans suffixe)
FROIDES = {"iced strawberry matcha", "iced mango matcha", "iced dragon latte"}
CHAUDES = {sans_accents(n) for n in [
    "Latte", "Cappuccino", "Americano", "Espresso", "Double Espresso", "Déca", "Flat White", "Macchiato",
    "Cortado", "Mocha", "Caramel Latte", "Vanilla Latte", "Cinnamon Latte", "Rose Latte", "Chai Latte",
    "Dirty Chai Latte", "Pink Chai Latte", "Matcha Latte", "Rose Matcha Latte", "Vanille Matcha Latte",
    "Chocolat chaud", "Thé", "Rooibos"]}
GLACEE = "version glacee"
# Articles sans cup (reconnus par un mot du nom) : ils ne sont jamais signalés comme inconnus
SANS_CUP = ["eau", "lemonaid", "charitea", "cake", "lait", "sirop", "extra", "miel", "t-shirt", "tshirt",
            "montant personnalise", "bagel"]


# ------------------------------------------------------------- identification
def nettoyer(nom: str):
    """'+ Latte ae' -> ('latte', 'ae') ; 'Latte' -> ('latte', '') ; 'Cake de Mel (la part) sp' -> ('cake de mel', 'sp')."""
    n = sans_accents(nom or "").strip()
    n = re.sub(r"^\+\s*", "", n)
    suffixe = ""
    m = re.search(r"\s+(ae|sp)$", n)
    if m:
        suffixe, n = m.group(1), n[:m.start()]
    n = re.sub(r"\s*\(la part\)\s*", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n, suffixe


def categorie(nom_propre: str) -> str:
    """'froide', 'chaude', 'glacee', 'sans_cup' ou 'inconnu'."""
    if nom_propre in FROIDES or nom_propre.startswith("iced "):
        return "froide"
    if nom_propre == GLACEE:
        return "glacee"
    if nom_propre in CHAUDES:
        return "chaude"
    if any(re.search(rf"(^|\W){re.escape(m)}(\W|$)", nom_propre) for m in SANS_CUP):
        return "sans_cup"
    return "inconnu"


def cups_commande(lignes):
    """lignes : [(nom_propre, quantité)] des lignes À EMPORTER d'une commande.
    Retourne (opaques, transparents, {nom inconnu: quantité})."""
    froides = chaudes = glacees = 0.0
    inconnus = Counter()
    for nom, q in lignes:
        cat = categorie(nom)
        if cat == "froide":
            froides += q
        elif cat == "chaude":
            chaudes += q
        elif cat == "glacee":
            glacees += q
        elif cat == "inconnu":
            inconnus[nom] += q
    glacees_util = min(glacees, chaudes)           # une Version glacée transforme une boisson chaude
    return chaudes - glacees_util, froides + glacees, inconnus


# ---------------------------------------------------------------- lecture Square
def taux_ligne(li, taxes_cmd):
    """Taux de TVA appliqués à une ligne (liste de floats)."""
    taux = []
    for at in li.get("applied_taxes", []) or []:
        t = taxes_cmd.get(at.get("tax_uid"))
        if t is not None:
            taux.append(t)
    return taux


def suffixe_ligne(li):
    """'ae', 'sp' ou '' : suffixe de l'article, ou de sa variante (ex. article « Latte », variante « ae »)."""
    _, suffixe = nettoyer(li.get("name", ""))
    if not suffixe:
        variante = sans_accents(li.get("variation_name", "") or "").strip()
        if variante in ("ae", "sp"):
            suffixe = variante
        else:
            _, suffixe = nettoyer(variante)
    return suffixe


def emporter(li, taxes_cmd):
    """True si la ligne est vendue à emporter, False si sur place, None si on ne sait pas."""
    suffixe = suffixe_ligne(li)
    if suffixe:                                     # anciens articles « ae » / « sp »
        return suffixe == "ae"
    taux = taux_ligne(li, taxes_cmd)
    if any(abs(t - TVA_EMPORTER) < 0.01 for t in taux):
        return True
    if any(abs(t - TVA_SUR_PLACE) < 0.01 for t in taux):
        return False
    return None


def commandes_square(debut: date, fin: date):
    """Commandes terminées entre debut (inclus) et fin (exclu), dates de Paris."""
    token = os.environ["SQUARE_ACCESS_TOKEN"]
    lieu = os.environ["SQUARE_LOCATION_ID"]
    en_tetes = {"Authorization": f"Bearer {token}", "Square-Version": SQUARE_VERSION}
    t0 = datetime.combine(debut, time.min, TZ).astimezone(ZoneInfo("UTC"))
    t1 = datetime.combine(fin, time.min, TZ).astimezone(ZoneInfo("UTC"))
    corps = {
        "location_ids": [lieu],
        "query": {
            "filter": {
                "state_filter": {"states": ["COMPLETED"]},
                "date_time_filter": {"closed_at": {
                    "start_at": t0.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "end_at": t1.strftime("%Y-%m-%dT%H:%M:%SZ")}},
            },
            "sort": {"sort_field": "CLOSED_AT", "sort_order": "ASC"},
        },
        "limit": 500,
    }
    while True:
        for essai in range(3):
            r = requests.post(f"{SQUARE_URL}/orders/search", json=corps, headers=en_tetes, timeout=60)
            if r.status_code < 500:
                break
        if not r.ok:
            try:
                detail = "; ".join(f"{e.get('code')} : {e.get('detail')}" for e in r.json().get("errors", []))
            except ValueError:
                detail = r.text[:300]
            sys.exit(f"Square a refusé la lecture des commandes (HTTP {r.status_code}) - {detail}")
        data = r.json()
        yield from data.get("orders", [])
        if not data.get("cursor"):
            return
        corps["cursor"] = data["cursor"]


def compter_commande(cmd):
    """(jour, opaques, transparents, inconnus, lignes_non_classees, detail) pour une commande de l'API.
    detail : [(nom, quantité, True / False / None)] après report du mode de la commande."""
    jour = datetime.fromisoformat(cmd["closed_at"].replace("Z", "+00:00")).astimezone(TZ).date()
    taxes = {}
    for t in cmd.get("taxes", []) or []:
        try:
            taxes[t["uid"]] = float(t.get("percentage", "nan"))
        except (KeyError, ValueError):
            pass
    lignes, non_classees, detail = [], 0.0, []
    for li in cmd.get("line_items", []) or []:
        q = float(li.get("quantity", 1) or 1)
        nom, _ = nettoyer(li.get("name", ""))
        detail.append((nom, q, emporter(li, taxes)))
    # Ligne sans taux reconnu (ex. boisson offerte à 0 €) : elle prend le mode des autres lignes de la commande
    modes = {e for _, _, e in detail if e is not None}
    mode_cmd = modes.pop() if len(modes) == 1 else None
    detail = [(nom, q, mode_cmd if e is None else e) for nom, q, e in detail]
    for nom, q, e in detail:
        if e is None:
            if categorie(nom) in ("froide", "chaude", "glacee"):
                non_classees += q
            continue
        if e:
            lignes.append((nom, q))
    o, t, inc = cups_commande(lignes)
    return jour, o, t, inc, non_classees, detail


def comptes_square(debut: date, fin: date):
    """{jour iso: {'opaques', 'transparents', 'non_classees', 'inconnus': {nom: q}}} depuis l'API."""
    res = {}
    for cmd in commandes_square(debut, fin):
        jour, o, t, inc, nc, _ = compter_commande(cmd)
        j = res.setdefault(jour.isoformat(), {"opaques": 0.0, "transparents": 0.0, "non_classees": 0.0, "inconnus": {}})
        j["opaques"] += o
        j["transparents"] += t
        j["non_classees"] += nc
        for n, q in inc.items():
            j["inconnus"][n] = j["inconnus"].get(n, 0) + q
    return res


# ------------------------------------------------------------- lecture export CSV (test)
def comptes_csv(chemin):
    """Même calcul à partir d'un export Square « détails des articles »."""
    brut = open(chemin, "rb").read()
    texte = brut.decode("utf-16") if brut[:2] in (b"\xff\xfe", b"\xfe\xff") else brut.decode("utf-8-sig")
    delim = "\t" if "\t" in texte.splitlines()[0] else ","
    commandes = defaultdict(list)                   # (jour, n° transaction) -> lignes à emporter
    for l in csv.DictReader(io.StringIO(texte), delimiter=delim):
        jour = date.fromisoformat(l["Date"])
        nom, suffixe = nettoyer(l["Article"])
        option = sans_accents(l.get("Option de restauration", "") or "")
        if suffixe:
            a_emporter = suffixe == "ae"
        else:
            a_emporter = "emporter" in option
        if a_emporter:
            cle = (jour, l.get("Nº\xa0de transaction") or l.get("Nº de transaction") or id(l))
            commandes[cle].append((nom, float(l["Qté"].replace(",", "."))))
    res = {}
    for (jour, _), lignes in commandes.items():
        o, t, inc = cups_commande(lignes)
        j = res.setdefault(jour.isoformat(), {"opaques": 0.0, "transparents": 0.0, "non_classees": 0.0, "inconnus": {}})
        j["opaques"] += o
        j["transparents"] += t
        for n, q in inc.items():
            j["inconnus"][n] = j["inconnus"].get(n, 0) + q
    return res


# ------------------------------------------------------------------ historique
def lire_historique():
    if HISTORIQUE.exists():
        return json.loads(HISTORIQUE.read_text())
    return {"debut": None, "jours": {}}


def mettre_a_jour_historique(aujourdhui: date):
    """Complète historique_cups.json depuis Square. Retourne l'historique."""
    h = lire_historique()
    if h.get("debut") and h.get("version") != VERSION_COMPTAGE:
        debut = date.fromisoformat(h["debut"])      # règle de comptage changée : on relit tout
        h["jours"] = {}
    elif not h.get("debut"):
        debut = aujourdhui - timedelta(days=JOURS_HISTORIQUE_INITIAL)
        h["debut"] = debut.isoformat()
    else:
        dernier = max((date.fromisoformat(d) for d in h["jours"]), default=date.fromisoformat(h["debut"]))
        debut = min(dernier, aujourdhui) - timedelta(days=JOURS_A_RELIRE)
        debut = max(debut, date.fromisoformat(h["debut"]))
    nouveaux = comptes_square(debut, aujourdhui + timedelta(days=1))
    for d in list(h["jours"]):
        if date.fromisoformat(d) >= debut:
            del h["jours"][d]
    h["jours"].update(nouveaux)
    h["jours"] = dict(sorted(h["jours"].items()))
    h["version"] = VERSION_COMPTAGE
    h["mis_a_jour"] = datetime.now(TZ).isoformat(timespec="minutes")
    HISTORIQUE.write_text(json.dumps(h, indent=1, ensure_ascii=False))
    return h


def serie(h, typ):
    """{date: cups} pour un type ; les jours sans vente valent 0 entre le début et aujourd'hui."""
    return defaultdict(float, {date.fromisoformat(d): v.get(typ, 0) for d, v in h["jours"].items()})


# --------------------------------------------------------------- stocks (Sheet)
def _date_souple(texte: str):
    texte = (texte or "").strip()
    m = re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})", texte)
    if m:
        try:
            return date(datetime.now(TZ).year, int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(texte, fmt).date()
        except ValueError:
            pass
    return None


def _nombre(texte):
    t = re.sub(r"[\s \xa0]", "", str(texte or "")).replace(",", ".")
    if t == "":
        return None
    try:
        return float(t)
    except ValueError:
        return None


def lire_mouvements(source=None):
    """Lignes du Sheet « Stocks cups » : Date | Type (Inventaire / Livraison) | Opaques | Transparents.
    Retourne (liste de (date, 'inventaire'|'livraison', {type: n ou None}), avertissements)."""
    source = source or os.environ.get("STOCK_SHEET_URL")
    if not source:
        return [], ["Aucun Google Sheet des stocks n'est configuré (secret STOCK_SHEET_URL)."]
    try:
        if source.startswith("http"):
            r = requests.get(source, timeout=30)
            r.raise_for_status()
            texte = r.content.decode("utf-8-sig")
        else:
            texte = Path(source).read_text(encoding="utf-8-sig")
    except (requests.RequestException, OSError) as err:
        return [], [f"Le Google Sheet des stocks est illisible ({err})."]
    premiere = texte.splitlines()[0] if texte.splitlines() else ""
    delim = ";" if premiere.count(";") > premiere.count(",") else ","
    res, alertes = [], []
    for ligne in csv.DictReader(io.StringIO(texte), delimiter=delim):
        l = {sans_accents((k or "").strip()): (v or "").strip() for k, v in ligne.items()}
        if not any(l.values()):
            continue
        d = _date_souple(l.get("date", ""))
        t = sans_accents(l.get("type", ""))
        genre = "inventaire" if t.startswith("inv") else "livraison" if t.startswith("liv") else None
        valeurs = {"opaques": _nombre(l.get("opaques")), "transparents": _nombre(l.get("transparents"))}
        if not d or not genre or all(v is None for v in valeurs.values()):
            alertes.append(f"Ligne du Sheet ignorée (date, type ou quantités illisibles) : {', '.join(v for v in ligne.values() if v)}")
            continue
        res.append((d, genre, valeurs))
    return res, alertes


def stock_estime(mouvements, conso, typ, aujourdhui: date):
    """Stock estimé maintenant = dernier inventaire + livraisons après cette date - cups vendus après cette date.
    Un inventaire est réputé fait le soir, après la fermeture."""
    inv = [(d, v[typ]) for d, g, v in mouvements if g == "inventaire" and v[typ] is not None and d <= aujourdhui]
    if not inv:
        return None
    d0 = max(d for d, _ in inv)
    depart = [n for d, n in inv if d == d0][-1]
    livre = sum(v[typ] or 0 for d, g, v in mouvements if g == "livraison" and d0 < d <= aujourdhui)
    vendu = sum(n for d, n in conso.items() if d0 < d <= aujourdhui)
    return {"date": d0, "inventaire": depart, "livre": livre, "vendu": vendu, "stock": depart + livre - vendu}


# ----------------------------------------------------------------- prévision
def somme(conso, debut: date, fin: date):
    """Somme des jours debut <= j < fin."""
    return sum(conso.get(debut + timedelta(days=i), 0) for i in range((fin - debut).days))


def profil_semaine(conso_tot, aujourdhui: date):
    """Part de chaque jour de la semaine dans la consommation (8 dernières semaines)."""
    tot = [0.0] * 7
    for i in range(1, 57):
        d = aujourdhui - timedelta(days=i)
        tot[d.weekday()] += conso_tot.get(d, 0)
    s = sum(tot)
    if s <= 0:
        return [1 / 6] * 6 + [0]                    # par défaut : lundi -> samedi
    return [t / s for t in tot]


def prevision(conso, debut_hist: date, aujourdhui: date, conso_tot, semaines=60):
    """Consommation prévue par semaine (liste de (lundi de départ, cups)) à partir de demain.
    Niveau récent (4 dernières semaines) × variation saisonnière constatée l'an dernier.
    Retourne aussi un résumé pour l'affichage."""
    hier = aujourdhui                                # la journée en cours est exclue (incomplète)
    recent = somme(conso, hier - timedelta(days=28), hier) / 4
    ly_ok = debut_hist <= hier - timedelta(days=364 + 28)
    ly_recent = somme(conso, hier - timedelta(days=364 + 28), hier - timedelta(days=364)) / 4 if ly_ok else None
    comparable = ly_recent is not None and ly_recent >= 15   # l'an dernier à la même période, le café tournait
    sem = []
    for k in range(semaines):
        s = aujourdhui + timedelta(days=1 + 7 * k)
        ref = s - timedelta(days=364)               # même jour de semaine, un an plus tôt
        if debut_hist <= ref - timedelta(days=7) and ref + timedelta(days=14) <= hier:
            ly_sem = somme(conso, ref - timedelta(days=7), ref + timedelta(days=14)) / 3   # lissé sur 3 semaines
        elif k >= 52 and len(sem) >= 52:
            sem.append((s, sem[k - 52][1]))         # au-delà d'un an : on répète la prévision
            continue
        else:
            ly_sem = None
        if ly_sem is None:
            v = recent
        elif comparable and recent > 0:
            v = recent * min(max(ly_sem / ly_recent, 0.1), 8)     # rythme récent × variation saisonnière
        elif comparable:
            v = ly_sem                              # café fermé ces dernières semaines : l'an dernier tel quel
        else:
            v = max(recent, ly_sem)                 # 1re année : prudence, le plus haut des deux
        sem.append((s, v))
    resume = {"recent": recent, "ly_recent": ly_recent, "saisonnalite": ly_ok, "comparable": comparable}
    return sem, resume


def jours_prevus(sem, profil):
    """{date: cups prévus} jour par jour, à partir de demain."""
    res = {}
    for s, v in sem:
        for i in range(7):
            d = s + timedelta(days=i)
            res[d] = v * profil[d.weekday()]
    return res


def date_rupture(stock, prev_jour, depuis: date):
    """Premier jour où la consommation cumulée dépasse le stock. None si au-delà de la prévision."""
    cumul = 0.0
    for d in sorted(prev_jour):
        if d <= depuis:
            continue
        cumul += prev_jour[d]
        if cumul > stock:
            return d
    return None


# ---------------------------------------------------------------------- calcul
def analyser(h, mouvements, aujourdhui: date):
    debut_hist = date.fromisoformat(h["debut"]) if h.get("debut") else aujourdhui
    conso = {t: serie(h, t) for t in TYPES}
    conso_tot = defaultdict(float)
    for t in TYPES:
        for d, v in conso[t].items():
            conso_tot[d] += v
    profil = profil_semaine(conso_tot, aujourdhui)
    res = {}
    for t in TYPES:
        sem, resume = prevision(conso[t], debut_hist, aujourdhui, conso_tot)
        prev = jours_prevus(sem, profil)
        st = stock_estime(mouvements, conso[t], t, aujourdhui)
        r = {"semaines": sem, "resume": resume, "stock": st, "prev": prev}
        if st:
            s = max(st["stock"], 0)
            rupt = date_rupture(s, prev, aujourdhui)
            r["rupture"] = rupt
            r["semaines_restantes"] = ((rupt - aujourdhui).days / 7) if rupt else None
            seuil = DELAI_LIVRAISON_SEMAINES + MARGE_SEMAINES
            r["commander_avant"] = (rupt - timedelta(weeks=seuil)) if rupt else None
            r["alerte"] = rupt is not None and rupt <= aujourdhui + timedelta(weeks=seuil)
            r["bientot"] = (not r["alerte"]) and rupt is not None and rupt <= aujourdhui + timedelta(weeks=seuil + 3)
            # Quantité minimale : à la réception (dans DELAI semaines), le stock doit encore couvrir COUVERTURE_MINI semaines
            cible = aujourdhui + timedelta(weeks=DELAI_LIVRAISON_SEMAINES + COUVERTURE_MINI_SEMAINES)
            besoin = sum(v for d, v in prev.items() if aujourdhui < d <= cible)
            carton = CUPS_PAR_CARTON[t]
            manque = besoin - s
            r["minimum"] = 0 if manque <= 0 else int(-(-manque // carton) * carton)
            # Tableau des quantités : combien de temps tient chaque commande possible
            quantites = sorted({q for q in [r["minimum"]] + [carton * k for k in (1, 2, 3, 4, 6, 8, 10, 15, 20)] if q > 0})
            r["tableau"] = [(q, date_rupture(s + q, prev, aujourdhui)) for q in quantites]
        res[t] = r
    return res, conso, profil


# ----------------------------------------------------------------- page HTML
def fr_nb(n):
    return f"{n:,.0f}".replace(",", " ")


def fr_date(d, annee=False):
    if d is None:
        return "plus d'un an"
    t = f"{JOURS_FR[d.weekday()][:3]}. {d.day} {MOIS_FR[d.month - 1]}"
    return t + (f" {d.year}" if annee or d.year != datetime.now(TZ).year else "")


def graphique(conso, sem, aujourdhui: date, debut_hist: date):
    """Petit graphique SVG : consommation par semaine (52 passées, réelles) puis 26 à venir (prévues)."""
    passe = []
    fin = aujourdhui + timedelta(days=1)
    for k in range(52, 0, -1):
        s = fin - timedelta(days=7 * k)
        if s < debut_hist:
            continue
        passe.append((s, somme(conso, s, s + timedelta(days=7))))
    futur = sem[:26]
    pts = passe + futur
    if not pts:
        return ""
    vmax = max(v for _, v in pts) or 1
    L, H, mg = 720, 170, 28
    larg = (L - mg) / len(pts)
    barres = []
    for i, (s, v) in enumerate(pts):
        h = (H - 24) * v / vmax
        x = mg + i * larg
        cls = "p" if i < len(passe) else "f"
        barres.append(f"<rect class='{cls}' x='{x:.1f}' y='{H - 18 - h:.1f}' width='{max(larg - 1.5, 1):.1f}' height='{h:.1f}'>"
                      f"<title>Semaine du {s:%d/%m/%Y} : {v:.0f} cups{' (prévu)' if cls == 'f' else ''}</title></rect>")
        if s.day <= 7 and i % 1 == 0:
            barres.append(f"<text x='{x:.1f}' y='{H - 4}' class='ax'>{MOIS_FR[s.month - 1]}</text>")
    xs = mg + len(passe) * larg
    return (f"<svg viewBox='0 0 {L} {H}' class='graph' role='img' aria-label='Consommation par semaine'>"
            f"<text x='0' y='12' class='ax'>{vmax:.0f}</text><line x1='{mg}' x2='{L}' y1='{H - 18}' y2='{H - 18}' class='axe'/>"
            f"{''.join(barres)}<line x1='{xs:.1f}' x2='{xs:.1f}' y1='0' y2='{H - 18}' class='auj'/>"
            f"<text x='{xs + 4:.1f}' y='12' class='ax'>aujourd’hui → prévision</text></svg>")


def generer_html(res, conso, h, aujourdhui: date, chemin, alertes):
    from html import escape as e
    debut_hist = date.fromisoformat(h["debut"]) if h.get("debut") else aujourdhui
    seuil = DELAI_LIVRAISON_SEMAINES + MARGE_SEMAINES
    cartes, details = [], []
    for t in TYPES:
        r = res[t]
        st = r["stock"]
        carton = CUPS_PAR_CARTON[t]
        if not st:
            etat = "<div class='etat neutre'>Aucun inventaire saisi : ajoutez une ligne « Inventaire » dans le Google Sheet.</div>"
            chiffres = ""
            tableau = ""
        else:
            s = st["stock"]
            if r["alerte"]:
                etat = (f"<div class='etat alerte'>⚠️ À commander maintenant<br><span>minimum conseillé : "
                        f"<b>{fr_nb(r['minimum'])} cups</b></span></div>")
            elif r["bientot"]:
                etat = f"<div class='etat bientot'>Commande à prévoir avant le <b>{fr_date(r['commander_avant'])}</b></div>"
            else:
                quand = (f"commander avant le {fr_date(r['commander_avant'])}" if r["commander_avant"]
                         else "pas de commande nécessaire avant au moins un an")
                etat = f"<div class='etat ok'>✔ Stock suffisant<br><span>{quand}</span></div>"
            sr = r["semaines_restantes"]
            chiffres = (
                "<div class='chiffres'>"
                f"<div><div class='gros'>{fr_nb(max(s, 0))}</div><div class='lb'>cups en stock (estimé)</div></div>"
                f"<div><div class='gros'>{fr_nb(r['semaines'][0][1])}</div><div class='lb'>cups / semaine prévus</div></div>"
                f"<div><div class='gros'>{'> 52' if sr is None else f'{sr:.1f}'.replace('.', ',')}</div><div class='lb'>semaines restantes</div></div>"
                f"<div><div class='moy'>{fr_date(r['rupture'])}</div><div class='lb'>rupture estimée</div></div>"
                "</div>")
            lignes = "".join(
                f"<tr{' class=min' if q == r['minimum'] else ''}><td>{fr_nb(q)} cups"
                f"{' <span class=tag>minimum</span>' if q == r['minimum'] else ''}</td>"
                f"<td class='sm'>{q // carton} carton{'s' if q // carton > 1 else ''}</td>"
                f"<td>{fr_date(d)}</td>"
                f"<td>{fr_date(d - timedelta(weeks=seuil)) if d else '—'}</td></tr>"
                for q, d in r["tableau"])
            tableau = (f"<h3>Si je commande…</h3><table><tr><th>Quantité</th><th></th><th>Le stock tient jusqu’au</th>"
                       f"<th>Commande suivante avant le</th></tr>{lignes}</table>"
                       f"<p class='sm'>Livraison supposée dans {DELAI_LIVRAISON_SEMAINES} semaines ; la prévision tient compte de la saison.</p>")
        cartes.append(f"<section class='carte {t}'><h2>{e(NOMS_TYPES[t])}</h2>{etat}{chiffres}{tableau}</section>")

        # détail du calcul
        rs = r["resume"]
        if rs["comparable"]:
            ly = (f"{fr_nb(rs['ly_recent'])} cups/semaine à la même période l’an dernier. "
                  "Prévision = rythme récent × variation constatée l’an dernier")
        elif rs["saisonnalite"]:
            ly = ("l’an dernier à la même période, le café n’avait pas encore assez de ventes pour comparer. "
                  "Prévision prudente : pour chaque semaine, le plus haut entre le rythme récent et la même semaine l’an dernier")
        else:
            ly = "pas encore d’historique de l’an dernier : prévision = rythme récent"
        prochaines = "".join(f"<td>{fr_nb(v)}</td>" for _, v in r["semaines"][:8])
        tetes = "".join(f"<th>{s:%d/%m}</th>" for s, _ in r["semaines"][:8])
        bloc_stock = ""
        if st:
            bloc_stock = (f"<p>Stock = inventaire du {st['date']:%d/%m/%Y} ({fr_nb(st['inventaire'])}) "
                          f"+ livraisons depuis ({fr_nb(st['livre'])}) − cups vendus depuis ({fr_nb(st['vendu'])}) "
                          f"= <b>{fr_nb(st['stock'])}</b>"
                          f"{' <span class=warn>(négatif : refaire un inventaire)</span>' if st['stock'] < 0 else ''}.</p>")
        details.append(
            f"<h3>{e(NOMS_TYPES[t])}</h3>{bloc_stock}"
            f"<p>Rythme des 4 dernières semaines : <b>{fr_nb(rs['recent'])} cups/semaine</b> ; {ly}.</p>"
            f"<div class='wrap'><table><tr><th>Semaine à partir du</th>{tetes}</tr><tr><td>Prévu</td>{prochaines}</tr></table></div>"
            f"{graphique(conso[t], r['semaines'], aujourdhui, debut_hist)}")

    # ventes récentes, jour par jour
    jours = [aujourdhui - timedelta(days=i) for i in range(13, -1, -1)]
    lignes_j = "".join(
        f"<tr><td>{JOURS_FR[d.weekday()][:3]}. {d:%d/%m}</td>"
        f"<td>{fr_nb(conso['opaques'].get(d, 0))}</td><td>{fr_nb(conso['transparents'].get(d, 0))}</td></tr>"
        for d in jours)

    # contrôles
    inconnus, non_cl = Counter(), 0.0
    for d, v in h["jours"].items():
        if date.fromisoformat(d) >= aujourdhui - timedelta(days=60):
            inconnus.update(v.get("inconnus", {}))
            non_cl += v.get("non_classees", 0)
    ctrl = [f"<li>{e(a)}</li>" for a in alertes]
    if inconnus:
        ctrl.append("<li>Articles vendus à emporter non reconnus ces 60 derniers jours (non comptés) : "
                    + ", ".join(f"{e(n)} ({q:.0f})" for n, q in inconnus.most_common(15))
                    + ". S’il s’agit de boissons, il faut les ajouter au programme.</li>")
    if non_cl:
        ctrl.append(f"<li>{non_cl:.0f} boisson(s) des 60 derniers jours sans taux de TVA reconnu (ni 5,5 % ni 10 %) : non comptées.</li>")
    controles = f"<h2>À vérifier</h2><ul class='ctrl'>{''.join(ctrl)}</ul>" if ctrl else ""

    alerte_titre = any(res[t].get("alerte") for t in TYPES)
    page = f"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<link rel="apple-touch-icon" href="icon.png"><link rel="icon" href="icon.png">
<meta name="apple-mobile-web-app-title" content="Cups">
<title>{'⚠️ ' if alerte_titre else ''}Cups · Joséphine</title>
<style>
:root{{--bg:#f6f4ef;--fg:#24211e;--mut:#736b62;--line:#e2dbcf;--card:#fff;--acc:#b8412f;--ok:#2f7a4d;--orange:#b7791f;--op:#6b4a3a;--tr:#3a7ca5}}
@media (prefers-color-scheme:dark){{:root{{--bg:#1c1a18;--fg:#efe9e0;--mut:#a39a90;--line:#38332e;--card:#272320;--acc:#ec8b78;--ok:#6fcf97;--orange:#e7b05a;--op:#c9a08c;--tr:#7fb8dc}}}}
*{{box-sizing:border-box}} body{{font:15px/1.45 -apple-system,system-ui,sans-serif;background:var(--bg);color:var(--fg);margin:0;padding:22px 16px 40px}}
main{{max-width:980px;margin:auto}} h1{{font-size:24px;margin:0}} h2{{font-size:19px;margin:0 0 12px}} h3{{font-size:15px;margin:20px 0 8px}}
.haut{{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:16px}}
.btn{{font:inherit;font-weight:600;padding:8px 14px;border:1px solid var(--line);border-radius:10px;background:var(--card);color:var(--fg);cursor:pointer}}
.cartes{{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:16px}}
.carte{{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:18px;border-top:6px solid var(--op)}} .carte.transparents{{border-top-color:var(--tr)}}
.etat{{border-radius:12px;padding:10px 14px;font-weight:600;margin-bottom:14px}} .etat span{{font-weight:400}}
.etat.ok{{background:rgba(47,122,77,.12);color:var(--ok)}} .etat.alerte{{background:rgba(184,65,47,.13);color:var(--acc);font-size:18px}}
.etat.bientot{{background:rgba(183,121,31,.14);color:var(--orange)}} .etat.neutre{{background:var(--bg);color:var(--mut);font-weight:400}}
.chiffres{{display:grid;grid-template-columns:1fr 1fr;gap:12px 16px}} .gros{{font-size:30px;font-weight:700;line-height:1.1}} .moy{{font-size:18px;font-weight:700;line-height:1.6}}
.lb,.sm{{font-size:13px;color:var(--mut)}} .warn{{color:var(--acc)}}
table{{border-collapse:collapse;width:100%;font-size:14px}} th,td{{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left}} th{{font-size:12px;color:var(--mut);font-weight:600}}
tr.min td{{font-weight:600}} .tag{{font-size:11px;background:var(--acc);color:#fff;border-radius:6px;padding:1px 6px;margin-left:4px}}
.wrap{{overflow-x:auto}} .det{{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:18px;margin-top:22px}}
.graph{{width:100%;height:auto;margin-top:10px}} .graph .p{{fill:var(--mut);opacity:.55}} .graph .f{{fill:var(--acc);opacity:.75}}
.graph .ax{{font-size:10px;fill:var(--mut)}} .graph .axe{{stroke:var(--line)}} .graph .auj{{stroke:var(--fg);stroke-dasharray:3 3}}
.ctrl li{{margin-bottom:6px}} .petit{{max-width:420px}}
</style></head><body><main>
<div class="haut"><div><h1>Cups à emporter</h1><div class="sm">Café Joséphine · mis à jour le {datetime.now(TZ):%d/%m/%Y à %H:%M}</div></div>
<button class="btn" onclick="location.href=location.pathname+'?r='+Date.now()">↻ Actualiser</button></div>
<div class="cartes">{''.join(cartes)}</div>
<div class="det">{controles}
<h2>Détail des calculs</h2>
<p class="sm">Alerte quand le stock couvre moins de {seuil} semaines (livraison {DELAI_LIVRAISON_SEMAINES} semaines + marge {MARGE_SEMAINES} semaines).
Le minimum conseillé laisse encore {COUVERTURE_MINI_SEMAINES} semaines de stock à la réception. Cartons : {CUPS_PAR_CARTON['opaques']} opaques, {CUPS_PAR_CARTON['transparents']} transparents.
Prévision : rythme des 4 dernières semaines × variation de la même période l’an dernier (moyenne de 3 semaines).
Un inventaire est compté comme fait le soir, après la fermeture.</p>
{''.join(details)}
<h3>Cups vendus à emporter, 14 derniers jours</h3>
<div class="petit"><table><tr><th>Jour</th><th>Opaques</th><th>Transparents</th></tr>{lignes_j}</table></div>
<p class="sm">Historique Square disponible depuis le {debut_hist:%d/%m/%Y}.</p>
</div></main></body></html>"""
    Path(chemin).parent.mkdir(parents=True, exist_ok=True)
    Path(chemin).write_text(page, encoding="utf-8")


# ----------------------------------------------------------------- contrôle d'un jour
def verifier_jour(jour: date):
    """Affiche, pour un jour, les cups comptés par l'API et ce que contiennent les commandes
    (taux de TVA, fulfillments), pour comparer avec l'export Square."""
    taux_vus, fulf_vus, ex = Counter(), Counter(), None
    par_boisson = defaultdict(Counter)              # nom -> {'à emporter', 'sur place', 'non classée'}
    tot_o = tot_t = non_cl = 0.0
    for cmd in commandes_square(jour, jour + timedelta(days=1)):
        _, o, t, inc, nc, detail = compter_commande(cmd)
        for nom, q, e in detail:
            if categorie(nom) in ("froide", "chaude", "glacee", "inconnu"):
                par_boisson[nom][{True: "à emporter", False: "sur place", None: "non classée"}[e]] += q
        tot_o += o
        tot_t += t
        non_cl += nc
        taxes = {x["uid"]: x.get("percentage") for x in cmd.get("taxes", []) or []}
        for li in cmd.get("line_items", []) or []:
            for at in li.get("applied_taxes", []) or []:
                taux_vus[taxes.get(at.get("tax_uid"))] += 1
        for f in cmd.get("fulfillments", []) or []:
            fulf_vus[f.get("type")] += 1
            ex = ex or f
    print(f"=== Contrôle du {jour:%d/%m/%Y} ===")
    print(f"Opaques : {tot_o:.0f}   Transparents : {tot_t:.0f}   Boissons sans TVA reconnue : {non_cl:.0f}")
    print("Taux de TVA rencontrés (lignes) :", dict(taux_vus))
    print("Types de fulfillment rencontrés :", dict(fulf_vus) or "aucun")
    if ex:
        print("Exemple de fulfillment :", json.dumps(ex, ensure_ascii=False)[:800])
    print("Détail par boisson :")
    for nom in sorted(par_boisson):
        c = par_boisson[nom]
        print(f"  {nom} : " + ", ".join(f"{k} {c[k]:.0f}" for k in ("à emporter", "sur place", "non classée") if c[k]))


# ----------------------------------------------------------------- réglages .env
def charger_env(chemin=None):
    """Lit le fichier .env à côté du script (NOM=valeur). N'affiche jamais les valeurs."""
    chemin = Path(chemin) if chemin else Path(__file__).with_name(".env")
    if not chemin.exists():
        return
    for ligne in chemin.read_text(encoding="utf-8-sig").splitlines():
        ligne = ligne.strip()
        if not ligne or ligne.startswith("#") or "=" not in ligne:
            continue
        cle, valeur = ligne.split("=", 1)
        valeur = valeur.strip().strip("'\"")
        if cle.strip() and valeur:
            os.environ.setdefault(cle.strip(), valeur)


# --------------------------------------------------------------------- main
def main():
    charger_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--html", nargs="?", const="site/index.html", help="crée la page HTML")
    ap.add_argument("--csv", help="compte les cups d'un export Square (test, n'utilise pas l'API)")
    ap.add_argument("--stock-csv", help="fichier ou lien CSV des stocks (sinon STOCK_SHEET_URL)")
    ap.add_argument("--date", help="date du jour AAAA-MM-JJ (test)")
    ap.add_argument("--sans-square", action="store_true", help="n'interroge pas Square (utilise historique_cups.json tel quel)")
    ap.add_argument("--verifier-jour", help="contrôle un jour (ex. 02/11/2026) via l'API et l'affiche")
    a = ap.parse_args()

    if a.csv:
        for j, v in sorted(comptes_csv(a.csv).items()):
            print(f"{j} : opaques {v['opaques']:.0f}, transparents {v['transparents']:.0f}"
                  + (f" | non reconnus : {v['inconnus']}" if v["inconnus"] else ""))
        return
    if a.verifier_jour:
        jour = _date_souple(a.verifier_jour)
        if not jour:
            sys.exit(f"Date de contrôle illisible : « {a.verifier_jour} ». Exemple : 02/11/2026")
        verifier_jour(jour)
        if not a.html:
            return

    aujourdhui = (_date_souple(a.date) or sys.exit(f"Date illisible : {a.date}")) if a.date else datetime.now(TZ).date()
    h = lire_historique() if a.sans_square else mettre_a_jour_historique(aujourdhui)
    mouvements, alertes = lire_mouvements(a.stock_csv)
    res, conso, _ = analyser(h, mouvements, aujourdhui)
    for t in TYPES:
        r = res[t]
        if r["stock"]:
            print(f"{NOMS_TYPES[t]} : stock {r['stock']['stock']:.0f}, {r['semaines'][0][1]:.0f}/semaine, "
                  f"rupture {r['rupture']}, alerte {r['alerte']}, minimum {r['minimum']}")
        else:
            print(f"{NOMS_TYPES[t]} : pas d'inventaire")
    for al in alertes:
        print("[attention]", al, file=sys.stderr)
    if a.html:
        generer_html(res, conso, h, aujourdhui, a.html, alertes)
        import shutil
        src = Path(__file__).with_name("icon.png")
        if src.exists():
            shutil.copy(src, Path(a.html).parent / "icon.png")
        print(f"Page créée : {a.html}")


if __name__ == "__main__":
    main()
