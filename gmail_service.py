# gmail_service.py
import base64
import json
import logging
import os
from email.mime.text import MIMEText
from typing import Dict, List, Optional

from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import Flow, InstalledAppFlow

# Scopes: ajustá según lo que necesitás (lectura/modificación + envío)
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly"
]

logger = logging.getLogger(__name__)


def load_credentials_from_token(token_info: Dict) -> Credentials:
    """
    token_info: objeto con access_token, refresh_token, token_uri, client_id, client_secret, scopes
    (En producción guardá y recuperá esto desde DB cifrado).
    """
    creds = Credentials(
        token=token_info["token"],
        refresh_token=token_info.get("refresh_token"),
        token_uri=token_info["token_uri"],
        client_id=token_info["client_id"],
        client_secret=token_info["client_secret"],
        scopes=token_info["scopes"],
    )

    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        logging.info("🔄 Token refrescado automáticamente.")
        # Guardar el nuevo token actualizado en USERS[email] o en tu DB
        token_info["token"] = creds.token

    return creds


def build_gmail_service(credentials: Credentials):
    return build("gmail", "v1", credentials=credentials, cache_discovery=False)


def create_watch(service, topic_name: str, label_ids: Optional[List[str]] = None) -> Dict:
    """
    Llama a users.watch para el usuario autenticado ('me').
    topic_name debe ser: projects/<PROJECT_ID>/topics/<TOPIC>
    """
    body = {"topicName": topic_name}
    if label_ids:
        body["labelIds"] = label_ids
        body["labelFilterBehavior"] = "INCLUDE"
    resp = service.users().watch(userId="me", body=body).execute()
    # resp contiene historyId y expiration
    return resp


def send_message(service, to: str, subject: str, body_text: str, sender: Optional[str] = None) -> Dict:
    """
    Envía mail con users.messages.send. Devuelve la respuesta de la API.
    """
    msg = MIMEText(body_text)
    if sender:
        msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    return service.users().messages().send(userId="me", body={"raw": raw}).execute()


# ---------- Helpers para recibir/parsear mensajes desde history / messages.get ----------
def _b64url_to_bytes(s: str) -> bytes:
    # Asegura padding correcto
    s2 = s + "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s2.encode())


def extract_headers(headers: List[Dict]) -> Dict[str, str]:
    d = {}
    for h in headers:
        d[h["name"].lower()] = h.get("value", "")
    return d


def extract_plain_body(payload: Dict) -> str:
    """
    Extrae texto plano recorriendo partes si es multipart.
    """
    mime = payload.get("mimeType", "")
    if mime == "text/plain" and payload.get("body", {}).get("data"):
        return _b64url_to_bytes(payload["body"]["data"]).decode(errors="ignore")

    # multipart
    for part in payload.get("parts", []) or []:
        text = extract_plain_body(part)
        if text:
            return text
    # fallback: snippet
    return ""


def parse_gmail_message(message_resource: Dict) -> Dict:
    """
    Normaliza: retorna {id, threadId, from, to, subject, body, timestamp, snippet}
    """
    payload = message_resource.get("payload", {})
    headers = extract_headers(payload.get("headers", []))
    body = extract_plain_body(payload) or message_resource.get("snippet", "")

    return {
        "id": message_resource.get("id"),
        "threadId": message_resource.get("threadId"),
        "from": headers.get("from", ""),
        "to": headers.get("to", ""),
        "subject": headers.get("subject", ""),
        "timestamp": headers.get("date", ""),  # podés normalizar fecha si querés
        "body": body,
        "snippet": message_resource.get("snippet", ""),
        "raw": message_resource,  # si querés persisitir TODO
    }


def fetch_messages_by_history(service, start_history_id: int) -> List[Dict]:
    """
    Llama a users.history.list con startHistoryId y devuelve los mensajes nuevos (trae ids y luego get).
    Importante: manejar excepciones cuando startHistoryId sea demasiado viejo -> hacer full sync.
    """
    result = []
    page_token = None
    # history.list devuelve items con history[].messagesAdded / messagesDeleted, etc.
    resp = service.users().history().list(userId="me", startHistoryId=start_history_id).execute()
    history = resp.get("history", [])
    message_ids = set()
    for h in history:
        for ma in h.get("messagesAdded", []):
            msg = ma.get("message", {})
            if msg.get("id"):
                message_ids.add(msg["id"])

    # ahora traemos cada mensaje completo
    for mid in message_ids:
        m = service.users().messages().get(userId="me", id=mid, format="full").execute()
        parsed = parse_gmail_message(m)
        result.append(parsed)
    return result
