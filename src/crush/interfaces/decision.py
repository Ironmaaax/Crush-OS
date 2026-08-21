# Copyright (C) 2026 Maxime Song

"""Décider d'une initiative — un seul chemin, quelle que soit la porte d'entrée.

POURQUOI CE MODULE EXISTE

Le moteur proactif produit des initiatives `VALIDATE` : celles qui demandent une
DÉCISION. Elles étaient poussées vers Telegram, mais on ne pouvait y répondre que
depuis le Command Center — la boucle était donc à moitié ouverte : la question
arrivait sur le téléphone, la réponse exigeait un ordinateur.

Ouvrir une seconde porte imposait un choix : dupliquer l'exécution, ou l'extraire.
La dupliquer aurait été la pire option — approuver un `DRAFT_RESPONSE` ENVOIE un
e-mail, et deux implémentations qui divergent, c'est un e-mail parti deux fois ou
pas du tout selon la porte utilisée. Tout passe donc ici.

DEUX FOIS N'EST PAS DEUX FOIS

`traiter()` refuse une initiative qui n'est plus `pending`. Ce n'est pas une
précaution théorique : un bouton Telegram reste tapotable après coup, la
notification reste dans le fil, et on tape volontiers deux fois quand rien ne
semble se passer. Sans ce garde-fou, le deuxième appui renvoie l'e-mail.

L'EMPLACEMENT

`interfaces/` et non `engine/` : l'exécution a besoin de `capabilities.tools.gmail`
et de l'orchestrateur, or la RÈGLE 3 interdit à l'engine d'importer providers et
capabilities. Ni dans `api/` : un canal de messagerie qui importe le module des
routes HTTP pour décider d'une initiative dit une dépendance qui n'existe pas.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from crush.capabilities.tools.gmail import send_gmail_draft
from crush.engine.proactive.schemas import InitiativeType
from crush.engine.proactive.store import InitiativeStore
from crush.kernel.settings import settings


@dataclass
class ResultatDecision:
    """Ce que la décision a réellement produit.

    `deja_traitee` est distinct de `appliquee` à dessein : « c'était déjà fait »
    n'est ni un succès ni une erreur, et le dire évite de laisser croire qu'on
    vient d'agir une seconde fois.
    """

    trouvee: bool = False
    deja_traitee: bool = False
    appliquee: bool = False
    approuvee: bool = False
    titre: str = ""
    statut_precedent: str = ""
    # Ce qui s'est passé, en une phrase lisible par un humain — le canal Telegram
    # l'affiche telle quelle sous le message d'origine.
    detail: str = ""
    erreur: str | None = None


async def traiter(
    initiative_id: str,
    approuvee: bool,
    orchestrator: object | None = None,
    store: InitiativeStore | None = None,
) -> ResultatDecision:
    """Applique une décision et dit ce qu'elle a produit.

    `orchestrator` est passé plutôt que retrouvé : ce module ne doit rien savoir
    de l'endroit où il tourne — une requête HTTP le tient de `app.state`, le canal
    Telegram de sa propre référence.
    """
    magasin = store or InitiativeStore()
    init = magasin.get_by_id(initiative_id)
    if init is None:
        return ResultatDecision(detail="Cette initiative n'existe plus.")

    statut = str(getattr(init, "status", "") or "")
    resultat = ResultatDecision(
        trouvee=True, approuvee=approuvee, titre=init.title, statut_precedent=statut
    )

    if statut and statut != "pending":
        resultat.deja_traitee = True
        resultat.detail = f"Déjà traitée ({statut})."
        return resultat

    if not approuvee:
        magasin.update_status(initiative_id, "rejected")
        resultat.appliquee = True
        resultat.detail = "Écartée."
        return resultat

    # Le statut est posé AVANT l'exécution. Dans l'autre ordre, un envoi qui met
    # quatre secondes laisse une fenêtre où l'initiative est encore `pending` :
    # un second appui pendant ce temps relancerait l'action.
    magasin.update_status(initiative_id, "approved")
    resultat.appliquee = True

    try:
        if init.type == InitiativeType.DRAFT_RESPONSE:
            message_id = await send_gmail_draft(
                draft_content=init.draft_content or "",
                credentials_path=Path(settings.google_credentials_path),
                token_path=Path(settings.google_token_path).parent / "google_gmail_token.json",
            )
            resultat.detail = "E-mail envoyé."
            logger.info("Initiative : e-mail envoyé", initiative=initiative_id, message=message_id)

        elif init.type == InitiativeType.AUTO_TASK:
            if orchestrator is None:
                resultat.detail = "Approuvée, mais l'orchestrateur n'est pas disponible."
                resultat.erreur = "orchestrateur indisponible"
            else:
                mission = init.mission_description or init.action
                asyncio.create_task(  # noqa: RUF006 — fire-and-forget assumé, cf. docstring
                    orchestrator.create_and_run(mission),  # type: ignore[attr-defined]
                    name=f"initiative-{initiative_id[:8]}",
                )
                resultat.detail = "Mission lancée."
                logger.info("Initiative : mission lancée", initiative=initiative_id)

        else:
            resultat.detail = "Notée comme approuvée."
            logger.info("Initiative approuvée", initiative=initiative_id, type=str(init.type))

    except Exception as exc:  # noqa: BLE001 — l'échec est rapporté, pas propagé
        # Le statut reste `approved` : la décision A été prise, c'est son exécution
        # qui a échoué. Le remettre à `pending` ferait repousser la question comme
        # si l'on n'avait rien répondu.
        resultat.erreur = str(exc)
        resultat.detail = f"Approuvée, mais l'action a échoué : {exc}"
        logger.error("Initiative : action échouée", initiative=initiative_id, erreur=str(exc))

    return resultat
