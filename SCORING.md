# Comment fonctionne le système de score

Document destiné aux personnes qui reçoivent les emails de la veille et qui veulent comprendre **pourquoi tel article a 5 étoiles et tel autre seulement 2**.

> ⚠️ **Mise à jour 2026** : la philosophie du scoring a évolué. L'IA évalue désormais le **potentiel d'innovation par INTÉGRATION CROSS-DOMAINE**, pas seulement « est-ce que l'article parle de PVD/CVD/ALD ? ». Détails ci-dessous.

---

## 1. À quoi sert le score ?

Chaque article scrapé est noté de **1 à 5 étoiles** par une intelligence artificielle (Google Gemini), selon son potentiel à générer une innovation technique en lien avec tes activités (couches minces PVD/ALD industrielles et décoratives, horlogerie, optique, médical, outils coupants).

Le score permet :
- **De classer les articles dans l'email** : les plus importants apparaissent en premier
- **De filtrer le bruit** : par défaut, seuls les articles à ★★ ou plus sont affichés
- **De repérer rapidement l'essentiel** : tu peux te limiter aux ★★★★ et ★★★★★ si tu manques de temps

---

## 2. La nouvelle philosophie : INNOVATION TRANSFÉRABLE

Le programme cherche à répondre à **une seule question** par article :

> Si on prenait cette technologie/découverte/idée et qu'on l'appliquait via dépôt PVD ou ALD, est-ce que ça créerait quelque chose de nouveau et utile ?

Ce changement permet de capter des innovations qui viennent d'**autres domaines** (photonique, biomimétisme, nanotech, MEMS, IA, métamatériaux) mais qui sont **transférables** à tes procédés. C'est exactement ces innovations transversales que tu loupais avec l'ancienne logique.

### Exemples concrets de pertinence cross-domaine

| Découverte d'origine | Domaine | Application PVD/ALD |
|---|---|---|
| Métasurfaces photoniques | Photonique | Couleurs structurales sans pigment sur cadrans de montre (PVD) |
| Effet lotus / surfaces biomimétiques | Bio-inspiration | Revêtements anti-traces sur outils ou composants |
| Auxétiques / métamatériaux | Mécanique avancée | Revêtements à propriétés mécaniques inédites |
| Machine learning sur croissance films | IA / process control | Optimisation auto des recettes PVD |
| MXene / 2D materials | Nanotech | Nouvelles cibles pour pulvérisation magnétron |
| Quantum dots | Nanotech | Couleurs et effets optiques par ALD |
| Self-assembly monolayers | Nanotech | Couches d'accroche pour PVD |

---

## 3. Les 5 niveaux de score

| Score | Étoiles | Libellé | Signification concrète |
|---|---|---|---|
| **5** | ★★★★★ | **Transférable directement** | Technique mature dans son domaine, intégration immédiate avec PVD/ALD possible, impact business évident **OU** découverte majeure d'un concurrent listé |
| **4** | ★★★★ | **Pont innovant** | Nécessite adaptation mais le potentiel cross-domaine est clair (ex: metasurfaces → couleurs décoratives via PVD) |
| **3** | ★★★ | **Lecture latérale** | Connexion possible mais pas évidente. À garder en veille pour quand l'idée mûrira |
| **2** | ★★ | **Marginal** | Tangent au sujet, peu probable de transfert direct |
| **1** | ★ | **Hors-sujet** | Aucune connexion crédible avec dépôts en couches minces |

> Dans l'email : les étoiles **dorées** sont pleines (acquises), les **grises** sont vides (manquées). Un article 3★ a donc 3 étoiles dorées + 2 étoiles grises.

---

## 4. Comment l'IA décide-t-elle ?

L'IA (Gemini Flash) reçoit pour chaque article :
- Le **titre**
- Le **résumé** (max 600 caractères)
- La **source** (ArXiv, MDPI, OpenAlex, Patents, Google News...)

Et elle applique la grille d'évaluation suivante :

### Critères qui font monter le score

