#!/usr/bin/env python3
"""Agent local — à lancer sur l'ordinateur que l'assistant doit piloter.

L'assistant tourne sur un serveur sans écran ni son. Certaines demandes ne
valent que pour VOTRE machine : régler SON volume, lancer SES applications,
l'éteindre. Cet agent comble ce fossé.

Il se connecte AU serveur, jamais l'inverse. Votre PC n'a donc aucun port
ouvert, rien à configurer sur la box, et l'agent fonctionne derrière n'importe
quel routeur. La reconnexion est automatique : on peut le laisser tourner en
permanence et redémarrer le serveur sans y penser.

PREMIÈRE UTILISATION

    python scripts/agent_pc.py --configurer

  demande l'adresse du serveur et le jeton, puis les enregistre dans
  ~/.assistant_agent.json avec des droits restreints.

ENSUITE

    python scripts/agent_pc.py              lance l'agent
    python scripts/agent_pc.py --test       vérifie la connexion puis s'arrête

Dépendance : `websockets`. Tout le reste est optionnel — les capacités
réellement disponibles sont détectées au démarrage et annoncées au serveur,
qui ne proposera que celles-là.
"""

from __future__ import annotations

import sys

# Console Windows : la sortie par défaut est la page de code ANSI (cp1252 en
# France), qui ne sait pas encoder « → » ni « ── ». Sans ce recalage, le
# premier message d'état fait planter l'agent sur UnicodeEncodeError.
# `backslashreplace` garantit qu'aucun affichage ne pourra plus lever.
for _flux in (sys.stdout, sys.stderr):
    try:
        _flux.reconfigure(encoding="utf-8", errors="backslashreplace")  # type: ignore[union-attr]
    except (AttributeError, ValueError, OSError):
        pass

import argparse  # noqa: E402 — le recalage d'encodage doit précéder tout affichage
import asyncio  # noqa: E402
import getpass  # noqa: E402
import json  # noqa: E402
import platform  # noqa: E402
import shutil  # noqa: E402
import socket  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any  # noqa: E402

try:
    import websockets
except ImportError:
    print("Le module « websockets » est requis :  pip install websockets", file=sys.stderr)
    sys.exit(1)

VERSION = "1.0"
CONFIG_PATH = Path.home() / ".assistant_agent.json"
SYSTEME = platform.system().lower()  # 'windows' | 'linux' | 'darwin'


# ══════════════════════════════════════════════════════════════════════════════
# Configuration
# ══════════════════════════════════════════════════════════════════════════════


def charger_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def enregistrer_config(config: dict[str, Any]) -> None:
    CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")
    # Le fichier porte le jeton d'accès à l'assistant : lisible par le seul
    # propriétaire. `chmod` est sans effet sur Windows, où les ACL du profil
    # utilisateur jouent déjà ce rôle.
    try:
        CONFIG_PATH.chmod(0o600)
    except OSError:
        pass


def assistant_configuration() -> dict[str, Any]:
    print("── Configuration de l'agent ──\n")
    # Le defaut vient de la config precedente de CETTE machine, jamais d'un hote
    # code en dur. La ligne suivante lit un jeton d'acces que `url_websocket`
    # place ensuite dans l'URL : un defaut pointant vers une machine qui n'est pas
    # la tienne enverrait une tentative d'authentification a un tiers, sur une
    # simple pression d'Entree. Mieux vaut refuser que deviner.
    precedent = str(charger_config().get("hote") or "")
    if precedent:
        hote = input(f"Adresse du serveur [{precedent}] : ").strip() or precedent
    else:
        hote = input("Adresse du serveur (ex. crush.ton-tailnet.ts.net) : ").strip()
    if not hote:
        raise SystemExit("Aucune adresse de serveur fournie - configuration annulee.")
    # getpass : le jeton ne doit pas rester dans l'historique du terminal.
    jeton = getpass.getpass("Jeton d'accès (invisible) : ").strip()
    nom = input(f"Nom de cette machine [{socket.gethostname()}] : ").strip() or socket.gethostname()

    config = {"hote": hote, "jeton": jeton, "nom": nom}
    enregistrer_config(config)
    print(f"\nEnregistré dans {CONFIG_PATH}")
    return config


