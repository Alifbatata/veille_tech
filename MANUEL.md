# Manuel d'utilisation — Veille Tech

Ce document s'adresse à toute personne qui veut **utiliser** le programme, sans connaissance technique préalable.

> 🆕 **Pour les vrais novices** : il existe maintenant un guide encore plus accessible avec des explications pas à pas et un lanceur Windows en double-clic. Voir [`COMMENT_CA_MARCHE.md`](COMMENT_CA_MARCHE.md) ou utiliser directement `lancer.bat`.

---

## 1. À quoi sert ce programme ?

Chaque fois que tu le lances, il :

1. **Cherche** sur internet les dernières publications scientifiques et articles de presse sur les revêtements industriels (PVD, CVD, ALD, etc.).
2. **Surveille les concurrents** listés dans `data/targets.json` (Oerlikon Balzers, Lam Research, Aixtron, Tokyo Electron, ULVAC, Picosun…).
3. **Découvre automatiquement** les nouveaux acteurs (entreprises et labos) qui apparaissent dans les brevets et papers, même si tu ne les connaissais pas.
4. **Cherche aussi dans 8+ domaines connexes** (photonique, MEMS, biomim, nanotech, IA, métamatériaux, métasurfaces, etc.) pour repérer les innovations transférables à PVD/ALD.
5. **Demande à une IA** (Google Gemini) de noter chaque article de 1 à 5 selon le **potentiel d'intégration cross-domaine** avec tes procédés.
6. **T'envoie un email** avec un résumé exécutif, les meilleurs articles classés par score, et une section dédiée aux acteurs nouvellement découverts.

L'objectif : tu reçois en 5 minutes ce qui t'aurait pris 3 heures à compiler manuellement, AVEC en bonus les pistes d'innovation auxquelles tu n'aurais pas pensé.

> 🆕 **Philosophie 2026** : la note 5★ ne signifie plus « ça parle de PVD » mais « cette découverte, COMBINÉE à PVD/ALD, génère une opportunité d'innovation ». Voir `SCORING.md` pour les détails.

---

## 2. Prérequis (à faire **une seule fois**)

### 2.1 Installer Python

- Aller sur https://www.python.org/downloads/ → Python 3.12 ou plus récent
- Pendant l'installation : **cocher « Add Python to PATH »**
- Vérifier dans PowerShell : `python --version` doit afficher `Python 3.12.x` ou supérieur

### 2.2 Installer les dépendances du projet

Ouvrir PowerShell **dans le dossier du projet** et lancer :

```powershell
pip install -r requirements.txt
```

### 2.3 Préparer le fichier `.env`

> 💡 **Le plus simple** : utilise l'assistant interactif `python configurer.py` qui te guide pas à pas pour chaque champ. Il pré-remplit ce qui existe déjà et te propose garder/modifier/voir.

À la racine du projet, créer un fichier appelé `.env` avec ce contenu :

```env
# Clé API Google Gemini (gratuite : https://aistudio.google.com/app/apikey)
GEMINI_API_KEY=AIza...ta_cle_ici

# Modèle Gemini principal (cascade fallback automatique sur ~38 modèles)
GEMINI_MODEL=gemini-2.5-flash
AI_BATCH_SIZE=30

# Compte Gmail expéditeur
GMAIL_USER=ton.compte@gmail.com
GMAIL_PASSWORD=xxxx xxxx xxxx xxxx

# Destinataires (séparés par virgule)
MAIL_RECIPIENT=mahmoud.unjeunearabe@gmail.com,mahmoud.alimohamad@positivecoating.ch
MAIL_MIN_SCORE=2

# (Optionnel) Clé Tavily — recherche Web élargie (1000 req/mois gratuites)
TAVILY_API_KEY=tvly-...

# (Optionnel) Clé Semantic Scholar — étend le rate-limit (sinon ban IP fréquent)
SEMANTIC_SCHOLAR_API_KEY=...

# ============================================================
# 🆕 Proxies résidentiels (FORTEMENT recommandés - anti-blocage)
# ============================================================
# Sans ça : ton IP perso est utilisée → risque de ban temporaire arXiv/GNews
# Avec ça : chaque requête sort par une IP résidentielle différente, indistinguable
# Provider recommandé : Decodo (https://decodo.com), ~$5-15/mois
# Format : http://USER:PASS@HOST:PORT
RESIDENTIAL_PROXY_PRIMARY=
RESIDENTIAL_PROXY_BACKUP=
PROXY_COUNTRY=CH
```

