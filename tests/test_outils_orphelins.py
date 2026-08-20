# Copyright (C) 2026 Maxime Song
# This file is part of CRUSH-OS,   .
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Les trois classes d'outils qui existent sans être enregistrées.

`fusion_360`, `printer_3d` et `map_control` sont définies mais absentes du
registre. Ne pas les enregistrer n'autorise pas à les laisser pourrir : deux
d'entre elles ont un appelant ou en auront un, et leurs défauts ne se voient
justement pas puisque aucun test ne les traverse.

Ce que ces tests protègent, dans l'ordre :
  1. aucune des trois ne lit un réglage inexistant — c'est ainsi que
     `fusion_360` transformait « Fusion est fermé » en AttributeError ;
  2. les échecs disent ce qui manque et sur quelle machine, au lieu de
     recracher un nom de module ou un chemin macOS ;
  3. `printer_3d` ne désigne jamais un G-code qu'il n'a pas produit, et
     n'annonce pas un profil qu'il n'a pas appliqué : au bout de la chaîne il
     y a une machine qui chauffe du plastique ;
  4. l'argument qui condamne `map_control` reste vrai — si `show_view`
     cessait de le couvrir, ces tests le diraient.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import sys
import time
from pathlib import Path

import pytest

from crush.capabilities.tools import fusion, map_control, printer, show_view
from crush.capabilities.tools.fusion import FusionTool
from crush.capabilities.tools.map_control import MapControlTool
from crush.capabilities.tools.printer import Printer3DTool
from crush.kernel.settings import settings

_ORPHELINS = (FusionTool, Printer3DTool, MapControlTool)
_MODULES = (fusion, printer, map_control)


# ── 1. Réglages lus : ils doivent exister ────────────────────────────────


def _reglages_lus(module: object) -> set[str]:
    """Noms des attributs lus sur le singleton `settings` dans ce module."""
    source = Path(inspect.getfile(module)).read_text(encoding="utf-8")  # type: ignore[arg-type]
    arbre = ast.parse(source)
    return {
        noeud.attr
        for noeud in ast.walk(arbre)
        if isinstance(noeud, ast.Attribute)
        and isinstance(noeud.value, ast.Name)
        and noeud.value.id == "settings"
    }


@pytest.mark.parametrize("module", _MODULES, ids=lambda m: m.__name__.rsplit(".", 1)[-1])
def test_aucun_reglage_fantome(module: object) -> None:
    """Un `settings.x` inexistant lève AttributeError là où on diagnostique.

    Le cas réel : `settings.fusion_mcp_port` n'a jamais existé, et il n'était
    lu que sur les chemins d'erreur. La panne banale — Fusion fermé — sortait
    donc en AttributeError, précisément quand l'opérateur avait besoin d'une
    phrase utile. Un test qui appellerait l'outil « au vert » ne l'aurait
    jamais vu ; on inspecte donc la source.
    """
    lus = _reglages_lus(module)
    inconnus = sorted(nom for nom in lus if not hasattr(settings, nom))
    assert inconnus == [], f"{module.__name__} lit des réglages absents de Settings : {inconnus}"


def test_le_lecteur_de_reglages_voit_vraiment_quelque_chose() -> None:
    """Garde-fou du test précédent : un parseur muet le rendrait toujours vert."""
    assert {"fusion_enabled", "fusion_mcp_url"} <= _reglages_lus(fusion)
    assert "printer_ip" in _reglages_lus(printer)


# ── 2. Schémas cohérents avec les signatures ─────────────────────────────


@pytest.mark.parametrize("classe", _ORPHELINS, ids=lambda c: c.name)
def test_schema_conforme_a_la_signature(classe: type) -> None:
    """Toute propriété annoncée au modèle doit être un paramètre réel.

    Une propriété orpheline serait acceptée par `**_` puis silencieusement
    ignorée : l'outil promettrait un réglage qu'il n'applique pas.
    """
    parametres = inspect.signature(classe.execute).parameters
    noms = set(parametres) - {"self"}
    assert any(p.kind is p.VAR_KEYWORD for p in parametres.values()), (
        f"{classe.name}: execute() doit absorber les clés inconnues du modèle"
    )

    proprietes = set(classe.input_schema["properties"])
    assert proprietes <= noms, f"{classe.name}: propriétés sans paramètre : {proprietes - noms}"

    requis = set(classe.input_schema.get("required", []))
    assert requis <= proprietes, f"{classe.name}: `required` hors des propriétés : {requis}"
    for nom in sorted(requis):
        assert parametres[nom].default is inspect.Parameter.empty, (
            f"{classe.name}: `{nom}` est annoncé obligatoire mais a une valeur par "
            "défaut — un appel sans lui passerait silencieusement"
        )


