# Comment ça marche — Guide pour novice

Ce document explique **étape par étape, en français simple**, ce que fait le programme de Veille Technologique. Aucune connaissance technique requise — tu peux le lire même si tu n'as jamais écrit une ligne de code.

---

## 🎯 Que fait ce programme, en une phrase ?

> **Une fois par semaine, il lit automatiquement des centaines d'articles scientifiques et industriels sur les revêtements de surface, demande à une IA de garder uniquement ceux qui sont vraiment intéressants, et t'envoie un email récapitulatif joliment mis en page.**

---

## 📦 Vue d'ensemble : les 5 grandes étapes

```
┌──────────────┐   ┌─────────────┐   ┌──────────────┐   ┌─────────────┐   ┌──────────┐
│  1. COLLECTE │ → │  2. FILTRE  │ → │  3. NOTATION │ → │  4. RESUME  │ → │ 5. EMAIL │
│   8 sources  │   │   doublons  │   │   IA 1 a 5   │   │  executif   │   │ HTML pro │
└──────────────┘   └─────────────┘   └──────────────┘   └─────────────┘   └──────────┘
```

À chaque étape, tu vas voir des messages colorés défiler dans la console — c'est normal, c'est le programme qui te tient au courant en temps réel.

---

## 🚀 Le tout premier lancement

### Étape A — Installer Python (si pas déjà fait)

Python est le langage qui fait tourner le programme. C'est gratuit.

1. Va sur **https://www.python.org/downloads/windows/**
2. Clique sur le gros bouton « Download Python 3.X »
3. Lance l'installateur
4. **TRÈS IMPORTANT** : sur le premier écran, **coche la case « Add python.exe to PATH »**
5. Clique « Install Now »
6. Attends que ça se termine

### Étape B — Double-cliquer sur `lancer.bat`

Une fois dans le dossier du projet, **double-clique simplement sur `lancer.bat`**.

Une fenêtre noire s'ouvre. Le script va automatiquement :

| Étape automatique | Ce qui se passe |
|---|---|
| 1. Détection Python | Vérifie que Python est bien installé |
| 2. Création de l'environnement | Prépare un dossier `.venv` qui isole le projet |
| 3. Installation des dépendances | Télécharge les outils nécessaires (rich, feedparser, etc.) |
| 4. Vérification de la config | Si pas de `.env`, lance l'assistant de configuration |
| 5. Démarrage du pipeline | Lance la collecte + filtrage + email |

Tu n'as **rien à taper** sauf répondre aux questions de l'assistant.

### Étape C — Configurer les clés API

Si c'est ton premier lancement, l'assistant `configurer.py` va se lancer. Il va te demander, **une par une**, les informations suivantes :

#### 🤖 1. Clé Google Gemini (OBLIGATOIRE — gratuite)

C'est l'IA qui lit chaque article et lui donne une note de 1 à 5.

**Comment l'obtenir** :
1. Va sur **https://aistudio.google.com/app/apikey**
2. Connecte-toi avec ton compte Google (Gmail)
3. Clique « Create API key » → « Create API key in new project »
4. Copie la clé qui commence par `AIza...`
5. Colle-la quand le programme te le demande

→ **Gratuit : 250 utilisations par jour, largement suffisant.**

#### 📧 2. Adresse Gmail expéditeur (OBLIGATOIRE)

L'adresse Gmail qui va **envoyer** le récapitulatif chaque semaine.
Doit être une adresse Gmail (pas Outlook, pas Yahoo).

#### 🔐 3. Mot de passe d'application Gmail (OBLIGATOIRE)

⚠️ **Attention** : ce n'est **PAS** ton mot de passe Gmail normal !
C'est un mot de passe spécial de 16 caractères que Google génère pour les applications.

**Comment l'obtenir** :
1. Active la double-authentification (si pas déjà fait) : https://myaccount.google.com/security
2. Va sur **https://myaccount.google.com/apppasswords**
3. Donne un nom (ex : « Veille Tech »)
4. Google te montre un mot de passe genre `abcd efgh ijkl mnop`
5. Colle-le quand le programme te le demande

#### 📬 4. Destinataires de l'email (optionnel)

Liste des emails qui vont **recevoir** le récapitulatif.
Plusieurs adresses séparées par des virgules.

