# Comment fonctionne le système de score

Document destiné aux personnes qui reçoivent les emails de la veille et qui veulent comprendre **pourquoi tel article a 5 étoiles et tel autre seulement 2**.

---

## 1. À quoi sert le score ?

Chaque article scrapé est noté de **1 à 5 étoiles** par une intelligence artificielle (Google Gemini), selon son intérêt technique et stratégique pour ton activité (revêtements industriels, PVD, CVD, ALD, science des surfaces).

Le score permet :
- **De classer les articles dans l'email** : les plus importants apparaissent en premier
- **De filtrer le bruit** : par défaut, seuls les articles à ★★ ou plus sont affichés
- **De repérer rapidement l'essentiel** : tu peux te limiter aux ★★★★ et ★★★★★ si tu manques de temps

---

## 2. Les 5 niveaux de score

| Score | Étoiles | Libellé | Signification concrète |
|---|---|---|---|
| **5** | ★★★★★ | **Percée majeure** | Innovation prête pour l'industrie. Impact direct et immédiat. À lire en priorité absolue. |
| **4** | ★★★★ | **Innovation solide** | Résultats mesurables publiés, méthode reproductible. À suivre de près. |
| **3** | ★★★ | **À surveiller** | Avancée intéressante mais encore académique ou préliminaire. À garder en tête. |
| **2** | ★★ | **Signal faible** | Tendance émergente, peu de données chiffrées. Indice de mouvement de marché ou de recherche. |
| **1** | ★ | **Note** | Mentionné pour exhaustivité, faible valeur immédiate. Souvent filtré du digest. |

> Dans l'email : les étoiles **dorées** sont pleines (acquises), les **grises** sont vides (manquées). Un article 3★ a donc 3 étoiles dorées + 2 étoiles grises.

---

## 3. Comment l'IA décide-t-elle ?

L'IA (Gemini Flash) reçoit pour chaque article :
- Le **titre**
- Le **résumé** (max 400 caractères)
- La **source** (ArXiv, MDPI, Google News...)

Et elle applique une grille d'analyse précise :

### Critères qui font monter le score
- Nouvelle **couleur ou effet visuel** obtenu par dépôt physique (PVD, PECVD, sputtering)
- Nouvel **alliage ou matériau cible** pour revêtements durs ou décoratifs
- Avancée en **ALD** : nouveaux précurseurs, températures, cycles
- Nouvelle **barrière laser, revêtement optique ou antireflet**
- Nouveaux **paramètres procédé** PVD/CVD/ALD (pression, polarisation, débit gaz)
- Nouvelle **propriété mesurée** : dureté, adhérence, corrosion, frottement

### Critères qui font baisser le score (ou rejettent l'article)
- **Marketing pur** sans contenu technique
- **Hors-sujet** : biologie cellulaire, chimie organique sans lien avec les surfaces
- **Doublons conceptuels** d'articles déjà connus

---

## 4. La règle spéciale pour les concurrents

Cette règle est **non-négociable** et appliquée systématiquement :

> Si un article mentionne **une des 21 entreprises listées dans `data/targets.json`** (Oerlikon, Ionbond, Platit, Evatec, Beneq, Fraunhofer, CEA-Leti, etc.), alors :
>
> 1. ✅ L'article est **toujours retenu**, même s'il semble partiellement marketing
> 2. ✅ Le score est **minimum 4** (Innovation solide)
> 3. ✅ Le score est **5** si on découvre un nouveau produit/procédé/brevet de ce concurrent
> 4. ✅ Le nom du concurrent apparaît dans la justification et dans les tags

**Pourquoi cette règle ?** Parce que rater un mouvement de concurrent direct (nouveau brevet, nouveau procédé déposé, nouveau marché attaqué) coûte plus cher que de lire un communiqué légèrement promotionnel.

---

## 5. Le filet de sécurité Python (double-vérification)

L'IA peut se tromper ou oublier la règle concurrents. Donc **après** son analyse, le programme exécute une **deuxième vérification automatique en Python** (`_force_company_scores`) :

1. Pour chaque article retenu, le code cherche le nom des 21 concurrents dans le titre et le résumé (recherche **insensible à la casse**)
2. Si match trouvé et que le score IA est < 4 → **score forcé à 4**
3. Si le tag de l'entreprise n'a pas été ajouté par l'IA → **ajouté automatiquement**

**Conséquence** : aucun article concurrent ne peut passer sous les ★★★★, même si l'IA a sous-évalué.

---

## 6. Exemples concrets

Voici des cas réels du dernier digest et leur logique de notation :

| Score | Titre | Pourquoi ce score |
|---|---|---|
| ★★★★★ | « Novel Metal Diboride Coatings via LPCVD » | Synthèse de matériaux ultra-durs avec procédé reproductible publié → percée |
| ★★★★★ | « MXene breakthrough boosts conductivity 160x » | Saut quantitatif (×160) + protocole nouveau → impact industriel direct |
| ★★★★ | « Optimization of Corrosion Resistance in CrAlN » | Méthode optimisée + données chiffrées + applications eau de mer → solide |
| ★★★★ | « GaN Epitaxial Strategy on Silicon » | Innovation procédé claire mais reste de niche → solide sans être percée |
| ★★★ | Article ArXiv théorique sur simulation DFT | Avancée académique, pas de transfert industriel immédiat → à surveiller |
| ★★ | Communiqué Google News sans détails techniques | Signal faible : indique un mouvement mais pas de substance |

---

## 7. Comment ajuster ce qui apparaît dans l'email

Trois leviers pour personnaliser ce que tu reçois :

### a. Le seuil minimum d'affichage dans l'email

Dans le fichier `.env` :

```env
MAIL_MIN_SCORE=2     # défaut : affiche les ★★ et plus
MAIL_MIN_SCORE=4     # mode "élite" : uniquement ★★★★ et ★★★★★
MAIL_MIN_SCORE=1     # mode "exhaustif" : tout, même les ★
```

Les articles en-dessous du seuil restent dans `data/ai_filter_output.json` (et dans l'archive) mais n'apparaissent pas dans le mail.

### b. Le seuil minimum de filtrage IA

Dans `main.py` (déjà appelé avec `min_score=2`) : tout article noté < 2 par l'IA est totalement écarté en amont. Tu peux passer à `min_score=3` si tu veux que l'archive elle-même ne contienne que les articles à 3★ et plus.

### c. La liste des concurrents

Édite `data/targets.json` pour ajouter/retirer des entreprises. Toute entreprise listée déclenche la **règle ★★★★ minimum**.

---

## 8. Questions fréquentes

**Q : Pourquoi un article qui mentionne mon concurrent a-t-il "seulement" 4★ ?**
R : Parce que l'IA n'y a pas vu de nouveau produit/procédé/brevet — c'est sans doute un communiqué général ou une mention dans un article tiers. Le filet de sécurité a forcé à 4★ minimum, mais ne monte à 5★ que si l'innovation est explicite.

**Q : Pourquoi un article ne contient-il pas de note 5★ cette semaine ?**
R : Les ★★★★★ sont rares par construction — l'IA est calibrée pour réserver ce score aux vraies percées. Une semaine sans 5★ est normale, surtout sur un sujet de niche.

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

## 9. Résumé en une phrase

> Plus le score est élevé, plus l'article décrit une innovation **technique, mesurable et prête à être appliquée** — avec un coup de pouce systématique pour tout ce qui mentionne un concurrent direct.
