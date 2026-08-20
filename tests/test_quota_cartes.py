# Copyright (C) 2026 Maxime Song
# This file is part of CRUSH-OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Plafond mensuel de chargements de carte.

Mapbox offre 50 000 chargements par mois puis facture. Une page laissée ouverte
qui recharge en boucle transforme un palier confortable en facture, sans que
rien ne prévienne. Le garde-fou vit côté serveur parce que c'est lui qui
distribue le jeton : sans jeton, aucune carte ne se charge.
"""

from __future__ import annotations

import json
from pathlib import Path

from crush.kernel.quota_cartes import consommer, etat


def test_le_compteur_part_de_zero(tmp_path: Path) -> None:
    e = etat(plafond=100, chemin=tmp_path / "q.json")

    assert e.chargements == 0
    assert e.depasse is False


def test_chaque_remise_de_jeton_compte(tmp_path: Path) -> None:
    cible = tmp_path / "q.json"
    for attendu in (1, 2, 3):
        assert consommer(plafond=100, chemin=cible).chargements == attendu


def test_le_plafond_bloque(tmp_path: Path) -> None:
    cible = tmp_path / "q.json"
    for _ in range(3):
        e = consommer(plafond=3, chemin=cible)

    assert e.depasse is True
    assert e.restants == 0


def test_le_depassement_continue_de_compter(tmp_path: Path) -> None:
    """Savoir DE COMBIEN on a dépassé vaut mieux qu'un compteur figé.

    Si le plafond est relevé plus tard, un compteur bloqué à la limite
    rouvrirait la carte alors que la consommation réelle est bien au-delà.
    """
    cible = tmp_path / "q.json"
    for _ in range(5):
        e = consommer(plafond=2, chemin=cible)

    assert e.chargements == 5


def test_plafond_nul_desactive_le_garde_fou(tmp_path: Path) -> None:
    """0 ne peut pas vouloir dire « aucune carte » : personne ne le souhaite."""
    cible = tmp_path / "q.json"
    for _ in range(50):
        e = consommer(plafond=0, chemin=cible)

    assert e.depasse is False
    assert e.restants == -1


def test_changement_de_mois_remet_a_zero(tmp_path: Path) -> None:
    """Le palier Mapbox se réinitialise chaque mois, le compteur aussi."""
    cible = tmp_path / "q.json"
    cible.write_text(json.dumps({"mois": "2020-01", "chargements": 49_000}), encoding="utf-8")

    assert etat(plafond=100, chemin=cible).chargements == 0


def test_compteur_abime_ne_bloque_pas_la_carte(tmp_path: Path) -> None:
    """Un fichier corrompu doit repartir de zéro, pas condamner la vue."""
    cible = tmp_path / "q.json"
    for contenu in ["{ pas du JSON", '["une liste"]', '{"chargements": "beaucoup"}']:
        cible.write_text(contenu, encoding="utf-8")
        assert etat(plafond=100, chemin=cible).chargements == 0


def test_disque_en_lecture_seule_laisse_passer(
    tmp_path: Path, monkeypatch: object
) -> None:
    """Ne pas pouvoir écrire le compteur ne doit pas casser l'affichage."""
    import crush.kernel.quota_cartes as mod

    def _refuse(*_a: object, **_k: object) -> None:
        raise OSError("disque en lecture seule")

    monkeypatch.setattr(mod, "ecrire_atomique", _refuse)  # type: ignore[attr-defined]
    e = consommer(plafond=100, chemin=tmp_path / "q.json")

    assert e.chargements == 1  # compté en mémoire, simplement pas persisté
