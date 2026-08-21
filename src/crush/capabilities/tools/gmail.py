# Copyright (C) 2026 Max Ea
# Copyright (C) 2026 Maxime Song — modifications de ce fork
# This file is part of CRUSH-OS,   .


"""Gmail — lecture de la boîte et envoi de brouillons validés.

Le chargement des credentials OAuth et la traduction des erreurs Google sont
mutualisés avec Calendar : voir le socle en tête de `calendar.py`. Ce module
n'en décrit que ce qui lui est propre (scopes, API à activer, libellé).
"""

from __future__ import annotations

import asyncio
import base64
from email.mime.text import MIMEText
from pathlib import Path

import httpx
from loguru import logger

from crush.capabilities.tools.base import Tool, ToolResult
from crush.capabilities.tools.calendar import (
    ErreurGoogle,
    ServiceGoogle,
    borner_entier,
    expliquer_erreur_api,
    obtenir_jeton_google,
)

_GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"

SERVICE_GMAIL = ServiceGoogle(
    cle="gmail",
    libelle="Gmail",
    scopes=(
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.send",
    ),
    api="gmail.googleapis.com",
)


class GmailListTool(Tool):
    """Liste les emails Gmail non lus ou récents."""

    name = "list_emails"
    description = (
        "Liste les emails Gmail de l'utilisateur. "
        "Utilise cet outil quand l'utilisateur demande ses mails, sa boîte mail, "
        "ses messages non lus."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "max_results": {
                "type": "integer",
                "description": "Nombre d'emails à retourner (défaut : 10, max : 50)",
            },
            "unread_only": {
                "type": "boolean",
                "description": "Si true, retourne uniquement les non lus (défaut : true)",
            },
        },
        "required": [],
    }

    def __init__(self, credentials_path: Path, token_path: Path) -> None:
        self._creds = credentials_path
        self._token = token_path

    async def execute(
        self, max_results: object = 10, unread_only: bool = True, **_: object
    ) -> ToolResult:
        nombre = borner_entier(max_results, defaut=10, mini=1, maxi=50)

        try:
            creds = await obtenir_jeton_google(SERVICE_GMAIL, self._creds, self._token)
        except ErreurGoogle as e:
            logger.warning("Gmail indisponible", code=e.code)
            return ToolResult(content=str(e), is_error=True)

        label_ids = ["UNREAD", "INBOX"] if unread_only else ["INBOX"]
        params = {"labelIds": label_ids, "maxResults": nombre}

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                headers = {"Authorization": f"Bearer {creds.token}"}

                # 1. Lister les IDs
                r = await client.get(f"{_GMAIL_BASE}/messages", headers=headers, params=params)
                r.raise_for_status()
                messages = r.json().get("messages", [])

                if not messages:
                    label = "non lus" if unread_only else "récents"
                    return ToolResult(content=f"Aucun email {label}.")

                # 2. Fetch metadata en parallèle
                async def fetch_meta(msg_id: str) -> dict:
                    resp = await client.get(
                        f"{_GMAIL_BASE}/messages/{msg_id}",
                        headers=headers,
                        params=[
                            ("format", "metadata"),
                            ("metadataHeaders", "From"),
                            ("metadataHeaders", "Subject"),
                            ("metadataHeaders", "Date"),
                        ],
                    )
                    resp.raise_for_status()
                    return resp.json()

                metas = await asyncio.gather(*[fetch_meta(m["id"]) for m in messages])

            lignes = [_resumer_message(m) for m in metas]
            logger.debug("Gmail emails listed", count=len(lignes))
            return ToolResult(content="\n\n---\n\n".join(lignes))

        except Exception as e:
            logger.error(f"Gmail list error: {type(e).__name__}: {e}")
            return ToolResult(content=expliquer_erreur_api(SERVICE_GMAIL, e), is_error=True)


def _resumer_message(msg: dict) -> str:
    hdrs = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
    sender = hdrs.get("From", "?")
    subject = hdrs.get("Subject", "(sans sujet)")
    snippet = msg.get("snippet", "")[:120]
    return f"De : {sender}\nSujet : {subject}\nAperçu : {snippet}"


# ── Send email ────────────────────────────────────────────────────────────────


def _parse_draft(draft_content: str) -> tuple[str, str, str | None, str]:
    """Parse le format de brouillon structuré.

    Retourne (to, subject, thread_id, body).
    """
    lines = draft_content.strip().splitlines()
    headers: dict[str, str] = {}
    thread_id: str | None = None
    body_lines: list[str] = []
    in_body = False

    for line in lines:
        if in_body:
            body_lines.append(line)
            continue
        if line.strip() == "---":
            in_body = True
            continue
        if line.startswith("[THREAD_ID:"):
            thread_id = line[len("[THREAD_ID:") :].rstrip("]").strip()
            continue
        if ":" in line:
            key, _, val = line.partition(":")
            headers[key.strip().lower()] = val.strip()

    to = headers.get("à", headers.get("to", ""))
    subject = headers.get("sujet", headers.get("subject", ""))
    body = "\n".join(body_lines).strip()
    return to, subject, thread_id, body


async def send_gmail_draft(
    draft_content: str,
    credentials_path: Path,
    token_path: Path,
) -> str:
    """Parse draft_content et envoie via Gmail REST API. Retourne l'id du message envoyé.

    Lève ErreurGoogle (message porteur du remède) si l'accès n'est pas en place :
    l'appelant proactif le remonte tel quel à l'utilisateur.
    """
    to, subject, thread_id, body = _parse_draft(draft_content)
    if not to:
        raise ValueError("Destinataire (À:) introuvable dans le brouillon")

    creds = await obtenir_jeton_google(SERVICE_GMAIL, credentials_path, token_path)

    msg = MIMEText(body, "plain", "utf-8")
    msg["To"] = to
    msg["Subject"] = subject
    msg["From"] = "me"

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
    payload: dict = {"raw": raw}
    if thread_id:
        payload["threadId"] = thread_id

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{_GMAIL_BASE}/messages/send",
            headers={"Authorization": f"Bearer {creds.token}"},
            json=payload,
        )
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(expliquer_erreur_api(SERVICE_GMAIL, exc)) from exc

    sent_id: str = resp.json().get("id", "")
    logger.info("Gmail message sent", to=to, subject=subject, message_id=sent_id)
    return sent_id
