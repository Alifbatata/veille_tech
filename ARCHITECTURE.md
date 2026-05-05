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
        ┌─────────────────────┼──────────────┬──────────┐
        ▼                     ▼              ▼          ▼
  ┌────────────┐  ┌─────────────┐  ┌────────────┐  ┌──────────┐
  │ scraper.py │→ │ ai_filter.py │→ │ archive.py │→ │ mailer.py │
  └─────┬──────┘  └──────┬──────┘  └──────┬─────┘  └────┬─────┘
             │                │              │
             ▼                ▼              ▼
       ┌──────────┐    ┌──────────┐   ┌────────────┐
       │ curl_cffi│    │  Gemini  │   │  SMTP TLS  │
       │feedparser│    │ Flash 2.5│   │ smtp.gmail │
       └──────────┘    └──────────┘   └────────────┘
             │                │              │
             ▼                ▼              ▼
       Sources web     google.generativeai  Inbox utilisateur

  Données persistées dans data/ :
    targets.json              (input — concurrents + mots-clés)
    seen_urls.json            (mémoire des URLs déjà envoyées)
    scraper_output.json       (sortie étape 2)
    ai_filter_output.json     (sortie étape 3)
    previous_ai_output.json   (snapshot du run précédent)
    scraper_checkpoint.json   (checkpoint partiel pendant le scraping)
    articles_archive.json     (archive cumulative pour rattrapage)
```

**Mode rattrapage** : `send_recap.py` est un point d'entrée alternatif qui lit `articles_archive.json` et expédie un récapitulatif sans relancer scraping/IA.

---

## 2. Pipeline détaillé

### Étape 1 — Rotation de l'historique (`main.py:108-125`)

- Si `data/ai_filter_output.json` existe (run précédent), il est copié vers `data/previous_ai_output.json`.
- Permet au mailer d'afficher éventuellement une section « la semaine dernière ».
- Échec non-fatal : le run continue même si la copie échoue.

### Étape 2 — Scraping (`scraper.py:run_scraper`)

**Phase RSS** :
- Itère sur `SOURCES_RSS` (5 flux : ArXiv ×2, MDPI, IEEE, ScienceDaily)
- Pour MDPI et ScienceDaily : pré-warm-up de la session (visite de la racine du domaine pour récupérer les cookies Cloudflare)
- Récupère le XML via `curl_cffi` impersonant Chrome 124
- Parsing avec `feedparser`
- Filtre par fraîcheur (`_is_recent`, fenêtre `RECENT_DAYS_LIMIT` jours)
- Cap à `MAX_ARTICLES_PER_SOURCE` articles par flux
- Micro-pause gaussienne `gauss(1.0, 0.3)s` entre flux

**Phase Google News (mode weekend furtif)** :
- `build_gnews_queries()` génère **1 requête unitaire par couple (entreprise, mot-clé)** :
  ```
  "Oerlikon" "PVD"
  ```
  WHY le produit cartésien et pas l'OR-grouping : Google News tronque silencieusement les requêtes OR longues et renvoie des résultats génériques. Une requête courte et précise par couple garantit la pertinence, au prix d'un volume plus élevé (compensé par les délais furtifs).
- Avec 21 entreprises × 14 mots-clés → **294 requêtes** par run.
- **Délais inter-requêtes humains mixtes** (mode weekend, moyenne ~141s, médiane ~104s) :
  - 15% rapides (20-50s) — humain qui scrolle un titre
  - 50% normaux (60-120s) — humain qui lit un résumé
  - 25% lents (120-300s) — humain qui lit un article complet
  - 10% très lents (5-9 min) — pause téléphone / café
- **Rotation de session toutes les 30 requêtes** + long break humain de 6-15 min (≈ 10 identités présentées à Google plutôt qu'une seule).
- **Pause circadienne** : après 6h de run continu, sleep aléatoire de 4-6h (simulation « humain qui dort » — Google ne voit jamais 50 recherches sur 12h non-stop).
- **Locales rotatives** : 8 variantes hl/gl/ceid (en-US, fr-FR, de-DE, en-GB, fr-CH, en-CA, it-IT, es-ES) tirées au hasard à chaque requête.
- **Header `Referer: https://www.google.com/`** ajouté à chaque appel (un humain n'arrive jamais sur news.google.com via une URL nue).
- **Détection précoce de blocage** : si la réponse ne commence pas par `<?xml` → traitement comme un blocage anti-bot.
- **Circuit breaker à 3 strikes consécutifs** : un blocage isolé déclenche un long break (~25 min) + nouvelle identité, puis on retente. Trois blocages d'affilée → abandon de la source GNews seulement (les 7 autres sources sont préservées et l'email part avec ce qu'on a).
- Checkpoint `data/scraper_checkpoint.json` tous les 5 appels et avant chaque pause longue (circadienne, recovery).
- **Budget temps total estimé** : ~18-22h pour les 294 requêtes (compatible avec un run weekend non surveillé).

