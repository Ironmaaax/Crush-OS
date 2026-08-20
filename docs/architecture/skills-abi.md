# Skills ABI — namespace public `skills.*`

Les skills installés dans `skills_data/installed/<skill-name>/skill.py` contiennent
du code utilisateur de la forme :

```python
from skills.base import SkillBase, PresetSkill

class MyCoolSkill(SkillBase):
    ...
```

Ce code ne fait PAS partie du package `crush-os` — il vit dans le répertoire de
données utilisateur, est portable (Skill Lab, catalogue Crush-skills, partage
communautaire), et **doit continuer à fonctionner après toute évolution interne
du package**.

À cet effet, le namespace `skills.*` est traité comme une **API publique stable**
(ABI), pas comme un détail d'implémentation.

## Garanties

Le package `crush-os` garantit la disponibilité des symboles suivants via le
namespace `skills.*` :

| Symbole | Définition réelle | Statut |
|---|---|---|
| `skills.base.SkillBase` | `crush.capabilities.skills.base.SkillBase` | **STABLE** |
| `skills.base.PresetSkill` | `crush.capabilities.skills.base.PresetSkill` | **STABLE** |

L'alias est posé par `src/crush/capabilities/skills/_abi_compat.py` au tout
premier import du package `crush.capabilities.skills`. Il survit à toute
réorganisation interne du package.

## Comment c'est fait

`_abi_compat.py` enregistre l'alias dans `sys.modules` :

```python
import sys
import crush.capabilities.skills as _skills_pkg
sys.modules.setdefault("skills", _skills_pkg)

from crush.capabilities.skills import base as _base
sys.modules.setdefault("skills.base", _base)
```

`setdefault` (et non `=`) protège contre une éventuelle pré-existence d'un
module `skills` (cas où l'utilisateur aurait son propre package `skills` dans
son PYTHONPATH — il garde alors la priorité).

## Politique de dépréciation

- **Aucune dépréciation des symboles stables ci-dessus sans version majeure**
  de `crush-os`.
- Une version majeure qui supprime ces symboles doit fournir un outil de
  migration automatique des skills installés (rewrite de `from skills.base
  import ...` vers le nouveau namespace).

## Pourquoi pas juste un import direct ?

On pourrait demander aux skills d'utiliser `from crush.capabilities.skills.base
import SkillBase` directement. Trois raisons de garder l'ABI `skills.*` :

1. **Stabilité du contrat** : un utilisateur qui partage son skill ne devrait
   pas avoir à mettre à jour son code à chaque refactor interne de `crush-os`.
2. **Portabilité** : un skill copié depuis un autre laptop ou un catalogue
   doit fonctionner sans préalable.
3. **Compatibilité ascendante** : les 8 skills installés au moment de la refonte
   (Phase B) ont déjà cette forme — les réécrire serait modifier du code
   utilisateur, ce que le CDC §0 règle 5 interdit pour les composants
   utilisateur.

## GATE C9

Le `noqa: F401` de l'import side-effect dans `_abi_compat.py` est **exclu
nommément** du GATE C9 (« zéro ré-export résiduel »). Voir CDC §C.2 GATE C9 :
l'alias ABI n'est pas un résidu de migration, c'est un contrat permanent.
