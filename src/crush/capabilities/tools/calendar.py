# Copyright (C) 2026 Max Ea
# Copyright (C) 2026 Maxime Song — modifications de ce fork
# This file is part of CRUSH-OS,   .
 

"""Google Calendar — lecture de l'agenda et création d'événements.

Ce module héberge aussi le **socle OAuth Google** partagé avec `gmail.py`
(description d'un service, chargement/rafraîchissement du jeton, traduction
des erreurs de l'API en instructions). Les deux outils avaient chacun leur
copie de cette logique, avec des ordres d'arguments inversés et des messages
d'erreur différents pour la même panne.

À terme, ce socle mérite son module `capabilities/tools/google_auth.py`
(cf. `spotify_auth.py`) ; il vit ici en attendant, et `gmail.py` l'importe.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
from loguru import logger

from crush.capabilities.tools.base import Tool, ToolResult
from crush.kernel.settings import settings

_CAL_BASE = "https://www.googleapis.com/calendar/v3"

# Fuseau par défaut des événements créés et des horaires renvoyés. Le forcer
# côté requête garantit des dates avec décalage explicite (+02:00) plutôt que
# des « Z » : le rappel calendrier de l'engine reconnaît ce format-là.
_TZ_DEFAUT = "Europe/Paris"

# Un nom IANA, pas une abréviation ni un décalage. On ne valide que la forme :
# zoneinfo dépend d'une base tzdata qui n'est pas garantie sur toutes les
# plateformes, et Google reste de toute façon l'autorité sur la liste réelle.
_TZ_VALIDE = re.compile(r"^[A-Za-z][A-Za-z0-9+_-]*(?:/[A-Za-z0-9+_.-]+)*$")
_DATE_SEULE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

try:
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    GOOGLE_DISPONIBLE = True
except ImportError:  # pragma: no cover - dépendance du cœur, absente seulement si install cassée
    GOOGLE_DISPONIBLE = False


# ── Socle OAuth Google (partagé calendar + gmail) ─────────────────────────────


@dataclass(frozen=True)
class ServiceGoogle:
    """Ce qui distingue Calendar de Gmail dans le parcours d'autorisation."""

    cle: str  # segment d'URL du flux web : /api/google/auth/<cle>
    libelle: str
    scopes: tuple[str, ...]
    api: str  # identifiant de l'API à activer dans la console Google

    @property
    def url_autorisation(self) -> str:
        return f"/api/google/auth/{self.cle}"

    @property
    def url_activation_api(self) -> str:
        return f"https://console.cloud.google.com/apis/library/{self.api}"


SERVICE_CALENDAR = ServiceGoogle(
    cle="calendar",
    libelle="Google Calendar",
    scopes=("https://www.googleapis.com/auth/calendar",),
    api="calendar-json.googleapis.com",
)


class ErreurGoogle(RuntimeError):
    """Panne d'authentification Google dont le message porte le remède.

    `code` sert aux tests et aux appelants qui veulent distinguer les cas sans
    inspecter le texte ; le texte, lui, s'adresse à l'utilisateur final.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _client_oauth_configure() -> bool:
    """Un client OAuth est-il déclaré en .env (l'UI sait alors régénérer le JSON) ?"""
    return bool(settings.google_client_id and settings.google_client_secret.get_secret_value())


def _remede_autorisation(service: ServiceGoogle, credentials_path: Path) -> str:
    """Explique comment obtenir un jeton, selon ce qui manque en amont."""
    if credentials_path.exists() or _client_oauth_configure():
        return (
            f"Remède : ouvre le parcours d'autorisation « {service.url_autorisation} » "
            f"(interface web → page Capacités → Intégrations → {service.libelle} → Connecter), "
            "puis valide l'accès avec ton compte Google. Le jeton est écrit tout seul au retour."
        )
    return (
        f"Aucun client OAuth n'est configuré : ni le fichier {credentials_path}, "
        "ni les réglages GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET.\n"
        "Remède, dans l'ordre :\n"
        "  1. console.cloud.google.com → APIs & Services → Credentials → Create OAuth client ID, "
        "type « Web application », URI de redirection "
        f"https://<ton-crush>/api/google/callback/{service.cle} ;\n"
        f"  2. active l'API du service : {service.url_activation_api} ;\n"
        "  3. renseigne GOOGLE_CLIENT_ID et GOOGLE_CLIENT_SECRET dans l'interface "
        f"(Capacités → Intégrations), ou dépose le JSON téléchargé dans {credentials_path} ;\n"
        f"  4. ouvre « {service.url_autorisation} » pour autoriser le compte."
    )


def _ecrire_jeton(token_path: Path, creds: Credentials) -> None:
    """Persiste le jeton rafraîchi ; sans ça le refresh recommencerait à chaque appel."""
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json())
    try:
        token_path.chmod(0o600)  # un refresh_token vaut un mot de passe
    except OSError:  # pragma: no cover - systèmes de fichiers sans permissions POSIX
        pass


