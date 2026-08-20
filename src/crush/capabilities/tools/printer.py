# Copyright (C) 2026 Barthélemy Houot
# Copyright (C) 2026 Maxime Song — modifications de ce fork
# This file is part of CRUSH-OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""
Outil imprimante 3D pour Crush — BambuLab.
- slice  : OrcaSlicer CLI (génère le G-code)
- print  : upload + start_print via bambulabs_api (MQTT)
- status : état temps réel via bambulabs_api
- cancel : stop_print via bambulabs_api

Cet outil n'est pas dans le registre global. Les trois actions réseau
(print / status / cancel) sont, elles, réalisables depuis une machine sans
écran : la BambuLab se pilote en MQTT sur le réseau local. Il manque deux
choses avant de pouvoir l'enregistrer, et aucune ne relève de ce fichier :
la dépendance `bambulabs-api` vit dans l'extra « hardware » (absente du
socle) et PRINTER_IP / PRINTER_SERIAL / PRINTER_ACCESS_CODE sont vides.
Tant que c'est le cas, l'enregistrer ajouterait ~1,1 Ko de schéma à chaque
requête pour un outil qui refuserait chaque appel.
"""

from __future__ import annotations

import asyncio
import shutil
import time
import uuid
from pathlib import Path

from loguru import logger

from crush.capabilities.tools.base import Tool, ToolResult
from crush.kernel.approval import get_approval_checker
from crush.kernel.settings import settings

# Le chemin d'OrcaSlicer était câblé sur l'unique disposition macOS, si bien
# que l'action « slice » échouait à coup sûr sur les deux machines du projet :
# le poste de développement Windows et la cible Debian ARM. On cherche donc
# l'exécutable, PATH d'abord — c'est le seul point que l'utilisateur peut
# corriger sans toucher au code, faute d'un réglage dédié.
_ORCA_NOMS = ("orca-slicer", "orcaslicer", "OrcaSlicer")
_ORCA_CHEMINS_CONNUS = (
    "/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer",
    "/usr/bin/orca-slicer",
    "/usr/local/bin/orca-slicer",
    "/opt/OrcaSlicer/orca-slicer",
    "C:/Program Files/OrcaSlicer/orca-slicer.exe",
)

_ORCA_ABSENT = (
    "OrcaSlicer introuvable sur cette machine. Le slicing exige son exécutable : "
    "l'installer, puis le rendre atteignable par le PATH (noms cherchés : "
    + ", ".join(_ORCA_NOMS)
    + "). Sur une machine sans écran, le plus simple reste de slicer depuis le "
    'poste de travail et de n\'envoyer ici que le G-code, avec action="print".'
)


def _trouver_orca() -> str | None:
    """Exécutable OrcaSlicer, ou None s'il n'est pas installé ici."""
    for nom in _ORCA_NOMS:
        if chemin := shutil.which(nom):
            return chemin
    for brut in _ORCA_CHEMINS_CONNUS:
        if Path(brut).exists():
            return brut
    return None


def _gcodes(dossier: Path) -> set[Path]:
    return set(dossier.glob("*.gcode")) | set(dossier.glob("*.3mf"))


def _gcode_produit(dossier: Path, souche: str, avant: set[Path], depuis: float) -> Path | None:
    """G-code réellement écrit par ce slicing-ci, ou None.

    Retenir le premier fichier par ordre alphabétique pouvait désigner un
    G-code d'une session précédente resté à côté du STL — et « print »
    aurait alors lancé l'impression de cet objet-là, sur du vrai plastique.
    On compare donc l'avant et l'après ; si le slicing a écrasé un fichier
    existant, la comparaison est vide et on retombe sur les dates, avec deux
    secondes de marge pour les systèmes de fichiers à granularité grossière.
    """
    apres = _gcodes(dossier)
    candidats = list(apres - avant) or [p for p in apres if p.stat().st_mtime >= depuis - 2]
    if not candidats:
        return None
    candidats.sort(key=lambda p: (not p.stem.startswith(souche), -p.stat().st_mtime))
    return candidats[0]


def _require_bambu() -> tuple[str, str, str] | None:
    """Retourne (ip, serial, access_code) ou None si non configuré."""
    ip = settings.printer_ip
    serial = settings.printer_serial
    code = settings.printer_access_code
    if not (ip and serial and code):
        return None
    return ip, serial, code


def _make_printer() -> object:
    """Instancie un Printer bambulabs_api avec les settings courants."""
    try:
        import bambulabs_api as bl
    except ModuleNotFoundError as exc:
        # « No module named 'bambulabs_api' » ne dit pas que c'est un choix :
        # la dépendance est volontairement hors du socle installé sur la Pi.
        raise RuntimeError(
            "Dépendance bambulabs-api absente : elle vit dans l'extra « hardware », "
            "pas dans le socle. Installer avec « uv sync --extra hardware »."
        ) from exc

    creds = _require_bambu()
    if creds is None:
        raise ValueError("PRINTER_IP / PRINTER_SERIAL / PRINTER_ACCESS_CODE non configurés")
    ip, serial, code = creds
    return bl.Printer(ip, code, serial)


def _wait_ready(printer: object, timeout: float = 5.0) -> None:
    """Attend que le client MQTT soit prêt, et échoue si le délai expire.

    Repartir en silence après le délai laissait l'appel suivant se casser sur
    une erreur MQTT interne, alors que la cause est presque toujours la même
    et qu'on la connaît déjà à cet instant : rien ne répond à cette IP.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if printer.mqtt_client_ready():
            return
        time.sleep(0.2)
    raise TimeoutError(
        f"BambuLab injoignable en MQTT sur {settings.printer_ip or '(PRINTER_IP vide)'} "
        f"après {timeout:.0f}s. Vérifier que l'imprimante est allumée, sur le même "
        "réseau que Crush, en mode LAN, et que PRINTER_ACCESS_CODE correspond au "
        "code affiché sur son écran."
    )


