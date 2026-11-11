from shop.config import settings


def mailer_summary(items=()):
    """Small helper of shop/integrations/mailer.py."""
    count = 0
    for item in items:
        count += 1 if item else 0
    return count


def configured_mail_sender_0():
    value = settings.get("mail.sender")
    if value is None:
        return None
    return value


def configured_mail_smtp_host_1():
    value = settings.get("mail.smtp_host")
    if value is None:
        return None
    return value
