# Copyright (C) 2026 Maxime Song

"""« Qu'est-ce que j'ai fait mardi ? » — la mémoire sur l'axe du temps.

CE QUI MANQUAIT

La mémoire retenait des faits sans âge utile : « max préfère le café » est vrai
en permanence, et rien ne disait quand on en avait parlé. La table `events` porte
pourtant chaque échange avec sa date, et un index sur `created_at` depuis le
premier jour — mais aucune méthode ne permettait de la lire par période. Le
journal était là, indexé, et inatteignable.

DEUX SOURCES, PARCE QU'UNE SEULE MENTIRAIT

- les **événements** : ce qui s'est passé, dans l'ordre où c'est arrivé ;
- les **faits vus** : ce dont on a parlé, y compris d'anciens souvenirs
  reconfirmés ce jour-là. Ne compter que les créations donnerait une journée
  artificiellement vide dès qu'on a surtout parlé de choses déjà sues.

LES DATES SONT TOLÉRANTES, PAS DEVINÉES

`hier`, `aujourd_hui`, `-3j` et `2026-08-19` sont acceptés, parce que c'est ce
qu'un modèle écrit spontanément et qu'un refus sur la forme coûte un aller-retour
pour rien. Mais rien n'est inventé : une date illisible est REFUSÉE avec la liste
des formes admises, plutôt que ramenée silencieusement à aujourd'hui — répondre
sur la mauvaise journée est pire que ne pas répondre.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from crush.capabilities.tools.base import Tool, ToolResult
from crush.kernel.contracts import MemoryStore

# Le bruit de fonctionnement de l'assistant. Il a sa place dans l'audit, pas dans
# la réponse à « qu'est-ce que j'ai fait » : ces lignes sont ce que l'assistant a
# fait de lui-même, pas ce que l'utilisateur a vécu.
_TYPES_TECHNIQUES = ("session_summary", "capability_gap_recorded", "skill_candidate_proposal")

_RELATIF = re.compile(r"^-\s*(\d{1,3})\s*(j|jours?|d|days?)$", re.IGNORECASE)


def _lire_date(texte: str, defaut: datetime) -> datetime | None:
    """Traduit une date écrite comme elle vient. `None` si illisible."""
    brut = (texte or "").strip().lower()
    if not brut:
        return defaut

    aujourd_hui = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    if brut in ("aujourd_hui", "aujourd'hui", "today", "ce jour"):
        return aujourd_hui
    if brut in ("hier", "yesterday"):
        return aujourd_hui - timedelta(days=1)
    if brut in ("avant-hier", "avant_hier"):
        return aujourd_hui - timedelta(days=2)
    if brut in ("demain", "tomorrow"):
        return aujourd_hui + timedelta(days=1)

    relatif = _RELATIF.match(brut)
    if relatif:
        return aujourd_hui - timedelta(days=int(relatif.group(1)))

    for motif in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%d/%m/%Y", "%d/%m"):
        try:
            lu = datetime.strptime(brut, motif)
        except ValueError:
            continue
        # `%d/%m` sans année tombe en 1900 : on la remet à l'année courante, sans
        # quoi la période serait vide et l'utilisateur croirait n'avoir rien fait.
        return lu.replace(year=aujourd_hui.year) if lu.year == 1900 else lu
    return None


def _resumer_evenement(evt: object) -> str:
    contenu = " ".join(str(getattr(evt, "content", "")).split())
    if len(contenu) > 220:
        contenu = contenu[:219] + "…"
    quand = getattr(evt, "created_at", None)
    heure = quand.strftime("%H:%M") if isinstance(quand, datetime) else "--:--"
    return f"  {heure} · [{getattr(evt, 'type', '?')}] {contenu}"


class MemoryJournalTool(Tool):
    """Ce qui s'est passé sur une période donnée."""

    name = "memory_journal"
    description = (
        "Ce qui s'est passé sur une période : échanges, souvenirs appris ou "
        "reconfirmés, corrections. Répond aux questions de MÉMOIRE DATÉE — "
        "« qu'est-ce que j'ai fait mardi ? », « de quoi a-t-on parlé la semaine "
        "dernière ? », « depuis quand je travaille là-dessus ? ». "
        "Différent de `memory_search`, qui cherche PAR SUJET sans notion de date, "
        "et de `session_recall`, qui relit une conversation précise. "
        "Résoudre le jour soi-même avant d'appeler : « mardi » devient une date."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "depuis": {
                "type": "string",
                "description": (
                    "Début de la période. Formes admises : 2026-08-19, 19/08/2026, "
                    "19/08, hier, avant-hier, aujourd_hui, -7j."
                ),
            },
            "jusqu_a": {
                "type": "string",
                "description": (
                    "Fin de la période, exclue. Omettre pour couvrir la seule "
                    "journée de `depuis`."
                ),
            },
            "limite": {
                "type": "integer",
                "description": "Nombre maximal d'événements rendus (défaut 60, max 300).",
            },
        },
        "required": ["depuis"],
    }

    def __init__(self, kernel: MemoryStore) -> None:
        self._kernel = kernel

    async def execute(
        self, depuis: str = "", jusqu_a: str = "", limite: int = 60, **_: object
    ) -> ToolResult:
        maintenant = datetime.now()
        debut = _lire_date(depuis, maintenant)
        if debut is None:
            return ToolResult(
                content=(
                    f"Date de début illisible : « {depuis} ». Formes admises : "
                    "2026-08-19, 19/08/2026, 19/08, hier, avant-hier, aujourd_hui, -7j."
                ),
                is_error=True,
            )

        if str(jusqu_a).strip():
            fin = _lire_date(jusqu_a, maintenant)
            if fin is None:
                return ToolResult(
                    content=f"Date de fin illisible : « {jusqu_a} ».", is_error=True
                )
            # Une fin donnée sans heure désigne la journée ENTIÈRE : « du 19 au 20 »
            # doit inclure le 20, sinon on répond à côté d'un jour.
            if fin.hour == 0 and fin.minute == 0:
                fin += timedelta(days=1)
        else:
            fin = debut + timedelta(days=1)

        if fin <= debut:
            return ToolResult(content="La fin précède le début.", is_error=True)

        try:
            plafond = max(1, min(300, int(limite)))
        except (TypeError, ValueError):
            plafond = 60

        evenements = self._kernel.list_events_between(  # type: ignore[attr-defined]
            debut, fin, limit=plafond, types_exclus=_TYPES_TECHNIQUES
        )
        faits = self._kernel.list_facts_seen_between(debut, fin, limit=60)  # type: ignore[attr-defined]

        lignes = [f"Période : du {debut:%d/%m/%Y %H:%M} au {fin:%d/%m/%Y %H:%M} (exclu)."]

        if evenements:
            lignes.append(f"\n{len(evenements)} événement(s) :")
            lignes += [_resumer_evenement(e) for e in evenements]
        else:
            lignes.append("\nAucun événement enregistré sur cette période.")

        if faits:
            lignes.append(f"\n{len(faits)} souvenir(s) appris ou reconfirmés :")
            for f in faits:
                neuf = f.created_at >= debut if isinstance(f.created_at, datetime) else False
                marque = "nouveau" if neuf else "revu"
                lignes.append(f"  [{marque}] {f.subject} {f.predicate} {f.object}")

        if not evenements and not faits:
            # On distingue « rien ne s'est passé » de « la mémoire ne va pas si
            # loin » : sans ça, une question sur l'an dernier renverrait un vide
            # qu'on lirait comme « je n'ai rien fait ».
            lignes.append(
                "\nÀ noter : la mémoire ne remonte qu'au premier échange enregistré. "
                "Une période vide peut signifier qu'elle est antérieure à cette date."
            )

        return ToolResult(content="\n".join(lignes))
