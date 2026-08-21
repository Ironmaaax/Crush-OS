# Copyright (C) 2026 Maxime Song
# This file is part of CRUSH-OS,   .


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

import os
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request

from crush.interfaces.channels.push import dernier_envoi
from crush.kernel import quota_cartes
from crush.kernel.paths import MEMORY_DATA_DIR, SAUVEGARDES_DIR
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
                "Échanger un premier message avec l'assistant : la base se crée seule.",
                "memoire",
            )
        ]
    taille = base.stat().st_size / 1024**2
    return [
        _maillon("Mémoire", _OK, f"{taille:.1f} Mo", "", "memoire"),
        _sauvegarde(),
        _copie_hors_machine(),
        _boite_obsidian(),
    ]


def _boite_obsidian() -> dict:
    """Le chemin de retour : ce qu'on écrit à la main vers la mémoire.

    Ce maillon dit si le fichier EXISTE et s'il attend quelque chose. Il ne
    prétend pas dire si la synchronisation vers le téléphone fonctionne — rien
    tournant sur cette machine ne peut le savoir, et un voyant vert qui
    signifierait seulement « le fichier est là » induirait en erreur.
    """
    from crush.providers.memory.boite_reception import NOM_FICHIER

    if not settings.obsidian_inbox_enabled:
        return _maillon(
            "Boîte de réception",
            _ABSENT,
            "désactivée — aucune correction écrite à la main n'est relue",
            "Mettre OBSIDIAN_INBOX_ENABLED=true dans .env, puis redémarrer.",
            "memoire",
        )

    chemin = MEMORY_DATA_DIR / "mirror" / NOM_FICHIER
    if not chemin.exists():
        return _maillon(
            "Boîte de réception",
            _DEGRADE,
            "fichier absent alors que la relecture est active",
            "Redémarrer le service : le fichier est créé au démarrage.",
            "memoire",
        )

    # Une consigne encore en attente n'est pas une anomalie tant que la passe
    # n'a pas tourné — au pire dix minutes. On l'affiche sans alarmer.
    en_attente = _consignes_en_attente(chemin)
    if en_attente:
        return _maillon(
            "Boîte de réception",
            _OK,
            f"{en_attente} consigne(s) en attente de la prochaine passe",
            "",
            "memoire",
        )
    return _maillon("Boîte de réception", _OK, "à jour, rien en attente", "", "memoire")


def _consignes_en_attente(chemin: Path) -> int:
    """Compte les lignes non traitées, sans importer la logique de lecture.

    Volontairement approximatif : ce compteur alimente un voyant, pas une
    décision. La lecture qui fait foi est celle de `boite_reception._lire`.
    """
    try:
        contenu = chemin.read_text(encoding="utf-8")
    except OSError:
        return 0
    total = 0
    for brute in contenu.split("## Traité")[0].splitlines():
        ligne = brute.strip()
        if not ligne or ligne.startswith(("#", ">", "<!--", "_", "---", "```", "|")):
            continue
        total += 1
    return total


# Au-delà, la passe quotidienne ne tourne manifestement plus. 36 h laisse passer
# une machine éteinte une nuit sans crier au loup.
_AGE_ALERTE_H = 36


def _age_h(quand: datetime) -> float:
    return (datetime.now() - quand).total_seconds() / 3600


def _age_lisible(heures: float) -> str:
    if heures < 1:
        return "il y a moins d'une heure"
    if heures < 48:
        return f"il y a {int(heures)} h"
    return f"il y a {int(heures / 24)} jours"


def _archives(dossier: Path) -> list[Path]:
    if not dossier.is_dir():
        return []
    return sorted(dossier.glob("memoire-*.tar.gz"))


def _sauvegarde() -> dict:
    """L'ÂGE de la dernière archive, pas sa simple existence.

    Une archive de trois mois donnait un voyant vert et une fausse assurance :
    c'est exactement le cas où l'on croit être couvert sans l'être.
    """
    archives = _archives(SAUVEGARDES_DIR)
    if not archives:
        return _maillon(
            "Sauvegarde",
            _ABSENT,
            "aucune archive — la mémoire n'existe qu'en un seul exemplaire",
            "Vérifier BACKUP_ENABLED, ou lancer « python scripts/sauvegarde_memoire.py ».",
            "memoire",
        )
    age = (datetime.now().timestamp() - archives[-1].stat().st_mtime) / 3600
    if age > _AGE_ALERTE_H:
        return _maillon(
            "Sauvegarde",
            _DEGRADE,
            f"dernière {_age_lisible(age)} — la passe quotidienne ne tourne plus",
            "Vérifier BACKUP_ENABLED et les journaux du service.",
            "memoire",
        )
    return _maillon(
        "Sauvegarde",
        _OK,
        f"dernière {_age_lisible(age)}, {len(archives)} archive(s) conservée(s)",
        "",
        "memoire",
    )


