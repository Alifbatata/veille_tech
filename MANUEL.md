# Manuel d'utilisation — Veille Tech

Ce document s'adresse à toute personne qui veut **utiliser** le programme, sans connaissance technique préalable.

> 🆕 **Pour les vrais novices** : il existe maintenant un guide encore plus accessible avec des explications pas à pas et un lanceur Windows en double-clic. Voir [`COMMENT_CA_MARCHE.md`](COMMENT_CA_MARCHE.md) ou utiliser directement `lancer.bat`.

---

## 1. À quoi sert ce programme ?

Chaque fois que tu le lances, il :

1. **Cherche** sur internet les dernières publications scientifiques et articles de presse sur les revêtements industriels (PVD, CVD, ALD, etc.).
2. **Surveille les concurrents** listés dans `data/targets.json` (Oerlikon, Ionbond, Platit, etc.).
3. **Demande à une IA** (Google Gemini) de noter chaque article de 1 à 5 selon sa pertinence.
4. **T'envoie un email** avec un résumé exécutif et les meilleurs articles, classés par score.

L'objectif : tu reçois en 5 minutes ce qui t'aurait pris 3 heures à compiler manuellement.

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

À la racine du projet, créer un fichier appelé `.env` avec ce contenu :

```env
# Clé API Google Gemini (gratuite : https://aistudio.google.com/app/apikey)
GEMINI_API_KEY=AIza...ta_cle_ici

# Compte Gmail expéditeur
GMAIL_USER=ton.compte@gmail.com
GMAIL_PASSWORD=xxxx xxxx xxxx xxxx

# Destinataires (séparés par virgule)
MAIL_RECIPIENT=mahmoud.unjeunearabe@gmail.com,mahmoud.alimohamad@positivecoating.ch

# (Optionnel) Clé Tavily pour élargir la recherche au Web académique
# Inscription gratuite : https://app.tavily.com (1000 requêtes/mois en free tier)
# Si absente, la recherche Web est simplement désactivée — RSS et Google News continuent normalement.
# TAVILY_API_KEY=tvly-...
```

> ⚠️ `GMAIL_PASSWORD` n'est **pas** ton mot de passe Gmail habituel. C'est un **mot de passe d'application** à 16 caractères :
> 1. Va sur https://myaccount.google.com/apppasswords
> 2. Créer un nouveau mot de passe pour « Veille Tech »
> 3. Copier les 16 caractères dans le fichier `.env`

