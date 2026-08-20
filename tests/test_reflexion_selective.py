# Copyright (C) 2026 Maxime Song
# This file is part of CRUSH-OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Arbitrage voie rapide / raisonnement sur le canal vocal.

L'enjeu est symétrique : un faux négatif fait répondre vite et à côté sur une
question de fond, un faux positif ajoute plusieurs secondes de silence à un
« allume la lumière ». Les deux cas sont couverts ici.
"""

from __future__ import annotations

import pytest

from crush.engine.reflection import needs_reflection


@pytest.mark.parametrize(
    "message",
    [
        "compare le train et l'avion pour aller à Marseille",
        "pourquoi le ciel est bleu au juste",
        "explique-moi comment fonctionne un moteur diesel",
        "vaut-il mieux louer ou acheter dans ma situation",
        "quels sont les avantages et les inconvénients de cette approche",
        "combien de temps il me faudrait pour apprendre le piano",
        "aide-moi à choisir entre les deux offres qu'on m'a faites",
        "quelle est la meilleure façon d'organiser ma semaine",
    ],
)
def test_questions_de_fond_declenchent_la_reflexion(message: str) -> None:
    assert needs_reflection(message) is True


@pytest.mark.parametrize(
    "message",
    [
        "bonjour",
        "quelle heure il est",
        "merci beaucoup pour ton aide",
        "raconte-moi une blague sur les développeurs",
        "il fait quel temps demain à Paris",
        "rappelle-moi d'appeler le dentiste demain matin",
    ],
)
def test_conversation_courante_reste_sur_la_voie_rapide(message: str) -> None:
    assert needs_reflection(message) is False


@pytest.mark.parametrize(
    "message",
    [
        "lance la playlist que j'écoutais hier soir chez Paul",
        "mets de la musique pour travailler, quelque chose de calme",
        "allume la lumière du salon et baisse un peu le chauffage",
        "arrête la musique s'il te plaît",
    ],
)
def test_les_commandes_ne_declenchent_jamais_la_reflexion(message: str) -> None:
    """Une commande reste une commande, même longue et même bien tournée.

    Ce sont les tours où la latence se remarque le plus : l'utilisateur attend
    un effet immédiat, pas une réponse.
    """
    assert needs_reflection(message) is False


def test_relance_courte_ne_declenche_pas() -> None:
    """« pourquoi ? » seul relance la conversation, il ne demande pas d'analyse."""
    assert needs_reflection("pourquoi ?") is False
    assert needs_reflection("et sinon ?") is False


def test_message_vide_ne_declenche_pas() -> None:
    assert needs_reflection("") is False
    assert needs_reflection("   ") is False
