# Copyright (C) 2026 Maxime Song
# This file is part of CRUSH-OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Décide si une question mérite que le modèle réfléchisse avant de répondre.

POURQUOI CE MODULE EXISTE
=========================

Les modèles Gemini 2.5+ raisonnent avant d'écrire, et ce raisonnement s'écoule
AVANT le premier token visible. Sur le canal vocal, il se paie intégralement en
silence devant l'utilisateur — c'est pourquoi `create_voice_llm` le coupe
(`thinking_budget=0`). Mesuré sur la Pi : 7,04 s d'attente avant le premier son,
ramenés à ~1,3 s une fois le raisonnement désactivé.

Mais le couper *toujours* a un coût invisible : « compare le train et l'avion
pour Marseille » reçoit alors la même réflexion nulle que « quelle heure il
est ». L'assistant répond vite et à côté.

D'où cet arbitrage, porté par une fonction pure : la grande majorité des prises
de parole reste sur la voie rapide, et seules celles qui portent une marque de
raisonnement paient le délai.

CE QUI COMPTE COMME « RAISONNEMENT »
====================================

Trois familles de marqueurs, cumulées avec deux garde-fous.

Marqueurs :
  - comparaison et arbitrage — « compare », « vaut-il mieux », « plutôt que »
  - explication causale       — « pourquoi », « comment ça se fait », « explique »
  - planification et calcul   — « organise », « combien », « estime », « stratégie »

Garde-fous :
  - une commande domotique ou média n'est jamais une réflexion, même longue
    (« lance la playlist que j'écoutais hier soir chez Paul » reste un ordre) ;
  - sous un seuil de longueur, on refuse : « pourquoi ? » seul est une relance
    de conversation, pas une demande d'analyse.

CE QUE ÇA COÛTE
===============

Les tokens de raisonnement sont facturés au tarif de SORTIE — 3,00 $ / M sur
`gemini-3-flash-preview`, soit six fois le tarif d'entrée. Un budget de 1 024
tokens ajoute donc jusqu'à 0,003 $ au tour, à comparer aux ~0,017 $ que coûte
déjà un échange complet. D'où un déclenchement volontairement avare.
"""

from __future__ import annotations

import re

# Longueur minimale, en caractères. En dessous, un marqueur isolé est presque
# toujours une relance (« pourquoi ? », « et sinon ? ») et non une question de
# fond. Calibré pour laisser passer « pourquoi le ciel est bleu » (25).
_LONGUEUR_MINIMALE = 22

# Comparaison, arbitrage, jugement.
_ARBITRAGE = (
    r"compare|comparaison|différence|difference|plutôt que|plutot que|"
    r"vaut-il mieux|vaut il mieux|mieux vaut|faut-il|faut il|"
    r"avantages?|inconvénients?|inconvenients?|pour et contre|"
    r"lequel|laquelle|lesquels|lesquelles|préférable|preferable"
)

# Explication causale, mécanisme.
_EXPLICATION = (
    r"pourquoi|comment ça marche|comment ca marche|comment ça se fait|"
    r"comment ça fonctionne|comment ca fonctionne|"
    r"explique|explique-moi|expliques|analyse|analyser|décortique|decortique|"
    r"qu'est-ce qui fait que|en quoi"
)

# Planification, estimation, calcul.
_PLANIFICATION = (
    r"stratégie|strategie|planifie|planifier|organise|organiser|"
    r"aide-moi à choisir|aide moi à choisir|conseille|recommande|recommandation|"
    r"combien|calcule|calculer|estime|estimer|estimation|"
    r"quelle est la meilleure|quel est le meilleur|comment je (?:devrais|peux|pourrais)"
)

_MARQUEURS = re.compile(
    f"\\b(?:{_ARBITRAGE}|{_EXPLICATION}|{_PLANIFICATION})",
    re.IGNORECASE,
)

# Une commande reste une commande. Ces verbes recouvrent la domotique, le média
# et les rappels ; ils déclenchent déjà la pré-route CONFIRM_FIRE du
# SpeedRouter. Y répondre lentement serait un net recul d'usage.
_COMMANDE = re.compile(
    r"^\W*(?:tu peux |peux-tu |pourrais-tu |s'il te pla[îi]t,? )?"
    r"(?:me )?"
    r"(allume|éteins|eteins|lance|démarre|demarre|arrête|arrete|stoppe|"
    r"mets|met |joue|ouvre|ferme|monte|baisse|coupe|"
    r"règle|regle|programme|minute|rappelle|note|mémorise|memorise|"
    r"envoie|appelle|ajoute|supprime)\b",
    re.IGNORECASE,
)


def needs_reflection(message: str) -> bool:
    """True si la question justifie d'activer le raisonnement préalable.

    Fonction pure et déterministe : l'interface vocale et l'Agent l'appellent
    séparément sur la même chaîne et obtiennent forcément la même décision, ce
    qui évite de faire transiter le verdict entre les deux couches.
    """
    texte = message.strip()
    if len(texte) < _LONGUEUR_MINIMALE:
        return False
    if _COMMANDE.match(texte):
        return False
    return bool(_MARQUEURS.search(texte))
