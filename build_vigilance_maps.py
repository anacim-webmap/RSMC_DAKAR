#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_vigilance_maps.py
========================

Pipeline de production des cartes de vigilance SWFDP (Afrique de l'Ouest).

Ce script comporte désormais une étape PRÉALABLE de récupération des
polygones nouvellement redessinés / refusionnés dans QGIS, puis fait le
reste du traitement habituel.

  0. (PRÉALABLE) Lit les 5 nouveaux fichiers fusionnés en .gpkg
     carte_1.gpkg ... carte_5.gpkg (un par échéance). Ces fichiers ne
     contiennent, en général, QUE les géométries à jour et les champs
     techniques issus de la fusion QGIS ("layer", "path") : les attributs
     métier (Phenomene, Seuil_code, Seuil_val, Unite, Couleur_ph,
     Commentair) y sont vides.
     On "remplit" alors chaque nouveau polygone avec le contenu de son
     fichier correspondant de la génération précédente :
         carte_1.gpkg -> carte_jour_1.gpkg
         carte_2.gpkg -> carte_jour_2.gpkg
         carte_3.gpkg -> carte_jour_3.gpkg
         carte_4.gpkg -> carte_jour_4.gpkg
         carte_5.gpkg -> carte_jour_5.gpkg
     L'appariement se fait par le champ commun "layer" (ex: "Hs>2m",
     "FF>40km:h", "T>40°C", ...), qui identifie le phénomène/seuil
     d'origine (nom du shapefile fusionné). Si plusieurs polygones neufs
     partagent le même "layer" dans une même échéance, les attributs des
     entités de référence correspondantes leur sont appliqués un par un,
     en recommençant depuis le début si besoin (plus de polygones neufs
     que d'entités de référence pour ce "layer").
     Si aucune entité de référence n'existe pour un "layer" donné dans le
     fichier du jour correspondant (ex: nouveau phénomène, ou fichier
     carte_jour_X.gpkg absent au premier lancement), le script se rabat
     sur une référence "globale" construite en regroupant les 5 fichiers
     de référence, puis, en dernier recours, sur une table de valeurs par
     défaut (LAYER_DEFAULTS, à adapter/compléter si de nouveaux types de
     phénomènes apparaissent).
  1. Harmonise les attributs de chaque entité pour qu'ils utilisent
     exactement les mêmes champs / noms de champs, avec le nom complet
     de chaque phénomène (ex: "G: Vent", "Hs: Houle", "RR: Pluie",
     "T: Température").
  2. Garantit que la couleur (Couleur_ph) est toujours la même pour un
     même couple (Phenomene, Seuil_val), même si une des tables sources
     contient une couleur incohérente : une palette de référence est
     construite automatiquement à partir de l'ensemble des 5 fichiers
     (vote majoritaire), puis appliquée à toutes les entités.
  3. Calcule automatiquement Date_début et Date_fin pour chaque
     échéance à partir d'une date de base (Jour-J = J1) :
         J1 : date_base        -> date_base + 1 jour
         J2 : date_base + 1j   -> date_base + 2 jours
         J3 : date_base + 2j   -> date_base + 3 jours
         J4 : date_base + 3j   -> date_base + 4 jours
         J5 : date_base + 4j   -> date_base + 5 jours
     La date de base vaut par défaut la date du jour d'exécution du
     script, et peut être fixée avec --date-base AAAA-MM-JJ.
  4. Réécrit, pour chaque jour, un GeoPackage propre et homogène
     (carte_jour_X.gpkg) -> utile si vous rechargez ces fichiers dans QGIS.
     C'est ce même fichier carte_jour_X.gpkg qui sert de référence pour la
     prochaine mise à jour (étape 0 du prochain lancement) : conservez-en
     une copie si vous voulez rejouer plusieurs fois la même fusion.
  5. Exporte un GeoJSON valide (carte_jour_X.geojson) pour chaque jour.
  6. Génère le fichier JavaScript correspondant (carte_jour_X.js), au
     format `var carte_jour_X = { ...GeoJSON... };`, directement
     compatible avec les balises <script src="carte_jour_X.js"> de
     carte_vigilance_final.html.

Utilisation
-----------
    python3 build_vigilance_maps.py --input-dir /chemin/vers/gpkg \
                                     --output-dir /chemin/vers/sortie

  --input-dir       Dossier contenant les NOUVEAUX fichiers fusionnés
                     carte_1.gpkg ... carte_5.gpkg (étape préalable).
                     Si l'un d'eux est absent, le script se rabat
                     automatiquement sur carte_jour_X.gpkg du même
                     dossier (compatibilité avec l'ancien flux, sans
                     étape préalable).
  --reference-dir    Dossier contenant les anciens fichiers déjà
                     attribués carte_jour_1.gpkg ... carte_jour_5.gpkg,
                     utilisés uniquement pour RÉCUPÉRER les attributs
                     métier (Phenomene, Seuil_code, Couleur_ph, ...) à
                     appliquer aux nouveaux polygones. Par défaut :
                     identique à --input-dir. Optionnel : si absents,
                     le script utilise la référence globale puis
                     LAYER_DEFAULTS.
  --output-dir       Dossier de sortie (par défaut : dossier courant).

Par défaut, --input-dir, --reference-dir et --output-dir valent le
répertoire courant.

Dépendances : geopandas, fiona, shapely (pip install geopandas fiona shapely)
"""

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

import fiona
import geopandas as gpd

# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------

JOURS = [1, 2, 3, 4, 5]

# Champs finaux, dans cet ordre, pour coller au format déjà utilisé par
# les fichiers carte_jour_X.js consommés par le HTML.
CHAMPS_FINAUX = [
    "fid", "id", "Phenomene", "Seuil_code", "Seuil_val", "Unite",
    "Echeance", "Date_début", "Date_fin", "Couleur_ph", "Commentair",
    "layer", "path",
]

# Champs "métier" récupérés depuis la référence (carte_jour_X.gpkg) pour
# habiller un polygone neuf qui n'a que sa géométrie + layer/path.
CHAMPS_METIER = [
    "id", "Phenomene", "Seuil_code", "Seuil_val", "Unite",
    "Couleur_ph", "Commentair",
]

# Si un .gpkg contient plusieurs couches, ordre de préférence des noms
# de couches à utiliser (la plus complète / la plus récente d'abord).
PREFERENCE_COUCHES = {
    1: ["carte_jour_1", "carte_jour1", "carte_1"],
    2: ["carte_jour_2", "carte_2"],
    3: ["carte_jour_3", "carte_3"],
    4: ["carte_jour_4", "carte_4"],
    5: ["carte_jour_5", "carte_5"],
}

# Table de secours ULTIME : utilisée uniquement si un "layer" présent
# dans les nouveaux polygones ne peut être retrouvé ni dans la
# référence du jour correspondant, ni dans la référence globale
# (aucun carte_jour_X.gpkg disponible, ou nouveau type de phénomène).
# -> à compléter si de nouveaux phénomènes/seuils apparaissent un jour
#    (le script émet un avertissement clair dans ce cas, avec le nom du
#    "layer" à ajouter ici).
LAYER_DEFAULTS = {
    "FF>40km:h": dict(Phenomene="G: Vent", Seuil_code="G>40km/h", Seuil_val=40.0,
                       Unite="Km/h", Couleur_ph="#e4f520",
                       Commentair="Zone de vent fort: Attention à la navigation !"),
    "FF>60km:h": dict(Phenomene="G: Vent", Seuil_code="G>60km/h", Seuil_val=60.0,
                       Unite="Km/h", Couleur_ph="#c9932e",
                       Commentair="Zone de vent très fort: Attention à la navigation !"),
    "Hs>2m": dict(Phenomene="Hs: Houle", Seuil_code="Hs>2m", Seuil_val=2.0,
                  Unite="m", Couleur_ph="#d963d7",
                  Commentair="Zone de forte houle: Attention à la navigation !"),
    "Hs>3m": dict(Phenomene="Hs: Houle", Seuil_code="Hs>3m", Seuil_val=3.0,
                  Unite="m", Couleur_ph="#6d18b6",
                  Commentair="Zone de houle dangereuse: Attention !!"),
    "RR>50mm:24h": dict(Phenomene="RR: Pluie", Seuil_code="RR>50mm/24h", Seuil_val=50.0,
                         Unite="mm", Couleur_ph="#36e541",
                         Commentair="Zone de fortes pluies en 24h de cumul"),
    "RR>50mm:12h": dict(Phenomene="RR: Pluie", Seuil_code="RR>50mm/12h", Seuil_val=50.0,
                         Unite="mm", Couleur_ph="#8fd694",
                         Commentair="Zone de fortes pluies en 12h de cumul"),
    "RR>100mm:24h": dict(Phenomene="RR: Pluie", Seuil_code="RR>100mm/24h", Seuil_val=100.0,
                          Unite="mm", Couleur_ph="#0a5c0a",
                          Commentair="Zone de très fortes pluies en 24h de cumul"),
    "T>40°C": dict(Phenomene="T: Température", Seuil_code="T>40°C", Seuil_val=40.0,
                    Unite="°C", Couleur_ph="#e88d49",
                    Commentair="Zone de forte température !"),
    "T>45°C": dict(Phenomene="T: Température", Seuil_code="T>45°C", Seuil_val=45.0,
                    Unite="°C", Couleur_ph="#bd201e",
                    Commentair="Zone de température extrême !"),
    "T<10°C": dict(Phenomene="T: Température", Seuil_code="T<10°C", Seuil_val=10.0,
                    Unite="°C", Couleur_ph="#6ec6e0",
                    Commentair="Zone de température fraîche"),
    "T<8°C": dict(Phenomene="T: Température", Seuil_code="T<8°C", Seuil_val=8.0,
                   Unite="°C", Couleur_ph="#1a4fa0",
                   Commentair="Zone de température froide"),
}


def nom_fichier_nouveau(jour: int) -> str:
    return f"carte_{jour}.gpkg"


def nom_fichier_reference(jour: int) -> str:
    return f"carte_jour_{jour}.gpkg"


def choisir_couche(chemin_gpkg: Path, jour: int) -> str:
    """Choisit la couche à utiliser dans un .gpkg pouvant contenir plusieurs couches."""
    couches = fiona.listlayers(str(chemin_gpkg))
    preferences = PREFERENCE_COUCHES.get(jour, [])
    for nom in preferences:
        if nom in couches:
            return nom
    # Sinon, on prend la première couche disponible.
    return couches[0]


def phenomene_court(valeur) -> str:
    """'G: Vent' -> 'G' ; 'Hs: Houle' -> 'Hs' ; laisse tel quel si pas de ':'."""
    if not valeur:
        return valeur
    return str(valeur).split(":")[0].strip()


def calculer_dates_echeance(jour: int, date_base: date) -> tuple:
    """
    Calcule automatiquement la date de début et la date de fin de validité
    d'une échéance, à partir d'une date de base (= Jour-J, date d'émission
    du bulletin, correspondant à J1) :

        J1 : Date_début = date_base           Date_fin = date_base + 1 jour
        J2 : Date_début = date_base + 1 jour   Date_fin = date_base + 2 jours
        J3 : Date_début = date_base + 2 jours  Date_fin = date_base + 3 jours
        J4 : Date_début = date_base + 3 jours  Date_fin = date_base + 4 jours
        J5 : Date_début = date_base + 4 jours  Date_fin = date_base + 5 jours

    Renvoie (Date_début, Date_fin) au format 'YYYY-MM-DD'.
    """
    debut = date_base + timedelta(days=jour - 1)
    fin = date_base + timedelta(days=jour)
    return debut.isoformat(), fin.isoformat()


def charger_gdf(chemin_gpkg: Path, jour: int) -> gpd.GeoDataFrame:
    couche = choisir_couche(chemin_gpkg, jour)
    gdf = gpd.read_file(chemin_gpkg, layer=couche)
    if gdf.crs is None:
        gdf.set_crs(epsg=4326, inplace=True)
    else:
        gdf = gdf.to_crs(epsg=4326)
    return gdf


# ----------------------------------------------------------------------
# Étape 0 (PRÉALABLE) : récupération des nouveaux polygones fusionnés et
# appariement avec les attributs métier de la génération précédente.
# ----------------------------------------------------------------------

def extraire_attributs_metier(row: dict) -> dict:
    return {champ: row.get(champ) for champ in CHAMPS_METIER}


def construire_reference_historique(args, jours_disponibles) -> tuple:
    """
    Charge, pour chaque jour, le fichier de référence carte_jour_X.gpkg
    (déjà attribué lors d'une génération précédente) s'il existe, et en
    déduit :
      - ref_par_jour[jour][layer] -> liste de dicts d'attributs métier,
        dans l'ordre d'origine.
      - ref_globale[layer] -> liste de dicts d'attributs métier, regroupés
        sur l'ensemble des jours disponibles (utilisée en repli si le
        jour correspondant n'a pas d'entité pour ce "layer").
    """
    ref_par_jour = {}
    ref_globale = defaultdict(list)

    for jour in JOURS:
        chemin_ref = args.reference_dir / nom_fichier_reference(jour)
        ref_par_jour[jour] = defaultdict(list)
        if not chemin_ref.exists():
            print(f"[INFO] Pas de référence {chemin_ref.name} (jour {jour}) : "
                  f"les nouveaux polygones de ce jour utiliseront la référence "
                  f"globale ou les valeurs par défaut si besoin.")
            continue
        try:
            gdf_ref = charger_gdf(chemin_ref, jour)
        except Exception as exc:
            print(f"[ATTENTION] Impossible de lire {chemin_ref} : {exc}", file=sys.stderr)
            continue

        for _, row in gdf_ref.iterrows():
            d = row.to_dict()
            layer = d.get("layer")
            if not layer:
                continue
            attrs = extraire_attributs_metier(d)
            ref_par_jour[jour][layer].append(attrs)
            ref_globale[layer].append(attrs)

        print(f"[OK] Référence {chemin_ref.name} chargée : {len(gdf_ref)} entités "
              f"({len(ref_par_jour[jour])} 'layer' distincts).")

    return ref_par_jour, ref_globale


def attribuer_polygones_neufs(gdf_neuf: gpd.GeoDataFrame, jour: int,
                               ref_par_jour: dict, ref_globale: dict) -> gpd.GeoDataFrame:
    """
    Pour chaque polygone du fichier neuf (carte_X.gpkg), retrouve ses
    attributs métier :
       1. dans la référence du même jour (carte_jour_X.gpkg), par "layer",
          en tournant sur la liste si plusieurs polygones neufs partagent
          le même "layer" ;
       2. sinon dans la référence globale (tous jours confondus) ;
       3. sinon dans LAYER_DEFAULTS (table de secours statique) ;
       4. sinon attributs vides + avertissement (nouveau phénomène inconnu,
          à ajouter manuellement à LAYER_DEFAULTS).
    """
    compteur_par_layer = defaultdict(int)
    layers_non_resolus = set()
    lignes = []

    ref_jour = ref_par_jour.get(jour, {})

    for _, row in gdf_neuf.iterrows():
        d = row.to_dict()
        layer = d.get("layer")

        candidats = ref_jour.get(layer) or ref_globale.get(layer)
        if candidats:
            i = compteur_par_layer[layer]
            attrs = dict(candidats[i % len(candidats)])
            compteur_par_layer[layer] += 1
        elif layer in LAYER_DEFAULTS:
            attrs = dict(LAYER_DEFAULTS[layer])
            attrs.setdefault("id", None)
        else:
            attrs = {champ: None for champ in CHAMPS_METIER}
            if layer:
                layers_non_resolus.add(layer)

        ligne = dict(d)
        for champ in CHAMPS_METIER:
            ligne[champ] = attrs.get(champ)
        lignes.append(ligne)

    if layers_non_resolus:
        print(f"[ATTENTION] Jour {jour} : impossible de retrouver des attributs pour "
              f"les 'layer' suivants (aucune référence, aucune valeur par défaut) : "
              f"{sorted(layers_non_resolus)}. Ajoutez-les à LAYER_DEFAULTS si besoin.",
              file=sys.stderr)

    return gpd.GeoDataFrame(lignes, geometry="geometry", crs=gdf_neuf.crs)


def charger_polygones_du_jour(args, jour: int, ref_par_jour: dict, ref_globale: dict):
    """
    Charge les polygones du jour, en privilégiant le nouveau fichier
    fusionné carte_X.gpkg (étape préalable). Si ce fichier n'existe pas,
    se rabat sur l'ancien fichier carte_jour_X.gpkg (flux historique,
    sans étape préalable : les attributs sont alors déjà présents).
    """
    chemin_neuf = args.input_dir / nom_fichier_nouveau(jour)
    chemin_ref = args.reference_dir / nom_fichier_reference(jour)

    if chemin_neuf.exists():
        gdf_neuf = charger_gdf(chemin_neuf, jour)
        print(f"[OK] {chemin_neuf.name} chargé (nouveaux polygones) : "
              f"{len(gdf_neuf)} entités.")
        gdf_habille = attribuer_polygones_neufs(gdf_neuf, jour, ref_par_jour, ref_globale)
        return gdf_habille

    if chemin_ref.exists():
        print(f"[INFO] {chemin_neuf.name} introuvable : utilisation directe de "
              f"{chemin_ref.name} (flux sans étape préalable).")
        return charger_gdf(chemin_ref, jour)

    print(f"[ATTENTION] Ni {chemin_neuf.name} ni {chemin_ref.name} introuvables, "
          f"jour {jour} ignoré.", file=sys.stderr)
    return None


# ----------------------------------------------------------------------
# Étapes 1-2 : harmonisation des champs + palette de couleurs de référence
# ----------------------------------------------------------------------

def construire_palette_reference(gdfs_par_jour: dict) -> dict:
    """
    Construit un dictionnaire {(Phenomene_court, Seuil_val): Couleur_ph}
    en votant à la majorité sur les fichiers disponibles, pour garantir un
    code couleur unique et cohérent par phénomène/seuil, quel que soit le
    jour.
    """
    votes = defaultdict(Counter)
    for jour, gdf in gdfs_par_jour.items():
        for _, row in gdf.iterrows():
            phen = phenomene_court(row.get("Phenomene"))
            seuil = row.get("Seuil_val")
            couleur = row.get("Couleur_ph")
            if phen is not None and seuil is not None and couleur:
                votes[(phen, float(seuil))][couleur] += 1

    palette = {cle: compteur.most_common(1)[0][0] for cle, compteur in votes.items()}
    return palette


def harmoniser_gdf(gdf: gpd.GeoDataFrame, jour: int, palette: dict, date_base: date) -> gpd.GeoDataFrame:
    """Reconstruit un GeoDataFrame avec exactement les champs CHAMPS_FINAUX."""
    date_debut, date_fin = calculer_dates_echeance(jour, date_base)

    lignes = []
    for i, row in enumerate(gdf.itertuples(index=False), start=1):
        d = row._asdict()
        # Nom complet du phénomène tel que fourni / retrouvé dans la référence
        # (ex: "G: Vent", "Hs: Houle", "RR: Pluie", "T: Température").
        phen_complet = d.get("Phenomene")
        # Code court utilisé uniquement pour regrouper les couleurs par
        # phénomène/seuil (la palette de référence est indexée dessus).
        phen_court = phenomene_court(phen_complet)
        seuil_val = d.get("Seuil_val")
        cle_couleur = (phen_court, float(seuil_val)) if seuil_val is not None else None

        couleur = palette.get(cle_couleur, d.get("Couleur_ph"))

        lignes.append({
            "fid": i,
            "id": d.get("id"),
            "Phenomene": phen_complet,
            "Seuil_code": d.get("Seuil_code"),
            "Seuil_val": seuil_val,
            "Unite": d.get("Unite"),
            "Echeance": d.get("Echeance") or f"J{jour}",
            "Date_début": date_debut,
            "Date_fin": date_fin,
            "Couleur_ph": couleur,
            "Commentair": d.get("Commentair"),
            "layer": d.get("layer"),
            "path": d.get("path"),
            "geometry": d.get("geometry"),
        })

    gdf_final = gpd.GeoDataFrame(lignes, geometry="geometry", crs="EPSG:4326")
    gdf_final = gdf_final[CHAMPS_FINAUX + ["geometry"]]
    return gdf_final


def ecrire_gpkg(gdf: gpd.GeoDataFrame, chemin_sortie: Path, jour: int):
    couche = f"carte_jour_{jour}"
    # Si le fichier de sortie existe déjà (ex: on écrit dans le même dossier
    # que les .gpkg sources), on le supprime d'abord pour repartir d'un
    # GeoPackage propre. Sans cela, GDAL peut refuser de recréer une couche
    # de même nom (y compris avec une casse différente, ex: 'Carte_jour_2'
    # vs 'carte_jour_2') et lever une erreur "Layer already exists".
    if chemin_sortie.exists():
        chemin_sortie.unlink()
    gdf.to_file(chemin_sortie, layer=couche, driver="GPKG")


def geojson_dict_depuis_gdf(gdf: gpd.GeoDataFrame, jour: int) -> dict:
    """Construit un dict GeoJSON propre (FeatureCollection) à partir du GeoDataFrame."""
    brut = json.loads(gdf.to_json())
    fc = {
        "type": "FeatureCollection",
        "name": f"carte_jour_{jour}",
        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}},
        "features": brut["features"],
    }
    return fc


def ecrire_geojson(fc: dict, chemin_sortie: Path):
    with open(chemin_sortie, "w", encoding="utf-8") as f:
        json.dump(fc, f, ensure_ascii=False, indent=None, separators=(", ", ": "))


def ecrire_js(fc: dict, chemin_sortie: Path, jour: int):
    contenu_json = json.dumps(fc, ensure_ascii=False, separators=(", ", ": "))
    with open(chemin_sortie, "w", encoding="utf-8") as f:
        f.write(f"var carte_jour_{jour} =\n{contenu_json}\n;\n")


def main():
    parser = argparse.ArgumentParser(description="Pipeline .gpkg -> .geojson -> .js pour la carte de vigilance.")
    parser.add_argument("--input-dir", type=Path, default=Path("."),
                         help="Répertoire contenant les NOUVEAUX fichiers fusionnés "
                              "carte_1.gpkg ... carte_5.gpkg (étape préalable). "
                              "À défaut, carte_jour_1.gpkg ... carte_jour_5.gpkg "
                              "y sont utilisés directement (flux historique).")
    parser.add_argument("--reference-dir", type=Path, default=None,
                         help="Répertoire contenant les anciens fichiers déjà "
                              "attribués carte_jour_1.gpkg ... carte_jour_5.gpkg, "
                              "utilisés pour récupérer Phenomene/Seuil_code/"
                              "Couleur_ph/Commentair des nouveaux polygones. "
                              "Par défaut : identique à --input-dir.")
    parser.add_argument("--output-dir", type=Path, default=Path("."),
                         help="Répertoire de sortie pour les .gpkg / .geojson / .js régénérés")
    parser.add_argument("--date-base", type=str, default=None,
                         help="Date de base (Jour-J, correspondant à J1) au format YYYY-MM-DD. "
                              "Par défaut : date du jour d'exécution du script. "
                              "J2 = date_base+1j, J3 = +2j, J4 = +3j, J5 = +4j "
                              "(mise à jour automatique, aucune saisie manuelle nécessaire).")
    args = parser.parse_args()

    if args.reference_dir is None:
        args.reference_dir = args.input_dir

    if args.date_base:
        date_base = date.fromisoformat(args.date_base)
    else:
        date_base = date.today()
    print(f"Date de base (J1) utilisée pour Date_début/Date_fin : {date_base.isoformat()}")

    args.output_dir.mkdir(parents=True, exist_ok=True)

    # 0. Étape préalable : référence historique (attributs métier) puis
    #    récupération/habillage des nouveaux polygones fusionnés.
    ref_par_jour, ref_globale = construire_reference_historique(args, JOURS)

    gdfs = {}
    for jour in JOURS:
        gdf = charger_polygones_du_jour(args, jour, ref_par_jour, ref_globale)
        if gdf is not None:
            gdfs[jour] = gdf

    if not gdfs:
        print("Aucun fichier .gpkg trouvé, arrêt.", file=sys.stderr)
        sys.exit(1)

    # 1-2. Palette de référence Couleur_ph par (Phenomene, Seuil_val),
    #      calculée sur l'ensemble des polygones (neufs, désormais habillés).
    palette = construire_palette_reference(gdfs)
    print("\nPalette de couleurs de référence (Phenomene, Seuil_val) -> Couleur_ph :")
    for cle, couleur in sorted(palette.items(), key=lambda x: (x[0][0], x[0][1])):
        print(f"   {cle[0]:>3} > {cle[1]:<6} -> {couleur}")

    # 3-6. Harmonisation + écriture des sorties pour chaque jour
    for jour, gdf in gdfs.items():
        gdf_final = harmoniser_gdf(gdf, jour, palette, date_base)

        chemin_gpkg = args.output_dir / f"carte_jour_{jour}.gpkg"
        chemin_geojson = args.output_dir / f"carte_jour_{jour}.geojson"
        chemin_js = args.output_dir / f"carte_jour_{jour}.js"

        ecrire_gpkg(gdf_final, chemin_gpkg, jour)
        fc = geojson_dict_depuis_gdf(gdf_final, jour)
        ecrire_geojson(fc, chemin_geojson)
        ecrire_js(fc, chemin_js, jour)

        print(f"\n[JOUR {jour}] {len(gdf_final)} entités écrites -> "
              f"{chemin_gpkg.name}, {chemin_geojson.name}, {chemin_js.name}")

    print("\nTerminé. Copiez les fichiers carte_jour_1.js ... carte_jour_5.js "
          "à côté de carte_vigilance_final.html pour mettre la carte à jour.")


if __name__ == "__main__":
    main()