def url_websocket(hote: str, jeton: str) -> str:
    """URL du canal agent.

    Le jeton passe en paramètre d'URL : un client WebSocket ne peut pas poser
    d'en-tête `Authorization` au handshake. En `wss://`, l'URL est chiffrée
    dans le tunnel TLS — elle n'est visible que du serveur.
    """
    schema = "ws" if hote.startswith(("localhost", "127.0.0.1")) else "wss"
    hote = hote.replace("https://", "").replace("http://", "").rstrip("/")
    return f"{schema}://{hote}/ws/agent?token={jeton}"


# ══════════════════════════════════════════════════════════════════════════════
# Capacités — chacune se déclare disponible ou non selon la plateforme
# ══════════════════════════════════════════════════════════════════════════════


def _run(cmd: list[str]) -> tuple[bool, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20, check=False)
        sortie = (r.stdout or r.stderr).strip()
        return r.returncode == 0, sortie
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)


# ── Volume ───────────────────────────────────────────────────────────────────


def volume_set(level: float = 0.5, **_: object) -> tuple[bool, str]:
    niveau = max(0.0, min(1.0, float(level)))
    pourcent = int(niveau * 100)
    if SYSTEME == "linux":
        ok, out = _run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{pourcent}%"])
        return ok, f"Volume à {pourcent} %" if ok else out
    if SYSTEME == "darwin":
        ok, out = _run(["osascript", "-e", f"set volume output volume {pourcent}"])
        return ok, f"Volume à {pourcent} %" if ok else out
    if SYSTEME == "windows":
        # Sans dépendance native : on simule les touches multimédia. 50 appuis
        # couvrent la plage complète (2 % par appui), puis on remonte au niveau
        # voulu. Grossier mais sans installation.
        ps = (
            "$o = New-Object -ComObject WScript.Shell; "
            "1..50 | ForEach-Object { $o.SendKeys([char]174) }; "
            f"1..{pourcent // 2} | ForEach-Object {{ $o.SendKeys([char]175) }}"
        )
        ok, out = _run(["powershell", "-NoProfile", "-Command", ps])
        return ok, f"Volume à ~{pourcent} %" if ok else out
    return False, "Plateforme non gérée."


def volume_mute(**_: object) -> tuple[bool, str]:
    if SYSTEME == "linux":
        ok, out = _run(["pactl", "set-sink-mute", "@DEFAULT_SINK@", "toggle"])
        return ok, "Son coupé/rétabli" if ok else out
    if SYSTEME == "darwin":
        ok, out = _run(["osascript", "-e", "set volume with output muted"])
        return ok, "Son coupé" if ok else out
    if SYSTEME == "windows":
        ok, out = _run([
            "powershell", "-NoProfile", "-Command",
            "(New-Object -ComObject WScript.Shell).SendKeys([char]173)",
        ])
        return ok, "Son coupé/rétabli" if ok else out
    return False, "Plateforme non gérée."


# ── Applications ─────────────────────────────────────────────────────────────


def app_launch(name: str = "", **_: object) -> tuple[bool, str]:
    if not name:
        return False, "Nom d'application manquant."
    if SYSTEME == "windows":
        ok, out = _run(["powershell", "-NoProfile", "-Command", f"Start-Process '{name}'"])
        return ok, f"{name} lancé" if ok else out
    if SYSTEME == "darwin":
        ok, out = _run(["open", "-a", name])
        return ok, f"{name} lancé" if ok else out
    chemin = shutil.which(name) or shutil.which(name.lower())
    if not chemin:
        return False, f"{name} introuvable dans le PATH."
    try:
        subprocess.Popen([chemin], start_new_session=True)  # noqa: S603
        return True, f"{name} lancé"
    except OSError as e:
        return False, str(e)


