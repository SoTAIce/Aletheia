"""User-configured stdio MCP servers (D-095, docs/user_mcp_plan.md).

Configuration is loaded from MCP_SERVERS_FILE (default mcp.toml) at startup, confirmed once
per configuration fingerprint (persisted in data/mcp_confirmed.json), and probed before the
service starts. Secrets from the host process environment are never passed unless explicitly
listed and allowed, and never appear in errors, fingerprints or display text.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time
import tomllib
from typing import Any

from mcp import StdioServerParameters

from app.config import PROJECT_ROOT
from app.tools.mcp_client import MCPClient

MAX_SERVERS = 10
MAX_TOOLS_PER_SERVER = 30
MAX_TOOL_NAME_CHARS = 64
MAX_TOOL_DESCRIPTION_CHARS = 1000
DEFAULT_TIMEOUT_SECONDS = 30
MIN_TIMEOUT_SECONDS = 1
MAX_TIMEOUT_SECONDS = 300
PROBE_TOTAL_TIMEOUT_SECONDS = 30.0

USER_MCP_EFFECTS = ('read', 'external')
FORBIDDEN_ENV_NAMES = frozenset({
    'DASHSCOPE_API_KEY',
    'TAVILY_API_KEY',
    'AGENT_MODEL_API_KEY',
    'WEB_ACCESS_TOKEN',
    'ALETHEIA_TEST_API_KEY',
})
DEFAULT_CONFIRM_PATH = PROJECT_ROOT / 'data' / 'mcp_confirmed.json'

_SERVER_NAME_RE = re.compile(r'^[a-z][a-z0-9_]{0,19}$')
_PREFIXED_TOOL_NAME_RE = re.compile(r'^[A-Za-z0-9_-]{1,64}$')
_SERVER_KEYS = frozenset({
    'command', 'args', 'env', 'cwd', 'timeout_seconds', 'tools', 'enabled', 'effects',
})


class UserMcpError(ValueError):
    """A user MCP configuration or startup check error; Chinese message, never env values."""


@dataclass(frozen=True, slots=True)
class UserMcpServer:
    name: str
    command: str                                # resolved executable path
    args: tuple[str, ...] = ()
    env: tuple[str, ...] = ()                   # allowed environment variable names
    cwd: str = ''                               # resolved absolute working directory
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    tools: tuple[str, ...] | None = None        # None: accept up to MAX_TOOLS_PER_SERVER
    enabled: bool = True
    effects: tuple[tuple[str, str], ...] = ()   # sorted (tool_name, 'read' | 'external')

    def effect_for(self, tool_name: str) -> str:
        """Configured effect for a raw tool name; anything not declared 'read' is 'external'."""
        for name, effect in self.effects:
            if name == tool_name:
                return 'read' if effect == 'read' else 'external'
        return 'external'

    def effects_dict(self) -> dict[str, str]:
        return dict(self.effects)

    def fingerprint(self) -> str:
        """SHA-256 of canonical JSON over executable, args, cwd, sorted env names, tools,
        effects and timeout_seconds. Never contains env values."""
        payload = {
            'command': self.command,
            'args': list(self.args),
            'cwd': self.cwd,
            'env': sorted(self.env),
            'tools': list(self.tools) if self.tools is not None else None,
            'effects': dict(self.effects),
            'timeout_seconds': self.timeout_seconds,
        }
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(canonical.encode('utf-8')).hexdigest()

    def to_stdio_params(self, environ=None) -> StdioServerParameters:
        """Build StdioServerParameters passing only the listed env names from environ."""
        source = os.environ if environ is None else environ
        env_map: dict[str, str] = {}
        for var in self.env:
            value = source.get(var)
            if not value:
                raise UserMcpError(f'用户服务 {self.name} 需要的环境变量 {var} 未设置')
            env_map[var] = value
        return StdioServerParameters(
            command=self.command,
            args=list(self.args),
            cwd=self.cwd,
            env=env_map,
        )


@dataclass(frozen=True, slots=True)
class UserMcpConfig:
    servers: tuple[UserMcpServer, ...]
    source: Path | None = None

    @property
    def enabled_servers(self) -> tuple[UserMcpServer, ...]:
        return tuple(server for server in self.servers if server.enabled)

    def __len__(self) -> int:
        return len(self.servers)

    def __bool__(self) -> bool:
        return bool(self.servers)

    def __iter__(self):
        return iter(self.servers)


@dataclass(frozen=True, slots=True)
class SelectedUserTools:
    accepted: tuple[Any, ...]                   # mcp.types.Tool objects
    missing: tuple[str, ...]                    # listed in config.tools but absent from server
    invalid: tuple[str, ...]                    # prefixed name rejected by naming rules
    truncated: int = 0                          # valid tools dropped beyond MAX_TOOLS_PER_SERVER


@dataclass(frozen=True, slots=True)
class ServerProbe:
    name: str
    tools: tuple[tuple[str, str], ...]          # ((prefixed_name, effect), ...)
    invalid: tuple[str, ...] = ()
    truncated: int = 0


def select_user_mcp_tools(server: UserMcpServer, raw_tools) -> SelectedUserTools:
    """Filter, validate prefixed names, and cap a user MCP server's tool list."""
    # A server listing one name twice keeps the first: registering both would fail halfway and
    # leave the first registered against a server that is then closed.
    by_name: dict[str, Any] = {}
    for tool in raw_tools:
        by_name.setdefault(tool.name, tool)
    raw_tools = list(by_name.values())
    missing: list[str] = []
    if server.tools is not None:
        candidates = []
        for wanted in server.tools:
            if wanted in by_name:
                candidates.append(by_name[wanted])
            else:
                missing.append(wanted)
    else:
        candidates = list(raw_tools)

    accepted = []
    invalid: list[str] = []
    for tool in candidates:
        prefixed = f'{server.name}__{tool.name}'
        if _PREFIXED_TOOL_NAME_RE.fullmatch(prefixed):
            accepted.append(tool)
        else:
            invalid.append(prefixed)

    truncated = 0
    if server.tools is None and len(accepted) > MAX_TOOLS_PER_SERVER:
        truncated = len(accepted) - MAX_TOOLS_PER_SERVER
        accepted = accepted[:MAX_TOOLS_PER_SERVER]

    return SelectedUserTools(
        accepted=tuple(accepted),
        missing=tuple(missing),
        invalid=tuple(invalid),
        truncated=truncated,
    )


