# Copyright (C) 2026 Maxime Song

"""« Dans quel état est mon assistant, là, maintenant ? » — en un appel.

Ces réponses existaient, éparpillées sur cinq pages et six appels réseau. Depuis
un téléphone en 4G, c'est un écran qui se remplit par morceaux pendant deux
secondes — sur la page qu'on ouvre justement pour un coup d'œil.

Ce qui est défendu ici, dans l'ordre de ce qui coûterait le plus cher :

1. aucune mesure inventée — un champ qu'on ne peut pas constater vaut `null`, et
   non une valeur plausible qui ferait cesser de vérifier ailleurs ;
2. un seul comptage des maillons, celui de l'Écosystème : deux chiffres qui
   divergent enverraient chercher une panne au mauvais endroit ;
3. une source en panne n'emporte pas la page entière.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from crush.interfaces.api.apercu import router
from crush.providers.memory.kernel import MemoryKernel
from crush.providers.memory.mirror import MemoryMirror


class _Tracker:
    """Doublure calquée sur la FORME RÉELLE de `UsageTracker`.

    La première version rendait `{"cost": ...}` — la clé que j'avais supposée.
    Le vrai tracker rend `cost_usd`, et le code de lecture essayait alors une
    liste de clés plausibles avant de retomber sur 0.0 : la page affichait
    « 0,00 € » sur des données qu'elle n'avait pas su lire. Une doublure qui
    s'écarte de la vraie forme rend le test complice du bug.
    """

    def get_daily_totals(self, days: int = 7) -> list[dict]:
        return [
            {"date": "2026-08-2" + str(i), "day": "VEN", "cost_usd": v}
            for i, v in enumerate((0.11, 0.32, 0.08, 0.44, 0.19, 0.27, 0.05))
        ]

    def get_monthly_totals(self) -> dict:
        return {"month": "2026-08", "cost_usd": 4.21, "tracked_since": "2026-08-01"}


class _TrackerCasse:
    def get_daily_totals(self, days: int = 7) -> list[dict]:
        raise RuntimeError("fichiers de conso illisibles")

    def get_monthly_totals(self) -> dict:
        raise RuntimeError("idem")


class _Scheduler:
    def status(self) -> list[dict]:
        return [
            {
                "name": "Briefing matinal",
                "description": "Agenda a 9h00",
                "next_run": "2099-01-01T09:00:00",
                "interval": "quotidien",
            },
            {
                "name": "Boîte de réception",
                "description": "Relit les consignes",
                "next_run": None,
                "interval": "toutes les 10 min",
            },
        ]


@pytest.fixture
def app_pleine(tmp_path: Path) -> FastAPI:
    kernel = MemoryKernel(tmp_path / "k.db")
    app = FastAPI()
    app.include_router(router)
    app.state.memory_kernel = kernel
    app.state.memory_mirror = MemoryMirror(kernel, tmp_path / "mirror")
    app.state.tracker = _Tracker()
    app.state.scheduler = _Scheduler()
    app.state.presence = SimpleNamespace()
    return app


@pytest.fixture
def client(app_pleine: FastAPI) -> TestClient:
    return TestClient(app_pleine)


def _apercu(client: TestClient) -> dict:
    r = client.get("/api/apercu")
    assert r.status_code == 200
    return r.json()


# ── 1. Aucune mesure inventée ─────────────────────────────────────────────────


def test_sans_tracker_le_cout_est_nul_pas_zero(tmp_path: Path) -> None:
    """`null` et `0,00 €` ne disent pas la même chose : le premier avoue qu'on
    ne sait pas, le second affirme qu'on n'a rien dépensé."""
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as c:
        cout = c.get("/api/apercu").json()["cout"]

    assert cout["aujourd_hui"] is None
    assert cout["mois"] is None
    assert cout["serie"] == []


def test_un_tracker_en_panne_ne_devient_pas_un_cout_de_zero(tmp_path: Path) -> None:
    app = FastAPI()
    app.include_router(router)
    app.state.tracker = _TrackerCasse()
    with TestClient(app) as c:
        cout = c.get("/api/apercu").json()["cout"]

    assert cout["aujourd_hui"] is None


def test_sans_kernel_le_nombre_de_faits_est_nul(tmp_path: Path) -> None:
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as c:
        assert c.get("/api/apercu").json()["memoire"]["faits"] is None


def test_le_cout_du_jour_est_le_dernier_de_la_serie(client: TestClient) -> None:
    cout = _apercu(client)["cout"]

    assert cout["serie"] == [0.11, 0.32, 0.08, 0.44, 0.19, 0.27, 0.05]
    assert cout["aujourd_hui"] == 0.05
    assert cout["mois"] == 4.21


# ── 2. Un seul comptage ───────────────────────────────────────────────────────


def test_le_resume_des_maillons_vient_de_l_ecosysteme(client: TestClient) -> None:
    """Recompter ici produirait deux chiffres pour la même question."""
    eco = _apercu(client)["ecosysteme"]

    assert {"ok", "degrade", "absent"} <= set(eco)
    assert isinstance(eco["a_regarder"], list)


def test_seuls_les_maillons_hors_norme_sont_remontes(client: TestClient) -> None:
    """L'aperçu montre ce qui demande un regard, pas la liste complète — celle-là
    est l'Écosystème, et la redoubler ici en ferait une page de plus à lire."""
    a_regarder = _apercu(client)["ecosysteme"]["a_regarder"]

    assert all(m["etat"] != "ok" for m in a_regarder)
    assert len(a_regarder) <= 5