def app_quit(name: str = "", **_: object) -> tuple[bool, str]:
    if not name:
        return False, "Nom d'application manquant."
    if SYSTEME == "windows":
        ok, out = _run(["taskkill", "/IM", f"{name}.exe", "/F"])
        return ok, f"{name} fermé" if ok else out
    ok, out = _run(["pkill", "-f", name])
    return ok, f"{name} fermé" if ok else out


# ── Énergie ──────────────────────────────────────────────────────────────────


def lock(**_: object) -> tuple[bool, str]:
    if SYSTEME == "windows":
        ok, out = _run(["rundll32.exe", "user32.dll,LockWorkStation"])
    elif SYSTEME == "darwin":
        ok, out = _run(["pmset", "displaysleepnow"])
    else:
        ok, out = _run(["loginctl", "lock-session"])
    return ok, "Session verrouillée" if ok else out


def sleep(**_: object) -> tuple[bool, str]:
    if SYSTEME == "windows":
        ok, out = _run(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"])
    elif SYSTEME == "darwin":
        ok, out = _run(["pmset", "sleepnow"])
    else:
        ok, out = _run(["systemctl", "suspend"])
    return ok, "Mise en veille" if ok else out


def shutdown(delay: int = 0, **_: object) -> tuple[bool, str]:
    minutes = max(0, int(delay))
    if SYSTEME == "windows":
        ok, out = _run(["shutdown", "/s", "/t", str(minutes * 60)])
    elif SYSTEME == "darwin":
        ok, out = _run(["osascript", "-e", 'tell app "System Events" to shut down'])
    else:
        ok, out = _run(["shutdown", "-h", f"+{minutes}" if minutes else "now"])
    quand = f"dans {minutes} min" if minutes else "maintenant"
    return ok, f"Extinction {quand}" if ok else out


def cancel_shutdown(**_: object) -> tuple[bool, str]:
    cmd = ["shutdown", "/a"] if SYSTEME == "windows" else ["shutdown", "-c"]
    ok, out = _run(cmd)
    return ok, "Extinction annulée" if ok else out


# ── État ─────────────────────────────────────────────────────────────────────


def status(**_: object) -> tuple[bool, str]:
    infos = [
        f"machine : {socket.gethostname()}",
        f"système : {platform.system()} {platform.release()}",
    ]
    try:
        import psutil  # optionnel

        infos.append(f"processeur : {psutil.cpu_percent(interval=0.3):.0f} %")
        infos.append(f"mémoire : {psutil.virtual_memory().percent:.0f} %")
        batterie = psutil.sensors_battery()
        if batterie:
            branchee = "branché" if batterie.power_plugged else "sur batterie"
            infos.append(f"batterie : {batterie.percent:.0f} % ({branchee})")
    except (ImportError, AttributeError, OSError):
        pass
    return True, " · ".join(infos)


# ── Registre des actions ─────────────────────────────────────────────────────

ACTIONS = {
    "volume_set": volume_set,
    "volume_mute": volume_mute,
    "app_launch": app_launch,
    "app_quit": app_quit,
    "lock": lock,
    "sleep": sleep,
    "shutdown": shutdown,
    "cancel_shutdown": cancel_shutdown,
    "status": status,
}

# Actions irréversibles ou perturbantes : elles ne s'exécutent qu'avec
# `--autoriser-sensibles`. Une injection de prompt dans une page web lue par
# l'assistant ne doit pas pouvoir éteindre l'ordinateur.
SENSIBLES = {"shutdown", "sleep", "app_quit"}


def actions_disponibles(autoriser_sensibles: bool) -> list[str]:
    noms = set(ACTIONS)
    if SYSTEME == "linux" and not shutil.which("pactl"):
        noms -= {"volume_set", "volume_mute"}
    if not autoriser_sensibles:
        noms -= SENSIBLES
    return sorted(noms)


# ══════════════════════════════════════════════════════════════════════════════
# Boucle de connexion
# ══════════════════════════════════════════════════════════════════════════════


async def session(url: str, nom: str, actions: list[str], une_fois: bool = False) -> bool:
    async with websockets.connect(url, max_size=2**20) as ws:
        await ws.send(json.dumps({
            "type": "hello",
            "name": nom,
            "platform": SYSTEME,
            "actions": actions,
            "version": VERSION,
        }))
        accueil = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
        if accueil.get("type") != "welcome":
            print(f"  refusé par le serveur : {accueil}", file=sys.stderr)
            return False

        print(f"  connecté — {len(actions)} action(s) annoncée(s)")
        if une_fois:
            return True

        async for brut in ws:
            message = json.loads(brut)
            if message.get("type") != "action":
                continue
            nom_action = message.get("action", "")
            fonction = ACTIONS.get(nom_action)
            if fonction is None or nom_action not in actions:
                reponse = {"ok": False, "error": f"Action « {nom_action} » indisponible."}
            else:
                params = message.get("params") or {}
                try:
                    # Les actions sont bloquantes (subprocess) : on les sort de
                    # la boucle asyncio pour ne pas geler la connexion, sinon
                    # une extinction différée bloquerait tout le reste.
                    ok, detail = await asyncio.to_thread(fonction, **params)
                    reponse = {"ok": ok, "detail" if ok else "error": detail}
                except Exception as e:  # noqa: BLE001 — on renvoie l'échec, on ne meurt pas
                    reponse = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                print(f"  {nom_action} → {'ok' if reponse.get('ok') else 'échec'}")

            await ws.send(json.dumps({"type": "result", "id": message.get("id"), **reponse}))
        return True


async def boucle(config: dict[str, Any], autoriser_sensibles: bool, une_fois: bool) -> int:
    url = url_websocket(config["hote"], config["jeton"])
    nom = config.get("nom") or socket.gethostname()
    actions = actions_disponibles(autoriser_sensibles)

    affichage = url.split("?")[0]  # jamais le jeton dans la console
    print(f"Agent « {nom} » → {affichage}")
    if autoriser_sensibles:
        print("  actions sensibles AUTORISÉES (extinction, veille, fermeture)")

    attente = 2
    while True:
        try:
            ok = await session(url, nom, actions, une_fois)
            if une_fois:
                return 0 if ok else 1
            attente = 2
        except KeyboardInterrupt:
            return 0
        except Exception as e:  # noqa: BLE001 — toute panne réseau doit être réessayée
            print(f"  déconnecté ({type(e).__name__}: {e})", file=sys.stderr)
            if une_fois:
                return 1
        # Réessai à intervalle croissant, plafonné à une minute : le serveur
        # peut redémarrer, l'agent doit revenir seul sans marteler.
        await asyncio.sleep(attente)
        attente = min(attente * 2, 60)


def main() -> int:
    parseur = argparse.ArgumentParser(description="Agent local de l'assistant")
    parseur.add_argument("--configurer", action="store_true", help="assistant de configuration")
    parseur.add_argument("--test", action="store_true", help="teste la connexion et s'arrête")
    parseur.add_argument(
        "--autoriser-sensibles",
        action="store_true",
        help="autorise extinction, veille et fermeture d'applications",
    )
    args = parseur.parse_args()

    config = assistant_configuration() if args.configurer else charger_config()
    if not config.get("hote") or not config.get("jeton"):
        print("Agent non configuré. Lancer :  python scripts/agent_pc.py --configurer")
        return 1

    try:
        return asyncio.run(boucle(config, args.autoriser_sensibles, args.test))
    except KeyboardInterrupt:
        print("\nArrêt.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