> ⚠️ `GMAIL_PASSWORD` n'est **pas** ton mot de passe Gmail habituel. C'est un **mot de passe d'application** à 16 caractères :
> 1. Va sur https://myaccount.google.com/apppasswords
> 2. Créer un nouveau mot de passe pour « Veille Tech »
> 3. Copier les 16 caractères dans le fichier `.env`

> 🌐 **Proxy résidentiel — fortement recommandé**. C'est ce qui te garantit de **ne jamais te faire bloquer** par les sources (Google News, arXiv, Patents). Provider conseillé : Decodo (~$7 prépayé = 6+ mois pour notre volume). Sans ça, le programme tourne quand même mais avec un risque de ban temporaire occasionnel. Voir le fichier `.env.example` pour les détails.

---

## 3. Comment lancer le programme

### Option A — Double-clic sur `lancer.bat` (recommandé pour novice)

Va simplement dans le dossier du projet et **double-clique sur `lancer.bat`**.
Le script va automatiquement :
- Vérifier que Python est installé
- Créer l'environnement virtuel si absent
- Installer les dépendances si manquantes
- Lancer l'assistant `configurer.py` si `.env` n'existe pas
- Démarrer `main.py`

C'est la méthode la plus simple — aucune ligne de commande à taper.

### Option B — En ligne de commande (PowerShell)

Dans PowerShell, depuis le dossier du projet :

```powershell
python main.py
```

### 🕐 IMPORTANT — quand lancer le programme

> **Lance le programme APRÈS 9h00 heure suisse** pour bénéficier des quotas IA frais.
>
> **Pourquoi ?** Les quotas gratuits Google AI Studio se renouvellent à minuit Pacific Time, ce qui correspond à **9h00 en Suisse**. Si tu lances avant 9h, tu utilises potentiellement les restes de quota de la veille.
>
> Le programme te le rappelle automatiquement au démarrage : un avertissement orange s'affiche si l'heure locale est entre minuit et 9h.

### 🔑 Modifier ta configuration sans tout recréer

Pour changer une clé API ou ajouter un destinataire d'email, lance simplement :

```powershell
python configurer.py
```

L'assistant te montre chaque champ existant et te propose de le **garder**, le **modifier** ou le **voir en clair**.

Pour vérifier seulement (sans modifier) que ta config est complète :

```powershell
python configurer.py --check
```

Tu verras défiler des logs comme ceci :

```
🚀 Démarrage de l'orchestrateur de veille technologique
📡 Récupération RSS : ArXiv – Applied Physics
   └─ 9 article(s) collecté(s)
📡 Récupération RSS : MDPI – Coatings
   └─ 50 article(s) collecté(s)
...
🔍 Google News [1/14] : « ("Positive Coating" OR "Oerlikon" ...) PVD »
   └─ 4 article(s) trouvé(s)
...
🤖 Modèle Gemini initialisé : gemini-2.5-flash
🔄 Batch 1/9 (articles 0–19)…
   └─ Batch offset=0 : 6 retenu(s) / 20 total
...
📧 Préparation de l'envoi du digest par email...
✅ Email envoyé avec succès
🎉 Orchestrateur terminé avec succès.
```

**Durée typique** : ~10 minutes.

---

## 4. Que se passe-t-il étape par étape ?

> 🆕 **Nouveau** : depuis la dernière mise à jour majeure, le pipeline est nettement plus rapide et plus furtif :
> - **Parallélisation** des 4 sources scientifiques OpenAlex / Crossref / HAL / Semantic Scholar → -60 min sur un run complet
> - **Pré-filtre Python** des articles hors-sujet AVANT Gemini → -15-30% d'appels IA (économise tokens)
> - **Cookies persistents** entre runs → ressemble à un utilisateur récurrent (browser-like)
> - **Atomic writes** sur tous les JSON → un Ctrl+C ou un crash ne corrompt plus de fichiers d'état
> - **Soft-ban detection** : les CAPTCHA / pages "rate limited" déguisées en HTTP 200 sont maintenant détectées et déclenchent un cooldown long
> - **Headers cohérents** : Accept-Encoding, Sec-Fetch-* adapté au contexte (API JSON vs RSS), Referer plausible, params API shufflés (anti fingerprinting applicatif)


