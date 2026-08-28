# MPA — Portail Aidant (pré-production)

Interface **Aidant** de la liquidation de la **Garantie de Soutien aux Aidants
(GSA)**, réécrite au propre à partir des maquettes validées, pour reprise par
**Pilot Systems**. Code volontairement resserré au seul périmètre aidant :
pas de console gestionnaire, pas de simulation de rôle pro, pas de données
de démonstration.

## Ce qui a changé par rapport aux maquettes de travail

| | Maquette (démo commerciale) | Ce livrable (pré-prod) |
|---|---|---|
| Catalogue | Snapshot aidance-pro embarqué (`CATALOGUE_RAW`) | **Aucun catalogue embarqué.** Connecteur HTTP vers `www.aidance-pro.com` (`catalogue.py`), état « non connecté » explicite tant que l'URL n'est pas posée. |
| Assurés | Jeux d'essai pré-remplis (Léa, Claire…) au démarrage | **Aucun assuré pré-rempli.** Le Store démarre vide ; seule la pré-inscription back-office en crée. |
| Échanges avec les pros | Simulés côté aidant (auto-chiffrage, auto-réalisation) | **Non automatisés.** Une demande de devis reste à l'état `demande` tant que le professionnel ne l'a pas chiffrée via l'API partenaire. Rechiffrage aidant supprimé. |
| Rôles | 4 rôles dans un seul shell (contrat, indemnisation, aidant, pro) | **Rôle aidant seul.** Les 3 autres rôles relèvent de la V0 back-office (Assur-aidant) ou de aidance-pro. |
| Formules commerciales, cockpit présentateur, personas, co-branding | Présents (usage commercial) | Retirés (hors périmètre pré-prod). |

## Architecture

```
core.py       Cœur de domaine : entités, Store, double-verrou PMV, moteurs
              (pré-inscription/profils, devis, remboursements), proxy LLM.
              Chaque règle porte sa référence RG-nn (spécifications
              fonctionnelles sections 2 & 3).
catalogue.py  Connecteur aidance-pro : AUCUNE donnée embarquée. Contrat
              d'interface JSON documenté en tête de fichier — c'est le
              document de référence pour l'intégration côté aidance-pro.
api.py        API HTTP (stdlib http.server), 3 surfaces distinctes :
              portail aidant, API partenaire aidance-pro, API back-office.
              Table des routes en tête de fichier.
aidant.html   Interface aidant seule (HTML/JS, sans dépendance). Accès par
              lien d'activation : /?a=<id_assure>.
tests.py      Suite de non-régression (27 assertions), catalogue de test
              injecté in-process — ne dépend d'aucun service externe.
```

## Les trois surfaces de l'API

**Portail aidant** (`aidant.html`) : dossier, profils, analyse
situationnelle, devis, remboursements, contact conseiller.

**API partenaire aidance-pro** — à appeler par aidance-pro, pas par ce
portail :
```
GET  /api/partenaire/devis?statut=demande
POST /api/partenaire/devis/<id>/etablir    {quantite, message_pro}
POST /api/partenaire/devis/<id>/realisee
```

**API back-office** (V0 Assur-aidant) :
```
POST /api/backoffice/preinscriptions
     {prenom, nom, email, ndd, montant, pfd:{prenom, lien, naissance, ndd}}
POST /api/backoffice/assures/<id>/crediter        {montant, ref?}
POST /api/backoffice/remboursements/<id>/valider|refuser|piece
POST /api/backoffice/devis/<id>/liberer            (libération J+7, tâche planifiée)
POST /api/catalogue/rafraichir                     (recharge le connecteur)
```

Le montant du crédit GSA est **reçu déjà calculé** à la pré-inscription : le
barème contractuel (NDD → montant) vit dans la V0 back-office, pas ici. Ce
portail ne connaît que le crédit qui lui est notifié.

## Connexion au catalogue aidance-pro

Rien n'est embarqué : voir le contrat d'interface complet en tête de
`catalogue.py`. Résumé :

```
GET {AIDANCE_PRO_CATALOGUE_URL}
Authorization: Bearer {AIDANCE_PRO_CATALOGUE_TOKEN}   (optionnel)

→ { "rows": [{pid, pro, pro_desc, nom, desc, familles, metiers,
              mod, nat, deps, unite, prix, rank}, …],
    "depts": [{"c":"...", "n":"..."}, …] }
```

