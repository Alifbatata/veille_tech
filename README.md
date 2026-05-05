# Veille Technologique Automatisée

Système de veille stratégique industrielle pour le suivi de concurrents et d'innovations en revêtements de surface (PVD / CVD / ALD / sputtering / DLC).

Le pipeline collecte des articles depuis 8 sources scientifiques et de presse, les filtre via une cascade de modèles Gemini (38 niveaux de fallback dynamiques), et envoie un digest HTML hebdomadaire scoré 1-5 par email.

## Sources de collecte

| Source | Volume typique | Authentification |
|---|---|---|
| RSS (ArXiv ×2, MDPI Coatings, IEEE Spectrum, ScienceDaily) | ~200 articles | aucune |
| arXiv Search API (5 requêtes thématiques) | ~100 articles | aucune |
| OpenAlex (6 requêtes thématiques) | ~150 articles | aucune (mailto poli) |
| Crossref (5 requêtes thématiques) | ~100 articles | aucune (mailto poli) |
| HAL CNRS (5 requêtes thématiques) | ~40 articles | aucune |
| Semantic Scholar | ~30 articles | clé optionnelle (rate-limit 429 sans) |
| Tavily Web Search | ~40 articles | clé optionnelle (1000 req/mois free) |
| Google News RSS (294 requêtes furtives, mode weekend) | ~1000+ articles | aucune |

## Documents

- [`MANUEL.md`](MANUEL.md) — guide utilisateur novice
- [`ARCHITECTURE.md`](ARCHITECTURE.md) — détails techniques (pipeline, anti-bot, fallback, etc.)
- [`SCORING.md`](SCORING.md) — explication public-facing de la notation IA

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

# 4. Configurer les secrets
copy .env.example .env          # Windows
# cp .env.example .env          # macOS / Linux
# puis éditer .env avec tes clés API et mot de passe Gmail
```

## Lancement

```bash
python main.py                                       # Pipeline complet (~18-22h en mode weekend)
python send_recap.py "alice@x.com,bob@y.com"         # Renvoyer l'archive sans re-scraper
python send_recap.py "user@x.com" --dry-run          # Générer un preview HTML local
```

## Stack technique

- **Python 3.12+** avec annotations de type complètes
- **`curl_cffi`** pour la furtivité TLS (impersonation Chrome 124/131/120)
- **`feedparser`** pour le parsing RSS/Atom
- **`google.generativeai`** avec cascade dynamique sur 38 modèles (Gemini 2.5/3.x/2.0/1.5 + Gemma 3/4)
- **`smtplib` + Gmail SMTP** pour la livraison (mot de passe d'application requis)

## Anti-détection

17 couches d'anti-détection comportementale (TLS impersonate rotatif, Client Hints Chrome, Sec-Fetch-*, locales rotatives, délais humains mixtes 4 modes, multiplicateur nuit ×1.8, pause circadienne 4-6h, rotation de session toutes les 30 req, shuffle aléatoire des requêtes, circuit breaker à 3 strikes, etc.). Voir `ARCHITECTURE.md` section « Anti-bot multi-couches » pour le détail.

## Licence

Projet personnel. Voir avec l'auteur avant tout usage commercial.
