"""Identité réseau de la machine hôte — noms et adresses par lesquels on l'atteint.

Sert au contrôle d'`Origin` (cf. `engine/auth.py`) : une requête navigateur est
acceptée si son `Origin` désigne CETTE machine. Sans ce contrôle, une page
malveillante ouverte dans le même navigateur peut ouvrir un WebSocket vers
l'assistant et hériter du cookie de session — c'est le détournement de
WebSocket inter-site (CSWSH), que `SameSite` seul ne couvre pas partout.

Trois sources, unies :
  - les adresses IP des interfaces locales (LAN) ;
  - le nom MagicDNS et les IP Tailscale, si `tailscale` est dans le PATH ;
  - les hôtes déclarés à la main dans `API_ALLOWED_ORIGINS`.

Le calcul est fait UNE fois et mis en cache : ces valeurs ne bougent pas en
cours d'exécution, et interroger Tailscale à chaque requête coûterait un
sous-process par appel.

Ne dépend que de la stdlib — L0, aucun import hors `crush.kernel`.
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
from functools import lru_cache
from urllib.parse import urlsplit

from loguru import logger

_TAILSCALE_TIMEOUT = 5.0


def _local_addresses() -> set[str]:
    """Noms et IP des interfaces de cette machine."""
    hosts: set[str] = {"localhost", "127.0.0.1", "::1", "[::1]"}
    try:
        hostname = socket.gethostname()
        hosts.add(hostname.lower())
        # `.local` : nom mDNS/Bonjour, courant sur un Pi (`raspberrypi.local`).
        hosts.add(f"{hostname.lower()}.local")
        for info in socket.getaddrinfo(hostname, None):
            addr = info[4][0]
            if addr:
                hosts.add(str(addr).lower())
    except OSError as exc:
        logger.debug("Résolution du nom d'hôte impossible : {}", exc)
    return hosts


def _tailscale_hosts() -> set[str]:
    """Nom MagicDNS et IP Tailscale de cette machine, si Tailscale est installé.

    Silencieux si Tailscale est absent : c'est le cas nominal en développement.
    """
    hosts: set[str] = set()
    if not shutil.which("tailscale"):
        return hosts
    try:
        res = subprocess.run(  # noqa: S603 — binaire résolu par shutil.which, args fixes
            ["tailscale", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=_TAILSCALE_TIMEOUT,
            check=False,
        )
        if res.returncode != 0:
            logger.debug("`tailscale status` a échoué (code {})", res.returncode)
            return hosts
        self_node = json.loads(res.stdout).get("Self") or {}
        dns_name = (self_node.get("DNSName") or "").rstrip(".")
        if dns_name:
            hosts.add(dns_name.lower())
            hosts.add(dns_name.split(".")[0].lower())  # nom court
        for ip in self_node.get("TailscaleIPs") or []:
            hosts.add(str(ip).lower())
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        logger.debug("Détection Tailscale impossible : {}", exc)
    return hosts


@lru_cache(maxsize=1)
def allowed_hosts() -> frozenset[str]:
    """Hôtes par lesquels cette machine est légitimement joignable.

    Mis en cache pour la durée du process. `allowed_hosts.cache_clear()` force
    un recalcul (utile en test, ou après un `tailscale up`).
    """
    from crush.kernel.settings import settings

    extra = {
        h.strip().lower()
        for h in settings.api_allowed_origins.split(",")
        if h.strip()
    }
    hosts = _local_addresses() | _tailscale_hosts() | extra
    logger.debug("Hôtes autorisés : {}", sorted(hosts))
    return frozenset(hosts)


def origin_allowed(origin: str | None) -> bool:
    """True si l'en-tête `Origin` désigne cette machine.

    `None` est accepté : les clients non-navigateur (agent PC, scripts, curl)
    n'envoient pas d'`Origin`. Ils restent protégés par le jeton — l'`Origin`
    ne défend que contre les requêtes déclenchées par une AUTRE page web.
    """
    if origin is None:
        return True
    if origin == "null":
        # `Origin: null` — sandbox iframe, fichier local. Jamais légitime ici.
        return False
    try:
        host = urlsplit(origin).hostname
    except ValueError:
        return False
    if not host:
        return False
    return host.lower() in allowed_hosts()
