# Copyright (C) 2026 Max Ea
# This file is part of CRUSH-OS,   .


"""Memory Kernel — couche d'accès SQLite source de vérité unique (CDC §6.1, §6.2).

Une base unique `memory_data/crush_memory.db`, quatre tables (events, facts,
fact_observations, fact_relations) + une virtual table FTS5 pour la recherche
textuelle des facts. Pas de sqlite-vec en PHASE 3 (décision : FTS5 seul pour
la pertinence — embeddings de facts reportés à PHASE 3.x si nécessaire).

API synchrone (sqlite3 stdlib) ; les appels asynchrones sont délégués à un
thread par les couches consommatrices (ingest, retrieval).

Invariants :
- On ne supprime JAMAIS un event ni un fact contredit (archive/superseded, jamais delete).
- Un fact actif ≡ status=ACTIVE.
- subject/predicate/category/object sont normalisés en lowercase et trim pour le matching.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from crush.providers.memory.schemas import (
    DecayPolicy,
    Event,
    Fact,
    FactObservation,
    FactRelation,
    FactStatus,
    ObservationType,
    RelationType,
)

# ── Schéma SQL ────────────────────────────────────────────────────────────────


_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS events (
        id TEXT PRIMARY KEY,
        type TEXT NOT NULL,
        source TEXT NOT NULL,
        content TEXT NOT NULL,
        metadata_json TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_events_created_at ON events(created_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS facts (
        id TEXT PRIMARY KEY,
        subject TEXT NOT NULL,
        predicate TEXT NOT NULL,
        object TEXT NOT NULL,
        category TEXT NOT NULL,
        status TEXT NOT NULL,
        confidence REAL NOT NULL,
        support_count INTEGER NOT NULL,
        decay_policy TEXT NOT NULL,
        importance REAL NOT NULL DEFAULT 0.5,
        valid_from TEXT,
        valid_to TEXT,
        source_event_id TEXT,
        created_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (source_event_id) REFERENCES events(id)
    )
    """,
    # Index combiné (subject, predicate, category) pour le matching de réconciliation.
    """
    CREATE INDEX IF NOT EXISTS idx_facts_match
        ON facts(subject, predicate, category, status)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_facts_status ON facts(status)
    """,
    """
    CREATE TABLE IF NOT EXISTS fact_observations (
        id TEXT PRIMARY KEY,
        fact_id TEXT NOT NULL,
        event_id TEXT NOT NULL,
        observation_type TEXT NOT NULL,
        confidence_delta REAL NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY (fact_id) REFERENCES facts(id),
        FOREIGN KEY (event_id) REFERENCES events(id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_obs_fact ON fact_observations(fact_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS fact_relations (
        id TEXT PRIMARY KEY,
        from_fact_id TEXT NOT NULL,
        to_fact_id TEXT NOT NULL,
        relation_type TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY (from_fact_id) REFERENCES facts(id),
        FOREIGN KEY (to_fact_id) REFERENCES facts(id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_rel_from ON fact_relations(from_fact_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_rel_to ON fact_relations(to_fact_id)
    """,
    # FTS5 sur le texte concaténé d'un fact pour la pertinence retrieval.
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(
        fact_id UNINDEXED,
        text,
        tokenize='unicode61 remove_diacritics 1'
    )
    """,
]


def _now_iso() -> str:
    return datetime.now().isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def normalize(s: str) -> str:
    """Normalise un terme pour le matching (lowercase, strip)."""
    return s.strip().lower()


# Mots qui ne portent aucune information de recherche. Sans ce filtre, « de » et
# « la » remonteraient la moitié de la base avec un bon score BM25, et noieraient
# les termes qui comptent.
_MOTS_VIDES_FTS = frozenset(
    """a ai as au aux avec ce ces dans de des du elle en est et eux il ils je
       la le les leur lui ma mais me mes moi mon ne nos notre nous on ou par
       pas peu pour qu que qui sa se ses son sont sur ta te tes toi ton tu un
       une vos votre vous y d l n s c j m t qu quoi dont quel quelle quels
       quelles comment pourquoi mets mettre fais faire dis dire""".split()
)


# Plafond de termes envoyés à FTS5. Une question de plus de douze mots porteurs
# n'est plus une recherche, c'est un paragraphe.
_MAX_TERMES = 12
# Un mot d'au moins six lettres reçoit aussi une racine tronquée à cinq, pour
# attraper les flexions : `projets` doit trouver « projet ».
_LONGUEUR_MIN_RACINE = 6
_LONGUEUR_RACINE = 5


def _termes_fts(query: str) -> list[str]:
    """Les termes porteurs d'une requête, prêts pour un MATCH FTS5.

    Uniquement des caractères alphanumériques : ce qui sort d'ici ne peut pas
    être confondu avec un opérateur FTS5 (`"`, `*`, `(`, `:`, `NEAR`...), donc
    aucune saisie utilisateur ne peut casser la requête ni en détourner le sens.
    """
    mots = re.findall(r"[0-9a-zà-öø-ÿ]{2,}", query.lower())
    porteurs = [m for m in mots if m not in _MOTS_VIDES_FTS][:_MAX_TERMES]

    # Un préfixe FTS5 s'étend vers la DROITE : `projet*` trouve « projets », mais
    # `projets*` ne trouve PAS « projet ». Or l'utilisateur écrit au pluriel et le
    # fait est stocké au singulier — mesuré sur la base réelle : la question
    # « quels sont mes projets » ne trouvait pas `max decided refonte projet`.
    #
    # On émet donc aussi une racine tronquée pour les mots longs. Volontairement
    # grossier : cela élargit le filet (`proje*` attrape aussi « projeter »), et
    # c'est acceptable parce que ce n'est qu'un ensemble de CANDIDATS — le score
    # de `retrieval.py` les reclasse ensuite.
    termes: list[str] = []
    for m in porteurs:
        termes.append(m)
        if len(m) >= _LONGUEUR_MIN_RACINE:
            termes.append(m[:_LONGUEUR_RACINE])
    # Dédoublonné en gardant l'ordre : « projets » et « projet » produiraient
    # deux fois « proje ».
    return list(dict.fromkeys(termes))


# ── Kernel ────────────────────────────────────────────────────────────────────


class MemoryKernel:
    """Couche d'accès SQLite. Source de vérité unique pour la mémoire structurée."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @property
    def db_path(self) -> Path:
        return self._db_path

    def _init_schema(self) -> None:
        with self._conn() as conn:
            for stmt in _SCHEMA:
                conn.execute(stmt)
            conn.commit()
        logger.debug("MemoryKernel schema ready", path=str(self._db_path))

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        # Phase C §C.1.8 — accès concurrent process API + process voix :
        # - WAL : les lecteurs ne bloquent plus l'écrivain (et inversement).
        # - busy_timeout=5000 : sans ce timeout, WAL sérialise toujours les
        #   écrivains → un write concurrent voix+API échouerait avec
        #   `database is locked` au lieu d'attendre. 5s de marge.
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 5000")
        try:
            yield conn
        finally:
            conn.close()

    # ── Events ────────────────────────────────────────────────────────────────

    def log_event(
        self,
        type: str,  # noqa: A002 — nom imposé par le contrat §6.2
        source: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> Event:
        """Insère un event brut. Immuable — jamais supprimé."""
        evt = Event(
            id=_new_id("evt"),
            type=type,
            source=source,
            content=content,
            created_at=datetime.now(),
            metadata_json=json.dumps(metadata, ensure_ascii=False) if metadata else None,
        )
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO events(id, type, source, content, metadata_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    evt.id,
                    evt.type,
                    evt.source,
                    evt.content,
                    evt.metadata_json,
                    evt.created_at.isoformat(),
                ),
            )
            conn.commit()
        return evt

    def list_events_between(
        self,
        debut: datetime,
        fin: datetime,
        limit: int = 200,
        types_exclus: tuple[str, ...] = (),
    ) -> list[Event]:
        """Les événements d'une période, du plus ancien au plus récent.

        La table `events` porte un index sur `created_at` depuis toujours, et
        aucune méthode ne permettait de s'en servir : on ne pouvait lire un
        événement que par son identifiant. Le journal de ce que l'assistant a
        vécu était donc là, indexé, et inatteignable — « qu'est-ce que j'ai fait
        mardi ? » n'avait pas de réponse.

        Ordre chronologique et non l'inverse : on relit une journée dans le sens
        où elle s'est déroulée.
        """
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE created_at >= ? AND created_at < ? "
                "ORDER BY created_at ASC LIMIT ?",
                (debut.isoformat(), fin.isoformat(), max(1, limit)),
            ).fetchall()
        evenements = [self._row_to_event(r) for r in rows]
        if types_exclus:
            evenements = [e for e in evenements if e.type not in types_exclus]
        return evenements

    def list_facts_seen_between(
        self, debut: datetime, fin: datetime, limit: int = 100
    ) -> list[Fact]:
        """Les faits appris OU revus dans la période.

        `last_seen_at` et pas seulement `created_at` : un souvenir ancien reconfirmé
        mardi fait partie de ce dont on a parlé mardi, alors qu'il n'a pas été
        appris ce jour-là. Ne rendre que les créations donnerait une journée
        artificiellement vide dès qu'on a surtout parlé de choses déjà sues.
        """
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM facts WHERE (last_seen_at >= ? AND last_seen_at < ?) "
                "OR (created_at >= ? AND created_at < ?) "
                "ORDER BY last_seen_at ASC LIMIT ?",
                (
                    debut.isoformat(),
                    fin.isoformat(),
                    debut.isoformat(),
                    fin.isoformat(),
                    max(1, limit),
                ),
            ).fetchall()
        return [self._row_to_fact(r) for r in rows]

    def get_event(self, event_id: str) -> Event | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        return self._row_to_event(row) if row else None

    def count_events(self) -> int:
        with self._conn() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])

    # ── Facts ─────────────────────────────────────────────────────────────────

    def insert_fact(self, fact: Fact) -> None:
        """Insère un nouveau fact. Met aussi à jour l'index FTS5."""
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO facts(id, subject, predicate, object, category, status, "
                "confidence, support_count, decay_policy, importance, valid_from, valid_to, "
                "source_event_id, created_at, last_seen_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    fact.id,
                    fact.subject,
                    fact.predicate,
                    fact.object,
                    fact.category,
                    fact.status.value,
                    fact.confidence,
                    fact.support_count,
                    fact.decay_policy.value,
                    fact.importance,
                    fact.valid_from.isoformat() if fact.valid_from else None,
                    fact.valid_to.isoformat() if fact.valid_to else None,
                    fact.source_event_id,
                    fact.created_at.isoformat(),
                    fact.last_seen_at.isoformat(),
                    fact.updated_at.isoformat(),
                ),
            )
            self._fts_upsert(conn, fact)
            conn.commit()

    def update_fact(self, fact: Fact) -> None:
        """Met à jour un fact existant + re-indexe FTS5.

        `object` fait partie de la clause SET, et ce n'était pas le cas. Seul
        `apply_correction` modifie ce champ (§6.7) — le chemin de correction
        DOCUMENTÉ — et il renvoyait un fact portant la nouvelle valeur sans que
        rien ne la persiste : la correction disparaissait au prochain
        `get_fact`. Pire, `_fts_upsert` ci-dessous indexait, lui, la valeur
        corrigée : une recherche plein texte trouvait le fait par son nouveau
        libellé et l'affichait avec l'ancien.

        Le test qui couvrait ce chemin vérifiait l'objet RENVOYÉ, jamais une
        relecture en base — d'où la survie du défaut.
        """
        with self._conn() as conn:
            self._update_fact_on(conn, fact)
            conn.commit()

    def _update_fact_on(self, conn: sqlite3.Connection, fact: Fact) -> None:
        """L'écriture seule, sur une connexion fournie et SANS commit.

        Extrait pour que `fusionner_doublons` puisse écrire plusieurs lignes dans
        UNE transaction. Une fusion enchaîne un survivant et N archivages ; avec
        une connexion par ligne, chacune committait seule, et un plantage entre
        deux laissait le survivant porteur du total du groupe pendant que des
        doublons restaient ACTIFS. La passe suivante les re-sommait : le support
        du survivant gonflait, définitivement, et c'est justement la clé de tri
        qui le désigne survivant.
        """
        conn.execute(
            "UPDATE facts SET object=?, status=?, confidence=?, support_count=?, "
            "decay_policy=?, importance=?, valid_from=?, valid_to=?, "
            "last_seen_at=?, updated_at=? WHERE id=?",
            (
                fact.object,
                fact.status.value,
                fact.confidence,
                fact.support_count,
                fact.decay_policy.value,
                fact.importance,
                fact.valid_from.isoformat() if fact.valid_from else None,
                fact.valid_to.isoformat() if fact.valid_to else None,
                fact.last_seen_at.isoformat(),
                datetime.now().isoformat(),
                fact.id,
            ),
        )
        self._fts_upsert(conn, fact)

    def get_fact(self, fact_id: str) -> Fact | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM facts WHERE id = ?", (fact_id,)).fetchone()
        return self._row_to_fact(row) if row else None

    def fusionner_doublons(self) -> int:
        """Fusionne les faits ACTIFS strictement identiques. Rend le nombre absorbé.

        POURQUOI CETTE PASSE EXISTE

        `find_active_exact` empêche désormais la CRÉATION de doublons, mais la
        base en contient déjà : mesuré, 4 × `max uses iris`, 4 × `max uses
        spotify`, et trois autres groupes. Ils venaient d'un matcher qui ne
        regardait que (sujet, prédicat, catégorie) et rendait un seul fait.

        Ce n'est pas qu'une question de propreté : le bloc de mémoire injecté
        dans le prompt est PLAFONNÉ. Quatre exemplaires du même fait mangent
        quatre places, et l'assistant paraît répéter ce qu'il sait.

        ON N'EFFACE RIEN. Le survivant est celui qui a le plus de support, puis
        la meilleure confiance ; il HÉRITE de la somme des supports et de la
        confiance maximale du groupe — une répétition est une confirmation. Les
        autres passent en SUPERSEDED, reliés au survivant, donc l'historique
        reste vérifiable.
        """
        with self._conn() as conn:
            groupes = conn.execute(
                "SELECT subject, predicate, object, category, COUNT(*) n "
                "FROM facts WHERE status = ? "
                "GROUP BY subject, predicate, object, category HAVING n > 1",
                (FactStatus.ACTIVE.value,),
            ).fetchall()

        absorbes = 0
        for g in groupes:
            # UNE transaction par groupe. Tout ou rien : soit le survivant porte
            # le total ET les doublons sont archivés, soit le groupe est intact.
            #
            # Un groupe par transaction plutôt que la passe entière : un groupe
            # illisible ou verrouillé ne doit pas annuler les fusions déjà
            # faites, et une transaction courte ne retient pas le verrou d'écriture
            # pendant toute la passe — le process voix écrit sur la même base.
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT * FROM facts WHERE subject=? AND predicate=? AND object=? "
                    "AND category=? AND status=?",
                    (g["subject"], g["predicate"], g["object"], g["category"],
                     FactStatus.ACTIVE.value),
                ).fetchall()
                faits = [self._row_to_fact(r) for r in rows]
                if len(faits) < 2:
                    continue

                faits.sort(key=lambda f: (-f.support_count, -f.confidence, f.created_at))
                survivant, doublons = faits[0], faits[1:]

                survivant.support_count = sum(f.support_count for f in faits)
                survivant.confidence = max(f.confidence for f in faits)
                survivant.importance = max(f.importance for f in faits)
                survivant.last_seen_at = max(f.last_seen_at for f in faits)
                self._update_fact_on(conn, survivant)

                for d in doublons:
                    d.status = FactStatus.SUPERSEDED
                    self._update_fact_on(conn, d)
                    self._link_facts_on(
                        conn,
                        FactRelation(
                            id=_new_id("rel"),
                            from_fact_id=survivant.id,
                            to_fact_id=d.id,
                            relation_type=RelationType.SUPERSEDES,
                            created_at=datetime.now(),
                        ),
                    )
                conn.commit()
                absorbes += len(doublons)

        if absorbes:
            logger.info(
                "Doublons de faits fusionnes", absorbes=absorbes, groupes=len(groupes)
            )
        return absorbes

    def find_active_exact(
        self, subject: str, predicate: str, object: str, category: str  # noqa: A002
    ) -> Fact | None:
        """Le fait ACTIF exactement identique, objet compris.

        POURQUOI CET ÉTAGE MANQUAIT

        `find_active_match` ne regarde que (sujet, prédicat, catégorie) et rend UN
        seul fait, le plus récemment vu. Ce modèle convient à un fait à valeur
        unique — « max préfère le café » — mais pas à « max utilise X », où X est
        multiple.

        Conséquence mesurée sur la base réelle : à l'arrivée de « max uses
        spotify », le matcher rendait « max uses vue obsidian » (même triplet,
        vu plus récemment), les objets différaient, et la réconciliation créait
        un fait. À chaque passage. Résultat : 4 × `max uses spotify`,
        4 × `max uses iris`, et trois autres groupes en double.

        Ce doublonnage abîme aussi la RÉCUPÉRATION : une recherche sur
        « spotify » rendait quatre fois la même chose, gaspillant quatre places
        du contexte au lieu d'une.
        """
        s, p = normalize(subject), normalize(predicate)
        o, c = normalize(object), normalize(category)
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM facts WHERE subject=? AND predicate=? AND object=? "
                "AND category=? AND status=? ORDER BY last_seen_at DESC LIMIT 1",
                (s, p, o, c, FactStatus.ACTIVE.value),
            ).fetchone()
        return self._row_to_fact(row) if row else None

    def find_active_match(self, subject: str, predicate: str, category: str) -> Fact | None:
        """Cherche un fact ACTIF avec même (subject, predicate, category) normalisés.

        Sert au matching de réconciliation §6.4 étape 4.
        """
        s, p, c = normalize(subject), normalize(predicate), normalize(category)
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM facts WHERE subject=? AND predicate=? AND category=? "
                "AND status=? ORDER BY last_seen_at DESC LIMIT 1",
                (s, p, c, FactStatus.ACTIVE.value),
            ).fetchone()
        return self._row_to_fact(row) if row else None

    def list_facts_by_status(self, status: FactStatus, limit: int | None = None) -> list[Fact]:
        sql = "SELECT * FROM facts WHERE status=? ORDER BY last_seen_at DESC"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        with self._conn() as conn:
            rows = conn.execute(sql, (status.value,)).fetchall()
        return [self._row_to_fact(r) for r in rows]

    def list_facts_by_category(
        self, category: str, status: FactStatus = FactStatus.ACTIVE
    ) -> list[Fact]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM facts WHERE category=? AND status=? ORDER BY last_seen_at DESC",
                (normalize(category), status.value),
            ).fetchall()
        return [self._row_to_fact(r) for r in rows]

    def count_facts(self, status: FactStatus | None = None) -> int:
        with self._conn() as conn:
            if status is None:
                return int(conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0])
            return int(
                conn.execute(
                    "SELECT COUNT(*) FROM facts WHERE status=?",
                    (status.value,),
                ).fetchone()[0]
            )

    # ── Observations & Relations ──────────────────────────────────────────────

    def record_observation(
        self,
        fact_id: str,
        event_id: str,
        observation_type: ObservationType,
        confidence_delta: float,
    ) -> FactObservation:
        obs = FactObservation(
            id=_new_id("obs"),
            fact_id=fact_id,
            event_id=event_id,
            observation_type=observation_type,
            confidence_delta=confidence_delta,
            created_at=datetime.now(),
        )
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO fact_observations(id, fact_id, event_id, observation_type, "
                "confidence_delta, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    obs.id,
                    obs.fact_id,
                    obs.event_id,
                    obs.observation_type.value,
                    obs.confidence_delta,
                    obs.created_at.isoformat(),
                ),
            )
            conn.commit()
        return obs

    def list_observations(self, fact_id: str) -> list[FactObservation]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM fact_observations WHERE fact_id=? ORDER BY created_at",
                (fact_id,),
            ).fetchall()
        return [
            FactObservation(
                id=r["id"],
                fact_id=r["fact_id"],
                event_id=r["event_id"],
                observation_type=ObservationType(r["observation_type"]),
                confidence_delta=r["confidence_delta"],
                created_at=datetime.fromisoformat(r["created_at"]),
            )
            for r in rows
        ]

    def link_facts(
        self, from_fact_id: str, to_fact_id: str, relation_type: RelationType
    ) -> FactRelation:
        rel = FactRelation(
            id=_new_id("rel"),
            from_fact_id=from_fact_id,
            to_fact_id=to_fact_id,
            relation_type=relation_type,
            created_at=datetime.now(),
        )
        with self._conn() as conn:
            self._link_facts_on(conn, rel)
            conn.commit()
        return rel

    @staticmethod
    def _link_facts_on(conn: sqlite3.Connection, rel: FactRelation) -> None:
        """L'insertion seule, sans commit. Même motif que `_update_fact_on`."""
        conn.execute(
            "INSERT INTO fact_relations(id, from_fact_id, to_fact_id, "
            "relation_type, created_at) VALUES (?, ?, ?, ?, ?)",
            (
                rel.id,
                rel.from_fact_id,
                rel.to_fact_id,
                rel.relation_type.value,
                rel.created_at.isoformat(),
            ),
        )

    def list_relations(self, fact_id: str) -> list[FactRelation]:
        """Toutes les relations dont le fact est source OU cible."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM fact_relations "
                "WHERE from_fact_id=? OR to_fact_id=? ORDER BY created_at",
                (fact_id, fact_id),
            ).fetchall()
        return [
            FactRelation(
                id=r["id"],
                from_fact_id=r["from_fact_id"],
                to_fact_id=r["to_fact_id"],
                relation_type=RelationType(r["relation_type"]),
                created_at=datetime.fromisoformat(r["created_at"]),
            )
            for r in rows
        ]

    # ── FTS5 ──────────────────────────────────────────────────────────────────

    def search_facts_fts(self, query: str, k: int = 10) -> list[tuple[Fact, float]]:
        """Recherche FTS5 → liste (fact, bm25_score) ; bm25 plus bas = plus pertinent.

        LE BUG QUI RENDAIT CETTE FONCTION INUTILISABLE

        La version précédente enveloppait la requête entière entre guillemets :
        `'"' + query + '"'`. En FTS5, cela ne « tolère pas les espaces » comme le
        commentaire l'affirmait — cela demande une PHRASE EXACTE, la suite
        consécutive de tous les termes. « mets de la musique » exigeait donc ces
        quatre mots dans cet ordre à l'intérieur d'un fait de trois mots.

        Mesuré sur la base réelle : `spotify` rendait 4 faits, `musique` 0, et
        `spotify musique` 0. Autrement dit, toute question formulée en langue
        naturelle rendait zéro résultat — et comme le score de `retrieval.py` est
        MULTIPLICATIF, une pertinence nulle annulait tout : le module renvoyait
        les mêmes six faits arbitraires, notés 0,000, pour n'importe quelle
        question.

        On découpe donc en termes joints par OR, chacun en préfixe : « musique »
        trouve « musiques », et une phrase remonte les faits qui partagent au
        moins un mot porteur. Les mots vides sont retirés — sans quoi « de » et
        « la » remonteraient la moitié de la base avec un bon score.
        """
        if not query.strip():
            return []
        termes = _termes_fts(query)
        if not termes:
            return []
        # Préfixe + OR : la disjonction est le seul opérateur qui laisse une
        # phrase entière trouver quelque chose. Les termes sont reconstruits à
        # partir de caractères alphanumériques uniquement, donc rien de ce que
        # l'utilisateur écrit ne peut être interprété comme un opérateur FTS5.
        safe = " OR ".join(f"{t}*" for t in termes)
        with self._conn() as conn:
            try:
                rows = conn.execute(
                    "SELECT facts.*, bm25(facts_fts) AS score FROM facts_fts "
                    "JOIN facts ON facts.id = facts_fts.fact_id "
                    "WHERE facts_fts MATCH ? "
                    "ORDER BY score LIMIT ?",
                    (safe, k),
                ).fetchall()
            except sqlite3.OperationalError:
                return []
        return [(self._row_to_fact(r), float(r["score"])) for r in rows]

    @staticmethod
    def _fts_upsert(conn: sqlite3.Connection, fact: Fact) -> None:
        """Réindexe le fact dans FTS5 (delete + insert pour gérer l'update)."""
        conn.execute("DELETE FROM facts_fts WHERE fact_id = ?", (fact.id,))
        text = " ".join(
            [
                fact.subject,
                fact.predicate,
                fact.object,
                fact.category,
            ]
        )
        conn.execute("INSERT INTO facts_fts(fact_id, text) VALUES (?, ?)", (fact.id, text))

    # ── Human correction (§6.7) ───────────────────────────────────────────────

    def apply_correction(
        self,
        target_fact_id: str,
        new_object: str | None = None,
        new_status: FactStatus | None = None,
        new_confidence: float | None = None,
        correction_text: str = "",
        source: str = "user_command",
    ) -> tuple[Event, Fact | None]:
        """Applique une correction humaine. Trace l'event, met à jour le fact.

        Renvoie (event, fact_mis_à_jour). Si target_fact_id introuvable, fact=None
        mais l'event est créé pour traçabilité.
        """
        evt = self.log_event(
            type="human_correction",
            source=source,
            content=correction_text or f"Correction du fact {target_fact_id}",
            metadata={
                "target_fact_id": target_fact_id,
                "new_object": new_object,
                "new_status": new_status.value if new_status else None,
                "new_confidence": new_confidence,
            },
        )
        fact = self.get_fact(target_fact_id)
        if fact is None:
            logger.warning("apply_correction: fact introuvable", fact_id=target_fact_id)
            return evt, None

        if new_object is not None:
            fact.object = normalize(new_object)
        if new_status is not None:
            fact.status = new_status
        if new_confidence is not None:
            fact.confidence = max(0.0, min(1.0, new_confidence))
        fact.last_seen_at = datetime.now()
        fact.updated_at = datetime.now()
        self.update_fact(fact)
        self.record_observation(
            fact_id=fact.id,
            event_id=evt.id,
            observation_type=ObservationType.CORRECT,
            confidence_delta=0.0,
        )
        return evt, fact

    # ── Row mappers ───────────────────────────────────────────────────────────

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> Event:
        return Event(
            id=row["id"],
            type=row["type"],
            source=row["source"],
            content=row["content"],
            created_at=datetime.fromisoformat(row["created_at"]),
            metadata_json=row["metadata_json"],
        )

    @staticmethod
    def _row_to_fact(row: sqlite3.Row) -> Fact:
        return Fact(
            id=row["id"],
            subject=row["subject"],
            predicate=row["predicate"],
            object=row["object"],
            category=row["category"],
            status=FactStatus(row["status"]),
            confidence=row["confidence"],
            support_count=row["support_count"],
            decay_policy=DecayPolicy(row["decay_policy"]),
            importance=row["importance"],
            valid_from=datetime.fromisoformat(row["valid_from"]) if row["valid_from"] else None,
            valid_to=datetime.fromisoformat(row["valid_to"]) if row["valid_to"] else None,
            source_event_id=row["source_event_id"],
            created_at=datetime.fromisoformat(row["created_at"]),
            last_seen_at=datetime.fromisoformat(row["last_seen_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )
