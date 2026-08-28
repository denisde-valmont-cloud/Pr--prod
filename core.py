# -*- coding: utf-8 -*-
"""
MPA — Portail Aidant · cœur de domaine (pré-production)
========================================================

Périmètre : l'interface Aidant de la liquidation GSA, réécrite au propre à
partir des maquettes validées. Ce module est la **spécification exécutable**
du comportement attendu ; la logique est directement transposable en Django.

Chaque règle porte la référence RG-nn du dossier de spécifications
fonctionnelles (sections 2 & 3).

RÈGLES MÉTIER PORTÉES ICI
-------------------------
· RG-13  NDD 1–4 éligibles (5–6 exclus).
· RG-41  Pré-inscription : PFD principal OBLIGATOIRE (prénom, lien, âge, NDD).
· RG-42  Double verrou du PMV : (1) profil aidant avec RIB, puis
         (2) profil situationnel du PFD principal. Voir pmv_blocage().
· RG-43  Invariant : disponible = solde − engagé (Enveloppe).
· RG-45  Tiers-payant : demande → devisé (forfait : direct) → accepté/engagé
         → réalisée (pro) → service fait (aidant) OU libération J+7 → payé.
         Remboursement : soumis → validé (partiel possible) / refusé / pièce.
· RG-46  Rejet bancaire → recrédit (Enveloppe.rejeter) — conservé pour la prod.

CE QUE CE MODULE NE PORTE PAS (volontairement)
----------------------------------------------
· Contrats & barèmes : la V0 back-office calcule le crédit ; la pré-inscription
  reçoit ici un MONTANT déjà arbitré (découplage back-office ↔ portail).
· Bordereau SEPA pain.001 : émis par la V0 back-office.
· Persistance & authentification : à la charge de l'implémentation de
  production (l'état est en mémoire ; voir README).

stdlib uniquement · arithmétique Decimal.
"""

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, List, Dict
import datetime
import itertools
import json
import os
import urllib.request


# ─────────────────────────────────────────────────────────────────────────────
#  Utilitaires
# ─────────────────────────────────────────────────────────────────────────────
def D(x) -> Decimal:
    return Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def eur(d: Optional[Decimal]) -> str:
    return "—" if d is None else f"{d} €"


def iban_valide(iban: str) -> bool:
    """Contrôle de structure + clé modulo 97 (ISO 13616)."""
    iban = (iban or "").replace(" ", "").upper()
    if len(iban) < 15 or len(iban) > 34 or not iban[:2].isalpha():
        return False
    r = iban[4:] + iban[:4]
    try:
        n = int("".join(str(int(c, 36)) for c in r))
    except ValueError:
        return False
    return n % 97 == 1


_seq = itertools.count(1)


def _uid(prefix: str) -> str:
    return f"{prefix}-{next(_seq):04d}"


# ─────────────────────────────────────────────────────────────────────────────
#  Référentiel NDD (RG-13)
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Ndd:
    niveau: int
    libelle: str
    eligible: bool


NDD: Dict[int, Ndd] = {
    1: Ndd(1, "Dépendance totale", True),
    2: Ndd(2, "Dépendance sévère", True),
    3: Ndd(3, "Dépendance importante", True),
    4: Ndd(4, "Dépendance moyenne", True),
    5: Ndd(5, "Dépendance faible", False),
    6: Ndd(6, "Pas de dépendance pour les actes du quotidien", False),
}

LIENS = ["Enfant", "Frère", "Sœur", "Conjoint", "Père", "Mère"]  # liste fermée (RG-02)


# ─────────────────────────────────────────────────────────────────────────────
#  Entités
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Aide:
    """Proche Familial Dépendant (PFD)."""
    prenom: str
    lien: str
    ndd: int = 0
    principal: bool = False          # PFD de référence : ouvre l'indemnisation
    id: str = field(default_factory=lambda: _uid("PFD"))
    naissance: str = ""              # année / âge — saisi à la pré-inscription
    profil: dict = field(default_factory=dict)  # profil situationnel (aidant)


@dataclass
class Mouvement:
    type: str                        # alimentation|engagement|paiement|rejet
    montant: Decimal
    ref: str
    date: str


