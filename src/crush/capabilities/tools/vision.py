# Copyright (C) 2026 Max Ea
# Copyright (C) 2026 Maxime Song — modifications de ce fork
# This file is part of CRUSH-OS,   .
 

"""Capture et analyse d'image sur la machine qui héberge l'assistant.

DEUX ÉCHECS QUI NE SE SOIGNENT PAS PAREIL
=========================================

L'outil confondait « tu ne m'as pas donné le droit » et « il n'y a rien à
capturer ». Les deux tombaient sur le même message, qui renvoyait aux
Préférences Système de macOS — sur un serveur Debian sans écran. L'utilisateur
cochait donc une permission qui ne pouvait rien changer.

Une permission refusée se répare en un clic ; un serveur sans caméra ni serveur
d'affichage ne se répare pas du tout. Le matériel est donc diagnostiqué AVANT
la permission, et avant toute tentative de capture : un remède impossible
proposé en premier fait perdre plus de temps que pas de remède du tout.
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import io
import os
import platform
import subprocess
from pathlib import Path

from loguru import logger
from openai import AsyncOpenAI
from PIL import Image, ImageGrab

from crush.capabilities.tools.base import Tool, ToolResult
from crush.kernel.contracts import VisualMemory
from crush.kernel.permissions import permissions as _perms
from crush.kernel.remote_agents import registry as _agents_distants
from crush.kernel.settings import settings


class CaptureImpossible(RuntimeError):
    """Échec de capture dont le message est déjà rédigé pour l'utilisateur.

    Les captures tournent dans un executor. Sans ce transport, la raison
    précise finissait dans les logs et l'appelant ne recevait qu'un `None`,
    traduit en un message générique et faux. Porter le texte dans l'exception
    garantit que ce que le modèle lit est ce qui s'est réellement passé.
    """


# ── Détection du matériel ────────────────────────────────────────────────────

# Rendus indirects pour que les tests puissent pointer ailleurs qu'à la racine.
_DEV = Path("/dev")
_SYS_V4L2 = Path("/sys/class/video4linux")

# Pilotes video4linux qui exposent un /dev/video* sans être une caméra : le
# noyau du Raspberry Pi enregistre ses codecs matériels et son ISP comme des
# nœuds v4l2. Les compter ferait annoncer une caméra sur une machine qui n'en
# a aucune, et le diagnostic mentirait dans le sens le plus coûteux.
_V4L2_SANS_CAPTEUR = ("bcm2835-codec", "bcm2835-isp", "rpivid", "rpi-hevc", "pispbe")

_INSTALL_OPENCV = (
    "`uv pip install opencv-python` (l'extra `uv sync --extra vision` marche "
    "aussi mais tire ultralytics/torch, ~2,5 Go sur ARM)"
)

_OPENCV_ABSENT = (
    "Capture caméra impossible : le module OpenCV (cv2) n'est pas installé sur "
    "cette machine. Il vit dans un extra, justement parce qu'il est lourd sur "
    f"ARM. Remède : {_INSTALL_OPENCV}, puis redémarrer le service crush-api."
)

_SANS_CLE_OPENAI = (
    "Vision indisponible : aucune clé OpenAI configurée. L'analyse d'image "
    "passe par l'API OpenAI (modèle réglé par VISION_MODEL). Remède : "
    "renseigner OPENAI_API_KEY dans le .env du serveur puis redémarrer "
    "crush-api. La capture n'a pas été tentée."
)

# Le modèle écrit spontanément « camera » ou « écran ». Refuser ces variantes
# ne protège rien : ça transforme une demande claire en erreur de syntaxe.
_ALIAS_SOURCE = {
    "webcam": "webcam",
    "camera": "webcam",
    "caméra": "webcam",
    "cam": "webcam",
    "screen": "screen",
    "ecran": "screen",
    "écran": "screen",
    "display": "screen",
    "desktop": "screen",
}


def _nom_v4l2(noeud: str) -> str:
    """Nom du pilote derrière /dev/<noeud>, vide s'il n'est pas lisible."""
    try:
        return (_SYS_V4L2 / noeud / "name").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _cameras_detectees() -> list[str] | None:
    """Caméras visibles sur l'hôte, ou None quand on ne peut pas conclure.

    None hors Linux : macOS et Windows n'ont pas d'équivalent aussi bon marché
    que /dev/video*, et affirmer une absence sans preuve serait pire que de
    tenter la capture et de rapporter l'échec réel.
    """
    if platform.system() != "Linux":
        return None
    trouvees: list[str] = []
    for noeud in sorted(_DEV.glob("video*")):
        nom = _nom_v4l2(noeud.name)
        if nom and nom.startswith(_V4L2_SANS_CAPTEUR):
            continue
        # Nom illisible : le nœud est conservé. Une capture qui échoue vaut
        # mieux qu'un « aucune caméra » prononcé à tort.
        trouvees.append(f"{noeud} ({nom})" if nom else str(noeud))
    return trouvees


def _resume_cameras() -> str:
    vues = _cameras_detectees()
    if vues is None:
        return "non énumérables sur cette plateforme"
    return ", ".join(vues) if vues else "aucune"


def _session_graphique() -> bool | None:
    """Existe-t-il un serveur d'affichage ? None quand la question ne se pose pas.

    Sur macOS et Windows la capture passe par l'API système, qui ne dépend
    d'aucune variable d'environnement : on ne préjuge pas.
    """
    if platform.system() != "Linux":
        return None
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _opencv_disponible() -> bool:
    """Présence de cv2 sans l'importer — un import coûte ~1 s sur ARM."""
    try:
        return importlib.util.find_spec("cv2") is not None
    except (ImportError, ValueError):
        return False


