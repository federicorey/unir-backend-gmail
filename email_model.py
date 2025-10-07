from pydantic import BaseModel

class EmailRequest(BaseModel):
    email: str
    to: str
    subject: str
    body: str