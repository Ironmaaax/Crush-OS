# Copyright (C) 2026 Maxime Song

"""Savoir s'il est joignable — et ne rien prétendre savoir de plus.

La tentation était d'appeler ça « présence ». Ç'aurait été faux : ce que le
tailnet sait dire, c'est qu'un appareil est CONNECTÉ, pas où il se trouve. Un
téléphone en ligne l'est aussi bien dans le salon que dans un train.

Cette distinction n'est pas un scrupule de vocabulaire. Cette mesure sert à
décider s'il faut interrompre quelqu'un, et une supposition fausse dans ce
sens-là réveille les gens. D'où les tests ci-dessous, qui défendent d'abord le
droit de dire « je ne sais pas ».
"""

from __future__ import annotations

from datetime import time
from types import SimpleNamespace

import pytest

from crush.engine.proactive.engine import _dans_le_silence
from crush.providers.presence import EtatPresence, Presence

_JSON_TAILSCALE = """
{
  "BackendState": "Running",
  "Self": {"HostName": "JARVIS"},
  "Peer": {
    "a": {"HostName": "Max-Pc", "OS": "windows",
          "Online": true, "LastSeen": "0001-01-01T00:00:00Z"},
    "b": {"HostName": "MAX Ultra", "OS": "android",
          "Online": false, "LastSeen": "2026-08-20T20:31:54Z"},
    "c": {"HostName": "Max-Book", "OS": "windows",
          "Online": false, "LastSeen": "2026-08-21T22:10:00Z"}
  }
}
"""


class _Registre:
    def __init__(self, agents: list[object] | None = None) -> None:
        self._agents = agents or []

    def list_agents(self) -> list[object]:
        return list(self._agents)


class _RegistreCasse:
    def list_agents(self) -> list[object]:
        raise RuntimeError("registre indisponible")


def _faux_tailscale(
    monkeypatch: pytest.MonkeyPatch, sortie: bytes = b"", code: int = 0, absent: bool = False
) -> None:
    async def _lancer(*_args: str, **_kwargs: object) -> object:
        if absent:
            raise FileNotFoundError("tailscale")

        class _Processus:
            returncode = code

            async def communicate(self) -> tuple[bytes, bytes]:
                return sortie, b""

            def kill(self) -> None:  # pragma: no cover
                pass

        return _Processus()

    monkeypatch.setattr("asyncio.create_subprocess_exec", _lancer)


# ── Le droit de dire « je ne sais pas » ───────────────────────────────────────


async def test_sans_tailscale_la_joignabilite_est_inconnue_pas_fausse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`None` et `False` mènent à des décisions opposées.

    « Je ne sais pas s'il est joignable » invite à essayer ; « il n'est pas
    joignable » invite à se taire. Les confondre revient à se taire quand on
    devrait parler.
    """
    _faux_tailscale(monkeypatch, absent=True)

    etat = await Presence().etat()

    assert etat.joignable is None
    assert etat.erreur and "introuvable" in etat.erreur


async def test_la_maison_reste_inconnue_tant_que_rien_ne_la_mesure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LE test qui empêche la dérive. Un appareil en ligne ne dit RIEN du lieu."""
    _faux_tailscale(monkeypatch, sortie=_JSON_TAILSCALE.encode())

    etat = await Presence().etat()

    assert etat.joignable is True
    assert etat.a_la_maison is None, "être en ligne n'est pas être chez soi"


async def test_une_sortie_illisible_ne_devient_pas_une_absence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _faux_tailscale(monkeypatch, sortie=b"ceci n'est pas du json")

    etat = await Presence().etat()

    assert etat.joignable is None
    assert etat.erreur


async def test_un_code_de_retour_non_nul_est_rapporte(monkeypatch: pytest.MonkeyPatch) -> None:
    _faux_tailscale(monkeypatch, sortie=b"", code=1)

    etat = await Presence().etat()

    assert etat.joignable is None
    assert etat.erreur and "code 1" in etat.erreur


# ── Ce qu'on sait vraiment ────────────────────────────────────────────────────


async def test_un_appareil_en_ligne_rend_joignable(monkeypatch: pytest.MonkeyPatch) -> None:
    _faux_tailscale(monkeypatch, sortie=_JSON_TAILSCALE.encode())

    etat = await Presence().etat()

    assert etat.joignable is True
    assert [a["nom"] for a in etat.appareils if a["en_ligne"]] == ["Max-Pc"]


