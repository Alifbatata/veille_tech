# Architecture technique — Veille Tech

Document destiné aux développeurs qui maintiennent ou étendent le programme.

---

## 1. Vue d'ensemble

```
                        ┌──────────────────┐
                        │     main.py      │   Orchestrateur
                        │  (entry point)   │   • Charge .env
                        └─────────┬────────┘   • Pipeline 5 étapes
                                  │             • Catch global → email d'erreur
        ┌─────────────────────────┼──────────────┬──────────┐
        ▼                         ▼              ▼          ▼
  ┌────────────┐  ┌──────────────────┐  ┌────────────┐  ┌──────────┐
  │ scraper.py │→ │   ai_filter.py    │→ │ archive.py │→ │ mailer.py │
  └─────┬──────┘  └────────┬─────────┘  └──────┬─────┘  └────┬─────┘
        │                  │                   │             │
        ▼                  ▼                   ▼             ▼
  ┌──────────────┐   ┌──────────────┐   ┌────────────┐  ┌────────────┐
  │ curl_cffi +  │   │   Gemini     │   │  JSON      │  │  SMTP TLS  │
  │ feedparser + │   │   Flash 2.5  │   │  cumulé    │  │  smtp.gmail│
  │ proxy_mgr    │   │ + cascade    │   └────────────┘  └────────────┘
  └──────┬───────┘   │ (38+ models) │
         │           └──────────────┘
         ▼
   8 sources web :
   RSS, arXiv search, OpenAlex, Crossref, HAL,
   Semantic Scholar, Tavily, Google Patents, Google News

  Données persistées dans data/ :
    targets.json              (input — 5 listes : companies, keywords,
                               solo_keywords, research_orgs, cross_domain_topics)
    seen_urls.json            (mémoire FIFO des URLs déjà envoyées, cap 10k)
    scraper_output.json       (sortie étape 2)
    ai_filter_output.json     (sortie étape 3)
    previous_ai_output.json   (snapshot du run précédent)
    scraper_checkpoint.json   (checkpoint partiel pendant le scraping)
    articles_archive.json     (archive cumulative pour rattrapage, cap 5k)
    discovered_actors.json    (acteurs découverts auto. via Patents/OpenAlex)
```

**Mode rattrapage** : `send_recap.py` est un point d'entrée alternatif qui lit `articles_archive.json` et expédie un récapitulatif sans relancer scraping/IA.

---

## 2. Pipeline détaillé

### Étape 1 — Rotation de l'historique

- Si `data/ai_filter_output.json` existe (run précédent), il est copié vers `data/previous_ai_output.json`.
- Permet au mailer d'afficher la section « Déjà vu la semaine passée » avec les top articles 4★/5★.
- Échec non-fatal : le run continue même si la copie échoue.

### Étape 2 — Scraping (`scraper.py:run_scraper`)

8 sources interrogées, dans cet ordre, avec pause inter-source de 3-9 min (`_inter_source_break`) :

| # | Source | Volume req/run (35 ent × 16 kw + 29 solos + 25 labos + 36 cross) | Quota |
|---|---|---|---|
| 1 | **RSS** (5 flux : ArXiv ×2, MDPI, IEEE, ScienceDaily) | 5 | aucun |
| 2 | **arXiv Search API** | ~111 (7 base + kw + solos + orgs + cross) | aucun (politely 3s+) |
| 3 | **OpenAlex** | ~112 | 100k/jour gratuit |
| 4 | **Crossref** | ~112 | aucun strict |
| 5 | **HAL** (CNRS, paires bilingues FR/EN) | ~112 | aucun strict |
| 6 | **Semantic Scholar** | ~111 | 100/5min sans clé, 1/s avec |
| 7 | **Tavily Web Search** | ~110 | 1000/mois free tier |
| 8 | **Google Patents** | ~112 (assignee broadcasté) | aucun officiel |
| 9 | **Google News** (entreprises × keywords + solos) | ~589 (35×16+29) | soft limit ~500/run |

**Construction des requêtes** (`build_*_queries()`) :

Chaque source a une **base hardcodée** (concepts fondamentaux PVD/CVD/ALD : 5 à 8 lignes selon la source) **plus le broadcast des 4 listes utilisateur** :
- `keywords` (couplés avec `companies` pour GNews)
- `solo_keywords` (broadcast partout, GNews inclus)
- `research_orgs` (broadcast science uniquement, format adapté : `assignee:"X"` pour Patents, `all:"X"` pour arXiv, etc.)
- `cross_domain_topics` (broadcast science uniquement, focus innovations transférables)

Exemple pour arXiv : `ti:"X" OR abs:"X"` pour chaque entrée. Pour Patents : `assignee:"X"` pour les research_orgs, phrase exacte pour le reste.