def _resolve_command(raw: Any, *, name: str) -> str:
    if not isinstance(raw, str) or not raw.strip() or '\x00' in raw:
        raise UserMcpError(f'用户服务 {name} 的 command 必须是非空字符串')
    cmd = raw.strip()
    found = shutil.which(cmd)
    if found is None:
        raise UserMcpError(f'用户服务 {name} 的 command 找不到可执行文件：{cmd}')
    return str(Path(found).resolve())


def _parse_args(raw: Any, *, name: str) -> tuple[str, ...]:
    if not isinstance(raw, list):
        raise UserMcpError(f'用户服务 {name} 的 args 必须是字符串列表')
    result: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise UserMcpError(f'用户服务 {name} 的 args 只能包含字符串')
        result.append(item)
    return tuple(result)


def _parse_cwd(raw: Any, *, name: str, base_dir: Path) -> str:
    if raw is None:
        return str(base_dir.resolve())
    if not isinstance(raw, str) or not raw.strip() or '\x00' in raw:
        raise UserMcpError(f'用户服务 {name} 的 cwd 必须是非空路径字符串')
    path = Path(raw.strip())
    if not path.is_absolute():
        raise UserMcpError(f'用户服务 {name} 的 cwd 必须是绝对路径')
    if not path.is_dir():
        raise UserMcpError(f'用户服务 {name} 的 cwd 目录不存在')
    return str(path.resolve())


