# Copyright (C) 2026 Maxime Song
# This file is part of CRUSH-OS,   .
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""État de la chaîne qui fait fonctionner l'assistant.

POURQUOI CETTE ROUTE EXISTE
===========================

La question « dans quel état est mon assistant ? » n'avait aucune réponse.
Il fallait ouvrir une session SSH, lire des journaux, appeler des outils un par
un. Sur une machine sans écran, consultée depuis un téléphone, c'est
inaccessible — et c'est précisément quand quelque chose cloche qu'on ne peut
pas se le permettre.

Chaque maillon rend trois choses : son état, ce qu'il fait, et **ce qu'il faut
faire s'il ne va pas**. Un état rouge sans remède ne vaut guère mieux qu'un
silence.

Aucune mesure n'est inventée : ce qui ne peut pas être constaté est rendu
`inconnu` plutôt qu'affiché en vert par défaut. Un tableau de bord qui ment est
pire qu'une page vide, parce qu'on cesse de vérifier.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request

from crush.kernel import quota_cartes
from crush.kernel.paths import MEMORY_DATA_DIR
from crush.kernel.remote_agents import registry
from crush.kernel.settings import settings

router = APIRouter()

# Vocabulaire d'état, volontairement à trois valeurs. « dégradé » existe parce
# que la plupart des pannes réelles n'en sont pas : une capacité indisponible
# faute de configuration n'est pas une panne, et la confondre avec une vraie
# avarie fait chercher un bug là où il n'y en a pas.
_OK = "ok"
_DEGRADE = "degrade"
_ABSENT = "absent"


def _maillon(
    nom: str,
    etat: str,
    detail: str,
    remede: str = "",
    famille: str = "",
) -> dict[str, Any]:
    return {
        "nom": nom,
        "etat": etat,
        "detail": detail,
        "remede": remede,
        "famille": famille,
    }


def _cerveau() -> list[dict]:
    """Le modèle qui répond, et celui qui réfléchit."""
    backend = settings.api_backend
    modele = {
        "gemini": settings.gemini_model,
        "anthropic": settings.anthropic_model,
        "openai": settings.openai_model,
        "mistral": settings.mistral_model,
    }.get(backend, backend)

    cle_posee = bool(settings.google_api_key.get_secret_value()) if backend == "gemini" else True
    maillons = [
        _maillon(
            "Modèle de langage",
            _OK if cle_posee else _ABSENT,
            f"{backend} · {modele}",
            "" if cle_posee else "Renseigner GOOGLE_API_KEY dans .env.",
            "cerveau",
        )
    ]

    if settings.reflection_enabled and backend == "gemini":
        maillons.append(
            _maillon(
                "Réflexion sélective",
                _OK,
                f"active à l'oral, budget {settings.reflection_thinking_budget} jetons",
                "",
                "cerveau",
            )
        )
    else:
        maillons.append(
            _maillon(
                "Réflexion sélective",
                _DEGRADE,
                "inactive — les questions de fond reçoivent la réponse rapide",
                "Mettre REFLECTION_ENABLED=true (backend Gemini requis).",
                "cerveau",
            )
        )
    return maillons


def _voix() -> list[dict]:
    """Entendre et parler : les deux moitiés du canal principal."""
    stt_pret = bool(settings.openai_api_key.get_secret_value())
    return [
        _maillon(
            "Transcription",
            _OK if stt_pret else _DEGRADE,
            f"{settings.stt_provider} · {settings.openai_stt_model}"
            if stt_pret
            else "aucune clé OpenAI — repli sur le modèle local, plus lent",
            "" if stt_pret else "Renseigner OPENAI_API_KEY, ou installer l'extra local-audio.",
            "voix",
        ),
        _maillon(
            "Synthèse",
            _OK,
            f"{settings.tts_provider}"
            + (f" · {settings.edge_voice}" if settings.tts_provider == "edge" else ""),
            "",
            "voix",
        ),
    ]


def _memoire() -> list[dict]:
    """Ce que l'assistant retient, et ce que ça pèse."""
    base = MEMORY_DATA_DIR / "crush_memory.db"
    if not base.exists():
        return [
            _maillon(
                "Mémoire",
                _ABSENT,
                "aucune base — elle se crée au premier échange",
                "",
                "memoire",
            )
        ]
    taille = base.stat().st_size / 1024**2
    sauvegardes = sorted((MEMORY_DATA_DIR.parent / "sauvegardes").glob("memoire-*.tar.gz"))
    return [
        _maillon("Mémoire", _OK, f"{taille:.1f} Mo", "", "memoire"),
        _maillon(
            "Sauvegarde",
            _OK if sauvegardes else _DEGRADE,
            f"dernière : {sauvegardes[-1].name.removeprefix('memoire-').removesuffix('.tar.gz')}"
            if sauvegardes
            else "aucune archive — la mémoire n'existe qu'en un seul exemplaire",
            ""
            if sauvegardes
            else "Lancer le script « sauvegarde_memoire », puis recopier l'archive ailleurs.",
            "memoire",
        ),
    ]


