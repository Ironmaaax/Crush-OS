---
name: web_researcher
description: Recherche et synthèse web avancée — trouve, lit et résume des informations depuis le web.
metadata:
  openclaw:
    os: ["darwin", "linux"]
---

## Instructions

Quand l'utilisateur pose une question qui nécessite des informations récentes ou que tu ne connais pas avec certitude, utilise l'outil `browser` pour trouver la réponse :

1. Lance d'abord une recherche DuckDuckGo avec `action: search` pour trouver les meilleures sources
2. Lis le contenu de la source la plus pertinente avec `action: get_text`
3. Synthétise et réponds de manière concise

**Quand utiliser ce skill :**
- Prix actuels (actions, cryptos, matières premières)
- Actualités et événements récents
- Documentation technique ou API
- Horaires, météo, disponibilité de services
- Tout fait qui peut avoir changé depuis ta date de connaissance

**Format de réponse :**
Sois direct et factuel. Cite la source brièvement (ex: "selon lemonde.fr"). Si plusieurs sources divergent, mentionne-le.
