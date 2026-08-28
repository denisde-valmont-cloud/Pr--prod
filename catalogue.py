# -*- coding: utf-8 -*-
"""
MPA — Portail Aidant · connecteur catalogue aidance-pro
========================================================

AUCUN catalogue n'est embarqué dans ce code (choix pré-prod assumé) : le
référentiel des professionnels et de leurs prestations appartient à
www.aidance-pro.com, qui l'exposera via un endpoint JSON en lecture seule.

CONTRAT D'INTERFACE ATTENDU (à implémenter côté aidance-pro / Pilot Systems)
----------------------------------------------------------------------------
GET  {AIDANCE_PRO_CATALOGUE_URL}
En-tête optionnel : Authorization: Bearer {AIDANCE_PRO_CATALOGUE_TOKEN}

Réponse : {
  "rows": [{
      "pid":      "identifiant catalogue de la prestation (stable)",
      "pro":      "raison sociale du professionnel",
      "pro_desc": "description de l'entreprise (fiche au survol)",
      "nom":      "intitulé de la prestation",
      "desc":     "description COMPLÈTE (non tronquée)",
      "familles": ["…"],            # facette 1 — familles de besoins
      "metiers":  ["…"],            # facette 2 — métiers
      "mod":      ["distanciel"|"presentiel_aidant"|"presentiel_pro"],
      "nat":      true|false,        # couverture nationale…
      "deps":     ["69","01", …],    # …sinon départements couverts
      "unite":    "Par heure"|"Par jour"|"Prestation complète",
      "prix":     123.45 | null,
      "rank":     0|1|2              # 0 premium · 1 labellisé · 2 autre
  }, …],
  "depts": [{"c":"01","n":"Ain"}, …]
}

Comportement : chargé au démarrage si la variable d'environnement est posée,
re-chargeable à chaud via POST /api/catalogue/rafraichir. Sans URL ou en cas
d'échec, le portail fonctionne avec un catalogue vide et l'affiche clairement
(état « non connecté ») — jamais de données fictives en pré-prod.
"""

import json
import os
import urllib.request

import core


def _slug(nom: str) -> str:
    return "PRO-" + "".join(c if c.isalnum() else "-" for c in (nom or "").upper())[:40]


def charger_depuis_dict(store: "core.Store", data: dict, source: str) -> int:
    """Alimente le Store à partir du JSON du contrat d'interface. Idempotent :
    remplace intégralement le catalogue courant."""
    store.pros.clear()
    store.prestations.clear()
    rows = data.get("rows") or []
    for i, r in enumerate(rows):
        pro_id = _slug(r.get("pro", ""))
        if pro_id not in store.pros:
            store.pros[pro_id] = core.Pro(pro_id, r.get("pro", ""), r.get("pro_desc", ""))
        prix = r.get("prix")
        p = core.Prestation(
            id=f"PR-{i+1}", pro_id=pro_id, nom=r.get("nom", ""),
            unite=r.get("unite", "Par heure"),
            prix=(None if prix is None else core.D(prix)),
            pid=str(r.get("pid", "")),
            familles=list(r.get("familles") or []),
            metiers=list(r.get("metiers") or []),
            mod=list(r.get("mod") or []),
            nat=bool(r.get("nat", True)),
            deps=[str(d) for d in (r.get("deps") or [])],
            rank=int(r.get("rank", 2)),
            desc=str(r.get("desc", "")),
        )
        store.prestations[p.id] = p
    store.depts = list(data.get("depts") or [])
    store.catalogue_source = source
    return len(rows)


def charger(store: "core.Store") -> str:
    """Charge le catalogue depuis AIDANCE_PRO_CATALOGUE_URL. Renvoie la source
    effective (« aidance-pro (live) » ou « non connecté »)."""
    url = os.environ.get("AIDANCE_PRO_CATALOGUE_URL", "").strip()
    if not url:
        store.catalogue_source = "non connecté"
        return store.catalogue_source
    try:
        req = urllib.request.Request(url)
        token = os.environ.get("AIDANCE_PRO_CATALOGUE_TOKEN", "").strip()
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        n = charger_depuis_dict(store, data, "aidance-pro (live)")
        print(f"  Catalogue aidance-pro chargé : {n} prestations.")
    except Exception as e:
        store.catalogue_source = "non connecté"
        print(f"  Catalogue aidance-pro indisponible ({e}) — portail en mode « non connecté ».")
    return store.catalogue_source
