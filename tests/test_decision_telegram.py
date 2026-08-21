# Copyright (C) 2026 Maxime Song

"""Répondre à une initiative là où la question est arrivée.

La boucle était à moitié ouverte : le moteur poussait « Décision attendue » sur
le téléphone, et il fallait ouvrir le Command Center sur un ordinateur pour dire
oui. Une question qu'on ne peut pas trancher là où on la lit finit non tranchée.

Ce qui est gardé ici, dans l'ordre de ce qui coûterait le plus cher :

1. deux fois n'est pas deux fois — approuver un `DRAFT_RESPONSE` ENVOIE un
   e-mail, et un bouton Telegram reste tapotable après coup ;
2. un seul chemin d'exécution, quelle que soit la porte ;
3. un canal qui ne sait pas afficher de boutons reçoit quand même le message ;
4. une action qui échoue ne fait pas repousser la question comme si de rien.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from crush.engine.proactive.engine import ProactiveEngine
from crush.engine.proactive.schemas import InitiativeType
from crush.interfaces import decision
from crush.interfaces.channels.push import CanalDecidant, PushCanaux
from crush.kernel.schemas import ExecutionMode, Initiative, Priority


class _MagasinFactice:
    """Reproduit `InitiativeStore` sans toucher au disque."""

    def __init__(self, initiatives: dict[str, object] | None = None) -> None:
        self._par_id = initiatives or {}
        self.statuts: list[tuple[str, str]] = []

    def get_by_id(self, initiative_id: str, days: int = 7) -> object | None:
        return self._par_id.get(initiative_id)

    def update_status(self, initiative_id: str, status: str) -> None:
        self.statuts.append((initiative_id, status))
        cible = self._par_id.get(initiative_id)
        if cible is not None:
            cible.status = status  # type: ignore[attr-defined]


def _initiative(
    iid: str = "init_1",
    type_: object = InitiativeType.SUGGESTION,
    statut: str = "pending",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=iid,
        title="Relancer le devis Dupont",
        type=type_,
        status=statut,
        draft_content="Bonjour, je reviens vers vous…",
        mission_description="préparer la relance",
        action="préparer une relance",
    )


# ── 1. Deux fois n'est pas deux fois ──────────────────────────────────────────


async def test_une_initiative_deja_traitee_est_refusee() -> None:
    """LE garde-fou. Un bouton Telegram reste tapotable, et on retape volontiers
    quand rien ne semble se passer — ici, ça renverrait l'e-mail."""
    magasin = _MagasinFactice({"init_1": _initiative(statut="approved")})

    r = await decision.traiter("init_1", approuvee=True, store=magasin)

    assert r.deja_traitee
    assert not r.appliquee
    assert r.statut_precedent == "approved"
    assert magasin.statuts == [], "aucun statut ne doit être réécrit"


