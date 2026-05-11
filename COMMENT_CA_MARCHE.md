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
│  9 sources   │   │   doublons  │   │   IA 1 a 5   │   │  executif   │   │ HTML pro │
└──────────────┘   └─────────────┘   └──────────────┘   └─────────────┘   └──────────┘
```

**Nouveauté 2026** : la philosophie de notation a évolué. L'IA évalue maintenant le **potentiel d'INTÉGRATION cross-domaine** : si une découverte d'un autre domaine (photonique, biomim, MEMS, nanotech...) est combinée à tes procédés PVD/ALD, est-ce que ça crée une innovation ? Voir le fichier `SCORING.md` pour les détails.

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

#### 🌐 7. Proxy résidentiel (optionnel mais TRÈS recommandé)

Pour ne **jamais te faire bloquer par les sources** (Google News, arXiv, etc.).

**Comment ça marche** : au lieu d'envoyer toutes tes recherches depuis ton IP perso (que les sites peuvent bloquer après quelques centaines de requêtes), tu utilises un proxy résidentiel qui change d'IP à chaque requête. Indistinguable d'un vrai utilisateur.

**Provider recommandé** : Decodo (https://decodo.com).
- Crée un compte (5 min)
- Dépose **$7 minimum** (te donne ~6-8 mois d'utilisation pour notre volume)
- Récupère ton URL au format `http://USER:PASS@gate.decodo.com:7000`
- Colle-la dans le configurer

Le programme fait un **health check au démarrage** + **failover automatique** si le proxy a un souci. Si tu ne mets rien, le programme tourne en mode direct (ton IP) — fonctionne mais avec un risque de blocage occasionnel.

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

Le programme va, **à tour de rôle**, interroger 9 sources différentes :

| Source | Type | Contenu typique |
|---|---|---|
| **RSS** (5 flux) | Flux scientifiques | ArXiv, MDPI Coatings, IEEE Spectrum, ScienceDaily |
| **arXiv Search** | API académique | Recherche par mot-clé sur tout l'index arXiv |
| **OpenAlex** | API académique | 250 millions d'œuvres scientifiques |
| **Crossref** | API académique | 140 millions de papers avec DOI |
| **HAL (CNRS)** | Archive française | Préprints français (CEA-Leti, CNRS, ONERA…) |
| **Semantic Scholar** | API académique | Papers enrichis IA, résumés auto |
| **Tavily Web** | Recherche web | Articles industriels et presse spécialisée |
| **🆕 Google Patents** | Brevets industriels | Innovations PVD/CVD/ALD avant publication presse (Applied Materials, Lam, etc.) |
| **Google News** | Actualités | ~589 recherches ciblées (entreprise × mot-clé + solos) |

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

## 🤖 Étape 3 : Notation IA — score 1 à 5 (philosophie 2026)

Le programme envoie les articles à une IA Google Gemini, par **paquets de 30**. L'IA évalue désormais le **potentiel d'INTÉGRATION cross-domaine** :

> Si on prenait cette technologie/découverte et qu'on l'appliquait via PVD ou ALD, est-ce que ça créerait quelque chose de nouveau et utile ?

Cette logique permet de capter les innovations qui viennent d'**autres domaines** (photonique, biomim, MEMS, nanotech, IA) mais qui sont **transférables** à tes procédés. C'est exactement le genre d'opportunités que tu loupais avec l'ancienne approche.

L'IA :
1. Lit le titre et le résumé de chaque article
2. Lui donne une note de **1 (hors-sujet) à 5 (transférable directement)**
3. **Justifie en donnant l'angle d'intégration** : « Les nanostructures plasmoniques peuvent être déposées par PVD pour créer des couleurs structurales sur cadrans »
4. Ajoute des tags (ex: `PVD`, `CEA-Leti`, `couche atomique`, `metasurfaces`)
5. **Force le score à 4 minimum** si l'article concerne un de tes concurrents (Oerlikon, Lam Research, Aixtron, Tokyo Electron, etc.)

**Si le quota du modèle Gemini s'épuise**, le programme bascule automatiquement vers le suivant dans une **cascade dynamique de ~38 modèles** (Gemini 2.5/2.0/1.5, Gemma 3, etc.). Tu ne perds aucun article.

### 🆕 Découverte automatique d'acteurs

Pendant qu'il collecte, le programme **extrait aussi automatiquement les noms des entreprises et labos** qu'il croise dans les résultats :
- Champ `assignee` des brevets Google Patents (les déposants)
- Affiliations institutionnelles des auteurs OpenAlex

Ces acteurs **non encore dans tes listes** sont agrégés dans `data/discovered_actors.json` avec un compteur d'occurrences cumulatif.

**À la fin de chaque run, le programme promeut automatiquement vers `targets.json` les acteurs qui dépassent 30 occurrences cumulées** (10 maximum par run, classifiés `companies` ou `research_orgs` selon une heuristique nom + source). Ta veille **s'enrichit toute seule** au fil des semaines, sans intervention.

Tu peux toujours valider/rejeter manuellement les candidats sous le seuil via l'**action 11 du menu** d'édition CLI. Pour ajuster les seuils par défaut, ajoute dans `.env` : `AUTO_PROMOTE_MIN_COUNT=30` et `AUTO_PROMOTE_MAX_PER_RUN=10`.

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
- **🆕 Section « 🔍 Acteurs découverts automatiquement »** : top 15 entreprises/labos vus dans les résultats mais pas dans tes listes — candidats à ajouter pour étendre ta veille
- **Section « ⏪ Déjà vu la semaine passée »** : top articles 4★/5★ du run précédent
- **🆕 Badge violet « 📌 Déjà envoyé »** : visible UNIQUEMENT en mode TOUT_RENVOYER, pour distinguer les articles déjà reçus auparavant des nouveaux

---

## 🛡️ Et la sécurité ?

| Risque | Protection |
|---|---|
| **Détection bot Google** | 18 couches d'anti-détection (TLS Chrome, locales rotatives, délais humains, pause nocturne, pre-flight arXiv, etc.) |
| **Bannissement IP** | Circuit breaker 3 strikes par source + **proxy résidentiel optionnel** (Decodo) qui élimine ce risque à ~$5/mois |
| **Crash Gemini** | Cascade automatique sur ~38 modèles découverts dynamiquement |
| **Crash réseau** | Chaque source qui rate retourne `[]`, pipeline continue |
| **Crash total** | Email d'alerte automatique sur ton Gmail |
| **Vol de tes clés API** | `.env` ignoré par git, jamais sur GitHub |
| **Proxy down en cours de run** | Auto-recovery toutes les 60s + failover vers backup si configuré |

---

## 🆘 En cas de problème

**Le programme plante / s'arrête en erreur** → Tu reçois un email automatique « ❌ Erreur critique Veille Tech » avec le détail. Lance-moi le message d'erreur, je débogue.

**Le scraping a réussi mais le filtrage IA ou l'email a planté** → Pas de panique, tes 4000+ articles sont sauvegardés dans `data/scraper_output.json`. Lance `python resume_pipeline.py "ton.email@x.com,collegue@y.com"` pour reprendre la phase IA + envoi sans relancer 14-22h de scraping. Ajoute `--dry-run` pour preview HTML, `--no-ai` pour réutiliser le filtrage déjà fait.

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
| **🆕 `discovered_actors.json`** | Acteurs découverts auto (cumulatif inter-runs) — entreprises/labos vus dans les résultats Patents/OpenAlex |

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

Au démarrage, le programme appelle l'API Google pour **lister TOUS les modèles auxquels ta clé donne accès** (typiquement ~38). Il les trie selon une table de priorité :

```
Tier 1 (rapide, qualité max)
1. gemini-2.5-flash       (250 req/jour free)
2. gemini-2.5-flash-lite  (1000 req/jour free)
3. gemini-2.5-pro         (100 req/jour free)

Tier 2 (Gemini 3 preview)
4. gemini-3-flash-preview
5. gemini-3.1-flash-lite-preview
6. gemini-3-pro-preview
7. gemini-3.1-pro-preview

Tier 3 (Gemini 2.0)
8. gemini-2.0-flash       (200 req/jour free)
9. gemini-2.0-flash-lite

Tier 4 (Gemma open-weights, quotas indépendants)
10. gemma-3-27b-it
11. gemma-3-12b-it
12. gemma-3-9b-it
13. gemma-3-4b-it
14. gemma-3-1b-it

... (et ~24 autres fallbacks jusqu'à épuisement total)
```

À chaque fois qu'un modèle hit son quota, le programme bascule **automatiquement** au suivant. Pipeline ne tombe que si tous les ~38 modèles sont saturés (extrêmement rare).

Pour le **résumé exécutif** (1 seul appel haute valeur), le programme essaie en priorité les modèles **Pro** (qualité maximale) puis bascule.

---

## 🔧 Edition avancée des cibles

Le menu d'édition (lance `python main.py` puis choisis « modifier les cibles ») a 14 actions, organisées en couleurs :

| Action | Couleur | Description |
|---|---|---|
| 1 / 2 | vert / jaune | Ajouter / Supprimer une **entreprise** (industriel, couplée avec keywords sur GNews) |
| 3 / 4 | vert / jaune | Ajouter / Supprimer un **mot-clé COUPLÉ** (× entreprises sur GNews + broadcast science) |
| 5 / 6 | magenta / jaune | Ajouter / Supprimer un **mot-clé SOLO** (phrase cherchée seule, broadcast partout) |
| 7 / 8 | bleu / jaune | Ajouter / Supprimer un **labo / organisme** de recherche (broadcast science uniquement) |
| 9 / 10 | violet / jaune | Ajouter / Supprimer un **thème CROSS-DOMAINE** (photonique, MEMS, biomim, transferable PVD/ALD) |
| **11** | **cyan** | **🔍 Revoir les acteurs DÉCOUVERTS automatiquement** (validation interactive) |
| 12 | cyan | Revoir la liste actuelle complète |
| 13 (défaut) | vert | Sauvegarder et continuer |
| 14 | jaune | Quitter sans sauvegarder |

**Bonne veille technologique 🛰️**