def charger_credentials_google(
    service: ServiceGoogle,
    credentials_path: Path,
    token_path: Path,
) -> Credentials:
    """Retourne des credentials utilisables, ou lève une ErreurGoogle qui dit quoi faire.

    Fonction bloquante (I/O disque + éventuel refresh HTTP) : à appeler via
    `asyncio.to_thread`.

    Aucun flux interactif ici. L'ancienne version tombait sur
    `InstalledAppFlow.run_local_server()` dès que le jeton manquait : sur un
    serveur sans écran, ça ouvre un navigateur inexistant et bloque le thread
    jusqu'au timeout de l'appelant. L'autorisation passe exclusivement par le
    parcours web `/api/google/auth/<service>`.
    """
    if not GOOGLE_DISPONIBLE:
        raise ErreurGoogle(
            "lib_absente",
            "Bibliothèque google-auth absente du venv.\n"
            "Remède : `uv sync` (google-auth-oauthlib fait partie des dépendances du cœur).",
        )

    if not token_path.exists():
        raise ErreurGoogle(
            "autorisation_absente",
            f"{service.libelle} n'est pas autorisé : aucun jeton dans {token_path}.\n"
            + _remede_autorisation(service, credentials_path),
        )

    try:
        donnees = json.loads(token_path.read_text())
    except (OSError, ValueError) as exc:
        raise ErreurGoogle(
            "jeton_illisible",
            f"Jeton {service.libelle} illisible ({token_path}) : {exc}.\n"
            f"Remède : supprime ce fichier puis refais « {service.url_autorisation} ».",
        ) from exc

    try:
        creds = Credentials.from_authorized_user_info(donnees)
    except ValueError as exc:
        # google-auth exige refresh_token + client_id + client_secret : un jeton
        # sans refresh_token vient d'un consentement sans access_type=offline.
        raise ErreurGoogle(
            "jeton_incomplet",
            f"Jeton {service.libelle} incomplet ({token_path}) : {exc}\n"
            f"Remède : refais « {service.url_autorisation} » — le parcours demande "
            "access_type=offline et réécrit un jeton rafraîchissable.",
        ) from exc

    accordes = set(creds.scopes or ())
    manquants = [s for s in service.scopes if s not in accordes]
    if accordes and manquants:
        raise ErreurGoogle(
            "scopes_insuffisants",
            f"Jeton {service.libelle} autorisé sans les droits nécessaires. "
            f"Manque : {', '.join(manquants)}. Accordés : {', '.join(sorted(accordes))}.\n"
            f"Remède : refais « {service.url_autorisation} » et accepte toutes les cases "
            "de l'écran de consentement (le parcours force prompt=consent).",
        )

    if creds.valid:
        return creds

    if not creds.refresh_token:
        raise ErreurGoogle(
            "jeton_expire",
            f"Jeton {service.libelle} expiré et non rafraîchissable "
            f"(pas de refresh_token dans {token_path}).\n"
            f"Remède : refais « {service.url_autorisation} ».",
        )

    try:
        creds.refresh(Request())
    except RefreshError as exc:
        raise ErreurGoogle(
            "refresh_refuse",
            f"Google refuse de rafraîchir le jeton {service.libelle} : {exc}\n"
            "Causes habituelles : accès révoqué depuis myaccount.google.com, mot de passe "
            "changé, ou application OAuth restée en mode « Testing » — dans ce mode Google "
            "invalide le refresh_token au bout de 7 jours.\n"
            f"Remède : refais « {service.url_autorisation} » ; pour que ça tienne, passe "
            "l'application en « In production » dans console.cloud.google.com → "
            "APIs & Services → OAuth consent screen.",
        ) from exc
    except Exception as exc:
        raise ErreurGoogle(
            "reseau",
            f"Impossible de joindre oauth2.googleapis.com pour rafraîchir le jeton "
            f"{service.libelle} ({type(exc).__name__}: {exc}).\n"
            "Remède : vérifie la connectivité réseau et le DNS de la machine.",
        ) from exc

    _ecrire_jeton(token_path, creds)
    logger.info("Jeton Google rafraîchi", service=service.cle)
    return creds


