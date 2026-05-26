# Veille Technologique Automatisée

Système de veille stratégique industrielle pour le suivi de concurrents, d'innovations en revêtements de surface (PVD / CVD / ALD / sputtering / DLC) **et de découvertes transférables** depuis d'autres domaines (photonique, MEMS, biomim, nanotech, IA process, métamatériaux).

Le pipeline collecte des articles depuis **9 sources** scientifiques et de presse, les filtre via une cascade de modèles Gemini (~38 niveaux de fallback dynamiques) selon une **logique d'innovation transférable cross-domaine**, découvre automatiquement les nouveaux acteurs (entreprises et labos), et envoie un digest HTML hebdomadaire scoré 1-5 par email.

## Sources de collecte (9 sources)

| Source | Volume typique | Authentification |
|---|---|---|
| RSS (ArXiv ×2, MDPI Coatings, IEEE Spectrum, ScienceDaily) | ~200 articles | aucune |
| arXiv Search API (~111 requêtes broadcastées) | ~250 articles | aucune (UA identifiable) |
| OpenAlex (~112 requêtes) | ~300 articles | aucune (mailto poli) |
| Crossref (~112 requêtes) | ~150 articles | aucune (mailto poli) |
| HAL CNRS (~112 requêtes bilingues FR/EN) | ~50 articles | aucune |
| Semantic Scholar (~111 requêtes) | ~30 articles | clé optionnelle |
| Tavily Web Search (~110 requêtes) | ~40 articles | clé optionnelle (1000 req/mois) |
| **🆕 Google Patents** (~112 requêtes, extraction assignees) | ~100 brevets | aucune |
| Google News RSS (~589 requêtes furtives, mode weekend) | ~1500+ articles | aucune |

## Fonctionnalités phares

- **🌐 Recherche cross-domaine** : 36 thèmes pré-remplis (photonique, MEMS, biomim, nanotech, métamatériaux, IA, décoratif) broadcastés sur les 7 sources scientifiques
- **🔍 Découverte automatique d'acteurs** : extraction continue des entreprises (Patents) et labos (OpenAlex) non listés. Section dédiée dans l'email + revue interactive CLI
- **♿ Auto-promotion vers `targets.json`** : un acteur récurrent (count cumulé ≥ 30) est promu automatiquement à la fin du run, classifié `companies` ou `research_orgs` selon heuristique (cap 10/run, configurable via `AUTO_PROMOTE_MIN_COUNT` / `AUTO_PROMOTE_MAX_PER_RUN`)
- **⏯ Reprise sans re-scraper** : `resume_pipeline.py` reprend depuis `scraper_output.json` si le filtrage IA ou l'envoi email a échoué — économise 14-22h
- **🎯 Scoring « innovation transférable »** : l'IA évalue le potentiel d'INTÉGRATION cross-domaine avec PVD/ALD, pas juste la présence de mots-clés
- **🌐 Proxy résidentiel optionnel** : pool 1-3 proxies + health check + failover + auto-recovery (provider recommandé : Decodo)
- **🔒 Anti-détection 20+ couches** : TLS Chrome impersonation, locales rotatives, délais humains, pause circadienne, pre-flight arXiv, circuit breakers, **soft-ban detection (CAPTCHA/Cloudflare)**, **headers contextuels** (Sec-Fetch adaptatif API vs RSS), **params shufflés**, **cookies persistents inter-runs**, Accept-Encoding gzip+deflate+br
- **⚡ Parallélisation inter-sources** : OpenAlex + Crossref + HAL + Semantic Scholar en 4 threads simultanés (gain -60 min/run)
- **🔬 Pré-filtre Python** : articles hors-sujet écartés AVANT Gemini → économise 15-30% des tokens IA
- **💾 Atomic writes JSON** : tous les fichiers d'état écrits via tempfile+rename → résistant aux crashs/Ctrl+C
- **📊 Tracker bande passante proxy** : compteur cumulatif persisté + circuit-breaker `PROXY_BANDWIDTH_CAP_MB` pour protéger un quota trial
- **♻️ Cascade IA ~38 modèles** : Gemini 2.5/3.x/2.0/1.5 + Gemma 3/4 découverts dynamiquement
- **🧠 Mémoire articles** : 3 modes (Filtrer / Tout renvoyer + badge / Reset), évite les doublons inter-runs
- **🔒 Vérification quotas pré-run** : panneau coloré avec statut OK/tendu/RISQUE par source

## Documents

- [`COMMENT_CA_MARCHE.md`](COMMENT_CA_MARCHE.md) — guide novice étape par étape (recommandé pour débuter)
- [`MANUEL.md`](MANUEL.md) — manuel utilisateur complet
- [`ARCHITECTURE.md`](ARCHITECTURE.md) — détails techniques (pipeline, anti-bot, proxy_manager, cascade Gemini)
- [`SCORING.md`](SCORING.md) — explication public-facing de la notation IA cross-domaine

## Installation rapide

```bash
# 1. Cloner le repo
git clone https://github.com/<user>/<repo>.git
cd <repo>

# 2. Créer l'environnement Python (3.12+)
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux

# 3. Installer les dépendances
pip install -r requirements.txt

# 4. Configurer les secrets via l'assistant interactif
python configurer.py
# (ou : copy .env.example .env puis éditer manuellement)
```

## Lancement

```bash
python main.py                                       # Pipeline complet (~14-22h selon les cibles)
python resume_pipeline.py "alice@x.com,bob@y.com"    # Reprendre après scraping (IA + envoi) sans re-scraper
python resume_pipeline.py "alice@x.com" --dry-run    # Preview HTML local (filtrage IA + génération sans envoi)
python resume_pipeline.py "alice@x.com" --no-ai      # Envoi sans relancer Gemini (réutilise ai_filter_output.json)
python send_recap.py "alice@x.com,bob@y.com"         # Renvoyer l'archive cumulative sans re-scraper
python send_recap.py "user@x.com" --dry-run          # Générer un preview HTML local
python -m src.proxy_manager                          # Tester les proxies résidentiels
python check.py                                      # Lister les modèles Gemini accessibles
```

## Stack technique

- **Python 3.12+** avec annotations de type complètes
- **`curl_cffi`** pour la furtivité TLS (impersonation Chrome 124/131/120)
- **`feedparser`** pour le parsing RSS/Atom
- **`google.generativeai`** avec cascade dynamique sur ~38 modèles
- **`smtplib` + Gmail SMTP** pour la livraison (mot de passe d'application)
- **Proxies résidentiels** (optionnel) : compatible IPRoyal / Decodo / Bright Data

## Anti-détection (18 couches)

TLS impersonate rotatif, User-Agent rotatif, Client Hints Chrome cohérents, Sec-Fetch-* + DNT, locales rotatives, délais humains mixtes (4 modes), multiplicateur nuit ×1.8, pause circadienne 4-6h, rotation de session toutes les 30 req, shuffle aléatoire des requêtes, pre-flight arXiv, circuit breakers par source, backoff progressif par domaine, warm-up MDPI/ScienceDaily, optionnellement proxy résidentiel pour fiabilité 99.5%. Voir `ARCHITECTURE.md` section « Anti-bot multi-couches » pour le détail.

## Licence

Projet personnel. Voir avec l'auteur avant tout usage commercial.