# ── 3. fusion_360 — le diagnostic doit nommer la machine visée ───────────


async def test_fusion_mcp_muet_donne_une_phrase_utile(monkeypatch: pytest.MonkeyPatch) -> None:
    """MCP injoignable : message exploitable, pas une exception d'attribut."""
    monkeypatch.setattr(settings, "fusion_enabled", True, raising=False)
    monkeypatch.setattr(settings, "fusion_mcp_url", "http://10.0.0.9:27182/mcp", raising=False)

    async def _jamais_pret(self: object) -> bool:
        return False

    monkeypatch.setattr(fusion._FusionClient, "initialize", _jamais_pret)
    monkeypatch.setattr(fusion._client, "_session_id", None, raising=False)

    resultat = await FusionTool().execute(action="read")

    assert resultat.is_error
    assert "10.0.0.9" in resultat.content, "le message doit nommer la machine interrogée"
    assert "AttributeError" not in resultat.content


async def test_fusion_desactive_dit_quel_reglage_changer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "fusion_enabled", False, raising=False)
    resultat = await FusionTool().execute(action="read")
    assert resultat.is_error
    assert "FUSION_ENABLED" in resultat.content


# ── 4. printer_3d — slicing ──────────────────────────────────────────────


@pytest.fixture
def stl(tmp_path: Path) -> Path:
    fichier = tmp_path / "piece.stl"
    fichier.write_text("solid piece\nendsolid piece\n", encoding="utf-8")
    return fichier


def test_orca_cherche_dans_le_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Le PATH prime : c'est le seul levier de l'utilisateur, faute de réglage."""
    monkeypatch.setattr(printer.shutil, "which", lambda nom: f"/bin/{nom}")
    assert printer._trouver_orca() == "/bin/orca-slicer"


def test_orca_ne_depend_pas_du_seul_macos() -> None:
    """Le chemin câblé était `/Applications/...`, introuvable sur les deux cibles."""
    hors_macos = [c for c in printer._ORCA_CHEMINS_CONNUS if "/Applications/" not in c]
    assert hors_macos, "aucun emplacement Linux/Windows : slice échouerait toujours ici"


async def test_slice_sans_orca_explique_quoi_faire(
    monkeypatch: pytest.MonkeyPatch, stl: Path
) -> None:
    monkeypatch.setattr(printer, "_trouver_orca", lambda: None)
    resultat = await Printer3DTool()._slice(str(stl), "")
    assert resultat.is_error
    assert "PATH" in resultat.content
    assert "print" in resultat.content, "doit orienter vers l'envoi direct du G-code"


async def test_profil_par_nom_refuse_au_lieu_d_etre_ignore(
    monkeypatch: pytest.MonkeyPatch, stl: Path
) -> None:
    """Le profil était accepté puis jamais transmis au slicer.

    Le G-code produit ne correspondait donc pas à ce qui avait été demandé,
    et rien ne le signalait avant l'impression.
    """
    monkeypatch.setattr(printer, "_trouver_orca", lambda: "/bin/orca-slicer")

    async def _interdit(*_a: object, **_k: object) -> object:
        raise AssertionError("le slicer ne doit pas être lancé avec un profil inapplicable")

    monkeypatch.setattr(printer.asyncio, "create_subprocess_exec", _interdit)

    resultat = await Printer3DTool()._slice(str(stl), "0.2mm Standard")

    assert resultat.is_error
    assert "Export preset" in resultat.content


async def test_profil_fichier_est_transmis_au_slicer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stl: Path
) -> None:
    reglages = tmp_path / "process.json"
    reglages.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(printer, "_trouver_orca", lambda: "/bin/orca-slicer")
    vu: list[str] = []

    class _Proc:
        returncode = 1

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"", b"stop"

    async def _faux_exec(*cmd: str, **_k: object) -> _Proc:
        vu.extend(cmd)
        return _Proc()

    monkeypatch.setattr(printer.asyncio, "create_subprocess_exec", _faux_exec)
    await Printer3DTool()._slice(str(stl), str(reglages))

    assert "--load-settings" in vu
    assert str(reglages) in vu


def test_gcode_produit_ignore_un_reste_de_session(tmp_path: Path) -> None:
    """Le premier fichier par ordre alphabétique pouvait être un vieux G-code.

    « print » aurait alors imprimé l'objet précédent : l'erreur se paie en
    plastique et en heures de machine, pas en message d'erreur.
    """
    ancien = tmp_path / "aaa_ancienne_piece.gcode"
    ancien.write_text("G0", encoding="utf-8")
    avant = printer._gcodes(tmp_path)

    depuis = time.time()
    nouveau = tmp_path / "zzz_piece.gcode"
    nouveau.write_text("G1", encoding="utf-8")

    assert printer._gcode_produit(tmp_path, "piece", avant, depuis) == nouveau


def test_gcode_produit_prefere_le_nom_du_stl(tmp_path: Path) -> None:
    depuis = time.time()
    for nom in ("aaa_autre.gcode", "piece_0.2mm.gcode"):
        (tmp_path / nom).write_text("G1", encoding="utf-8")

    produit = printer._gcode_produit(tmp_path, "piece", set(), depuis)

    assert produit is not None
    assert produit.name == "piece_0.2mm.gcode"


def test_gcode_absent_est_signale(tmp_path: Path) -> None:
    assert printer._gcode_produit(tmp_path, "piece", set(), time.time()) is None


# ── 5. printer_3d — réseau et dépendance ─────────────────────────────────


def test_mqtt_muet_leve_une_erreur_nommant_l_imprimante(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sortir en silence faisait échouer l'appel suivant sur une erreur opaque."""
    monkeypatch.setattr(settings, "printer_ip", "192.168.1.42", raising=False)

    class _JamaisPrete:
        def mqtt_client_ready(self) -> bool:
            return False

    with pytest.raises(TimeoutError) as excinfo:
        printer._wait_ready(_JamaisPrete(), timeout=0.0)

    assert "192.168.1.42" in str(excinfo.value)
    assert "LAN" in str(excinfo.value)