- ✅ **Lien direct PVD/CVD/ALD** : nouvelle couleur, nouveau matériau cible, nouveau précurseur, nouveau paramètre procédé
- ✅ **Pont cross-domaine** : technique d'un autre domaine (photonique, MEMS, biomim, nanotech) avec un angle d'intégration crédible vers tes dépôts
- ✅ **Mention d'un concurrent listé** dans `data/targets.json` (règle automatique ci-dessous)
- ✅ **Mesures chiffrées** : dureté, adhérence, résistance corrosion, gain de réflectance, conductivité
- ✅ **Procédé reproductible** publié (paper avec méthodologie complète, pas un teaser marketing)

### Critères qui font baisser le score (ou rejettent l'article)

- ❌ **Marketing pur** sans contenu technique
- ❌ **Hors-sujet total** : biologie cellulaire pure, économie, sport, politique
- ❌ **Doublons conceptuels** (déjà couvert ailleurs dans le batch)

---

## 5. La règle spéciale pour les concurrents

Cette règle est **non-négociable** et appliquée systématiquement :

> Si un article mentionne **une des entreprises listées dans `data/targets.json`** (Aixtron, Oerlikon Balzers, Lam Research, Tokyo Electron, ULVAC, Applied Materials, Picosun, Beneq, Veeco, Coat-X, etc.), alors :
>
> 1. ✅ L'article est **toujours retenu**, même s'il semble partiellement marketing
> 2. ✅ Le score est **minimum 4** (Pont innovant)
> 3. ✅ Le score est **5** si on découvre un nouveau produit/procédé/brevet de ce concurrent
> 4. ✅ Le nom du concurrent apparaît dans la justification et dans les tags

**Pourquoi cette règle ?** Parce que rater un mouvement de concurrent direct (nouveau brevet, nouveau procédé déposé, nouveau marché attaqué) coûte plus cher que de lire un communiqué légèrement promotionnel.

---

## 6. La justification : où l'IA explique son raisonnement

Dans chaque carte article de l'email, tu vois une ligne « 💡 Justification » qui contient :

- **L'angle d'intégration** : « Les nanostructures plasmoniques décrites peuvent être déposées par PVD pour créer des couleurs structurales sur cadrans »
- **Les acteurs cités** : entreprises ou labos mentionnés, même s'ils ne sont pas dans tes listes (signal de découverte)
- **Le domaine d'application** suggéré quand pertinent (horlogerie, médical, outils, optique)

Cette ligne est précieuse : si tu la trouves convaincante, l'innovation mérite ton attention. Si elle reste vague, le score est probablement à relativiser.

---

## 7. Le filet de sécurité Python (double-vérification)

L'IA peut se tromper ou oublier la règle concurrents. Donc **après** son analyse, le programme exécute une **deuxième vérification automatique en Python** (`_force_company_scores`) :

1. Pour chaque article retenu, le code cherche le nom des entreprises listées dans le titre et le résumé (recherche **insensible à la casse**)
2. Si match trouvé et que le score IA est < 4 → **score forcé à 4**
3. Si le tag de l'entreprise n'a pas été ajouté par l'IA → **ajouté automatiquement**

**Conséquence** : aucun article concurrent ne peut passer sous les ★★★★, même si l'IA a sous-évalué.

---

## 8. Exemples concrets du nouveau scoring

| Score | Titre type | Pourquoi ce score |
|---|---|---|
| ★★★★★ | « Lam Research dépose un brevet sur ALD basse-température pour ICs avancés » | Concurrent listé + nouveau brevet → règle automatique 5★ |
| ★★★★★ | « Metasurfaces enabling structural coloration without dyes » | Pont direct vers couleurs sans pigment sur dépôts PVD → transférable immédiatement |
| ★★★★ | « MXene electrode for next-gen batteries » | Pont innovant : MXene pulvérisable, transfert au PVD plausible avec adaptation |
| ★★★★ | « Bio-inspired antifouling coating from shark skin » | Concept biomimétique adaptable à dépôts décoratifs ou outils |
| ★★★ | « DFT simulation of GaN epitaxial growth » | Avancée académique, applications industrielles encore lointaines |
| ★★ | « Trends in industrial automation 2026 » | Tangent : pas de pointe technique transférable directement |
| ★ | « Stock market update : semiconductor sector » | Hors-sujet, pas de contenu technique pertinent |

---

## 9. Découverte automatique d'acteurs (nouveau)

