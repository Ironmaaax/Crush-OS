"""Rendu des gabarits de prompt — substitution explicite de `{{variable}}`.

POURQUOI CE MODULE
==================

Les prompts étaient rédigés avec des noms propres en dur, puis personnalisés
par substitution de chaîne au moment de les charger :

    static_system = static_system.replace("Max", firstname)
    static_system = static_system.replace("Crush", assistant_name)

Trois défauts. La substitution n'est pas ancrée : elle frappe n'importe quelle
sous-chaîne, y compris à l'intérieur d'un mot. Elle est invisible — rien dans
le fichier de prompt n'indique qu'un mot y est variable. Et elle ne couvrait
qu'un seul fichier, les autres prompts gardant leurs noms figés.

Ici, une variable se déclare `{{user}}` dans le gabarit et se résout à
l'appel. Un placeholder inconnu lève une erreur au lieu de laisser passer un
prompt à moitié rendu, où l'assistant s'adresserait à `{{user}}`.

`str.format` est délibérément écarté : les prompts contiennent des exemples de
JSON et de code, donc des accolades simples que `format` interpréterait.
"""

from __future__ import annotations

import re
from pathlib import Path

_PLACEHOLDER = re.compile(r"\{\{\s*([a-z_][a-z0-9_]*)\s*\}\}")


class UnknownPlaceholderError(KeyError):
    """Le gabarit référence une variable que l'appelant n'a pas fournie."""


def render(template: str, **variables: str) -> str:
    """Remplace chaque `{{nom}}` par la valeur correspondante.

    Lève `UnknownPlaceholderError` si le gabarit cite une variable absente :
    mieux vaut un démarrage bruyant qu'un assistant qui appelle son
    utilisateur « {{user}} » pendant des semaines.
    """
    manquantes: set[str] = set()

    def _remplacer(match: re.Match[str]) -> str:
        nom = match.group(1)
        if nom not in variables:
            manquantes.add(nom)
            return match.group(0)
        return variables[nom]

    rendu = _PLACEHOLDER.sub(_remplacer, template)
    if manquantes:
        raise UnknownPlaceholderError(
            f"Variables non fournies : {', '.join(sorted(manquantes))}"
        )
    return rendu


def render_file(path: Path, **variables: str) -> str:
    """Charge un gabarit et le rend. L'encodage est explicite (cf. Windows)."""
    return render(path.read_text(encoding="utf-8"), **variables)


def placeholders(template: str) -> set[str]:
    """Variables citées par un gabarit — utile pour les tests et le diagnostic."""
    return {m.group(1) for m in _PLACEHOLDER.finditer(template)}
