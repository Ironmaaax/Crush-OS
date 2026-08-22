# Copyright (C) 2026 Max Ea
# This file is part of CRUSH-OS,   .


"""Retrieval : facts actifs + score importance × récence × pertinence × confidence (§6.9).

Récupère les facts pertinents pour une query, plus les contradictions connues
(facts supersedés liés). Pas seulement des chunks vectoriels — des facts.

Pertinence : FTS5 BM25 (sqlite-vec reporté en PHASE 3.x — cf. décision Q2=c).
Decay : applique une atténuation à la `récence` selon `decay_policy`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

from crush.providers.memory.kernel import MemoryKernel
from crush.providers.memory.schemas import DecayPolicy, Fact, FactStatus, RelationType

# ── Constantes du score ──────────────────────────────────────────────────────

# Demi-vie (jours) pour le facteur de récence par DecayPolicy (§6.6).
# Une demi-vie de 7 jours signifie : la récence vaut 0.5 au bout de 7 jours.
_HALFLIFE_DAYS: dict[DecayPolicy, float] = {
    DecayPolicy.NONE: float("inf"),
    DecayPolicy.VERY_SLOW: 365.0 * 2,  # 2 ans
    DecayPolicy.SLOW: 365.0,  # 1 an
    DecayPolicy.MEDIUM: 90.0,  # 3 mois
    DecayPolicy.FAST: 14.0,  # 2 semaines
}

# Échelle de normalisation BM25 → [0, 1]. Un |bm25| plus grand = plus pertinent ;
# au-delà de ce seuil la pertinence sature, parce que passé un certain point
# « très pertinent » ne se subdivise plus utilement. Calé sur 8 et non 20 : les
# faits sont des documents de quatre champs courts, leurs magnitudes réelles
# restent sous 8, et un plafond trop haut tassait tout dans [0,67 ; 1,0].
_BM25_CAP = 8.0


@dataclass
class ScoredFact:
    fact: Fact
    score: float
    relevance: float
    recency: float
    contradictions: list[Fact] = field(default_factory=list)


class MemoryRetrieval:
    """Récupère les facts saillants pour une query."""

    def __init__(self, kernel: MemoryKernel) -> None:
        self._kernel = kernel

    def retrieve(
        self,
        query: str,
        k: int = 5,
        now: datetime | None = None,
    ) -> list[ScoredFact]:
        """Renvoie les k facts les plus saillants pour la query.

        - Pertinence : FTS5 BM25 sur (subject + predicate + object + category).
        - Récence : decay par DecayPolicy + age (jours).
        - Score = importance × récence × pertinence × confidence.
        """
        ref = now or datetime.now()
        # On élargit la fenêtre de candidats (k*4) puis on re-score localement
        # pour intégrer les axes non-FTS (importance, récence, confidence).
        candidates = self._kernel.search_facts_fts(query, k=k * 4)
        if not candidates:
            # Pas de matching textuel : on fait un fallback léger sur les facts
            # actifs les plus récents pour ne pas renvoyer vide en cold start.
            cold = self._kernel.list_facts_by_status(FactStatus.ACTIVE, limit=k)
            candidates = [(f, 0.0) for f in cold]

        scored: list[ScoredFact] = []
        for fact, bm25 in candidates:
            if fact.status != FactStatus.ACTIVE:
                continue
            relevance = _bm25_to_relevance(bm25)
            recency = _recency_factor(fact, ref)
            total = fact.importance * recency * relevance * fact.confidence
            scored.append(
                ScoredFact(
                    fact=fact,
                    score=total,
                    relevance=relevance,
                    recency=recency,
                )
            )

        scored.sort(key=lambda s: -s.score)
        top = scored[:k]

        # Joindre les contradictions connues (facts supersedés liés)
        for sf in top:
            sf.contradictions = self._known_contradictions(sf.fact.id)
        return top

    def _known_contradictions(self, fact_id: str) -> list[Fact]:
        """Liste les facts supersedés par le fact donné (cf. §6.9 contradictions)."""
        relations = self._kernel.list_relations(fact_id)
        contradictions: list[Fact] = []
        for rel in relations:
            if rel.relation_type != RelationType.SUPERSEDES:
                continue
            if rel.from_fact_id == fact_id:
                # Ce fait supersede un autre → l'ancien est une contradiction connue
                target = self._kernel.get_fact(rel.to_fact_id)
                if target is not None:
                    contradictions.append(target)
        return contradictions


def _bm25_to_relevance(bm25: float) -> float:
    """BM25 de FTS5 → pertinence dans [0, 1], CROISSANTE avec la qualité du match.

    LA FORMULE ÉTAIT INVERSÉE

    FTS5 rend un bm25 négatif, et plus il est négatif, plus le document est
    pertinent. L'ancienne version calculait `exp(-|bm25| / cap)`, une fonction
    DÉCROISSANTE en |bm25| : un fait touchant trois termes de la question
    (bm25 ≈ -6) obtenait 0,74, tandis qu'un fait n'en touchant qu'un
    (bm25 ≈ -1,2) obtenait 0,94. Le moins pertinent gagnait de 27 %. Le
    docstring d'origine annonçait pourtant l'inverse de ce que le code faisait.

    Ce défaut est resté invisible parce que `search_facts_fts` enveloppait la
    requête en phrase exacte et ne rendait donc jamais rien : la pertinence
    valait 0 partout, la formule n'était jamais exercée. Réparer la recherche
    a rendu ce bug actif — d'où sa correction ici, dans le même mouvement.

    Le plafond passe de 20 à 8 : sur des documents de quatre champs courts
    (sujet, prédicat, objet, catégorie), les magnitudes réelles restent sous 8.
    Avec un plafond à 20, toutes les pertinences se tassaient dans [0,67 ; 1,0]
    et l'axe ne discriminait presque rien.
    """
    if bm25 == 0.0:
        # Pas de match : le repli « cold start » est traité par l'appelant.
        return 0.0
    # Croissante en |bm25|, bornée, sans cas dégénéré : 1 - exp(-x) vaut 0 en 0
    # et tend vers 1. À x = cap, on atteint 0,63 ; au-delà, la saturation est
    # voulue — passé un certain point, « très pertinent » ne se subdivise plus.
    rel = 1.0 - math.exp(-min(abs(bm25), _BM25_CAP) / _BM25_CAP)
    return max(0.0, min(1.0, rel))


def _recency_factor(fact: Fact, now: datetime) -> float:
    """Atténuation par âge selon DecayPolicy. NONE → toujours 1.0."""
    halflife = _HALFLIFE_DAYS.get(fact.decay_policy, 90.0)
    if halflife == float("inf"):
        return 1.0
    delta_days = max(0.0, (now - fact.last_seen_at).total_seconds() / 86400.0)
    return 0.5 ** (delta_days / halflife)