def _piste_ecran_utilisateur() -> str:
    """Ce qu'on peut honnêtement proposer à qui voulait voir SON écran.

    Le registre des agents distants est interrogé plutôt que supposé :
    `scripts/agent_pc.py` n'annonce aujourd'hui que volume, applications,
    verrouillage et extinction. Promettre une capture via remote_pc serait un
    mensonge vérifiable ; le jour où l'agent gagnera cette action, ce message
    la nommera sans qu'on ait à y revenir.
    """
    for agent in _agents_distants.list_agents():
        for nom in agent.actions:
            if "screen" in nom.lower() or "capture" in nom.lower():
                return (
                    f"Le poste « {agent.name} » annonce l'action « {nom} » : "
                    "appelle l'outil remote_pc avec cette action pour voir cet écran."
                )
    return (
        "Aucun outil ne sait capturer l'écran de l'utilisateur : remote_pc pilote "
        "son poste (volume, applications, verrouillage, extinction) mais n'expose "
        "aucune action de capture. Lui demander de décrire son écran est, pour "
        "l'instant, la seule voie."
    )


class VisionTool(Tool):
    """Capture et analyse une frame webcam ou écran via GPT-4o Vision.

    Règle absolue : aucune frame n'est jamais écrite sur le disque.
    Tout transit en RAM (bytes) : capturé → encodé base64 → envoyé → oublié.
    """

    name = "vision"
    description = (
        "Capture et analyse une image via GPT-4o Vision. "
        "IMPORTANT : la capture porte sur la machine qui HÉBERGE l'assistant — "
        "un serveur sans écran ni caméra, sauf périphérique branché dessus — et "
        "jamais sur l'ordinateur de l'utilisateur, qui n'est pas joignable par "
        "cet outil. "
        "Actions disponibles :\n"
        "- 'snapshot' (défaut) : capture + question libre (usage général). "
        "Utilise quand l'utilisateur dit : 'regarde', 'tu vois ça ?', 'décris ce que tu vois'.\n"
        "- 'read_document' : extrait et transcrit le texte d'un document physique "
        "(livre, facture, datasheet, note manuscrite). "
        "Utilise quand l'utilisateur dit : 'lis ça', 'qu'est-ce qu'il y a écrit'.\n"
        "- 'analyze_schema' : analyse un schéma électronique, PCB ou diagramme. "
        "Utilise quand l'utilisateur dit : 'regarde ce schéma', 'analyse ce PCB'.\n"
        "- 'recall' : retrouve un souvenir visuel passé dans la mémoire. "
        "Utilise quand l'utilisateur dit : 'tu te souviens de ce que tu avais vu ?', "
        "'le schéma que je t'avais montré'. Seule action qui n'exige aucun matériel : "
        "source n'est pas requis."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["snapshot", "read_document", "analyze_schema", "recall"],
                "description": "Action à effectuer. Défaut: 'snapshot'.",
            },
            "source": {
                "type": "string",
                "enum": ["webcam", "screen"],
                "description": (
                    "Source de la capture, sur le serveur qui héberge l'assistant :"
                    " 'webcam' pour une caméra branchée dessus, 'screen' pour son"
                    " écran. Non requis pour 'recall'."
                ),
            },
            "question": {
                "type": "string",
                "description": (
                    "Question précise à poser sur l'image ou terme de recherche pour 'recall'. "
                    "Ex: 'Y a-t-il des erreurs dans ce code ?'"
                ),
            },
            "detail": {
                "type": "string",
                "enum": ["low", "high"],
                "description": (
                    "Niveau de détail. 'high' pour code/texte fin. Défaut: 'low'"
                    " (forcé 'high' pour read_document et analyze_schema)."
                ),
            },
        },
        "required": ["question"],
    }

    def __init__(self, visual_memory: VisualMemory) -> None:
        # Client OpenAI construit À LA DEMANDE. L'instancier ici (eager) crashait
        # tout le démarrage de l'API si l'utilisateur n'a pas de clé OpenAI (ex.
        # backend principal Mistral/Gemini/local) : AsyncOpenAI(api_key="") lève
        # « Missing credentials ». La vision se désactive proprement à l'usage.
        self._openai: AsyncOpenAI | None = None
        self._visual_memory = visual_memory

    def _get_openai_client(self) -> AsyncOpenAI | None:
        if self._openai is None:
            key = settings.openai_api_key.get_secret_value()
            if not key:
                return None
            self._openai = AsyncOpenAI(api_key=key)
        return self._openai

    async def execute(
        self,
        question: str,
        action: str = "snapshot",
        source: str = "webcam",
        detail: str = "low",
        **_: object,
    ) -> ToolResult:

        # ── Recall — pas de capture ───────────────────────────────────────────
        if action == "recall":
            matches = await self._visual_memory.search(question)
            if matches:
                return ToolResult(
                    content="Voici ce dont je me souviens :\n\n" + "\n\n---\n\n".join(matches)
                )
            return ToolResult(content="Je n'ai pas de souvenir visuel correspondant à ta demande.")

        source_normalisee = _ALIAS_SOURCE.get(str(source).strip().lower())
        if source_normalisee is None:
            return ToolResult(
                content=f"Source inconnue : '{source}'. Utilise 'webcam' ou 'screen'.",
                is_error=True,
            )

        # ── Refus argumenté, avant toute tentative ────────────────────────────
        blocage = (
            self._blocage_webcam() if source_normalisee == "webcam" else self._blocage_ecran()
        )
        if blocage is not None:
            logger.info("Vision refusée ({}) : {}", source_normalisee, blocage.split("\n")[0])
            return ToolResult(content=blocage, is_error=True)

        # Clé vérifiée AVANT la capture : sans elle l'image ne sera jamais
        # analysée, et allumer la caméra pour jeter la frame ensuite serait une
        # intrusion gratuite dans la pièce où vit le serveur.
        client = self._get_openai_client()
        if client is None:
            return ToolResult(content=_SANS_CLE_OPENAI, is_error=True)

        loop = asyncio.get_running_loop()
        capturer = (
            self._capture_webcam if source_normalisee == "webcam" else self._capture_screen
        )
        try:
            jpeg_bytes = await loop.run_in_executor(None, capturer)
        except CaptureImpossible as e:
            logger.warning("Vision : capture {} impossible — {}", source_normalisee, e)
            return ToolResult(content=str(e), is_error=True)

        if not jpeg_bytes:
            return ToolResult(
                content=(
                    f"Capture {source_normalisee} vide, sans erreur signalée par le "
                    "périphérique. Regarde les logs du service : "
                    "`journalctl -u crush-api -n 50`."
                ),
                is_error=True,
            )

        # ── Paramètres selon l'action ─────────────────────────────────────────
        if action in ("read_document", "analyze_schema"):
            detail = "high"

        b64 = base64.b64encode(jpeg_bytes).decode("utf-8")
        logger.debug(
            "Vision capture",
            action=action,
            source=source_normalisee,
            size_kb=round(len(jpeg_bytes) / 1024, 1),
        )

        prompt = self._build_prompt(action, question)

        try:
            response = await client.chat.completions.create(
                model=settings.vision_model,
                max_tokens=2000 if action in ("read_document", "analyze_schema") else 1024,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/jpeg;base64,{b64}",
                                    "detail": detail,
                                },
                            },
                            {"type": "text", "text": prompt},
                        ],
                    }
                ],
            )
            result_text = response.choices[0].message.content or ""
            logger.debug("Vision result", action=action, preview=result_text[:80])

            # ── Stocker dans la mémoire visuelle (fire and forget) ────────────
            asyncio.create_task(
                self._store_memory(result_text, source_normalisee, action, question),
                name="vision-memory",
            )

            return ToolResult(content=result_text)
        except Exception as e:
            logger.error("Vision API error", error=str(e))
            return ToolResult(content=f"Erreur GPT-4o Vision : {e}", is_error=True)

    # ── Diagnostic préalable ──────────────────────────────────────────────────

    def _blocage_webcam(self) -> str | None:
        """Raison de ne pas même essayer, ou None si la capture est jouable.

        L'ordre compte. Le matériel d'abord : envoyer l'utilisateur cocher une
        permission sur une machine sans caméra, c'est lui vendre un remède qui
        ne peut rien guérir. La dépendance en dernier : installer OpenCV sur un
        serveur sans caméra ne sert à rien non plus.
        """
        cameras = _cameras_detectees()
        if cameras is not None and not cameras:
            return (
                "Aucune caméra sur la machine qui héberge l'assistant "
                f"({platform.system()}/{platform.machine()}) : pas un seul "
                "périphérique de capture sous /dev/video*. Ce n'est pas une "
                "question de permission — l'accorder ne ferait apparaître aucune "
                "caméra.\n"
                "Remède : brancher une webcam USB sur le serveur, installer le "
                f"support de capture ({_INSTALL_OPENCV}), puis redémarrer "
                "crush-api. La webcam de l'ordinateur de l'utilisateur n'est pas "
                "joignable depuis ici, aucun outil ne l'expose."
            )
        if not _perms.get("camera"):
            return (
                "Permission « caméra » désactivée — c'est le seul blocage, le "
                f"matériel est là (caméras vues : {_resume_cameras()}). "
                "Pour l'accorder : interface web → Réglages → Permissions → "
                "Caméra, ou `PATCH /api/permissions/camera` avec "
                '`{"enabled": true}`.'
            )
        if not _opencv_disponible():
            return _OPENCV_ABSENT
        return None

    def _blocage_ecran(self) -> str | None:
        """Raison de ne pas même essayer une capture d'écran, ou None."""
        if _session_graphique() is False:
            return (
                "Aucun écran à capturer : la machine qui héberge l'assistant n'a "
                "pas de session graphique (ni DISPLAY ni WAYLAND_DISPLAY). Ce "
                "n'est pas une question de permission — l'accorder ne ferait "
                "apparaître aucun écran, et aucune installation n'y changera rien "
                "tant que le serveur reste headless.\n"
                + _piste_ecran_utilisateur()
            )
        if not _perms.get("screen"):
            return (
                "Permission « capture d'écran » désactivée — c'est le seul "
                "blocage, une session graphique est bien présente. Pour "
                "l'accorder : interface web → Réglages → Permissions → Écran, ou "
                '`PATCH /api/permissions/screen` avec `{"enabled": true}`.'
            )
        return None

    def _build_prompt(self, action: str, question: str) -> str:
        if action == "read_document":
            return (
                "Lis et transcris intégralement le texte visible sur ce document. "
                "Conserve la structure (titres, paragraphes, tableaux, listes). "
                "Si c'est une note manuscrite, transcris telle quelle. "
                "Réponds uniquement avec le contenu extrait, sans commentaires."
            )
        if action == "analyze_schema":
            return (
                f"Tu es un expert en électronique, PCB design et schémas techniques. "
                f"Identifie les composants, connexions et problèmes potentiels. "
                f"Sois précis sur les références si lisibles. "
                f"{question or 'Analyse ce schéma/PCB.'}"
            )
        return question

    async def _store_memory(self, description: str, source: str, action: str, context: str) -> None:
        try:
            await self._visual_memory.store(description=description, source=source, context=context)
        except Exception as e:
            logger.debug("Visual memory store failed", error=str(e))

    # ── Captures ──────────────────────────────────────────────────────────────

    def _capture_webcam(self) -> bytes:
        """Ouvre la webcam, chauffe 3 frames, capture, ferme. Zéro fichier disque."""
        try:
            import cv2  # type: ignore[import-untyped]
        except ImportError as e:
            raise CaptureImpossible(_OPENCV_ABSENT) from e

        index = settings.vision_webcam_index
        cap = None
        try:
            cap = cv2.VideoCapture(index)
            if not cap.isOpened():
                raise CaptureImpossible(
                    f"La caméra d'index {index} ne s'ouvre pas (caméras vues : "
                    f"{_resume_cameras()}). Deux causes possibles : l'index est "
                    "faux — corrige VISION_WEBCAM_INDEX — ou un autre processus "
                    "tient déjà le flux (daemon vision, navigateur)."
                )

            # 3 frames de chauffe — exposition + balance des blancs se stabilisent
            for _ in range(3):
                cap.read()

            ret, frame = cap.read()
            if not ret or frame is None:
                raise CaptureImpossible(
                    f"La caméra d'index {index} s'ouvre mais ne délivre aucune "
                    "image. Rebranche le périphérique, ou vérifie qu'aucun autre "
                    "processus ne le monopolise."
                )

            # BGR (OpenCV) → RGB → PIL → JPEG en RAM, zéro disque
            frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            pil_img = Image.fromarray(frame_rgb)
            buf = io.BytesIO()
            pil_img.save(buf, format="JPEG", quality=settings.vision_jpeg_quality, optimize=True)
            return buf.getvalue()

        except CaptureImpossible:
            raise
        except Exception as e:
            # Toute autre exception d'OpenCV est illisible pour l'utilisateur ;
            # on la nomme au lieu de la laisser remonter en trace brute.
            raise CaptureImpossible(
                f"Erreur inattendue pendant la capture caméra ({type(e).__name__}) : {e}"
            ) from e
        finally:
            if cap is not None:
                cap.release()

    def _capture_screen(self) -> bytes:
        """Capture l'écran en RAM. macOS : screencapture stdout. Fallback : PIL.ImageGrab."""
        if platform.system() == "Darwin":
            result = self._capture_screen_macos()
            if result:
                return result
        return self._capture_screen_pil()

    def _capture_screen_macos(self) -> bytes | None:
        """screencapture → fichier temp → lecture → suppression immédiate.

        Le fichier existe < 100ms. Le stdout de screencapture est vide sur macOS Sonoma+
        (bug Apple connu), d'où le passage par un fichier temporaire.
        """
        import tempfile

        fd, tmp_path = tempfile.mkstemp(suffix=".jpg", prefix="crush_screen_")
        os.close(fd)
        try:
            proc = subprocess.run(
                ["screencapture", "-x", "-t", "jpg", tmp_path],
                capture_output=True,
                timeout=10,
            )
            if proc.returncode != 0:
                logger.debug("screencapture échoué", returncode=proc.returncode)
                return None
            with open(tmp_path, "rb") as f:
                data = f.read()
            if not data:
                logger.debug("screencapture: fichier vide")
                return None
            logger.debug("Screen capture via screencapture", kb=round(len(data) / 1024, 1))
            return self._resize_jpeg(data)
        except Exception as e:
            logger.debug("screencapture tempfile failed", error=str(e))
            return None
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    def _capture_screen_pil(self) -> bytes:
        """PIL.ImageGrab — fallback cross-platform."""
        try:
            screenshot = ImageGrab.grab()
        except Exception as e:
            # Pillow lève ici quand il a été compilé sans XCB, ou quand ni
            # `gnome-screenshot` ni `grim` ne sont installés sous Wayland. Le
            # texte de l'exception est la seule information exploitable : on le
            # rend tel quel au lieu de le noyer sous un message générique.
            raise CaptureImpossible(
                f"Capture d'écran refusée par Pillow : {e}. Sur Linux, ImageGrab "
                "exige un Pillow compilé avec XCB (X11) ou le binaire `grim` "
                "(Wayland) — et, dans tous les cas, une session graphique ouverte."
            ) from e

        max_w = settings.vision_screen_max_width
        if screenshot.width > max_w:
            ratio = max_w / screenshot.width
            screenshot = screenshot.resize(
                (max_w, int(screenshot.height * ratio)),
                Image.LANCZOS,
            )
        buf = io.BytesIO()
        screenshot.save(buf, format="JPEG", quality=settings.vision_jpeg_quality, optimize=True)
        logger.debug(
            "Screen capture PIL",
            size=f"{screenshot.width}x{screenshot.height}",
            kb=round(buf.tell() / 1024, 1),
        )
        return buf.getvalue()

    def _resize_jpeg(self, jpeg_bytes: bytes) -> bytes:
        """Redimensionne un JPEG si l'écran dépasse vision_screen_max_width."""
        try:
            img = Image.open(io.BytesIO(jpeg_bytes))
            max_w = settings.vision_screen_max_width
            if img.width <= max_w:
                return jpeg_bytes
            ratio = max_w / img.width
            img = img.resize((max_w, int(img.height * ratio)), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=settings.vision_jpeg_quality, optimize=True)
            logger.debug("Screen resized", width=max_w, kb=round(buf.tell() / 1024, 1))
            return buf.getvalue()
        except Exception:
            return jpeg_bytes
