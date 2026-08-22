# Copyright (C) 2026 Maxime Song

"""Rendre les faits ATTEIGNABLES en conversation, et pas seulement les 22 premiers.

LE TROU

`engine/faits.py` injecte les faits les mieux notés dans chaque prompt, mais le
bloc est plafonné : mesuré le 22/08/2026, 22 places pour 37 faits actifs. Les 15
autres n'étaient pas « mal classés », ils étaient INATTEIGNABLES — vérifié
chemin par chemin :

- `memory_search` interroge l'index vectoriel, alimenté par `topics/*.md` et
  `sessions/*.jsonl`. Aucun fait n'y entrait.
- `session_recall` lit ce même index plus le FTS des transcriptions.
- `memory_journal` demande une fenêtre de DATES : il faut déjà savoir quel jour
  la chose a été dite.
- `MemoryRetrieval` (providers/memory/retrieval.py) n'est instancié nulle part.

Autrement dit : poser une question dont la réponse tient dans un fait hors des 22
ne pouvait pas aboutir. Ce module ferme ce trou.

POURQUOI DES PHRASES ET NON DES TRIPLETS

Un embedding encode du langage, pas un schéma. `max communicates_as monsieur`
est une suite de jetons dont deux ne sont pas des mots français ; « Max veut
qu'on l'appelle monsieur » est une phrase, et c'est elle qui se rapproche de la
question « comment tu me parles ». Le rendu ci-dessous existe uniquement pour
ça : donner au modèle d'embedding de la langue à encoder.

C'est aussi ce qui distingue ce rendu de celui de `engine/faits.py` : là-bas on
compte les jetons d'un bloc payé à chaque tour, donc on écrit serré ; ici on
écrit pour être RETROUVÉ, donc on écrit en français.

POURQUOI UNE RESYNCHRONISATION EN BLOC

L'index et SQLite sont deux écritures : elles dérivent. Plutôt qu'un suivi
incrémental — qui dérive dès le premier plantage entre les deux — on retire tout
ce qui vient des faits et on réindexe l'état courant. 37 faits à 12,5 ms font
moins d'une demi-seconde, et le résultat ne dépend pas de l'historique.
"""

from __future__ import annotations

from crush.kernel.contracts import VectorIndex
from crush.providers.memory.kernel import MemoryKernel
from crush.providers.memory.schemas import Fact, FactStatus

# Marque de provenance dans les métadonnées de l'index. Sert à retirer en bloc
# les entrées issues des faits sans toucher aux topics ni aux transcriptions.
SOURCE = "fact"

# Un fait par phrase, sans découpage : `_chunk_text` en amont ne coupera pas une
# phrase de cette longueur, donc un fait reste un vecteur — ce qui garde la
# correspondance doc_id → fait exacte.
_PREFIXE_ID = "fait:"

# Prédicat → phrase. Le vocabulaire est fermé (`kernel/vocab.PREDICATES`), donc
# cette table peut l'être aussi ; un prédicat inconnu retombe sur une forme
# neutre plutôt que de disparaître de l'index.
_PHRASES = {
    "prefers": "{n} préfère {o}.",
    "communicates_as": "{n} veut qu'on lui parle avec {o}. C'est sa façon de communiquer.",
    "uses": "{n} utilise {o} comme outil.",
    "is": "{n} est {o}.",
    "decided": "{n} a décidé : {o}.",
    "plans": "{n} prévoit {o}.",
    "targets": "{n} a pour objectif {o}.",
    "works_on": "{n} travaille sur {o}.",
    "needs": "{n} a besoin de {o}.",
    "struggles_with": "{n} a des difficultés avec {o}.",
    "requires_validation_for": "{n} exige d'être consulté avant {o}.",
    "believes": "{n} pense que {o}.",
    "values": "{n} tient à {o}.",
    "has": "{n} a {o}.",
    "dislikes": "{n} n'aime pas {o}.",
    # Prédicat des corrections humaines : la formulation dit qu'un fait a CHANGÉ,
    # pour qu'une question du type « qu'est-ce que j'ai corrigé » le trouve.
    "changed": "{n} a corrigé une information : {o}.",
}

# Catégorie → mot ajouté en fin de phrase. Une question porte souvent sur le
# GENRE de l'information (« quelles sont mes contraintes ») autant que sur son
# contenu ; sans ce mot, la catégorie n'est nulle part dans le texte encodé.
_GENRES = {
    "identity": "identité",
    "persona": "manière de lui parler",
    "preference": "préférence, goût",
    "values": "valeur",
    "belief": "croyance, opinion",
    "work_style": "façon de travailler",
    "constraint": "contrainte, limite",
    "goal": "objectif, but",
    "project": "projet en cours",
    "decision": "décision, choix",
    "habit": "habitude",
    "relationship": "relation, personne",
    "health_fitness": "santé, forme physique",
    "tool": "outil, logiciel",
    "memory_correction": "correction apportée à la mémoire",
}


def texte_indexable(fait: Fact, prenom: str = "Max") -> str:
    """La phrase qui représente un fait dans l'index vectoriel.

    Trois éléments, et chacun sert une forme de question :
    la phrase (« Max préfère la concision ») pour une question directe, le genre
    (« préférence, goût ») pour une question par catégorie, et le prédicat brut
    pour qu'une recherche lexicale sur l'index reste possible.
    """
    modele = _PHRASES.get(fait.predicate, "{n} — {p} : {o}.")
    phrase = modele.format(n=prenom, o=fait.object.strip(), p=fait.predicate)
    genre = _GENRES.get(fait.category, fait.category)
    return f"{phrase} ({genre})"


class FactIndex:
    """Tient l'index vectoriel en accord avec les faits actifs du Kernel."""

    def __init__(
        self,
        kernel: MemoryKernel,
        vector_index: VectorIndex,
        user_firstname: str = "Max",
    ) -> None:
        self._kernel = kernel
        self._index = vector_index
        self._prenom = user_firstname

    async def synchroniser(self) -> int:
        """Réindexe tous les faits actifs. Rend le nombre indexé.

        En bloc et non incrémental : voir l'en-tête du module. L'ordre compte —
        on RETIRE d'abord, sinon un fait archivé la nuit précédente (doublon
        absorbé, croyance corrigée) resterait trouvable alors qu'il n'est plus
        vrai, et rien ne le distinguerait d'un fait courant.
        """
        await self._index.remove_source(SOURCE)

        actifs = self._kernel.list_facts_by_status(FactStatus.ACTIVE)
        for fait in actifs:
            await self._index.add(
                doc_id=f"{_PREFIXE_ID}{fait.id}",
                text=texte_indexable(fait, self._prenom),
                metadata={
                    "source": SOURCE,
                    "fact_id": fait.id,
                    "category": fait.category,
                    "predicate": fait.predicate,
                    "confidence": fait.confidence,
                },
            )
        await self._index.persist()
        return len(actifs)
