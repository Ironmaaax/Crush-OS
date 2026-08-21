# Copyright (C) 2026 Maxime Song

"""Push des initiatives proactives — le fil qui manquait entre le moteur et toi.

Le moteur produisait des initiatives `VALIDATE` — celles qui demandent une
DÉCISION — en les diffusant aux seuls clients WebSocket connectés. Sur une
machine allumée en permanence et consultée depuis un téléphone, elles dormaient
dans le Command Center jusqu'à ce qu'on pense à l'ouvrir.

Ce qui est gardé ici :

- une décision attendue part TOUJOURS ;
- une simple notification ne part qu'au-dessus du seuil de priorité, parce
  qu'elle a déjà un chemin qui marche et que tout pousser revient à n'être plus
  lu du tout ;
- un canal muet n'empêche pas les autres, et ne casse pas le cycle ;
- sans canal branché, le comportement d'avant est intact — l'assistant doit
  tourner sur une machine sans Telegram.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from crush.engine.proactive.engine import ProactiveEngine, _atteint_le_seuil
from crush.interfaces.api.ecosysteme import _initiatives_poussees
from crush.interfaces.channels import push as module_push
from crush.interfaces.channels.push import CanalPoussant, DernierEnvoi, PushCanaux
from crush.kernel.schemas import (
    ExecutionMode,
    Initiative,
    InitiativeType,
    Priority,
)


@pytest.fixture(autouse=True)
def _reinitialise_le_dernier_envoi() -> object:
    """`_dernier` est un module-global : sans remise a zero, l'ordre des tests compte."""
    module_push._dernier = None
    yield
    module_push._dernier = None


def _initiative(mode: ExecutionMode, priorite: Priority = Priority.HIGH) -> Initiative:
    return Initiative(
        id="init_1",
        type=InitiativeType.SUGGESTION,
        title="Relancer le devis Dupont",
        context="mail sans réponse depuis 6 jours",
        reasoning="tu relances d'habitude au bout d'une semaine",
        action="préparer une relance et te la soumettre",
        priority=priorite,
        execution_mode=mode,
        created_at=datetime.now(),
    )


class _CanalOk:
    """Canal qui sait écrire spontanément."""

    def __init__(self, nom: str = "telegram") -> None:
        self.platform = SimpleNamespace(value=nom)
        self.recus: list[str] = []

    async def send_message(self, text: str) -> None:
        self.recus.append(text)


class _CanalMuet:
    """Canal qui échoue — bot arrêté, réseau coupé."""

    def __init__(self) -> None:
        self.platform = SimpleNamespace(value="discord")

    async def send_message(self, text: str) -> None:
        raise RuntimeError("bot non démarré")


class _CanalSansPush:
    """Canal qui ne sait que répondre : un webhook n'a pas de destinataire."""

    def __init__(self) -> None:
        self.platform = SimpleNamespace(value="whatsapp")

    async def send(self, reply: str, target: object) -> None:  # pragma: no cover
        raise AssertionError("send() ne doit pas servir au push")


# ── Le trieur de canaux ──────────────────────────────────────────────────────


def test_seuls_les_canaux_capables_sont_retenus() -> None:
    """`send_message` n'est pas dans ChannelAdapter : tous ne savent pas le faire."""
    push = PushCanaux([_CanalOk(), _CanalSansPush()])
    assert push.disponible()
    assert len(push._canaux) == 1


def test_sans_canal_capable_le_push_est_indisponible() -> None:
    push = PushCanaux([_CanalSansPush()])
    assert not push.disponible()


def test_le_protocol_reconnait_la_capacite() -> None:
    assert isinstance(_CanalOk(), CanalPoussant)
    assert not isinstance(_CanalSansPush(), CanalPoussant)


async def test_pousse_sur_tous_les_canaux_pas_seulement_le_premier() -> None:
    """Plusieurs canaux configurés, c'est le souhait d'être joint sur plusieurs."""
    a, b = _CanalOk("telegram"), _CanalOk("signal")
    push = PushCanaux([a, b])

    assert await push.pousser("décision attendue")

    assert a.recus == ["décision attendue"]
    assert b.recus == ["décision attendue"]


async def test_un_canal_muet_ne_prive_pas_les_autres() -> None:
    bon = _CanalOk()
    push = PushCanaux([_CanalMuet(), bon])

    assert await push.pousser("message")  # vrai : au moins un a abouti

    assert bon.recus == ["message"]


async def test_tous_muets_renvoie_faux_sans_lever() -> None:
    """Le cycle proactif ne doit pas mourir parce qu'un bot est arrêté."""
    push = PushCanaux([_CanalMuet()])
    assert await push.pousser("message") is False


# ── Le seuil de priorité ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("priorite", "seuil", "attendu"),
    [
        (Priority.HIGH, "high", True),
        (Priority.MEDIUM, "high", False),
        (Priority.LOW, "high", False),
        (Priority.MEDIUM, "medium", True),
        (Priority.LOW, "low", True),
        (Priority.HIGH, "  HIGH  ", True),  # tolérance de saisie
    ],
)
def test_le_seuil_filtre_ce_qui_interrompt(
    priorite: Priority, seuil: str, attendu: bool
) -> None:
    assert _atteint_le_seuil(priorite, seuil) is attendu