@dataclass
class Enveloppe:
    """PMV canonique (RG-43). Solde = disponible + engagé ; le payé est sorti.
    alimentation → (disponible) → engagement → (engagé) → paiement → (payé).
    Un rejet bancaire recrédite le disponible (RG-46)."""
    assure_id: str
    iban: str = ""
    nom_legal: str = ""
    disponible: Decimal = field(default_factory=lambda: D(0))
    engage: Decimal = field(default_factory=lambda: D(0))
    paye: Decimal = field(default_factory=lambda: D(0))
    mouvements: List[Mouvement] = field(default_factory=list)

    def _log(self, t, m, ref):
        self.mouvements.append(Mouvement(t, m, ref, datetime.date.today().isoformat()))

    def alimenter(self, montant: Decimal, ref: str):
        if montant <= 0:
            raise ValueError("Alimentation : montant positif requis")
        self.disponible += montant
        self._log("alimentation", montant, ref)

    def engager(self, montant: Decimal, ref: str):
        if montant <= 0:
            raise ValueError("Engagement : montant positif requis")
        if montant > self.disponible:
            raise ValueError(f"Engagement {eur(montant)} > disponible {eur(self.disponible)}")
        self.disponible -= montant
        self.engage += montant
        self._log("engagement", montant, ref)

    def payer(self, montant: Decimal, ref: str):
        if montant > self.engage:
            raise ValueError(f"Paiement {eur(montant)} > engagé {eur(self.engage)}")
        self.engage -= montant
        self.paye += montant
        self._log("paiement", montant, ref)

    def rejeter(self, montant: Decimal, ref: str):
        """Rejet bancaire : recrédit du disponible (RG-46)."""
        self.paye -= montant
        self.disponible += montant
        self._log("rejet", montant, ref)

    @property
    def solde(self) -> Decimal:
        return self.disponible + self.engage


@dataclass
class Assure:
    id: str
    prenom: str
    nom: str
    email: str
    ndd: int                          # NDD du PFD de référence (retenu au crédit)
    aides: List[Aide] = field(default_factory=list)
    telephone: str = ""
    profil: dict = field(default_factory=dict)   # profil situationnel aidant
    analyse: str = ""                            # synthèse validée par l'aidant
    contacts: List[dict] = field(default_factory=list)  # demandes conseiller

    @property
    def nom_complet(self):
        return f"{self.prenom} {self.nom}"


@dataclass
class Pro:
    id: str
    nom: str
    desc: str = ""


@dataclass
class Prestation:
    id: str
    pro_id: str
    nom: str
    unite: str                        # "Par heure" | "Par jour" | "Prestation complète"
    prix: Optional[Decimal]
    pid: str = ""                     # identifiant catalogue aidance-pro
    familles: List[str] = field(default_factory=list)
    metiers: List[str] = field(default_factory=list)
    mod: List[str] = field(default_factory=list)   # distanciel|presentiel_aidant|presentiel_pro
    nat: bool = True                  # couverture nationale (sinon liste deps)
    deps: List[str] = field(default_factory=list)
    rank: int = 2                     # 0 premium · 1 labellisé · 2 autre
    desc: str = ""

    @property
    def forfait(self):
        return self.unite == "Prestation complète"


@dataclass
class Devis:
    id: str
    assure_id: str
    prestation_id: str
    pro_id: str
    statut: str                       # demande|devisé|engagé|réalisée|payé|refusé
    quantite: Optional[Decimal] = None
    total: Optional[Decimal] = None
    message: str = ""                 # mot de l'aidant à la demande
    message_pro: str = ""             # mot du pro avec son devis
    bordereau: bool = False           # inclus dans un bordereau (V0 back-office)


@dataclass
class Remboursement:
    id: str
    assure_id: str
    libelle: str
    motif: str
    montant: Decimal
    justificatif: str
    statut: str                       # soumise|à_payer|refusée|piece_demandee|payé
    montant_valide: Optional[Decimal] = None
    partiel: bool = False


# ─────────────────────────────────────────────────────────────────────────────
#  Store — état partagé (en mémoire ; persistance = implémentation de prod)
# ─────────────────────────────────────────────────────────────────────────────
class Store:
    def __init__(self):
        self.assures: Dict[str, Assure] = {}
        self.enveloppes: Dict[str, Enveloppe] = {}   # clé = assure_id
        self.pros: Dict[str, Pro] = {}
        self.prestations: Dict[str, Prestation] = {}
        self.devis: Dict[str, Devis] = {}
        self.remboursements: Dict[str, Remboursement] = {}
        self.catalogue_source: str = "non connecté"
        self.depts: List[dict] = []

    def pmv(self, assure_id: str) -> Enveloppe:
        return self.enveloppes[assure_id]


