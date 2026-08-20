# Copyright (C) 2026 Max Ea
# This file is part of CRUSH-OS,   .
 

from __future__ import annotations

from crush.engine.session import Session, SessionManager


def test_session_add_message() -> None:
    session = Session()
    session.add_message("user", "Bonjour")
    session.add_message("assistant", "Salut chef.")
    assert len(session.messages) == 2
    assert session.messages[0] == {"role": "user", "content": "Bonjour"}


def test_session_manager_create() -> None:
    mgr = SessionManager()
    session = mgr.get_or_create()
    assert session is not None
    assert str(session.id) in mgr.list_ids()


def test_session_manager_get_existing() -> None:
    mgr = SessionManager()
    s1 = mgr.get_or_create()
    s2 = mgr.get_or_create(str(s1.id))
    assert s1.id == s2.id


def test_session_manager_get_none() -> None:
    mgr = SessionManager()
    result = mgr.get("non-existent-id")
    assert result is None


def test_session_manager_unknown_id_creates_new() -> None:
    mgr = SessionManager()
    s1 = mgr.get_or_create("unknown-uuid")
    s2 = mgr.get_or_create("unknown-uuid")
    # "unknown-uuid" n'est pas dans le registre, donc deux sessions distinctes
    # (get_or_create crée une nouvelle si l'id n'est pas connu)
    assert s1.id != s2.id
