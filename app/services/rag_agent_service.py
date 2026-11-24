"""Session-facing service using Aletheia's single registry; every task runs in one LoopRun."""

import asyncio
import hashlib
import json
import logging
import math
import re
from dataclasses import asdict
from datetime import datetime, timezone
from uuid import uuid4
from collections.abc import AsyncIterator, Sequence
from contextlib import AsyncExitStack, asynccontextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TYPE_CHECKING
if TYPE_CHECKING:
    from app.services.memory_service import LongTermMemoryService

from mcp import StdioServerParameters
from openai import AsyncOpenAI

from app.agent.openai_compatible_tool_calling_model import profile_model
from app.agent.runtime import AgentRunResult
from app.config import config
from app.models.conversation_memory import (
    ConversationMemory, ConversationMessage, ConversationTurn, MessageRole, TurnStatus,
    finish_conversation_write as _durable,
)
from app.agent.step_models import StepExecutionReceipt, ToolCall, ToolResult
from app.models.taskstate import TaskStatus
from app.models.run_state import RunMemory, RunState, StaleExecutionError
from app.agent.loop_run import LoopRun
from app.tools.tool_loader import local_mcp_servers, open_tool_registry
from app.services.conversation_archive import ConversationArchive
from app.services.context_manager import ContextManager
from app.agent.step_runner import RoleModel
from app.models.model_profile import ROLES
from app.services.run_assembler import RunAssembler
from app.services.summary_scheduler import SummaryScheduler
from app.tools.context_archive_tool import ContextArchiveTool


logger = logging.getLogger(__name__)


def _restore_result(data: dict) -> AgentRunResult:
    """Reconstruct an immutable execution receipt; never execute archived work."""
    data = dict(data)
    steps = []
    for item in data.pop('steps'):
        item = dict(item)
        item['source_refs'] = tuple(item['source_refs'])
        item['tool_calls'] = tuple(ToolCall(**c) for c in item['tool_calls'])
        item['tool_results'] = tuple(ToolResult(**(r | {'source_refs': tuple(r['source_refs'])}))
                                     for r in item['tool_results'])
        steps.append(StepExecutionReceipt(**item))
    data['status'] = TaskStatus(data['status'])
    data['step_log'] = tuple(data.get('step_log', ()))   # older archives have no run log (D-034)
    return AgentRunResult(steps=tuple(steps), **data)

def _total_tokens(usage) -> int:
    value = (usage or {}).get('total_tokens') if isinstance(usage, dict) else None
    return value if type(value) is int and value > 0 else 0


def _archived_tokens(archive) -> int:
    """Tokens the archived runs of a conversation used, from their stored results (D-060)."""
    total = 0
    for turn in archive.load_turns():
        try:
            result = archive.read_turn_result(turn.turn_id)
        except (KeyError, ValueError):
            continue
        total += _total_tokens((result or {}).get('usage'))
    return total


def _emit(on_event, event: dict) -> None:
    """Report host-authored progress; a failing callback never affects the request (D-045)."""
    if on_event is None:
        return
    try:
        on_event(event)
    except Exception as exc:
        logger.warning('Progress callback failed: %s', type(exc).__name__)


def _turn_view(turn: ConversationTurn, resumable: bool) -> dict:
    """The user-facing fields of one archived turn; never tool output or run details."""
    reply = turn.messages[-1].content if len(turn.messages) > 1 and turn.messages[-1].role is MessageRole.ASSISTANT else None
    return {'turn_id': turn.turn_id, 'task_id': turn.task_id,
            'status': turn.status.value if turn.status is not None else None,
            'question_id': turn.question_id, 'resumable': resumable,
            'created_at': turn.created_at.isoformat(),
            'finished_at': turn.finished_at.isoformat() if turn.finished_at else None,
            'user': turn.messages[0].content, 'reply': reply}


def _check_limit(limit) -> None:
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError('limit must be an integer between 1 and 200')


MODES = ('single', 'team', 'auto')


MAX_ATTACHMENTS = 5
MAX_ATTACHMENT_CHARS = 1_000_000
WORKSPACE_CHANGED_NOTE = ('The working directory of this conversation has changed since the earlier turns; '
                          'file paths mentioned before refer to the previous directory and may not exist now.')


def _check_choice(name, value) -> None:
    if value is not None and type(value) is not bool:
        raise ValueError(f'{name} must be true, false or None')



def _check_runner(value):
    """'loop' is the only single-mode runner since stage 4c (D-076)."""
    if value == 'plan':
        raise ValueError('single_runner plan was removed in stage 4c (D-076); use loop')
    if value != 'loop':
        raise ValueError('single_runner must be loop')



@dataclass(frozen=True, slots=True)
class RoleModels:
    """The model of each role for one run (stage 4d-2); one object for every role without a
    profile file. profiles is None for an injected model."""
    main: Any
    route: Any
    member: Any
    explorer: Any
    profiles: Any = None

