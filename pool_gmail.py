# poll_gmail.py
import time
from gmail_service import build_gmail_service, load_credentials_from_token, parse_gmail_message

# Supongamos que tenés guardado el token_info (obtenido en el paso 4)
token_info = {...}  # acá pegás lo que devuelve flow.credentials.to_json()

creds = load_credentials_from_token(token_info)
service = build_gmail_service(creds)

def fetch_inbox():
    messages = service.users().messages().list(userId="me", labelIds=["INBOX", "CATEGORY_PERSONAL"], q="is:unread").execute()
    for m in messages.get("messages", []):
        full = service.users().messages().get(userId="me", id=m["id"], format="full").execute()
        parsed = parse_gmail_message(full)
        print(f"De: {parsed['from']} - Asunto: {parsed['subject']}")
        print(parsed['body'])
        print("-----------")
        # opcional: marcar como leído
        """service.users().messages().modify(
            userId="me", id=m["id"], body={"removeLabelIds": ["UNREAD"]}
        ).execute()"""

while True:
    fetch_inbox()
    time.sleep(60)  # cada minuto