def test_chaque_maillon_remonte_porte_son_remede(client: TestClient) -> None:
    for m in _apercu(client)["ecosysteme"]["a_regarder"]:
        assert {"nom", "etat", "detail", "remede"} <= set(m)


# ── 3. Une source en panne n'emporte pas la page ──────────────────────────────


def test_une_presence_qui_leve_ne_casse_pas_l_apercu(tmp_path: Path) -> None:
    class _PresenceCasse:
        async def etat(self) -> object:
            raise RuntimeError("tailscale muet")

    app = FastAPI()
    app.include_router(router)
    app.state.presence = _PresenceCasse()
    with TestClient(app) as c:
        corps = c.get("/api/apercu").json()

    assert corps["presence"]["resume"] is None
    assert corps["cerveau"]["backend"], "le reste de la page doit tenir"


def test_un_scheduler_qui_leve_rend_une_liste_vide(tmp_path: Path) -> None:
    class _SchedulerCasse:
        def status(self) -> list[dict]:
            raise RuntimeError("indisponible")

    app = FastAPI()
    app.include_router(router)
    app.state.scheduler = _SchedulerCasse()
    with TestClient(app) as c:
        assert c.get("/api/apercu").json()["boucles"] == []


def test_l_apercu_repond_sur_une_installation_toute_nue() -> None:
    """Aucune dépendance n'est requise : la page doit s'ouvrir sur une machine
    neuve, sinon on ne peut pas s'en servir pour diagnostiquer un démarrage."""
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as c:
        r = c.get("/api/apercu")

    assert r.status_code == 200
    corps = r.json()
    assert corps["assistant"]
    assert corps["horodatage"]


# ── 4. Ce que la page lit vraiment ────────────────────────────────────────────


def test_les_boucles_gardent_leur_cadence_et_leur_echeance(client: TestClient) -> None:
    boucles = _apercu(client)["boucles"]

    assert [b["nom"] for b in boucles] == ["Briefing matinal", "Boîte de réception"]
    assert boucles[0]["quand"] == "2099-01-01T09:00:00"
    # Une boucle à intervalle n'a PAS d'échéance : inventer un « prochain
    # passage » exact serait faux, rien ne sait quand elle a tourné.
    assert boucles[1]["quand"] is None
    assert boucles[1]["cadence"] == "toutes les 10 min"