# ─────────────────────────────────────────────────────────────────────────────
#  Verrou du PMV (RG-42)
# ─────────────────────────────────────────────────────────────────────────────
def pmv_blocage(store: Store, assure_id: str) -> Optional[str]:
    """Raison de blocage du PMV, ou None s'il est utilisable.
    Double verrou : (1) profil aidant complété (RIB), puis (2) profil
    situationnel du PFD principal renseigné par l'aidant."""
    a = store.assures.get(assure_id)
    env = store.enveloppes.get(assure_id)
    if not (env and env.iban):
        return "Complétez votre profil (RIB) pour accéder à votre PMV."
    princ = next((x for x in (a.aides if a else []) if x.principal), None)
    if princ is None or not princ.profil:
        return "Complétez le profil de votre proche (PFD) pour accéder à votre PMV."
    return None


# ─────────────────────────────────────────────────────────────────────────────
#  Moteur 1 — Dossier de l'assuré (pré-inscription & profils)
# ─────────────────────────────────────────────────────────────────────────────
class MoteurAssure:
    """Pré-inscription (appelée par la V0 back-office) et complétion des
    profils par l'aidant. Le crédit du PMV est reçu déjà calculé : le barème
    contractuel vit dans le back-office (découplage, cf. en-tête)."""

    def __init__(self, store: Store):
        self.s = store

    def preinscrire(self, prenom, nom, email, ndd, montant,
                    pfd: dict) -> Assure:
        """RG-41 : le PFD principal est obligatoire dès la pré-inscription.
        RG-13 : NDD 1–4 uniquement. Le montant est le crédit GSA arbitré en
        back-office (barème du contrat, éventuel ajustement gestionnaire)."""
        try:
            nd = int(ndd)
        except (TypeError, ValueError):
            raise ValueError("NDD invalide.")
        info = NDD.get(nd)
        if info is None or not info.eligible:
            raise ValueError(f"NDD {nd} non éligible à la GSA (couvre 1 à 4).")
        m = D(montant)
        if m <= 0:
            raise ValueError("Le crédit GSA doit être strictement positif.")
        pfd = dict(pfd or {})
        if not (pfd.get("prenom") and pfd.get("lien")):
            raise ValueError("PFD principal obligatoire : prénom, lien, âge, NDD (RG-41).")
        if pfd.get("lien") not in LIENS:
            raise ValueError(f"Lien de parenté invalide (liste fermée : {', '.join(LIENS)}).")

        a = Assure(_uid("AS"), str(prenom).strip(), str(nom).strip(),
                   str(email).strip(), nd)
        a.aides.append(Aide(str(pfd["prenom"]).strip(), pfd["lien"],
                            int(pfd.get("ndd", nd) or nd), principal=True,
                            naissance=str(pfd.get("naissance", "")).strip()))
        self.s.assures[a.id] = a
        env = Enveloppe(a.id, "", a.nom_complet)
        env.alimenter(m, f"Crédit GSA NDD {nd}")
        self.s.enveloppes[a.id] = env
        return a

    def completer_profil(self, assure_id, iban, telephone="", profil=None) -> Assure:
        """Profil aidant : le RIB (validé) lève le premier verrou (RG-42) ;
        les autres informations alimentent l'analyse situationnelle."""
        a = self._assure(assure_id)
        iban = (iban or "").replace(" ", "").upper()
        if not iban_valide(iban):
            raise ValueError("IBAN invalide : vérifiez votre RIB.")
        env = self.s.enveloppes.get(assure_id)
        if env is None:
            raise ValueError("PMV introuvable pour cet assuré.")
        env.iban = iban
        if telephone:
            a.telephone = str(telephone)
        if profil:
            a.profil = dict(profil)
        return a

    def ajouter_aide(self, assure_id, prenom, lien, ndd=0, naissance="") -> Assure:
        """Ajout d'un PFD complémentaire par l'aidant (jamais principal :
        RG-21, le montant reste celui du PFD de référence)."""
        a = self._assure(assure_id)
        if lien not in LIENS:
            raise ValueError(f"Lien de parenté invalide (liste fermée : {', '.join(LIENS)}).")
        try:
            nd = int(ndd)
        except (TypeError, ValueError):
            nd = 0
        a.aides.append(Aide(str(prenom).strip(), lien, nd, principal=False,
                            naissance=str(naissance).strip()))
        return a

    def completer_profil_pfd(self, assure_id, pfd_id, profil) -> Assure:
        """Profil situationnel d'un PFD ; sur le principal, lève le second
        verrou (RG-42)."""
        a = self._assure(assure_id)
        for pfd in a.aides:
            if pfd.id == pfd_id:
                pfd.profil = dict(profil or {})
                return a
        raise KeyError(f"PFD introuvable : {pfd_id}")

    def completer_analyse(self, assure_id, texte) -> Assure:
        """L'aidant valide (après édition éventuelle) la synthèse
        situationnelle : seule la version validée fait foi (RG-61)."""
        a = self._assure(assure_id)
        a.analyse = (texte or "").strip()
        return a

    def enregistrer_contact(self, assure_id, type_, resume, telephone="") -> Assure:
        """Demande de contact conseiller (rappel, message, rendez-vous).
        Stockée côté serveur — la prod la relaiera au CRM."""
        a = self._assure(assure_id)
        if type_ not in ("rappel", "message", "rdv"):
            raise ValueError("Type de demande inconnu.")
        a.contacts.insert(0, {
            "id": _uid("CX"), "type": type_, "resume": str(resume or "").strip(),
            "telephone": str(telephone or "").strip(),
            "date": datetime.date.today().isoformat(), "statut": "transmise",
        })
        return a

    def _assure(self, assure_id) -> Assure:
        a = self.s.assures.get(assure_id)
        if a is None:
            raise KeyError(f"Assuré introuvable : {assure_id}")
        return a


