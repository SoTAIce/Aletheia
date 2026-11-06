"""Role-based permissions. Roles and users are loaded from roles.json and users.json."""

import json
from pathlib import Path


def load(root):
    root = Path(root)
    roles = json.loads((root / "roles.json").read_text(encoding="utf-8"))
    users = json.loads((root / "users.json").read_text(encoding="utf-8"))
    return roles, users


def resolve(role, roles, seen=None):
    """Permissions of a role: inherited ones first, then its own allow list, minus its deny list.

    The same `seen` set is shared by the whole walk, so a role reached a second time
    (through another parent or a cycle) contributes nothing the second time.
    """
    seen = set() if seen is None else seen
    if role in seen or role not in roles:
        return set()
    seen.add(role)
    spec = roles[role]
    perms = set()
    for parent in spec.get("inherits", []):
        perms |= resolve(parent, roles, seen)
    perms |= set(spec.get("allow", []))
    perms -= set(spec.get("deny", []))
    return perms


def user_perms(name, users, roles):
    """Union of the user's roles (each resolved with a fresh walk), minus the user's own deny list."""
    user = users[name]
    perms = set()
    for role in user.get("roles", []):
        perms |= resolve(role, roles)
    perms -= set(user.get("deny", []))
    return sorted(perms)


def can(name, perm, users, roles):
    perms = user_perms(name, users, roles)
    area = perm.split(":")[0]
    return perm in perms or f"{area}:*" in perms