class RagAgentService:
    """Each accepted request is a turn; explicit resumes retain the same task.

    Inject a ToolCallingModel for tests.
    query_stream keeps the event API but emits the final result, not model tokens.
    """

    def __init__(
        self, streaming: bool = True, model: Any | None = None, *,
        mcp_servers: Sequence[StdioServerParameters] | None = None,
        knowledge_search=None, include_knowledge: bool = True,
        max_executions: int = 16,
        memory_directory=None, conversation_compressor=None,
        longterm_memory:"LongTermMemoryService | None" = None,
        longterm_recall_limit: int = 5,
        longterm_timeout_seconds: float = 3.0,
        execution_mode: str = 'single', team_options=None, hooks=None,
        single_runner: str = 'loop', loop_options=None, delegate: bool = False,
        workspace_servers=None, workspace_policy=None, default_workspace: str | None = None,
        personas_dir: str | None = None,
        permission_rules=None, asker=None,
        user_mcp_servers=(),
        instructions_trust=None,
    ) -> None:
        self.streaming = streaming
        self.model = model
        # Hooks of every run of this service (D-059); None keeps the caller's.
        from app.agent.hooks import HookSet
        if hooks is not None and not isinstance(hooks, HookSet):
            raise TypeError('hooks must be a HookSet')
        self._hooks = hooks
        from app.agent.permission_rules import PermissionRules
        if permission_rules is not None and not isinstance(permission_rules, PermissionRules):
            raise TypeError('permission_rules must be PermissionRules')
        self._permission_rules = permission_rules
        self._asker = asker
        self._user_mcp_servers = (
            tuple(user_mcp_servers.enabled_servers)
            if hasattr(user_mcp_servers, 'enabled_servers') else tuple(user_mcp_servers)
        )
        from app.services.project_instructions import InstructionsTrust
        if instructions_trust is not None and not isinstance(instructions_trust, InstructionsTrust):
            raise TypeError('instructions_trust must be InstructionsTrust')
        self._instructions_trust = instructions_trust
        self._spent_tokens: dict = {}       # conversation key -> tokens used, for the cap (D-060)
        self._memory_directory = config.conversation_storage_dir if memory_directory is None else memory_directory
        self._conversation_compressor = conversation_compressor
        if execution_mode not in ('single', 'team', 'auto'):
            raise ValueError('execution_mode must be single, team or auto')
        from app.agent.delegation import DelegationOptions
        if team_options is not None and not isinstance(team_options, DelegationOptions):
            raise TypeError('team_options must be DelegationOptions')
        self._execution_mode = execution_mode
        # Single mode is one agent loop (D-072). 'plan' (plan then execute steps) was removed with the
        # plan/execute skeleton in stage 4c (D-076); the option stays for old callers and takes only 'loop'.
        _check_runner(single_runner)
        from app.agent.agent_loop import LoopOptions
        if loop_options is not None and not isinstance(loop_options, LoopOptions):
            raise TypeError('loop_options must be LoopOptions')
        self._single_runner = single_runner
        self._loop_options = loop_options or LoopOptions(timeout_seconds=config.agent_task_timeout_seconds)
        # On-demand read-only delegation inside the single loop (loop-delegate-001); off by default.
        if type(delegate) is not bool:
            raise TypeError('delegate must be a bool')
        self._delegate = delegate
        self._team_options = team_options or (DelegationOptions() if execution_mode == 'team' else None)
        self._routes: dict[tuple[object, str], object] = {}   # task -> ModeChoice made at its start (D-026)
        self._task_options: dict[tuple[object, str], dict] = {}   # task -> run options chosen at its start (D-051)
        self._mcp_servers = tuple(mcp_servers) if mcp_servers is not None else local_mcp_servers(
            filesystem_root=config.mcp_filesystem_root or None,
            write_root=config.mcp_filesystem_write_root or None,
            tavily_api_key=config.tavily_api_key,
        )
        # Choosing the working directory (workspace-001, D-074): workspace_servers(root, writable)
        # gives one run's tool servers for a checked directory; None keeps the fixed mcp_servers.
        if workspace_servers is not None and not callable(workspace_servers):
            raise TypeError('workspace_servers must be callable')
        from app.services.workspace import WorkspacePolicy
        if workspace_policy is not None and not isinstance(workspace_policy, WorkspacePolicy):
            raise TypeError('workspace_policy must be WorkspacePolicy')
        self._workspace_servers = workspace_servers
        self._workspace_policy = workspace_policy or (WorkspacePolicy.from_config() if workspace_servers else None)
        self._default_workspace = str(Path(default_workspace).resolve()) if default_workspace else None
        self._session_workspaces: dict = {}     # conversation key -> directory of its last task
        # Personas (docs/persona_plan.md): None reads PERSONAS_DIR, '' turns them off. A persona's
        # working directory is checked with this service's policy, so only where /dir works.
        from app.services.personas import PersonaStore
        directory = config.personas_dir if personas_dir is None else personas_dir
        self._personas = PersonaStore(
            directory, workspace_policy=self._workspace_policy if workspace_servers else None,
        ) if directory else None
        self._knowledge_search = knowledge_search
        self._include_knowledge = include_knowledge
        # The task deadline comes from the chosen runner's options (RunAssembler, stage 5b).
        self._limits = dict(max_executions=max_executions)
        self._run_assembler = RunAssembler(
            team_options=self._team_options, loop_options=self._loop_options, delegate=self._delegate,
            limits=self._limits, routes=self._routes, task_options=self._task_options,
            default_workspace=self._default_workspace, emit=_emit,
        )
        self._conversations: dict[object, ConversationMemory] = {}
        self._locks: dict[object, asyncio.Lock] = {}
        self._tasks: dict[tuple[object, str], LoopRun] = {}
        self._pending_outcomes: dict[tuple[object, str], tuple] = {}
        self._summary_scheduler = SummaryScheduler(self._conversations)
        self._closed = False
        if type(longterm_recall_limit) is not int or not 1 <= longterm_recall_limit <=100:
            raise ValueError("longterm_recall_limit must be between 1 and 100")

        if (
            isinstance(longterm_timeout_seconds, bool)
            or not isinstance(longterm_timeout_seconds, (int, float))
            or not math.isfinite(longterm_timeout_seconds)
            or longterm_timeout_seconds <= 0
        ):
            raise ValueError("longterm_timeout_seconds must be positive and finite")

        self._longterm_memory = longterm_memory
        self._longterm_recall_limit = longterm_recall_limit
        self._longterm_timeout_seconds = longterm_timeout_seconds

    def _validate_identity(self, user_id: str | None) -> None:
        if user_id is None:
            if self._longterm_memory is not None:
                raise ValueError("user_id is required when long-term memory is enabled")
            return

        if not isinstance(user_id, str) or not user_id.strip():
            raise ValueError("user_id must be non-empty")
        if len(user_id.encode("utf-8")) > 256:
            raise ValueError("user_id is too long")

    @staticmethod
    def _session_key(user_id: str | None, session_id: str):
        if user_id is None:
            return session_id
        return ("user", user_id, session_id)

    @staticmethod
    def _archive_label(user_id: str | None, session_id: str) -> str:
        if user_id is None:
            return session_id
        return json.dumps(
            ["user", user_id, session_id],
            ensure_ascii=False,
            separators=(",", ":"),
        )
    @staticmethod
    def _prompt_cache_key(label: str) -> str | None:
        """Per-conversation cache routing key for the separate endpoint (D-057): a hash, so
        the session and user names never leave the machine; DashScope gets none. Each model
        sends it only when its own profile supports it (stage 4d-2)."""
        if not any(profile.prompt_cache_key for profile in config.model_profiles().used()):
            return None
        return 'aletheia-' + hashlib.sha256(label.encode('utf-8')).hexdigest()[:24]

    @asynccontextmanager
    async def _models(self, cache_key: str | None = None, hooks=None):
        """The run's main model, wrapped in HookedModel, inside the run's hooks (D-059)."""
        async with self._role_models(cache_key, hooks) as models:
            yield models.main

    @asynccontextmanager
    async def _role_models(self, cache_key: str | None = None, hooks=None, profiles=None):
        """The run's model per role, each distinct model wrapped once in HookedModel (D-059, 4d-2)."""
        from app.agent.hooks import HookedModel, using_hooks
        hooks = hooks if hooks is not None else self._hooks
        with using_hooks(hooks) if hooks is not None else nullcontext():
            async with self._base_models(cache_key, profiles) as models:
                wrapped = {}
                def wrap(model):
                    return wrapped.setdefault(id(model), HookedModel(model))
                yield RoleModels(wrap(models.main), wrap(models.route), wrap(models.member),
                                 wrap(models.explorer), models.profiles)

    @asynccontextmanager
    async def _base_models(self, cache_key: str | None = None, profiles=None):
        if self.model is not None:
            yield RoleModels(self.model, self.model, self.model, self.model, None)
            return
        # One model per profile a role uses; profiles on the same endpoint, key setting and
        # timeout share one client. Without a profile file: one client, one model (4d-1, D-050).
        profiles = profiles if profiles is not None else config.model_profiles()
        async with AsyncExitStack() as stack:
            clients, built = {}, {}
            for profile in profiles.used():
                key = (profile.base_url, profile.api_key_env, profile.timeout_seconds)
                if key not in clients:
                    clients[key] = await stack.enter_async_context(AsyncOpenAI(
                        api_key=profile.api_key(config), base_url=profile.base_url,
                        timeout=profile.timeout_seconds, max_retries=0))
                built[profile.name] = profile_model(clients[key], profile, cache_key)
            yield RoleModels(*(built[profiles.roles[role]] for role in ROLES), profiles)

    @staticmethod
    def _member_role(models, role, conversation):
        """The RoleModel of a member role, or None when it runs on the main model (stage 4d-2).
        A role on another profile gets a context manager sized to that profile's window."""
        model = getattr(models, role)
        if model is models.main:
            return None
        manager = None
        if models.profiles is not None:
            profile = models.profiles.role(role)
            if profile is not models.profiles.role('main'):
                manager = ContextManager(window_tokens=profile.context_tokens,
                                         output_tokens=profile.max_output_tokens,
                                         safety_tokens=config.context_safety_tokens,
                                         archive=conversation.archive)
        return RoleModel(model, manager)

    async def _run_hooks(self, key, conversation, extra=None, *, workspace=None):
        """This run's hooks: the service's (or the caller's), the permission rule gate (D-094),
        the request's own (extra, e.g. the web page's approval gate, D-061), plus the audit log
        and the conversation token cap when configured (D-060). Returns (hooks, audit or None)."""
        from app.agent.builtin_hooks import AuditHook, ConversationTokenCap
        from app.agent.hooks import EMPTY, HookSet, installed_hooks
        from app.agent.permission_rules import RuleGate
        hooks = self._hooks if self._hooks is not None else installed_hooks() or EMPTY
        extra_gates = extra.gates if extra is not None else ()
        for gate in extra_gates:
            if isinstance(gate, RuleGate):
                gate.use_workspace(workspace)      # the request's gate learns the task's directory
        has_user_mcp = bool(self._user_mcp_servers)
        if (self._permission_rules or has_user_mcp) and not any(isinstance(g, RuleGate) for g in extra_gates):
            from app.agent.permission_rules import PermissionRules
            rules = self._permission_rules if self._permission_rules is not None else PermissionRules(())
            rule_gate = RuleGate(rules, workspace=workspace, asker=self._asker, ask_external=has_user_mcp)
            hooks = HookSet(hooks.observers, (*hooks.gates, rule_gate), gate_timeout=hooks.gate_timeout)
        if extra is not None:
            hooks = HookSet((*hooks.observers, *extra.observers), (*hooks.gates, *extra.gates),
                            gate_timeout=hooks.gate_timeout)
        audit = None
        if config.hooks_audit and conversation.archive is not None:
            audit = AuditHook(conversation.archive)
            hooks = hooks.with_observers(audit)
        if config.conversation_max_tokens is not None:
            if key not in self._spent_tokens:
                self._spent_tokens[key] = (await asyncio.to_thread(_archived_tokens, conversation.archive)
                                           if conversation.archive is not None else 0)
            cap = ConversationTokenCap(config.conversation_max_tokens, self._spent_tokens[key])
            hooks = HookSet(hooks.observers, (*hooks.gates, cap), gate_timeout=hooks.gate_timeout)
        return hooks, audit

    def run_options(self) -> dict:
        """Per-request choices this service accepts (D-051). A mode outside these is refused; the
        default mode is always accepted. Team and auto need the team prepared at startup."""
        accepted = {'single', self._execution_mode} | ({'team', 'auto'} if self._team_options is not None else set())
        offered = {'modes': [m for m in MODES if m in accepted], 'default_mode': self._execution_mode,
                   'runners': ['loop'],
                   'default_runner': self._single_runner,
                   'memory_available': self._longterm_memory is not None}
        if self._workspace_servers is not None:      # only when offered, so the old answer stays as it was
            offered.update(workspace_available=True, default_workspace=self._default_workspace)
        return offered

    @property
    def personas(self):
        """The persona store, or None when personas are off (PERSONAS_DIR empty)."""
        return self._personas

    @property
    def permission_rules(self):
        """The active permission rules, or None when no rules file is configured (D-094)."""
        return self._permission_rules

    @property
    def user_mcp_servers(self):
        """Enabled user MCP servers configured at startup (D-095)."""
        return self._user_mcp_servers

    @property
    def instructions_trust(self):
        """Trust store for working-directory project instructions, or None when disabled (D-096)."""
        return self._instructions_trust

    def _closed_folders(self) -> tuple[Path, ...]:
        return self._workspace_policy.closed_folders() if self._workspace_policy is not None else ()

    def instructions_status(self, workspace: str | None = None):
        """Inspect the working directory's project instructions file; None when disabled or absent."""
        if self._instructions_trust is None:
            return None
        target = workspace or self._default_workspace
        if not target:
            return None
        from app.services.project_instructions import find_instructions
        return find_instructions(target, self._closed_folders(), self._instructions_trust)

    def confirm_instructions(self, workspace: str | None, digest: str):
        """Confirm a project instructions file after re-reading and verifying its SHA-256 digest."""
        if self._instructions_trust is None:
            raise ValueError('project instructions are disabled')
        from app.services.project_instructions import (
            InstructionsChanged, ProjectInstructions, find_instructions,
        )
        target = workspace or self._default_workspace
        if not target:
            raise InstructionsChanged('没有设置工作目录。')
        found = find_instructions(target, self._closed_folders(), self._instructions_trust)
        if found is None or found.status == 'unreadable' or not digest or found.digest != digest:
            raise InstructionsChanged()
        self._instructions_trust.confirm(target, found.name, found.digest)
        return ProjectInstructions(
            name=found.name,
            text=found.text,
            chars=found.chars,
            truncated=found.truncated,
            digest=found.digest,
            status='loaded',
            reason=None,
        )

    def _resolve_persona(self, user_id, session_id, persona_id):
        """The persona of a new task: the one named for this request, else the session's. An
        unbound session is bound to the one it gets, so its first task fixes it."""
        store = self._personas
        if persona_id is not None:
            return store.get(user_id, persona_id)
        chosen = store.session_persona_id(user_id, session_id)
        persona = store.get(user_id, chosen)
        if not store.is_bound(user_id, session_id):
            store.bind(user_id, session_id, chosen)
        return persona

    async def _persona_choices(self, user_id, session_id, persona_id, command, question, choices):
        """A new task's persona, its progress notes, its command's constraint text and the run
        choices (execution_mode, allow_writes, use_memory, workspace) after both filled them.

        The command comes first: its mode wins over the request's, and write=false forces a
        read-only task. The persona's defaults then fill what is still open. A command that
        writes refuses to start when writing ends up off (docs/personal_commands_plan.md)."""
        execution_mode, allow_writes, use_memory, workspace = choices
        chosen = await asyncio.to_thread(self._resolve_persona, user_id, session_id, persona_id)
        found, command_text = None, None
        if command is not None:
            from app.services import personal_commands
            found, command_text = await asyncio.to_thread(
                personal_commands.for_task, self._personas.commands, user_id, chosen.persona_id,
                command, question, self.run_options()['modes'])
            if found.mode is not None:
                execution_mode = found.mode
            if found.write is False:
                allow_writes = False
        choices, used, skipped = self.persona_defaults(chosen, execution_mode, allow_writes, use_memory, workspace)
        if found is not None and found.write is True and choices[1] is False:
            raise personal_commands.needs_write(found)
        notes = [{'type': 'persona', 'persona_id': chosen.persona_id, 'name': chosen.name,
                  'chars': chosen.chars, 'used': used, 'skipped': skipped}]
        if found is not None:
            notes.append({'type': 'command', 'name': found.name, 'chars': found.chars,
                          'fixed': found.fixed, 'mode': found.mode, 'write': found.write})
        return chosen, notes, command_text, choices

    def persona_defaults(self, persona, execution_mode=None, allow_writes=None, use_memory=None, workspace=None):
        """Fill the choices a request left open from the persona (the request's own win).
        A default this service cannot honour is skipped and reported, never fatal.
        Returns ((execution_mode, allow_writes, use_memory, workspace), used, skipped)."""
        used, skipped = [], []
        defaults = persona.options if persona is not None else {}
        if execution_mode is None and 'mode' in defaults:
            if defaults['mode'] in self.run_options()['modes']:
                execution_mode = defaults['mode']
                used.append('mode')
            else:
                skipped.append('mode')
        if allow_writes is None and 'write' in defaults:
            allow_writes = defaults['write']
            used.append('write')
        if use_memory is None and 'use_memory' in defaults:
            if defaults['use_memory'] and self._longterm_memory is None:
                skipped.append('use_memory')
            else:
                use_memory = defaults['use_memory']
                used.append('use_memory')
        if workspace is None and 'workspace' in defaults:
            check = self._workspace_policy.check(defaults['workspace']) if self._workspace_servers else None
            if check is None or check.problem is not None or check.system_drive:
                skipped.append('workspace')
            else:
                workspace = check.path
                used.append('workspace')
        return (execution_mode, allow_writes, use_memory, workspace), used, skipped

    def check_workspace(self, path):
        """Whether a directory can be the working directory (workspace-001); nothing is listed."""
        if self._workspace_servers is None:
            raise ValueError('workspace choice is not available')
        return self._workspace_policy.check(path)

    async def save_attachment(self, session_id: str, name: str, text: str, *, user_id: str | None = None) -> dict:
        """Keep a text file the user dropped into the conversation (workspace-001); the model
        reads it with read_context_artifact. Needs the conversation archive."""
        self._validate_identity(user_id)
        self._validate_input('attachment', session_id)
        if not self._memory_directory:
            raise ValueError('attachments need the conversation archive')
        if not isinstance(name, str) or not name.strip():
            raise ValueError('attachment name must not be empty')
        name = name.replace('\\', '/').rsplit('/', 1)[-1].strip()[:200]
        if not isinstance(text, str) or not text or len(text) > MAX_ATTACHMENT_CHARS or '\x00' in text:
            raise ValueError(f'attachment must be text of 1 to {MAX_ATTACHMENT_CHARS} characters')
        root, label = self._archive_root(user_id), self._archive_label(user_id, session_id)

        def save():
            return ConversationArchive(root, label).save_text(text, kind='upload', metadata={'name': name})
        ref = await asyncio.to_thread(save)
        return {'ref': ref, 'name': name, 'chars': len(text)}

    def task_options(self, session_id: str, task_id: str, *, user_id: str | None = None) -> dict | None:
        """The run options a live blocked task started with; a resume keeps them (D-051)."""
        options = self._task_options.get((self._session_key(user_id, session_id), task_id))
        return dict(options) if options is not None else None

    def _forget_task(self, key, task_id) -> None:
        self._tasks.pop((key, task_id), None)
        self._routes.pop((key, task_id), None)
        self._task_options.pop((key, task_id), None)

    @staticmethod
    def _validate_input(question: str, session_id: str) -> None:
        if not isinstance(question, str) or not question.strip():
            raise ValueError("question must not be empty")
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session_id must not be empty")

    async def query_result(
        self, question: str, session_id: str, *, user_id: str | None = None,
        request_id: str | None = None, resume_task_id: str | None = None,
        question_id: str | None = None, on_event=None,
        execution_mode: str | None = None, allow_writes: bool | None = None, use_memory: bool | None = None,
        single_runner: str | None = None,
        hooks=None, workspace: str | None = None, workspace_confirmed: bool = False,
        attachments=None, persona: str | None = None, command: str | None = None,
        output_schema: dict | None = None,
    ) -> AgentRunResult:
        """on_event, if given, receives host-authored progress records ('route',
        then LoopRun's 'plan' and 'step'); see LoopRun.run (D-045).

        execution_mode, single_runner, allow_writes and use_memory choose for this request only; None keeps the
        service default (D-051). allow_writes=False hides the write and run tools as for a read-only
        task (D-027); use_memory=False skips long-term recall. A resumed task keeps its own choices.

        persona names the persona (its id) for this request only; None uses the session's. A new
        task takes the persona's instructions as a constraint and its defaults for the choices left
        open (docs/persona_plan.md); a resumed task keeps what it started with.

        command names a personal command of the task's persona, question being the whole line; see
        _persona_choices (docs/personal_commands_plan.md)."""
        if persona is not None and (self._personas is None or resume_task_id is not None):
            raise ValueError('persona needs personas on and a new task')
        if command is not None and (self._personas is None or resume_task_id is not None):
            raise ValueError('command needs personas on and a new task')
        if output_schema is not None and resume_task_id is not None:
            raise ValueError('output_schema is for a new task, not a resume')
        workspace, attachments, output_contract = self._check_request(
            question, session_id, user_id, request_id, resume_task_id, question_id, execution_mode,
            allow_writes, use_memory, single_runner, hooks, workspace, workspace_confirmed, attachments,
            output_schema)
        stamp = datetime.now(timezone.utc)
        key = self._session_key(user_id, session_id)
        async with self._locks.setdefault(key, asyncio.Lock()):
            conversation = await self._session_conversation(key, user_id, session_id)
            turn_id = f'request:{request_id}' if request_id is not None else str(uuid4())
            outcome_key = (key, turn_id)
            replayed = await self._replay(conversation, turn_id, outcome_key, question, resume_task_id, question_id)
            if replayed is not None:
                return replayed
            chosen, notes, command_text = None, (), None
            if resume_task_id is None and self._personas is not None:
                chosen, notes, command_text, (execution_mode, allow_writes, use_memory, workspace) = (
                    await self._persona_choices(user_id, session_id, persona, command, question,
                                                (execution_mode, allow_writes, use_memory, workspace)))
            runtime, memory, options, question_id = self._open_task(
                key, user_id, session_id, question, resume_task_id, question_id,
                execution_mode, single_runner, allow_writes, use_memory, workspace,
                output_contract=output_contract)
            task = memory.task_state
            turn = ConversationTurn([ConversationMessage(MessageRole.USER, question, stamp)], turn_id, stamp,
                                    task_id=task.task_id, status=TurnStatus.RECEIVED, question_id=question_id)
            # Every external call is after the original input has been published.
            await self._publish_input(conversation, turn, turn_id, outcome_key)
            try:
                for note in notes:
                    _emit(on_event, note)
                context_manager = await self._prepare_task(
                    conversation, key, user_id, question, task, runtime, question_id, attachments, options,
                    chosen, command_text, output_contract=output_contract, on_event=on_event)
                result = await self._execute(
                    conversation, key, user_id, session_id, question, hooks, on_event, outcome_key,
                    task, memory, runtime, options, context_manager, output_contract=output_contract)
            except StaleExecutionError:
                await self._settle_stale(conversation, outcome_key)
                raise
            except (Exception, asyncio.CancelledError) as exc:
                await self._settle_failure(conversation, outcome_key, key, task, exc)
                raise
            await _durable(self._publish_outcome(conversation, outcome_key))
            return result

    def _check_request(self, question, session_id, user_id, request_id, resume_task_id, question_id,
                       execution_mode, allow_writes, use_memory, single_runner, hooks, workspace,
                       workspace_confirmed, attachments, output_schema=None):
        """Validate one request before any lock or I/O; returns workspace, attachments and contract."""
        if self._closed:
            raise RuntimeError('service is closed')
        self._validate_input(question, session_id)
        self._validate_identity(user_id)
        if execution_mode is not None and execution_mode not in self.run_options()['modes']:
            raise ValueError('execution mode is not available')
        output_contract = None
        if output_schema is not None:
            from app.agent.output_contract import OutputContract
            output_contract = OutputContract(output_schema)
            if execution_mode == 'team' or (
                    execution_mode is None and self._execution_mode == 'team' and self._personas is None):
                raise ValueError('output_schema is not supported in team mode')
        if single_runner is not None:
            _check_runner(single_runner)
        _check_choice('allow_writes', allow_writes)
        _check_choice('use_memory', use_memory)
        _check_choice('workspace_confirmed', workspace_confirmed)
        if workspace is not None:
            if self._workspace_servers is None:
                raise ValueError('workspace choice is not available')
            # Checked here again whatever the caller checked (D-074); raises WorkspaceError.
            workspace = self._workspace_policy.require(workspace, confirmed=bool(workspace_confirmed))
        attachments = tuple(attachments or ())
        if len(attachments) > MAX_ATTACHMENTS or not all(isinstance(r, str) and r for r in attachments):
            raise ValueError(f'at most {MAX_ATTACHMENTS} attachment references')
        if attachments and resume_task_id is not None:
            # A resumed loop continues its own messages and would not see a new pinned note.
            raise ValueError('attachments go with a new message, not with an answer')
        if use_memory and self._longterm_memory is None:
            raise ValueError('long-term memory is not configured')
        for name, value in (('request_id', request_id), ('resume_task_id', resume_task_id), ('question_id', question_id)):
            if value is not None and (not isinstance(value, str) or not value.strip() or len(value) > 256):
                raise ValueError(f'{name} must be a nonempty string of at most 256 characters')
        if question_id is not None and resume_task_id is None:
            raise ValueError('question_id requires resume_task_id')
        from app.agent.hooks import HookSet
        if hooks is not None and not isinstance(hooks, HookSet):
            raise TypeError('hooks must be a HookSet')
        return workspace, attachments, output_contract

    async def _session_conversation(self, key, user_id, session_id):
        """The session's conversation, loaded (and its archive restored) on first use."""
        conversation = self._conversations.get(key)
        if conversation is None:
            label = self._archive_label(user_id, session_id)
            archive = None
            if self._memory_directory:
                archive = await asyncio.to_thread(
                    ConversationArchive, self._archive_root(user_id), label
                )
            conversation = ConversationMemory(label, compressor=self._conversation_compressor, archive=archive,
                                              background_compression=archive is not None)
            await conversation.restore()
            self._conversations[key] = conversation
            # Reconstruct old backlog on session load, including restart.
            self._notify_summary(key)
        return conversation

    async def _replay(self, conversation, turn_id, outcome_key, question, resume_task_id, question_id):
        """The archived result of a repeated request, or None for a new turn; raises on a conflict."""
        try:
            existing = await conversation.read_turn(turn_id)
        except KeyError:
            existing = None
        if existing is None:
            return None
        if (existing.messages[0].content != question or existing.status is None
                or (resume_task_id is None) != (existing.question_id is None)
                or (resume_task_id is not None and existing.task_id != resume_task_id)
                or (question_id is not None and existing.question_id != question_id)):
            raise ValueError('request ID conflict')
        if outcome_key in self._pending_outcomes:
            await _durable(self._publish_outcome(conversation, outcome_key))
            existing = await conversation.read_turn(turn_id)
        payload = await conversation.turn_result(turn_id)
        if payload is not None:
            restored = _restore_result(payload)
            if (restored.task_id != existing.task_id or restored.status.value != existing.status.value
                    or restored.content != existing.messages[-1].content):
                raise ValueError('archived result conflicts with turn')
            return restored
        raise RuntimeError(f'request already {existing.status.value}; automatic execution replay is unsafe')

    def _open_task(self, key, user_id, session_id, question, resume_task_id, question_id,
                   execution_mode, single_runner, allow_writes, use_memory, workspace,
                   output_contract=None):
        """The blocked task to resume (and its question) or a new task, with its run options."""
        runtime = None
        if resume_task_id is not None:
            runtime = self._tasks.get((key, resume_task_id))
            if runtime is None or runtime.memory.task_state.status is not TaskStatus.BLOCKED:
                raise ValueError('No resumable task in this user/session; restart restores history, not live tasks')
            task = runtime.memory.task_state
            questions = [q for q in task.scratchpad.open_questions
                         if q.goal_revision == task.goal_revision and q.blocking and q.resolved_at is None]
            if question_id is None:
                if len(questions) != 1:
                    raise ValueError('question_id is required when there is not exactly one blocking question')
                question_id = questions[0].question_id
            if not any(q.question_id == question_id for q in questions):
                raise ValueError('question_id is not an unresolved blocking question of this task')
            memory = runtime.memory
            options = self._task_options.get((key, resume_task_id))
        else:
            memory = None
            options = None
        if options is None:
            options = {'mode': execution_mode or self._execution_mode,
                       'runner': single_runner or self._single_runner,
                       'write': True if allow_writes is None else allow_writes,
                       'memory': (self._longterm_memory is not None) if use_memory is None else use_memory}
            if self._workspace_servers is not None:
                options['workspace'] = workspace       # None: the startup directory and its servers
        if runtime is None and output_contract is not None and options['mode'] == 'team':
            raise ValueError('output_schema is not supported in team mode')
        if memory is None:
            schema = output_contract.schema if output_contract is not None else None
            memory = RunMemory(RunState(user_id or session_id, question, output_schema=schema))
        return runtime, memory, options, question_id

    async def _publish_input(self, conversation, turn, turn_id, outcome_key):
        """Persist the original input; a failure after publication still settles its turn."""
        try:
            await _durable(conversation.add(turn))
        except asyncio.CancelledError:
            try:
                await conversation.read_turn(turn_id)
            except KeyError:
                raise
            self._pending_outcomes[outcome_key] = (TurnStatus.CANCELLED, 'Request cancelled before execution', None)
            await _durable(self._publish_outcome(conversation, outcome_key))
            raise
        except Exception as exc:
            # Publication may have succeeded before a cache reload failed.
            # Preserve that accepted input, but never start external work.
            try:
                await conversation.read_turn(turn_id)
            except KeyError:
                pass
            else:
                self._pending_outcomes[outcome_key] = (TurnStatus.FAILED, f'Input persistence failed: {type(exc).__name__}', None)
                await _durable(self._publish_outcome(conversation, outcome_key))
            raise

    async def _prepare_task(self, conversation, key, user_id, question, task, runtime, question_id,
                            attachments, options, persona=None, command_text=None, output_contract=None,
                            on_event=None):
        """The task's context manager, with the answer, attachments, workspace note, history and recall
        pinned, and a new task's persona instructions, project instructions, personal command and
        output contract added as constraints."""
        main = config.model_profiles().role('main')      # the default profile without a file (4d-2)
        context_manager = ContextManager(
            window_tokens=main.context_tokens,
            output_tokens=main.max_output_tokens,
            safety_tokens=config.context_safety_tokens, archive=conversation.archive,
        )
        if runtime is not None:
            task.resolve_question(question_id, question)
        if runtime is None and persona is not None and persona.instructions:
            from app.services.personas import PERSONA_PREAMBLE
            task.add_constraint(PERSONA_PREAMBLE + '\n\n' + persona.instructions)
        current = options.get('workspace') or self._default_workspace
        if runtime is None and self._instructions_trust is not None and current:
            from app.services.project_instructions import find_instructions
            found = await asyncio.to_thread(
                find_instructions, current, self._closed_folders(), self._instructions_trust)
            if found is not None:
                _emit(on_event, found.event())
                if found.status == 'loaded':
                    task.add_constraint(found.constraint_text())
        if runtime is None and command_text:
            task.add_constraint(command_text)
        if runtime is None and output_contract is not None:
            task.add_constraint(output_contract.constraint_text())
        if attachments:
            task.pin_content(await self._attachment_note(conversation, attachments), 'attachments')
        previous = self._session_workspaces.get(key)
        if runtime is None and previous is not None and previous != current:
            task.pin_content(WORKSPACE_CHANGED_NOTE, 'workspace_note')
        if runtime is None and current:
            self._session_workspaces[key] = current
        if runtime is None:
            history = await context_manager.prepare_conversation(conversation, task.active_goal)
            task.pin_content(history, 'conversation_memory')
            if self._longterm_memory is not None and options['memory']:
                try:
                    # With personas on, recall the shared memories plus this persona's own; the
                    # default persona sees the shared ones only (docs/persona_memory_plan.md).
                    scope = {}
                    if self._personas is not None and persona is not None:
                        from app.services.personas import DEFAULT_ID
                        scope = {'persona_scope': None if persona.persona_id == DEFAULT_ID else persona.persona_id}
                    async with asyncio.timeout(self._longterm_timeout_seconds):
                        hits = await self._longterm_memory.recall(
                            user_id=user_id, query=question, limit=self._longterm_recall_limit, **scope)
                    payload = context_manager.longterm_context(
                        hits=hits, user_id=user_id, token_budget=config.longterm_context_tokens)
                    if json.loads(payload)['entries']:
                        task.pin_content(payload, 'longterm_memory')
                except Exception as exc:
                    logger.warning('Long-term recall unavailable: %s', type(exc).__name__)
        return context_manager

    async def _execute(self, conversation, key, user_id, session_id, question, hooks, on_event, outcome_key,
                       task, memory, runtime, options, context_manager, output_contract=None):
        """Open the models and tools, run the task through the assembler and record its outcome."""
        cache_key = self._prompt_cache_key(self._archive_label(user_id, session_id))
        workspace = options.get('workspace') or self._default_workspace
        run_hooks, audit = await self._run_hooks(key, conversation, hooks, workspace=workspace)
        async with self._role_models(cache_key, run_hooks) as models:
            model = models.main
            def _on_user_mcp_unavailable(server_name: str, error_type: str) -> None:
                _emit(on_event, {
                    'type': 'mcp_server', 'server': server_name,
                    'result': 'unavailable', 'error': error_type,
                })
            async with open_tool_registry(
                memory, mcp_servers=self._servers_for(options),
                user_servers=self._user_mcp_servers,
                on_user_server_unavailable=_on_user_mcp_unavailable if self._user_mcp_servers else None,
                conversation=conversation,
                knowledge_search=self._knowledge_search, include_knowledge=self._include_knowledge,
                local_tools=(ContextArchiveTool(conversation.archive, context_manager.tool_result_chars),)
                            if conversation.archive else (),
                context_manager=context_manager,
            ) as registry:
                runtime, result = await self._run_assembler.run(
                    key=key, task=task, question=question, options=options, runtime=runtime,
                    model=model, memory=memory, registry=registry,
                    context_manager=context_manager, audit=audit, on_event=on_event,
                    loop_run_factory=LoopRun, route_model=models.route,
                    member=self._member_role(models, 'member', conversation),
                    explorer=self._member_role(models, 'explorer', conversation),
                    output_contract=output_contract,
                )
                if key in self._spent_tokens:
                    self._spent_tokens[key] += _total_tokens(result.usage)
                # Retain the result before resource cleanup or durable completion can fail.
                self._pending_outcomes[outcome_key] = (TurnStatus(result.status.value), result.content, asdict(result))
                if result.status is TaskStatus.BLOCKED:
                    self._tasks[(key, task.task_id)] = runtime
                else:
                    self._forget_task(key, task.task_id)
        return result

    async def _settle_stale(self, conversation, outcome_key):
        # This interaction no longer owns task state. Finish only its
        # own durable turn; do not fail or remove the newer execution.
        self._pending_outcomes[outcome_key] = (TurnStatus.FAILED,
            'Execution superseded; stale output was not committed', None)
        await _durable(self._publish_outcome(conversation, outcome_key))

    async def _settle_failure(self, conversation, outcome_key, key, task, exc):
        if outcome_key not in self._pending_outcomes:
            status = TurnStatus.CANCELLED if isinstance(exc, asyncio.CancelledError) else TurnStatus.FAILED
            content = ('Request cancelled; tool side effects may already exist' if status is TurnStatus.CANCELLED
                       else f'Request failed: {type(exc).__name__}')
            if task.status not in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                task.fail_task(content)
            self._forget_task(key, task.task_id)
            self._pending_outcomes[outcome_key] = (status, content, None)
        await _durable(self._publish_outcome(conversation, outcome_key))

    def _archive_root(self, user_id: str | None) -> Path:
        root = Path(self._memory_directory)
        return root / "user-scoped" if user_id is not None else root

    def _resumable(self, key, turn: ConversationTurn) -> bool:
        return turn.status is TurnStatus.BLOCKED and turn.task_id is not None and self._live_blocked(key, turn.task_id)

    def _live_blocked(self, key, task_id: str) -> bool:
        runtime = self._tasks.get((key, task_id))
        return runtime is not None and runtime.memory.task_state.status is TaskStatus.BLOCKED

    def resumable(self, session_id: str, task_id: str, *, user_id: str | None = None) -> bool:
        """Whether this process still holds that blocked task, so an answer can resume it (D-046)."""
        return self._live_blocked(self._session_key(user_id, session_id), task_id)

    async def list_sessions(self, *, user_id: str | None = None, limit: int = 50) -> list[dict]:
        """This user's sessions, newest first, with the latest user message (D-045)."""
        self._validate_identity(user_id)
        _check_limit(limit)
        sessions = []
        if self._memory_directory:
            root = self._archive_root(user_id)
            for label in await asyncio.to_thread(ConversationArchive.session_ids, root):
                if user_id is None:
                    session_id = label
                else:
                    try:
                        kind, owner, session_id = json.loads(label)
                    except (ValueError, TypeError):
                        continue
                    if kind != 'user' or owner != user_id or self._archive_label(user_id, session_id) != label:
                        continue
                try:
                    turns = await asyncio.to_thread(ConversationArchive(root, label).load_turns, 1)
                except ValueError as exc:
                    logger.warning('Skipping unreadable session archive: %s', type(exc).__name__)
                    continue
                if turns:
                    sessions.append((session_id, turns[-1]))
        else:
            for key, conversation in self._conversations.items():
                if user_id is None:
                    session_id = key if isinstance(key, str) else None
                else:
                    session_id = key[2] if isinstance(key, tuple) and key[1] == user_id else None
                turns = conversation.retrieve_turns(1) if session_id is not None else []
                if turns:
                    sessions.append((session_id, turns[-1]))
        sessions.sort(key=lambda item: item[1].finished_at or item[1].created_at, reverse=True)
        return [{'session_id': session_id,
                 'updated_at': (turn.finished_at or turn.created_at).isoformat(),
                 'last_message': turn.messages[0].content[:200]}
                for session_id, turn in sessions[:limit]]

    async def session_turns(self, session_id: str, *, user_id: str | None = None, limit: int = 20) -> list[dict]:
        """The latest turns of one session, oldest first; an unknown session has none (D-045)."""
        self._validate_identity(user_id)
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session_id must not be empty")
        _check_limit(limit)
        key = self._session_key(user_id, session_id)
        if self._memory_directory:
            root, label = self._archive_root(user_id), self._archive_label(user_id, session_id)
            if not await asyncio.to_thread(ConversationArchive.exists, root, label):
                return []
            turns = await asyncio.to_thread(ConversationArchive(root, label).load_turns, limit)
        else:
            conversation = self._conversations.get(key)
            turns = conversation.retrieve_turns(limit) if conversation is not None else []
        return [_turn_view(turn, self._resumable(key, turn)) for turn in turns]

    async def task_traces(self, session_id: str, task_id: str, *, user_id: str | None = None) -> list[dict]:
        """The traces of one task's runs, oldest first, from the session's audit files (trace-001,
        D-073). A blocked task and its resume are two runs. Without an archive, an audit file or a
        matching run the list is empty. Read-only: no run path changes."""
        self._validate_identity(user_id)
        for name, value in (('session_id', session_id), ('task_id', task_id)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must not be empty")
        if not self._memory_directory:
            return []
        root, label = self._archive_root(user_id), self._archive_label(user_id, session_id)
        suffix = '-' + (re.sub(r'[^0-9A-Za-z_-]', '', task_id)[:64] or 'task')

        def read():
            if not ConversationArchive.exists(root, label):
                return []
            archive = ConversationArchive(root, label)
            from app.agent.trace_view import build_trace
            traces = []
            for name in archive.list_audits():
                if not name.endswith(suffix):
                    continue
                try:
                    records, skipped = archive.load_audit(name)
                except (KeyError, ValueError, OSError):
                    continue
                trace = build_trace(records)
                if trace.get('task_id') not in (None, task_id):
                    continue
                trace['skipped_lines'] = skipped
                if skipped:
                    trace['complete'] = False
                traces.append(trace)
            return sorted(traces, key=lambda t: str(t.get('started_at') or ''))
        return await asyncio.to_thread(read)

    def _servers_for(self, options):
        """The tool servers of one run: the chosen working directory's, or the fixed ones."""
        if self._workspace_servers is not None and options.get('workspace'):
            return tuple(self._workspace_servers(options['workspace'], bool(options['write'])))
        return self._mcp_servers

    async def _attachment_note(self, conversation, refs):
        """A host note naming the files the user attached; the texts stay in the archive."""
        if conversation.archive is None:
            raise ValueError('attachments need the conversation archive')
        files = []
        for ref in refs:
            info = await asyncio.to_thread(conversation.archive.artifact_info, ref)   # foreign refs raise
            if info['kind'] != 'upload':
                raise ValueError('not an attachment reference')
            files.append({'name': info['metadata'].get('name'), 'ref': ref, 'chars': info['chars']})
        return json.dumps({'attachments': files,
                           'how_to_read': 'The user attached these text files to this message. Read them '
                                          'with read_context_artifact(ref), in pieces when long.'},
                          ensure_ascii=False)

    async def _publish_outcome(self, conversation, outcome_key):
        status, content, payload = self._pending_outcomes[outcome_key]
        await conversation.complete_turn(outcome_key[1], status, content, result=payload)
        self._pending_outcomes.pop(outcome_key, None)
        self._notify_summary(outcome_key[0])

    def _notify_summary(self, key):
        try:
            self._summary_scheduler.notify(key)
        except Exception as exc:
            # Scheduling is an optimization over durable history, never part
            # of the accepted request's execution outcome.
            logger.warning('Summary scheduling unavailable: %s', type(exc).__name__)

    async def __aenter__(self):
        if self._closed:
            raise RuntimeError('service is closed')
        return self

    async def __aexit__(self, *exc):
        await self.aclose()

    async def aclose(self, *, timeout_seconds=5.0):
        self._closed = True
        await self._summary_scheduler.aclose(timeout_seconds=timeout_seconds)

    async def query(self, question: str, session_id: str, *, user_id: str | None = None,
                    request_id: str | None = None, resume_task_id: str | None = None,
                    question_id: str | None = None) -> str:
        return (await self.query_result(question, session_id, user_id=user_id, request_id=request_id,
                                       resume_task_id=resume_task_id, question_id=question_id)).content

    async def query_stream(self, question: str, session_id: str, *, user_id: str | None = None,
                           request_id: str | None = None, resume_task_id: str | None = None,
                           question_id: str | None = None) -> AsyncIterator[dict[str, Any]]:
        try:
            result = await self.query_result(question, session_id, user_id=user_id, request_id=request_id,
                                             resume_task_id=resume_task_id, question_id=question_id)
            yield {"type": "content", "data": result.content, "node": "agent_runtime"}
            terminal = "complete" if result.status is TaskStatus.COMPLETED else (
                "blocked" if result.status is TaskStatus.BLOCKED else "error"
            )
            yield {"type": terminal, "status": result.status.value, "data": result.content}
        except Exception as exc:
            yield {"type": "error", "data": f"{type(exc).__name__}: {exc}"}
