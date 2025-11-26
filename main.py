# app.py
import os
import json
import logging
import base64
from wsgiref import headers

from fastapi import FastAPI, Request, BackgroundTasks, HTTPException, Body
from fastapi.responses import RedirectResponse, JSONResponse

from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleRequest
from datetime import datetime
from email_model import EmailRequest
import email.utils as email_utils
from publish import forward_to_core
import asyncio

from gmail_service import (
    SCOPES, build_gmail_service, create_watch, load_credentials_from_token,
    fetch_messages_by_history, send_message
)
from core_api_client import CoreAPIClient
from googleapiclient.errors import HttpError

app = FastAPI()
logging.basicConfig(
    level=logging.INFO,  # mostrará logs tipo INFO, WARNING, ERROR
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# ---- CONFIG ----
CLIENT_SECRETS_FILE = "credentials.json"  # descargada desde Cloud Console
PUBSUB_TOPIC = os.environ.get("PUBSUB_TOPIC")  # projects/<PROJECT>/topics/<topic>
FRONTEND_BASE = os.environ.get("FRONTEND_BASE", "http://localhost:3000")

# URL base del servicio en ngrok (producción)
# IMPORTANTE: Esta URL debe estar registrada en Google Cloud Console como redirect URI autorizado
NGROK_BASE_URL = "https://lilah-tophaceous-overhonestly.ngrok-free.dev"
OAUTH_REDIRECT_URI_DEFAULT = f"{NGROK_BASE_URL}/auth/gmail/callback"
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
    redirect_uri = os.environ.get("OAUTH_REDIRECT_URI", OAUTH_REDIRECT_URI_DEFAULT)
    logger.info(f"🔐 Iniciando OAuth con redirect_uri: {redirect_uri}")
    
    flow = Flow.from_client_secrets_file(
        CLIENT_SECRETS_FILE,
        scopes=SCOPES,
        redirect_uri=redirect_uri,
    )
    auth_url, _ = flow.authorization_url(access_type="offline", include_granted_scopes="true", prompt="consent")
    return RedirectResponse(auth_url)


@app.get("/auth/gmail/callback")
def auth_callback(code: str):
    redirect_uri = os.environ.get("OAUTH_REDIRECT_URI", OAUTH_REDIRECT_URI_DEFAULT)
    logger.info(f"🔐 Callback OAuth recibido con redirect_uri: {redirect_uri}")
    
    try:
        flow = Flow.from_client_secrets_file(
            CLIENT_SECRETS_FILE,
            scopes=SCOPES,
            redirect_uri=redirect_uri,
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

        resp = create_watch(service, "projects/unir-gmail/topics/gmail_notifications", label_ids=["INBOX"])
        USERS[email]["last_history_id"] = int(resp["historyId"])

        # 🔹 Guarda el token en un archivo para persistencia
        with open(f"token_{email}.json", "w") as f:
            json.dump(token_info, f)

        logger.info(f"✅ OAuth exitoso para {email}")
        
        # Redirigir al frontend con parámetro de éxito
        frontend_redirect = f"{FRONTEND_BASE}/linkedAccounts?gmail_connected=true&email={email}"
        return RedirectResponse(url=frontend_redirect)
        
    except Exception as e:
        logger.error(f"❌ Error en callback OAuth: {str(e)}")
        # Redirigir al frontend con error
        frontend_redirect = f"{FRONTEND_BASE}/linkedAccounts?gmail_connected=false&error={str(e)}"
        return RedirectResponse(url=frontend_redirect)



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
    print("✅ Webhook /webhook/gmail fue llamado")
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
    print(f"🔔 [Webhook recibido] email={email}, history_id={history_id}")

    info = USERS.get(email)
    if not info:
        print("⚠️ No hay credenciales guardadas para ese usuario")
        return

    creds = load_credentials_from_token(info["token_info"])
    service = build_gmail_service(creds)

    last_known = info.get("last_history_id")
    if not last_known:
        # Si es la primera vez, inicializar con el que llegó
        info["last_history_id"] = history_id
        print("🆕 No había history_id previo, se inicializa con el actual")
        return

    try:
        messages = fetch_messages_by_history(service, start_history_id=last_known)
    except HttpError as e:
        print(f"❌ Error en fetch_messages_by_history: {e}")
        if e.resp.status == 404 and "historyId" in str(e):
            print("⚠️ Re-suscribiendo con watch() para obtener nuevo history_id...")
            resp = create_watch(service, "projects/unir-gmail/topics/gmail_notifications", ["INBOX"])
            info["last_history_id"] = int(resp["historyId"])
        return
    except Exception as e:
        print(f"❌ Error en fetch_messages_by_history: {e}")
        return

    if not messages:
        print("⚠️ No se encontraron mensajes nuevos")
        info["last_history_id"] = history_id
        return

    # Tomar solo el último mensaje
    m = messages[-1]
    print(f"📬 Último mail: {m['from']} - {m['subject']}")
    # Construir mensaje unificado

    unified_message = {
        "channel": "gmail",
        "sender": m["from"],
        "message": m.get("snippet") or m.get("body", ""),
        "timestamp": m.get("publishTime") or datetime.utcnow().isoformat(),
        "message_id": m.get("id"),
        "message_type": "email"
    }
    print(f"body {unified_message}")

    # Llamar al forward_to_core de manera asíncrona
    try:
        forward_to_core(unified_message)
        print(f"✅ Mensaje reenviado al Core: {m['subject']}")
    except Exception as e:
        print(f"❌ Error reenviando mensaje al Core: {e}")

    info["last_history_id"] = history_id




# --- Endpoint para enviar mail (acepta formato Core y EmailRequest) ---
@app.post("/send/gmail")
def send_gmail(req: dict):
    """Acepta ambos formatos:
    - Core API: {"to", "message", "message_type", ["email"], ["subject"]}
    - EmailRequest: {"email", "to", "subject", "body"}
    """
    # Campos básicos
    to = req.get("to")
    if not to:
        raise HTTPException(400, "'to' is required")

    # message puede venir como 'message' (Core) o 'body' (EmailRequest)
    body = req.get("message") or req.get("body") or ""
    # subject opcional
    subject = req.get("subject") or "Mensaje desde Unir"

    # Seleccionar remitente: primero 'email' en el payload, luego DEFAULT_GMAIL_SENDER, luego primer USERS
    email = req.get("email")
    if not email:
        default_sender = os.environ.get("DEFAULT_GMAIL_SENDER")
        if default_sender and default_sender in USERS:
            email = default_sender
            logger.info(f"📧 Usando DEFAULT_GMAIL_SENDER: {email}")
        elif USERS:
            email = next(iter(USERS.keys()))
            logger.info(f"📧 Usando cuenta por defecto disponible: {email}")
        else:
            raise HTTPException(400, "'email' is required and no authorized gmail account available to send")

    # validar credenciales
    info = USERS.get(email)
    if not info:
        raise HTTPException(404, f"user {email} not authorized")

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

@app.post("/watch/gmail/reset")
def reset_watch(email: str):
    info = USERS.get(email)
    if not info:
        raise HTTPException(404, "user not authorized")

    creds = load_credentials_from_token(info["token_info"])
    service = build_gmail_service(creds)

    resp = create_watch(
        service,
        "projects/unir-gmail/topics/gmail_notifications",
        label_ids=["INBOX"]
    )

    USERS[email]["last_history_id"] = int(resp["historyId"])
    return {"status": "watch_reset", "watch": resp}

