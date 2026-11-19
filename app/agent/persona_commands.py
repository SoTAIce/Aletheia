"""/persona and /btw in the interactive mode, and the line a task's persona event prints
(docs/persona_plan.md). Texts are Chinese and host-written; the user's own persona names and
instructions are shown only on their own terminal."""

from app.services.personas import DEFAULT_ID, OPTION_KEYS, PersonaError, parse_option_text

PERSONAS_OFF = '个性化人设没有开启：PERSONAS_DIR 是空的。'
PERSONA_HELP = ('人设：/persona 列出，/persona 名字 切换，/persona new 名字 新建，/persona show 查看，'
                '/persona set 键 值 改默认设置（值写 - 去掉），/persona rename 新名字，/persona delete 名字；'
                '/btw 内容 给当前人设加一行指令，/btw undo 撤掉上一次。')
OPTION_NAMES = {'mode': '执行模式', 'write': '写文件', 'use_memory': '长期记忆', 'workspace': '工作目录'}
SEPARATE_HINT = '前面的对话还在这个会话里；想完全分开，可以换一个 --session-id 启动新会话。'


def _value_text(value) -> str:
    if value is True:
        return '开'
    if value is False:
        return '关'
    return str(value)


def options_text(options: dict) -> str:
    return '，'.join(f'{OPTION_NAMES[k]} {_value_text(options[k])}' for k in OPTION_KEYS if k in options)


def persona_notice(event: dict) -> str | None:
    """The line a 'persona' progress event prints; None for the untouched default persona."""
    used, skipped = event.get('used') or [], event.get('skipped') or []
    if event.get('persona_id') == DEFAULT_ID and not event.get('chars') and not used and not skipped:
        return None
    line = f"本次任务用的人设：{event.get('name')}（指令 {event.get('chars', 0)} 字）。"
    if used:
        line += '用了它的默认设置：' + '、'.join(OPTION_NAMES.get(k, k) for k in used) + '。'
    if skipped:
        line += '这些默认设置这次用不了，已跳过：' + '、'.join(OPTION_NAMES.get(k, k) for k in skipped) + '。'
    return line


def _show(store, user_id, persona, write) -> None:
    write(f'人设「{persona.name}」，文件：{store.file_path(user_id, persona.persona_id)}')
    write('默认设置：' + (options_text(persona.options) or '没有'))
    if not persona.instructions:
        write('指令：还没有。用 /btw 加一行，或者直接编辑上面的文件。')
        return
    write(f'指令（{persona.chars} 字）：')
    for number, line in enumerate(persona.instructions.split('\n'), 1):
        write(f'{number:>3}  {line}')


def persona_scope(store, user_id, session_id):
    """(memory persona id, persona name) of the session's persona: None for the default one,
    whose memories are shared (docs/persona_memory_plan.md)."""
    persona = store.get(user_id, store.session_persona_id(user_id, session_id))
    return (None if persona.persona_id == DEFAULT_ID else persona.persona_id), persona.name


def memory_label(store, user_id, persona_id) -> str:
    """Whom a memory belongs to, for /memories and the web list."""
    if persona_id is None:
        return '公共'
    try:
        return store.get(user_id, persona_id).name
    except PersonaError:
        return '已删除的人设'


