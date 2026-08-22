# Copyright (C) 2026 Maxime Song

"""Le CÂBLAGE de la mémoire, et non son contenu.

`test_memoire_dans_le_prompt.py` vérifie que `bloc_memoire()` rend le bon texte.
Ce fichier vérifie la question d'à côté, celle qui avait vraiment coûté cher :
est-ce que ce texte ARRIVE au modèle ?

Le bug d'origine n'était pas un mauvais rendu. C'était une cinquantaine de faits
corrects, scorés, corrigeables — et aucun appelant. Trois raccords peuvent
casser en silence sans qu'aucun test de contenu ne bronche :

1. `Gateway(memory_store=…)` → le prompt système du premier appel LLM.
2. Les notifications → le prompt système du SECOND appel (celui qui écrit la
   réponse réellement lue ; la passe 1 n'émet qu'un accusé de réception).
3. `AutoDream._run_deep` → `fusionner_doublons()`, qui n'avait aucun appelant
   hors tests.

Chacun s'assure ici en observant ce que le provider LLM REÇOIT, jamais en
inspectant l'implémentation qui le construit.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path

from crush.engine.agent import Agent
from crush.engine.background.notifications import NotificationQueue
from crush.engine.background.worker import BackgroundWorker
from crush.engine.gateway import Gateway
from crush.engine.session import SessionManager
from crush.kernel.schemas import ToolCapture
from crush.kernel.settings import settings as _test_settings
from crush.providers.llm.base import LLMProvider
from crush.providers.memory.auto_dream import AutoDream
from crush.providers.memory.kernel import MemoryKernel
from crush.providers.memory.schemas import DecayPolicy, Fact, FactStatus


class _LLMMouchard(LLMProvider):
    """Enregistre chaque prompt système reçu. C'est le seul point d'observation
    honnête : ce que le modèle voit vraiment, pas ce que le code croit envoyer."""

    def __init__(self, response: str = "[I] Bien monsieur.") -> None:
        self._response = response
        self.systems: list[str] = []

    async def complete(
        self,
        messages: list[dict],
        system: str,
        tools: list[dict] | None = None,
        stream: bool = False,
        context: str = "",
    ) -> str | AsyncIterator[str]:
        self.systems.append(system)
        if stream:
            return self._stream()
        return self._response

    async def _stream(self) -> AsyncIterator[str]:
        for mot in self._response.split():
            yield mot + " "

    async def health_check(self) -> bool:
        return True


def _fait_identite(kernel: MemoryKernel) -> None:
    """Un fait dont l'objet est introuvable par hasard dans un prompt système."""
    quand = datetime.now()
    kernel.insert_fact(
        Fact(
            id="f_cablage",
            subject="max",
            predicate="pilote",
            object="zeppelin-cramoisi",
            category="identity",
            status=FactStatus.ACTIVE,
            confidence=0.95,
            support_count=3,
            decay_policy=DecayPolicy.SLOW,
            importance=0.9,
            created_at=quand,
            last_seen_at=quand,
            updated_at=quand,
        )
    )


# ── 1. Gateway → premier appel LLM ────────────────────────────────────────────


async def test_les_faits_atteignent_le_modele(tmp_path: Path) -> None:
    """LE test qui manquait. `bloc_memoire()` pouvait rendre le texte parfait
    pendant que `Gateway` ne l'appelait jamais — c'était exactement l'état du
    code avant cette série de correctifs."""
    kernel = MemoryKernel(tmp_path / "k.db")
    _fait_identite(kernel)

    llm = _LLMMouchard()
    gw = Gateway(
        session_manager=SessionManager(),
        agent=Agent(settings=_test_settings, llm=llm),
        notifications=NotificationQueue(),
        worker=BackgroundWorker(llm=llm, notifications=NotificationQueue()),
        memory_store=kernel,
    )

    await gw.handle(message="Salut", stream=False)

    assert llm.systems, "aucun appel LLM — le test n'observe rien"
    assert "zeppelin-cramoisi" in llm.systems[0]


async def test_sans_memory_store_le_prompt_reste_valide(tmp_path: Path) -> None:
    """Le paramètre est optionnel : les appelants qui ne le passent pas (voix,
    tâches de fond) doivent continuer de fonctionner, sans bloc et sans en-tête
    vide."""
    llm = _LLMMouchard()
    gw = Gateway(
        session_manager=SessionManager(),
        agent=Agent(settings=_test_settings, llm=llm),
        notifications=NotificationQueue(),
        worker=BackgroundWorker(llm=llm, notifications=NotificationQueue()),
    )

    await gw.handle(message="Salut", stream=False)

    assert llm.systems
    assert "Ce que tu sais de" not in llm.systems[0]


# ── 2. Retransmission au second appel LLM ─────────────────────────────────────


async def test_le_second_appel_recoit_notifications_et_memoire() -> None:
    """`synthesize` appelait `_build_system()` NU. Or c'est cette passe qui écrit
    la réponse lue par l'utilisateur : l'assistant perdait notifications, rappel
    et mémoire exactement dès qu'un outil entrait en jeu."""
    llm = _LLMMouchard("Voilà monsieur.")
    agent = Agent(settings=_test_settings, llm=llm)
    session = SessionManager().get_or_create(None)

    capture = ToolCapture(calls=[("t1", "spotify_play", {"query": "liberta"})])
    flux = agent.synthesize(
        session,
        ack_text="Je lance ça.",
        capture=capture,
        results=["ok"],
        notifications=["La sauvegarde nocturne a échoué."],
        recall_summary="Hier il parlait de son Pi.",
        memoire="**Qui il est**\n- pilote zeppelin-cramoisi",
    )
    async for _ in flux:
        pass

    assert llm.systems
    systeme = llm.systems[-1]
    assert "sauvegarde nocturne" in systeme
    assert "zeppelin-cramoisi" in systeme
    assert "son Pi" in systeme