Au-delà du score, le programme **détecte automatiquement les nouveaux acteurs** (entreprises et labos) qui apparaissent dans les résultats Google Patents et OpenAlex :

- Extraction du **champ `assignee`** des brevets (= déposant)
- Extraction des **affiliations institutionnelles** des auteurs OpenAlex
- Filtrage : seulement ceux qui **ne sont PAS déjà dans tes listes**
- Compteur cumulatif inter-runs : plus un acteur revient, plus c'est un signal fort

→ Section dédiée **« 🔍 Acteurs découverts automatiquement »** en fin d'email.
→ Action 11 du menu d'édition CLI (`python main.py`) : revue interactive pour valider/rejeter.

---

## 10. Comment ajuster ce qui apparaît dans l'email

Trois leviers pour personnaliser ce que tu reçois :

### a. Le seuil minimum d'affichage dans l'email

Dans le fichier `.env` :

```env
MAIL_MIN_SCORE=2     # défaut : affiche les ★★ et plus
MAIL_MIN_SCORE=4     # mode "élite" : uniquement ★★★★ et ★★★★★
MAIL_MIN_SCORE=1     # mode "exhaustif" : tout, même les ★
```

Les articles en-dessous du seuil restent dans `data/ai_filter_output.json` (et dans l'archive) mais n'apparaissent pas dans le mail.

### b. La liste des concurrents (impact 4★ minimum)

Édite `data/targets.json` (champ `companies`) pour ajouter/retirer des entreprises. Toute entreprise listée déclenche la **règle ★★★★ minimum**.

### c. Les thèmes cross-domaine (impact recall)

Édite `data/targets.json` (champ `cross_domain_topics`) pour orienter la veille vers d'autres domaines transverses (ex: ajouter "topological insulator thin film" si tu veux pister cette niche).

---

## 11. Questions fréquentes

**Q : Pourquoi un article qui mentionne mon concurrent a-t-il "seulement" 4★ ?**
R : Parce que l'IA n'y a pas vu de nouveau produit/procédé/brevet — c'est sans doute un communiqué général ou une mention dans un article tiers. Le filet de sécurité a forcé à 4★ minimum, mais ne monte à 5★ que si l'innovation est explicite.

**Q : Un article photonique avec 5★ qui ne parle pas de PVD : est-ce une erreur ?**
R : **Non, c'est exactement le but de la nouvelle philosophie.** Si la justification IA explique comment cette innovation photonique peut être déposée par PVD pour créer une nouveauté décorative, le 5★ est mérité. Lis bien la justification, c'est là que se trouve l'angle d'intégration.

**Q : Pourquoi un article ne contient-il pas de note 5★ cette semaine ?**
R : Les ★★★★★ sont rares par construction — l'IA est calibrée pour réserver ce score aux vraies percées transférables. Une semaine sans 5★ est normale, surtout sur un sujet de niche.

**Q : L'IA peut-elle se tromper ?**
R : Oui. C'est pourquoi :
- Le filet Python rattrape les oublis sur les concurrents
- La justification est toujours affichée dans l'email — tu peux juger toi-même
- Tu peux ajuster le seuil `MAIL_MIN_SCORE` si tu trouves le filtrage trop sévère ou trop laxiste

**Q : Que se passe-t-il si je rajoute une entreprise dans `targets.json` après coup ?**
R : Au prochain run de `main.py`, les articles mentionnant cette entreprise seront automatiquement remontés à ★★★★. Mais les articles **déjà archivés avant** ne sont pas re-notés rétroactivement.

**Q : Est-ce que le score peut changer pour un même article entre deux runs ?**
R : Oui, légèrement — l'IA n'est pas 100% déterministe (température 0.1, presque mais pas tout à fait fixe). Si tu vois un article à 4★ une semaine et 3★ une autre, c'est dû à cette variabilité naturelle du modèle.

---

## 12. Résumé en une phrase

> Plus le score est élevé, plus l'article décrit une innovation **techniquement réelle ET transférable à tes procédés PVD/ALD** — avec un coup de pouce systématique pour tout ce qui mentionne un concurrent direct, et une attention particulière aux découvertes cross-domaine (photonique, biomim, nanotech, IA) qui pourraient devenir tes prochaines innovations.