def test_dependance_hardware_absente_dit_comment_l_installer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`No module named 'bambulabs_api'` cache que l'absence est un choix.

    La dépendance est rangée dans l'extra « hardware », volontairement hors
    du socle installé sur la Pi.
    """
    monkeypatch.setitem(sys.modules, "bambulabs_api", None)
    monkeypatch.setattr(settings, "printer_ip", "192.168.1.42", raising=False)
    monkeypatch.setattr(settings, "printer_serial", "01P00A000000000", raising=False)
    monkeypatch.setattr(settings, "printer_access_code", "12345678", raising=False)

    with pytest.raises(RuntimeError) as excinfo:
        printer._make_printer()

    assert "hardware" in str(excinfo.value)


async def test_statut_sans_configuration_liste_les_reglages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "printer_ip", "", raising=False)
    resultat = await Printer3DTool().execute(action="status")
    assert resultat.is_error
    assert "PRINTER_IP" in resultat.content


# ── 6. map_control — doublon de show_view, et pas l'inverse ──────────────


def test_show_view_couvre_toutes_les_actions_de_map_control() -> None:
    """Le seul reliquat est `toggle_panels` ; le reste est un doublon exact."""
    actions_mc = set(MapControlTool.input_schema["properties"]["action"]["enum"])
    non_couvertes = actions_mc - set(show_view.ACTIONS)
    assert non_couvertes == {"toggle_panels"}, (
        "show_view ne couvre plus map_control : réexaminer la suppression proposée, "
        f"actions orphelines = {sorted(non_couvertes)}"
    )


def test_table_de_villes_redondante() -> None:
    """Les deux tables ne divergent jamais ; celle de show_view est plus riche.

    Les rares villes absentes de show_view retombent sur le même repli
    Nominatim : supprimer map_control ne perd aucune adresse.
    """
    divergences = {
        ville: (coord, show_view.CITY_COORDS[ville])
        for ville, coord in map_control.CITY_COORDS.items()
        if ville in show_view.CITY_COORDS and coord != show_view.CITY_COORDS[ville]
    }
    assert divergences == {}
    assert len(show_view.CITY_COORDS) > len(map_control.CITY_COORDS)


def test_toggle_panels_garde_une_cible_reelle() -> None:
    """`toggle_panels` fonctionne encore : c'est ce qui interdit la suppression seche.

    Une premiere version de ce test affirmait le contraire, en ne lisant que
    `home.html` et `globe.js`. Elle ignorait que `app.py` monte
    StaticFiles(html=True) sur `/` : `index.html` est donc bel et bien servie,
    et c'est elle qui traite la commande. Le test restait vert alors que sa
    propre premisse etait fausse — et son nom promettait une verification sur
    « l'interface servie » qu'il ne faisait pas.
    """
    static = Path(inspect.getfile(show_view)).parents[2] / "interfaces" / "ui" / "static"

    # La page servie a la racine, celle que le montage StaticFiles expose.
    index = (static / "index.html").read_text(encoding="utf-8", errors="replace")
    assert "toggle_panels" in index, (
        "index.html traite la commande : supprimer map_control la ferait perdre"
    )

    # home.html, la page principale, ne la traite pas : la redondance avec
    # show_view est donc reelle mais incomplete.
    home = (static / "home.html").read_text(encoding="utf-8", errors="replace")
    assert "panels-toggle" not in home

    globe = (static / "globe.js").read_text(encoding="utf-8", errors="replace")
    assert "toggle_panels" in globe, "la vue globe reste le destinataire nominal"

async def test_map_control_annonce_un_affichage_qu_il_n_a_pas_verifie() -> None:
    """Constat, pas régression : c'est l'argument central de la suppression.

    `show_view` refuse quand aucun navigateur n'est connecté ou quand la vue
    n'est pas installée ; `map_control` diffuse à l'aveugle et répond « fait ».
    Sur une machine sans écran, la réponse est fausse par construction.
    """
    envoyes: list[dict] = []
    outil = MapControlTool(broadcast_event=envoyes.append)

    resultat = await outil.execute(action="fly_to", location="paris")

    assert not resultat.is_error
    assert envoyes and envoyes[0]["type"] == "map_fly_to"


def test_les_orphelins_restent_hors_du_registre() -> None:
    """Aucune des trois n'est instanciée dans la composition root.

    Test de constat : il documente l'arbitrage plutôt qu'il ne l'impose. Le
    jour où l'une d'elles est enregistrée, ce test tombe et force à relire le
    coût — la liste d'outils est renvoyée à chaque requête, et `fusion_360`
    pèse à lui seul près d'un cinquième de son volume actuel.
    """
    racine = Path(inspect.getfile(show_view)).parents[2]
    bootstrap = (racine / "bootstrap.py").read_text(encoding="utf-8")
    presents = [c.__name__ for c in _ORPHELINS if f"{c.__name__}(" in bootstrap]
    assert presents == [], f"outils désormais enregistrés, arbitrage à refaire : {presents}"


def test_cout_du_schema_des_orphelins() -> None:
    """Chiffre l'argument : ce qu'on ajouterait à chaque requête.

    Les seuils sont larges à dessein — ils n'existent que pour empêcher une
    description de doubler de taille sans que personne ne le remarque.
    """
    import json

    tailles = {
        c.name: len(
            json.dumps(
                {
                    "name": c.name,
                    "description": c.description,
                    "input_schema": c.input_schema,
                },
                ensure_ascii=False,
            )
        )
        for c in _ORPHELINS
    }
    assert tailles["fusion_360"] > 3000, "description Fusion amaigrie : réexaminer l'arbitrage"
    assert sum(tailles.values()) < 12000


def test_printer_s_importe_sans_l_extra_hardware(monkeypatch: pytest.MonkeyPatch) -> None:
    """`bambulabs_api` reste un import paresseux, jamais une condition d'import.

    Le socle installé sur la Pi n'a pas l'extra « hardware ». Si le module le
    remontait en tête de fichier, `capabilities.tools` deviendrait
    inimportable là-bas — et l'échec se manifesterait au démarrage, loin de
    sa cause.
    """
    monkeypatch.setitem(sys.modules, "bambulabs_api", None)
    monkeypatch.delitem(sys.modules, printer.__name__, raising=False)

    recharge = importlib.import_module(printer.__name__)

    assert recharge.Printer3DTool.name == "printer_3d"