def test_les_canaux_sont_distingues_actifs_ou_dormants(client: TestClient) -> None:
    """Un canal éteint volontairement n'est pas une panne : la page le range
    ailleurs plutôt que de l'afficher en rouge."""
    relie = _apercu(client)["relie"]
    noms = {r["nom"] for r in relie}

    assert {"Telegram", "Discord", "Signal", "Slack", "Whatsapp"} <= noms
    assert all(isinstance(r["actif"], bool) for r in relie)


def test_les_heures_de_silence_sont_affichees(client: TestClient) -> None:
    silence = _apercu(client)["silence"]

    assert "plage" in silence
    assert isinstance(silence["urgent_passe"], bool)


def test_l_horodatage_permet_de_voir_qu_un_releve_est_perime(client: TestClient) -> None:
    """Sans lui, un onglet laissé ouvert depuis la veille affiche des chiffres
    d'hier sans que rien ne le signale."""
    from datetime import datetime

    corps = _apercu(client)
    lu = datetime.fromisoformat(corps["horodatage"])

    assert (datetime.now() - lu).total_seconds() < 60


# ── 5. Les deux bugs attrapés en regardant les vraies données ─────────────────


def test_un_canal_allume_n_est_pas_annonce_dormant(monkeypatch: pytest.MonkeyPatch) -> None:
    """LE bug observé : la page affichait Telegram « dormant » pendant que le bot
    répondait. `settings.telegram_enabled` N'EXISTE PAS, donc le `getattr(...,
    False)` renvoyait toujours faux — lire la configuration au lieu de la
    réalité, exactement ce que cette page se donne pour mission d'éviter.
    """
    monkeypatch.setenv("TELEGRAM_ENABLED", "true")
    app = FastAPI()
    app.include_router(router)

    with TestClient(app) as c:
        relie = c.get("/api/apercu").json()["relie"]

    telegram = next(r for r in relie if r["nom"] == "Telegram")
    assert telegram["actif"] is True


def test_un_canal_eteint_reste_dormant(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_ENABLED", "false")
    app = FastAPI()
    app.include_router(router)

    with TestClient(app) as c:
        relie = c.get("/api/apercu").json()["relie"]

    assert next(r for r in relie if r["nom"] == "Discord")["actif"] is False


def test_le_cout_est_rendu_dans_sa_vraie_devise(client: TestClient) -> None:
    """Le tracker compte en DOLLARS (`cost_usd`). La première version essayait
    une liste de clés plausibles, retombait sur 0.0, et affichait « 0,00 € » —
    un montant faux, dans la mauvaise monnaie, présenté comme un relevé."""
    cout = _apercu(client)["cout"]

    assert cout["devise"] == "USD"


class _TrackerSansSuivi:
    def get_daily_totals(self, days: int = 7) -> list[dict]:
        return [{"date": "2026-08-22", "cost_usd": 0.0}]

    def get_monthly_totals(self) -> dict:
        return {"cost_usd": 0.0, "tracked_since": None}


def test_rien_de_suivi_n_est_pas_zero_depense() -> None:
    """« 0,00 » laisserait croire à une dépense nulle alors que la mesure n'a
    simplement jamais commencé."""
    app = FastAPI()
    app.include_router(router)
    app.state.tracker = _TrackerSansSuivi()

    with TestClient(app) as c:
        cout = c.get("/api/apercu").json()["cout"]

    assert cout["aujourd_hui"] is None
    assert cout["serie"] == []


class _TrackerCleInconnue:
    def get_daily_totals(self, days: int = 7) -> list[dict]:
        return [{"date": "2026-08-22", "cout_en_roubles": 12.0}]

    def get_monthly_totals(self) -> dict:
        return {"cout_en_roubles": 12.0, "tracked_since": "2026-08-01"}


def test_une_cle_inconnue_ne_devient_pas_zero() -> None:
    """Si le tracker change de forme, la page doit se taire, pas inventer."""
    app = FastAPI()
    app.include_router(router)
    app.state.tracker = _TrackerCleInconnue()

    with TestClient(app) as c:
        cout = c.get("/api/apercu").json()["cout"]

    assert cout["mois"] is None
    assert cout["serie"] == []
