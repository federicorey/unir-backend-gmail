# app.py
import os
import json
import logging
import base64
from wsgiref import headers

from fastapi import FastAPI, Request, BackgroundTasks, HTTPException
from fastapi.responses import RedirectResponse, JSONResponse

from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleRequest
from datetime import datetime
from email_model import EmailRequest
import email.utils as email_utils

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

import glob


# Cargar tokens guardados (si existen)
for path in glob.glob("token_*.json"):
    with open(path, "r") as f:
        info = json.load(f)
        email = path.replace("token_", "").replace(".json", "")
        USERS[email] = {"token_info": info}


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
    creds = flow.credentials

    service = build_gmail_service(creds)
    profile = service.users().getProfile(userId="me").execute()
    email = profile.get("emailAddress")

    token_info = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": creds.scopes,
    }

    USERS[email] = {"token_info": token_info}

    # 🔹 Guarda el token en un archivo para persistencia
    with open(f"token_{email}.json", "w") as f:
        json.dump(token_info, f)

    return {"status": "ok", "email": email, "creds": token_info}




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
def send_gmail(req: EmailRequest):
    email = req.email
    to = req.to
    subject = req.subject
    body = req.body
    # buscar user creds (ejemplo simple: email es quien autoriza)
    info = USERS.get(email)
    if not info:
        raise HTTPException(404, "user not authorized")

    creds = load_credentials_from_token(info["token_info"])
    service = build_gmail_service(creds)
    resp = send_message(service, to, subject, body, sender=email)
    return resp

@app.get("/gmail/inbox")
def list_recent_mails(email: str, limit: int = 10):
    info = USERS.get(email)
    if not info:
        raise HTTPException(404, "user not authorized")

    creds = load_credentials_from_token(info["token_info"])
    service = build_gmail_service(creds)

    # Obtener los últimos 'limit' mails de la bandeja de entrada
    result = service.users().messages().list(userId="me", labelIds=["UNREAD"], q="category:primary", maxResults=limit).execute()
    messages = result.get("messages", [])
    mails = []

    for m in messages:
        full = service.users().messages().get(userId="me", id=m["id"], format="full").execute()
        # Convert internalDate (milliseconds since epoch) to dd/mm/yy
        #internal_date = full.get("internalDate", "")
        headers = full.get("payload", {}).get("headers", [])
        date_header = next((h["value"] for h in headers if h["name"].lower() == "date"), None)
        
        if date_header:
            # Convierte la fecha del header MIME a datetime
            parsed_date = email_utils.parsedate_to_datetime(date_header)
            formatted_date = parsed_date.strftime("%d/%m/%y %H:%M")
        elif "internalDate" in full:
            dt = datetime.fromtimestamp(int(full["internalDate"]) / 1000)
            formatted_date = dt.strftime("%d/%m/%y %H:%M")
        else:
            formatted_date = ""

        parsed = {
            "id": m["id"],
            "from": "",
            "subject": "",
            "snippet": full.get("snippet", ""),
            "date": formatted_date
        }

        for h in headers:
            if h["name"].lower() == "from":
                parsed["from"] = h["value"]
            elif h["name"].lower() == "subject":
                parsed["subject"] = h["value"]
        mails.append(parsed)

    return {"email": email, "messages": mails}

