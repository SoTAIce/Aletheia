"""Notifications and audit log."""

SENT = []


def log_event(name, detail):
    print(f"[event] {name}: {detail}")


def send_email(user, text):
    SENT.append((user, text))
    log_event("email", user)


def send_sms(user, text):
    log_event("sms", user)
