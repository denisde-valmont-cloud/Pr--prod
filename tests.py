# -*- coding: utf-8 -*-
"""Suite de non-régression — MPA Portail Aidant (pré-prod).
Rejoue le parcours complet sur un serveur en mémoire, catalogue de test injecté
via le connecteur (le vrai catalogue vient d'aidance-pro ; aucun n'est embarqué).
Usage : python3 tests.py
"""
import json, socket, threading, time, urllib.request, urllib.error, sys

import core, catalogue, api as apimod
from http.server import ThreadingHTTPServer

# ── catalogue de test injecté par le connecteur (contrat d'interface) ──
CAT_TEST = {
    "rows": [
        {"pid": "AP-001", "pro": "Les Aînés d'abord", "pro_desc": "Service d'aide à domicile.",
         "nom": "Aide à domicile — actes du quotidien", "desc": "Auxiliaire de vie à domicile.",
         "familles": ["Aide humaine"], "metiers": ["Aide à domicile"],
         "mod": ["presentiel_aidant"], "nat": False, "deps": ["69", "01"],
         "unite": "Par heure", "prix": 28.0, "rank": 0},
        {"pid": "AP-002", "pro": "Cap Répit", "pro_desc": "Séjours de répit.",
         "nom": "Séjour de répit — forfait découverte", "desc": "Deux jours de répit.",
         "familles": ["Aide au parcours de vie"], "metiers": ["Accompagnant éducatif et social (AES)"],
         "mod": ["presentiel_pro"], "nat": True, "deps": [],
         "unite": "Prestation complète", "prix": 180.0, "rank": 1},
    ],
    "depts": [{"c": "69", "n": "Rhône"}, {"c": "01", "n": "Ain"}],
}

OK, KO = 0, 0
def check(label, cond, info=""):
    global OK, KO
    if cond: OK += 1; print(f"  ✓ {label}")
    else:    KO += 1; print(f"  ✗ {label}  {info}")