def test_un_seuil_illisible_pousse_plutot_que_de_taire() -> None:
    """Être bruyant sur une mauvaise config vaut mieux que perdre une décision.

    Le bruit se remarque et se corrige ; le silence, non.
    """
    assert _atteint_le_seuil(Priority.LOW, "n_importe_quoi") is True


# ── Le moteur ────────────────────────────────────────────────────────────────


def _moteur(push: object | None = None) -> ProactiveEngine:
    moteur = ProactiveEngine(
        notification_queue=SimpleNamespace(add=lambda _m: None),  # type: ignore[arg-type]
        broadcast_event=lambda _e: None,
        builder=SimpleNamespace(),  # type: ignore[arg-type]
        generator=SimpleNamespace(),  # type: ignore[arg-type]
        store=SimpleNamespace(),  # type: ignore[arg-type]
    )
    if push is not None:
        moteur.brancher_push(push)  # type: ignore[arg-type]
    return moteur


async def test_une_decision_attendue_part_toujours() -> None:
    """C'est LE cas qui se perdait : diffusé aux seuls clients connectés."""
    canal = _CanalOk()
    moteur = _moteur(PushCanaux([canal]))

    await moteur._dispatch(_initiative(ExecutionMode.VALIDATE))

    assert canal.recus, "une initiative VALIDATE n'a pas été poussée"
    assert "Relancer le devis Dupont" in canal.recus[0]
    assert "Décision attendue" in canal.recus[0]


async def test_une_notification_haute_est_poussee(monkeypatch: pytest.MonkeyPatch) -> None:
    canal = _CanalOk()
    moteur = _moteur(PushCanaux([canal]))

    await moteur._dispatch(_initiative(ExecutionMode.NOTIFY, Priority.HIGH))

    assert canal.recus


async def test_une_notification_basse_reste_dans_la_file() -> None:
    """Elle a déjà un chemin qui marche : la prochaine conversation."""
    canal = _CanalOk()
    moteur = _moteur(PushCanaux([canal]))

    await moteur._dispatch(_initiative(ExecutionMode.NOTIFY, Priority.LOW))

    assert canal.recus == []


async def test_le_reglage_desactive_coupe_le_push(monkeypatch: pytest.MonkeyPatch) -> None:
    canal = _CanalOk()
    moteur = _moteur(PushCanaux([canal]))
    monkeypatch.setattr(
        "crush.engine.proactive.engine.settings.push_proactive_enabled", False, raising=False
    )

    await moteur._dispatch(_initiative(ExecutionMode.VALIDATE))

    assert canal.recus == []


async def test_sans_push_branche_le_comportement_d_avant_est_intact() -> None:
    """L'assistant doit tourner sur une machine sans aucun canal configuré."""
    recu: list[str] = []
    moteur = ProactiveEngine(
        notification_queue=SimpleNamespace(add=recu.append),  # type: ignore[arg-type]
        broadcast_event=lambda _e: None,
        builder=SimpleNamespace(),  # type: ignore[arg-type]
        generator=SimpleNamespace(),  # type: ignore[arg-type]
        store=SimpleNamespace(),  # type: ignore[arg-type]
    )

    await moteur._dispatch(_initiative(ExecutionMode.NOTIFY))

    assert recu, "la file de notifications doit rester servie"


# ── Le voyant ne doit pas mentir ─────────────────────────────────────────────


async def test_le_dernier_envoi_reussi_est_enregistre() -> None:
    push = PushCanaux([_CanalOk()])
    assert module_push.dernier_envoi() is None

    await push.pousser("x")

    dernier = module_push.dernier_envoi()
    assert dernier is not None and dernier.reussi
    assert dernier.erreur is None


async def test_le_dernier_envoi_retient_la_raison_de_l_echec() -> None:
    push = PushCanaux([_CanalMuet()])

    await push.pousser("x")

    dernier = module_push.dernier_envoi()
    assert dernier is not None and not dernier.reussi
    assert dernier.erreur and "bot non démarré" in dernier.erreur


def test_ecosysteme_degrade_quand_le_dernier_envoi_a_echoue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Le cas reel : token valide, identifiant correct, et « chat not found ».

    Un bot Telegram ne peut pas engager la conversation. La configuration seule
    aurait affiche un voyant vert pendant que chaque envoi echouait.
    """
    monkeypatch.setenv("TELEGRAM_ENABLED", "true")
    module_push._dernier = DernierEnvoi(
        horodatage=datetime.now(), reussi=False, erreur="telegram : chat not found"
    )

    maillon = _initiatives_poussees()

    assert maillon["etat"] == "degrade"
    assert "chat not found" in maillon["detail"]
    assert maillon["remede"], "un etat degrade sans remede ne vaut rien"


def test_ecosysteme_vert_seulement_apres_un_envoi_reussi(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TELEGRAM_ENABLED", "true")
    module_push._dernier = DernierEnvoi(horodatage=datetime.now(), reussi=True)

    maillon = _initiatives_poussees()

    assert maillon["etat"] == "ok"
    assert "réussi" in maillon["detail"]
    assert maillon["remede"] == ""


def test_ecosysteme_ne_pretend_rien_avant_le_premier_envoi(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ni vert menteur, ni rouge injustifie : on dit qu'on n'a pas encore essaye."""
    monkeypatch.setenv("TELEGRAM_ENABLED", "true")

    maillon = _initiatives_poussees()

    assert maillon["etat"] == "ok"
    assert "aucun envoi encore" in maillon["detail"]