def _copie_hors_machine() -> dict:
    """Une archive sur le support qu'elle protège ne protège pas de sa panne.

    C'est le maillon qui manquait : on pouvait avoir sept archives fraîches et
    tout perdre avec la carte SD.
    """
    cible = settings.backup_copy_to.strip()
    if not cible:
        return _maillon(
            "Copie hors machine",
            _DEGRADE,
            "aucune — les archives vivent sur le support qu'elles protègent",
            "Renseigner BACKUP_COPY_TO (partage réseau, clé USB, dossier synchronisé).",
            "memoire",
        )
    distantes = _archives(Path(cible).expanduser())
    if not distantes:
        return _maillon(
            "Copie hors machine",
            _DEGRADE,
            f"{cible} configuré mais vide — la copie échoue ou le support est absent",
            "Vérifier que la destination est montée et inscriptible.",
            "memoire",
        )
    age = (datetime.now().timestamp() - distantes[-1].stat().st_mtime) / 3600
    return _maillon(
        "Copie hors machine",
        _OK if age <= _AGE_ALERTE_H else _DEGRADE,
        f"{cible} — dernière {_age_lisible(age)}",
        "" if age <= _AGE_ALERTE_H else "La copie ne se fait plus ; vérifier la destination.",
        "memoire",
    )


def _ou_il_est(requete: object | None = None) -> dict:
    """Ce qu'on sait de sa joignabilite. Ni vert menteur, ni rouge injustifie.

    Le maillon reste `ok` meme quand aucun appareil n'est en ligne : etre absent
    du tailnet n'est pas une panne de l'assistant. Ce qui EST un probleme, c'est
    de ne pas pouvoir mesurer -- la ou une decision d'interrompre quelqu'un se
    prendrait alors a l'aveugle.
    """
    presence = getattr(getattr(requete, "app", None), "state", None)
    presence = getattr(presence, "presence", None) if presence is not None else None
    if presence is None:
        return _maillon(
            "Ou il est",
            _ABSENT,
            "mesure indisponible",
            "Redemarrer le service : la mesure est construite au demarrage.",
            "autonomie",
        )
    etat = getattr(presence, "_cache", None)
    if etat is None:
        return _maillon(
            "Ou il est",
            _OK,
            "pas encore mesure (au prochain cycle proactif)",
            "",
            "autonomie",
        )
    if etat.erreur:
        return _maillon(
            "Ou il est",
            _DEGRADE,
            f"mesure impossible : {etat.erreur}",
            "Verifier que `tailscale` repond : « tailscale status --json ».",
            "autonomie",
        )
    en_ligne = sum(1 for a in etat.appareils if a["en_ligne"])
    return _maillon(
        "Ou il est",
        _OK,
        f"{etat.resume()} — {en_ligne}/{len(etat.appareils)} appareil(s) en ligne",
        "",
        "autonomie",
    )


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
        _initiatives_poussees(),
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


def _initiatives_poussees() -> dict:
    """Une initiative qui demande une décision peut-elle t'atteindre ?

    Le moteur produisait des décisions à prendre diffusées aux seuls clients
    WebSocket connectés : elles dormaient dans le Command Center jusqu'à ce qu'on
    pense à l'ouvrir. Ce maillon existe pour que l'absence de canal se voie.

    Les canaux se règlent par variable d'environnement et non par `Settings` —
    `interfaces/channels/setup.py` les lit ainsi. On reste cohérent avec la
    source de vérité plutôt qu'avec le style du fichier.
    """
    if not settings.push_proactive_enabled:
        return _maillon(
            "Initiatives poussées",
            _DEGRADE,
            "désactivé — une décision attendue patiente jusqu'à ta prochaine visite",
            "Mettre PUSH_PROACTIVE_ENABLED=true.",
            "autonomie",
        )
    canal = any(
        os.getenv(v, "false").lower() == "true"
        for v in ("TELEGRAM_ENABLED", "MESSAGING_GATEWAY_ENABLED")
    )
    if not canal:
        return _maillon(
            "Initiatives poussées",
            _DEGRADE,
            "aucun canal — les initiatives attendent que tu ouvres l'interface",
            "Activer TELEGRAM_ENABLED et renseigner son owner.",
            "autonomie",
        )
    # La configuration ne prouve rien. Un canal activé, avec un token valide et le
    # bon identifiant, reste incapable d'écrire tant que l'utilisateur n'a pas
    # parlé au bot le premier : Telegram refuse par « chat not found ». D'où la
    # lecture du dernier envoi RÉEL — un voyant vert pendant que tout échoue est
    # exactement ce que cette page dit vouloir éviter.
    dernier = dernier_envoi()
    seuil = settings.push_notify_priority_min
    if dernier is not None and not dernier.reussi:
        return _maillon(
            "Initiatives poussées",
            _DEGRADE,
            f"le dernier envoi a échoué {_age_lisible(_age_h(dernier.horodatage))} "
            f"— {dernier.erreur or 'raison inconnue'}",
            "Écrire une première fois au bot : il ne peut pas engager la conversation.",
            "autonomie",
        )
    if dernier is None:
        return _maillon(
            "Initiatives poussées",
            _OK,
            f"configuré, aucun envoi encore effectué (seuil « {seuil} »)",
            "",
            "autonomie",
        )
    return _maillon(
        "Initiatives poussées",
        _OK,
        f"dernier envoi réussi {_age_lisible(_age_h(dernier.horodatage))}, "
        f"notifications à partir de « {seuil} »",
        "",
        "autonomie",
    )


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
        _cerveau()
        + _voix()
        + _memoire()
        + _autonomie()
        + [_ou_il_est(request)]
        + _garde_fous()
        + _integrations()
    )
    return {
        "maillons": maillons,
        "resume": {
            "ok": sum(1 for m in maillons if m["etat"] == _OK),
            "degrade": sum(1 for m in maillons if m["etat"] == _DEGRADE),
            "absent": sum(1 for m in maillons if m["etat"] == _ABSENT),
        },
    }
