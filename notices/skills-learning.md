# Attribution — Boucle d'apprentissage de skills

## Sources d'inspiration

### Hermes Agent — NousResearch
**Dépôt** : https://github.com/NousResearch/hermes-agent  
** ** : MIT  
**Copyright** : NousResearch

Le mécanisme de création autonome de skills (« nudges » de persistance après une tâche
complexe, auto-amélioration à l'usage) est inspiré de l'architecture Hermes Agent.

Patterns réutilisés sous   MIT :
- Structure de skill en dossier (`SKILL.md` + scripts optionnels)
- Déclenchement de la synthèse après une tâche non-triviale
- Amélioration incrémentale du skill avec de nouvelles expériences

**Texte de la   MIT :**

```
MIT License

Copyright (c) NousResearch

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

---

### Standard agentskills.io
**Site** : https://agentskills.io  
**GitHub** : https://github.com/agentskills/agentskills  
** ** : Open Standard (contributions communautaires)  
**Origine** : Anthropic, ouvert à l'écosystème

Le format `SKILL.md` avec frontmatter YAML (champs `name`, `description`, `license`,
`compatibility`, `metadata`, `allowed-tools`) est conforme au standard agentskills.io.

L'adaptateur `skills/standard.py` implémente le format de manifest tel que spécifié dans
la documentation officielle (`/specification`), permettant l'import et l'export de skills
vers/depuis n'importe quel agent compatible (Hermes, Gemini CLI, Claude Code, etc.).

---

## Fichiers concernés

| Fichier | Rôle |
|---|---|
| `skills/synthesizer.py` | Génère des skills depuis des trajectoires de tâches |
| `skills/standard.py` | Adaptateur import/export format agentskills.io |
| `tools/skills.py` | Outils LLM exposés à Crush |
| `prompts/system_static.md` | Section "Apprentissage" (nudge de persistance) |
