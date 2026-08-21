# Copyright (C) 2026 Max Ea
# Copyright (C) 2026 Maxime Song — modifications de ce fork
# This file is part of CRUSH-OS,   .


"""Outil de pilotage des vues visuelles.

Une « vue » n'est pas un écran physique : la machine qui héberge Crush
n'en a pas. Une vue est un module JavaScript chargé par la page d'accueil
de l'interface web, qui s'enregistre auprès de `Crush.views` puis reçoit
les évènements `show_view` / `hide_view` / `view_command` par WebSocket
(cf. `interfaces/ui/static/home.js` et `_shared.js`).

Deux conséquences dictent tout ce fichier :

1. `Crush.views.activate(id)` retourne silencieusement si `id` n'est pas
   enregistré. Un view_id inventé ne produit donc AUCUNE erreur côté
   navigateur — c'est à l'outil de refuser en amont, sinon il ment.
2. Le broadcast part vers zéro abonné quand aucun navigateur n'est
   ouvert, sans que rien ne le signale. Même conclusion : l'outil doit
   vérifier avant d'annoncer un affichage.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from crush.capabilities.tools.base import Tool, ToolResult
from crush.kernel.paths import SKILLS_INSTALLED_DIR, UI_STATIC_DIR

CITY_COORDS: dict[str, tuple[float, float]] = {
    # Villes françaises
    "paris": (48.8566, 2.3522),
    "lyon": (45.7640, 4.8357),
    "marseille": (43.2965, 5.3698),
    "bordeaux": (44.8378, -0.5792),
    "nice": (43.7102, 7.2620),
    "toulouse": (43.6047, 1.4442),
    "strasbourg": (48.5734, 7.7521),
    "nantes": (47.2184, -1.5536),
    "saint-lunaire": (48.6340, -2.1270),
    "rennes": (48.1173, -1.6778),
    "lille": (50.6292, 3.0573),
    # Monuments / lieux précis
    "tour eiffel": (48.8584, 2.2945),
    "eiffel tower": (48.8584, 2.2945),
    "arc de triomphe": (48.8738, 2.2950),
    "notre-dame": (48.8530, 2.3499),
    "sacré-cœur": (48.8867, 2.3431),
    "louvre": (48.8606, 2.3376),
    "notre dame": (48.8530, 2.3499),
    "colosseum": (41.8902, 12.4922),
    "colisée": (41.8902, 12.4922),
    "statue of liberty": (40.6892, -74.0445),
    "burj khalifa": (25.1972, 55.2744),
    "eiffel": (48.8584, 2.2945),
    # Villes internationales
    "new york": (40.7128, -74.0060),
    "los angeles": (34.0522, -118.2437),
    "tokyo": (35.6762, 139.6503),
    "beijing": (39.9042, 116.4074),
    "london": (51.5074, -0.1278),
    "londres": (51.5074, -0.1278),
    "berlin": (52.5200, 13.4050),
    "madrid": (40.4168, -3.7038),
    "rome": (41.9028, 12.4964),
    "dubai": (25.2048, 55.2708),
    "sydney": (-33.8688, 151.2093),
    "moscou": (55.7558, 37.6176),
    "moscow": (55.7558, 37.6176),
    "pékin": (39.9042, 116.4074),
    "shanghai": (31.2304, 121.4737),
    "singapour": (1.3521, 103.8198),
    "singapore": (1.3521, 103.8198),
    "seoul": (37.5665, 126.9780),
    "séoul": (37.5665, 126.9780),
    "toronto": (43.6532, -79.3832),
    "montréal": (45.5017, -73.5673),
    "montreal": (45.5017, -73.5673),
    "mexico": (19.4326, -99.1332),
    "bangkok": (13.7563, 100.5018),
    "amsterdam": (52.3676, 4.9041),
    "vienne": (48.2082, 16.3738),
    "vienna": (48.2082, 16.3738),
    "prague": (50.0755, 14.4378),
    "istanbul": (41.0082, 28.9784),
    "le caire": (30.0444, 31.2357),
    "cairo": (30.0444, 31.2357),
    "johannesburg": (-26.2041, 28.0473),
    "nairobi": (-1.2921, 36.8219),
    "mumbai": (19.0760, 72.8777),
    "delhi": (28.6139, 77.2090),
    "new delhi": (28.6139, 77.2090),
}

# Source unique des actions : schéma, message d'erreur et description en
# dérivent, pour qu'aucun des trois ne puisse mentir sur les deux autres.
ACTIONS: tuple[str, ...] = (
    "list",
    "show",
    "hide",
    "home",
    "fly_to",
    "globe_view",
    "zoom_in",
    "zoom_out",
    "view_command",
)
_ACTIONS_TXT = ", ".join(ACTIONS)

# Identifiant de la vue globe, câblé en dur côté navigateur (home.js) pour
# les actions cartographiques.
GLOBE_VIEW_ID = "globe"

_NO_CLIENT_MSG = (
    "Aucun navigateur n'est connecté à l'interface web : rien n'a été affiché. "
    "Les vues s'affichent dans le navigateur, pas sur la machine qui héberge "
    "Crush (elle n'a pas d'écran). Ouvrir la page d'accueil de Crush dans un "
    "navigateur, puis relancer la commande."
)

_INSTALL_HINT = (
    "Une vue est un skill de type « view » : il lui faut "
    "skills_data/installed/<nom>/skill.yaml contenant « type: view », ET ses "
    "assets dans src/crush/interfaces/ui/static/skills/<nom>/. Installation : "
    "POST /api/skills/install/<nom> ou la page « Capacités » de l'interface web."
)

# Une vue peut s'enregistrer avec un littéral (globe.js) ou via une constante
# déclarée en tête de fichier (les vues du catalogue). On accepte les deux :
# c'est le JS qui fait foi, skill.yaml ne déclare pas toujours view_id.
_JS_REGISTER_RE = re.compile(r"""Crush\.views\.register\(\s*["']([\w.\-]+)["']""")
_JS_VIEW_ID_RE = re.compile(r"""VIEW_ID\s*=\s*["']([\w.\-]+)["']""")

# Les fichiers de vue atteignent 175 Ko et sont relus à chaque appel de
# l'outil ; sur la carte SD d'une Pi ça se paie. Clé = (chemin, mtime,
# taille) : une réinstallation de vue invalide l'entrée d'elle-même.
_JS_IDS_CACHE: dict[tuple[str, int, int], frozenset[str]] = {}


@dataclass(frozen=True)
class ViewInfo:
    """Une vue que le navigateur chargera effectivement."""

    view_id: str
    skill: str
    description: str


def _ids_declared_in_js(path: Path) -> frozenset[str]:
    try:
        stat = path.stat()
        key = (str(path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        return frozenset()
    cached = _JS_IDS_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        source = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return frozenset()
    ids = frozenset(_JS_REGISTER_RE.findall(source)) | frozenset(_JS_VIEW_ID_RE.findall(source))
    _JS_IDS_CACHE[key] = ids
    return ids


def discover_views() -> tuple[list[ViewInfo], list[str]]:
    """Inventorie les vues, du point de vue du navigateur.

    Reproduit exactement la règle de `GET /api/skills/view-scripts`, seul
    chemin par lequel home.html charge des vues : un dossier d'assets sous
    `static/skills/<nom>/` ET un `skill.yaml` installé déclarant
    « type: view ». Un dossier d'assets sans skill.yaml n'est jamais servi
    — on le retourne à part, car c'est précisément l'état où une vue
    « existe sur le disque » sans être affichable, et où le diagnostic
    serait sinon incompréhensible.
    """
    views: list[ViewInfo] = []
    orphelins: list[str] = []
    base = UI_STATIC_DIR / "skills"
    if not base.exists():
        return views, orphelins

    import yaml

    for skill_static in sorted(base.iterdir()):
        if not skill_static.is_dir():
            continue
        nom = skill_static.name
        yaml_path = SKILLS_INSTALLED_DIR / nom / "skill.yaml"
        if not yaml_path.exists():
            orphelins.append(nom)
            continue
        try:
            meta = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
        except Exception:
            orphelins.append(nom)
            continue
        if meta.get("type") != "view":
            orphelins.append(nom)
            continue

        ids: set[str] = set()
        for asset in sorted(skill_static.iterdir()):
            if asset.suffix == ".js":
                ids |= _ids_declared_in_js(asset)
        if not ids:
            # Aucun identifiant lisible dans le JS : on retombe sur la
            # convention de l'installeur (skill.yaml:view_id, sinon le nom).
            ids = {str(meta.get("view_id") or nom)}

        description = str(meta.get("description") or "").strip()
        for view_id in sorted(ids):
            views.append(ViewInfo(view_id=view_id, skill=nom, description=description))
    return views, orphelins


class ShowViewTool(Tool):
    name = "show_view"
    description = (
        "Affiche ou pilote une vue visuelle DANS L'INTERFACE WEB de Crush, "
        "c'est-à-dire la page ouverte dans un navigateur (téléphone, ordinateur). "
        "La machine qui héberge Crush n'a pas d'écran : rien ne s'affiche sur "
        "elle, et aucune vue n'est affichable tant qu'aucun navigateur n'est "
        "connecté.\n\n"
        'COMMENCER PAR action="list" : elle donne les view_id réellement '
        "installés. Ne jamais deviner un view_id — un identifiant inconnu est "
        "refusé.\n\n"
        "ACTIONS GLOBALES :\n"
        "- list : lister les vues disponibles, avec leur view_id\n"
        "- show : afficher une vue par son view_id\n"
        "- hide : masquer une vue précise\n"
        '- home : retour à la sphère d\'accueil — pour "reviens", "retour", "ferme", "sphère"\n\n'
        "ACTIONS GLOBE UNIQUEMENT (lieux terrestres réels) :\n"
        "- fly_to : naviguer vers une ville, monument, pays sur Terre.\n"
        "  ⚠️ STRICTEMENT pour des lieux GÉOGRAPHIQUES TERRESTRES.\n"
        "  ❌ NE JAMAIS utiliser pour planètes, étoiles, constellations, objets célestes,\n"
        "     personnages, marques, sociétés — même si le nom ressemble à un lieu.\n"
        '  ✓ "Lyon", "Tokyo", "tour Eiffel", "mont Fuji"\n'
        '  ✗ "Vénus", "Mars" (planète), "Orion", "Bételgeuse", "Andromède"\n'
        "  Zoom : ville=10, monument=16, pays=5.\n"
        '- globe_view : dézoom total ("vue globale")\n'
        "- zoom_in / zoom_out : zoom avant / arrière sur le globe\n\n"
        "ACTIONS VUE-SPÉCIFIQUES (autres vues que le globe) :\n"
        "- view_command : envoyer une commande à une vue active.\n"
        "  Utilise ceci quand l'utilisateur demande quelque chose qui correspond à une\n"
        "  commande exposée par la vue active (le SYSTEM_PROMPT de la vue les liste).\n"
        '  Ex. astronomie : view_command(view_id="astronomy", '
        'command="focus_constellation", params={"name": "Orion"})\n\n'
        "Pour fly_to, le globe s'affiche automatiquement."
    )
    input_schema: dict = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": list(ACTIONS),
                "description": f"Action à effectuer. Valeurs acceptées : {_ACTIONS_TXT}.",
            },
            "view_id": {
                "type": "string",
                "description": (
                    'ID de la vue, tel que renvoyé par action="list". Requis pour '
                    "show / hide / view_command. Ignoré pour list / home / fly_to / "
                    "zoom_in / zoom_out / globe_view (ces dernières ciblent le globe)."
                ),
            },
            "location": {
                "type": "string",
                "description": "Lieu géographique TERRESTRE (fly_to uniquement).",
            },
            "command": {
                "type": "string",
                "description": "Commande spécifique à la vue (pour view_command).",
            },
            "params": {
                "type": "object",
                "description": "Paramètres pour view_command (clé/valeur libre).",
            },
            "zoom": {
                "type": "integer",
                "description": (
                    "Niveau de zoom (2–18)."
                    " Villes: 10, monuments/quartiers: 15–16, pays: 5, continent: 3."
                ),
                "default": 10,
            },
        },
        "required": ["action"],
    }

    def __init__(
        self,
        broadcast_event: Callable[[dict], None],
        count_clients: Callable[[], int] | None = None,
    ) -> None:
        self._broadcast = broadcast_event
        # `count_clients` reste optionnel tant que le bootstrap ne l'injecte
        # pas : sans lui on retombe sur l'introspection ci-dessous.
        self._count_clients = count_clients

    def _nb_clients(self) -> int | None:
        """Nombre de navigateurs abonnés, ou None si on ne peut pas savoir.

        L'outil ne reçoit qu'une fonction de broadcast — capabilities/ (L1)
        n'a pas le droit d'importer engine/ (L2) pour typer la file. On
        remonte donc à l'objet porteur de la méthode liée et on compte ses
        abonnés en duck typing. Quand la fonction injectée n'est pas une
        méthode de file (process voice_agent qui poste en HTTP, doubles de
        test), on retourne None : ne rien savoir ne doit pas se transformer
        en « aucun client », sinon on bloquerait des affichages valides.
        """
        if self._count_clients is not None:
            try:
                return int(self._count_clients())
            except Exception:
                return None
        abonnes = getattr(getattr(self._broadcast, "__self__", None), "_subscribers", None)
        try:
            return len(abonnes)  # type: ignore[arg-type]
        except TypeError:
            return None

    def _refus_sans_client(self) -> ToolResult | None:
        """Refuse une action d'affichage quand personne ne peut la voir."""
        if self._nb_clients() == 0:
            return ToolResult(content=_NO_CLIENT_MSG, is_error=True)
        return None

    def _refus_vue_absente(self, view_id: str) -> ToolResult | None:
        """Refuse un view_id que le navigateur n'a pas chargé.

        `Crush.views.activate` ignore silencieusement un id inconnu : sans
        ce contrôle, l'outil répondrait « Vue X affichée » alors que l'écran
        n'a pas bougé.
        """
        views, orphelins = discover_views()
        if any(v.view_id == view_id for v in views):
            return None
        if views:
            dispo = ", ".join(sorted({v.view_id for v in views}))
            return ToolResult(
                content=(
                    f"Vue « {view_id} » inconnue : le navigateur ne l'a pas chargée, "
                    f"l'afficher n'aurait aucun effet. Vues disponibles : {dispo}. "
                    'Utiliser show_view(action="list") pour le détail.'
                ),
                is_error=True,
            )
        return ToolResult(content=self._texte_aucune_vue(orphelins), is_error=True)

    def _texte_aucune_vue(self, orphelins: list[str]) -> str:
        constat = "Aucune vue n'est installée : le navigateur n'en chargerait aucune. "
        texte = constat + _INSTALL_HINT
        if orphelins:
            texte += (
                " Assets présents mais skill non installée : "
                + ", ".join(orphelins)
                + " — il manque leur skill.yaml (le nom au catalogue peut différer du "
                'dossier, ex. dossier "globe" ↔ skill "globe-view").'
            )
        return texte

    async def execute(
        self,
        action: str,
        view_id: str | None = None,
        location: str | None = None,
        zoom: int = 10,
        command: str | None = None,
        params: dict | None = None,
        **_: object,
    ) -> ToolResult:
        if action not in ACTIONS:
            return ToolResult(
                content=f"Action inconnue : {action}. Actions valides : {_ACTIONS_TXT}.",
                is_error=True,
            )

        if action == "list":
            return self._lister()

        if action in ("show", "hide", "view_command") and not view_id:
            return ToolResult(
                content=(
                    f"Paramètre view_id requis pour action={action}. "
                    'Obtenir les view_id valides avec show_view(action="list").'
                ),
                is_error=True,
            )

        # Les actions cartographiques visent toutes la vue globe en dur : sans
        # elle, le broadcast partirait dans le vide.
        if action in ("fly_to", "zoom_in", "zoom_out", "globe_view"):
            refus = self._refus_vue_absente(GLOBE_VIEW_ID)
            if refus is not None:
                return refus
        elif action in ("show", "hide", "view_command") and view_id:
            refus = self._refus_vue_absente(view_id)
            if refus is not None:
                return refus

        refus = self._refus_sans_client()
        if refus is not None:
            return refus

        if action == "show":
            self._broadcast({"type": "show_view", "view_id": view_id})
            return ToolResult(content=f"Vue {view_id} affichée dans l'interface web.")

        if action == "home":
            self._broadcast({"type": "show_home"})
            return ToolResult(content="Retour à la vue d'accueil.")

        if action == "hide":
            self._broadcast({"type": "hide_view", "view_id": view_id})
            return ToolResult(content=f"Vue {view_id} masquée.")

        if action == "fly_to":
            if not location:
                return ToolResult(content="Paramètre location requis pour fly_to.", is_error=True)
            coords = await self._geocode(location)
            if not coords:
                return ToolResult(content=f"Lieu introuvable : {location}", is_error=True)
            lat, lon = coords
            self._broadcast({"type": "show_view", "view_id": GLOBE_VIEW_ID})
            self._broadcast(
                {
                    "type": "view_command",
                    "view_id": GLOBE_VIEW_ID,
                    "command": "fly_to",
                    "params": {
                        "lat": lat,
                        "lon": lon,
                        "zoom": max(2, min(18, zoom)),
                        "location_name": location,
                    },
                }
            )
            return ToolResult(content=f"Navigation vers {location}.")

        if action == "zoom_out":
            self._broadcast(
                {
                    "type": "view_command",
                    "view_id": GLOBE_VIEW_ID,
                    "command": "zoom_out",
                    "params": {},
                }
            )
            return ToolResult(content="Vue dézoomée.")

        if action == "zoom_in":
            self._broadcast(
                {
                    "type": "view_command",
                    "view_id": GLOBE_VIEW_ID,
                    "command": "zoom_in",
                    "params": {},
                }
            )
            return ToolResult(content="Zoom avant.")

        if action == "view_command":
            if not command:
                return ToolResult(content="Paramètre command requis.", is_error=True)
            self._broadcast(
                {
                    "type": "view_command",
                    "view_id": view_id,
                    "command": command,
                    "params": params or {},
                }
            )
            return ToolResult(content=f"Commande {command} envoyée à {view_id}.")

        # globe_view — dernier cas de la liste ACTIONS.
        self._broadcast({"type": "show_view", "view_id": GLOBE_VIEW_ID})
        self._broadcast(
            {
                "type": "view_command",
                "view_id": GLOBE_VIEW_ID,
                "command": "globe_view",
                "params": {},
            }
        )
        return ToolResult(content="Vue globe globale.")

    def _lister(self) -> ToolResult:
        views, orphelins = discover_views()
        if not views:
            return ToolResult(content=self._texte_aucune_vue(orphelins))

        lignes = [f"{len(views)} vue(s) affichable(s) dans l'interface web :"]
        for v in views:
            suffixe = f" — {v.description}" if v.description else ""
            lignes.append(f"- {v.view_id} (skill « {v.skill} »){suffixe}")
        lignes.append('Afficher : show_view(action="show", view_id="<id>").')
        if orphelins:
            lignes.append(
                "Assets présents mais skill non installée (donc non affichables) : "
                + ", ".join(orphelins)
                + "."
            )
        if self._nb_clients() == 0:
            lignes.append(
                "Attention : aucun navigateur connecté actuellement — rien ne "
                "s'affichera tant qu'une page de l'interface web ne sera pas ouverte."
            )
        return ToolResult(content="\n".join(lignes))

    async def _geocode(self, location: str) -> tuple[float, float] | None:
        key = location.lower().strip()
        if key in CITY_COORDS:
            return CITY_COORDS[key]
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                r = await client.get(
                    "https://nominatim.openstreetmap.org/search",
                    params={"q": location, "format": "json", "limit": 1},
                    headers={"User-Agent": "Crush/3.0"},
                )
                results = r.json()
                if results:
                    return float(results[0]["lat"]), float(results[0]["lon"])
        except Exception:
            pass
        return None