**Phase Google News (mode weekend furtif)** :
- `build_gnews_queries()` génère **1 requête unitaire par couple (entreprise, mot-clé)** + 1 par solo : ~589 req
- WHY le produit cartésien : Google News tronque silencieusement les longues OR-grouping. Une requête courte par couple garantit la pertinence
- **Délais inter-requêtes humains mixtes** (mode weekend, moyenne ~141s) :
  - 15% rapides (20-50s) — humain qui scrolle un titre
  - 50% normaux (60-120s) — humain qui lit un résumé
  - 25% lents (120-300s) — humain qui lit un article complet
  - 10% très lents (5-9 min) — pause téléphone / café
- **Rotation de session toutes les 30 requêtes** + long break humain de 6-15 min
- **Pause circadienne** : après 6h, sleep aléatoire de 4-6h (« humain qui dort »)
- **Locales rotatives** : 8 variantes hl/gl/ceid tirées au hasard
- **Header `Referer: https://www.google.com/`** ajouté à chaque appel
- **Circuit breaker à 3 strikes consécutifs** : abandon GNews mais le pipeline continue
- Checkpoint `data/scraper_checkpoint.json` tous les 5 appels

**Pré-flight arXiv** (depuis le ban Mai 2026) : 1 requête test au démarrage du bloc arXiv. Si HTTP 429/403 → message clair « ton IP est probablement en cooldown serveur arXiv », le bloc est skippé et OpenAlex/Crossref/etc. couvrent ~85-95% du même corpus. Délai inter-requête arXiv passé à 15-30s + User-Agent identifiable + HTTPS forcé.

**Découverte automatique d'acteurs** (depuis Mai 2026) :
- À chaque résultat **Patents** : `_record_actor(patent.assignee, "patents")`
- À chaque résultat **OpenAlex** : `_record_actor(institution.display_name, "openalex")`
- Filtrage : ignore les acteurs déjà dans `companies` ou `research_orgs`
- Persistance cumulative dans `data/discovered_actors.json` avec compteur d'occurrences
- Plus un acteur revient sur plusieurs runs, plus le signal est fort

**Post-traitement** :
- Dédoublonnage par **URL** : `_normalize_url` strip params trackers, fragment, slash final, lowercase
- Dédoublonnage par **titre** : `_normalize_title` + `_dedup_by_title` capturent le même article via plusieurs sources. En cas de collision, on conserve le résumé le plus long
- Si `USE_MEMORY=True` : exclusion des URLs présentes dans `seen_urls.json`
- Tag `was_seen` ajouté à chaque article (utilisé par mailer pour le badge violet « Déjà envoyé » en mode TOUT_RENVOYER)
- `seen_urls.json` mis à jour (FIFO, max 10 000)

**Sortie** : dict `{meta, articles}` sérialisé en `data/scraper_output.json`. `_save_discovered_actors()` persiste les acteurs nouvellement vus.

### Étape 3 — Filtrage IA (`ai_filter.py:filter_articles_with_ai`)

**Découpage** :
- Articles découpés en batchs de `AI_BATCH_SIZE` (défaut 30)
- 700 articles → ~16 batchs

**Pour chaque batch** :
1. `_build_user_prompt` formate les articles avec ID, source, titre, résumé tronqué à 600 chars
2. `_call_gemini_with_retry(json_mode=True)` :
   - `response_mime_type="application/json"` (JSON pur, pas de markdown)
   - `max_output_tokens=32768` (marge confortable)
   - `temperature=0.1` (déterministe)
   - Retry exponentiel sur quota / service indispo
