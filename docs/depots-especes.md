# Module « Dépôts d'espèces »

Suivi des dépôts d'espèces des magasins (Léon Blum / Ornano) : la responsable photographie ses documents depuis son téléphone, le site lit les montants, rapproche encaissé et déposé, alerte le gérant et croise avec les Google Sheets « Comptage coffre » et la banque (Pennylane).

## Les deux interfaces

| URL | Pour qui | Accès |
|---|---|---|
| `/depots/` | La responsable (PWA à installer sur l'écran d'accueil du téléphone) | identifiant + code (compte `manager` dans `CASH_USERS`) |
| `/caisse/` | Le gérant (tableau de bord, alertes, réglages) | identifiant + code (compte `owner` dans `CASH_USERS`) |

Le dépôt GitHub est public : aucun code ni secret n'y est écrit. Les comptes sont gardés en base avec des codes hachés, et se définissent par `PUT /cash/admin/users` avec le jeton d'administration (`{"users": "identifiant:code:role:libellé,..."}`). Cinq codes faux de suite bloquent un identifiant pendant un quart d'heure.

### Parcours de la responsable
1. Entre son identifiant et son code (une seule fois, le téléphone s'en souvient 90 jours).
2. **Choisit obligatoirement le magasin** (gros bouton Léon Blum ou Ornano) avant d'accéder aux photos. Le magasin reste affiché en bandeau coloré sur tous les écrans suivants.
3. Ajoute ses photos : relevé de caisse (storeMate « Paiements magasin » ou rapport opérateur Orika), bordereau « sac sécurisé billets », ticket de l'automate, éventuellement une facture payée en espèces. Photos ou PDF.
4. Ajoute si besoin une remarque, puis **Envoyer**. C'est terminé pour elle : l'écran lui indique quand les documents seront analysés.

En mode `immediate` (lecture par l'API), l'étape 4 est remplacée par un écran de vérification : journées couvertes, espèces encaissées, montant déposé, écart, correction possible, puis **Confirmer**. Un document de l'autre magasin y bloque la confirmation.

Aucun rappel n'est envoyé à la responsable : les alertes vont uniquement au gérant.

## Règles de calcul
- `attendu = Σ espèces des relevés − Σ factures payées en espèces`
- `écart = attendu − déposé` (positif = moins déposé qu'encaissé ; une partie correspond aux pièces gardées au coffre)
- Le montant du dépôt retenu est celui du **ticket de l'automate** (sinon le total du bordereau). La date du dépôt est celle du ticket.
- Bordereau et ticket sont associés par numéro de sac, sinon par montant.

## Alertes (vers le gérant)
| Alerte | Gravité | Quand |
|---|---|---|
| Écart de dépôt | critique, e-mail immédiat | `|écart| ≥ seuil` (10 € par défaut, réglable) |
| Dépôt non crédité en banque | critique, e-mail immédiat | dépôt absent des transactions Pennylane après N jours (sac `DPT VRAC ESP 00051794xx`) |
| Journée sans relevé | à vérifier | journée ouverte non couverte par un relevé depuis plus de N jours |
| Espèces non déposées | à vérifier | relevé confirmé sans dépôt après N jours |
| Bordereau ≠ automate | à vérifier | la banque a compté un autre montant que le bordereau |
| Relevé / sac en doublon | à vérifier | période déjà couverte ou sac déjà enregistré |
| Dépôt du sheet sans justificatif | à vérifier | dépôt saisi dans le sheet sans bordereau envoyé |
| Dépôt absent du sheet | info | dépôt confirmé sur le site, pas encore reporté dans le sheet |
| Lecture incertaine | info | confiance de lecture < 60 % |

Les alertes non critiques sont regroupées dans un **récap e-mail quotidien** (heure réglable). Tout est visible et acquittable dans `/caisse/`.

Les contrôles tournent toutes les 15 minutes en tâche de fond (`CASH_SCHEDULER=1`) ou à la demande (`POST /cash/checks/run`, bouton « Lancer les contrôles »).

## Google Sheets « Comptage coffre »
Lecture seule de l'onglet de période courant (« Du … au … ») de chaque sheet : liste des dépôts, ajouts de monnaie, factures, écarts par caissier, comptage. Les IDs des sheets sont dans `config.json` (`cash.stores[].sheet_id`).

Mise en place : créer un compte de service Google (Google Cloud → IAM → comptes de service, activer l'API Google Sheets), télécharger sa clé JSON, la mettre dans `GOOGLE_SERVICE_ACCOUNT_JSON`, et **partager les deux sheets en lecteur** avec l'adresse e-mail du compte de service.

## Pennylane (rapprochement bancaire)
Clé API par société dans `PENNYLANE_API_KEY_AEJB` / `PENNYLANE_API_KEY_JAB` (Pennylane → Paramètres → API). Chaque dépôt est recherché par numéro de sac dans le libellé bancaire, sinon par montant et date proche.

## Lecture des photos
**Mode `deferred` (par défaut), sur l'abonnement claude.ai du gérant.** Le site stocke les photos sans les lire. Deux fois par jour, une tâche Claude planifiée (« Analyse des dépôts d'espèces », 12h05 et 18h05 heure de Paris) :
1. lit la file `GET /cash/worker/queue` et les consignes `GET /cash/worker/prompt` ;
2. télécharge chaque photo (`GET /cash/worker/documents/{id}`) et la lit elle-même ;
3. renvoie ses lectures (`POST /cash/worker/submissions/{id}/extractions`) : le site calcule l'écart, enregistre l'envoi et déclenche les alertes et e-mails ;
4. lance les contrôles (`POST /cash/worker/checks`), ce qui réveille aussi le service Render.

La tâche passe en réalité toutes les heures de 8h05 à 21h05 et demande d'abord `GET /cash/worker/should-run`. Elle ne travaille que s'il y a des photos en attente et que c'est l'heure d'un créneau prévu, ou que le gérant a cliqué « Analyser maintenant » dans le tableau de bord (`POST /cash/analysis/request`). Sinon elle s'arrête aussitôt.

La tâche s'authentifie avec un jeton propre, dérivé de `CASH_SECRET` (ou `CASH_WORKER_TOKEN`), qui ne donne accès qu'à la file d'analyse. Un document d'un autre magasin ou une lecture incomplète n'empêchent pas l'enregistrement : ils créent une alerte pour le gérant, qui peut annuler l'envoi depuis le tableau de bord. Si des photos attendent plus de 14 heures, une alerte « Photos en attente d'analyse » signale que la tâche ne tourne plus.

**Mode `immediate`.** Les photos sont lues à l'envoi par l'API Anthropic (`ANTHROPIC_API_KEY`, payante à l'usage, modèle `claude-opus-5-5`) ou OpenAI (`CASH_VISION_PROVIDER=openai`), et la responsable vérifie les montants avant de confirmer.

## Configuration
- `config.json` → bloc `cash` : magasins (libellé, entité, numéros de magasin de la caisse, couleur, sheet, société Pennylane) et règles par défaut.
- Les règles (seuil, délais, jours de fermeture, fériés) se modifient aussi depuis « Réglages » dans `/caisse/` (stockées en base).
- Variables d'environnement : voir `.env.example`.
- Les photos sont stockées dans `data/cash_docs/<magasin>/<AAAA-MM>/<envoi>/` (à sauvegarder ; sur un hébergeur à disque éphémère, prévoir un volume persistant).

## Déploiement sur Render (service existant « LAB-Assist »)
Le service Render déploie la branche principale du dépôt GitHub : fusionner la branche du module dans `main` déclenche le déploiement. Les nouvelles pages sont alors `https://<service>.onrender.com/depots/` et `/caisse/`.

À régler dans Render → Environment :
- `CASH_USERS`, `CASH_SECRET`, `ANTHROPIC_API_KEY`, `CASH_PUBLIC_URL`
- `DATABASE_URL` vers un PostgreSQL (Supabase, Neon, Render Postgres…). Sans cela, la base SQLite est effacée à chaque déploiement ou redémarrage.
- `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE`, `SUPABASE_BUCKET` : copie durable des photos (le disque local de Render gratuit est effacé à chaque déploiement ; le site relit alors les photos depuis Supabase Storage).
- SMTP, Google (`GOOGLE_SERVICE_ACCOUNT_JSON`) et Pennylane selon les intégrations voulues.

Offre gratuite Render : le service s'endort après 15 minutes sans trafic (premier chargement jusqu'à une minute) et les contrôles automatiques ne tournent pas pendant le sommeil. Deux options : un moniteur UptimeRobot qui appelle `/health` toutes les 5 minutes, ou passer en instance Starter.

## Tests
```
pip install -r requirements.txt pytest
python -m pytest tests/test_cash.py
```
Les tests utilisent `CASH_MOCK_EXTRACTION=1` : des fichiers JSON remplacent les photos.

## API (préfixe `/cash`)
`POST /auth` · `GET /stores` · `POST /submissions` (multipart `store_id` + `files[]`) · `POST /submissions/{id}/confirm` · `POST /submissions/{id}/cancel` · `GET /submissions` · `GET /submissions/{id}` · `GET /documents/{id}` · `GET /manager/recent` · `GET /dashboard?month=AAAA-MM&store_id=` · `GET /alerts` · `POST /alerts/{id}/ack|reopen` · `POST /checks/run` · `POST /sheets/sync` · `POST /digest/send` · `GET|PUT /settings`

Tâche d'analyse (jeton de la tâche ou gérant) : `GET /worker/queue` · `GET /worker/prompt` · `GET /worker/documents/{id}` · `POST /worker/submissions/{id}/extractions` (`{"documents": [{"document_id": 1, "extraction": {…}}]}`) · `POST /worker/checks`
