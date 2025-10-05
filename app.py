# app.py
import os
import json
import logging
import base64

from fastapi import FastAPI, Request, BackgroundTasks, HTTPException
from fastapi.responses import RedirectResponse, JSONResponse

from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleRequest

from gmail_service import (
    SCOPES, build_gmail_service, create_watch, load_credentials_from_token,
    fetch_messages_by_history, send_message
)

app = FastAPI()
logger = logging.getLogger(__name__)

# ---- CONFIG ----
CLIENT_SECRETS_FILE = "credentials.json"  # descargada desde Cloud Console
PUBSUB_TOPIC = os.environ.get("PUBSUB_TOPIC")  # projects/<PROJECT>/topics/<topic>
FRONTEND_BASE = os.environ.get("FRONTEND_BASE", "http://localhost:3000")
# -----------------

# Aquí simulamos un storage simple. En producción usá DB.
USERS = {}  # user_email -> {client_id, client_secret, token_info..., last_history_id}

# --- OAuth endpoints (web server flow) ---
@app.get("/auth/gmail/start")
def auth_start(state: str = "app"):
    flow = Flow.from_client_secrets_file(
        CLIENT_SECRETS_FILE,
        scopes=SCOPES,
        redirect_uri=os.environ.get("OAUTH_REDIRECT_URI", "http://localhost:8000/auth/gmail/callback"),
    )
    auth_url, _ = flow.authorization_url(access_type="offline", include_granted_scopes="true", prompt="consent")
    return RedirectResponse(auth_url)


@app.get("/auth/gmail/callback")
def auth_callback(code: str):
    flow = Flow.from_client_secrets_file(
        CLIENT_SECRETS_FILE,
        scopes=SCOPES,
        redirect_uri=os.environ.get("OAUTH_REDIRECT_URI", "http://localhost:8000/auth/gmail/callback"),
    )
    flow.fetch_token(code=code)
    creds = flow.credentials  # google.oauth2.credentials.Credentials

    # obtener info básica de usuario
    service = build_gmail_service(creds)
    profile = service.users().getProfile(userId="me").execute()
    email = profile.get("emailAddress")

    # guardá tokens (ejemplo simple)
    USERS[email] = {
        "token_info": {
            "token": creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri": creds.token_uri,
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "scopes": creds.scopes,
        }
    }

    # crear watch y almacenar historyId inicial
    resp = create_watch(service, PUBSUB_TOPIC, label_ids=["INBOX"])
    USERS[email]["last_history_id"] = int(resp.get("historyId"))
    return JSONResponse({"email": email, "watch": resp})


# --- Webhook para recibir Pub/Sub push desde tu subscription (Gmail -> Pub/Sub -> push -> este endpoint) ---
@app.post("/webhook/gmail")
async def webhook_gmail(request: Request, background_tasks: BackgroundTasks):
    """
    Formato de Pub/Sub push:
    {
      "message": { "data": "BASE64URL(...)",
                   "messageId": "...",
                   "publishTime": "..." },
      "subscription": "projects/.../subscriptions/..."
    }
    """
    body = await request.json()
    if "message" not in body:
        raise HTTPException(400, "invalid pubsub push")

    data_b64 = body["message"].get("data", "")
    # corregir padding y decodificar
    data_bytes = data_b64 + "=" * (-len(data_b64) % 4)
    payload_json = base64.urlsafe_b64decode(data_bytes.encode()).decode()
    payload = json.loads(payload_json)
    email = payload.get("emailAddress")
    history_id = int(payload.get("historyId"))

    # En prod: validar JWT de pubsub (si configuraste autenticación) — ver docs.
    # Hacemos el trabajo de manera asíncrona:
    background_tasks.add_task(handle_notification, email, history_id)
    return {"status": "accepted"}


def handle_notification(email: str, history_id: int):
    # recuperá credenciales del usuario
    info = USERS.get(email)
    if not info:
        logger.warning("No credentials for %s", email)
        return

    creds = load_credentials_from_token(info["token_info"])
    service = build_gmail_service(creds)

    last_known = info.get("last_history_id", history_id - 1)
    try:
        messages = fetch_messages_by_history(service, start_history_id=last_known)
    except Exception as e:
        logger.exception("Error fetching by history: %s", e)
        # si startHistoryId demasiado viejo -> hacer sync completo o listar mensajes recientes
        messages = []
    # procesá/normalizá cada mensaje
    for m in messages:
        logger.info("New mail for %s: %s - %s", email, m["from"], m["subject"])
        # guardá en DB / push a la cola interna / normalizar
    # actualizar last_history_id
    info["last_history_id"] = history_id


# --- Endpoint para enviar mail (ejemplo) ---
@app.post("/send/gmail")
def send_gmail(email: str, to: str, subject: str, body: str):
    # buscar user creds (ejemplo simple: email es quien autoriza)
    info = USERS.get(email)
    if not info:
        raise HTTPException(404, "user not authorized")

    creds = load_credentials_from_token(info["token_info"])
    service = build_gmail_service(creds)
    resp = send_message(service, to, subject, body, sender=email)
    return resp
