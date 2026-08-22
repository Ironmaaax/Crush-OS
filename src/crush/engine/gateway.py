# Copyright (C) 2026 Max Ea
# Copyright (C) 2026 Maxime Song — modifications de ce fork
# This file is part of CRUSH-OS,   .


from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from loguru import logger

from crush.engine.agent import Agent
from crush.engine.background.notifications import NotificationQueue
from crush.engine.background.worker import BackgroundWorker
from crush.engine.faits import bloc_memoire
from crush.engine.llm_errors import friendly_llm_error
from crush.engine.router import RouteEnum, SpeedRouter
from crush.engine.session import Session, SessionManager
from crush.kernel.contracts import CrossSessionRecall, MemoryStore


def _fallback(exc: BaseException | None = None) -> str:
    if exc is not None:
        return friendly_llm_error(exc)
    return friendly_llm_error(RuntimeError("unknown"))


class Gateway:
    """Point d'entrée unique. Gère session, notifications, routing et agent.

    Phase C : le constructeur Gateway était DÉJÀ bien injecté en pré-C
    (5 dépendances reçues par paramètres typés). Le singleton historique
    `_tool_registry_instance` a été supprimé à l'étape 2 (b) — les call-sites
    (preset, http_skills) reçoivent maintenant le ToolRegistry via constructeur
    ou `request.app.state.container.tool_registry`.

    Flux double-passe pour les outils (CF) :
    1. Premier appel LLM streamé : détection du tag + ack text + capture tool_use.
    2. Exécution parallèle des outils (overlap avec TTS de l'ack).
    3. Second appel LLM (synthesize) : résultats injectés dans le contexte,
       LLM produit une réponse naturelle — pas de dump brut.
    L'utilisateur reçoit : ack streamé → synthèse streamée dans la même bulle.
    [BG] : le worker est soumis par le WebSocket après "done".
    """

    def __init__(
        self,
        session_manager: SessionManager,
        agent: Agent,
        notifications: NotificationQueue,
        worker: BackgroundWorker,
        recall: CrossSessionRecall | None = None,
        summarize_recall: bool = True,
        memory_store: MemoryStore | None = None,
    ) -> None:
        self._sessions = session_manager
        self._agent = agent
        self._notifications = notifications
        self._worker = worker
        self._recall = recall
        # À l'oral, on renonce au résumé LLM du rappel : il coûte un
        # aller-retour complet AVANT la vraie question.
        self._summarize_recall = summarize_recall
        # Le Memory Kernel, pour que les faits atteignent le prompt. Optionnel :
        # sans lui, le comportement d'avant est intact -- l'assistant se contente
        # de la synthese en prose, comme il le faisait jusqu'ici.
        self._memory_store = memory_store

    async def handle(
        self,
        message: str,
        session_id: str | None = None,
        stream: bool = True,
    ) -> tuple[Session, RouteEnum, str | AsyncIterator[str]]:
        session = self._sessions.get_or_create(session_id)
        logger.info("Gateway handle", session_id=str(session.id))

        pending = self._notifications.drain()
        notif_texts = [n.content for n in pending] if pending else None
        if notif_texts:
            logger.info("Injecting notifications", count=len(notif_texts))

        # Rappel cross-session uniquement au premier message de la session
        recall_summary: str | None = None
        if self._recall is not None and not session.messages:
            try:
                recall_summary = await self._recall.recall(
                    message, summarize=self._summarize_recall
                )
                if recall_summary:
                    logger.debug("CrossSessionRecall injected", chars=len(recall_summary))
            except Exception as e:
                logger.warning("CrossSessionRecall failed", error=str(e))

        # Ce que l'assistant sait de son utilisateur. Calcule ICI et non dans
        # `_build_system` : celui-ci est synchrone, et y mettre une lecture SQLite
        # installerait de l'I/O bloquante dans le constructeur de prompt. Meme
        # motif que `recall_summary` juste au-dessus.
        memoire = await self._bloc_memoire()

        try:
            raw_stream, tool_capture = self._agent.start_routing_stream(
                session=session,
                user_message=message,
                notifications=notif_texts,
                recall_summary=recall_summary,
                memoire=memoire,
            )

            route, text_stream = await SpeedRouter.extract_route(raw_stream)
            logger.debug("Route detected", route=route.value)

            agent = self._agent
            notifications = self._notifications

            async def _pipe() -> AsyncIterator[str]:
                tool_task: asyncio.Task | None = None
                ack_text = ""  # Accumule le texte streamé avant les outils

                async for chunk in text_stream:
                    ack_text += chunk
                    yield chunk
                    # Dès que _stream_capturing peuple capture (content_block_stop tool_use),
                    # on démarre la task outil — elle tourne pendant que la voice WS fait du TTS.
                    if tool_task is None and tool_capture is not None and tool_capture.calls:
                        tool_task = asyncio.create_task(
                            agent.execute_captured_tools(tool_capture),
                            name="cf-tools",
                        )

                # Fallback : LLM sans préambule texte
                if tool_task is None and tool_capture is not None and tool_capture.calls:
                    tool_task = asyncio.create_task(
                        agent.execute_captured_tools(tool_capture),
                        name="cf-tools",
                    )

                # Second appel LLM pour synthétiser les résultats — avant "done"
                if tool_task is not None:
                    try:
                        results = await tool_task
                        logger.debug("CF tools done", names=[n for _, n, _ in tool_capture.calls])
                        if ack_text.strip():
                            yield " "
                        # Les notifications et le rappel sont RETRANSMIS a la
                        # passe 2. Celle-ci ecrit la reponse que l'utilisateur
                        # lit vraiment -- la passe 1 n'a produit qu'un accuse de
                        # reception et des appels d'outils. Sans ce passage,
                        # l'assistant perdait son contexte des qu'un outil
                        # entrait en jeu.
                        synth_stream = agent.synthesize(
                            session,
                            ack_text,
                            tool_capture,
                            results,
                            notifications=notif_texts,
                            recall_summary=recall_summary,
                            memoire=memoire,
                        )
                        _, clean_synth = await SpeedRouter.extract_route(synth_stream)
                        async for chunk in clean_synth:
                            yield chunk
                    except Exception as e:
                        logger.opt(exception=True).error(
                            "CF tool or synthesize error",
                            error=type(e).__name__,
                            detail=str(e),
                        )
                        notifications.add(f"Outil échoué : {e}")
                        yield friendly_llm_error(e)

            return await self._finalize(session, route, _pipe(), stream)

        except Exception as e:
            logger.opt(exception=True).error(
                "Gateway error", error=type(e).__name__, detail=str(e), session_id=str(session.id)
            )
            return session, RouteEnum.INSTANT, _fallback(e)

    async def _bloc_memoire(self) -> str | None:
        """Les faits du Kernel, rendus pour le prompt. `None` si indisponible.

        Deporte en thread : la lecture est synchrone (~50 lignes SQLite, quelques
        microsecondes), mais `kernel.py` pose la regle que les appels
        asynchrones la delegent a un thread. On la respecte plutot que de parier
        sur la taille de la base.

        Un echec ne prive pas l'utilisateur de sa reponse : on rend `None` et
        l'assistant retombe sur la synthese en prose.
        """
        if self._memory_store is None:
            return None
        try:
            bloc = await asyncio.to_thread(bloc_memoire, self._memory_store)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Bloc memoire illisible", error=str(exc))
            return None
        if bloc:
            logger.debug("Faits injectes dans le prompt", chars=len(bloc))
        return bloc or None

    async def _finalize(
        self,
        session: Session,
        route: RouteEnum,
        response: str | AsyncIterator[str],
        stream: bool,
    ) -> tuple[Session, RouteEnum, str | AsyncIterator[str]]:
        """Si stream=False : draine la réponse, ajoute l'assistant en session."""
        if stream:
            return session, route, response
        if isinstance(response, str):
            text = response
        else:
            text = "".join([chunk async for chunk in response])
        session.add_message("assistant", text)
        return session, route, text
