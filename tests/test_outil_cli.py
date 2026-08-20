# Copyright (C) 2026 Maxime Song
# This file is part of CRUSH-OS,   .
 

"""Audit production : run_script inappelable, whitelist execute_cli héritée de macOS.

Les deux défauts constatés sur la Pi :
  • run_script levait un TypeError à chaque appel (schéma et signature divergents) ;
  • execute_cli refusait 'echo' tout en autorisant des binaires macOS absents
    de Debian ARM64.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from crush.capabilities.tools.cli import (
    CLI_WHITELIST,
    CLIRunnerTool,
    ExecuteCLITool,
)


@pytest.fixture()
def cli() -> ExecuteCLITool:
    return ExecuteCLITool()


@pytest.fixture()
def catalogue_vide(tmp_path: Path) -> CLIRunnerTool:
    """L'état réel de la Pi : config/tools.yaml entièrement commenté."""
    chemin = tmp_path / "tools.yaml"
    chemin.write_text("# tout est commenté\n", encoding="utf-8")
    return CLIRunnerTool(whitelist_path=chemin)


def _catalogue(tmp_path: Path, contenu: str) -> CLIRunnerTool:
    chemin = tmp_path / "tools.yaml"
    chemin.write_text(contenu, encoding="utf-8")
    return CLIRunnerTool(whitelist_path=chemin)


# ── Défaut 1 : run_script — schéma déclaré vs signature réelle ────────────────


def test_schema_et_signature_concordent(catalogue_vide: CLIRunnerTool) -> None:
    """Tout paramètre déclaré au modèle doit exister dans execute()."""
    parametres = inspect.signature(catalogue_vide.execute).parameters
    for nom in catalogue_vide.input_schema["properties"]:
        assert nom in parametres, f"'{nom}' est annoncé au modèle mais absent d'execute()"


def test_aucun_argument_obligatoire_dans_la_signature(catalogue_vide: CLIRunnerTool) -> None:
    """Un argument omis par le modèle doit produire un message, pas un TypeError.

    C'est la cause exacte du « missing 1 required positional argument » de l'audit :
    le registre appelle execute(**inputs) et remonte l'exception telle quelle.
    """
    for nom, param in inspect.signature(catalogue_vide.execute).parameters.items():
        if param.kind is inspect.Parameter.VAR_KEYWORD:
            continue
        assert param.default is not inspect.Parameter.empty, f"'{nom}' n'a pas de valeur par défaut"


def test_schema_non_vide_et_alias_requis(catalogue_vide: CLIRunnerTool) -> None:
    assert catalogue_vide.input_schema["properties"]
    assert catalogue_vide.input_schema["required"] == ["alias"]


async def test_appel_sans_argument_ne_leve_pas(catalogue_vide: CLIRunnerTool) -> None:
    """Reproduction directe de l'appel de l'audit : POST /tools/execute avec {}."""
    result = await catalogue_vide.execute()
    assert result.is_error
    assert "alias" in result.content


async def test_appel_sans_argument_donne_la_marche_a_suivre(
    catalogue_vide: CLIRunnerTool,
) -> None:
    """Le message doit être une instruction : où déclarer un script, et comment."""
    result = await catalogue_vide.execute()
    assert "tools.yaml" in result.content
    assert "command:" in result.content
    assert "execute_cli" in result.content


async def test_alias_inconnu_catalogue_vide(catalogue_vide: CLIRunnerTool) -> None:
    result = await catalogue_vide.execute(alias="sonde_audit")
    assert result.is_error
    assert "inconnu" in result.content
    assert "tools.yaml" in result.content


def test_description_annonce_le_catalogue_vide(catalogue_vide: CLIRunnerTool) -> None:
    """Le modèle ne doit pas apprendre l'inutilité de l'outil en l'appelant."""
    assert "AUCUN script" in catalogue_vide.description


def test_pas_d_enum_vide_dans_le_schema(catalogue_vide: CLIRunnerTool) -> None:
    """`enum: []` est un schéma invalide côté API — il ne doit pas être émis."""
    assert "enum" not in catalogue_vide.input_schema["properties"]["alias"]


def test_enum_liste_les_alias_disponibles(tmp_path: Path) -> None:
    outil = _catalogue(
        tmp_path,
        'sauvegarde:\n  command: ["echo", "ok"]\nstats:\n  command: ["echo", "ok"]\n',
    )
    assert outil.input_schema["properties"]["alias"]["enum"] == ["sauvegarde", "stats"]


def test_entree_sans_command_est_ecartee(tmp_path: Path) -> None:
    """Un alias sans `command` planterait sur un KeyError : ne pas l'annoncer."""
    outil = _catalogue(tmp_path, 'casse:\n  description: "oubli de command"\n')
    assert outil.input_schema["properties"]["alias"].get("enum") is None
    assert "casse" not in outil.description