async def _delete(store, user_id, arg, write, memory, read_line) -> None:
    """/persona delete: its memories go with it, after a typed 'y' when it has any."""
    persona = store.find(user_id, arg)
    if persona.persona_id == DEFAULT_ID:
        store.delete(user_id, persona.persona_id)        # raises: the default persona stays
    deleted = 0
    if memory is not None:
        try:
            count = await memory.count_persona(user_id or 'local', persona.persona_id)
        except Exception as exc:
            write(f'查不到这个人设的记忆：{type(exc).__name__}，人设没有删除。')
            return
        if count:
            write(f'人设「{persona.name}」有 {count} 条记忆，会一起删除，删了找不回来。')
            try:
                answer = await (read_line or _no_answer)('输入 y 确认，其他任意输入取消：')
            except EOFError:
                answer = ''
            if answer.strip().lower() != 'y':
                write('已取消，人设和记忆都没有删除。')
                return
            try:
                deleted = await memory.forget_persona(user_id or 'local', persona.persona_id)
            except Exception as exc:
                write(f'删除记忆失败：{type(exc).__name__}，人设没有删除。')
                return
    store.delete(user_id, persona.persona_id)
    if deleted:
        write(f'已删除人设「{persona.name}」和它的 {deleted} 条记忆，用它的会话回到“默认”。')
    else:
        write(f'已删除人设「{persona.name}」，用它的会话回到“默认”。')


async def _no_answer(prompt):
    raise EOFError


async def persona_command(command: str, service, user_id, session_id: str, write=print, waiting=None,
                          *, memory=None, read_line=None) -> None:
    """Run one /persona or /btw line against the session's persona. memory, when on, is the
    long-term memory service: deleting a persona deletes its memories (persona-memory)."""
    store = service.personas
    if store is None:
        write(PERSONAS_OFF)
        return
    name, _, rest = command.partition(' ')
    rest = rest.strip()
    later = '（等待回答的任务仍用原来的人设）' if waiting else ''
    try:
        current_id = store.session_persona_id(user_id, session_id)
        if name == '/btw':
            if rest == 'undo':
                persona = store.undo_btw(user_id, current_id)
                write(f'已撤掉上一次 /btw，「{persona.name}」的指令现在 {persona.chars} 字。')
            else:
                persona = store.btw(user_id, current_id, rest)
                write(f'已加到「{persona.name}」的指令里，共 {persona.chars} 字，下个任务起生效。{later}')
            return
        sub, _, arg = rest.partition(' ')
        arg = arg.strip()
        if not rest:
            write('人设（* 是这个会话用的）：')
            for entry in store.list_personas(user_id):
                mark = '*' if entry['persona_id'] == current_id else ' '
                if entry.get('problem'):
                    write(f"{mark} （{entry['persona_id']}）文件有问题：{entry['message']}")
                    continue
                extra = options_text(entry['options'])
                write(f"{mark} {entry['name']}  指令 {entry['chars']} 字" + (f'  默认设置：{extra}' if extra else ''))
        elif sub == 'new':
            persona = store.create(user_id, arg)
            store.bind(user_id, session_id, persona.persona_id)
            write(f'已新建人设「{persona.name}」，这个会话下个任务起用它。用 /btw 加指令。{later}')
        elif sub == 'show':
            _show(store, user_id, store.get(user_id, current_id), write)
        elif sub == 'set':
            key, _, value = arg.partition(' ')
            value = value.strip()
            if not key or not value:
                write('用法：/persona set 键 值，键是 ' + '、'.join(OPTION_KEYS) + '；值写 - 表示去掉。')
                return
            option = None if value == '-' else parse_option_text(key, value)
            persona = store.update(user_id, current_id, options={key: option})
            write(f'已更新「{persona.name}」的默认设置：' + (options_text(persona.options) or '没有') + f'。{later}')
        elif sub == 'rename':
            persona = store.update(user_id, current_id, name=arg)
            write(f'已改名为「{persona.name}」。')
        elif sub == 'delete':
            await _delete(store, user_id, arg, write, memory, read_line)
        else:
            persona = store.find(user_id, rest)
            if persona.persona_id == current_id:
                write(f'这个会话已经在用「{persona.name}」。')
                return
            store.bind(user_id, session_id, persona.persona_id)
            write(f'已切换到「{persona.name}」，下个任务起生效。{later}{SEPARATE_HINT}')
    except PersonaError as exc:
        write(exc.message)
    except OSError as exc:
        write(f'人设操作失败：{type(exc).__name__}')
