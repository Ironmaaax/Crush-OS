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

---

### Standard agentskills.io
**Site** : https://agentskills.io  
**GitHub** : https://github.com/agentskills/agentskills  
** ** : Open Standard (contributions communautaires)  
**Origine** : Anthropic, ouvert à l'écosystème

Le format `SKILL.md` avec frontmatter YAML (champs `name`, `description`,
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