```
   ┌─────────────────────────────────────────────────────────────┐
   │  Tu tapes : python main.py                                   │
   └─────────────────────────────────────────────────────────────┘
                              │
                              ▼
   ┌─────────────────────────────────────────────────────────────┐
   │  PRE-RUN INTERACTIF                                          │
   │  • Vérification heure (avert si avant 9h Suisse)             │
   │  • Choix mémoire : Filtrer / Tout renvoyer / Reset           │
   │  • Affichage des 5 listes de cibles + édition optionnelle    │
   │  • Choix volume RSS (5 presets) + estimation durée totale    │
   │  • 🔒 Vérification quotas API (table colorée 9 sources)      │
   │  • Récap final + confirmation                                │
   └─────────────────────────────────────────────────────────────┘
                              │
                              ▼
   ┌─────────────────────────────────────────────────────────────┐
   │  ÉTAPE 1 — Sauvegarde de l'historique                        │
   │  Le digest précédent est copié dans previous_ai_output.json  │
   └─────────────────────────────────────────────────────────────┘
                              │
                              ▼
   ┌─────────────────────────────────────────────────────────────┐
   │  ÉTAPE 2 — Scraping (durée variable selon les sources)       │
   │                                                              │
   │  9 sources interrogées séquentiellement :                    │
   │  • 5 flux RSS (ArXiv, MDPI, IEEE, ScienceDaily)              │
   │  • arXiv Search : ~111 requêtes (broadcast keywords/solos/   │
   │    research_orgs/cross_domain_topics)                        │
   │  • OpenAlex / Crossref / HAL / Semantic Scholar : ~111 chaque│
   │  • Tavily Web (si TAVILY_API_KEY) : ~110                     │
   │  • Google Patents (NOUVEAU) : ~112 (extraction des assignees)│
   │  • Google News : ~589 recherches (entreprise × keyword + solos)
   │                                                              │
   │  Découverte automatique d'acteurs : extrait les noms de      │
   │  toutes les entreprises/labos vus dans les résultats Patents │
   │  et OpenAlex → data/discovered_actors.json (cumulatif)       │
   │                                                              │
   │  Anti-bot : 18 couches (TLS Chrome rotation, locales rota-   │
   │  tives, délais humains, pre-flight arXiv, cooldown progres-  │
   │  sif, circuit breakers, optionnellement proxy résidentiel).  │
   │                                                              │
   │  → écrit data/scraper_output.json                            │
   └─────────────────────────────────────────────────────────────┘
                              │
                              ▼
   ┌─────────────────────────────────────────────────────────────┐
   │  ÉTAPE 3 — Filtrage IA (philosophie 2026 : transférable)     │
   │                                                              │
   │  • Découpe les articles en lots de AI_BATCH_SIZE (défaut 30) │
   │  • Envoie chaque lot à Gemini Flash                          │
   │  • Gemini évalue le POTENTIEL D'INTEGRATION cross-domaine    │
   │    (PVD/ALD + photonique/biomim/MEMS/nanotech/IA = ?)        │
   │  • Note chaque article de 1 à 5 avec angle d'intégration     │
   │  • Force score ≥ 4 si un concurrent listé est mentionné      │
   │  • Génère un résumé exécutif global                          │
   │                                                              │
   │  Résilience : cascade auto sur ~38 modèles (Gemini 2.5/2.0/  │
   │  1.5, Gemma 3 27B/12B/9B/4B/1B...) si saturation.            │
   │  → écrit data/ai_filter_output.json                          │
   └─────────────────────────────────────────────────────────────┘
                              │
                              ▼
   ┌─────────────────────────────────────────────────────────────┐
   │  ÉTAPE 4 — Envoi email (~5 secondes)                         │
   │                                                              │
   │  Sections du digest :                                        │
   │  1. Header + résumé exécutif                                 │
   │  2. Articles classés par score (5★ → 1★)                     │
   │  3. 🔍 Acteurs DÉCOUVERTS automatiquement (NOUVEAU)          │
   │  4. ⏪ Déjà vu la semaine passée                              │
   │                                                              │
   │  Badge violet 📌 « Déjà envoyé » sur les articles connus     │
   │  (mode TOUT_RENVOYER uniquement).                            │
   └─────────────────────────────────────────────────────────────┘
                              │
                              ▼
                       📬 Email reçu
```