class Printer3DTool(Tool):
    name = "printer_3d"
    description = (
        "Contrôle la BambuLab via MQTT.\n\n"
        "Actions disponibles :\n"
        "- slice  : slicer un fichier STL en G-code avec OrcaSlicer\n"
        "- print  : uploader le G-code et lancer l'impression sur la BambuLab\n"
        "- status : état de l'impression en cours (progression, temps restant)\n"
        "- cancel : annuler l'impression en cours\n\n"
        "Toujours demander confirmation via printer_slice / printer_print avant d'exécuter.\n\n"
        "Utilise cet outil quand l'utilisateur dit :\n"
        "'imprime ce modèle', 'slice ce STL', 'état de l'impression', 'annule l'impression'"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["slice", "print", "status", "cancel"],
            },
            "stl_path": {
                "type": "string",
                "description": "Chemin vers le fichier STL (pour slice)",
            },
            "gcode_path": {
                "type": "string",
                "description": "Chemin vers le fichier G-code .gcode ou .3mf (pour print)",
            },
            "profile": {
                "type": "string",
                "description": (
                    "Chemin d'un fichier de réglages OrcaSlicer exporté (.json/.ini)."
                    " Laisser vide pour les réglages par défaut du slicer :"
                    " le CLI ne sait pas retrouver un préréglage par son nom."
                ),
            },
            "plate": {
                "type": "integer",
                "description": "Numéro de plateau BambuLab (défaut: 1)",
            },
        },
        "required": ["action"],
    }

    async def execute(
        self,
        action: str,
        stl_path: str = "",
        gcode_path: str = "",
        profile: str = "",
        plate: int = 1,
        **_: object,
    ) -> ToolResult:

        checker = get_approval_checker()
        action_id = str(uuid.uuid4())[:8]

        if action == "slice":
            if checker:
                ok = await checker.check(
                    "printer_slice",
                    f"Slicer {Path(stl_path).name if stl_path else '?'} "
                    f"(profil : {profile or 'défaut du slicer'})",
                    action_id,
                )
                if not ok:
                    return ToolResult(content="Slicing refusé.", is_error=True)
            return await self._slice(stl_path, profile)

        if action == "print":
            if checker:
                fname = Path(gcode_path).name if gcode_path else "?"
                ok = await checker.check(
                    "printer_print",
                    f"Lancer l'impression de {fname} sur la BambuLab",
                    action_id,
                )
                if not ok:
                    return ToolResult(content="Impression refusée.", is_error=True)
            return await self._print(gcode_path, plate)

        if action == "status":
            return await self._status()

        if action == "cancel":
            return await self._cancel()

        return ToolResult(content=f"Action inconnue: {action}", is_error=True)

    # ── Slice ────────────────────────────────────────────────────────────────

    async def _slice(self, stl_path: str, profile: str) -> ToolResult:
        if not stl_path:
            return ToolResult(content="stl_path requis pour slice", is_error=True)

        stl = Path(stl_path).expanduser()
        if not stl.exists():
            return ToolResult(content=f"Fichier non trouvé: {stl_path}", is_error=True)

        orca = _trouver_orca()
        if orca is None:
            return ToolResult(content=_ORCA_ABSENT, is_error=True)

        args_profil: list[str] = []
        if profile:
            # Le profil était accepté puis jamais transmis : le slicing se
            # faisait avec les réglages par défaut, sous un nom qui promettait
            # autre chose, et le G-code partait ensuite à l'impression.
            reglages = Path(profile).expanduser()
            if not reglages.is_file():
                return ToolResult(
                    content=(
                        f"Profil « {profile} » inutilisable : le CLI d'OrcaSlicer ne "
                        "retrouve pas un préréglage par son nom, il attend un fichier "
                        "de réglages exporté (OrcaSlicer → clic droit sur le préréglage "
                        "→ « Export preset »), dont on donne ici le chemin. Laisser le "
                        "paramètre vide pour slicer avec les réglages par défaut."
                    ),
                    is_error=True,
                )
            args_profil = ["--load-settings", str(reglages)]

        output_dir = stl.parent
        cmd = [orca, "--slice", str(stl), *args_profil, "--output", str(output_dir)]
        logger.info(f"Slicing {stl.name} profile='{profile or 'defaut'}'")

        avant = _gcodes(output_dir)
        depuis = time.time()
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        except TimeoutError:
            proc.kill()
            return ToolResult(content="Timeout slicing (>120s)", is_error=True)

        if proc.returncode != 0:
            return ToolResult(content=f"Erreur slicing: {stderr.decode()[:300]}", is_error=True)

        produit = _gcode_produit(output_dir, stl.stem, avant, depuis)
        if produit is None:
            return ToolResult(
                content=(
                    "OrcaSlicer a rendu la main sans erreur mais aucun G-code n'est "
                    f"apparu dans {output_dir} : il n'y a rien à imprimer."
                ),
                is_error=True,
            )
        return ToolResult(content=f"Slicing terminé. Fichier : {produit}")

    # ── Print (bambulabs_api) ────────────────────────────────────────────────

    async def _print(self, gcode_path: str, plate: int = 1) -> ToolResult:
        if not gcode_path:
            return ToolResult(content="gcode_path requis pour print", is_error=True)

        gcode = Path(gcode_path).expanduser()
        if not gcode.exists():
            return ToolResult(content=f"Fichier non trouvé: {gcode_path}", is_error=True)

        if _require_bambu() is None:
            return ToolResult(
                content="PRINTER_IP / PRINTER_SERIAL / PRINTER_ACCESS_CODE non configurés",
                is_error=True,
            )

        def _do_print() -> str:
            printer = _make_printer()
            printer.connect()
            try:
                _wait_ready(printer)
                with gcode.open("rb") as f:
                    remote_name = printer.upload_file(f, gcode.name)
                time.sleep(1)
                ok = printer.start_print(remote_name, plate_number=plate)
                if ok:
                    return f"Impression lancée : {gcode.name} (plateau {plate})"
                return "Échec démarrage impression (start_print=False)"
            finally:
                printer.disconnect()

        try:
            msg = await asyncio.wait_for(asyncio.to_thread(_do_print), timeout=60)
            if msg.startswith("Échec"):
                return ToolResult(content=msg, is_error=True)
            return ToolResult(content=msg)
        except Exception as e:
            logger.error(f"Printer print error: {e}")
            return ToolResult(content=str(e), is_error=True)

    # ── Status (bambulabs_api) ───────────────────────────────────────────────

    async def _status(self) -> ToolResult:
        if _require_bambu() is None:
            return ToolResult(
                content="PRINTER_IP / PRINTER_SERIAL / PRINTER_ACCESS_CODE non configurés",
                is_error=True,
            )

        def _get_status() -> dict:
            printer = _make_printer()
            printer.connect()
            try:
                _wait_ready(printer)
                time.sleep(1)
                return {
                    "state": str(printer.get_state()),
                    "percentage": printer.get_percentage(),
                    "time_min": printer.get_time(),
                    "file": printer.get_file_name(),
                }
            finally:
                printer.disconnect()

        try:
            info = await asyncio.wait_for(asyncio.to_thread(_get_status), timeout=15)
            state = info["state"]
            pct = info["percentage"]
            mins = info["time_min"]
            fname = info["file"] or ""

            if pct not in (None, ""):
                parts = [f"État : {state}", f"Progression : {pct}%"]
                if mins:
                    parts.append(f"Temps restant : {mins}min")
                if fname:
                    parts.append(f"Fichier : {fname}")
                return ToolResult(content=" | ".join(parts))
            return ToolResult(content=f"État BambuLab : {state}")
        except Exception as e:
            logger.error(f"Printer status error: {e}")
            return ToolResult(content=str(e), is_error=True)

    # ── Cancel (bambulabs_api) ───────────────────────────────────────────────

    async def _cancel(self) -> ToolResult:
        if _require_bambu() is None:
            return ToolResult(
                content="PRINTER_IP / PRINTER_SERIAL / PRINTER_ACCESS_CODE non configurés",
                is_error=True,
            )

        def _do_cancel() -> bool:
            printer = _make_printer()
            printer.connect()
            try:
                _wait_ready(printer)
                return printer.stop_print()
            finally:
                printer.disconnect()

        try:
            ok = await asyncio.wait_for(asyncio.to_thread(_do_cancel), timeout=15)
            if ok:
                return ToolResult(content="Impression annulée.")
            return ToolResult(content="Échec annulation (stop_print=False)", is_error=True)
        except Exception as e:
            logger.error(f"Printer cancel error: {e}")
            return ToolResult(content=str(e), is_error=True)