def _parse_env(raw: Any, *, name: str, environ) -> tuple[str, ...]:
    if not isinstance(raw, list):
        raise UserMcpError(f'用户服务 {name} 的 env 必须是环境变量名列表')
    names: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip() or item != item.strip():
            raise UserMcpError(f'用户服务 {name} 的 env 必须是非空变量名字符串')
        if item in FORBIDDEN_ENV_NAMES:
            raise UserMcpError(f'用户服务 {name} 的 env 不能包含受保护变量 {item}')
        if not environ.get(item):
            raise UserMcpError(f'用户服务 {name} 需要的环境变量 {item} 未设置')
        names.append(item)
    return tuple(dict.fromkeys(names))


def _parse_tools(raw: Any, *, name: str) -> tuple[str, ...]:
    if not isinstance(raw, list) or not raw:
        raise UserMcpError(f'用户服务 {name} 的 tools 必须是非空字符串列表')
    cleaned: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip() or item != item.strip():
            raise UserMcpError(f'用户服务 {name} 的 tools 只能包含非空工具名')
        cleaned.append(item)
    return tuple(dict.fromkeys(cleaned))


def _parse_effects(raw: Any, *, name: str) -> tuple[tuple[str, str], ...]:
    if not isinstance(raw, dict):
        raise UserMcpError(f'用户服务 {name} 的 effects 必须是表结构')
    items: list[tuple[str, str]] = []
    for tool_name, effect in raw.items():
        if not isinstance(tool_name, str) or not tool_name.strip() or tool_name != tool_name.strip():
            raise UserMcpError(f'用户服务 {name} 的 effects 里有不合法的工具名')
        if not isinstance(effect, str) or effect not in USER_MCP_EFFECTS:
            raise UserMcpError(
                f'用户服务 {name} 的 effects.{tool_name} 取值不对（只认 {", ".join(USER_MCP_EFFECTS)}）'
            )
        items.append((tool_name, effect))
    items.sort(key=lambda pair: pair[0])
    return tuple(items)


def parse_user_mcp(
    data: dict,
    *,
    base_dir: str | Path,
    environ=None,
    source: Path | None = None,
) -> UserMcpConfig:
    """Validate a parsed TOML mapping and return UserMcpConfig."""
    if not isinstance(data, dict):
        raise UserMcpError('用户 MCP 配置文件内容必须是表结构')
    unknown_top = set(data) - {'servers'}
    if unknown_top:
        raise UserMcpError(
            f'用户 MCP 配置文件里有不认识的段：{", ".join(sorted(unknown_top))}（只认 [servers.<名字>]）'
        )
    raw_servers = data.get('servers', {})
    if not isinstance(raw_servers, dict):
        raise UserMcpError('用户 MCP 配置文件的 servers 必须是表结构（[servers.<名字>]）')
    if len(raw_servers) > MAX_SERVERS:
        raise UserMcpError(f'用户 MCP 服务最多 {MAX_SERVERS} 个（当前 {len(raw_servers)} 个）')

    env_source = os.environ if environ is None else environ
    base = Path(base_dir)
    servers: list[UserMcpServer] = []
    for name, body in raw_servers.items():
        if not isinstance(name, str) or not _SERVER_NAME_RE.fullmatch(name):
            raise UserMcpError(
                f'用户服务名 {name!r} 不合法（须以小写字母开头，只含小写字母、数字和下划线，最长 20 个字符）'
            )
        if not isinstance(body, dict):
            raise UserMcpError(f'用户服务 {name} 必须是一个表（[servers.{name}]）')
        extra = set(body) - _SERVER_KEYS
        if extra:
            raise UserMcpError(f'用户服务 {name} 有不认识的字段：{", ".join(sorted(extra))}')
        if 'command' not in body:
            raise UserMcpError(f'用户服务 {name} 缺少必填字段 command')
        enabled = body.get('enabled', True)
        if type(enabled) is not bool:
            raise UserMcpError(f'用户服务 {name} 的 enabled 必须是布尔值')
        command = _resolve_command(body['command'], name=name)
        args = _parse_args(body['args'], name=name) if 'args' in body else ()
        cwd = _parse_cwd(body.get('cwd'), name=name, base_dir=base)
        env = _parse_env(body['env'], name=name, environ=env_source) if 'env' in body else ()
        timeout = body.get('timeout_seconds', DEFAULT_TIMEOUT_SECONDS)
        if type(timeout) is not int or not (MIN_TIMEOUT_SECONDS <= timeout <= MAX_TIMEOUT_SECONDS):
            raise UserMcpError(
                f'用户服务 {name} 的 timeout_seconds 必须是 {MIN_TIMEOUT_SECONDS}～{MAX_TIMEOUT_SECONDS} 的整数'
            )
        tools = _parse_tools(body['tools'], name=name) if 'tools' in body else None
        effects = _parse_effects(body['effects'], name=name) if 'effects' in body else ()
        servers.append(UserMcpServer(
            name=name,
            command=command,
            args=args,
            env=env,
            cwd=cwd,
            timeout_seconds=timeout,
            tools=tools,
            enabled=enabled,
            effects=effects,
        ))
    return UserMcpConfig(servers=tuple(servers), source=source)


