# Copyright (C) 2026 Maxime Song

"""« Dans quel état est mon assistant, là, maintenant ? » — en un seul écran.

CE QUE LES AUTRES PAGES NE FONT PAS

L'Écosystème est une liste de contrôle : maillon par maillon, son état et son
remède. On l'ouvre quand quelque chose cloche. Le Cerveau montre la FORME de
l'assistant, pas son état. Le Pilotage montre ce qui demande une décision.

Aucune ne répond à la question qu'on se pose en s'asseyant : est-ce que tout
tourne, qu'est-ce qui attend, combien ça coûte, et quand la prochaine passe. Ces
réponses existaient, éparpillées sur cinq pages et six appels réseau.

UN SEUL APPEL

Tout est agrégé ici plutôt que composé côté navigateur. Six requêtes depuis un
téléphone en 4G, c'est six aller-retours et un écran qui se remplit par morceaux
pendant deux secondes — or c'est précisément la page qu'on ouvre pour un coup
d'œil rapide.

AUCUNE MESURE INVENTÉE

Même règle que l'Écosystème : ce qui ne peut pas être constaté est rendu `null`,
jamais approché. Un aperçu qui invente une valeur plausible est pire qu'un champ
vide, parce qu'on cesse de vérifier ailleurs.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Request

from crush.interfaces.api.ecosysteme import canal_actif, ecosysteme
from crush.kernel.paths import MEMORY_DATA_DIR, SAUVEGARDES_DIR
from crush.kernel.remote_agents import registry
from crush.kernel.settings import settings

router = APIRouter()

_CANAUX = ("telegram", "discord", "signal", "slack", "whatsapp")


def _age_heures(horodatage: float) -> float:
    return (datetime.now().timestamp() - horodatage) / 3600


def _lisible(heures: float | None) -> str:
    if heures is None:
        return "inconnu"
    if heures < 1:
        return "il y a moins d'une heure"
    if heures < 48:
        return f"il y a {int(heures)} h"
    return f"il y a {int(heures / 24)} jours"


def _cerveau() -> dict[str, Any]:
    backend = settings.api_backend
    modele = {
        "gemini": settings.gemini_model,
        "anthropic": settings.anthropic_model,
        "openai": settings.openai_model,
        "mistral": settings.mistral_model,
    }.get(backend, backend)
    return {
        "backend": backend,
        "modele": modele,
        "reflexion": bool(settings.reflection_enabled),
        "voix": settings.tts_provider,
    }


def _memoire(request: Request) -> dict[str, Any]:
    base = MEMORY_DATA_DIR / "crush_memory.db"
    kernel = getattr(request.app.state, "memory_kernel", None)

    faits = None
    if kernel is not None:
        try:
            from crush.providers.memory.schemas import FactStatus

            faits = kernel.count_facts(FactStatus.ACTIVE)
        except Exception:  # noqa: BLE001 — un compteur absent vaut mieux qu'un faux
            faits = None

    archives = sorted(SAUVEGARDES_DIR.glob("memoire-*.tar.gz")) if SAUVEGARDES_DIR.is_dir() else []
    age = _age_heures(archives[-1].stat().st_mtime) if archives else None

    return {
        "faits": faits,
        "octets": base.stat().st_size if base.exists() else None,
        "archives": len(archives),
        "sauvegarde_age_h": round(age, 1) if age is not None else None,
        "sauvegarde_lisible": _lisible(age),
        # Plus de 36 h : la passe quotidienne ne tourne manifestement plus. Le
        # seuil laisse passer une nuit d'extinction sans crier au loup.
        "sauvegarde_inquiete": age is not None and age > 36,
    }


def _cout(request: Request) -> dict[str, Any]:
    """Le coût réel, ou `null`. Jamais une estimation présentée comme un relevé."""
    tracker = getattr(request.app.state, "tracker", None)
    if tracker is None:
        return {"aujourd_hui": None, "mois": None, "serie": [], "devise": "EUR"}
    try:
        jours = tracker.get_daily_totals(days=7)
        mois = tracker.get_monthly_totals()
    except Exception:  # noqa: BLE001
        return {"aujourd_hui": None, "mois": None, "serie": [], "devise": "EUR"}

    # `cost_usd` est la cle reelle du tracker, et la devise est le DOLLAR. La
    # premiere version essayait une liste de cles plausibles et retombait sur
    # 0.0 : elle affichait donc « 0,00 € » -- un montant faux, dans la mauvaise
    # monnaie, presente comme un releve. Une cle absente rend `None`.
    def _valeur(entree: object) -> float | None:
        if isinstance(entree, dict) and isinstance(entree.get("cost_usd"), (int, float)):
            return float(entree["cost_usd"])
        return None

    valeurs = [_valeur(j) for j in (jours or [])]
    serie = [round(v, 4) for v in valeurs if v is not None]
    total_mois = _valeur(mois)

    # Rien n'a jamais ete suivi : « 0 » laisserait croire a une depense nulle
    # alors que la mesure n'a simplement pas commence.
    suivi_depuis = (mois or {}).get("tracked_since") if isinstance(mois, dict) else None
    if suivi_depuis is None:
        return {"aujourd_hui": None, "mois": None, "serie": [], "devise": "USD"}

    return {
        "aujourd_hui": serie[-1] if serie else None,
        "mois": round(total_mois, 2) if total_mois is not None else None,
        "serie": serie,
        "devise": "USD",
        "suivi_depuis": suivi_depuis,
    }


def _initiatives(request: Request) -> dict[str, Any]:
    store = getattr(request.app.state, "initiative_store", None)
    if store is None:
        try:
            from crush.engine.proactive.store import InitiativeStore

            store = InitiativeStore()
        except Exception:  # noqa: BLE001
            return {"en_attente": None, "haute": 0, "liste": []}
    try:
        en_attente = store.load_pending_all(days=7)
    except Exception:  # noqa: BLE001
        return {"en_attente": None, "haute": 0, "liste": []}

    def _priorite(i: object) -> str:
        return str(getattr(getattr(i, "priority", ""), "value", getattr(i, "priority", "")))

    return {
        "en_attente": len(en_attente),
        "haute": sum(1 for i in en_attente if _priorite(i) == "high"),
        "liste": [
            {
                "id": getattr(i, "id", ""),
                "titre": getattr(i, "title", ""),
                "priorite": _priorite(i),
                # Le POURQUOI, en une ligne. Trancher sans lui obligeait a
                # ouvrir le Command Center pour chaque item -- et c'est
                # precisement cette corvee qui a laisse la file monter a 25.
                "pourquoi": str(getattr(i, "reasoning", "") or getattr(i, "context", ""))[:160],
                "type": str(getattr(getattr(i, "type", ""), "value", getattr(i, "type", ""))),
                "decision": str(
                    getattr(getattr(i, "execution_mode", ""), "value", "")
                ).lower()
                == "validate",
            }
            for i in en_attente[:12]
        ],
    }


def _relie() -> list[dict[str, Any]]:
    """Ce qui est effectivement branché, et ce qui dort. La distinction compte :
    un canal éteint volontairement n'est pas une panne."""
    liens: list[dict[str, Any]] = []
    for canal in _CANAUX:
        liens.append(
            {
                "nom": canal.capitalize(),
                "actif": canal_actif(canal),
                "famille": "canal",
            }
        )
    for agent in registry.list_agents():
        liens.append(
            {
                "nom": agent.name,
                "actif": True,
                "famille": "machine",
                "detail": f"{agent.platform} · {len(agent.actions)} action(s)",
            }
        )
    liens.append(
        {
            "nom": "Coffre Obsidian",
            "actif": (MEMORY_DATA_DIR / "mirror" / "boite-de-reception.md").exists(),
            "famille": "memoire",
        }
    )
    return liens