> 💡 **Recherche Web élargie (Tavily) — optionnelle**. Pour activer la recherche au-delà des flux RSS et Google News (utile pour rattraper les papers universitaires non syndiqués), il faut deux choses :
> 1. Ajouter `TAVILY_API_KEY=...` dans `.env` (clé gratuite sur https://app.tavily.com)
> 2. Modifier `main.py` ligne `run_scraper(...)` pour passer `include_web=True`
>
> Sans clé Tavily, le programme tourne normalement avec RSS + Google News (comportement par défaut).

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

```
   ┌─────────────────────────────────────────────────────────────┐
   │  Tu tapes : python main.py                                   │
   └─────────────────────────────────────────────────────────────┘
                              │
                              ▼
   ┌─────────────────────────────────────────────────────────────┐
   │  ÉTAPE 1 — Sauvegarde de l'historique                        │
   │  Le digest précédent est copié dans previous_ai_output.json  │
   │  pour garder une trace.                                      │
   └─────────────────────────────────────────────────────────────┘
                              │
                              ▼
   ┌─────────────────────────────────────────────────────────────┐
   │  ÉTAPE 2 — Scraping (durée variable selon les sources)       │
   │                                                              │
   │  Sources interrogées par défaut :                            │
   │  • 5 flux RSS scientifiques (ArXiv, MDPI, IEEE, ...)         │
   │  • arXiv Search API : 5 requêtes mots-clés sur tout l'index  │
   │  • OpenAlex : 6 requêtes sur 250M+ papers académiques        │
   │  • Google News : recherches concurrent × mot-clé             │
   │  • (optionnel) Tavily Web si TAVILY_API_KEY présente         │
   │                                                              │
   │  Anti-bot : rotation User-Agent + empreinte TLS Chrome,      │
   │  délais aléatoires, cooldown progressif sur les blocages.    │
   │                                                              │
   │  Filtres : articles > 90 jours et déjà envoyés sont ignorés. │
   │  → écrit data/scraper_output.json                            │
   └─────────────────────────────────────────────────────────────┘
                              │
                              ▼
   ┌─────────────────────────────────────────────────────────────┐
   │  ÉTAPE 3 — Filtrage IA (~2 minutes)                          │
   │                                                              │
   │  • Découpe les articles en lots de 20                        │
   │  • Envoie chaque lot à Gemini Flash                          │
   │  • Gemini note chaque article de 1 à 5                       │
   │  • Force score ≥ 4 si un concurrent est mentionné            │
   │  • Génère un résumé exécutif global                          │
   │                                                              │
   │  Résilience : si le quota Gemini saute, bascule auto vers    │
   │  flash-lite, puis Gemma 27B, puis Gemma 12B (4 niveaux).     │
   │  → écrit data/ai_filter_output.json                          │
   └─────────────────────────────────────────────────────────────┘
                              │
                              ▼
   ┌─────────────────────────────────────────────────────────────┐
   │  ÉTAPE 4 — Envoi email (~5 secondes)                         │
   │                                                              │
   │  • Construit un email HTML stylisé                           │
   │  • Trie les articles par score décroissant                   │
   │  • Envoie via SMTP Gmail aux destinataires                   │
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

Tous les paramètres sont dans **`src/config.py`** :

| Paramètre | Valeur actuelle | Effet |
|---|---|---|
| `MAX_ARTICLES_PER_SOURCE` | 50 | Nombre max d'articles pris par flux RSS |
| `RECENT_DAYS_LIMIT` | 90 | Articles plus vieux ignorés (en jours) |
| `USE_MEMORY` | False (par défaut) ou choisi à l'Étape 2/4 du lancement interactif | Active le filtre des URLs déjà envoyées |
| `SCRAPE_LIMIT_MONTH` | True | Active le filtre de fraîcheur |

Pour changer la liste des concurrents ou des mots-clés : éditer **`data/targets.json`**.

---

## 10. Dépannage

### « Aucun article reçu » dans l'email

C'est normal si rien de neuf n'a été publié cette semaine sur tes sujets. L'email arrive quand même avec « 0 innovation(s) » dans le sujet.

### Erreur « GEMINI_API_KEY manquante »

Vérifier que le fichier `.env` existe à la racine et contient bien la ligne `GEMINI_API_KEY=...`.

### Erreur d'authentification SMTP

- Vérifier que `GMAIL_PASSWORD` est bien un **mot de passe d'application** (16 caractères avec espaces), pas ton mot de passe normal
- Vérifier que la double authentification Google est activée sur le compte (prérequis pour les mots de passe d'app)

### « Quota API dépassé »

Le free tier de Gemini est limité à 20 requêtes/jour pour `gemini-2.5-flash`. Le programme utilise ~10 requêtes par run. Si tu lances plusieurs fois le même jour, tu peux toucher la limite. Solution :
- Attendre minuit (heure Pacifique = 9h heure française)
- Ou activer le pay-as-you-go sur https://aistudio.google.com (~quelques centimes par run)

### MDPI renvoie 0 article

Notre bypass anti-Cloudflare est efficace mais peut occasionnellement échouer si Cloudflare met à jour ses signatures. Vérifier dans les logs `⚠️ ... a renvoyé du HTML (blocage anti-bot)`. Solution : relancer plus tard, ou changer la valeur d'`impersonate` dans `src/scraper.py` (ex: `chrome120`, `safari17_0`).

---

## 11. Résumé en 3 lignes

1. `python main.py` une fois par semaine
2. Tu reçois un email avec uniquement les nouveautés
3. Tout se configure dans `src/config.py` et `data/targets.json`