def load_user_mcp(path: str | Path, *, environ=None) -> UserMcpConfig:
    """Read and parse a user MCP TOML file; errors are Chinese UserMcpError."""
    source = Path(path).resolve()
    try:
        data = tomllib.loads(source.read_text(encoding='utf-8'))
    except FileNotFoundError:
        raise UserMcpError(f'找不到用户 MCP 配置文件 {source}') from None
    except (OSError, UnicodeDecodeError) as exc:
        raise UserMcpError(f'读不了用户 MCP 配置文件 {source}（{type(exc).__name__}）') from None
    except tomllib.TOMLDecodeError:
        raise UserMcpError(f'用户 MCP 配置文件 {source} 不是合法的 TOML') from None
    return parse_user_mcp(data, base_dir=source.parent, environ=environ, source=source)


def load_confirmed_fingerprints(confirm_path: str | Path | None = None) -> dict[str, str]:
    """Read stored {server_name: fingerprint} records; missing or corrupt files return {}."""
    target = DEFAULT_CONFIRM_PATH if confirm_path is None else Path(confirm_path)
    try:
        raw = json.loads(target.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)}


def _save_confirmed_fingerprints(records: dict[str, str], confirm_path: Path) -> None:
    confirm_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = confirm_path.with_name(confirm_path.name + '.tmp')
    tmp.write_text(json.dumps(records, ensure_ascii=False, sort_keys=True, indent=2) + '\n', encoding='utf-8')
    tmp.replace(confirm_path)


def _format_server_summary(server: UserMcpServer) -> str:
    lines = [
        f'即将启动用户 MCP 服务：{server.name}',
        f'  命令：{server.command}',
        f'  参数：{list(server.args) if server.args else "（无）"}',
        f'  工作目录：{server.cwd}',
        f'  环境变量名：{", ".join(server.env) if server.env else "（无）"}',
    ]
    return '\n'.join(lines)


def confirm_user_mcp(
    mcp_config: UserMcpConfig,
    *,
    confirmed: bool = False,
    confirm_path: str | Path | None = None,
    write=print,
    ask=input,
    interactive: bool | None = None,
) -> str | None:
    """Confirm each enabled user MCP server whose fingerprint is not yet stored (D-095).

    Returns a Chinese refusal reason, or None when every enabled server is confirmed.
    Confirmed fingerprints are saved to confirm_path (default data/mcp_confirmed.json).
    """
    enabled = mcp_config.enabled_servers
    if not enabled:
        return None
    target = DEFAULT_CONFIRM_PATH if confirm_path is None else Path(confirm_path)
    stored = load_confirmed_fingerprints(target)
    pending = [server for server in enabled if stored.get(server.name) != server.fingerprint()]
    if not pending:
        return None

    is_interactive = sys.stdin.isatty() if interactive is None else interactive
    updated = dict(stored)
    for server in pending:
        write(_format_server_summary(server))
        if not confirmed:
            if not is_interactive:
                return '有未确认的用户 MCP 服务，不能交互确认；确认要启动时加 --confirm-mcp。'
            try:
                answer = ask(f'允许启动用户服务 {server.name} 吗？输入 y 确认，其他任意输入取消：')
            except EOFError:
                answer = ''
            if answer.strip().lower() != 'y':
                return f'已取消：没有确认启动用户服务 {server.name}。'
        updated[server.name] = server.fingerprint()

    _save_confirmed_fingerprints(updated, target)
    return None


