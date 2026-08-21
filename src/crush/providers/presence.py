# Copyright (C) 2026 Maxime Song

"""Savoir si l'utilisateur est joignable — et ne pas prétendre savoir le reste.

CE QUE ÇA MESURE, ET CE QUE ÇA NE MESURE PAS

La tentation était d'appeler ça « présence ». Ç'aurait été faux. Ce que le tailnet
sait dire, c'est qu'un appareil est CONNECTÉ : un téléphone en ligne l'est aussi
bien dans le salon que dans un train à trois cents kilomètres. Trois questions
distinctes, donc, et une seule réponse par question :

- **joignable** — au moins un de ses appareils est sur le tailnet. C'est ce qui
  décide si un message a une chance d'être vu maintenant.
- **au poste** — l'agent PC est connecté. Là on sait vraiment quelque chose : il
  est devant un clavier, et un affichage à l'écran a un sens.
- **à la maison** — personne ne peut le dire ici. Il faudrait Home Assistant ou un
  périmètre géographique. Reste donc à `None` tant que ce n'est pas branché, et
  jamais déduit du reste.

POURQUOI CETTE PRUDENCE

`ecosysteme.py` s'est fait la même règle : ce qui ne peut pas être constaté est
rendu « inconnu » plutôt qu'affiché en vert. Une présence devinée est pire
qu'absente — elle sert à décider s'il faut interrompre quelqu'un, et une
supposition fausse dans ce sens réveille les gens.

CE QUE ÇA CHANGE CONCRÈTEMENT

Le moteur proactif s'en sert pour les heures de silence : une question qui n'a
rien d'urgent n'a pas à sonner à trois heures du matin. Et le contexte des
initiatives sait désormais s'il est à son poste, ce qui distingue « je te
l'affiche » de « je t'envoie ça ».
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from loguru import logger

# Une mesure plus vieille que ça ne vaut rien pour décider d'interrompre
# quelqu'un — mais relancer `tailscale status` à chaque appel coûterait un
# sous-processus par question posée.
_FRAICHEUR_S = 20.0

# Au-delà, on considère que la commande ne répondra pas. Elle est locale : si elle
# met plus de trois secondes, c'est que le démon est en peine, et l'attendre
# bloquerait un cycle proactif entier.
_DELAI_S = 3.0


@dataclass
class EtatPresence:
    """Ce qu'on sait, et ce qu'on ne sait pas.

    `None` n'est pas `False` : « je ne sais pas s'il est joignable » et « il n'est
    pas joignable » mènent à des décisions opposées, et les confondre revient à
    se taire quand on devrait parler.
    """

    joignable: bool | None = None
    au_poste: bool = False
    a_la_maison: bool | None = None
    appareils: list[dict[str, Any]] = field(default_factory=list)
    mesure_le: datetime | None = None
    erreur: str | None = None

    def resume(self) -> str:
        """Une phrase pour le contexte du modèle et pour la page Écosystème."""
        morceaux = []
        if self.joignable is None:
            morceaux.append("joignabilité inconnue")
        else:
            morceaux.append("joignable" if self.joignable else "aucun appareil en ligne")
        if self.au_poste:
            morceaux.append("à son poste")
        if self.a_la_maison is True:
            morceaux.append("à la maison")
        elif self.a_la_maison is False:
            morceaux.append("absent du domicile")
        return ", ".join(morceaux)


class Presence:
    """Agrège les signaux disponibles, et garde le résultat quelques secondes."""

    def __init__(self, registre_agents: object | None = None) -> None:
        # Le registre des agents distants est passé plutôt qu'importé : ce provider
        # doit rester utilisable dans un test sans monter le kernel.
        self._registre = registre_agents
        self._cache: EtatPresence | None = None
        self._mesure_monotone: float | None = None

    async def etat(self, forcer: bool = False) -> EtatPresence:
        maintenant = asyncio.get_running_loop().time()
        if (
            not forcer
            and self._cache is not None
            and self._mesure_monotone is not None
            and maintenant - self._mesure_monotone < _FRAICHEUR_S
        ):
            return self._cache

        appareils, erreur = await self._appareils_du_tailnet()
        etat = EtatPresence(
            joignable=None if erreur else any(a["en_ligne"] for a in appareils),
            au_poste=self._agent_connecte(),
            # Volontairement non renseigné : voir l'en-tête du module.
            a_la_maison=None,
            appareils=appareils,
            mesure_le=datetime.now(),
            erreur=erreur,
        )
        # L'agent PC connecté PROUVE la joignabilité, même si le tailnet n'a pas
        # pu répondre : la connexion passe par lui.
        if etat.au_poste:
            etat.joignable = True

        self._cache = etat
        self._mesure_monotone = maintenant
        return etat

    def _agent_connecte(self) -> bool:
        if self._registre is None:
            return False
        try:
            return bool(self._registre.list_agents())  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 — un registre indisponible n'est pas une absence
            return False

    async def _appareils_du_tailnet(self) -> tuple[list[dict[str, Any]], str | None]:
        """Lit `tailscale status --json`. Erreur rendue, jamais devinée."""
        try:
            processus = await asyncio.create_subprocess_exec(
                "tailscale",
                "status",
                "--json",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except FileNotFoundError:
            return [], "tailscale introuvable sur cette machine"
        except OSError as exc:
            return [], f"tailscale illisible : {exc}"

        try:
            brut, _ = await asyncio.wait_for(processus.communicate(), timeout=_DELAI_S)
        except TimeoutError:
            processus.kill()
            return [], f"tailscale n'a pas répondu en {int(_DELAI_S)} s"

        if processus.returncode != 0:
            return [], f"tailscale a échoué (code {processus.returncode})"

        try:
            donnees = json.loads(brut.decode("utf-8", "replace"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            return [], f"sortie tailscale illisible : {exc}"

        appareils = []
        for pair in (donnees.get("Peer") or {}).values():
            nom = str(pair.get("HostName") or "").strip()
            if not nom:
                continue
            vu = str(pair.get("LastSeen") or "")
            appareils.append(
                {
                    "nom": nom,
                    "os": str(pair.get("OS") or ""),
                    "en_ligne": bool(pair.get("Online")),
                    # « 0001-01-01… » est ce que renvoie Tailscale pour un pair
                    # actuellement en ligne : l'afficher tel quel donnerait
                    # « vu il y a deux mille ans ».
                    "vu_le": "" if vu.startswith("0001-") else vu[:19],
                }
            )
        appareils.sort(key=lambda a: (not a["en_ligne"], a["nom"].lower()))
        logger.debug("Présence mesurée", appareils=len(appareils))
        return appareils, None
