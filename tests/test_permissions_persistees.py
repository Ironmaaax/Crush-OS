# Copyright (C) 2026 Maxime Song
# This file is part of CRUSH-OS,   .
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Persistance des permissions runtime (kernel.permissions + API /api/permissions).

Le défaut corrigé : l'état ne vivait qu'en mémoire, donc tout redémarrage du
service reverrouillait screen/camera/files. Ces tests verrouillent le contrat
inverse — survie au redémarrage, dégradation propre, écriture atomique — et
l'honnêteté de l'API (404 sur clé inconnue, avertissement si le disque refuse).

Aucun test ne touche le fichier réel `memory_data/permissions.json` : tous les
stores testés reçoivent un chemin sous `tmp_path`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from crush.interfaces.api.config import permissions as api_perms
from crush.kernel import permissions as kernel_perms
from crush.kernel import persistance as kernel_persistance
from crush.kernel.paths import MEMORY_DATA_DIR
from crush.kernel.permissions import (
    PERMISSION_KEYS,
    STATE_FILE,
    PermissionStore,
)

# ── Helpers ───────────────────────────────────────────────────────────────────


def _fichier(tmp_path: Path) -> Path:
    return tmp_path / "permissions.json"


def _client(store: PermissionStore, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Monte le router sur une mini-app, branché sur un store jetable."""
    monkeypatch.setattr(api_perms, "_perm_store", store)
    app = FastAPI()
    app.include_router(api_perms.router)
    return TestClient(app)


def _explose(*_: object, **__: object) -> None:
    raise OSError("disque plein")


# ── Survie au redémarrage ─────────────────────────────────────────────────────


def test_permission_relue_par_un_nouveau_store(tmp_path: Path) -> None:
    """Le cas du bug : cocher « fichiers » puis redémarrer le service."""
    path = _fichier(tmp_path)
    PermissionStore(path).set("files", True)

    apres_redemarrage = PermissionStore(path)

    assert apres_redemarrage.get("files") is True


def test_revocation_persistee_aussi(tmp_path: Path) -> None:
    path = _fichier(tmp_path)
    store = PermissionStore(path)
    store.set("files", True)
    store.set("files", False)

    assert PermissionStore(path).get("files") is False


def test_les_cles_non_touchees_gardent_leur_defaut(tmp_path: Path) -> None:
    path = _fichier(tmp_path)
    PermissionStore(path).set("files", True)

    rechargé = PermissionStore(path)

    assert rechargé.get("screen") is False
    assert rechargé.get("camera") is False
    assert rechargé.get("microphone") is True


def test_fichier_ecrit_lisible_a_la_main(tmp_path: Path) -> None:
    path = _fichier(tmp_path)
    PermissionStore(path).set("screen", True)

    data = json.loads(path.read_text(encoding="utf-8"))

    assert data == {"camera": False, "files": False, "microphone": True, "screen": True}


@pytest.mark.permissions_reelles
def test_le_singleton_ecrit_dans_le_repertoire_de_donnees() -> None:
    """Un choix propre à la machine : dans memory_data/ (gitignoré), pas config/."""
    assert STATE_FILE == MEMORY_DATA_DIR / "permissions.json"
    # Attribut privé assumé : c'est la seule preuve que le singleton exporté
    # est bien persistant, et c'était précisément le défaut d'origine.
    assert kernel_perms.permissions._path == STATE_FILE


# ── Dégradation propre ────────────────────────────────────────────────────────


def test_fichier_absent_retombe_sur_les_defauts(tmp_path: Path) -> None:
    store = PermissionStore(_fichier(tmp_path))

    assert store.all() == {"microphone": True, "screen": False, "camera": False, "files": False}


def test_json_corrompu_ne_fait_pas_tomber_le_service(tmp_path: Path) -> None:
    path = _fichier(tmp_path)
    path.write_text('{"files": tru', encoding="utf-8")

    store = PermissionStore(path)

    assert store.get("files") is False


@pytest.mark.parametrize("contenu", ["[]", '"files"', "null", "42"])
def test_json_valide_mais_pas_un_objet(tmp_path: Path, contenu: str) -> None:
    path = _fichier(tmp_path)
    path.write_text(contenu, encoding="utf-8")

    assert PermissionStore(path).get("files") is False


def test_cles_inconnues_et_types_faux_sont_ignores(tmp_path: Path) -> None:
    path = _fichier(tmp_path)
    path.write_text(
        json.dumps({"files": True, "screen": "oui", "root": True}),
        encoding="utf-8",
    )

    store = PermissionStore(path)

    assert store.get("files") is True
    assert store.get("screen") is False  # "oui" n'est pas un booléen → défaut
    assert set(store.all()) == set(PERMISSION_KEYS)


def test_fichier_corrompu_est_reecrit_proprement_au_prochain_toggle(tmp_path: Path) -> None:
    path = _fichier(tmp_path)
    path.write_text("{{{", encoding="utf-8")

    PermissionStore(path).set("camera", True)

    assert json.loads(path.read_text(encoding="utf-8"))["camera"] is True


# ── Écriture atomique ─────────────────────────────────────────────────────────


def test_aucun_temporaire_ne_survit_a_une_ecriture(tmp_path: Path) -> None:
    PermissionStore(_fichier(tmp_path)).set("files", True)

    assert list(tmp_path.glob("*.tmp")) == []


def test_echec_en_cours_d_ecriture_laisse_l_etat_precedent_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Contrat d'atomicité : jamais de JSON à moitié écrit sur le disque."""
    path = _fichier(tmp_path)
    store = PermissionStore(path)
    store.set("files", True)

    # L'ecriture atomique a ete mutualisee dans kernel.persistance : c'est la
    # que os.replace est appele, plus dans permissions.py.
    monkeypatch.setattr(kernel_persistance.os, "replace", _explose)
    store.set("screen", True)

    sur_disque = json.loads(path.read_text(encoding="utf-8"))
    assert sur_disque["files"] is True
    assert sur_disque["screen"] is False
    assert list(tmp_path.glob("*.tmp")) == []


def test_echec_d_ecriture_est_signale_mais_n_interrompt_pas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = PermissionStore(_fichier(tmp_path))
    monkeypatch.setattr(kernel_perms, "_write_atomic", _explose)

    store.set("files", True)

    assert store.get("files") is True  # appliqué pour cette session
    assert store.storage_error is not None
    assert "redémarrage" in store.storage_error


def test_storage_error_efface_apres_une_ecriture_reussie(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = PermissionStore(_fichier(tmp_path))
    monkeypatch.setattr(kernel_perms, "_write_atomic", _explose)
    store.set("files", True)
    monkeypatch.undo()

    store.set("files", False)

    assert store.storage_error is None


# ── Store éphémère (tests, instances jetables) ────────────────────────────────


def test_store_sans_chemin_n_ecrit_rien(tmp_path: Path) -> None:
    store = PermissionStore()
    store.set("files", True)

    assert store.get("files") is True
    assert list(tmp_path.iterdir()) == []


def test_cle_inconnue_refusee_et_non_persistee(tmp_path: Path) -> None:
    path = _fichier(tmp_path)
    store = PermissionStore(path)

    assert store.set("root", True) is False
    assert "root" not in store.all()
    assert not path.exists()


# ── Deux processus autour du même fichier ─────────────────────────────────────


def test_changement_externe_visible_sans_redemarrage(tmp_path: Path) -> None:
    """L'API web et l'agent vocal sont deux processus : l'un doit voir l'autre."""
    path = _fichier(tmp_path)
    api = PermissionStore(path)
    agent_vocal = PermissionStore(path)

    api.set("files", True)

    assert agent_vocal.get("files") is True


def test_reload_explicite(tmp_path: Path) -> None:
    path = _fichier(tmp_path)
    store = PermissionStore(path)
    store.set("camera", True)
    path.unlink()

    store.reload()

    assert store.get("camera") is False


def test_fichier_supprime_ne_revoque_pas_a_chaud(tmp_path: Path) -> None:
    """Un incident de disque passager ne doit pas retirer une permission."""
    path = _fichier(tmp_path)
    store = PermissionStore(path)
    store.set("files", True)
    path.unlink()

    assert store.get("files") is True


# ── API /api/permissions ──────────────────────────────────────────────────────


def test_get_expose_l_etat_complet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = PermissionStore(_fichier(tmp_path))
    store.set("files", True)

    r = _client(store, monkeypatch).get("/api/permissions")

    assert r.status_code == 200
    assert r.json() == {"microphone": True, "screen": False, "camera": False, "files": True}


def test_patch_ecrit_bien_sur_le_disque(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _fichier(tmp_path)
    client = _client(PermissionStore(path), monkeypatch)

    r = client.patch("/api/permissions/files", json={"enabled": True})

    assert r.status_code == 200
    assert r.json()["persisted"] is True
    assert PermissionStore(path).get("files") is True


def test_patch_cle_inconnue_repond_404_et_liste_les_cles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(PermissionStore(_fichier(tmp_path)), monkeypatch)

    r = client.patch("/api/permissions/fichiers", json={"enabled": True})

    assert r.status_code == 404
    detail = r.json()["detail"]
    assert "fichiers" in detail
    for cle in PERMISSION_KEYS:
        assert cle in detail


def test_patch_signale_l_echec_d_enregistrement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ne pas répondre « OK » quand le réglage sera perdu au redémarrage."""
    store = PermissionStore(_fichier(tmp_path))
    client = _client(store, monkeypatch)
    monkeypatch.setattr(kernel_perms, "_write_atomic", _explose)

    body = client.patch("/api/permissions/files", json={"enabled": True}).json()

    assert body["persisted"] is False
    assert "droits d'écriture" in body["warning"]
