"""What a tool does to the workspace, declared by the tool rather than guessed from its name (D-087).

effect is one of:
- read: reads only;
- write: changes files in the workspace;
- run: runs code in the sandbox, which may change the workspace;
- external: not known, or acts outside the workspace.

effect_of() takes the tool's own declaration first; a tool that declares nothing is looked up in
BUILTIN_EFFECTS, this project's own tools and the one table of their names; anything else is
'external' and is treated as a side effect (fail closed, as D-080). Built-in MCP servers declare
their own effect; tools from a user's own MCP server (D-095) get their effect solely from user
configuration ('read' or 'external', never from readOnlyHint).
"""

from __future__ import annotations

EFFECTS = ('read', 'write', 'run', 'external')

BUILTIN_EFFECTS = {
    # The filesystem server, the local tools and web search read only.
    'read_file': 'read', 'list_directory': 'read', 'search_files': 'read',
    'get_current_time': 'read', 'retrieve_knowledge': 'read', 'search_web': 'read',
    'read_context_artifact': 'read',
    # Runs code with the workspace mounted read-only (D-028).
    'run_python_code': 'read',
    'write_file': 'write', 'edit_file': 'write',
    'run_tests': 'run', 'run_python': 'run',
}


def effect_of_name(name, declared=None) -> str:
    """The effect of a tool by its declaration, else its built-in name, else 'external'."""
    if declared in EFFECTS:
        return declared
    return BUILTIN_EFFECTS.get(name, 'external') if isinstance(name, str) else 'external'


def effect_of(definition) -> str:
    """The effect of a ToolDefinition (or anything with name and an optional effect)."""
    return effect_of_name(getattr(definition, 'name', None), getattr(definition, 'effect', None))


def has_side_effect(effect: str) -> bool:
    """Anything but a read: its outcome is uncertain after a raise, a timeout or a cancel (D-032)."""
    return effect != 'read'


def names_with(effects, names=None) -> frozenset[str]:
    """Built-in tool names whose effect is in effects (for name sets kept for older imports)."""
    names = BUILTIN_EFFECTS if names is None else names
    return frozenset(n for n in names if effect_of_name(n) in effects)
