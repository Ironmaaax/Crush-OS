# Copyright (C) 2026 Maxime Song
# This file is part of CRUSH-OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Les modes d'approbation vivent hors du dépôt, et survivent à la migration.

L'état était persisté dans `config/approvals.json`, fichier **suivi par git**.
Chaque décision prise depuis l'interface salissait le dépôt, et le prochain
`git pull` sur la Pi partait en conflit sur un fichier que personne n'avait
édité à la main. Ces tests verrouillent le nouvel emplacement, la reprise des
choix existants, et le fait qu'une configuration abîmée ne bloque pas le
démarrage.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import crush.kernel.approvals as mod
from crush.kernel.approvals import ApprovalConfig, ApprovalMode


@pytest.fixture
def emplacements(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Isole les deux chemins : jamais d'écriture dans le vrai dépôt."""
    nouveau = tmp_path / "memory_data" / "approvals.json"
    ancien = tmp_path / "config" / "approvals.json"
    ancien.parent.mkdir(parents=True)
    monkeypatch.setattr(mod, "CONFIG_FILE", nouveau)
    monkeypatch.setattr(mod, "_ANCIEN_FICHIER", ancien)
    return nouveau, ancien


def test_le_fichier_vit_hors_du_depot() -> None:
    """L'emplacement réel doit être sous memory_data/, qui est gitignoré."""
    from crush.kernel.paths import CONFIG_DIR, MEMORY_DATA_DIR

    assert mod.CONFIG_FILE.parent == MEMORY_DATA_DIR
    assert mod.CONFIG_FILE.parent != CONFIG_DIR


def test_les_choix_existants_sont_repris(emplacements: tuple[Path, Path]) -> None:
    """Le cœur de la migration : ne rien perdre d'une installation en service."""
    nouveau, ancien = emplacements
    ancien.write_text(
        json.dumps({"email_send": "always", "file_delete": "never"}),
        encoding="utf-8",
    )

    config = mod.load_approval_config()

    assert config.email_send == ApprovalMode.ALWAYS
    assert config.file_delete == ApprovalMode.NEVER
    assert nouveau.exists(), "la reprise doit écrire au nouvel emplacement"


def test_l_ancien_fichier_n_est_pas_supprime(emplacements: tuple[Path, Path]) -> None:
    """Nettoyer le dépôt est un geste git, pas une suppression sous les pieds."""
    _, ancien = emplacements
    ancien.write_text(json.dumps({"email_send": "always"}), encoding="utf-8")

    mod.load_approval_config()

    assert ancien.exists()


def test_le_nouveau_fichier_prime_sur_l_ancien(emplacements: tuple[Path, Path]) -> None:
    """Une fois migré, l'ancien fichier ne doit plus jamais être consulté.

    Sans cette précédence, un `git pull` ramenant l'ancien fichier écraserait
    des décisions prises depuis.
    """
    nouveau, ancien = emplacements
    nouveau.parent.mkdir(parents=True, exist_ok=True)
    nouveau.write_text(json.dumps({"email_send": "never"}), encoding="utf-8")
    ancien.write_text(json.dumps({"email_send": "always"}), encoding="utf-8")

    assert mod.load_approval_config().email_send == ApprovalMode.NEVER


def test_aucun_des_deux_fichiers_donne_les_defauts(emplacements: tuple[Path, Path]) -> None:
    config = mod.load_approval_config()

    assert config == ApprovalConfig()


def test_fichier_abime_ne_bloque_pas_le_demarrage(emplacements: tuple[Path, Path]) -> None:
    """Une approbation illisible ne doit pas empêcher le service de démarrer."""
    nouveau, _ = emplacements
    nouveau.parent.mkdir(parents=True, exist_ok=True)
    nouveau.write_text("{ ceci n'est pas du JSON", encoding="utf-8")

    assert mod.load_approval_config() == ApprovalConfig()


def test_valeur_hors_domaine_ignoree(emplacements: tuple[Path, Path]) -> None:
    """Un mode inconnu retombe sur le défaut au lieu de lever un ValueError.

    L'ancienne version faisait `ApprovalMode(v)` sans filtre : une valeur
    falsifiée faisait perdre TOUTE la configuration, pas seulement sa clé.
    """
    nouveau, _ = emplacements
    nouveau.parent.mkdir(parents=True, exist_ok=True)
    nouveau.write_text(
        json.dumps({"email_send": "peut-etre", "file_delete": "never"}),
        encoding="utf-8",
    )

    config = mod.load_approval_config()

    assert config.email_send == ApprovalConfig().email_send
    assert config.file_delete == ApprovalMode.NEVER


def test_ecriture_atomique_sans_residu(emplacements: tuple[Path, Path]) -> None:
    nouveau, _ = emplacements
    mod.save_approval_config(ApprovalConfig(email_send=ApprovalMode.NEVER))

    assert json.loads(nouveau.read_text(encoding="utf-8"))["email_send"] == "never"
    assert not list(nouveau.parent.glob("*.tmp")), "aucun temporaire ne doit rester"


# ── Écriture de secrets ──────────────────────────────────────────────────────


def test_ecriture_atomique_restreint_les_droits(tmp_path: Path) -> None:
    """Un jeton OAuth naissait en 644, lisible par tout compte de la machine.

    Le rafraîchissement en aval restreignait bien ses droits ; l'écriture
    INITIALE, faite par le callback OAuth, ne le faisait pas. La protection ne
    valait donc que pour les jetons déjà renouvelés au moins une fois.
    """
    import os
    import stat

    from crush.kernel.persistance import ecrire_atomique

    cible = tmp_path / "secret.json"
    ecrire_atomique(cible, '{"refresh_token": "x"}', mode=0o600)

    droits = stat.S_IMODE(cible.stat().st_mode)
    if os.name != "nt":  # Windows n'applique pas les permissions POSIX
        assert droits == 0o600, f"droits {oct(droits)} — le secret est lisible par d'autres"
    assert cible.read_text(encoding="utf-8") == '{"refresh_token": "x"}'
    assert not list(tmp_path.glob("*.tmp"))
