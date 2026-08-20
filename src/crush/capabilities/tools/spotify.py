# Copyright (C) 2026 Max Ea
# Copyright (C) 2026 Maxime Song — modifications de ce fork
# This file is part of CRUSH-OS,   .
 

from __future__ import annotations

import asyncio
import json

import httpx
from loguru import logger

from crush.capabilities.tools.base import Tool, ToolResult
from crush.capabilities.tools.spotify_auth import _get_access_token
from crush.kernel.paths import MEMORY_DATA_DIR
from crush.kernel.persistance import ecrire_atomique
from crush.kernel.settings import settings

_API_BASE = "https://api.spotify.com/v1"

# Source unique des actions supportées : la description de l'outil, l'enum du
# schéma et le message « action inconnue » en sont TOUS dérivés. C'est leur
# désynchronisation qui faisait travailler le modèle à l'aveugle — la
# description annonçait six actions alors que le code en servait huit, et
# aucune ne permettait de savoir ce qui jouait.
_ACTIONS: dict[str, str] = {
    "status": (
        "ce qui joue (titre, artiste), si la lecture est en cours et sur quel "
        "appareil — à appeler AVANT d'affirmer qu'une musique joue"
    ),
    "play": "reprendre la lecture",
    "pause": "mettre en pause",
    "toggle": "basculer lecture/pause",
    "next": "piste suivante",
    "previous": "piste précédente",
    "search_track": "chercher un morceau par nom ou artiste et le jouer (requiert 'query')",
    "search_playlist": "chercher une playlist par nom et la jouer (requiert 'query')",
    "volume_delta": "ajuster le volume de 'delta' points (ex. delta=-10 pour baisser de 10 %)",
}


def _actions_documentees() -> str:
    return " ; ".join(f"'{nom}' : {role}" for nom, role in _ACTIONS.items())


def _est_onglet(appareil: dict) -> bool:
    """L'appareil est-il le lecteur embarqué dans la page web de l'assistant ?

    Le Web Playback SDK de `home.js` enregistre un appareil portant le nom
    d'affichage de l'assistant. Spotify n'expose aucun identifiant stable qui
    permettrait de le reconnaître autrement : le nom est le seul indice.
    """
    nom = (appareil.get("name") or "").strip().lower()
    return nom == settings.display_assistant_name.strip().lower()


# ── Appareil préféré ─────────────────────────────────────────────────────────
#
# Sans mémoire, il fallait nommer l'enceinte à chaque demande — « lance Iris sur
# le PC » — sous peine de retomber sur le premier appareil venu, souvent
# l'onglet du navigateur, dont la lecture meurt avec la page.  On retient donc
# le dernier appareil EXTERNE réellement utilisé, et on y revient tant qu'il
# est joignable.
#
# Le nom plutôt que l'identifiant : Spotify renouvelle les `device_id` à chaque
# session de l'application, alors que « MacBook de Max » reste stable.

_FICHIER_APPAREIL = MEMORY_DATA_DIR / "spotify_appareil.json"