def test_yaml_invalide_ne_casse_pas_l_outil(tmp_path: Path) -> None:
    outil = _catalogue(tmp_path, "ceci: n'est: pas: du yaml\n")
    assert outil.input_schema["required"] == ["alias"]


async def test_action_inconnue_liste_les_actions(catalogue_vide: CLIRunnerTool) -> None:
    result = await catalogue_vide.execute(alias="x", action="list")
    assert result.is_error
    assert "run" in result.content and "confirm" in result.content


async def test_command_en_chaine_est_acceptee(tmp_path: Path) -> None:
    """`command: echo bonjour` (chaîne YAML) ne doit pas être découpé lettre à lettre."""
    outil = _catalogue(tmp_path, "salut:\n  command: echo bonjour\n")
    assert outil._commande(outil._scripts["salut"]) == ["echo", "bonjour"]


async def test_args_en_chaine_sont_decoupes(tmp_path: Path) -> None:
    """Le modèle envoie parfois une chaîne là où le schéma annonce un tableau."""
    outil = _catalogue(tmp_path, 'greet:\n  command: ["echo"]\n')
    lance: dict = {}

    async def faux_exec(*args: object, **kwargs: object) -> MagicMock:
        lance["cmd"] = list(args)
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"ok", b""))
        proc.returncode = 0
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=faux_exec):
        await outil.execute(alias="greet", args="Max le second")

    assert lance["cmd"] == ["echo", "Max", "le", "second"]


async def test_binaire_absent_message_explicite(tmp_path: Path) -> None:
    """Un script pointant vers un binaire absent doit nommer le coupable."""
    outil = _catalogue(tmp_path, 'fantome:\n  command: ["/usr/bin/nexistepas_crush"]\n')
    result = await outil.execute(alias="fantome")
    assert result.is_error
    assert "nexistepas_crush" in result.content
    assert "tools.yaml" in result.content


# ── Défaut 2 : whitelist execute_cli portée depuis macOS ──────────────────────


@pytest.mark.parametrize("binaire", ["sips", "osascript", "screencapture", "say", "afinfo", "open"])
def test_binaires_macos_retires(binaire: str) -> None:
    """Ces binaires n'existent pas sous Debian : les autoriser ne produisait
    qu'un « No such file or directory » et laissait croire à un pilotage du Mac."""
    assert binaire not in CLI_WHITELIST


@pytest.mark.parametrize(
    "binaire",
    ["echo", "ls", "cat", "grep", "stat", "file", "wc", "df", "ps", "journalctl", "ffprobe", "jq"],
)
def test_binaires_de_lecture_autorises(binaire: str) -> None:
    assert binaire in CLI_WHITELIST


@pytest.mark.parametrize("binaire", ["dd", "chmod", "chown", "sudo", "curl", "wget", "bash", "sh"])
def test_protection_non_desserree(binaire: str) -> None:
    assert binaire not in CLI_WHITELIST


async def test_echo_passe_desormais(cli: ExecuteCLITool, monkeypatch: pytest.MonkeyPatch) -> None:
    """La commande de sonde de l'audit doit aboutir."""
    faux_settings = MagicMock()
    faux_settings.allow_unsandboxed_exec = False
    monkeypatch.setattr("crush.capabilities.tools.cli.settings", faux_settings)

    proc = MagicMock()
    proc.communicate = AsyncMock(return_value=(b"sonde", b""))
    proc.returncode = 0

    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)) as exec_mock:
        result = await cli.execute(command="echo sonde")

    assert exec_mock.called
    assert not result.is_error


# ── Défaut 3 : message de refus exploitable ───────────────────────────────────


async def test_refus_nomme_le_binaire(cli: ExecuteCLITool) -> None:
    result = await cli.execute(command="tcpdump -i eth0")
    assert result.is_error
    assert "tcpdump" in result.content


async def test_refus_liste_les_binaires_autorises(cli: ExecuteCLITool) -> None:
    result = await cli.execute(command="tcpdump -i eth0")
    for attendu in ("ls", "cat", "ffmpeg", "git"):
        assert attendu in result.content


async def test_refus_oriente_vers_browser_pour_curl(cli: ExecuteCLITool) -> None:
    result = await cli.execute(command="curl https://example.com")
    assert "browser" in result.content


async def test_refus_macos_oriente_vers_remote_pc(cli: ExecuteCLITool) -> None:
    """Le modèle croit piloter un Mac : le refus doit le détromper et l'aiguiller."""
    result = await cli.execute(command="screencapture /tmp/x.png")
    assert result.is_error
    assert "remote_pc" in result.content
    assert "Raspberry" in result.content


