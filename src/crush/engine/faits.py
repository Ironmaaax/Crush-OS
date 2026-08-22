# Copyright (C) 2026 Maxime Song

"""Ce que l'assistant sait de son utilisateur, rendu pour le prompt.

LE TROU QUE CE MODULE COMBLE

Le Memory Kernel contenait une cinquantaine de faits structurés, scorés,
corrigeables — et AUCUN chemin ne les amenait dans une conversation. Vérifié :

- `MemoryRetrieval` (providers/memory/retrieval.py) n'était instancié nulle part.
- `memory_search` lit l'index vectoriel, alimenté par `topics/*.md` et
  `sessions/*.jsonl` uniquement. Les faits n'y entrent jamais.
- `session_recall` lit ce même index plus le FTS des transcripts.
- Le miroir Markdown n'est indexé nulle part et `memory_load_topic` ne sait pas
  le lire.

Le seul lecteur conversationnel des faits était `memory_journal`, une fenêtre de
DATES. Autrement dit : un fait appris en juin et non reconfirmé était
inatteignable, sauf en nommant son jour. Ce que l'assistant « savait » de son
utilisateur à chaque tour se réduisait à 389 octets de prose plate.

POURQUOI UN BLOC DÉTERMINISTE ET NON UNE RECHERCHE PAR REQUÊTE

À cette échelle, la recherche est une optimisation prématurée, et elle coûte
cher en effets de bord :

- Elle dépend d'une correspondance lexicale. Mesuré sur la base réelle :
  « comment tu me parles » ne trouve RIEN, alors que les faits `prefers
  concision`, `communicates_as approche directe` et `prefers appellation
  monsieur` existent — ils ne partagent simplement aucun mot avec la question.
- Elle rend le préfixe système variable d'un tour à l'autre, ce qui interdit
  toute mise en cache du prompt.
- Elle a une branche de repli qui injecte des faits arbitraires que l'appelant ne
  peut pas distinguer des faits pertinents.

Les cinquante faits filtrés tiennent dans ~500 jetons. On les donne tous, dans un
ordre stable. La sélection par requête commencera à payer vers le millier de
faits ; `retrieval.py` l'attend, réparé, pour ce jour-là.
"""

from __future__ import annotations

from crush.kernel.contracts import MemoryStore
from crush.kernel.schemas import Fact, FactStatus

# En dessous, le fait n'est pas su : c'est une hypothèse que l'ingestion n'a vue
# qu'une fois. Le miroir Markdown emploie déjà ce seuil pour mettre en quarantaine
# les croyances incertaines — on le reprend pour ne pas avoir deux vérités sur ce
# qui compte comme « su ».
_CONFIANCE_MINIMALE = 0.5

# Plafond de faits injectés. Choisi pour tenir sous ~600 jetons : le prompt
# système fait déjà ~7 800 jetons et il est repayé à chaque tour, faute de mise
# en cache. Au-delà, on paierait un contexte que le modèle survolerait.
_PLAFOND = 22

# Un objet plus long que ça n'est pas un fait, c'est une phrase que l'extraction
# a mal découpée. On le tronque plutôt que de laisser une ligne manger le bloc.
_LONGUEUR_OBJET = 90

# Les catégories, dans l'ordre où elles servent à répondre. L'identité et la
# façon de s'adresser à lui d'abord : elles pèsent sur CHAQUE réponse, alors
# qu'un souvenir d'outil ne pèse que sur les questions qui le concernent.
_ORDRE_CATEGORIES = (
    "identity",
    "persona",
    "preference",
    "values",
    "work_style",
    "constraint",
    "goal",
    "project",
    "decision",
    "habit",
    "relationship",
    "health_fitness",
    "tool",
)

_TITRES = {
    "identity": "Qui il est",
    "persona": "Comment lui parler",
    "preference": "Ce qu'il préfère",
    "values": "Ce à quoi il tient",
    "work_style": "Sa façon de travailler",
    "constraint": "Ses contraintes",
    "goal": "Ses objectifs",
    "project": "Ses projets",
    "decision": "Ses décisions",
    "habit": "Ses habitudes",
    "relationship": "Ses relations",
    "health_fitness": "Santé et forme",
    "tool": "Ses outils",
}


def _poids(fait: Fact) -> float:
    """Importance × confiance : ce qui compte, et ce dont on est sûr.

    Pas de facteur de récence ici, contrairement au score de `retrieval.py`. Un
    fait d'identité ne devient pas moins vrai parce qu'on n'en a pas reparlé
    depuis deux mois, et l'atténuer ferait sortir du bloc précisément ce qui est
    le plus stable.
    """
    return fait.importance * fait.confidence


def bloc_memoire(
    store: MemoryStore,
    plafond: int = _PLAFOND,
    confiance_minimale: float = _CONFIANCE_MINIMALE,
) -> str:
    """Rend les faits actifs en un bloc Markdown, ou une chaîne vide.

    Déterministe : à base inchangée, la sortie est identique d'un appel à
    l'autre. C'est ce qui la rend relisable — et ce qui permettra une mise en
    cache du préfixe système le jour où l'horodatage à la minute sera réglé.

    Lecture SQLite synchrone : ~50 lignes, quelques microsecondes. L'appelant
    est chargé de la déporter dans un thread s'il est asynchrone.
    """
    try:
        actifs = store.list_facts_by_status(FactStatus.ACTIVE)
    except Exception:  # noqa: BLE001 — un prompt sans mémoire vaut mieux que pas de réponse
        return ""

    retenus = [f for f in actifs if f.confidence >= confiance_minimale]
    if not retenus:
        return ""

    # On coupe sur le poids, PUIS on regroupe. L'inverse aurait laissé une
    # catégorie bavarde évincer une catégorie décisive.
    retenus.sort(key=lambda f: (-_poids(f), f.subject, f.predicate))
    retenus = retenus[: max(1, plafond)]

    par_categorie: dict[str, list[Fact]] = {}
    for f in retenus:
        par_categorie.setdefault(f.category, []).append(f)

    connues = [c for c in _ORDRE_CATEGORIES if c in par_categorie]
    autres = sorted(c for c in par_categorie if c not in _ORDRE_CATEGORIES)

    lignes: list[str] = []
    for categorie in connues + autres:
        lignes.append(f"**{_TITRES.get(categorie, categorie)}**")
        for f in par_categorie[categorie]:
            objet = f.object.strip()
            if len(objet) > _LONGUEUR_OBJET:
                objet = objet[: _LONGUEUR_OBJET - 1] + "…"
            # La confiance est rendue quand elle n'est pas franche. Un fait à
            # 55 % ne doit pas être affirmé sur le même ton qu'un fait à 95 % —
            # sans cette marque, le modèle les traite à égalité.
            marque = "" if f.confidence >= 0.8 else f" _(à confirmer, {f.confidence:.0%})_"
            lignes.append(f"- {f.predicate} {objet}{marque}")
        lignes.append("")

    return "\n".join(lignes).strip()