async def obtenir_jeton_google(
    service: ServiceGoogle,
    credentials_path: Path,
    token_path: Path,
) -> Credentials:
    """Version async : le chargement bloquant part en thread, borné dans le temps.

    Sans borne, un refresh sur un réseau qui ne répond pas retiendrait l'agent
    120 s (timeout par défaut de google-auth) sans rien dire.
    """
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(charger_credentials_google, service, credentials_path, token_path),
            timeout=30.0,
        )
    except TimeoutError as exc:
        raise ErreurGoogle(
            "reseau",
            f"Vérification des accès {service.libelle} sans réponse au bout de 30 s "
            "(rafraîchissement du jeton bloqué).\n"
            "Remède : vérifie la connectivité réseau de la machine.",
        ) from exc


def _detail_erreur_google(reponse: httpx.Response) -> tuple[str, str, str]:
    """Extrait (raison, message, url d'aide) du corps d'erreur JSON de Google."""
    try:
        erreur = reponse.json().get("error", {})
    except ValueError:
        return "", reponse.text[:200], ""
    if not isinstance(erreur, dict):
        return "", str(erreur)[:200], ""

    message = str(erreur.get("message", ""))
    raison = ""
    aide = ""

    for item in erreur.get("errors", []) or []:
        if isinstance(item, dict):
            raison = raison or str(item.get("reason", ""))
            aide = aide or str(item.get("extendedHelp", ""))

    # Format « google.rpc » : la vraie raison (SERVICE_DISABLED) et le lien
    # d'activation exact du projet vivent dans details[].
    for detail in erreur.get("details", []) or []:
        if not isinstance(detail, dict):
            continue
        raison = raison or str(detail.get("reason", ""))
        for lien in detail.get("links", []) or []:
            if isinstance(lien, dict) and lien.get("url"):
                aide = aide or str(lien["url"])

    return raison, message, aide


def expliquer_erreur_api(service: ServiceGoogle, exc: Exception) -> str:
    """Traduit une exception d'appel HTTP en message qui dit quoi faire."""
    if isinstance(exc, httpx.HTTPStatusError):
        return _expliquer_statut(service, exc.response)
    if isinstance(exc, httpx.TimeoutException):
        return (
            f"{service.libelle} : pas de réponse de l'API dans le délai imparti.\n"
            "Remède : réessaie ; si ça persiste, vérifie la connectivité de la machine."
        )
    if isinstance(exc, httpx.RequestError):
        return (
            f"{service.libelle} injoignable ({type(exc).__name__}: {exc}).\n"
            "Remède : vérifie le réseau et le DNS de la machine."
        )
    return f"Erreur {service.libelle} ({type(exc).__name__}) : {exc}"