---

## 5. Vais-je recevoir les mêmes articles à chaque run ?

**Tu décides à chaque lancement.** Au démarrage, le programme te demande dans **Étape 2/4 : mémoire** comment tu veux gérer les articles déjà envoyés.

**Trois choix possibles :**

| Touche | Mode | Quand l'utiliser |
|--------|------|------------------|
| **F** (défaut) | **Filtrer** les articles déjà envoyés | Run hebdo normal — tu ne veux que des nouveautés |
| **T** | **Tout renvoyer** sans filtre | Test, démo, ou rattrapage |
| **R** | **Réinitialiser** la mémoire (effacer `seen_urls.json`) puis filtrer | Nouvelle équipe destinataire, ou repartir propre |

**Comment fonctionne le filtre :**
- Chaque article que tu reçois a une URL unique
- Cette URL est sauvegardée dans `data/seen_urls.json` (max 10 000 entrées, FIFO)
- Au prochain run en mode **F**, ces URLs sont automatiquement exclues du digest

**Concrètement :**
- Aujourd'hui tu as reçu 49 articles → leurs URLs sont mémorisées
- Lundi prochain, le programme va re-scraper ~165 articles bruts
- En mode **F**, il en filtrera ~49 (ceux déjà envoyés)
- Tu recevras donc **uniquement les nouveautés** de la semaine

**Pour les runs automatisés (cron / Planificateur Windows)** : sans interaction utilisateur, le défaut est **T (tout renvoyer)**. Pour activer le filtre dans un run automatisé, exporte la variable d'environnement `USE_MEMORY=true` avant `python main.py`.

---

## 6. Envoyer un récapitulatif de TOUT l'historique à de nouveaux destinataires

**Cas d'usage** : tu finalises les tests, et tu veux maintenant envoyer à toute l'équipe **tout ce qui a été collecté depuis le début** — pour qu'ils ne « ratent pas » la phase de mise au point.

**Pourquoi `USE_MEMORY=False` ne marche PAS pour ça** : il ne ferait que désactiver le filtre des URLs vues. Mais le scraper ne récupère que ce qui est *actuellement* dans les flux RSS / Google News (~50 derniers articles par source). Les articles d'il y a 2 mois ont déjà disparu des flux. Donc tu obtiendrais une liste **incomplète**.

**La vraie solution : l'archive cumulative.**

Le programme maintient automatiquement un fichier `data/articles_archive.json` qui accumule tous les articles filtrés depuis le démarrage. Pour envoyer un récapitulatif à des nouveaux destinataires :

```powershell
# Récap complet à une seule personne
python send_recap.py "alice@boite.com"

# Plusieurs destinataires
python send_recap.py "alice@boite.com,bob@boite.com,carol@boite.com"

# Uniquement les meilleurs articles (score ≥ 4)
python send_recap.py "alice@boite.com" --min-score 4

# Sujet personnalisé
python send_recap.py "alice@boite.com" --subject "Veille Tech — Bienvenue dans le pipeline"

# Tester sans envoyer (génère data/recap_preview.html)
python send_recap.py "test@test.com" --dry-run
```

