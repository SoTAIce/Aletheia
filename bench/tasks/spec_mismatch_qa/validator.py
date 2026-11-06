"""Password policy check. See SPEC.md."""

MIN_LENGTH = 8
MAX_LENGTH = 64


def check_password(password, username):
    problems = []
    if len(password) < MIN_LENGTH:
        problems.append("R1")
    if len(password) > MAX_LENGTH:
        problems.append("R2")
    if " " in password:
        problems.append("R6")
    if not any("A" <= ch <= "Z" for ch in password):
        problems.append("R3")
    if not any(ch in "0123456789" for ch in password):
        problems.append("R4")
    if username and username in password:
        problems.append("R5")
    return problems