# ─────────────────────────────────────────────────────────────────────────────
#  Moteur 2 — Transactionnel / devis (RG-45)
# ─────────────────────────────────────────────────────────────────────────────
class MoteurDevis:
    JOURS_AUTO = 7   # libération automatique J+7 après « réalisée » (RG-45)

    def __init__(self, store: Store):
        self.s = store

    # — côté aidant —
    def demander(self, assure_id, prestation_id, message="") -> Devis:
        if assure_id not in self.s.assures:
            raise KeyError(f"Assuré introuvable : {assure_id}")
        b = pmv_blocage(self.s, assure_id)
        if b:
            raise ValueError(b)
        p = self.s.prestations.get(prestation_id)
        if p is None:
            raise KeyError(f"Prestation introuvable : {prestation_id}")
        d = Devis(_uid("DV"), assure_id, prestation_id, p.pro_id,
                  statut="demande", message=str(message or "").strip())
        # un forfait à prix fixe n'a pas besoin de devis : engageable directement
        if p.forfait and p.prix is not None:
            d.quantite = D(1)
            d.total = p.prix
            d.statut = "devisé"
        self.s.devis[d.id] = d
        return d

    def accepter(self, devis_id) -> Devis:
        """Acceptation par l'aidant → engagement du PMV (garde-fou RG-43)."""
        d = self._devis(devis_id)
        if d.statut != "devisé":
            raise ValueError("Seul un devis à l'état « devisé » peut être accepté.")
        env = self.s.pmv(d.assure_id)
        env.engager(d.total, f"Devis {d.id}")
        d.statut = "engagé"
        return d

    def refuser(self, devis_id) -> Devis:
        d = self._devis(devis_id)
        if d.statut not in ("demande", "devisé"):
            raise ValueError("Ce devis ne peut plus être refusé.")
        d.statut = "refusé"
        return d

    def confirmer_service_fait(self, devis_id) -> Devis:
        """Service fait (aidant) : seconde validation → paiement (RG-45)."""
        d = self._devis(devis_id)
        if d.statut != "réalisée":
            raise ValueError("Le professionnel n'a pas encore déclaré la prestation réalisée.")
        return self._payer(d)

    # — côté pro : frontière d'intégration aidance-pro (API partenaire) —
    def etablir(self, devis_id, quantite, message_pro="") -> Devis:
        d = self._devis(devis_id)
        if d.statut != "demande":
            raise ValueError("Seule une demande en attente peut être chiffrée.")
        p = self.s.prestations[d.prestation_id]
        if p.prix is None:
            raise ValueError("Prestation sans tarif : chiffrage impossible.")
        d.quantite = D(quantite)
        if d.quantite <= 0:
            raise ValueError("Quantité positive requise.")
        d.total = (d.quantite * p.prix).quantize(Decimal("0.01"), ROUND_HALF_UP)
        if message_pro:
            d.message_pro = str(message_pro).strip()
        d.statut = "devisé"
        return d

    def declarer_realisee(self, devis_id) -> Devis:
        d = self._devis(devis_id)
        if d.statut != "engagé":
            raise ValueError("Seul un devis engagé peut être déclaré réalisé.")
        d.statut = "réalisée"
        return d

    # — exploitation (V0 back-office / tâche planifiée) —
    def liberer_auto(self, devis_id) -> Devis:
        """Libération J+7 sans confirmation de l'aidant (RG-45)."""
        d = self._devis(devis_id)
        if d.statut != "réalisée":
            raise ValueError("Libération impossible : prestation non déclarée réalisée.")
        return self._payer(d)

    def _payer(self, d: Devis) -> Devis:
        env = self.s.pmv(d.assure_id)
        env.payer(d.total, f"Devis {d.id}")
        d.statut = "payé"     # la ligne alimentera le bordereau (V0 back-office)
        return d

    def _devis(self, devis_id) -> Devis:
        d = self.s.devis.get(devis_id)
        if d is None:
            raise KeyError(f"Devis introuvable : {devis_id}")
        return d