def main():
    catalogue.charger_depuis_dict(apimod.APP.store, CAT_TEST, "test (injecté)")
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), apimod.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start(); time.sleep(0.3)
    B = f"http://127.0.0.1:{port}"

    def req(m, p, b=None):
        d = json.dumps(b).encode() if b is not None else None
        r = urllib.request.Request(B+p, data=d, method=m,
                                   headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(r, timeout=6) as x:
                return x.status, json.loads(x.read())
        except urllib.error.HTTPError as e:
            try: return e.code, json.loads(e.read())
            except Exception: return e.code, {}

    # 0 · diag + catalogue
    c, d = req("GET", "/api/diag")
    check("diag", c == 200 and d["catalogue_prestations"] == 2, d)
    c, cat = req("GET", "/api/catalogue")
    check("catalogue servi", c == 200 and len(cat["prestations"]) == 2 and len(cat["pros"]) == 2)
    horaire = next(p for p in cat["prestations"] if p["unite"] == "Par heure")
    forfait = next(p for p in cat["prestations"] if p["unite"] == "Prestation complète")

    # 1 · pré-inscription (back-office) : PFD principal obligatoire, NDD, montant
    c, e = req("POST", "/api/backoffice/preinscriptions",
               {"prenom": "Paul", "nom": "Martin", "email": "p@x.fr", "ndd": 5,
                "montant": 887, "pfd": {"prenom": "Jeanne", "lien": "Mère", "ndd": 5}})
    check("NDD 5 refusé (RG-13)", c == 400, e)
    c, e = req("POST", "/api/backoffice/preinscriptions",
               {"prenom": "Paul", "nom": "Martin", "email": "p@x.fr", "ndd": 2, "montant": 887})
    check("pré-inscription sans PFD refusée (RG-41)", c == 400, e)
    c, a = req("POST", "/api/backoffice/preinscriptions",
               {"prenom": "Paul", "nom": "Martin", "email": "p@x.fr", "ndd": 2, "montant": 887,
                "pfd": {"prenom": "Jeanne", "lien": "Mère", "naissance": "1945", "ndd": 2}})
    check("pré-inscription + PFD principal", c == 201 and a["aides"][0]["principal"] is True
          and a["pmv"]["disponible"] == "887.00", a)
    aid = a["id"]

    # 2 · double verrou (RG-42)
    check("verrou 1 (RIB)", a["pmv_pret"] is False and "RIB" in (a["pmv_blocage"] or ""))
    c, e = req("POST", "/api/devis", {"assure_id": aid, "prestation_id": horaire["id"]})
    check("devis bloqué sans RIB", c == 400 and "RIB" in e.get("error", ""))
    c, e = req("POST", f"/api/assures/{aid}/profil", {"iban": "FR00INVALIDE"})
    check("IBAN invalide rejeté", c == 400)
    c, a = req("POST", f"/api/assures/{aid}/profil",
               {"iban": "FR7630006000011234567890189", "telephone": "0600000000",
                "profil": {"marital": "Marié(e)", "besoins": ["Aide à domicile"]}})
    check("verrou 2 (profil PFD)", c == 200 and a["pmv_pret"] is False
          and "proche" in (a["pmv_blocage"] or ""))
    pfd_id = a["aides"][0]["id"]
    c, a = req("POST", f"/api/assures/{aid}/aides/{pfd_id}/profil",
               {"profil": {"lieu": "Domicile, seul(e)", "mobilite": "Marche avec aide",
                           "besoins": ["Répit de l'aidant"]}})
    check("PMV débloqué après les 2 verrous", c == 200 and a["pmv_pret"] is True)

    # 3 · devis tarif variable : demande SANS chiffrage automatique
    c, dv = req("POST", "/api/devis", {"assure_id": aid, "prestation_id": horaire["id"],
                                       "message": "Dossier MDPH, le mercredi."})
    check("demande créée à l'état « demande » (pas d'automatisation pro)",
          c == 201 and dv["statut"] == "demande" and dv["total"] is None, dv)
    did = dv["id"]
    c, e = req("POST", f"/api/devis/{did}/accepter", {})
    check("acceptation impossible avant chiffrage du pro", c == 400)

    # 4 · API partenaire aidance-pro : chiffrage puis réalisation
    c, ds = req("GET", "/api/partenaire/devis?statut=demande")
    check("file partenaire : 1 demande", c == 200 and len(ds) == 1)
    c, dv = req("POST", f"/api/partenaire/devis/{did}/etablir",
                {"quantite": 4, "message_pro": "4 h sur 2 mercredis."})
    check("devis établi par le pro (4 × 28 = 112)", c == 200 and dv["total"] == "112.00")
    c, dv = req("POST", f"/api/devis/{did}/accepter", {})
    check("acceptation → engagé", c == 200 and dv["statut"] == "engagé")
    c, a = req("GET", f"/api/assures/{aid}")
    check("PMV : engagement (RG-43)", a["pmv"]["disponible"] == "775.00"
          and a["pmv"]["engage"] == "112.00")
    c, e = req("POST", f"/api/devis/{did}/service-fait", {})
    check("service fait refusé avant « réalisée »", c == 400)
    c, dv = req("POST", f"/api/partenaire/devis/{did}/realisee", {})
    check("pro : réalisée", c == 200 and dv["statut"] == "réalisée")
    c, dv = req("POST", f"/api/devis/{did}/service-fait", {})
    check("aidant : service fait → payé", c == 200 and dv["statut"] == "payé")
    c, a = req("GET", f"/api/assures/{aid}")
    check("PMV : payé", a["pmv"]["paye"] == "112.00" and a["pmv"]["engage"] == "0.00")

    # 5 · forfait : devisé direct, refus sans engagement
    c, dv = req("POST", "/api/devis", {"assure_id": aid, "prestation_id": forfait["id"]})
    check("forfait → devisé direct", c == 201 and dv["statut"] == "devisé"
          and dv["total"] == "180.00")
    c, dv = req("POST", f"/api/devis/{dv['id']}/refuser", {})
    check("refus du forfait", c == 200 and dv["statut"] == "refusé")

    # 6 · remboursements : soumission aidant, validation back-office (partielle)
    c, r = req("POST", "/api/remboursements",
               {"assure_id": aid, "libelle": "Taxi", "motif": "Transport", "montant": "900"})
    check("remboursement soumis", c == 201 and r["statut"] == "soumise")
    c, r = req("POST", f"/api/backoffice/remboursements/{r['id']}/valider", {})
    check("validation partielle à hauteur du disponible (RG-44)",
          c == 200 and r["partiel"] is True and r["montant_valide"] == "775.00")

    # 7 · contact conseiller (serveur)
    c, a = req("POST", f"/api/assures/{aid}/contacts",
               {"type": "rappel", "telephone": "0600000000", "resume": "Dès que possible"})
    check("demande conseiller enregistrée", c == 201 and len(a["contacts"]) == 1)

    # 8 · erreurs
    check("assuré inconnu → 404", req("GET", "/api/assures/AS-XXXX")[0] == 404)
    check("route inconnue → 404", req("GET", "/api/nimportequoi")[0] == 404)

    httpd.shutdown()
    print(f"\n{OK} OK · {KO} KO")
    sys.exit(1 if KO else 0)


if __name__ == "__main__":
    main()