def _autonomie() -> list[dict]:
    """Ce que l'assistant fait sans qu'on le lui demande."""
    docker = settings.docker_enabled
    return [
        _maillon(
            "Moteur proactif",
            _OK,
            f"un cycle toutes les {settings.proactive_interval_minutes} minutes",
            "",
            "autonomie",
        ),
        _maillon(
            "Banc d'essai des compétences",
            _OK if docker else _DEGRADE,
            "conteneur isolé"
            if docker
            else "sans conteneur : les compétences ne sont pas testées, donc pas installables",
            "" if docker else "Installer Docker et mettre DOCKER_ENABLED=true.",
            "autonomie",
        ),
        _maillon(
            "Postes pilotables",
            _OK if registry.list_agents() else _ABSENT,
            ", ".join(a.name for a in registry.list_agents()) or "aucun poste connecté",
            ""
            if registry.list_agents()
            else "Lancer scripts/agent_pc.py sur la machine à piloter.",
            "autonomie",
        ),
    ]


def _garde_fous() -> list[dict]:
    """Ce qui empêche l'assistant de nuire, ou de coûter trop cher."""
    plafond = settings.budget_monthly_usd
    return [
        _maillon(
            "Plafond de dépense",
            _OK if settings.budget_enabled else _DEGRADE,
            f"{plafond:.2f} $ par mois, alerte à {settings.budget_warn_pct:.0f} %"
            if settings.budget_enabled
            else "aucune alerte — la dépense n'est pas surveillée",
            "" if settings.budget_enabled else "Mettre BUDGET_ENABLED=true dans .env.",
            "garde-fous",
        ),
        _quota_cartes(),
        _maillon(
            "Exécution hors conteneur",
            _OK if not settings.skill_sandbox_allow_host_exec else _DEGRADE,
            "refusée — le code généré ne tourne que dans un conteneur"
            if not settings.skill_sandbox_allow_host_exec
            else "AUTORISÉE : le code écrit par le modèle tourne avec les droits du service",
            ""
            if not settings.skill_sandbox_allow_host_exec
            else "Remettre SKILL_SANDBOX_ALLOW_HOST_EXEC=false.",
            "garde-fous",
        ),
    ]


def _quota_cartes() -> dict:
    """Consommation Mapbox du mois, avant qu'elle ne devienne une facture.

    Le compteur est une estimation — il compte les remises de jeton, pas les
    cartes facturées par Mapbox — et c'est dit ici plutôt que laissé croire à
    une mesure exacte.
    """
    plafond = settings.mapbox_monthly_limit
    if not settings.mapbox_token.get_secret_value():
        return _maillon(
            "Cartes Mapbox",
            _ABSENT,
            "aucun jeton — la vue globe est inactive",
            "Ajouter MAPBOX_TOKEN dans .env, ou passer la vue sur MapLibre + OSM.",
            "garde-fous",
        )
    q = quota_cartes.etat(plafond)
    if plafond <= 0:
        return _maillon(
            "Cartes Mapbox",
            _DEGRADE,
            f"{q.chargements} chargements ce mois-ci, AUCUN plafond",
            "Mapbox facture au-delà de 50 000 par mois : fixer MAPBOX_MONTHLY_LIMIT.",
            "garde-fous",
        )
    part = q.chargements / plafond
    return _maillon(
        "Cartes Mapbox",
        _DEGRADE if part >= 0.8 else _OK,
        f"{q.chargements} / {plafond} chargements ce mois-ci (estimation)",
        "Proche du plafond : la carte se coupera d'elle-même avant la facturation."
        if part >= 0.8
        else "",
        "garde-fous",
    )


def _integrations() -> list[dict]:
    """Les services extérieurs, et ce qui manque pour chacun."""
    def _jeton(chemin: str) -> bool:
        return Path(chemin).exists()

    items = [
        ("Google Calendar", _jeton(settings.google_token_path), "/api/google/auth/calendar"),
        (
            "Gmail",
            _jeton(settings.google_credentials_path.replace("credentials", "gmail_token")),
            "/api/google/auth/gmail",
        ),
        ("Spotify", _jeton(settings.spotify_token_path), "la page Intégrations"),
        ("Notion", bool(settings.notion_token.get_secret_value()), "NOTION_TOKEN dans .env"),
    ]
    return [
        _maillon(
            nom,
            _OK if pret else _ABSENT,
            "autorisé" if pret else "non autorisé",
            "" if pret else f"Passer par {ou}.",
            "integrations",
        )
        for nom, pret, ou in items
    ]


@router.get("/api/ecosysteme")
async def ecosysteme(request: Request) -> dict:
    """Vue d'ensemble : chaque maillon, son état, et son remède."""
    maillons = (
        _cerveau() + _voix() + _memoire() + _autonomie() + _garde_fous() + _integrations()
    )
    return {
        "maillons": maillons,
        "resume": {
            "ok": sum(1 for m in maillons if m["etat"] == _OK),
            "degrade": sum(1 for m in maillons if m["etat"] == _DEGRADE),
            "absent": sum(1 for m in maillons if m["etat"] == _ABSENT),
        },
    }