def _appareil_prefere() -> str:
    """Nom du dernier appareil externe utilisé, ou chaîne vide."""
    try:
        donnees = json.loads(_FICHIER_APPAREIL.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    return str(donnees.get("nom", "")).strip() if isinstance(donnees, dict) else ""


def _memoriser_appareil(appareil: dict) -> None:
    """Retient un appareil externe. L'onglet n'est JAMAIS mémorisé.

    Le retenir ramènerait la lecture dans la page à chaque fois, y compris
    quand une enceinte est disponible — l'inverse de ce qu'on cherche.
    """
    if _est_onglet(appareil):
        return
    nom = (appareil.get("name") or "").strip()
    if not nom or nom == _appareil_prefere():
        return
    try:
        ecrire_atomique(_FICHIER_APPAREIL, json.dumps({"nom": nom}, ensure_ascii=False))
        logger.info("Spotify : appareil préféré retenu — « {} »", nom)
    except OSError as exc:
        logger.warning("Spotify : préférence d'appareil non enregistrée ({})", exc)



def _decrire_appareil(appareil: dict) -> str:
    nom = (appareil.get("name") or "appareil sans nom").strip()
    type_ = (appareil.get("type") or "").strip().lower()
    return f"« {nom} » ({type_})" if type_ else f"« {nom} »"


def _minutes(ms: object) -> str:
    """Millisecondes -> m:ss.

    L'état est souvent restitué à l'oral : « 2:35 sur 3:14 » se dit, pas
    « 155009 ms ».
    """
    try:
        secondes = max(0, int(ms or 0)) // 1000
    except (TypeError, ValueError):
        return "?"
    return f"{secondes // 60}:{secondes % 60:02d}"


def _echec_lecture(nom: str, code: int) -> str:
    """Message d'echec exploitable par le modele, pas un simple code HTTP.

    Le 404 de l'API Spotify signifie « aucun appareil actif » — cas le plus
    frequent quand l'application n'est ouverte nulle part. On le dit
    explicitement ET on indique la marche a suivre, pour que l'assistant
    enchaine de lui-meme (lancer Spotify sur le poste via `remote_pc`, puis
    reessayer) au lieu de rendre un code d'erreur brut a l'utilisateur.
    """
    if code == 404:
        return (
            f"« {nom} » trouvé, mais aucun appareil Spotify actif. "
            "Ouvre Spotify sur un appareil — sur l'ordinateur, tu peux le faire "
            "toi-même avec remote_pc (action app_launch, name Spotify), attendre "
            "quelques secondes, puis réessayer."
        )
    if code == 403:
        return (
            f"« {nom} » trouvé, mais la lecture est refusée. "
            "Le contrôle de lecture par l'API Spotify exige un compte Premium."
        )
    return f"« {nom} » trouvé, mais la lecture a échoué (code {code})."


class SpotifyTool(Tool):
    name = "spotify_control"
    description = (
        "Contrôle la lecture Spotify du compte de l'utilisateur — ses appareils "
        "Spotify (ordinateur, téléphone, enceinte), et non la machine qui héberge "
        "l'assistant. Aucune action de contrôle ne renvoie ce qui est en train de "
        "jouer : utilise 'status' pour le savoir, sinon tu réponds à l'aveugle."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": list(_ACTIONS),
                "description": f"Action à effectuer. {_actions_documentees()}.",
            },
            "query": {
                "type": "string",
                "description": "Terme de recherche (requis pour search_track et search_playlist).",
            },
            "delta": {
                "type": "integer",
                "description": (
                    "Variation de volume en points de pourcentage, requise pour "
                    "volume_delta. Négative pour baisser (ex. -10)."
                ),
            },
        },
        "required": ["action"],
    }

    async def execute(self, **kwargs: object) -> ToolResult:
        # Le modèle envoie parfois « Status » ou « play » avec une espace : la
        # normalisation évite un aller-retour d'erreur pour une casse.
        action = str(kwargs.get("action", "")).strip().lower()
        query = str(kwargs.get("query", ""))

        token = await _get_access_token()
        if not token:
            return ToolResult(
                content="Spotify non connecté. Va sur /api/spotify/auth pour autoriser.",
                is_error=True,
            )

        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                headers = {"Authorization": f"Bearer {token}"}

                async def _lister_appareils() -> list[dict]:
                    """Appareils Spotify visibles du compte, liste vide si l'appel échoue.

                    Extrait de `_active_device_id` pour que `status` puisse dire
                    ce qui est DISPONIBLE sans dupliquer l'appel ni, surtout,
                    dupliquer la règle de reconnaissance de l'onglet.
                    """
                    r = await client.get(f"{_API_BASE}/me/player/devices", headers=headers)
                    if not r.is_success:
                        return []
                    return [d for d in (r.json().get("devices") or []) if d]

                async def _active_device_id() -> str | None:
                    """Choisit l'appareil de lecture, en EVITANT l'onglet du navigateur.

                    L'interface web heberge un lecteur Spotify (Web Playback SDK,
                    cf. `home.js`) qui apparait comme un appareil portant le nom
                    de l'assistant. Y jouer la musique la met en concurrence avec
                    la voix de synthese, servie par la MEME page : la reponse de
                    l'assistant interrompait la lecture quelques secondes apres
                    l'avoir lancee.

                    Le code preferait justement cet appareil. On inverse : une
                    application Spotify externe (ordinateur, telephone) d'abord,
                    l'onglet seulement en dernier recours — mieux vaut une lecture
                    fragile que pas de lecture du tout.
                    """
                    devices = await _lister_appareils()
                    if not devices:
                        return None

                    externes = [d for d in devices if not _est_onglet(d)]
                    # Un appareil externe DEJA actif est le meilleur choix : c'est
                    # celui que l'utilisateur ecoute.
                    actif_externe = next((d for d in externes if d.get("is_active")), None)
                    # A defaut, celui retenu la derniere fois : sans cette memoire
                    # il fallait nommer l'enceinte a chaque demande, sous peine de
                    # retomber sur le premier appareil venu.
                    prefere_nom = _appareil_prefere().lower()
                    prefere = next(
                        (
                            d
                            for d in externes
                            if (d.get("name") or "").strip().lower() == prefere_nom
                        ),
                        None,
                    ) if prefere_nom else None
                    choisi = actif_externe or prefere or (externes[0] if externes else devices[0])
                    _memoriser_appareil(choisi)
                    if prefere is not None and actif_externe is None:
                        logger.info(
                            "Spotify : reprise sur l'appareil habituel — {}",
                            _decrire_appareil(prefere),
                        )
                    if not externes:
                        logger.info(
                            "Spotify : aucun appareil externe, lecture dans l'onglet. "
                            "Elle s'arrête si la page est fermée ou rechargée ; ouvrir "
                            "Spotify sur l'ordinateur donne une lecture indépendante."
                        )
                    return str(choisi["id"])

                async def _play(body: dict | None = None) -> httpx.Response:
                    """Lance la lecture sur l'appareil retenu.

                    L'appareil est choisi AVANT l'appel, pas seulement en secours
                    sur un 404 : sinon, quand l'onglet du navigateur est deja
                    actif, la lecture y part directement et n'atteint jamais la
                    selection — c'etait le cas nominal, et celui qui posait
                    probleme.
                    """
                    device_id = await _active_device_id()
                    r = await client.put(
                        f"{_API_BASE}/me/player/play",
                        headers=headers,
                        params={"device_id": device_id} if device_id else None,
                        json=body or {},
                    )
                    if r.status_code == 404:
                        if not device_id:
                            return r
                        # `play: True` et non False : le transfert doit DEMARRER
                        # la lecture. Avec False, le morceau se chargeait puis
                        # restait en pause, et il fallait dire « mets play ».
                        await client.put(
                            f"{_API_BASE}/me/player",
                            headers=headers,
                            json={"device_ids": [device_id], "play": True},
                        )
                        # Le transfert est ASYNCHRONE cote Spotify : enchainer
                        # sans attendre vise un appareil qui n'a pas encore pris
                        # la main, et la commande se perd.
                        await asyncio.sleep(0.5)
                        r = await client.put(
                            f"{_API_BASE}/me/player/play",
                            headers=headers,
                            params={"device_id": device_id},
                            json=body or {},
                        )
                    return await _garantir_lecture(r, body)

                async def _garantir_lecture(r: httpx.Response, body: dict | None) -> httpx.Response:
                    """Verifie que la lecture a REELLEMENT demarre, et la relance sinon.

                    Comportement constate sur l'API Spotify, appareil en pause :

                        PUT /me/player/play {"uris": [...]}  -> 204, is_playing FALSE
                        PUT /me/player/play {}               -> 200, is_playing TRUE

                    Envoyer un `uris` CHARGE le morceau sans le lancer. Il faut
                    un second appel a CORPS VIDE — l'equivalent d'appuyer sur
                    lecture. Relancer avec le meme `uris` ne sert a rien : cela
                    recharge le morceau et le laisse en pause.

                    Les playlists (`context_uri`) demarrent, elles, du premier
                    coup — d'ou une panne visible uniquement sur les morceaux.
                    """
                    del body  # volontairement ignore : voir la docstring
                    if r.status_code not in (200, 204):
                        return r
                    await asyncio.sleep(0.4)
                    etat = await client.get(f"{_API_BASE}/me/player", headers=headers)
                    # `etat.content` : Spotify renvoie parfois un 200 à corps vide,
                    # sur lequel `.json()` levait une erreur de décodage non gérée.
                    if (
                        etat.status_code == 200
                        and etat.content
                        and not etat.json().get("is_playing", False)
                    ):
                        relance = await client.put(
                            f"{_API_BASE}/me/player/play", headers=headers, json={}
                        )
                        if relance.status_code in (200, 204):
                            return relance
                    return r

                async def _sans_lecture() -> str:
                    """Réponse quand aucune session de lecture n'existe.

                    Dire « rien ne joue » ne suffit pas : le modèle a besoin de
                    savoir s'il EXISTE un appareil sur lequel lancer quelque
                    chose, sinon il annonce une lecture qui échouera juste après.
                    """
                    devices = await _lister_appareils()
                    if not devices:
                        return (
                            "Rien ne joue, et aucun appareil Spotify n'est disponible. "
                            "Ouvre Spotify sur un appareil — sur l'ordinateur, tu peux le "
                            "faire avec remote_pc (action app_launch, name Spotify) — "
                            "attends quelques secondes, puis réessaie."
                        )
                    liste = ", ".join(_decrire_appareil(d) for d in devices)
                    texte = f"Rien ne joue. Appareils Spotify disponibles : {liste}."
                    if all(_est_onglet(d) for d in devices):
                        texte += (
                            " Seul le lecteur de la page web est disponible : la musique "
                            "y sera coupée par la synthèse vocale. Ouvre Spotify sur "
                            "l'ordinateur ou le téléphone pour une lecture stable."
                        )
                    return texte

                async def _status() -> ToolResult:
                    """État du lecteur via GET /me/player.

                    Cet endpoint est le seul à renvoyer d'un bloc la piste, l'état
                    lecture/pause ET l'appareil actif ; /me/player/currently-playing
                    omet l'appareil, qui est justement l'information manquante.
                    """
                    r = await client.get(f"{_API_BASE}/me/player", headers=headers)
                    if r.status_code == 401:
                        return ToolResult(
                            content=(
                                "Jeton Spotify refusé (401). Réautorise l'accès sur "
                                "/api/spotify/auth."
                            ),
                            is_error=True,
                        )
                    # 204 = aucune session. Un 200 à corps vide s'observe aussi :
                    # `r.json()` lèverait alors une erreur de décodage.
                    if r.status_code == 204 or not r.content:
                        return ToolResult(content=await _sans_lecture())
                    if r.status_code != 200:
                        return ToolResult(
                            content=f"État Spotify indisponible (code {r.status_code}).",
                            is_error=True,
                        )

                    data = r.json()
                    item = data.get("item") or {}
                    etat = "Lecture en cours" if data.get("is_playing") else "En pause"

                    if not item:
                        parties = [
                            f"{etat}, mais Spotify ne renvoie aucune piste "
                            "(publicité, podcast ou session privée)."
                        ]
                    else:
                        artistes = ", ".join(
                            a["name"] for a in (item.get("artists") or []) if a.get("name")
                        )
                        ligne = f"{etat} : « {item.get('name') or '?'} »"
                        if artistes:
                            ligne += f" par {artistes}"
                        duree = item.get("duration_ms")
                        if duree:
                            ligne += f" ({_minutes(data.get('progress_ms'))} / {_minutes(duree)})"
                        parties = [ligne + "."]

                    appareil = data.get("device") or {}
                    if appareil:
                        ligne_app = f"Appareil : {_decrire_appareil(appareil)}"
                        volume = appareil.get("volume_percent")
                        if isinstance(volume, int):
                            ligne_app += f", volume {volume} %"
                        parties.append(ligne_app + ".")
                        if _est_onglet(appareil):
                            parties.append(
                                "C'est le lecteur de la page web : la lecture s'arrête "
                                "si l'onglet est fermé ou rechargé. Ouvrir Spotify sur "
                                "l'ordinateur ou le téléphone la rend indépendante."
                            )

                    return ToolResult(content=" ".join(parties))

                if action == "status":
                    return await _status()

                if action == "toggle":
                    r = await client.get(f"{_API_BASE}/me/player", headers=headers)
                    is_playing = r.status_code == 200 and r.json().get("is_playing", False)
                    if is_playing:
                        r2 = await client.put(f"{_API_BASE}/me/player/pause", headers=headers)
                    else:
                        r2 = await _play()
                    label = "Pause." if is_playing else "Lecture reprise."
                    return ToolResult(
                        content=label
                        if r2.status_code in (200, 204)
                        else f"Erreur Spotify ({r2.status_code})"
                    )

                if action == "play":
                    r = await _play()
                    return ToolResult(
                        content="Lecture reprise."
                        if r.status_code in (200, 204)
                        else f"Erreur Spotify ({r.status_code})"
                    )

                if action == "pause":
                    r = await client.put(f"{_API_BASE}/me/player/pause", headers=headers)
                    return ToolResult(
                        content="Lecture mise en pause."
                        if r.status_code in (200, 204)
                        else f"Erreur Spotify ({r.status_code})"
                    )

                if action == "next":
                    r = await client.post(f"{_API_BASE}/me/player/next", headers=headers)
                    return ToolResult(
                        content="Piste suivante."
                        if r.status_code in (200, 204)
                        else f"Erreur Spotify ({r.status_code})"
                    )

                if action == "previous":
                    r = await client.post(f"{_API_BASE}/me/player/previous", headers=headers)
                    return ToolResult(
                        content="Piste précédente."
                        if r.status_code in (200, 204)
                        else f"Erreur Spotify ({r.status_code})"
                    )

                if action == "search_track":
                    if not query:
                        return ToolResult(
                            content="'query' requis pour search_track.", is_error=True
                        )
                    r = await client.get(
                        f"{_API_BASE}/search",
                        headers=headers,
                        params={"q": query, "type": "track", "limit": 5},
                    )
                    r.raise_for_status()
                    # Spotify peut retourner des items null — on filtre
                    items = [i for i in r.json().get("tracks", {}).get("items", []) if i]
                    if not items:
                        return ToolResult(
                            content=f"Aucun morceau trouvé pour « {query} ».", is_error=True
                        )
                    track = items[0]
                    uri = track["uri"]
                    name = track["name"]
                    artist = ", ".join(a["name"] for a in track.get("artists", []))
                    play_r = await _play({"uris": [uri]})
                    if play_r.status_code in (200, 204):
                        return ToolResult(content=f"Lecture de « {name} » par {artist}.")
                    return ToolResult(
                        content=_echec_lecture(name, play_r.status_code), is_error=True
                    )

                if action == "volume_delta":
                    # 'delta' n'était pas déclaré dans le schéma : le modèle ne
                    # pouvait pas le connaître, et l'envoie désormais parfois sous
                    # forme de chaîne. Un int() nu plantait alors l'outil.
                    try:
                        delta = int(kwargs.get("delta") or 0)
                    except (TypeError, ValueError):
                        return ToolResult(
                            content=(
                                "'delta' doit être un entier de points de pourcentage "
                                "(ex. 10 pour monter, -10 pour baisser)."
                            ),
                            is_error=True,
                        )
                    r = await client.get(f"{_API_BASE}/me/player", headers=headers)
                    if r.status_code != 200 or not r.content:
                        return ToolResult(
                            content="Impossible de récupérer l'état du lecteur.", is_error=True
                        )
                    current = r.json().get("device", {}).get("volume_percent", 50)
                    new_vol = max(0, min(100, current + delta))
                    r2 = await client.put(
                        f"{_API_BASE}/me/player/volume",
                        headers=headers,
                        params={"volume_percent": new_vol},
                    )
                    if r2.status_code in (200, 204):
                        return ToolResult(content=f"Volume : {new_vol}%.")
                    return ToolResult(content=f"Erreur volume ({r2.status_code})", is_error=True)

                if action == "search_playlist":
                    if not query:
                        return ToolResult(
                            content="'query' requis pour search_playlist.", is_error=True
                        )
                    r = await client.get(
                        f"{_API_BASE}/search",
                        headers=headers,
                        params={"q": query, "type": "playlist", "limit": 5},
                    )
                    r.raise_for_status()
                    # Spotify peut retourner des items null — on filtre
                    items = [i for i in r.json().get("playlists", {}).get("items", []) if i]
                    if not items:
                        return ToolResult(
                            content=f"Aucune playlist trouvée pour « {query} ».", is_error=True
                        )
                    playlist = items[0]
                    uri = playlist.get("uri", "")
                    name = playlist.get("name", query)
                    if not uri:
                        return ToolResult(
                            content=f"Playlist « {name} » trouvée mais URI manquant.", is_error=True
                        )
                    play_r = await _play({"context_uri": uri})
                    if play_r.status_code in (200, 204):
                        return ToolResult(content=f"Lecture de la playlist « {name} ».")
                    return ToolResult(
                        content=(
                            f"Playlist trouvée ({name}) mais impossible"
                            f" de lancer ({play_r.status_code})."
                        ),
                        is_error=True,
                    )

                # Un refus doit dire quoi faire ensuite : sans la liste, le modèle
                # rejouait la même action inventée au tour suivant.
                return ToolResult(
                    content=(
                        f"Action inconnue : « {action or '(vide)'} ». "
                        f"Actions valides : {', '.join(_ACTIONS)}. "
                        "Pour connaître l'état du lecteur, utilise 'status'."
                    ),
                    is_error=True,
                )

        except httpx.HTTPStatusError as e:
            # `raise_for_status()` des recherches lève ici. Sans ce filet, un
            # jeton révoqué ou un quota dépassé remontait en exception non gérée
            # et l'outil ne rendait aucun message exploitable.
            code = e.response.status_code
            logger.warning("SpotifyTool HTTP error", action=action, status=code)
            if code == 401:
                return ToolResult(
                    content="Jeton Spotify refusé (401). Réautorise l'accès sur /api/spotify/auth.",
                    is_error=True,
                )
            if code == 429:
                attente = e.response.headers.get("Retry-After", "quelques")
                return ToolResult(
                    content=f"Spotify limite les requêtes. Réessaie dans {attente} secondes.",
                    is_error=True,
                )
            return ToolResult(content=f"Requête Spotify refusée (code {code}).", is_error=True)
        except httpx.TimeoutException:
            logger.warning("SpotifyTool timeout", action=action)
            return ToolResult(content="Timeout Spotify. Réessaie dans un instant.", is_error=True)
        except httpx.RequestError as e:
            logger.error("SpotifyTool request error", error=str(e))
            return ToolResult(content=f"Erreur réseau Spotify : {e}", is_error=True)