# ── 3. AutoDream → fusion des doublons ────────────────────────────────────────


async def test_la_passe_nocturne_absorbe_les_doublons(tmp_path: Path) -> None:
    """`fusionner_doublons()` existait, était testée, et n'avait AUCUN appelant en
    production. `find_active_exact` empêche d'en créer de nouveaux, mais les
    groupes déjà présents (9 sur 50, mesuré) seraient restés indéfiniment, chacun
    mangeant une place du bloc mémoire plafonné."""
    kernel = MemoryKernel(tmp_path / "k.db")
    quand = datetime.now()
    for i in range(3):
        kernel.insert_fact(
            Fact(
                id=f"dup{i}",
                subject="max",
                predicate="prefers",
                object="concision",
                category="preference",
                status=FactStatus.ACTIVE,
                confidence=0.7,
                support_count=1,
                decay_policy=DecayPolicy.MEDIUM,
                importance=0.6,
                created_at=quand,
                last_seen_at=quand,
                updated_at=quand,
            )
        )

    dream = AutoDream(
        llm=_LLMMouchard(),
        prefs_path=tmp_path / "prefs.md",
        sessions_dir=tmp_path / "sessions",
        memory_ingest=None,  # cas réel quand `ingest_deep_enabled` est faux
        mirror=None,
        kernel=kernel,
    )
    await dream._run_deep()

    actifs = kernel.list_facts_by_status(FactStatus.ACTIVE)
    assert len(actifs) == 1, f"doublons non absorbés : {[f.id for f in actifs]}"
    # Le survivant hérite du soutien cumulé — sinon la fusion perdrait
    # l'information qui justifiait le rang du fait.
    assert actifs[0].support_count == 3


class _IngestPiege:
    """Un ingest dont TOUT accès d'attribut inattendu explose.

    Première version du correctif : la fusion lisait `self._ingest.kernel`. Or
    `MemoryIngest` n'expose pas son kernel — l'`AttributeError` partait dans un
    `except Exception` journalisé en warning, et la fusion ne tournait jamais
    sans que rien n'échoue visiblement.

    Un test qui se contente d'affirmer `not hasattr(dream._ingest, "kernel")` sur
    un `_ingest` valant `None` est tautologique : c'est vrai de n'importe quel
    objet. Ce piège-ci, lui, transforme la rechute en échec bruyant.
    """

    def __getattr__(self, nom: str) -> object:
        raise AssertionError(f"la fusion ne doit pas passer par l'ingest (accès à .{nom})")

    async def ingest(self, **_: object) -> None:  # appelé légitimement par la synthèse
        return None


async def test_la_fusion_ne_passe_pas_par_l_ingestion(tmp_path: Path) -> None:
    """Régression de câblage, rendue falsifiable : si la fusion retourne lire le
    kernel à travers l'ingest, `_IngestPiege` lève et le test échoue."""
    kernel = MemoryKernel(tmp_path / "k.db")
    quand = datetime.now()
    for i in range(2):
        kernel.insert_fact(
            Fact(
                id=f"d{i}",
                subject="max",
                predicate="prefers",
                object="thé",
                category="preference",
                status=FactStatus.ACTIVE,
                confidence=0.7,
                support_count=1,
                decay_policy=DecayPolicy.MEDIUM,
                importance=0.6,
                created_at=quand,
                last_seen_at=quand,
                updated_at=quand,
            )
        )

    dream = AutoDream(
        llm=_LLMMouchard(),
        prefs_path=tmp_path / "prefs.md",
        sessions_dir=tmp_path / "sessions",
        memory_ingest=_IngestPiege(),  # type: ignore[arg-type]
        kernel=kernel,
    )
    await dream._run_deep()

    assert len(kernel.list_facts_by_status(FactStatus.ACTIVE)) == 1


async def test_la_fusion_est_tout_ou_rien(tmp_path: Path) -> None:
    """La fusion écrivait par connexions séparées, chacune avec son commit. Un
    plantage entre deux laissait le survivant porteur du total du groupe alors
    que des doublons restaient ACTIFS — et la passe suivante les re-sommait.

    Le support gonflait donc à chaque nuit, définitivement, et c'est précisément
    la clé qui désigne le survivant. Ici : deuxième passe sur une base déjà
    propre, le total doit être STABLE."""
    kernel = MemoryKernel(tmp_path / "k.db")
    quand = datetime.now()
    for i in range(3):
        kernel.insert_fact(
            Fact(
                id=f"s{i}",
                subject="max",
                predicate="uses",
                object="spotify",
                category="tool",
                status=FactStatus.ACTIVE,
                confidence=0.7,
                support_count=2,
                decay_policy=DecayPolicy.MEDIUM,
                importance=0.6,
                created_at=quand,
                last_seen_at=quand,
                updated_at=quand,
            )
        )

    assert kernel.fusionner_doublons() == 2
    premier = kernel.list_facts_by_status(FactStatus.ACTIVE)[0].support_count
    assert premier == 6

    # Idempotence : rien à absorber, donc rien à re-sommer.
    assert kernel.fusionner_doublons() == 0
    assert kernel.list_facts_by_status(FactStatus.ACTIVE)[0].support_count == premier