async def test_aucun_appareil_en_ligne_est_une_reponse_pas_une_erreur(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _faux_tailscale(monkeypatch, sortie=b'{"Peer": {"a": {"HostName": "X", "Online": false}}}')

    etat = await Presence().etat()

    assert etat.joignable is False
    assert etat.erreur is None


async def test_l_agent_pc_connecte_prouve_la_joignabilite(monkeypatch: pytest.MonkeyPatch) -> None:
    """Même si le tailnet ne répond pas : la connexion de l'agent passe par lui."""
    _faux_tailscale(monkeypatch, absent=True)

    etat = await Presence(registre_agents=_Registre([object()])).etat()

    assert etat.au_poste is True
    assert etat.joignable is True


async def test_un_registre_casse_n_est_pas_une_absence(monkeypatch: pytest.MonkeyPatch) -> None:
    _faux_tailscale(monkeypatch, sortie=_JSON_TAILSCALE.encode())

    etat = await Presence(registre_agents=_RegistreCasse()).etat()

    assert etat.au_poste is False
    assert etat.joignable is True, "le tailnet répond, lui"


async def test_la_date_bidon_de_tailscale_n_est_pas_affichee(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tailscale renvoie « 0001-01-01 » pour un pair EN LIGNE : l'afficher tel
    quel donnerait « vu il y a deux mille ans »."""
    _faux_tailscale(monkeypatch, sortie=_JSON_TAILSCALE.encode())

    etat = await Presence().etat()

    enligne = next(a for a in etat.appareils if a["nom"] == "Max-Pc")
    assert enligne["vu_le"] == ""
    hors = next(a for a in etat.appareils if a["nom"] == "Max-Book")
    assert hors["vu_le"] == "2026-08-21T22:10:00"


async def test_les_appareils_en_ligne_sont_en_tete(monkeypatch: pytest.MonkeyPatch) -> None:
    _faux_tailscale(monkeypatch, sortie=_JSON_TAILSCALE.encode())

    etat = await Presence().etat()

    assert etat.appareils[0]["en_ligne"] is True


async def test_la_mesure_est_gardee_quelques_secondes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Un sous-processus par question posée serait payé à chaque cycle proactif."""
    appels: list[int] = []

    async def _lancer(*_a: str, **_k: object) -> object:
        appels.append(1)

        class _P:
            returncode = 0

            async def communicate(self) -> tuple[bytes, bytes]:
                return _JSON_TAILSCALE.encode(), b""

            def kill(self) -> None:  # pragma: no cover
                pass

        return _P()

    monkeypatch.setattr("asyncio.create_subprocess_exec", _lancer)
    p = Presence()

    await p.etat()
    await p.etat()
    await p.etat(forcer=True)

    assert len(appels) == 2, "deux mesures : la première, puis celle forcée"


# ── Le résumé donné au modèle ─────────────────────────────────────────────────


def test_le_resume_ne_pretend_rien_quand_on_ne_sait_pas() -> None:
    assert "inconnue" in EtatPresence(joignable=None).resume()


def test_le_resume_dit_le_poste_quand_il_est_su() -> None:
    r = EtatPresence(joignable=True, au_poste=True).resume()
    assert "joignable" in r and "poste" in r


def test_le_resume_ne_parle_pas_du_domicile_sans_mesure() -> None:
    r = EtatPresence(joignable=True, a_la_maison=None).resume()
    assert "maison" not in r and "absent du domicile" not in r


# ── Les heures de silence ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("heure", "plage", "attendu"),
    [
        (time(3, 0), "23:00-07:00", True),
        (time(23, 30), "23:00-07:00", True),
        (time(23, 0), "23:00-07:00", True),
        (time(7, 0), "23:00-07:00", False),
        (time(14, 0), "23:00-07:00", False),
        (time(6, 59), "23:00-07:00", True),
        # Plage qui n'enjambe pas minuit
        (time(14, 0), "13:00-15:00", True),
        (time(16, 0), "13:00-15:00", False),
    ],
)
def test_la_plage_de_silence_enjambe_minuit(heure: time, plage: str, attendu: bool) -> None:
    assert _dans_le_silence(heure, plage) is attendu


@pytest.mark.parametrize("plage", ["", "   ", "n'importe quoi", "25:00-07:00", "23h-7h", "23:00"])
def test_une_plage_illisible_ne_baillonne_rien(plage: str) -> None:
    """Le sens de l'erreur est inversé par rapport au seuil de priorité — et pour
    la même raison. Là, une valeur illisible POUSSE parce que le bruit se
    remarque et se corrige. Ici, elle ne doit pas faire taire l'assistant : une
    faute de frappe dans un réglage ne doit jamais faire perdre des messages.
    """
    assert _dans_le_silence(time(3, 0), plage) is False


def test_une_plage_vide_de_duree_nulle_ne_silence_pas() -> None:
    assert _dans_le_silence(time(12, 0), "12:00-12:00") is False


# ── Le contexte donné au générateur d'initiatives ─────────────────────────────


async def test_le_contexte_reste_muet_si_la_mesure_a_echoue() -> None:
    """Le générateur lirait « absent » comme un fait, et proposerait de différer
    des choses sur la base d'une commande qui n'a pas répondu."""
    from crush.engine.proactive.context_builder import ContextBuilder

    class _PresenceMuette:
        async def etat(self) -> EtatPresence:
            return EtatPresence(joignable=None, erreur="tailscale introuvable")

    builder = ContextBuilder(
        calendar_tool=SimpleNamespace(),  # type: ignore[arg-type]
        notion_tool=SimpleNamespace(),  # type: ignore[arg-type]
        presence=_PresenceMuette(),
    )

    assert await builder._resumer_presence() == ""


async def test_le_contexte_porte_la_presence_quand_elle_est_sue() -> None:
    from crush.engine.proactive.context_builder import ContextBuilder

    class _PresenceSure:
        async def etat(self) -> EtatPresence:
            return EtatPresence(joignable=True, au_poste=True)

    builder = ContextBuilder(
        calendar_tool=SimpleNamespace(),  # type: ignore[arg-type]
        notion_tool=SimpleNamespace(),  # type: ignore[arg-type]
        presence=_PresenceSure(),
    )

    assert "poste" in await builder._resumer_presence()


async def test_sans_presence_injectee_le_contexte_fonctionne() -> None:
    """L'assistant doit tourner sur une machine sans tailnet."""
    from crush.engine.proactive.context_builder import ContextBuilder

    builder = ContextBuilder(
        calendar_tool=SimpleNamespace(),  # type: ignore[arg-type]
        notion_tool=SimpleNamespace(),  # type: ignore[arg-type]
    )

    assert await builder._resumer_presence() == ""


# ── Le moteur respecte la plage ────────────────────────────────────────────────


class _PushEspion:
    def __init__(self) -> None:
        self.envois: list[str] = []

    def disponible(self) -> bool:
        return True

    async def pousser(self, texte: str) -> bool:
        self.envois.append(texte)
        return True

    async def pousser_decision(self, texte: str, initiative_id: str) -> bool:
        self.envois.append(texte)
        return True


def _moteur_avec(push: object) -> object:
    from crush.engine.proactive.engine import ProactiveEngine

    moteur = ProactiveEngine(
        notification_queue=SimpleNamespace(add=lambda _m: None),
        broadcast_event=lambda _e: None,
        builder=SimpleNamespace(),
        generator=SimpleNamespace(),
        store=SimpleNamespace(),
    )
    moteur.brancher_push(push)
    return moteur


def _initiative(mode: object, priorite: object) -> object:
    from datetime import datetime as _dt

    from crush.engine.proactive.schemas import InitiativeType
    from crush.kernel.schemas import Initiative

    return Initiative(
        id="init_nuit",
        type=InitiativeType.SUGGESTION,
        title="Ranger le garage",
        context="rien d'urgent",
        reasoning="tu l'avais dit",
        action="prevoir un creneau",
        priority=priorite,
        execution_mode=mode,
        created_at=_dt.now(),
    )


async def test_une_suggestion_nocturne_attend_le_matin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Elle n'est pas perdue : la file de notifications la servira a la prochaine
    conversation. Elle ne SONNE simplement pas a trois heures du matin."""
    from crush.kernel.schemas import ExecutionMode, Priority

    push = _PushEspion()
    monkeypatch.setattr(
        "crush.engine.proactive.engine.settings.push_heures_silence", "00:00-23:59", raising=False
    )

    await _moteur_avec(push)._dispatch(_initiative(ExecutionMode.NOTIFY, Priority.HIGH))

    assert push.envois == []


async def test_une_decision_urgente_passe_malgre_la_nuit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sinon RIEN ne part la nuit, y compris ce qui ne peut pas attendre le matin."""
    from crush.kernel.schemas import ExecutionMode, Priority

    push = _PushEspion()
    monkeypatch.setattr(
        "crush.engine.proactive.engine.settings.push_heures_silence", "00:00-23:59", raising=False
    )
    monkeypatch.setattr(
        "crush.engine.proactive.engine.settings.push_silence_laisse_passer_urgent",
        True,
        raising=False,
    )

    await _moteur_avec(push)._dispatch(_initiative(ExecutionMode.VALIDATE, Priority.HIGH))

    assert push.envois, "une decision urgente doit passer"


async def test_le_reglage_peut_tout_faire_taire(monkeypatch: pytest.MonkeyPatch) -> None:
    from crush.kernel.schemas import ExecutionMode, Priority

    push = _PushEspion()
    monkeypatch.setattr(
        "crush.engine.proactive.engine.settings.push_heures_silence", "00:00-23:59", raising=False
    )
    monkeypatch.setattr(
        "crush.engine.proactive.engine.settings.push_silence_laisse_passer_urgent",
        False,
        raising=False,
    )

    await _moteur_avec(push)._dispatch(_initiative(ExecutionMode.VALIDATE, Priority.HIGH))

    assert push.envois == []


async def test_hors_plage_tout_repart(monkeypatch: pytest.MonkeyPatch) -> None:
    from crush.kernel.schemas import ExecutionMode, Priority

    push = _PushEspion()
    monkeypatch.setattr(
        "crush.engine.proactive.engine.settings.push_heures_silence", "", raising=False
    )

    await _moteur_avec(push)._dispatch(_initiative(ExecutionMode.NOTIFY, Priority.HIGH))

    assert push.envois