@router.get("/api/apercu")
async def lire_l_apercu(request: Request) -> dict[str, Any]:
    """Tout ce qu'il faut pour un coup d'œil, en une requête."""
    presence = getattr(request.app.state, "presence", None)
    etat_presence: dict[str, Any] = {"resume": None, "joignable": None, "au_poste": False}
    if presence is not None:
        try:
            e = await presence.etat()
            etat_presence = {
                "resume": e.resume(),
                "joignable": e.joignable,
                "au_poste": e.au_poste,
                "erreur": e.erreur,
            }
        except Exception:  # noqa: BLE001 — l'aperçu ne tombe pas pour un champ
            pass

    scheduler = getattr(request.app.state, "scheduler", None)
    boucles: list[dict[str, Any]] = []
    if scheduler is not None:
        try:
            boucles = [
                {
                    "nom": b.get("name", ""),
                    "quand": b.get("next_run"),
                    "cadence": b.get("interval", ""),
                    "detail": b.get("description", ""),
                }
                for b in scheduler.status()
            ]
        except Exception:  # noqa: BLE001
            boucles = []

    # On réutilise l'Écosystème plutôt que de recompter : deux comptages qui
    # divergent enverraient chercher une panne sur la mauvaise page.
    resume_eco: dict[str, Any] = {}
    try:
        vue = await ecosysteme(request)
        resume_eco = dict(vue.get("resume") or {})
        resume_eco["a_regarder"] = [
            {"nom": m["nom"], "etat": m["etat"], "detail": m["detail"], "remede": m["remede"]}
            for m in vue.get("maillons", [])
            if m["etat"] != "ok"
        ][:5]
    except Exception:  # noqa: BLE001
        resume_eco = {}

    return {
        "horodatage": datetime.now().isoformat(timespec="seconds"),
        "assistant": settings.assistant_name or "Crush",
        "cerveau": _cerveau(),
        "memoire": _memoire(request),
        "cout": _cout(request),
        "initiatives": _initiatives(request),
        "presence": etat_presence,
        "relie": _relie(),
        "boucles": boucles,
        "ecosysteme": resume_eco,
        "silence": {
            "plage": settings.push_heures_silence,
            "urgent_passe": bool(settings.push_silence_laisse_passer_urgent),
        },
    }