**Post-traitement** :
- Dédoublonnage par URL (clé : `link.strip().rstrip("/")`)
- Si `USE_MEMORY=True` : exclusion des URLs présentes dans `data/seen_urls.json`
- Mise à jour de `seen_urls.json` (FIFO, max 10 000 entrées)

**Sortie** : dict `{meta, articles}` sérialisé en `data/scraper_output.json`

### Étape 3 — Filtrage IA (`ai_filter.py:filter_articles_with_ai`)

**Découpage** :
- Articles découpés en batchs de `AI_BATCH_SIZE` (défaut 20)
- 165 articles → 9 batchs

**Pour chaque batch** :
1. `_build_user_prompt` formate les articles avec ID, source, titre, résumé tronqué à 400 chars
2. `_call_gemini_with_retry(json_mode=True)` appelle Gemini avec :
   - `response_mime_type="application/json"` (garantit JSON pur, pas de fences markdown)
   - `max_output_tokens=8192`
   - `temperature=0.1` (déterministe)
   - Retry exponentiel sur `ResourceExhausted` / `ServiceUnavailable`
3. `_parse_json_response` parse en JSON direct + filet de sécurité regex
4. `_force_company_scores` garantit score ≥ 4 si un concurrent est mentionné dans le titre/résumé (vérification Python indépendante de l'IA)

**Synthèse finale** :
- Tri décroissant par score
- `_generate_executive_summary` fait un appel Gemini supplémentaire (mode texte) sur les 30 meilleurs articles → résumé exécutif global de 3 phrases

**Sortie** : dict `{meta: {tldr, retained_count, ...}, articles}` sérialisé en `data/ai_filter_output.json`

### Étape 3.5 — Archive cumulative (`archive.py:update_archive`)

- Charge `data/articles_archive.json` (vide si absent)
- Fusionne les nouveaux articles filtrés en dédupliquant par URL canonique (trim + rstrip "/")
- Conserve la dernière version (priorité au scoring le plus récent en cas de conflit)
- Cap à 5 000 entrées (FIFO par date de collecte décroissante)
- Permet à `send_recap.py` d'envoyer un récapitulatif historique sans relancer le pipeline

### Étape 4 — Envoi email (`mailer.py:send_digest`)

- `MIMEMultipart("alternative")` avec versions HTML + texte brut
- HTML stylisé inline (badges de score colorés, icônes par source/catégorie)
- Connexion SMTP STARTTLS sur `smtp.gmail.com:587`
- Authentification via mot de passe d'application
- Multi-destinataires supportés (split sur `,`)
- **Étoiles de score** : composant `_render_stars(score)` génère 5 caractères ★, dichotomie pleine `#F59E0B` (doré) vs vide `#E5E7EB` (gris clair) — standard UX Amazon/Trustpilot
- **Couleurs de badge** : 5 hues distinctes (violet/vert/bleu/ambre/gris) au lieu d'un dégradé monochromatique
- **Centrage** : double sécurité `align="center"` + `margin:0 auto` pour compatibilité Outlook

---

## 3. Décisions techniques marquantes

### Bypass anti-bot via empreinte TLS

`curl_cffi` impersonne le handshake TLS de Chrome 124 (suite de chiffres, courbes elliptiques, ALPN, etc.). Cloudflare et autres WAF ne peuvent pas distinguer notre client d'un vrai Chrome. C'est ce qui permet de contourner la protection MDPI sans captcha.

**Important** : ajouter un proxy intermédiaire **casse cette empreinte** (le proxy renégocie le TLS avec sa propre signature). C'est pourquoi nous n'utilisons pas de proxy.

### Délais aléatoires gaussiens

Tous les `time.sleep` utilisent `random.gauss(μ, σ)` clampé à un minimum, pas `random.uniform`. La distribution normale ressemble plus au comportement humain (concentration autour de la moyenne, queues fines) qu'une distribution uniforme.

### Stratégie Google News : produit cartésien furtif (au lieu de OR-grouping)

Tentative initiale d'OR-grouping (`("A" OR "B" OR ... OR "U") "kw"` → 14 requêtes au lieu de 294) abandonnée : **Google News tronque silencieusement les requêtes OR longues** et renvoie systématiquement les mêmes résultats génériques (souvent les plus populaires sur le mot-clé seul). On perdait 80% de la couverture entreprise.

Choix actuel : produit cartésien complet (294 req) avec délais et anti-détection comportementaux poussés (cf. section anti-bot). Le coût en temps est compensé par la qualité de la couverture — chaque couple (entreprise, mot-clé) reçoit un classement Google News dédié.

### Mode JSON natif Gemini

`response_mime_type="application/json"` empêche Gemini de wrapper la réponse dans ` ```json ... ``` `. Critique pour la fiabilité du parsing — sans ça, ~70% des batches échouaient.

### Chaîne de fallback Gemini en cascade — découverte dynamique

Le free tier Gemini réserve des surprises : `gemini-2.0-flash` et `gemini-2.0-flash-lite` ont parfois un `limit:0` (inaccessibles sans facturation), tandis que `gemini-2.5-flash`, `gemini-2.5-flash-lite` et la famille `gemma-3-*` ont des quotas free réels et indépendants.

**Découverte dynamique au boot** (`ai_filter.py:_discover_available_models`) : au premier appel `_init_client()`, le module appelle `genai.list_models()` et énumère **tous les modèles auxquels la clé API a accès** (typiquement ~38 : Gemini 2.5 Flash/Lite/Pro, Gemini 2.0 Flash/Lite, Gemini 3 preview, Gemini 3.1 preview, Gemma 3 (1B/4B/12B/27B), Gemma 4 preview, gemini-flash-latest, etc.).

La liste est triée selon une table de préférence projet (`_MODEL_PREFERENCE`) :
1. **Tier 1 (poids 10-30)** : `gemini-2.5-flash` → `flash-lite` → `pro`
2. **Tier 2 (poids 40-55)** : `gemini-2.0-flash` → `flash-lite` → `pro` → `flash-thinking`
3. **Tier 3 (poids 60-70)** : `gemini-1.5-pro` → `1.5-flash` → `1.5-flash-8b`
4. **Tier 4 (poids 80-98)** : `gemma-3-27b-it` → `12b` → `9b` → `4b` → `1b` → `gemma-2-*`
5. **Tier 5 (poids 100, queue)** : tout le reste découvert (preview, latest, robotics, lyria, deep-research, etc.) — essayé en dernier recours

À chaque 429 quota épuisé sur le modèle actif, `_swap_to_fallback_model()` avance d'un cran dans la chaîne et redémarre un cycle complet de retries. La chaîne effective contient typiquement **38+ niveaux de fallback** au lieu des 4 fixés statiquement auparavant. Le pipeline ne tombe en panne qu'après épuisement TOTAL de tous les modèles accessibles à la clé.

Si `list_models()` échoue (réseau coupé, clé restreinte), on retombe sur la chaîne statique de secours `[gemini-2.5-flash-lite, gemma-3-27b-it, gemma-3-12b-it]`.

**Caveats Gemma** : modèles open-weights, ne supportent pas `response_mime_type=application/json` ni `system_instruction` au constructeur. Le code détecte le préfixe `gemma` (`_is_gemma`) pour :
  - désactiver `response_mime_type` (filet regex post-parsing dans `_parse_json_response` rattrape les wrappers markdown)
  - préfixer le prompt système au prompt utilisateur (sauf appels `prefix_system_prompt=False` — voir ci-dessous)

**Bug résumé exécutif corrigé** : avant, `_generate_executive_summary()` recevait sur Gemma le `SYSTEM_PROMPT` (qui exige `{tldr, retained: [...]}`) ET le prompt utilisateur (qui demande un texte de 3 phrases). Gemma satisfaisait les deux en concaténant texte propre + dump JSON, polluant le digest. Fix : nouveau paramètre `prefix_system_prompt=False` pour les appels texte libre + `_sanitize_executive_summary()` qui coupe avant toute fence ` ``` ` ou objet `{"tldr": ...}` résiduel et retire les introductions polluantes (« Voici un résumé : »).

### Sources de collecte multi-canal

Le pipeline interroge cinq familles de sources, dont trois activées par défaut :

| Source | Activation | Force |
|---|---|---|
| **RSS (5 flux)** | par défaut | Captures rapides des revues scientifiques (MDPI, ArXiv flux generic, IEEE, ScienceDaily) |
| **arXiv Search API** | par défaut | Recherche par mot-clé sur **tout** l'index arXiv (pas seulement les ~50 derniers du flux) — 5 requêtes thématiques |
| **OpenAlex** | par défaut | Base de **250M+** œuvres scientifiques structurées (DOI, concepts, abstracts) — 6 requêtes. Gratuit, sans clé |
| **Crossref** | par défaut | ~140M œuvres avec DOI — 5 requêtes. Gratuit, sans clé. Le « pool polite » via paramètre `mailto=` donne une priorité d'accès. Note : Crossref a déprécié `from-pub-date` au profit de `filter=from-pub-date:YYYY-MM-DD` (corrigé) |
| **HAL (CNRS)** | par défaut | Archive ouverte française, particulièrement forte sur **CEA-Leti, CNRS, ONERA** — 5 requêtes. Gratuit, sans clé. Bilingue FR/EN |
| **Semantic Scholar** | par défaut | ~200M papers enrichis IA — 4 requêtes. Gratuit (rate-limit 1 req/s avec ou sans clé, mais clé = 1 req/s **garanti**, sans clé = bannissement IP fréquent). Fenêtre date élargie à 365 jours côté client (SS classe par pertinence et non par date — sans cet élargissement, le filtre 90j coupait tout) |
| **Google News RSS** | par défaut | Couvre les communiqués industriels et la presse spécialisée |
| **Tavily Web** | activé `include_web=True` (graceful sans clé) | Recherche web généraliste pilotée par LLM ; nécessite `TAVILY_API_KEY` (1000 req/mois free) pour produire des résultats |

**Logique anti-doublons** : tous les flux convergent dans `all_articles`, puis `_normalize_url()` strip les paramètres de tracking (UTM, fbclid, etc.), le fragment et le slash final, lowercase le tout. Une page citée par 3 sources différentes avec 3 URLs taggées différemment est dédupliquée à 1 seul article.

Pour pousser plus loin, **Crossref**, **HAL** (CNRS open archive) et **Semantic Scholar** seraient des extensions complémentaires propres à ajouter (mêmes signatures que `fetch_openalex_works`).

### Anti-bot multi-couches (17 couches)

1. **TLS impersonation** : `curl_cffi` reproduit l'empreinte TLS de Chrome (rotation entre `chrome124`/`chrome131`/`chrome120` à la création de session). Cloudflare et autres WAF voient un vrai navigateur.
2. **User-Agent rotatif** : 5 UA différents (Windows/macOS/Linux × 2 versions Chrome + 1 Safari) re-tirés à chaque obtention de session.
3. **Accept-Language varié** : 3 variantes, re-tirées à chaque obtention de session.
4. **Client Hints Chrome cohérents** (`Sec-Ch-Ua`, `Sec-Ch-Ua-Mobile`, `Sec-Ch-Ua-Platform`) : générés dynamiquement à partir du UA tiré (Windows/macOS/Linux + version majeure Chrome). Leur absence est un signal anti-bot fort utilisé par Akamai et Cloudflare modernes.
5. **Headers Sec-Fetch-* + Upgrade-Insecure-Requests + DNT** : envoyés par tous les navigateurs modernes pour signaler le contexte de navigation. Leur absence indique un client non-navigateur.
6. **Locales Google News rotatives** : 8 couples (hl/gl/ceid) — en-US, fr-FR, de-DE, en-GB, fr-CH, en-CA, it-IT, es-ES — tirés au hasard à chaque requête GNews. Évite le pattern « toujours en-US ».
7. **Header `Referer: https://www.google.com/`** sur les appels GNews : un humain arrive sur news.google.com depuis google.com ou via favori, jamais via une URL nue avec query string.
8. **Shuffle aléatoire de l'ordre des 294 requêtes GNews** (`random.shuffle(queries)`) : un humain ne fait pas Oerlikon×14 keywords puis Bodycote×14 keywords de manière alphabétique. Mélanger casse ce pattern facilement détectable.
9. **Délais inter-requêtes mixtes humains** : `_humanlike_inter_request_delay()` mélange 4 modes (fast 15% / normal 50% / slow 25% / very-slow 10%) — moyenne ~141s, médiane ~104s, plage [20s, 700s]. Indistinguable d'un humain qui lit/scrolle.
10. **Multiplicateur nuit ×1.8** sur les délais GNews entre 1h et 6h heure locale (`_is_night_time()`). Un humain insomniaque consulte plus lentement que dans la journée. Les délais nocturnes peuvent dépasser 12 minutes/requête.
11. **Pauses inter-sources** : 3-9 min entre RSS, arXiv, OpenAlex, Crossref, HAL, Semantic Scholar, Tavily, GoogleNews. Imite un changement d'activité humain.
12. **Rotation de session GNews toutes les 30 requêtes** : `_reset_session()` détruit la session (cookies + impersonate + UA + Client Hints) et en recrée une avec une nouvelle identité. ~10 identités sont présentées à Google sur un run au lieu d'une seule.
13. **Long break inter-rotation** : 6-15 min de sommeil entre deux groupes de 30 requêtes (humain qui change d'onglet, lit autre chose).
14. **Pause circadienne** : après 6h de run GNews continu, sleep aléatoire de 4-6h. Aucun humain ne fait des recherches non-stop pendant 12h+ ; cette pause casse définitivement le pattern « bot 24/7 ».
15. **Backoff progressif par domaine** (`_DOMAIN_COOLDOWN`) : un 403/429 sur un domaine déclenche une pénalité (60-180s selon le code statut), doublée à chaque récidive. Les autres domaines ne sont pas affectés.
16. **Circuit breaker GNews à 3 strikes consécutifs** : 1 blocage → long break ~25 min + nouvelle identité, on retente ; 3 blocages d'affilée → abandon GNews pour le run, les 7 autres sources continuent. Évite le bannissement IP par retry agressif.
17. **Warm-up MDPI/ScienceDaily** + **détection de blocage proactive** : visite de la racine du domaine avant les pages spécifiques (cookies Cloudflare validés). Si la réponse Google News n'est pas du XML, ou si MDPI renvoie du HTML au lieu du flux RSS, on enregistre le blocage **avant** de tenter à nouveau.

**Important** : ne **jamais** ajouter de proxy intermédiaire, cela casse l'empreinte TLS impersonate (le proxy renégocie le TLS avec sa propre signature, identifiable instantanément).

**Risque résiduel honnête** : ces 14 couches abaissent fortement la probabilité de détection mais ne la suppriment pas — l'IP reste fixe (sans proxy résidentiel payant). Le **circuit breaker** est le filet de sécurité ultime : même en cas de blocage, le pipeline n'entre jamais en boucle infinie et ne risque pas de bannissement IP permanent.

### Vérification Python double-check des concurrents

Même si l'IA oublie de surclasser un article concurrent, `_force_company_scores` repasse en Python avec une recherche substring case-insensitive sur le titre + résumé. Filet de sécurité indépendant de la qualité du LLM.

### Mémoire des URLs vues

Implémentée comme un fichier JSON FIFO de 10 000 URLs max. Permet d'éviter d'envoyer 50 fois le même article si publié sur plusieurs sources. Désactivable via `USE_MEMORY=False`.

---

## 4. Configuration

| Source | Variable | Défaut | Effet |
|---|---|---|---|
| `src/config.py` | `MAX_ARTICLES_PER_SOURCE` | 50 | Cap par flux RSS |
| `src/config.py` | `RECENT_DAYS_LIMIT` | 90 | Fenêtre de fraîcheur |
| `src/config.py` | `USE_MEMORY` | True | Filtre les URLs déjà vues |
| `src/config.py` | `SCRAPE_LIMIT_MONTH` | True | Active le filtre fraîcheur |
| `data/targets.json` | `companies` | 21 entreprises | Liste des concurrents surveillés |
| `data/targets.json` | `keywords` | 14 mots-clés | Termes scientifiques |
| `.env` | `GEMINI_API_KEY` | — | Obligatoire |
| `.env` | `GEMINI_MODEL` | `gemini-2.5-flash` | Modèle Gemini |
| `.env` | `AI_BATCH_SIZE` | 20 | Articles par appel Gemini |
| `.env` | `GMAIL_USER` | — | Compte SMTP expéditeur |
| `.env` | `GMAIL_PASSWORD` | — | App password 16 chars |
| `.env` | `MAIL_RECIPIENT` | `GMAIL_USER` | Liste virgule-séparée |
| `.env` | `MAIL_MIN_SCORE` | 2 | Score min affiché dans le digest |
| `.env` | `TAVILY_API_KEY` | — | Optionnelle. Active la recherche Web élargie via Tavily |
| `.env` | `SEMANTIC_SCHOLAR_API_KEY` | — | Optionnelle. Augmente le rate-limit SS de 1 r/s à 100 r/s |

---

## 5. Gestion d'erreurs et résilience

| Endroit | Comportement |
|---|---|
| `main.py:try/except global` | Toute exception non gérée → `send_error_email()` envoie un mail d'alerte |
| `scraper.py:fetch_rss_feed` | Catch `RequestsError`, `OSError` → retourne `[]`, le run continue |
| `scraper.py:fetch_google_news` | Catch idem + détection HTML/non-XML → `None` → abort de la phase GNews mais le pipeline continue |
| `scraper.py:run_scraper` | Checkpoint `data/scraper_checkpoint.json` tous les 5 appels GNews |
| `ai_filter.py:_call_gemini_with_retry` | Retry exponentiel sur quota / service indispo (3 tentatives, backoff 2^n) puis cascade dans `FALLBACK_CHAIN`. Chaîne actuelle (4 niveaux) : `gemini-2.5-flash` (principal) → `gemini-2.5-flash-lite` (fallback #1, quotas free séparés) → `gemma-3-27b-it` (fallback #2, open-weights) → `gemma-3-12b-it` (fallback #3, open-weights plus léger). Pour Gemma : `response_mime_type=application/json` désactivé automatiquement, prompt système préfixé manuellement. |
| `scraper.py:_record_block` + `_respect_domain_cooldown` | Backoff progressif par domaine sur 403/429 : 1er blocage = pénalité base, 2e = ×2, 3e = ×4… Empêche de marteler un site qui vient de nous bloquer. Persiste pour la durée du process. |
| `scraper.py:fetch_openalex_works` / `fetch_arxiv_search` | Catch `RequestsError`/`OSError` → `[]`, le pipeline continue sans ces sources. |
| `scraper.py:fetch_broad_web_search` | Tavily désactivé (retour `[]`) si `TAVILY_API_KEY` absente. Erreurs HTTP/réseau non-fatales : pipeline continue sans les résultats web. |
| `ai_filter.py:_process_batch` | Catch `JSONDecodeError`/`ValueError` → batch ignoré, le pipeline continue |
| `main.py:Étape 3` | Catch `GeminiUnavailableError` → email envoyé sans filtrage IA |
| `main.py:Étape 4` | Catch `MailerConfigError`/`MailerSendError` → log uniquement, pas de propagation |

**Garantie** : aucun chemin d'erreur ne fait crasher l'orchestrateur sans envoyer une notification (digest ou alerte).

---

## 6. Quotas et coûts à surveiller

| Service | Quota free tier | Conso par run |
|---|---|---|
| Gemini 2.5 Flash | 250 req/jour (free) | ~10 req (9 batchs + 1 résumé) |
| Gemini 2.5 Flash Lite (fallback #1) | 1000 req/jour (free) | activé uniquement si le modèle principal sature |
| Gemma 3 27B IT (fallback #2) | quotas free indépendants | activé si #1 sature aussi |
| Gemma 3 12B IT (fallback #3) | quotas free indépendants | dernière roue de secours, activé si #2 sature aussi |
| OpenAlex (par défaut) | aucune limite pratique, gratuit, sans clé | 6 req/run |
| arXiv Search API (par défaut) | aucune limite stricte (~3s entre req) | 5 req/run |
| Crossref (par défaut) | aucune limite pratique avec `mailto=` (pool polite) | 5 req/run |
| HAL (par défaut) | aucune limite stricte | 5 req/run |
| Semantic Scholar (par défaut) | 1 req/s sans clé, 100 req/s avec clé | 4 req/run |
| Tavily Web Search | 1000 req/mois (free) | 4 req/run si `TAVILY_API_KEY` présente |
| Gmail SMTP | 500 envois/jour | 1 envoi |
| Google News RSS | aucun officiel | 14 req |
| ArXiv RSS | aucun strict | 2 req |
| MDPI/IEEE/ScienceDaily | dépend de Cloudflare | 3 req |

À la fréquence prévue (1 run/semaine), aucun quota ne pose problème.

---

## 7. Extensions naturelles (non implémentées)

- **Section « diff vs semaine dernière »** : `previous_ai_output.json` est conservé mais jamais relu par le mailer
- **Cache Anthropic-style sur Gemini** : Google ne propose pas de prompt caching natif identique à Anthropic, mais une équivalence existe (Context Caching API)
- **Notification Slack/Teams** en plus de l'email
- **Dashboard web** local avec Streamlit pour explorer l'historique

Aucune n'est requise pour le MVP actuel.

---

## 8. Arbre de fichiers

```
veille_tech/
├── main.py                       # Orchestrateur principal (run hebdomadaire)
├── send_recap.py                 # Rattrapage : envoie l'archive complète
├── requirements.txt              # Dépendances pip
├── .env                          # Secrets (NON commité)
├── CLAUDE.md                     # Directives pour l'agent IA
├── MANUEL.md                     # Guide utilisateur novice
├── ARCHITECTURE.md               # Ce document
├── README.md                     # Intro courte
├── check.py                      # Utilitaire : liste les modèles Gemini autorisés
│
├── src/
│   ├── scraper.py                # Phase 2 — collecte RSS + Google News
│   ├── ai_filter.py              # Phase 3 — filtrage Gemini
│   ├── archive.py                # Phase 3.5 — archive cumulative
│   ├── mailer.py                 # Phase 4 — envoi email HTML
│   └── config.py                 # Constantes + chargement targets.json
│
└── data/
    ├── targets.json              # Concurrents + mots-clés (input)
    ├── seen_urls.json            # Mémoire FIFO des URLs déjà envoyées
    ├── scraper_output.json       # Sortie phase 2
    ├── ai_filter_output.json     # Sortie phase 3
    ├── previous_ai_output.json   # Snapshot run précédent
    ├── articles_archive.json     # Archive cumulative (alimentée à chaque run)
    └── scraper_checkpoint.json   # Checkpoint partiel scraping
```