def _expliquer_statut(service: ServiceGoogle, reponse: httpx.Response) -> str:
    code = reponse.status_code
    raison, message, aide = _detail_erreur_google(reponse)
    minuscule = f"{raison} {message}".lower()

    if code == 401:
        return (
            f"{service.libelle} : Google a rejeté le jeton (401 {message or 'unauthorized'}).\n"
            f"Remède : refais « {service.url_autorisation} » pour réautoriser le compte."
        )

    api_desactivee = raison in {"accessNotConfigured", "SERVICE_DISABLED"}
    if code == 403 and (api_desactivee or "disabled" in minuscule):
        return (
            f"L'API {service.api} n'est pas activée sur le projet Google Cloud "
            f"({message or 'accessNotConfigured'}).\n"
            f"Remède : ouvre {aide or service.url_activation_api}, clique « Enable », "
            "puis attends une minute avant de réessayer."
        )

    if code == 403 and (
        raison in {"insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}
        or "insufficient authentication scopes" in minuscule
    ):
        return (
            f"{service.libelle} : le jeton n'a pas les droits demandés "
            f"({', '.join(service.scopes)}).\n"
            f"Remède : refais « {service.url_autorisation} » et accepte toutes les cases "
            "de l'écran de consentement."
        )

    if code == 429 or "ratelimit" in minuscule or "quota" in minuscule:
        return (
            f"{service.libelle} : quota Google dépassé ({message or code}).\n"
            "Remède : attends quelques minutes ; si c'est régulier, relève le quota dans "
            "console.cloud.google.com → APIs & Services → Quotas."
        )

    if code == 404:
        return (
            f"{service.libelle} : ressource introuvable (404 {message}).\n"
            "Remède : vérifie que le compte autorisé est bien celui qui possède la ressource."
        )

    if code == 400:
        detail = message or reponse.text[:200]
        return f"{service.libelle} : requête refusée par Google (400) — {detail}"

    if code >= 500:
        return (
            f"{service.libelle} : panne côté Google ({code} {message}).\n"
            "Remède : réessaie dans quelques minutes."
        )

    return f"{service.libelle} : erreur HTTP {code} — {message or reponse.text[:200]}"


def borner_entier(valeur: object, defaut: int, mini: int, maxi: int) -> int:
    """Coerce et borne un entier : les modèles envoient volontiers « 7 » en chaîne.

    Publique parce que partagée : `gmail.py` l'utilise pour borner max_results.
    Laissée privée, elle avait été importée sous ce nom depuis gmail.py, ce qui
    rendait tout le paquet inimportable — donc l'application entière muette.
    """
    try:
        nombre = int(str(valeur).strip())
    except (TypeError, ValueError):
        return defaut
    return max(mini, min(maxi, nombre))


# ── Outils Calendar ───────────────────────────────────────────────────────────


class CalendarListTool(Tool):
    """Liste les prochains événements Google Calendar."""

    name = "list_calendar_events"
    description = (
        "Liste les prochains événements du Google Calendar de l'utilisateur. "
        "Utilise cet outil quand l'utilisateur demande son agenda, son planning ou ses rendez-vous."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "days_ahead": {
                "type": "integer",
                "description": "Fenêtre en jours à partir de maintenant (défaut : 7, max : 365)",
            },
        },
        "required": [],
    }

    def __init__(self, credentials_path: Path, token_path: Path) -> None:
        self._creds = credentials_path
        self._token = token_path

    async def execute(self, days_ahead: object = 7, **_: object) -> ToolResult:
        jours = borner_entier(days_ahead, defaut=7, mini=1, maxi=365)

        try:
            creds = await obtenir_jeton_google(SERVICE_CALENDAR, self._creds, self._token)
        except ErreurGoogle as e:
            logger.warning("Calendar indisponible", code=e.code)
            return ToolResult(content=str(e), is_error=True)

        maintenant = datetime.now(UTC)
        params = {
            "timeMin": maintenant.isoformat(),
            # Sans timeMax, « les 7 prochains jours » renvoyait en réalité les 35
            # prochains événements, quelle que soit leur date : l'outil mentait.
            "timeMax": (maintenant + timedelta(days=jours)).isoformat(),
            "maxResults": min(250, max(10, jours * 5)),
            "singleEvents": "true",
            "orderBy": "startTime",
            "timeZone": _TZ_DEFAUT,
        }

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.get(
                    f"{_CAL_BASE}/calendars/primary/events",
                    headers={"Authorization": f"Bearer {creds.token}"},
                    params=params,
                )
                resp.raise_for_status()

            events = resp.json().get("items", [])
            if not events:
                return ToolResult(content=f"Aucun événement dans les {jours} prochains jours.")

            # Format « - <ISO> : <titre> » : le rappel calendrier de l'engine
            # (engine/background/scheduler.py) le parse par expression régulière.
            lines = []
            for e in events:
                start = e.get("start", {})
                debut = start.get("dateTime", start.get("date", "?"))
                lines.append(f"- {debut} : {e.get('summary', '(sans titre)')}")

            logger.debug("Calendar events listed", count=len(lines))
            return ToolResult(content="\n".join(lines))

        except Exception as e:
            logger.error(f"Calendar list error: {type(e).__name__}: {e}")
            return ToolResult(content=expliquer_erreur_api(SERVICE_CALENDAR, e), is_error=True)


