# Projet : Système de Veille Stratégique Industrielle
# Stack : Python 3.12+, curl_cffi, feedparser, Google Gemini API, SMTPlib (Gmail)

---

## Documents de référence (lire avant toute modif structurelle)

- `MANUEL.md` — guide utilisateur novice. Ne pas modifier sans demande explicite.
- `ARCHITECTURE.md` — source de vérité sur les modules, le pipeline et les décisions techniques.
- `SCORING.md` — explication public-facing de la notation IA.
- `README.md` — intro courte.

---

## Points d'entrée

| Commande | Rôle |
|---|---|
| `python main.py` | Pipeline complet : RSS + Google News + filtrage IA + email |
| `python send_recap.py "alice@x.com,bob@y.com"` | Rattrapage depuis `data/articles_archive.json` (sans re-scraper, sans coût Gemini) |
| `python send_recap.py "x@x.com" --dry-run` | Génère `data/recap_preview.html` pour inspecter le rendu |
| `python src/scraper.py` | Test scraper isolé |
| `python src/ai_filter.py` | Test filtrage IA sur `scraper_output.json` existant |
| `python src/mailer.py` | Test rendu HTML |
| `python check.py` | Liste les modèles Gemini autorisés pour la clé courante |
| `pip install -r requirements.txt` | Installation des dépendances |

---

## Données persistées (dossier `data/`)

| Fichier | Rôle |
|---|---|
| `targets.json` | Concurrents + mots-clés (input utilisateur) |
| `seen_urls.json` | Mémoire FIFO des URLs déjà envoyées (cap 10 000) |
| `scraper_output.json` | Sortie phase 2 (articles bruts) |
| `ai_filter_output.json` | Sortie phase 3 (articles notés et retenus) |
| `previous_ai_output.json` | Snapshot du run précédent (rotation hebdomadaire) |
| `articles_archive.json` | Archive cumulative (cap 5 000) — alimentée par chaque run, lue par `send_recap.py` |
| `scraper_checkpoint.json` | Checkpoint partiel pendant le scraping GNews |

---

## Directives de code (Strict)

- **Typage** : annotations obligatoires sur toute fonction publique (`from __future__ import annotations` autorisé)
- **Furtivité scraping** :
  - `curl_cffi` avec `impersonate="chrome124"` (suffisant pour Cloudflare MDPI)
  - Délais `random.gauss(μ, σ)` **clampés** à un minimum, jamais `random.uniform`
  - Micro-pauses gaussiennes inter-requêtes (RSS et Google News)
- **Gestion d'erreurs** :
  - Capturer les exceptions réseau spécifiquement : `(RequestsError, OSError)` — jamais `except:` nu, jamais `except Exception` générique pour le réseau
  - L'orchestrateur ne doit jamais crasher : `main.py` a un try/except global qui envoie un email d'alerte
- **Gemini** :
  - Toujours `response_mime_type="application/json"` quand on attend du JSON (sinon Gemini wrappe en ` ```json ... ``` `)
  - `_MAX_OUTPUT_TOKENS = 32768` (constante module dans `ai_filter.py`). Marge confortable pour batchs ≤ 50 articles.
  - **Auto-split sur troncature** : si `finish_reason == MAX_TOKENS` ou JSON malformé, `_process_batch` splitte le batch en 2 et relance récursivement (cap `_MAX_BATCH_SPLIT_DEPTH=2`, worst case 7 appels API). L'utilisateur peut donc laisser `AI_BATCH_SIZE=20` sans risquer de perdre un batch sur article pathologique.
  - `_call_gemini_with_retry` retourne un `_GeminiCallResult(text, truncated)` (NamedTuple) — les callers résumé exécutif accèdent à `.text`, batch utilise aussi `.truncated`.
  - Toujours parser avec `json.loads` + filet de sécurité regex en fallback
  - Le prompt système est construit dynamiquement avec `TARGET_COMPANIES` (ne pas hardcoder)
- **Style** : PEP 8, snake_case fonctions/variables, PascalCase classes
- **Commentaires** : aucun commentaire évident. Seuls les WHY non-obvious (workaround, invariant caché, contrainte externe)

---

## Pièges techniques rencontrés (à ne pas re-débugger)

