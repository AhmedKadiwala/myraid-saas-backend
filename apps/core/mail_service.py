import base64
import logging
from email.mime.text import MIMEText

from django.conf import settings
from django.core.mail import EmailMessage, send_mail

logger = logging.getLogger(__name__)


def send_mail_from_user(actor, recipient_email: str, subject: str, body: str) -> bool:
    """
    Send an email on behalf of a specific user.
    If the user has granted Google mail permissions (Gmail API access token),
    sends directly via the Gmail API so it comes from that user's Gmail.
    Otherwise, falls back to Django's configured mail backend with the user's
    name and address as sender/reply-to.
    """
    # 1. Try Gmail API if actor has google_credential with send capability
    cred = getattr(actor, "google_credential", None)
    if cred and cred.access_token and cred.can_send_mail:
        try:
            import requests

            msg = MIMEText(body, "plain", "utf-8")
            msg["to"] = recipient_email
            sender_name = actor.get_full_name() or actor.email
            msg["from"] = f"{sender_name} <{actor.email}>"
            msg["subject"] = subject

            raw_bytes = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
            resp = requests.post(
                "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
                headers={
                    "Authorization": f"Bearer {cred.access_token}",
                    "Content-Type": "application/json",
                },
                json={"raw": raw_bytes},
                timeout=10,
            )
            if resp.status_code in (200, 201):
                logger.info(
                    "Invitation email sent via Gmail API from %s to %s",
                    actor.email,
                    recipient_email,
                )
                return True
            else:
                logger.warning(
                    "Gmail API failed (%s): %s. Falling back to email backend.",
                    resp.status_code,
                    resp.text,
                )
        except Exception as exc:
            logger.warning(
                "Error sending via Gmail API: %s. Falling back to email backend.", exc
            )

    # 2. Fallback to standard Django mail backend
    try:
        sender_header = f"{actor.get_full_name() or actor.email} <{actor.email}>"
        email = EmailMessage(
            subject=subject,
            body=body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[recipient_email],
            reply_to=[actor.email],
            headers={"Sender": actor.email, "X-Invited-By": actor.email},
        )
        email.send(fail_silently=False)
        logger.info(
            "Invitation email sent via backend to %s on behalf of %s",
            recipient_email,
            actor.email,
        )
        return True
    except Exception as exc:
        logger.info("Could not send email to %s: %s", recipient_email, exc)
        return False