class CalendarCreateTool(Tool):
    """Crée un événement dans Google Calendar."""

    name = "create_calendar_event"
    description = (
        "Crée un nouvel événement dans le Google Calendar de l'utilisateur. "
        "Utilise cet outil quand l'utilisateur veut ajouter un rendez-vous ou un rappel. "
        "Les dates sont en ISO 8601 ; une date seule (2026-08-19) crée un événement "
        "sur la journée entière."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Titre de l'événement"},
            "start": {
                "type": "string",
                "description": (
                    "Début ISO 8601 : « 2026-08-19T14:00:00 » (heure locale), "
                    "« 2026-08-19T14:00:00+02:00 » (décalage explicite) "
                    "ou « 2026-08-19 » (journée entière)"
                ),
            },
            "end": {
                "type": "string",
                "description": "Fin, même format que start, et strictement après start",
            },
            "description": {"type": "string", "description": "Description optionnelle"},
            "timezone": {
                "type": "string",
                "description": (
                    f"Fuseau IANA appliqué aux heures sans décalage (défaut : {_TZ_DEFAUT})"
                ),
            },
        },
        "required": ["title", "start", "end"],
    }

    def __init__(self, credentials_path: Path, token_path: Path) -> None:
        self._creds = credentials_path
        self._token = token_path

    async def execute(
        self,
        title: str = "",
        start: str = "",
        end: str = "",
        description: str = "",
        timezone: str = _TZ_DEFAUT,
        **_: object,
    ) -> ToolResult:
        try:
            debut, fin = _bornes_evenement(start, end)
            titre = _titre_valide(title)
            fuseau = _fuseau_valide(timezone)
        except ValueError as e:
            # Rejeté avant tout appel réseau : le modèle peut corriger et rappeler.
            return ToolResult(content=str(e), is_error=True)

        try:
            creds = await obtenir_jeton_google(SERVICE_CALENDAR, self._creds, self._token)
        except ErreurGoogle as e:
            logger.warning("Calendar indisponible", code=e.code)
            return ToolResult(content=str(e), is_error=True)

        body = {
            "summary": titre,
            "description": description,
            "start": _borne_google(debut, fuseau),
            "end": _borne_google(fin, fuseau),
        }

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(
                    f"{_CAL_BASE}/calendars/primary/events",
                    headers={
                        "Authorization": f"Bearer {creds.token}",
                        "Content-Type": "application/json",
                    },
                    json=body,
                )
                resp.raise_for_status()

            created = resp.json()
            logger.info("Calendar event created", title=titre, event_id=created.get("id"))
            return ToolResult(content=f"Événement créé : {created.get('htmlLink', titre)}")

        except Exception as e:
            logger.error(f"Calendar create error: {type(e).__name__}: {e}")
            return ToolResult(content=expliquer_erreur_api(SERVICE_CALENDAR, e), is_error=True)