Si tu laisses vide, le programme envoie à toi-même (l'expéditeur).

#### 🌐 5. Clé Tavily (optionnelle — gratuite)

Service de recherche web qui ajoute une 7e source de collecte.

**Comment l'obtenir** :
1. Va sur **https://app.tavily.com**
2. Crée un compte gratuit (1 clic avec Google)
3. Va dans « API Keys » → copie la clé qui commence par `tvly-`

→ **Gratuit : 1000 utilisations par mois.**

#### 🎓 6. Clé Semantic Scholar (optionnelle — gratuite)

Base de 200 millions de papers scientifiques.

**Comment l'obtenir** :
1. Va sur **https://www.semanticscholar.org/product/api**
2. Demande une clé via le formulaire
3. **Validation manuelle 24-48h** : tu reçois la clé par email (commence par `s2k-`)

Si tu ne mets pas de clé, le programme saute simplement cette source.

---

## 📅 Quand lancer le programme ? — Conseil important

> ⚠️ **Lance le programme APRÈS 9h du matin (heure suisse).**

**Pourquoi ?** Les quotas gratuits Google AI Studio se renouvellent chaque jour à **minuit Pacific Time**, ce qui correspond à **9h00 heure suisse**. Si tu lances avant 9h, tu utilises potentiellement les restes du quota de la veille — ce qui n'est pas optimal.

**Cas d'usage typique** : tu pars en weekend vendredi soir.
- ❌ Pas idéal : lancer vendredi 18h00 (encore le quota de jeudi)
- ✅ Mieux : lancer **vendredi à 21h00** ou **samedi matin 9h30** (quotas frais du jour)

Le programme te le rappelle automatiquement au démarrage : si l'heure locale est entre minuit et 9h, un avertissement orange s'affiche et te demande si tu veux continuer quand même.

---

## ⚙️ Choisir le volume d'articles — au démarrage

Quand tu lances le programme, il te propose **5 presets** :

| Preset | Articles / source RSS | Durée totale | Pour qui ? |
|---|---|---|---|
| 🚀 Test rapide | 25 | ~5 min | Vérifier que tout fonctionne avant un vrai run |
| 📰 Standard hebdo | 50 | ~3-4 h | Usage hebdomadaire normal |
| 📚 Approfondi | 100 | ~8-10 h | Si tu lances tous les 15 jours |
| 🏆 Marathon weekend | 200 | ~18-22 h | Si tu lances 1 fois par mois ou après une longue pause |
| ✏️ Personnalisé | tu choisis | variable | Pour les utilisateurs avancés |

**Recommandation** : si tu pars en weekend du vendredi soir au lundi matin, choisis **Marathon weekend**. Tu auras le digest dans ta boîte mail dimanche soir ou lundi matin.

---

## 🤖 Étape 1 : Collecte automatique des articles

Le programme va, **à tour de rôle**, interroger 8 sources différentes :

| Source | Type | Contenu typique |
|---|---|---|
| **RSS** (5 flux) | Flux scientifiques | ArXiv, MDPI Coatings, IEEE Spectrum, ScienceDaily |
| **arXiv Search** | API académique | Recherche par mot-clé sur tout l'index arXiv |
| **OpenAlex** | API académique | 250 millions d'œuvres scientifiques |
| **Crossref** | API académique | 140 millions de papers avec DOI |
| **HAL (CNRS)** | Archive française | Préprints français (CEA-Leti, CNRS, ONERA…) |
| **Semantic Scholar** | API académique | Papers enrichis IA, résumés auto |
| **Tavily Web** | Recherche web | Articles industriels et presse spécialisée |
| **Google News** | Actualités | 294 recherches ciblées (entreprise × mot-clé) |

Tu vois défiler dans la console des messages comme :
```
📡 Récupération RSS : MDPI – Coatings
   └─ 50 article(s) collecté(s)
🔬 arXiv search [3/5] : « ti:"chemical vapor deposition" OR ... »
   └─ arXiv search : 20 résultat(s)
```

**Pourquoi ça prend autant de temps ?** Pour ne pas se faire bloquer par Google et autres, le programme se comporte comme un humain : il fait des pauses naturelles entre chaque recherche (parfois 2-3 minutes, parfois plus). Il y a même une **pause « nuit »** de 4-6h en cours de run pour simuler quelqu'un qui dort.

**Si une source plante** (ex: Semantic Scholar qui rate-limite) : pas grave, le programme passe à la suivante. **Aucune source bloquante.**

---

## 🧹 Étape 2 : Suppression des doublons

Un même article peut apparaître sur 3 sources différentes (OpenAlex, Crossref, RSS). Le programme :
1. Normalise toutes les URLs (enlève les paramètres de tracking, le `/` final, etc.)
2. Garde une seule version de chaque article
3. Garde aussi en mémoire les URLs déjà envoyées les semaines précédentes pour ne pas te resourner les mêmes articles

Tu vois dans la console :
```
🧹 Dédoublonnage : 24 doublon(s) éliminé(s)
```

---

## 🤖 Étape 3 : Notation IA — score 1 à 5

Le programme envoie les articles à une IA Google Gemini, par **paquets de 20**. L'IA :
1. Lit le titre et le résumé de chaque article
2. Lui donne une note de **1 (peu intéressant) à 5 (innovation majeure)**
3. Justifie son choix en une phrase
4. Ajoute des tags (ex: `PVD`, `CEA-Leti`, `couche atomique`)
5. **Force le score à 4 minimum** si l'article concerne un de tes concurrents (Oerlikon, Ionbond, Platit…)

**Si le quota du modèle Gemini s'épuise**, le programme bascule automatiquement vers le modèle suivant dans une liste de **16 modèles de secours** (cascade dynamique). Tu ne perds aucun article.

---

## 📝 Étape 4 : Résumé exécutif

Une fois tous les articles notés, le programme demande à une IA Gemini Pro (la plus puissante) de générer un **résumé exécutif de 3 phrases** sur les tendances principales de la semaine. Ce résumé apparaît en haut du digest email.

---

## 📧 Étape 5 : Envoi de l'email

Le programme :
1. Met en forme le digest avec un beau design HTML (cartes, étoiles, badges colorés)
2. Trie les articles par score décroissant
3. Génère deux versions (HTML stylisé + texte brut, pour compatibilité tous clients mail)
4. Envoie via Gmail SMTP à tous les destinataires configurés

Tu reçois l'email avec :
- Le résumé exécutif en haut
- Les articles classés du plus haut score (5 ⭐) au plus bas
- Pour chaque article : titre, source, justification IA, tags, lien direct

---

## 🛡️ Et la sécurité ?

| Risque | Protection |
|---|---|
| **Détection bot Google** | 17 couches d'anti-détection (TLS Chrome, locales rotatives, délais humains, pause nocturne, etc.) |
| **Bannissement IP** | Circuit breaker à 3 strikes : si 3 blocages d'affilée, on abandonne Google News (les 7 autres sources continuent) |
| **Crash Gemini** | Cascade automatique sur 16 modèles |
| **Crash réseau** | Chaque source qui rate retourne `[]`, pipeline continue |
| **Crash total** | Email d'alerte automatique sur ton Gmail |
| **Vol de tes clés API** | `.env` ignoré par git, jamais sur GitHub |

---

## 🆘 En cas de problème

**Le programme plante / s'arrête en erreur** → Tu reçois un email automatique « ❌ Erreur critique Veille Tech » avec le détail. Lance-moi le message d'erreur, je débogue.

**Le digest n'arrive pas dans ta boîte** → Vérifie le dossier **Spam**. Gmail peut considérer le mail SMTP comme suspect au début. Marque comme « non-spam » la première fois.

**L'email arrive vide ou avec peu d'articles** → C'est normal certaines semaines (peu de publications). Le programme ne renvoie JAMAIS un article déjà envoyé (sauf si tu vides `data/seen_urls.json`).

**Tu veux changer une clé API** → Lance `python configurer.py` pour modifier le `.env` étape par étape.

**Tu veux juste vérifier ta config** → Lance `python configurer.py --check` (affiche un tableau récap sans rien modifier).

**Tu veux savoir quels modèles IA sont disponibles** → Lance `python check.py` (affiche les 32 modèles + leurs quotas).

---

## 📂 Que retrouver après chaque run ?

Dans le dossier `data/` :

| Fichier | Contenu |
|---|---|
| `scraper_output.json` | Tous les articles bruts collectés (avant filtrage IA) |
| `ai_filter_output.json` | Articles retenus avec scores et justifications |
| `articles_archive.json` | Historique cumulatif (5000 derniers articles vus) |
| `seen_urls.json` | Mémoire des URLs déjà envoyées pour ne pas dupliquer |
| `previous_ai_output.json` | Snapshot du run précédent (utilisé pour la section « la semaine dernière ») |

Tu peux ouvrir n'importe lequel de ces fichiers JSON avec un éditeur de texte ou un navigateur web pour explorer.

Dans `logs/` :
- `veille.log` (+ rotations `.1`, `.2`…) : journaux détaillés de chaque exécution

---

## 🎯 En résumé : qu'est-ce que je dois faire concrètement chaque semaine ?

**Le vendredi soir / samedi matin (après 9h)** :
1. Double-clique sur `lancer.bat`
2. Choisis le preset `📰 Standard hebdo` (ou `🏆 Marathon weekend` si tu pars longtemps)
3. Confirme et laisse tourner
4. Reviens lundi matin → ton email t'attend dans ta boîte

C'est tout. **Aucune intervention manuelle** entre les exécutions.

---

## 🧠 Pour les curieux : la cascade IA en détail

Le programme essaie les modèles dans cet ordre, en cascade automatique :

```
1. gemini-2.5-flash       (rapide, 250/jour)
2. gemini-2.5-flash-lite  (1000/jour)
3. gemini-2.5-pro         (très précis, 100/jour)
4. gemini-3-flash-preview (le tout dernier de Google)
5. gemini-3.1-flash-lite-preview
6. gemini-3-pro-preview
7. gemini-3.1-pro-preview
8. gemini-2.0-flash       (200/jour)
9. ... (et 7 autres fallbacks jusqu'à Gemma 27B)
```

Pour le **résumé exécutif** (1 seul appel haute valeur), le programme essaie en priorité les modèles **Pro** (qualité maximale) puis bascule.

**Bonne veille technologique 🛰️**