async def probe_user_mcp(
    mcp_config: UserMcpConfig,
    *,
    total_timeout: float = PROBE_TOTAL_TIMEOUT_SECONDS,
    environ=None,
) -> dict[str, ServerProbe]:
    """Start each enabled user MCP server once, list and verify its tools, then close it."""
    results: dict[str, ServerProbe] = {}
    deadline = time.monotonic() + float(total_timeout)
    for server in mcp_config.enabled_servers:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise UserMcpError(f'用户服务 {server.name} 启动检查失败（TimeoutError）')
        budget = min(remaining, float(server.timeout_seconds))
        try:
            async with asyncio.timeout(budget):
                params = server.to_stdio_params(environ=environ)
                async with MCPClient.connect(params, read_timeout_seconds=budget) as client:
                    raw_tools = await client.list_tools()
        except UserMcpError:
            raise
        except Exception as exc:
            raise UserMcpError(f'用户服务 {server.name} 启动检查失败（{type(exc).__name__}）') from None

        selected = select_user_mcp_tools(server, raw_tools)
        if selected.missing:
            raise UserMcpError(
                f'用户服务 {server.name} 缺少配置里指定的工具：{", ".join(selected.missing)}'
            )
        if not selected.accepted:
            raise UserMcpError(f'用户服务 {server.name} 没有可接入的合格工具')
        tools = tuple((f'{server.name}__{tool.name}', server.effect_for(tool.name)) for tool in selected.accepted)
        results[server.name] = ServerProbe(
            name=server.name,
            tools=tools,
            invalid=selected.invalid,
            truncated=selected.truncated,
        )
    return results


def format_user_mcp_config(
    mcp_config: UserMcpConfig,
    *,
    confirm_path: str | Path | None = None,
    probed: dict[str, ServerProbe] | None = None,
) -> str:
    """Chinese summary of user MCP servers for --show-mcp."""
    if not mcp_config.servers:
        return '用户 MCP 配置文件为空（0 个服务）。'
    stored = load_confirmed_fingerprints(confirm_path)
    lines = [f'用户 MCP 服务（共 {len(mcp_config.servers)} 个）：']
    for server in mcp_config.servers:
        is_confirmed = stored.get(server.name) == server.fingerprint()
        status = '启用' if server.enabled else '停用'
        confirm_text = '已确认' if is_confirmed else '未确认'
        tools_text = ', '.join(server.tools) if server.tools is not None else f'全部（最多 {MAX_TOOLS_PER_SERVER} 个）'
        effects_text = (', '.join(f'{k}={v}' for k, v in server.effects)
                        if server.effects else '（默认全部为 external）')
        lines += [
            f'[{server.name}] 状态：{status}｜确认：{confirm_text}｜单次调用时限：{server.timeout_seconds} 秒',
            f'  命令：{server.command}',
            f'  参数：{list(server.args) if server.args else "（无）"}',
            f'  工作目录：{server.cwd}',
            f'  环境变量名：{", ".join(server.env) if server.env else "（无）"}',
            f'  工具清单：{tools_text}',
            f'  属性表：{effects_text}',
        ]
        if probed and server.name in probed:
            info = probed[server.name]
            lines.append(f'  已探测工具（{len(info.tools)} 个）：')
            for tool_name, effect in info.tools:
                lines.append(f'    - {tool_name}（{effect}）')
            if info.invalid:
                lines.append(f'  未接入的不合格工具名：{", ".join(info.invalid)}')
            if info.truncated:
                lines.append(f'  超出上限未接入的工具数：{info.truncated}')
    return '\n'.join(lines)