async def test_binaire_autorise_mais_absent(
    cli: ExecuteCLITool, monkeypatch: pytest.MonkeyPatch
) -> None:
    faux_settings = MagicMock()
    faux_settings.allow_unsandboxed_exec = False
    monkeypatch.setattr("crush.capabilities.tools.cli.settings", faux_settings)

    with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError("no jq")):
        result = await cli.execute(command="jq . fichier.json")

    assert result.is_error
    assert "jq" in result.content
    assert "install" in result.content.lower()


async def test_command_manquante_ne_leve_pas(cli: ExecuteCLITool) -> None:
    result = await cli.execute()
    assert result.is_error
    assert "command" in result.content


# ── Durcissements : interpréteurs, écrasements, options détournées ────────────


@pytest.mark.parametrize("commande", ["python3 -c 'import os'", "pip install requests", "uv sync"])
async def test_interpreteurs_exigent_approbation(cli: ExecuteCLITool, commande: str) -> None:
    """python/pip/uv exécutent du code arbitraire : jamais sans accord humain."""
    result = await cli.execute(command=commande)
    assert not result.is_error
    assert "⚠️" in result.content


@pytest.mark.parametrize("commande", ["cp a.txt /etc/b.txt", "mv a.txt b.txt", "unzip x.zip -d /"])
async def test_ecrasements_exigent_approbation(cli: ExecuteCLITool, commande: str) -> None:
    """Le sandbox ne déplace que le cwd : un chemin absolu écrit toujours où il veut."""
    result = await cli.execute(command=commande)
    assert not result.is_error
    assert "⚠️" in result.content


async def test_find_exec_refuse(cli: ExecuteCLITool) -> None:
    """`find -exec` contourne la whitelist en lançant n'importe quel binaire."""
    result = await cli.execute(command="find . -name '*.py' -exec rm {} ;", confirmed=True)
    assert result.is_error
    assert "-exec" in result.content


async def test_find_delete_refuse(cli: ExecuteCLITool) -> None:
    result = await cli.execute(command="find /tmp -delete", confirmed=True)
    assert result.is_error


async def test_find_lecture_reste_possible(
    cli: ExecuteCLITool, monkeypatch: pytest.MonkeyPatch
) -> None:
    faux_settings = MagicMock()
    faux_settings.allow_unsandboxed_exec = False
    monkeypatch.setattr("crush.capabilities.tools.cli.settings", faux_settings)

    proc = MagicMock()
    proc.communicate = AsyncMock(return_value=(b"./a.py", b""))
    proc.returncode = 0

    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        result = await cli.execute(command="find . -name '*.py'")

    assert not result.is_error


async def test_git_config_injection_refusee(cli: ExecuteCLITool) -> None:
    """`git -c alias.x='!cmd' x` exécute une commande arbitraire via la config."""
    result = await cli.execute(command="git -c core.pager=/bin/sh log")
    assert result.is_error
    assert "-c" in result.content


async def test_git_log_option_c_reste_possible(
    cli: ExecuteCLITool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`git log -c` est un affichage de diff : seules les options globales sont filtrées."""
    faux_settings = MagicMock()
    faux_settings.allow_unsandboxed_exec = False
    monkeypatch.setattr("crush.capabilities.tools.cli.settings", faux_settings)

    proc = MagicMock()
    proc.communicate = AsyncMock(return_value=(b"commit", b""))
    proc.returncode = 0

    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        result = await cli.execute(command="git log -c -1")

    assert not result.is_error


async def test_journalctl_lecture_ok(
    cli: ExecuteCLITool, monkeypatch: pytest.MonkeyPatch
) -> None:
    faux_settings = MagicMock()
    faux_settings.allow_unsandboxed_exec = False
    monkeypatch.setattr("crush.capabilities.tools.cli.settings", faux_settings)

    proc = MagicMock()
    proc.communicate = AsyncMock(return_value=(b"-- Logs --", b""))
    proc.returncode = 0

    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        result = await cli.execute(command="journalctl -u crush-api -n 50")

    assert not result.is_error


async def test_journalctl_vacuum_refuse(cli: ExecuteCLITool) -> None:
    result = await cli.execute(command="journalctl --vacuum-time=1s", confirmed=True)
    assert result.is_error


async def test_option_interdite_non_levee_par_confirmed(cli: ExecuteCLITool) -> None:
    """confirmed=true ne débloque pas une option qui contourne la whitelist."""
    with patch("asyncio.create_subprocess_exec") as exec_mock:
        await cli.execute(command="find . -exec cat {} ;", confirmed=True)
    exec_mock.assert_not_called()


# ── Le tool n'invente pas un Mac ──────────────────────────────────────────────


def test_description_ne_promet_pas_le_mac(cli: ExecuteCLITool) -> None:
    assert "Mac" not in cli.description
    assert "remote_pc" in cli.description
    assert "Raspberry" in cli.description