async def test_un_second_appui_n_envoie_pas_l_email_deux_fois(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    envois: list[str] = []

    async def _faux_envoi(**kwargs: object) -> str:
        envois.append(str(kwargs.get("draft_content", "")))
        return "msg_1"

    monkeypatch.setattr(decision, "send_gmail_draft", _faux_envoi)
    magasin = _MagasinFactice({"init_1": _initiative(type_=InitiativeType.DRAFT_RESPONSE)})

    await decision.traiter("init_1", approuvee=True, store=magasin)
    await decision.traiter("init_1", approuvee=True, store=magasin)

    assert len(envois) == 1


async def test_le_statut_est_pose_avant_l_action(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dans l'autre ordre, un envoi qui met quatre secondes laisse une fenêtre où
    l'initiative est encore en attente : un appui pendant ce temps la relance."""
    vu: list[str] = []
    magasin = _MagasinFactice({"init_1": _initiative(type_=InitiativeType.DRAFT_RESPONSE)})

    async def _faux_envoi(**_: object) -> str:
        vu.append("envoi")
        return "msg_1"

    monkeypatch.setattr(decision, "send_gmail_draft", _faux_envoi)

    await decision.traiter("init_1", approuvee=True, store=magasin)

    assert magasin.statuts == [("init_1", "approved")]
    assert vu == ["envoi"]


# ── 2. Un seul chemin ─────────────────────────────────────────────────────────


async def test_refuser_ecarte_sans_rien_executer(monkeypatch: pytest.MonkeyPatch) -> None:
    appels: list[str] = []
    monkeypatch.setattr(
        decision, "send_gmail_draft", lambda **_: appels.append("envoi")  # type: ignore[misc]
    )
    magasin = _MagasinFactice({"init_1": _initiative(type_=InitiativeType.DRAFT_RESPONSE)})

    r = await decision.traiter("init_1", approuvee=False, store=magasin)

    assert r.appliquee and not r.approuvee
    assert magasin.statuts == [("init_1", "rejected")]
    assert appels == []


async def test_une_initiative_disparue_ne_leve_pas() -> None:
    r = await decision.traiter("init_absente", approuvee=True, store=_MagasinFactice())

    assert not r.trouvee
    assert r.detail


async def test_une_tache_auto_lance_la_mission() -> None:
    lancees: list[str] = []

    class _Orchestrateur:
        async def create_and_run(self, mission: str) -> None:
            lancees.append(mission)

    magasin = _MagasinFactice({"init_1": _initiative(type_=InitiativeType.AUTO_TASK)})

    r = await decision.traiter(
        "init_1", approuvee=True, orchestrator=_Orchestrateur(), store=magasin
    )

    assert r.appliquee
    assert "Mission lancée" in r.detail


async def test_sans_orchestrateur_c_est_dit_pas_tu() -> None:
    magasin = _MagasinFactice({"init_1": _initiative(type_=InitiativeType.AUTO_TASK)})

    r = await decision.traiter("init_1", approuvee=True, orchestrator=None, store=magasin)

    assert r.appliquee
    assert r.erreur, "un échec silencieux laisserait croire la mission lancée"


# ── 3. Une action qui échoue ──────────────────────────────────────────────────


async def test_une_action_qui_echoue_reste_approuvee(monkeypatch: pytest.MonkeyPatch) -> None:
    """Repasser en attente ferait repousser la question comme si on n'avait pas
    répondu — alors que la décision, elle, a bien été prise."""

    async def _casse(**_: object) -> str:
        raise RuntimeError("Gmail refuse")

    monkeypatch.setattr(decision, "send_gmail_draft", _casse)
    magasin = _MagasinFactice({"init_1": _initiative(type_=InitiativeType.DRAFT_RESPONSE)})

    r = await decision.traiter("init_1", approuvee=True, store=magasin)

    assert magasin.statuts == [("init_1", "approved")]
    assert r.erreur and "Gmail refuse" in r.erreur
    assert "échoué" in r.detail


# ── 4. Le push : boutons quand c'est possible, texte sinon ────────────────────


class _CanalAvecBoutons:
    def __init__(self) -> None:
        self.platform = SimpleNamespace(value="telegram")
        self.decisions: list[tuple[str, str]] = []
        self.textes: list[str] = []

    async def send_message(self, text: str) -> None:
        self.textes.append(text)

    async def send_decision(self, text: str, initiative_id: str) -> None:
        self.decisions.append((text, initiative_id))


class _CanalTexteSeul:
    def __init__(self) -> None:
        self.platform = SimpleNamespace(value="signal")
        self.textes: list[str] = []

    async def send_message(self, text: str) -> None:
        self.textes.append(text)


def test_le_protocol_distingue_les_canaux_capables() -> None:
    assert isinstance(_CanalAvecBoutons(), CanalDecidant)
    assert not isinstance(_CanalTexteSeul(), CanalDecidant)


async def test_un_canal_sans_boutons_recoit_quand_meme_le_message() -> None:
    """Il perd le bouton, pas le message. Sinon configurer un second canal
    reviendrait à y perdre les questions."""
    avec, sans = _CanalAvecBoutons(), _CanalTexteSeul()
    push = PushCanaux([avec, sans])

    assert await push.pousser_decision("Décision attendue", "init_9")

    assert avec.decisions == [("Décision attendue", "init_9")]
    assert sans.textes == ["Décision attendue"]
    assert avec.textes == [], "le canal capable ne doit pas recevoir les deux formes"


async def test_une_simple_notification_ne_porte_pas_de_boutons() -> None:
    """Un bouton sur ce qui n'attend pas de réponse est un faux choix."""
    avec = _CanalAvecBoutons()
    push = PushCanaux([avec])

    await push.pousser("Il pleuvra demain")

    assert avec.textes == ["Il pleuvra demain"]
    assert avec.decisions == []


# ── 5. Le moteur choisit la bonne forme ───────────────────────────────────────


class _PushEspion:
    def __init__(self) -> None:
        self.textes: list[str] = []
        self.decisions: list[str] = []

    def disponible(self) -> bool:
        return True

    async def pousser(self, texte: str) -> bool:
        self.textes.append(texte)
        return True

    async def pousser_decision(self, texte: str, initiative_id: str) -> bool:
        self.decisions.append(initiative_id)
        return True


def _moteur(push: object) -> ProactiveEngine:
    moteur = ProactiveEngine(
        notification_queue=SimpleNamespace(add=lambda _m: None),  # type: ignore[arg-type]
        broadcast_event=lambda _e: None,
        builder=SimpleNamespace(),  # type: ignore[arg-type]
        generator=SimpleNamespace(),  # type: ignore[arg-type]
        store=SimpleNamespace(),  # type: ignore[arg-type]
    )
    moteur.brancher_push(push)  # type: ignore[arg-type]
    return moteur


def _init_moteur(mode: ExecutionMode) -> Initiative:
    return Initiative(
        id="init_42",
        type=InitiativeType.SUGGESTION,
        title="Relancer le devis",
        context="sans réponse depuis 6 jours",
        reasoning="tu relances d'habitude",
        action="préparer une relance",
        priority=Priority.HIGH,
        execution_mode=mode,
        created_at=datetime.now(),
    )


async def test_une_decision_attendue_part_avec_son_identifiant() -> None:
    """Sans l'identifiant, le bouton ne saurait pas quoi approuver."""
    push = _PushEspion()

    await _moteur(push)._dispatch(_init_moteur(ExecutionMode.VALIDATE))

    assert push.decisions == ["init_42"]
    assert push.textes == []


async def test_une_notification_part_en_texte_simple() -> None:
    push = _PushEspion()

    await _moteur(push)._dispatch(_init_moteur(ExecutionMode.NOTIFY))

    assert push.textes and push.decisions == []
