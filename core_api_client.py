"""Cliente HTTP para comunicarse con el Core API."""
import httpx
import json
import logging
from typing import Dict, Optional

logger = logging.getLogger(__name__)

class CoreAPIClient:
    """Cliente para comunicarse con el Seminario-Api-Core."""
    
    def __init__(self, core_api_url: str = "http://localhost:8003"):
        self.core_api_url = core_api_url
        
    async def send_gmail_notification(self, email: str, history_id: int) -> bool:
        """Enviar notificación de nuevo mensaje Gmail al Core API."""
        try:
            payload = {
                "message": {
                    "data": "",  # Para compatibilidad con formato Pub/Sub
                    "messageId": f"gmail_{history_id}",
                    "publishTime": ""
                }
            }
            
            # Datos adicionales para el Core API
            notification_data = {
                "email": email,
                "history_id": history_id,
                "source": "gmail_service"
            }
            
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.core_api_url}/api/v1/webhook/gmail",
                    json=notification_data,
                    timeout=30.0
                )
                
                if response.status_code == 200:
                    logger.info(f"Successfully notified Core API about Gmail update for {email}")
                    return True
                else:
                    logger.error(f"Failed to notify Core API: {response.status_code} - {response.text}")
                    return False
                    
        except Exception as e:
            logger.error(f"Error sending notification to Core API: {str(e)}")
            return False
    
    async def sync_gmail_messages(self, email: str, limit: int = 50) -> Dict:
        """Solicitar sincronización manual de mensajes Gmail."""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.core_api_url}/api/v1/webhook/gmail/sync",
                    params={"email": email, "limit": limit},
                    timeout=30.0
                )
                
                if response.status_code == 200:
                    return response.json()
                else:
                    logger.error(f"Failed to sync Gmail messages: {response.status_code}")
                    return {"status": "error", "message": "Sync failed"}
                    
        except Exception as e:
            logger.error(f"Error in sync request: {str(e)}")
            return {"status": "error", "message": str(e)}

