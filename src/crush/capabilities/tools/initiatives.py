# Copyright (C) 2026 Maxime Song
# This file is part of CRUSH-OS,   .
 

"""Arbitrage des suggestions proactives, à la voix comme à l'écrit.

POURQUOI CET OUTIL EXISTE
=========================

Le moteur proactif tourne toutes les trois heures sur une machine allumée en
permanence. Une initiative en mode `validate` était diffusée **une seule fois**
par WebSocket, à qui se trouvait connecté à cet instant — c'est-à-dire presque
jamais. Elle restait ensuite visible sur `/dashboard`, une page que l'interface
mobile ne mentionne nulle part.

Constat en production : quinze suggestions attendaient un arbitrage que rien ne
permettait de rendre depuis le canal réellement utilisé, la voix. Chacune avait
coûté un appel LLM pour être produite, et aucune n'avait servi.

Le prompt annonce désormais leur nombre ; cet outil permet de les entendre et
d'y répondre.
"""

from __future__ import annotations

from typing import Any

from crush.capabilities.tools.base import Tool, ToolResult
from crush.kernel.contracts import InitiativeStore

# Au-delà, la réponse cesse d'être écoutable à voix haute. L'assistant en
# propose une à la fois ; le nombre total est rappelé pour que l'utilisateur
# sache qu'il en reste.
_MAX_LISTEES = 6

_ORDRE_PRIORITE = {"high": 0, "medium": 1, "low": 2}


def _valeur(objet: Any, champ: str, defaut: str = "") -> str:  # noqa: ANN401
    """Lit un champ, que l'initiative soit un objet ou un dictionnaire."""
    brut = getattr(objet, champ, None)
    if brut is None and isinstance(objet, dict):
        brut = objet.get(champ)
    if brut is None:
        return defaut
    # Les priorités et types sont des StrEnum : str() rend leur valeur.
    return str(getattr(brut, "value", brut))


