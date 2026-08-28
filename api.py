# -*- coding: utf-8 -*-
"""
MPA — Portail Aidant · API HTTP (pré-production)
=================================================

Trois surfaces, volontairement distinctes :

  PORTAIL AIDANT (appelée par aidant.html)
    GET  /                                    → sert aidant.html
    GET  /api/assures/<id>                    → dossier complet de l'aidant
    GET  /api/catalogue                       → catalogue (connecteur aidance-pro)
    POST /api/assures/<id>/profil             → profil aidant + RIB   (verrou 1)
    POST /api/assures/<id>/aides              → ajout PFD complémentaire
    POST /api/assures/<id>/aides/<pid>/profil → profil du PFD         (verrou 2)
    POST /api/assures/<id>/analyse            → validation de la synthèse
    POST /api/assures/<id>/contacts           → demande conseiller (rappel/msg/rdv)
    POST /api/devis                           → demande de devis
    POST /api/devis/<id>/accepter|refuser|service-fait
    POST /api/remboursements                  → dépense avancée à rembourser
    POST /api/analyse/generer  ·  POST /api/anticiper2   → proxys LLM
    GET  /api/diag                            → diagnostic (LLM, catalogue)

  API PARTENAIRE aidance-pro (les échanges pros ne sont PAS simulés ici :
  c'est aidance-pro qui appellera ces routes)
    GET  /api/partenaire/devis?statut=demande → demandes en attente de chiffrage
    POST /api/partenaire/devis/<id>/etablir   → {quantite, message_pro}
    POST /api/partenaire/devis/<id>/realisee  → prestation réalisée

  API BACK-OFFICE (appelée par la V0 back-office Assur-aidant)
    POST /api/backoffice/preinscriptions      → {prenom, nom, email, ndd,
                                                 montant, pfd:{prenom,lien,naissance,ndd}}
    POST /api/backoffice/assures/<id>/crediter        → {montant, ref?}
    POST /api/backoffice/remboursements/<id>/valider|refuser|piece
    POST /api/backoffice/devis/<id>/liberer   → libération J+7 (tâche planifiée)
    POST /api/catalogue/rafraichir            → recharge le connecteur

Hors périmètre (V0 back-office) : bordereau SEPA pain.001, contrats & barèmes.
À la charge de la production : persistance, authentification, journalisation.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
import json
import os
import sys
import threading

import core
import catalogue

HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "aidant.html")


class App:
    def __init__(self):
        self.lock = threading.Lock()
        self.store = core.Store()
        self.assures = core.MoteurAssure(self.store)
        self.devis = core.MoteurDevis(self.store)
        self.remb = core.MoteurRemboursement(self.store)
        catalogue.charger(self.store)


APP = App()


# ─────────────────────────────────────────────────────────────────────────────
#  Sérialisation
# ─────────────────────────────────────────────────────────────────────────────
def jsonable(o):
    if hasattr(o, "__dataclass_fields__"):
        return {k: jsonable(getattr(o, k)) for k in o.__dataclass_fields__}
    if isinstance(o, dict):
        return {k: jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [jsonable(x) for x in o]
    if hasattr(o, "quantize"):
        return str(o)
    return o


def pmv_view(env, detail=False):
    d = {"disponible": str(env.disponible), "engage": str(env.engage),
         "paye": str(env.paye), "solde": str(env.solde)}
    if detail:
        d["mouvements"] = [jsonable(m) for m in env.mouvements]
    return d


def assure_view(a, store, detail=False):
    env = store.enveloppes.get(a.id)
    blocage = core.pmv_blocage(store, a.id)
    return {
        "id": a.id, "prenom": a.prenom, "nom": a.nom,
        "nom_complet": a.nom_complet, "email": a.email,
        "ndd": a.ndd,
        "ndd_libelle": core.NDD[a.ndd].libelle if a.ndd in core.NDD else None,
        "aides": [jsonable(x) for x in a.aides],
        "telephone": a.telephone,
        "profil": a.profil or {},
        "analyse": a.analyse,
        "contacts": list(a.contacts),
        "profil_complet": bool(env and env.iban),
        "pmv_pret": blocage is None,
        "pmv_blocage": blocage,
        "rib": (env.iban if env else ""),
        "pmv": pmv_view(env, detail) if env else None,
    }


# ─────────────────────────────────────────────────────────────────────────────
#  Handler HTTP
# ─────────────────────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    server_version = "MPA-Aidant/1.0"

    def log_message(self, *a):            # journalisation prod : à brancher
        pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._cors()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _err(self, msg, status=400):
        self._json({"error": str(msg)}, status)

    def _serve_html(self):
        try:
            with open(HTML_PATH, "rb") as f:
                body = f.read()
        except FileNotFoundError:
            return self._json({"service": "MPA Portail Aidant", "ok": True,
                               "note": "aidant.html absent — placez-le à côté de api.py"})
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self._cors()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _parts(self):
        u = urlparse(self.path)
        return [p for p in u.path.split("/") if p], {k: v[0] for k, v in parse_qs(u.query).items()}

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n))
        except Exception:
            return {}

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    # ── GET ──────────────────────────────────────────────────────────────
    def do_GET(self):
        parts, q = self._parts()
        s = APP.store
        try:
            if not parts or parts == ["aidant.html"]:
                return self._serve_html()

            if parts == ["api", "diag"]:
                key = os.environ.get("ANTHROPIC_API_KEY")
                return self._json({
                    "app": "MPA — Portail Aidant (pré-prod)",
                    "anthropic_api_key_present": bool(key),
                    "assistant_mode": "llm" if key else "repli (heuristique client)",
                    "catalogue_source": s.catalogue_source,
                    "catalogue_prestations": len(s.prestations),
                })

            if parts == ["api", "catalogue"]:
                return self._json({
                    "source": s.catalogue_source,
                    "pros": [jsonable(p) for p in s.pros.values()],
                    "prestations": [jsonable(p) for p in s.prestations.values()],
                    "depts": s.depts,
                })

            if len(parts) == 3 and parts[:2] == ["api", "assures"]:
                a = s.assures.get(parts[2])
                if not a:
                    return self._err("Assuré introuvable", 404)
                out = assure_view(a, s, detail=True)
                out["devis"] = [jsonable(d) for d in s.devis.values()
                                if d.assure_id == a.id]
                out["remboursements"] = [jsonable(r) for r in s.remboursements.values()
                                         if r.assure_id == a.id]
                return self._json(out)

            if parts == ["api", "partenaire", "devis"]:
                st = q.get("statut")
                ds = [d for d in s.devis.values() if not st or d.statut == st]
                return self._json([jsonable(d) for d in ds])

            return self._err("Route inconnue", 404)
        except Exception as e:
            return self._err(e, 500)

    # ── POST ─────────────────────────────────────────────────────────────
    def do_POST(self):
        parts, _ = self._parts()
        body = self._body()
        with APP.lock:
            s = APP.store
            try:
                # ── Portail aidant ──
                if (len(parts) == 4 and parts[:2] == ["api", "assures"]
                        and parts[3] == "profil"):
                    a = APP.assures.completer_profil(parts[2], body.get("iban", ""),
                                                     body.get("telephone", ""),
                                                     body.get("profil"))
                    return self._json(assure_view(a, s, detail=True))

                if (len(parts) == 4 and parts[:2] == ["api", "assures"]
                        and parts[3] == "aides"):
                    a = APP.assures.ajouter_aide(parts[2], body.get("prenom", ""),
                                                 body.get("lien", ""),
                                                 body.get("ndd", 0),
                                                 body.get("naissance", ""))
                    return self._json(assure_view(a, s, detail=True), 201)

                if (len(parts) == 6 and parts[:2] == ["api", "assures"]
                        and parts[3] == "aides" and parts[5] == "profil"):
                    a = APP.assures.completer_profil_pfd(parts[2], parts[4],
                                                         body.get("profil"))
                    return self._json(assure_view(a, s, detail=True))

                if (len(parts) == 4 and parts[:2] == ["api", "assures"]
                        and parts[3] == "analyse"):
                    a = APP.assures.completer_analyse(parts[2], body.get("texte", ""))
                    return self._json(assure_view(a, s, detail=True))

                if (len(parts) == 4 and parts[:2] == ["api", "assures"]
                        and parts[3] == "contacts"):
                    a = APP.assures.enregistrer_contact(parts[2], body.get("type", ""),
                                                        body.get("resume", ""),
                                                        body.get("telephone", ""))
                    return self._json(assure_view(a, s, detail=True), 201)

                if parts == ["api", "devis"]:
                    d = APP.devis.demander(body["assure_id"], body["prestation_id"],
                                           body.get("message", ""))
                    return self._json(jsonable(d), 201)

                if len(parts) == 4 and parts[:2] == ["api", "devis"]:
                    did, action = parts[2], parts[3]
                    if did not in s.devis:
                        return self._err("Devis introuvable", 404)
                    if action == "accepter":
                        d = APP.devis.accepter(did)
                    elif action == "refuser":
                        d = APP.devis.refuser(did)
                    elif action == "service-fait":
                        d = APP.devis.confirmer_service_fait(did)
                    else:
                        return self._err("Action devis inconnue", 404)
                    return self._json(jsonable(d))

                if parts == ["api", "remboursements"]:
                    r = APP.remb.soumettre(body["assure_id"], body["libelle"],
                                           body["motif"], body["montant"],
                                           body.get("justificatif", ""))
                    return self._json(jsonable(r), 201)

                # ── Proxys assistant (LLM) ──
                if parts == ["api", "analyse", "generer"]:
                    try:
                        txt = core.anticiper_prompt(body.get("prompt", ""),
                                                    system=core.SYSTEME_ANALYSE,
                                                    max_tokens=1400)
                        return self._json({"texte": txt})
                    except Exception:
                        return self._json({"texte": None})

                if parts == ["api", "anticiper2"]:
                    try:
                        txt = core.anticiper_prompt(body.get("prompt", ""))
                        return self._json({"text": txt})
                    except Exception:
                        return self._json({"text": None})

                # ── API partenaire aidance-pro ──
                if (len(parts) == 5 and parts[:3] == ["api", "partenaire", "devis"]):
                    did, action = parts[3], parts[4]
                    if did not in s.devis:
                        return self._err("Devis introuvable", 404)
                    if action == "etablir":
                        d = APP.devis.etablir(did, body["quantite"],
                                              body.get("message_pro", ""))
                    elif action == "realisee":
                        d = APP.devis.declarer_realisee(did)
                    else:
                        return self._err("Action partenaire inconnue", 404)
                    return self._json(jsonable(d))

                # ── API back-office (V0 Assur-aidant) ──
                if parts == ["api", "backoffice", "preinscriptions"]:
                    a = APP.assures.preinscrire(body["prenom"], body["nom"],
                                                body["email"], body["ndd"],
                                                body["montant"], body.get("pfd"))
                    return self._json(assure_view(a, s, detail=True), 201)

                if (len(parts) == 5 and parts[:3] == ["api", "backoffice", "assures"]
                        and parts[4] == "crediter"):
                    env = s.enveloppes.get(parts[3])
                    if env is None:
                        return self._err("Assuré introuvable", 404)
                    env.alimenter(core.D(body["montant"]),
                                  body.get("ref", "Recrédit gestionnaire"))
                    return self._json(assure_view(s.assures[parts[3]], s, detail=True))

                if (len(parts) == 5 and parts[:3] == ["api", "backoffice", "remboursements"]):
                    rid, action = parts[3], parts[4]
                    if rid not in s.remboursements:
                        return self._err("Remboursement introuvable", 404)
                    if action == "valider":
                        r = APP.remb.valider(rid)
                    elif action == "refuser":
                        r = APP.remb.refuser(rid)
                    elif action == "piece":
                        r = APP.remb.demander_piece(rid)
                    else:
                        return self._err("Action remboursement inconnue", 404)
                    return self._json(jsonable(r))

                if (len(parts) == 5 and parts[:3] == ["api", "backoffice", "devis"]
                        and parts[4] == "liberer"):
                    d = APP.devis.liberer_auto(parts[3])
                    return self._json(jsonable(d))

                if parts == ["api", "catalogue", "rafraichir"]:
                    src = catalogue.charger(s)
                    return self._json({"source": src,
                                       "prestations": len(s.prestations)})

                return self._err("Route inconnue", 404)
            except KeyError as e:
                return self._err(f"Champ ou ressource manquant : {e}", 400)
            except ValueError as e:
                return self._err(e, 400)
            except Exception as e:
                return self._err(e, 500)


def serve(port=None):
    port = port if port is not None else int(os.environ.get("PORT", 8765))
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"  MPA — Portail Aidant (pré-prod) sur le port {port}")
    print(f"  Portail  →  http://127.0.0.1:{port}/?a=<id_assure>")
    print(f"  Diag     →  http://127.0.0.1:{port}/api/diag")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()


if __name__ == "__main__":
    serve(int(sys.argv[1]) if len(sys.argv) > 1 else None)