`desc` doit être la description **complète** (non tronquée) : elle
s'affiche telle quelle sous chaque prestation. `pro_desc` alimente la fiche
au survol du nom du professionnel.

Sans URL posée, ou en cas d'échec de connexion, le portail affiche un état
« catalogue non connecté » — jamais de données fictives en pré-production.
Rechargement à chaud : `POST /api/catalogue/rafraichir`.

## Règles métier portées (RG-nn — cf. spécifications fonctionnelles)

- **RG-13** NDD 1–4 éligibles (5–6 exclus).
- **RG-41** Pré-inscription : PFD principal obligatoire (prénom, lien, âge, NDD).
- **RG-42** Double verrou du PMV : (1) profil aidant + RIB, (2) profil
  situationnel du PFD principal.
- **RG-43** Invariant PMV : disponible = solde − engagé.
- **RG-44** Remboursement : validation partielle à hauteur du disponible.
- **RG-45** Devis : demande → devisé (forfait : direct) → accepté/engagé →
  réalisée (pro, via API partenaire) → service fait (aidant) OU libération
  J+7 (back-office) → payé.
- **RG-46** Rejet bancaire → recrédit (`Enveloppe.rejeter`, méthode
  disponible, non exposée par une route — la gestion des rejets bancaires
  est un flux back-office à brancher).
- **RG-60/63** Orientation contrainte à une taxonomie fermée à 9 catégories,
  re-filtrée côté client que la réponse vienne du LLM ou du repli
  heuristique local.

## Variables d'environnement

| Variable | Effet |
|---|---|
| `ANTHROPIC_API_KEY` | Active l'assistant (analyse situationnelle + orientation). Sans clé : repli heuristique déterministe côté client, sur la même taxonomie fermée. `/api/diag` indique le mode actif. |
| `AIDANCE_PRO_CATALOGUE_URL` | URL du catalogue live aidance-pro. Sans elle : catalogue vide, état affiché explicitement. |
| `AIDANCE_PRO_CATALOGUE_TOKEN` | Jeton Bearer optionnel pour l'URL ci-dessus. |
| `PORT` | Port d'écoute (Render le fournit automatiquement). |

## Lancer en local

```bash
python3 api.py               # port 8765 par défaut
python3 tests.py             # suite de non-régression (27 assertions)
```
Ouvrir `http://127.0.0.1:8765/?a=<id_assure>` — l'identifiant s'obtient via
`POST /api/backoffice/preinscriptions` (voir `tests.py` pour un exemple
complet).

## Déployer sur Render

1. Pousser ces fichiers dans un dépôt Git.
2. **New → Blueprint** (détecte `render.yaml`), ou **New → Web Service** avec :
   - Build command : `pip install -r requirements.txt`
   - Start command : `python api.py`
3. Poser les variables d'environnement ci-dessus dans le tableau de bord Render.

## Hors périmètre — à la charge de la production (Pilot Systems)

- **Persistance** : l'état est en mémoire (`core.Store`), reparti à zéro à
  chaque redémarrage. Le modèle de données (dataclasses `core.py`) sert de
  schéma de départ pour la transposition Django/PostgreSQL.
- **Authentification** : l'accès aidant utilise un lien d'activation
  (`?a=<id>`) sans contrôle — à remplacer par une authentification en
  production (lien à usage unique + session, ou compte).
- **Bordereau SEPA (pain.001)** et **contrats/barèmes** : gérés par la V0
  back-office Assur-aidant, hors périmètre de ce portail.
- **CRM** : les demandes de contact conseiller sont stockées sur l'assuré
  (`Assure.contacts`) mais pas relayées ; à brancher sur l'outil interne.
- **Rejet bancaire** : la mécanique existe (`Enveloppe.rejeter`) mais
  aucune route ne l'expose — à raccrocher au retour du prestataire de
  paiement.
- **HDS** : dès que des données réelles entrent (dépassement du stade
  pré-prod), l'hébergement doit basculer sur l'infrastructure certifiée
  (AZNETWORK).

## Non-régression

Toute modification doit laisser `python3 tests.py` intégralement vert
(27/27) avant livraison.
