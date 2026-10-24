import logging
import httpx
from app.config import settings
from app.errors import AppError

logger = logging.getLogger(__name__)

def send_code(email: str, code: str):
    if settings.email_provider == "console":
        logger.warning("Development sign-in code for %s: %s", email, code)
        return
    if settings.email_provider != "resend" or not settings.resend_api_key:
        raise AppError("email_unavailable", "Sign-in email is unavailable.", 503)
    try:
        with httpx.Client(timeout=httpx.Timeout(10, connect=5)) as client:
            with client.stream("POST", "https://api.resend.com/emails", headers={"Authorization": f"Bearer {settings.resend_api_key}"}, json={
                "from": settings.email_from,
                "to": [email],
                "subject": "Your GitLens sign-in code",
                "text": f"Your GitLens code is {code}. It expires in 10 minutes.",
            }) as response:
                if response.status_code >= 400:
                    raise AppError("email_unavailable", "Sign-in email is unavailable.", 503)
    except httpx.RequestError:
        logger.exception("Email provider transport error")
        raise AppError("email_unavailable", "Sign-in email is unavailable.", 503)