3. **Auto-split sur troncature** : si `MAX_TOKENS` ou JSON malformé → split batch en 2 et relance récursivement (cap depth=2)
4. `_parse_json_response` parse + filet de sécurité regex
5. `_force_company_scores` garantit score ≥ 4 si concurrent mentionné (vérification Python indépendante de l'IA)

**Nouvelle philosophie scoring (refonte 2026 — voir SCORING.md)** :

Le `SYSTEM_PROMPT` dans `_build_system_prompt()` n'évalue plus seulement « parle-t-il de PVD ? » mais **le potentiel d'INTÉGRATION cross-domaine** : si on prenait cette technologie et qu'on l'appliquait via PVD/ALD, est-ce que ça créerait quelque chose de nouveau ? Cette logique vaut autant pour les articles directement PVD que pour ceux d'autres domaines (photonique, MEMS, biomim, nanotech, IA process control) **transférables**.

Échelle 1-5 :
- **5** = transférable directement OU concurrent listé avec nouveau produit/brevet
- **4** = pont innovant (adaptation requise mais potentiel cross-domaine clair)
- **3** = lecture latérale (à garder en veille)
- **2** = marginal
- **1** = hors-sujet

La justification IA doit donner :
- L'**angle d'intégration** concret (« nanostructures plasmoniques → couleurs structurales décoratives via PVD »)
- Les **acteurs cités** (entreprises ou labos, même nouveaux pour nous)
- Le **domaine d'application** quand pertinent (horlogerie, médical, optique...)

**Synthèse finale** :
- Tri décroissant par score
- `_generate_executive_summary` fait un appel Gemini supplémentaire (mode texte) sur les 30 meilleurs → résumé exécutif global de 3 phrases

**Sortie** : `data/ai_filter_output.json`

### Étape 3.5 — Archive cumulative (`archive.py:update_archive`)

- Charge `data/articles_archive.json` (vide si absent)
- Fusionne les nouveaux articles filtrés en dédupliquant par URL canonique
- Conserve la dernière version (priorité au scoring le plus récent)
- Cap à 5 000 entrées (FIFO)
- Permet à `send_recap.py` d'envoyer un récap historique sans relancer le pipeline

### Étape 4 — Envoi email (`mailer.py:send_digest`)

- `MIMEMultipart("alternative")` HTML + texte brut
- HTML stylisé inline (badges colorés, icônes par source)
- Connexion SMTP STARTTLS sur `smtp.gmail.com:587`
- Multi-destinataires via `,`

**Sections du digest dans l'ordre** :
1. **Header + TLDR exécutif** (3 phrases sur les opportunités d'intégration repérées)
2. **Articles classés par score décroissant** (5★ → 1★ selon `MAIL_MIN_SCORE`)
3. **🔍 Acteurs découverts automatiquement** (NOUVEAU) : top 15 acteurs les plus fréquents (≥ 2 occurrences cumulées) avec compteur, sources, instructions pour valider via CLI
4. **⏪ Déjà vu la semaine passée** : top articles 4★/5★ du run précédent

**Composants visuels** :
- **Étoiles de score** : `_render_stars(score)` — 5 caractères ★, doré `#F59E0B` plein vs `#E5E7EB` vide
- **Badges score** : 5 hues distinctes (violet/vert/bleu/ambre/gris)
- **Badge `📌 Déjà envoyé`** (violet) : visible uniquement en mode TOUT_RENVOYER, sur les articles dont `was_seen=True`
- **Centrage** : double sécurité `align="center"` + `margin:0 auto` (Outlook)

---

## 3. Décisions techniques marquantes

### Bypass anti-bot via empreinte TLS (curl_cffi)

`curl_cffi` impersonne le handshake TLS de Chrome (rotation `chrome124`/`chrome131`/`chrome120`). Les WAF (Cloudflare, Akamai) ne distinguent pas du vrai Chrome. C'est ce qui permet de contourner MDPI sans captcha.

**Important** : si on ajoutait un proxy non-résidentiel intermédiaire, ça **casserait l'empreinte TLS** (le proxy renégocie avec sa propre signature, identifiable). C'est pourquoi seuls les **proxies résidentiels** (cf. `proxy_manager.py`) sont utilisables — leur trafic ressemble à un navigateur résidentiel ordinaire.

### Proxy résidentiel avec failover (proxy_manager.py)

Module `src/proxy_manager.py` gère un pool de 1 à 3 proxies chargés depuis `.env` (`RESIDENTIAL_PROXY_PRIMARY/BACKUP/TERTIARY`).

**Architecture** :
- **Health check au démarrage** : ping `httpbin.org/ip` via chaque proxy
- **Failover automatique** : si proxy actif échoue (407/timeout/proxy error), bascule vers le suivant sain
- **Auto-recovery** : un proxy marqué down est retesté après 60s. S'il revient, réactivation automatique
- **Seuil adaptatif** : 1 seul proxy → 5 échecs avant disable (mode mono-provider tolérant) ; 2-3 proxies → 3 échecs (rotation rapide)
- **Mode direct fallback** : si tous les proxies sont down, programme tombe en mode direct sans crasher
- **Geo-targeting** : variable `PROXY_COUNTRY` (CH/FR/DE/...) injecte automatiquement `-country-XX` dans le username (compatible avec IPRoyal, Decodo, Bright Data). **Note** : sur certains trials gratuits (Decodo 100 MB), le geo n'est pas inclus et provoque un 407 ; laisser `PROXY_COUNTRY=` vide.
- **Sécurité credentials** : jamais loggués en clair, masqués `***:***@host:port`
- **Tracker de bande passante** : chaque réponse HTTP via le proxy est comptabilisée (taille du body + overhead estimé 2.5 KB pour headers TX/RX). Cumulatif sur tous les runs, persisté dans `data/proxy_bandwidth.json`. Affichage en fin de run.
- **Circuit-breaker bandwidth** : variable `PROXY_BANDWIDTH_CAP_MB` (en MB). Si le cumul dépasse le cap, le proxy est désactivé pour le reste du run (mode direct forcé). Évite d'exploser un quota trial. Reset manuel via `python -m src.proxy_manager --reset`.

**Choix du port Decodo** :
- **Port 7000** (rotating) : IP différente à chaque requête → optimal pour anti-ban scraping (notre cas)
- **Port 10001-10010** (sticky) : IP fixe par session → utile pour cookies/login (pas notre cas)

Provider recommandé : **Decodo** (ex-Smartproxy) en mono-provider (~$8.50/GB, $7 minimum). Trial 100 MB disponible (sans geo-targeting).

Test : `python -m src.proxy_manager` → liste les proxies, fait health check, affiche statut + bandwidth report.

### Parallélisation inter-sources scientifiques

**Activé par défaut** (`SCRAPE_PARALLEL_SCIENTIFIC=true`). OpenAlex, Crossref, HAL et Semantic Scholar tournent dans 4 threads simultanés via `ThreadPoolExecutor`. Chaque thread :
- A sa **propre session curl_cffi** via `threading.local()` (pas de race condition sur cookies/headers)
- Crée sa propre session avec son impersonate TLS, son UA et son entrée proxy
- Suit les délais gaussiens intra-source (anti-bot par domaine préservé)

**Pourquoi pas de risque ban croisé** : les 4 sources sont sur des domaines disjoints (`api.openalex.org`, `api.crossref.org`, `api.archives-ouvertes.fr`, `api.semanticscholar.org`). Le proxy résidentiel rotating port 7000 donne une IP différente à chaque requête, donc même le proxy ne voit pas les 4 sources « surger » depuis la même IP.

**Thread-safety** : les dicts globaux (`_DOMAIN_COOLDOWN`, `_discovered_actors_session`, `_query_stats_session`) sont protégés par `threading.Lock` (`_globals_lock`).

**Sources NON-parallélisées** (gardées séquentielles) :
- RSS feeds (fast — pas nécessaire)
- arXiv search (pre-flight + circuit breaker spécifique)
- Google Patents (sensible anti-bot, pre-flight)
- Google News (TRÈS sensible anti-bot, jamais parallélisé)
- Tavily Web (rate-limit API)

**Gain mesuré** : test 4 sources × 1 query = 3.0s en parallèle vs ~10s+ en séquentiel. Sur run complet : ~80 min séquentiel → ~22 min parallèle (-60 min).

### Délais aléatoires gaussiens

Tous les `time.sleep` utilisent `random.gauss(μ, σ)` clampé à un minimum, pas `random.uniform`. La distribution normale ressemble plus au comportement humain (concentration autour de la moyenne, queues fines).

### Stratégie Google News : produit cartésien furtif

Tentative initiale d'OR-grouping abandonnée car Google News tronque silencieusement les requêtes OR longues. Choix actuel : produit cartésien complet avec délais et anti-détection comportementaux poussés (cf. section anti-bot ci-dessous).

### Mode JSON natif Gemini

`response_mime_type="application/json"` empêche Gemini de wrapper la réponse dans ` ```json ... ``` `. Critique pour la fiabilité du parsing.

### Chaîne de fallback Gemini (38+ modèles découverts dynamiquement)

Au premier appel `_init_client()`, le module appelle `genai.list_models()` et énumère **tous les modèles auxquels la clé a accès** (typiquement ~38 : Gemini 2.5 Flash/Lite/Pro, Gemini 2.0 Flash/Lite, Gemini 3 preview, Gemma 3 27B/12B/9B/4B/1B, Gemma 4 preview, etc.).

La liste est triée selon `_MODEL_PREFERENCE` :
1. **Tier 1** : `gemini-2.5-flash` → `flash-lite` → `pro`
2. **Tier 2** : `gemini-2.0-*`
3. **Tier 3** : `gemini-1.5-*`
4. **Tier 4** : `gemma-3-*`
5. **Tier 5** : tout le reste (preview, latest, etc.)

À chaque 429 quota épuisé, `_swap_to_fallback_model()` avance d'un cran. Pipeline ne tombe que si tous les 38+ modèles sont saturés.

**Caveats Gemma** : open-weights, ne supportent pas `response_mime_type=application/json`. Le code détecte le préfixe `gemma` (`_is_gemma`) pour désactiver `response_mime_type` (filet regex post-parsing rattrape les wrappers markdown) et préfixer le prompt système au prompt utilisateur.

### Anti-bot multi-couches (18 couches)

1. **TLS impersonation** (curl_cffi rotation chrome124/131/120)
2. **User-Agent rotatif** (5 UA Win/macOS/Linux × Chrome/Safari)
3. **Accept-Language varié** (3 variantes re-tirées par session)
4. **Client Hints Chrome cohérents** (Sec-Ch-Ua-* dynamique selon UA)
5. **Headers Sec-Fetch-* + Upgrade-Insecure-Requests + DNT**
6. **Locales Google News rotatives** (8 hl/gl/ceid)
7. **Header `Referer: https://www.google.com/`** sur GNews
8. **Shuffle aléatoire** des 589 requêtes GNews
9. **Délais inter-requêtes mixtes humains** (4 modes : fast 15% / normal 50% / slow 25% / very-slow 10%)
10. **Multiplicateur nuit ×1.8** (1h-6h heure locale)
11. **Pauses inter-sources** (3-9 min entre RSS, arXiv, OpenAlex, etc.)
12. **Rotation session GNews toutes les 30 req** (~10 identités au lieu d'une)
13. **Long break inter-rotation** (6-15 min)
14. **Pause circadienne** (après 6h : sleep 4-6h)
15. **Backoff progressif par domaine** (`_DOMAIN_COOLDOWN`)
16. **Circuit breaker GNews 3 strikes**
17. **Warm-up MDPI/ScienceDaily** + détection blocage proactive
18. **Pré-flight arXiv** + circuit breaker 3 strikes spécifique + UA identifiable + HTTPS

**Couche supplémentaire optionnelle** : proxy résidentiel (cf. proxy_manager.py) — élimine le risque IP-fixe, ~$5-15/mois pour fiabilité 99.5%.

### Vérification Python double-check des concurrents

Même si l'IA oublie de surclasser un article concurrent, `_force_company_scores` repasse en Python avec recherche substring case-insensitive. Filet indépendant du LLM.

### Mémoire des URLs vues + badge "Déjà envoyé"

Implémentée comme JSON FIFO de 10 000 URLs max. 3 modes utilisateur :
- **Filtrer** : exclude les URLs déjà vues (default, recommandé)
- **Tout renvoyer** : tous les articles passent, badge violet `📌 Déjà envoyé` sur ceux déjà vus auparavant
- **Reset** : vide `seen_urls.json` puis filtre normal

### Découverte d'acteurs cumulative

`data/discovered_actors.json` agrège inter-runs les noms d'entités vues dans les résultats Patents et OpenAlex. Trois champs clés persistés :
- `count` — somme cumulée des occurrences (incrémentée à chaque hit dans n'importe quel run)
- `appearances_runs` — nombre de runs **distincts** où l'acteur a été vu (incrémenté +1 par run, indépendant de `count`)
- `sources` — liste des sources qui ont mentionné l'acteur

Cette distinction `count` vs `appearances_runs` permet à l'auto-tuner d'imposer une condition de **stickiness** (un acteur doit revenir sur ≥2 runs distincts, pas juste spike dans un seul run).

### 🆕 Auto-tuning (`src/auto_tuner.py`) — boucle d'amélioration continue

À la fin de chaque `run_scraper()`, le module `auto_tuner.run_full_tuning()` exécute en séquence :

#### Étape A — Backup atomique de `targets.json`
Snapshot horodaté `data/backups/targets_YYYYMMDD_HHMMSS.json`, rotation des 10 derniers. Permet le rollback manuel si l'auto-tuning fait une erreur de jugement.

#### Étape B — Auto-promote v2 (`auto_promote_actors_v2`)
Critères stricts (TOUS requis) :
- `count >= AUTO_PROMOTE_MIN_COUNT` (défaut 5, vs 30 v1)
- `appearances_runs >= AUTO_PROMOTE_MIN_RUNS` (défaut 2) — **stickiness inter-runs**
- Non présent dans `targets.json` (companies/research_orgs)

Heuristique de classification enrichie (`_classify_actor`) :
- Mots-clés labo (`university`, `institut`, `cnrs`, `fraunhofer`, `hochschule`, `synchrotron`, `epfl`, `ethz`, `mit`, `caltech`, `kaist`, …) → `research_orgs`
- Suffixes entreprise (`gmbh`, `ag`, `inc`, `ltd`, `corp`, `technologies`, `systems`, `industries`, `coatings`, `materials`, …) → `companies`
- Fallback source : `openalex` seule → `research_orgs`, `patents` seule → `companies`

Cap `AUTO_PROMOTE_MAX_PER_RUN` (défaut 10).

#### Étape C — Auto-purge des cibles stériles (`auto_purge_sterile_targets`)
Une cible (`solo_keywords` / `cross_domain_topics` / `research_orgs`) est retirée SI ET SEULEMENT SI :
- `runs_total >= AUTO_PURGE_MIN_RUNS` (défaut 8) — assez d'historique
- `hits_total == 0` sur **toutes** les sources qui l'ont essayée — protection croisée
- `consecutive_zeros >= AUTO_PURGE_MIN_CONSECUTIVE_ZEROS` (défaut 8) sur au moins une source

Cap `AUTO_PURGE_MAX_PER_RUN` (défaut 5). Les `companies` et `keywords` sont **exemptés** (combinés en OR-groups GNews → attribution individuelle impossible).

Les cibles supprimées sont archivées dans `data/archived_targets.json` (cap 50 entrées) pour rollback manuel.

#### Étape D — Recalcul des tiers de requêtes (`compute_max_results`)
Au démarrage du run suivant, chaque appel à `fetch_openalex_works(query, max_results=25)` (et les 6 autres fetch_*) passe par `_tuned_max_results(query, "openalex", 25)` qui calcule :

| Tier | Critère | Multiplicateur | Effet |
|---|---|---|---|
| **Hot** | top `AUTO_EXPAND_HOT_PERCENTILE`% (10%) des `hits_total` | `× 1.5` | Plus de couverture sur les requêtes productives |
| **Cold** | `consecutive_zeros >= AUTO_EXPAND_COLD_CONSECUTIVE_ZEROS` (3) | `× 0.5` | Économie bandwidth/quotas sur les requêtes peu fertiles |
| **Standard** | reste | `× 1.0` | Inchangé |

Bornes appliquées : `max_results ∈ [5, 200]`. Cache process-wide via `_tier_cache`, invalidé par `invalidate_tier_cache()` à la fin du run.

#### Mode dry-run et désactivation
- `AUTO_TUNE_ENABLED=false` désactive tout l'auto-tuning (Étapes B, C, D)
- `AUTO_TUNE_DRY_RUN=true` log les actions mais ne touche jamais aux fichiers — utile pour audit avant activation
- Chaque sous-module a son propre switch : `AUTO_PURGE_ENABLED`, `AUTO_EXPAND_ENABLED`

Action 11 et 12 du menu CLI restent disponibles pour la revue/inspection manuelle.

---

## 4. Configuration

### `data/targets.json` (5 listes)

| Liste | Rôle | Cherchée dans |
|---|---|---|
| `companies` | Équipementiers industriels | GNews (× keywords) |
| `keywords` | Mots-clés techniques | GNews (× companies) + 7 sources scientifiques |
| `solo_keywords` | Phrases multi-mots spécifiques | GNews + 7 sources scientifiques |
| `research_orgs` | Labos / universités qui publient | 7 sources scientifiques (PAS GNews) |
| `cross_domain_topics` | Thèmes transversaux (photonique, MEMS, biomim...) | 7 sources scientifiques (PAS GNews) |

### `src/config.py`

| Var | Défaut | Effet |
|---|---|---|
| `MAX_ARTICLES_PER_SOURCE` | 50 | Cap par flux RSS |
| `RECENT_DAYS_LIMIT` | 90 | Fenêtre de fraîcheur |
| `USE_MEMORY` | True (override par UI) | Filtre URLs déjà vues |
| `SCRAPE_LIMIT_MONTH` | True | Active le filtre fraîcheur |

### `.env`

| Var | Défaut | Effet |
|---|---|---|
| `GEMINI_API_KEY` | — | **Obligatoire** |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Modèle principal de la cascade |
| `AI_BATCH_SIZE` | 30 | Articles par appel Gemini |
| `GMAIL_USER` | — | Compte SMTP expéditeur (**obligatoire**) |
| `GMAIL_PASSWORD` | — | App password 16 chars (**obligatoire**) |
| `MAIL_RECIPIENT` | `GMAIL_USER` | Liste virgule-séparée |
| `MAIL_MIN_SCORE` | 2 | Score min affiché |
| `TAVILY_API_KEY` | — | Optionnelle (1000/mois free) |
| `SEMANTIC_SCHOLAR_API_KEY` | — | Optionnelle (rate-limit étendu) |
| **`RESIDENTIAL_PROXY_PRIMARY`** | — | **Optionnelle** Proxy résidentiel principal |
| **`RESIDENTIAL_PROXY_BACKUP`** | — | **Optionnelle** Proxy backup |
| **`RESIDENTIAL_PROXY_TERTIARY`** | — | **Optionnelle** 3e proxy (rare) |
| `AUTO_TUNE_ENABLED` | `true` | Master switch de l'auto-tuner (promote v2 + purge + tiers) |
| `AUTO_TUNE_DRY_RUN` | `false` | `true` = log les actions sans toucher au disque |
| `AUTO_PROMOTE_MIN_COUNT` | 5 | Seuil count cumulé pour auto-promotion (v2, abaissé de 30) |
| `AUTO_PROMOTE_MIN_RUNS` | 2 | Stickiness : acteur doit apparaître sur ≥ N runs distincts |
| `AUTO_PROMOTE_MAX_PER_RUN` | 10 | Cap promotions par run |
| `AUTO_PURGE_ENABLED` | `true` | Active la suppression auto des cibles stériles |
| `AUTO_PURGE_MIN_RUNS` | 8 | Historique minimal avant éligibilité purge |
| `AUTO_PURGE_MIN_CONSECUTIVE_ZEROS` | 8 | Runs consécutifs à 0 requis pour purge |
| `AUTO_PURGE_MAX_PER_RUN` | 5 | Cap suppressions par run |
| `AUTO_EXPAND_ENABLED` | `true` | Active les tiers Hot/Standard/Cold sur `max_results` |
| `AUTO_EXPAND_HOT_MULTIPLIER` | 1.5 | Multiplicateur sur le tier Hot |
| `AUTO_EXPAND_COLD_MULTIPLIER` | 0.5 | Multiplicateur sur le tier Cold (économie quotas) |
| `AUTO_EXPAND_HOT_PERCENTILE` | 10 | Top N% des hits_total → Hot |
| `AUTO_EXPAND_COLD_CONSECUTIVE_ZEROS` | 3 | Seuil consécutifs à 0 pour passer Cold |
| **`PROXY_COUNTRY`** | `CH` | Code pays geo-targeting (laisser vide en trial Decodo gratuit) |
| **`PROXY_BANDWIDTH_CAP_MB`** | 0 | Plafond bande passante en MB (0 = pas de cap). Recommandé 80 pour trial 100 MB |
| **`SCRAPE_PARALLEL_SCIENTIFIC`** | `true` | Lance OpenAlex/Crossref/HAL/S2/EuropePMC/BASE en parallèle. Gain ~60 min/run |
| **`SCRAPE_PARALLEL_MAX_WORKERS`** | 5 | Nb max de threads workers (1 par source) |
| `DEDUP_ENABLED` | `true` | Déduplication sémantique TF-IDF avant Gemini |
| `DEDUP_THRESHOLD` | `0.85` | Similarité cosine min pour considérer comme doublon |
| `PRERANK_ENABLED` | `true` | Pre-ranking BM25 avant Gemini (-15-30% tokens) |
| `PRERANK_KEEP_TOP_FRACTION` | `0.80` | Fraction minimale du flux préservée |
| `PRERANK_MIN_BM25_NORMALIZED` | `0.05` | Seuil normalisé pour couper la queue |
| `MMR_ENABLED` | `true` | Diversification MMR du top |
| `MMR_LAMBDA` | `0.7` | 1.0 = pure relevance, 0.0 = pure diversité |
| `MMR_TOP_K` | `60` | Nombre d'articles affectés par MMR |
| `MULTI_JUDGE_ENABLED` | `true` | Re-scoring 2e modèle sur top-tier/low-confidence |
| `MULTI_JUDGE_TRIGGER_SCORE` | `4` | Score ≥ déclenche multi-judge |
| `MULTI_JUDGE_TRIGGER_CONFIDENCE` | `0.5` | Confidence < déclenche multi-judge |
| `MULTI_JUDGE_MAX_CANDIDATES` | `50` | Cap articles re-scorés/run |
| `CALIBRATION_TRACK_ENABLED` | `true` | Track distribution scores inter-runs (drift) |
| `FEEDBACK_ENABLED` | `true` | Boutons 👍/👎 dans email + IMAP poll |
| `FEEDBACK_POLL_IMAP` | `true` | Lit l'inbox Gmail au démarrage |
| `FEEDBACK_FEW_SHOT_COUNT` | `3` | Nb d'exemples 👍/👎 injectés dans le prompt |
| `HEATMAP_ENABLED` | `true` | Section heatmap concurrentielle dans le digest |
| `HEATMAP_ANOMALY_RATIO` | `2.0` | Ratio current/avg pour déclencher alerte |
| `HEATMAP_MIN_MENTIONS` | `2` | Mentions absolues minimales pour anomalie |
| `EMERGENCE_ENABLED` | `true` | Détection auto de nouveaux topics émergents |
| `EMERGENCE_MIN_SCORE` | `4` | Score min des articles utilisés pour TF-IDF n-grams |
| `EMERGENCE_TOP_N` | `10` | Nombre max de topics suggérés/run |
| `TRANSLATION_ENABLED` | `false` | Traduction EN→FR des résumés top (coût Gemini +1 req) |
| `TRANSLATION_TARGET_LANG` | `fr` | Langue cible (fr, de, it...) |

---

## 5. Gestion d'erreurs et résilience

| Endroit | Comportement |
|---|---|
| `main.py:try/except global` | Toute exception non gérée → `send_error_email()` |
| `scraper.py:fetch_*` | Catch `RequestsError`/`OSError` → `[]`, pipeline continue |
| `scraper.py:fetch_arxiv_search` | Retourne `None` sur 429/403 → caller compte les blocages → circuit breaker |
| `scraper.py:fetch_google_patents` | Robuste : try/except sur format JSON, cooldown 180s sur 403/429, retourne `[]` |
| `scraper.py:run_scraper` | Checkpoint tous les 5 appels GNews + pré-flight arXiv |
| `proxy_manager.py` | Pool failover + auto-recovery + fallback mode direct si tous down |
| `ai_filter.py:_call_gemini_with_retry` | Retry exponentiel + cascade 38+ modèles |
| `scraper.py:_record_block` | Backoff progressif par domaine |
| `ai_filter.py:_process_batch` | Auto-split sur troncature (depth max 2) |
| `main.py:Étape 3` | Catch `GeminiUnavailableError` → email envoyé sans IA |
| `main.py:Étape 4` | Catch erreur mailer → log uniquement |

**Garantie** : aucun chemin d'erreur ne fait crasher l'orchestrateur sans envoyer une notification.

---

## 6. Quotas et coûts à surveiller

| Service | Quota free tier | Conso/run typique |
|---|---|---|
| Gemini 2.5 Flash | 250 req/jour | ~16 req (15 batchs + 1 résumé exé) |
| Cascade Gemini fallback | ~38 modèles avec quotas indépendants | activée si saturation |
| OpenAlex | 100k/jour, sans clé | ~112 |
| arXiv Search | aucun strict (politely 3s+) | ~111 |
| Crossref | aucun strict avec `mailto=` | ~112 |
| HAL | aucun strict | ~112 |
| Semantic Scholar | 100/5min sans clé | ~111 |
| Tavily | **1000/mois free** ⚠️ | ~110/run × 5 runs/mois = 550/mois |
| Google Patents | aucun officiel | ~112 |
| Google News RSS | aucun officiel, **soft limit ~500/run** | ~589 (au seuil tendu) |
| Gmail SMTP | 500 envois/jour | 1 |

À fréquence 1 run/semaine (~5/mois), tous les quotas sont sous limites.

---

## 7. Extensions naturelles

- **Section « diff vs semaine dernière »** : `previous_ai_output.json` est conservé, le mailer affiche les top articles 4★/5★ du run précédent dans une section dédiée
- **Découverte d'acteurs étendue à HAL/Crossref/SS** : actuellement Patents+OpenAlex seulement. Ajouter l'extraction depuis les autres sources scientifiques pour plus de coverage
- **Cache Anthropic-style sur Gemini** : Google Context Caching API (équivalent du prompt caching Anthropic) pour réduire les coûts/latence sur les SYSTEM_PROMPT longs
- **Notification Slack/Teams** en plus de l'email
- **Dashboard web** local avec Streamlit pour explorer l'historique et les acteurs découverts

---

## 8. Arbre de fichiers

```
veille_tech/
├── main.py                          # Orchestrateur principal (run hebdomadaire)
├── resume_pipeline.py               # Reprise après scraping (IA + archive + email)
├── send_recap.py                    # Rattrapage : envoie l'archive complète
├── configurer.py                    # Config interactive .env (clés API + proxy)
├── requirements.txt                 # Dépendances pip
├── .env                             # Secrets (NON commité)
├── .env.example                     # Template documenté
├── CLAUDE.md                        # Directives pour l'agent IA
├── MANUEL.md                        # Guide utilisateur novice
├── ARCHITECTURE.md                  # Ce document
├── SCORING.md                       # Doc public-facing du scoring IA
├── COMMENT_CA_MARCHE.md             # Vue d'ensemble vulgarisée
├── README.md                        # Intro courte
├── check.py                         # Utilitaire : liste les modèles Gemini autorisés
│
├── src/
│   ├── scraper.py                   # Phase 2 — collecte 8 sources + actor extraction
│   ├── ai_filter.py                 # Phase 3 — filtrage Gemini cross-domaine
│   ├── archive.py                   # Phase 3.5 — archive cumulative
│   ├── mailer.py                    # Phase 4 — envoi email HTML (+ section acteurs)
│   ├── config.py                    # Constantes + chargement targets.json (5 listes)
│   └── proxy_manager.py             # Pool proxies résidentiels + failover
│
└── data/
    ├── targets.json                 # 5 listes (companies, keywords, solo, orgs, cross)
    ├── seen_urls.json               # Mémoire FIFO des URLs déjà envoyées
    ├── scraper_output.json          # Sortie phase 2
    ├── ai_filter_output.json        # Sortie phase 3
    ├── previous_ai_output.json      # Snapshot run précédent (pour section "Déjà vu")
    ├── articles_archive.json        # Archive cumulative (pour rattrapage)
    ├── scraper_checkpoint.json      # Checkpoint partiel scraping
    ├── discovered_actors.json       # Acteurs découverts auto (Patents/OpenAlex)
    └── proxy_bandwidth.json         # Cumul MB consommées via proxy (cap automatique)
```
