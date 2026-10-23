# Module « Dépôts d'espèces »

Suivi des dépôts d'espèces des magasins (Léon Blum / Ornano) : la responsable photographie ses documents depuis son téléphone, le site lit les montants, rapproche encaissé et déposé, alerte le gérant et croise avec les Google Sheets « Comptage coffre » et la banque (Pennylane).

## Les deux interfaces

| URL | Pour qui | Accès |
|---|---|---|
| `/depots/` | La responsable (PWA à installer sur l'écran d'accueil du téléphone) | identifiant + code (compte `manager` dans `CASH_USERS`) |
| `/caisse/` | Le gérant (tableau de bord, alertes, réglages) | identifiant + code (compte `owner` dans `CASH_USERS`) |

Comptes par défaut : gérant `1` / `8626`, responsable `2` / `1234`. Pour un adjoint, ajouter `3:1234:manager:Adjoint` à `CASH_USERS`.

### Parcours de la responsable
1. Entre son identifiant et son code (une seule fois, le téléphone s'en souvient 90 jours).
2. **Choisit obligatoirement le magasin** (gros bouton Léon Blum ou Ornano) avant d'accéder aux photos. Le magasin reste affiché en bandeau coloré sur tous les écrans suivants.
3. Ajoute ses photos : relevé de caisse (storeMate « Paiements magasin » ou rapport opérateur Orika), bordereau « sac sécurisé billets », ticket de l'automate, éventuellement une facture payée en espèces. Photos ou PDF.
4. Le site lit chaque document et affiche : journées couvertes, espèces encaissées, montant déposé, écart. Elle peut corriger un montant ou une date, ajouter une remarque, puis **Confirmer**.
5. Si un document appartient à l'autre magasin (numéro de magasin storeMate/Orika ou titulaire du compte AEJB/JAB sur le bordereau), la confirmation est **bloquée** avec un bouton « Mauvais magasin → changer ».

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
Par défaut, les photos sont lues par Claude via le SDK Anthropic (modèle `claude-opus-5-5`, images et PDF envoyés tels quels). Il faut une **clé API** créée sur la console Anthropic (`ANTHROPIC_API_KEY`) : un abonnement claude.ai ne permet pas d'appeler l'API depuis un serveur. `CASH_VISION_PROVIDER=openai` rebascule sur le mécanisme OpenAI du module CNI.

## Configuration
- `config.json` → bloc `cash` : magasins (libellé, entité, numéros de magasin de la caisse, couleur, sheet, société Pennylane) et règles par défaut.
- Les règles (seuil, délais, jours de fermeture, fériés) se modifient aussi depuis « Réglages » dans `/caisse/` (stockées en base).
- Variables d'environnement : voir `.env.example`.
- Les photos sont stockées dans `data/cash_docs/<magasin>/<AAAA-MM>/<envoi>/` (à sauvegarder ; sur un hébergeur à disque éphémère, prévoir un volume persistant).

## Tests
```
pip install -r requirements.txt pytest
python -m pytest tests/test_cash.py
```
Les tests utilisent `CASH_MOCK_EXTRACTION=1` : des fichiers JSON remplacent les photos.

## API (préfixe `/cash`)
`POST /auth` · `GET /stores` · `POST /submissions` (multipart `store_id` + `files[]`) · `POST /submissions/{id}/confirm` · `POST /submissions/{id}/cancel` · `GET /submissions` · `GET /submissions/{id}` · `GET /documents/{id}` · `GET /manager/recent` · `GET /dashboard?month=AAAA-MM&store_id=` · `GET /alerts` · `POST /alerts/{id}/ack|reopen` · `POST /checks/run` · `POST /sheets/sync` · `POST /digest/send` · `GET|PUT /settings`