**Avantages :**
- ✅ Pas de re-scraping (instantané)
- ✅ Pas de coût Gemini (l'IA a déjà fait son travail)
- ✅ Inclut TOUT l'historique, même les articles disparus des flux

**Quand utiliser quoi :**

| Situation | Commande |
|---|---|
| Run hebdomadaire normal | `python main.py` |
| Onboarding d'un nouveau destinataire | `python send_recap.py "...,..."` |
| Tester le rendu sans spammer | `python send_recap.py "x@x.com" --dry-run` |
| Le scraping a marché mais IA / email a planté | `python resume_pipeline.py "...,..."` (voir ci-dessous) |

### Reprise sans re-scraper (`resume_pipeline.py`)

**Cas d'usage** : tu lances `python main.py`, le scraping termine après plusieurs heures (4000+ articles dans `data/scraper_output.json`), MAIS le filtrage IA crashe ou l'envoi email échoue (quota Gemini, mauvaise config SMTP, etc.). Tu ne veux pas relancer 14-22h de scraping.

```powershell
# Mode interactif (prompt pour les destinataires)
python resume_pipeline.py

# Avec destinataires CLI
python resume_pipeline.py "alice@x.com,bob@y.com"

# Preview HTML sans envoi
python resume_pipeline.py "alice@x.com" --dry-run

# Envoi sans relancer Gemini (réutilise data/ai_filter_output.json déjà filtré)
python resume_pipeline.py "alice@x.com" --no-ai

# Skip confirmation interactive avant envoi
python resume_pipeline.py "alice@x.com" --yes
```

Étapes effectuées :
1. Lit `data/scraper_output.json`
2. Filtre les articles < 30 jours
3. Lance le filtrage IA Gemini (sauf si `--no-ai`)
4. Met à jour l'archive cumulative
5. Demande confirmation puis envoie l'email

---

## 7. Options avancées de la mémoire

**Au lancement interactif** (`lancer.bat` ou `python main.py`) : utilise les choix **F/T/R** à l'Étape 2/4 (voir §5).

**Pour un run automatisé (cron, Planificateur Windows) :**
- Variable d'env `USE_MEMORY=true` → active le filtre
- Variable d'env `USE_MEMORY=false` (ou absente) → désactive le filtre
- Pour réinitialiser la mémoire en mode automatisé : supprime le fichier `data/seen_urls.json` avant le lancement

---

## 8. Lancer le programme automatiquement chaque semaine

### Sur Windows (Planificateur de tâches)

1. Ouvrir « Planificateur de tâches » (touche Windows → taper « planificateur »)
2. Action → Créer une tâche de base
3. Nom : `Veille Tech Hebdomadaire`
4. Déclencheur : Hebdomadaire → choisir jour + heure (ex: lundi 8h00)
5. Action : Démarrer un programme
   - Programme : `python.exe`
   - Arguments : `main.py`
   - Démarrer dans : `C:\Users\mohamadm\Desktop\veille_tech`
6. Cocher « Ouvrir les propriétés... » → onglet Conditions → décocher « Démarrer la tâche uniquement si l'ordinateur est sur secteur » si portable
7. Valider

L'ordinateur doit être allumé à l'heure choisie. Sinon, le Planificateur peut être configuré pour exécuter au prochain démarrage.

---

## 9. Personnalisation rapide

### Paramètres globaux (`src/config.py`)

| Paramètre | Valeur actuelle | Effet |
|---|---|---|
| `MAX_ARTICLES_PER_SOURCE` | 50 (modifiable au lancement via les 5 presets) | Cap par flux RSS |
| `RECENT_DAYS_LIMIT` | 90 | Articles plus vieux ignorés |
| `USE_MEMORY` | choisi à chaque lancement interactif | Filtre des URLs déjà envoyées |

### Cibles de recherche (`data/targets.json`) — 5 listes

| Liste | Rôle | Editable via menu CLI ? |
|---|---|---|
| `companies` | Entreprises industrielles (équipementiers PVD/ALD) | ✅ Actions 1/2 |
| `keywords` | Mots-clés couplés × companies sur Google News | ✅ Actions 3/4 |
| `solo_keywords` | Phrases multi-mots cherchées seules | ✅ Actions 5/6 |
| `research_orgs` | Labos / universités qui publient (broadcast science) | ✅ Actions 7/8 |
| `cross_domain_topics` | Thèmes transversaux (photonique, MEMS, biomim, nanotech, IA, etc.) | ✅ Actions 9/10 |

Pour éditer : lance `python main.py`, accepte de modifier les cibles → tu accèdes à un **menu de 14 actions** organisées par couleur. La numérotation correspond aux paires +/- (ajout/suppression) par liste.

### 🆕 Auto-tuning (boucle d'amélioration continue)

À la fin de chaque run, le module `src/auto_tuner.py` ferme la boucle de feedback automatique. Tu n'as plus besoin d'éditer manuellement `targets.json` la plupart du temps — le système s'optimise tout seul à partir des stats des runs précédents.

**Trois actions automatiques en séquence :**

#### 1. Auto-promotion v2 — abaissée à 5 occurrences + stickiness

Les acteurs (entreprises/labos) vus régulièrement dans les résultats Patents et OpenAlex sont automatiquement ajoutés à `targets.json` quand :
- `count >= 5` occurrences cumulées (vs 30 auparavant)
- **ET** `appearances_runs >= 2` (vus sur au moins 2 runs distincts) — la condition de **stickiness** évite de promouvoir un acteur qui aurait spike sur un seul run (ex: une conférence ponctuelle)

Heuristique de classification enrichie : suffixes entreprise (`GmbH`/`Inc`/`Ltd`/`Technologies`/`Systems`/`Coatings`/...), mots-clés labo (`University`/`Institut`/`Fraunhofer`/`CNRS`/`Hochschule`/...).

Cap : **10 promotions max par run** (configurable).

#### 2. Auto-purge des cibles stériles

Une cible (`solo_keyword`, `cross_domain_topic` ou `research_org`) est **automatiquement retirée** de `targets.json` quand :
- `runs_total >= 8` (assez d'historique pour juger)
- `hits_total == 0` (n'a JAMAIS produit un hit, jamais)
- `consecutive_zeros >= 8` (8 runs consécutifs à 0)
- **ET** aucune source n'a produit de hit (protection croisée : si la cible est productive sur Crossref mais pas sur OpenAlex, elle reste)

Cap : **5 suppressions max par run** (conservateur). Backup atomique de `targets.json` dans `data/backups/` (rotation 10) avant chaque modification. L'historique des suppressions est conservé dans `data/archived_targets.json` pour rollback possible.

> ⚠️ Les `companies` et `keywords` ne sont **PAS** auto-purgés : ils sont combinés en OR-groups dans les requêtes Google News (14 requêtes au lieu de 294), donc l'attribution individuelle des hits est impossible.

#### 3. Auto-expansion par tier (Hot / Standard / Cold)

Le système classe automatiquement chaque requête `(query, source)` selon son historique de hits :

| Tier | Critère | Effet sur le prochain run |
|---|---|---|
| 🔥 **Hot** | Top 10% des hits cumulés | `max_results × 1.5` (plus de couverture sur ce qui marche) |
| 📊 **Standard** | Le reste | inchangé |
| 🧊 **Cold** | ≥ 3 runs consécutifs à 0 | `max_results × 0.5` (économie bandwidth/quotas) |

Bornes : `max_results` ∈ `[5, 200]`. Le système s'auto-régule à chaque run.

#### Variables `.env` (toutes optionnelles, valeurs par défaut industrielles)

```env
AUTO_TUNE_ENABLED=true                  # Master switch (tout désactiver d'un coup)
AUTO_TUNE_DRY_RUN=false                 # true = log les actions sans toucher au disque

# Auto-promote v2
AUTO_PROMOTE_MIN_COUNT=5
AUTO_PROMOTE_MIN_RUNS=2
AUTO_PROMOTE_MAX_PER_RUN=10

# Auto-purge (très conservateur par défaut)
AUTO_PURGE_ENABLED=true
AUTO_PURGE_MIN_RUNS=8
AUTO_PURGE_MIN_CONSECUTIVE_ZEROS=8
AUTO_PURGE_MAX_PER_RUN=5

# Auto-expansion par tier
AUTO_EXPAND_ENABLED=true
AUTO_EXPAND_HOT_MULTIPLIER=1.5
AUTO_EXPAND_COLD_MULTIPLIER=0.5
AUTO_EXPAND_HOT_PERCENTILE=10
AUTO_EXPAND_COLD_CONSECUTIVE_ZEROS=3
```

#### Mode dry-run (audit avant activation)

Pour voir ce que le système ferait sans rien modifier :

```powershell
$env:AUTO_TUNE_DRY_RUN="true"; python main.py
```

Les logs afficheront `🧪 [DRY-RUN] ...` pour chaque action qui aurait été prise.

#### Revue manuelle reste toujours disponible

Tu peux toujours utiliser **Action 11** du menu d'édition (`python main.py` → menu édition) pour revoir / rejeter / ajouter manuellement les acteurs sous le seuil d'auto-promotion :
- `aN` → ajouter le candidat #N à `companies`
- `lN` → ajouter le candidat #N à `research_orgs`
- `rN` → rejeter (retirer des candidats)
- `q` → quitter la revue

Et **Action 12** pour voir les stats des requêtes (top 15 productives / top 15 stériles / tendance inter-runs) — c'est sur ces stats que l'auto-tuner se base.

---

## 10. Dépannage

### « Aucun article reçu » dans l'email

C'est normal si rien de neuf n'a été publié cette semaine sur tes sujets. L'email arrive quand même avec « 0 innovation(s) » dans le sujet.

### Erreur « GEMINI_API_KEY manquante »

Vérifier que le fichier `.env` existe à la racine et contient bien la ligne `GEMINI_API_KEY=...`.

### Le scraping a marché mais l'IA ou l'email a planté (ne pas tout relancer !)

Tes articles bruts sont sauvegardés dans `data/scraper_output.json`. Pour reprendre le pipeline sans relancer 14-22h de scraping :

```powershell
python resume_pipeline.py "alice@boite.com,bob@boite.com"
```

Voir section 6 pour les options (`--dry-run` preview, `--no-ai` réutilise filtrage existant, `--yes` skip confirmation).

### Erreur d'authentification SMTP

- Vérifier que `GMAIL_PASSWORD` est bien un **mot de passe d'application** (16 caractères avec espaces), pas ton mot de passe normal
- Vérifier que la double authentification Google est activée sur le compte (prérequis pour les mots de passe d'app)

### « Quota API dépassé »

Le free tier de `gemini-2.5-flash` est de 250 req/jour. Le programme bascule **automatiquement** sur la cascade de ~38 modèles fallback (Gemini Lite, Gemma 27B/12B…) en cas de saturation. Tu ne perds aucun article. Si vraiment tous les 38 sont épuisés (extrêmement rare), attendre 24h ou activer le pay-as-you-go sur https://aistudio.google.com.

### MDPI renvoie 0 article (timeout)

Notre bypass anti-Cloudflare est efficace mais peut occasionnellement échouer si MDPI est lent. Les timeouts ont été augmentés (warm-up 20s, fetch 30s pour MDPI/ScienceDaily) — devrait suffire. Si ça persiste : relancer plus tard, ou changer la valeur d'`impersonate` dans `src/scraper.py` (ex: `chrome120`).

### arXiv search reste bloqué (HTTP 429)

Si tu vois « 🛑 arXiv pre-flight : HTTP 429/403 » : ton IP est temporairement en cooldown serveur arXiv. Ce n'est pas un bug du code (qui a un User-Agent identifiable + HTTPS + délais 15-30s + circuit breaker pré-flight). C'est arXiv qui te demande de patienter. Solutions :
- **Attendre 4-6h** que la sliding window se reset
- OU configurer un **proxy résidentiel** (variable `RESIDENTIAL_PROXY_PRIMARY` dans `.env`) qui fait sortir chaque requête par une IP résidentielle différente — élimine ce risque structurellement
- OU laisser le pipeline continuer : les autres sources (OpenAlex, Crossref, S2) couvrent ~85-95% du même corpus arXiv

### Test du proxy résidentiel

```powershell
python -m src.proxy_manager
```

Affiche le pool, fait un health check (via api.ipify.org puis fallback httpbin/icanhazip), montre l'IP actuelle, le cumul de bande passante consommée et le cap configuré. Si ✅ tu peux lancer le pipeline. Si ❌, vérifie tes credentials dans `.env`.

**Reset du compteur bande passante** (utile en début de nouveau quota mensuel) :

```powershell
python -m src.proxy_manager --reset
```

### Plafond de bande passante (trial Decodo 100 MB)

Variable `.env` : `PROXY_BANDWIDTH_CAP_MB=80`. Si le cumul cross-run dépasse, le proxy bascule **automatiquement** en mode direct pour le reste du run. Le compteur est persisté dans `data/proxy_bandwidth.json`. Affichage en fin de run dans le log.

### Le pipeline est-il en mode parallèle ?

Variable `.env` : `SCRAPE_PARALLEL_SCIENTIFIC=true` (défaut). Lance OpenAlex / Crossref / HAL / Semantic Scholar **en parallèle** via 4 threads. Gain typique : -60 min sur un run complet. Pour debug d'une source en particulier, mettre `false`.

---

## 11. Résumé en 3 lignes

1. `python main.py` une fois par semaine
2. Tu reçois un email avec uniquement les nouveautés
3. Tout se configure dans `src/config.py` et `data/targets.json`
