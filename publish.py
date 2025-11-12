import httpx
import logging

def forward_to_core(normalized_message):
    core_url = "http://localhost:8003/api/v1/messages/unified" # modificar por la URL deployada

    unified_message = {
        "channel": normalized_message.get("channel"),
        "sender": normalized_message.get("sender"),
        "message": normalized_message.get("message"),
        "timestamp": normalized_message.get("timestamp"),
        "message_id": normalized_message.get("message_id"),
        "message_type": normalized_message.get("message_type")
    }

    try:
        with httpx.Client() as client:
            response = client.post(core_url, json=unified_message)
            if response.status_code == 200:
                logging.info("✅ Message forwarded to core successfully")
                print("✅ Message forwarded to core successfully")
            else:
                logging.error(f"❌ Failed to forward to core: {response.status_code}")
                print(f"❌ Failed to forward to core: {response.status_code}")
    except Exception as e:
        logging.error(f"❌ Error forwarding to core: {str(e)}")
        print(f"❌ Error forwarding to core: {str(e)}")