- **PowerShell 5.1 + here-doc Python** : `"=" * 60` dans une heredoc est parsé par PS et casse. Utiliser un fichier `.py` temporaire à la place.
- **PowerShell `2>&1`** : enveloppe stderr Python dans `NativeCommandError` (faux warning ; pas un crash). Ne pas rediriger stderr explicitement, le runtime capture déjà.
- **Free tier Gemini 2.5 Flash** : 20 req/jour. Un run consomme ~10 req. Ne pas multiplier les tests si quota tendu.
- **Articles PVD/CVD** : domaine de niche, âge typique des plus récents = 60-180 jours. `RECENT_DAYS_LIMIT` à 30 filtrait tout — gardé à 90.
- **MDPI Cloudflare** : `impersonate="chrome124"` suffit. Ajouter un proxy CASSE l'empreinte TLS et fait perdre le bypass.
- **Google News RSS** : la syntaxe `("A" OR "B" OR "C") "keyword"` fonctionne. OR-grouping permet 14 req au lieu de 294 (N×M).
- **Délais GNews** : `gauss(25.0, 5.0)` minimum 15s — pas plus court (rate-limit), pas plus long (gaspille du temps).
- **Date système** : cohérente avec le contexte (~2026). Vérifier `Get-Date` si filtre date suspect.

---

## Préférences de collaboration

- **Plan avant action** : avant toute édition, exposer le correctif en une phrase technique précise. Si tâche multi-étapes, numéroter.
- **Solutions industrielles** : refuser explicitement les rustines fragiles (proxies gratuits, scraping headless lourd, retry naïf en boucle infinie). Justifier le refus.
- **Tests ciblés avant runs complets** : pour valider un fix scraping/IA, faire un test sur 1-3 sources / 1-2 batches avant de lancer `main.py` (~10 min).
- **Cleanup systématique** : tout fichier temporaire (`test_*.py`, `diagnose.py`, `bootstrap.py`, `*.log`) doit être supprimé après usage.
- **Doc en parallèle** : si comportement public change, mettre à jour `MANUEL.md` / `ARCHITECTURE.md` / `SCORING.md` dans le même travail.
- **Confirmation pour actions à effet visible** : envoi d'email réel, push git, modif de config user-tweakée — toujours demander avant.

---

## Patterns explicitement à éviter

- **Proxies gratuits / scrapers tiers gratuits** (GitHub proxy lists, ScraperAPI free, etc.) → incompatibles avec `curl_cffi impersonate`. Si proxy un jour nécessaire, parler de Bright Data / Smartproxy résidentiels payants.
- **Lancer `main.py` pour valider un fix** → faire un test ciblé qui prouve le fix en quelques secondes, pas un run complet de 10 min.
- **Modifier les valeurs `src/config.py`** (`USE_MEMORY`, `RECENT_DAYS_LIMIT`, `MAX_ARTICLES_PER_SOURCE`, `SCRAPE_LIMIT_MONTH`) sans demande explicite — ce sont des préférences utilisateur.
- **Laisser des fichiers temporaires dans le repo** après une session.
- **Skipper les hooks git** (`--no-verify`, `--no-gpg-sign`) sauf demande explicite.
- **Sur-engineering** : pas de feature flag, pas d'abstraction prématurée, pas de retry exponentiel pour des appels réseau locaux.

---

## Workflow imposé à l'agent

1. **Analyse** — lire les logs et les fichiers pertinents. Identifier la fonction fautive avec `chemin:ligne`.
2. **Planification** — exposer le correctif en une phrase technique avant de coder. Pour les tâches larges, découper en étapes numérotées avec impact estimé.
3. **Implémentation** — éditer (pas réécrire) les fichiers existants. Préserver logique, typage, style.
4. **Vérification** — proposer la commande CLI précise. Pour scraping/IA, prouver le fix avec un test ciblé avant le run complet.
5. **Documentation** — si comportement public change, mettre à jour MANUEL/ARCHITECTURE/SCORING.

---

## Décisions architecturales déjà actées (ne pas remettre en question sans raison)

- OR-grouping des requêtes Google News (14 au lieu de 294) — `scraper.py:build_gnews_queries`
- Mode JSON natif Gemini activé par appel via `json_mode=True` — `ai_filter.py:_call_gemini_with_retry`
- Vérification Python post-IA des concurrents (`_force_company_scores`) — filet de sécurité indépendant du LLM
- Archive cumulative séparée des seen_urls — sert le mode rattrapage `send_recap.py`
- Étoiles email : dichotomie pleine doré `#F59E0B` / vide gris `#E5E7EB` — `mailer.py:_render_stars`
- 5 hues distincts pour les badges score (violet/vert/bleu/ambre/gris) — `mailer.py:SCORE_LABELS`
- Filtre `RECENT_DAYS_LIMIT` à 90 jours — adapté au domaine PVD/CVD