class InitiativesTool(Tool):
    """Liste, accepte ou écarte les suggestions proactives en attente."""

    name = "initiatives"
    description = (
        "Consulter et arbitrer les suggestions que l'assistant a préparées de "
        "lui-même et qui attendent une décision. action='list' les énumère, "
        "action='approve' en accepte une, action='reject' l'écarte. Les deux "
        "dernières exigent l'identifiant rendu par 'list'. À utiliser quand "
        "l'utilisateur demande ses suggestions, ou répond à une que tu viens "
        "de lui proposer."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "approve", "reject"],
                "description": "Ce qu'il faut faire des suggestions en attente.",
            },
            "initiative_id": {
                "type": "string",
                "description": (
                    "Quelle suggestion trancher : son identifiant rendu par "
                    "action='list', OU quelques mots de son titre. À l'oral "
                    "l'utilisateur dit « celle sur le deep work », pas un "
                    "identifiant — les deux formes sont acceptées. Obligatoire "
                    "pour 'approve' et 'reject'."
                ),
            },
        },
        "required": ["action"],
    }

    def __init__(self, store: InitiativeStore) -> None:
        self._store = store

    async def execute(  # type: ignore[override]
        self,
        action: str,
        initiative_id: str = "",
    ) -> ToolResult:
        if action == "list":
            return self._lister()
        if action in {"approve", "reject"}:
            return self._arbitrer(action, initiative_id)
        return ToolResult(
            content=f"Action inconnue : « {action} ». Valides : list, approve, reject.",
            is_error=True,
        )

    # ── Lecture ──────────────────────────────────────────────────────────────

    def _en_attente(self) -> list[Any]:
        """Suggestions en attente, les plus urgentes d'abord."""
        try:
            brutes = list(self._store.load_pending_all(days=7))
        except Exception as exc:  # noqa: BLE001 — une lecture ratée ne casse pas la conv
            raise _LectureImpossible(str(exc)) from exc
        # Seul le mode `validate` attend une decision. Les `notify` ont deja ete
        # glissees en fin de reponse a l'utilisateur, et les `auto` ne font rien
        # — mais aucune des deux ne voit son statut repasser de « pending », si
        # bien que le magasin en annoncait 36 la ou 15 attendaient vraiment.
        # Les compter aurait fait proposer a l'utilisateur d'arbitrer des choses
        # qu'il a deja recues.
        a_arbitrer = [i for i in brutes if _valeur(i, "execution_mode").lower() == "validate"]
        return sorted(
            a_arbitrer,
            key=lambda i: _ORDRE_PRIORITE.get(_valeur(i, "priority", "low").lower(), 3),
        )

    def _lister(self) -> ToolResult:
        try:
            attente = self._en_attente()
        except _LectureImpossible as exc:
            return ToolResult(content=f"Suggestions illisibles : {exc}", is_error=True)

        if not attente:
            return ToolResult(content="Aucune suggestion en attente.")

        lignes = [f"{len(attente)} suggestion(s) en attente de ta décision :"]
        for init in attente[:_MAX_LISTEES]:
            lignes.append(
                f"- [{_valeur(init, 'priority', '?')}] {_valeur(init, 'title')} "
                f"— {_valeur(init, 'action')} (id: {_valeur(init, 'id')})"
            )
        reste = len(attente) - _MAX_LISTEES
        if reste > 0:
            lignes.append(f"… et {reste} autre(s), moins urgentes.")
        lignes.append(
            "Propose-les UNE À LA FOIS. Pour trancher : "
            "initiatives(action='approve'|'reject', initiative_id=…)."
        )
        return ToolResult(content="\n".join(lignes))

    # ── Arbitrage ────────────────────────────────────────────────────────────

    def _arbitrer(self, action: str, initiative_id: str) -> ToolResult:
        if not initiative_id.strip():
            return ToolResult(
                content=(
                    f"Identifiant manquant : « {action} » a besoin de savoir QUELLE "
                    "suggestion trancher. Appelle d'abord action='list'."
                ),
                is_error=True,
            )

        try:
            attente = self._en_attente()
        except _LectureImpossible as exc:
            return ToolResult(content=f"Suggestions illisibles : {exc}", is_error=True)

        reference = initiative_id.strip()
        connus = {_valeur(i, "id"): i for i in attente}
        cible = connus.get(reference)

        if cible is None:
            # Repli par le titre. Le Gateway n'enchaîne QU'UN tour d'outils :
            # le modèle ne peut pas appeler 'list' pour trouver l'identifiant
            # puis 'reject' dans le même échange. Sans ce repli, « écarte celle
            # sur le brain dump » ne pouvait aboutir qu'au tour suivant — et le
            # modèle relistait en boucle sans jamais trancher.
            mots = reference.lower()
            candidats = [i for i in attente if mots in _valeur(i, "title").lower()]
            if len(candidats) == 1:
                cible = candidats[0]
            elif len(candidats) > 1:
                titres = " / ".join(_valeur(i, "title") for i in candidats[:4])
                return ToolResult(
                    content=(
                        f"« {reference} » correspond à {len(candidats)} suggestions : "
                        f"{titres}. Demande laquelle avant de trancher."
                    ),
                    is_error=True,
                )

        if cible is None:
            # Ni identifiant, ni titre : le dire, plutôt que d'écrire un statut
            # dans le vide et d'annoncer un succès.
            dispo = " / ".join(_valeur(i, "title") for i in attente[:4]) or "aucune"
            return ToolResult(
                content=(
                    f"Aucune suggestion en attente ne correspond à « {reference} » — "
                    f"elle a peut-être déjà été tranchée. En attente : {dispo}."
                ),
                is_error=True,
            )

        statut = "approved" if action == "approve" else "rejected"
        try:
            self._store.update_status(_valeur(cible, "id"), statut)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(content=f"Décision non enregistrée : {exc}", is_error=True)

        verbe = "acceptée" if action == "approve" else "écartée"
        restant = len(attente) - 1
        suite = f" Il en reste {restant}." if restant > 0 else " Il n'en reste aucune."
        return ToolResult(content=f"« {_valeur(cible, 'title')} » {verbe}.{suite}")


class _LectureImpossible(RuntimeError):
    """Le magasin d'initiatives n'a pas pu être lu."""
