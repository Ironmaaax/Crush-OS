

# ── Signalement d'une panne d'accès Google ───────────────────────────────────
#
# Une autorisation révoquée — jeton expiré, mot de passe changé — arrêtait les
# rappels d'agenda en silence. Le planificateur journalisait en DEBUG et
# passait à autre chose : la panne se découvrait des semaines plus tard, en se
# demandant pourquoi les rappels avaient cessé.


class _FileFactice:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def add(self, message: str) -> None:
        self.messages.append(message)


class _ProactifFactice:
    def __init__(self) -> None:
        self.diffusions: list[str] = []

    def broadcast(self, message: str) -> None:
        self.diffusions.append(message)


def _planificateur(file: object | None) -> object:
    """Planificateur nu : on n'exerce que le signalement, pas les boucles."""
    from crush.engine.background.scheduler import Scheduler

    s = Scheduler.__new__(Scheduler)
    s._notifications = file  # type: ignore[attr-defined]
    s._proactive = _ProactifFactice()  # type: ignore[attr-defined]
    return s


def test_la_panne_passe_par_les_notifications() -> None:
    """Le bon canal : la file est drainée dans la prochaine conversation."""
    file = _FileFactice()
    planificateur = _planificateur(file)

    planificateur._signaler("agenda inaccessible")  # type: ignore[attr-defined]

    assert file.messages == ["agenda inaccessible"]
    assert planificateur._proactive.diffusions == [], (  # type: ignore[attr-defined]
        "la diffusion n'atteint que les clients connectés à l'instant"
    )


def test_sans_file_le_message_est_quand_meme_emis() -> None:
    """Mieux vaut un canal imparfait que le silence."""
    planificateur = _planificateur(None)

    planificateur._signaler("agenda inaccessible")  # type: ignore[attr-defined]

    assert planificateur._proactive.diffusions == ["agenda inaccessible"]  # type: ignore[attr-defined]