# ── Validation des entrées de create_calendar_event ───────────────────────────


def _titre_valide(title: str) -> str:
    titre = (title or "").strip()
    if not titre:
        raise ValueError("`title` est vide : un événement doit avoir un titre.")
    return titre


def _fuseau_valide(timezone: str) -> str:
    fuseau = (timezone or "").strip() or _TZ_DEFAUT
    if not _TZ_VALIDE.match(fuseau):
        raise ValueError(
            f"`timezone` invalide : « {fuseau} ». Attendu un nom IANA "
            f"(« Europe/Paris », « UTC »), pas un décalage ni une abréviation."
        )
    return fuseau


def _analyser_instant(valeur: str, champ: str) -> date | datetime:
    """Parse une borne ISO 8601. Une date nue signifie « journée entière »."""
    brut = (valeur or "").strip()
    if not brut:
        raise ValueError(
            f"`{champ}` est vide. Attendu de l'ISO 8601 : « 2026-08-19T14:00:00 » "
            "pour une heure, « 2026-08-19 » pour une journée entière."
        )
    if _DATE_SEULE.match(brut):
        return date.fromisoformat(brut)
    try:
        return datetime.fromisoformat(brut)
    except ValueError as exc:
        raise ValueError(
            f"`{champ}` n'est pas une date ISO 8601 : « {brut} » ({exc}). "
            "Attendu « 2026-08-19T14:00:00 », « 2026-08-19T14:00:00+02:00 » "
            "ou « 2026-08-19 ». Les formulations en langage naturel "
            "(« demain 14h ») doivent être converties avant l'appel."
        ) from exc


def _bornes_evenement(start: str, end: str) -> tuple[date | datetime, date | datetime]:
    """Valide le couple début/fin et renvoie les bornes normalisées."""
    debut = _analyser_instant(start, "start")
    fin = _analyser_instant(end, "end")

    journee_entiere = isinstance(debut, date) and not isinstance(debut, datetime)
    fin_journee_entiere = isinstance(fin, date) and not isinstance(fin, datetime)
    if journee_entiere != fin_journee_entiere:
        raise ValueError(
            "`start` et `end` doivent être du même type : deux dates nues pour une "
            "journée entière, ou deux horodatages complets."
        )

    if journee_entiere:
        # Google traite end.date comme exclusif : un événement d'une journée se
        # note [jour, jour+1). Le modèle envoie naturellement start == end.
        if fin <= debut:
            fin = debut + timedelta(days=1)
        return debut, fin

    if (debut.tzinfo is None) != (fin.tzinfo is None):
        raise ValueError(
            "`start` et `end` doivent porter tous les deux un décalage horaire, ou aucun "
            "des deux — sinon la durée de l'événement est ambiguë."
        )
    if fin <= debut:
        raise ValueError(
            f"`end` ({fin.isoformat()}) doit être strictement après `start` ({debut.isoformat()})."
        )
    return debut, fin


def _borne_google(borne: date | datetime, fuseau: str) -> dict:
    """Encode une borne au format attendu par l'API Calendar."""
    if isinstance(borne, datetime):
        if borne.tzinfo is not None:
            # Le décalage porte déjà l'instant ; ajouter timeZone n'apporterait
            # qu'une contradiction possible.
            return {"dateTime": borne.isoformat()}
        return {"dateTime": borne.isoformat(), "timeZone": fuseau}
    return {"date": borne.isoformat()}