# ─────────────────────────────────────────────────────────────────────────────
#  Moteur 3 — Remboursements (RG-45)
# ─────────────────────────────────────────────────────────────────────────────
class MoteurRemboursement:
    def __init__(self, store: Store):
        self.s = store

    # — côté aidant —
    def soumettre(self, assure_id, libelle, motif, montant, justificatif) -> Remboursement:
        if assure_id not in self.s.assures:
            raise KeyError(f"Assuré introuvable : {assure_id}")
        b = pmv_blocage(self.s, assure_id)
        if b:
            raise ValueError(b)
        m = D(montant)
        if m <= 0:
            raise ValueError("Montant positif requis.")
        r = Remboursement(_uid("RB"), assure_id, str(libelle).strip(),
                          str(motif).strip(), m, str(justificatif or "").strip(),
                          statut="soumise")
        self.s.remboursements[r.id] = r
        return r

    # — côté gestionnaire (V0 back-office) —
    def valider(self, rid) -> Remboursement:
        """Validation gestionnaire ; partielle à hauteur du disponible (RG-44)."""
        r = self._remb(rid)
        env = self.s.pmv(r.assure_id)
        if env.disponible <= 0:
            raise ValueError("Disponible nul — aucun remboursement possible")
        montant = min(r.montant, env.disponible)
        env.engager(montant, f"Remboursement {r.id}")
        r.montant_valide = montant
        r.partiel = montant < r.montant
        r.statut = "à_payer"
        return r

    def refuser(self, rid) -> Remboursement:
        r = self._remb(rid)
        r.statut = "refusée"
        return r

    def demander_piece(self, rid) -> Remboursement:
        r = self._remb(rid)
        r.statut = "piece_demandee"
        return r

    def _remb(self, rid) -> Remboursement:
        r = self.s.remboursements.get(rid)
        if r is None:
            raise KeyError(f"Remboursement introuvable : {rid}")
        return r


# ─────────────────────────────────────────────────────────────────────────────
#  Assistant — proxys LLM (analyse situationnelle & orientation)
#  Sans clé : le client applique son repli heuristique local (RG-63).
# ─────────────────────────────────────────────────────────────────────────────
def anticiper_prompt(prompt: str, system: str = None, max_tokens: int = 1100) -> str:
    """Relaie un prompt au modèle et renvoie le texte. Lève si ANTHROPIC_API_KEY
    est absente ou en cas d'erreur, pour que le client bascule sur son repli.
    L'orientation reste contrainte à la taxonomie fermée côté prompt et
    re-filtrée côté client (RG-60)."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY absente")
    payload = {
        "model": "claude-sonnet-4-6",
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        payload["system"] = system
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"content-type": "application/json", "x-api-key": key,
                 "anthropic-version": "2023-06-01"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())
    return "".join(b.get("text", "") for b in data.get("content", [])
                   if b.get("type") == "text").strip()


SYSTEME_ANALYSE = (
    "Tu es un travailleur social qui rédige une SYNTHÈSE SITUATIONNELLE "
    "pour un aidant familial et son proche dépendant, dans le cadre d'une "
    "garantie de soutien aux aidants. Rédige en français, ton neutre et "
    "factuel, 40 lignes maximum, en courts paragraphes : (1) la situation de "
    "l'aidant et sa charge, (2) la situation et l'autonomie du proche, "
    "(3) les tensions, risques et points de vigilance, (4) les axes de soutien "
    "prioritaires. Appuie-toi UNIQUEMENT sur les informations fournies ; "
    "n'invente aucun fait, aucun chiffre, aucun nom de dispositif ou de "
    "prestataire. Pas de diagnostic médical. Cette synthèse aidera à orienter "
    "le choix des solutions : reste descriptive et analytique, sans recommander "
    "de prestation nommée."
)
