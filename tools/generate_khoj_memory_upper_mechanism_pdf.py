from __future__ import annotations

import ast
import math
from pathlib import Path
from typing import Iterable

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    KeepTogether,
    PageBreak,
    PageTemplate,
    Paragraph,
    Preformatted,
    Spacer,
    Table,
    TableStyle,
)


ROOT = Path(r"D:\tries\agent")
REPO = ROOT / "khoj-src"
SRC = REPO / "src" / "khoj"
OUTPUT = ROOT / "output" / "pdf" / "khoj_memory_upper_mechanism_notes.pdf"
COMMIT = "ae229ca894c0b80ad84664afcfdde523b5e87057"
ANALYSIS_DATE = "2026-08-31"

FILES = {
    "manage": SRC / "database" / "management" / "commands" / "manage_memories.py",
    "helpers": SRC / "routers" / "helpers.py",
    "prompts": SRC / "processor" / "conversation" / "prompts.py",
    "utils": SRC / "processor" / "conversation" / "utils.py",
    "api_chat": SRC / "routers" / "api_chat.py",
    "adapters": SRC / "database" / "adapters" / "__init__.py",
}

PAGE_W, PAGE_H = A4
LEFT = 12.5 * mm
RIGHT = 12.5 * mm
TOP = 13 * mm
BOTTOM = 14 * mm
CONTENT_W = PAGE_W - LEFT - RIGHT

INK = colors.HexColor("#18232D")
MUTED = colors.HexColor("#596875")
TEAL = colors.HexColor("#08766E")
TEAL_DARK = colors.HexColor("#07544F")
TEAL_LIGHT = colors.HexColor("#E8F5F3")
BLUE = colors.HexColor("#315F8A")
BLUE_LIGHT = colors.HexColor("#EAF1F8")
AMBER = colors.HexColor("#B86400")
AMBER_LIGHT = colors.HexColor("#FFF3DF")
RED = colors.HexColor("#B33636")
RED_LIGHT = colors.HexColor("#FCECEC")
GRID = colors.HexColor("#C8D2D9")
CODE_BG = colors.HexColor("#F3F5F7")
CODE_BORDER = colors.HexColor("#BCC7D0")


def register_fonts() -> None:
    groups = {
        "CJK": [Path(r"C:\Windows\Fonts\NotoSansSC-VF.ttf"), Path(r"C:\Windows\Fonts\msyh.ttc")],
        "CJKSerif": [Path(r"C:\Windows\Fonts\NotoSerifSC-VF.ttf"), Path(r"C:\Windows\Fonts\simsun.ttc")],
        "Code": [Path(r"C:\Windows\Fonts\consola.ttf"), Path(r"C:\Windows\Fonts\cour.ttf")],
    }
    for name, paths in groups.items():
        for path in paths:
            if path.exists():
                pdfmetrics.registerFont(TTFont(name, str(path)))
                break
        else:
            raise FileNotFoundError(f"Font not found: {name}")


register_fonts()
styles = getSampleStyleSheet()
styles.add(ParagraphStyle(name="TitleCN", fontName="CJKSerif", fontSize=19, leading=24, textColor=TEAL_DARK, alignment=TA_CENTER, spaceAfter=5))
styles.add(ParagraphStyle(name="SubtitleCN", fontName="CJK", fontSize=8.8, leading=12.2, textColor=MUTED, alignment=TA_CENTER, spaceAfter=5))
styles.add(ParagraphStyle(name="H1CN", fontName="CJKSerif", fontSize=13.4, leading=17, textColor=TEAL_DARK, spaceBefore=4, spaceAfter=4, keepWithNext=True))
styles.add(ParagraphStyle(name="H2CN", fontName="CJK", fontSize=10.2, leading=13.4, textColor=INK, spaceBefore=4, spaceAfter=3, keepWithNext=True))
styles.add(ParagraphStyle(name="BodyCN", fontName="CJK", fontSize=8.15, leading=11.75, textColor=INK, spaceAfter=3.1))
styles.add(ParagraphStyle(name="SmallCN", fontName="CJK", fontSize=7.25, leading=9.9, textColor=INK))
styles.add(ParagraphStyle(name="TinyCN", fontName="CJK", fontSize=6.55, leading=8.7, textColor=INK))
styles.add(ParagraphStyle(name="HeaderCN", fontName="CJK", fontSize=7.15, leading=9.7, textColor=colors.white))
styles.add(ParagraphStyle(name="CalloutCN", fontName="CJK", fontSize=7.75, leading=11.1, textColor=INK))
styles.add(ParagraphStyle(name="CodeCN", fontName="Code", fontSize=5.7, leading=6.95, textColor=colors.HexColor("#17212B")))


def p(text: str, style: str = "BodyCN") -> Paragraph:
    return Paragraph(text, styles[style])


def h1(number: str, title: str) -> Paragraph:
    return p(f"{number}  {title}", "H1CN")


def h2(title: str) -> Paragraph:
    return p(title, "H2CN")


def bullet(text: str) -> Paragraph:
    return p(f"<font color='#08766E'>●</font>　{text}", "BodyCN")


def callout(title: str, text: str, tone: str = "teal") -> Table:
    palette = {
        "teal": (TEAL_LIGHT, TEAL),
        "blue": (BLUE_LIGHT, BLUE),
        "amber": (AMBER_LIGHT, AMBER),
        "red": (RED_LIGHT, RED),
    }
    background, accent = palette[tone]
    table = Table([[p(f"<b>{title}</b>　{text}", "CalloutCN")]], colWidths=[CONTENT_W])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), background),
        ("BOX", (0, 0), (-1, -1), 0.55, accent),
        ("LINEBEFORE", (0, 0), (0, -1), 3, accent),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    return table


def table(headers: list[str], rows: list[list[str]], widths: list[float]) -> Table:
    if not math.isclose(sum(widths), CONTENT_W, abs_tol=1):
        raise ValueError((sum(widths), CONTENT_W))
    data = [[p(f"<b>{x}</b>", "HeaderCN") for x in headers]]
    data += [[p(x, "TinyCN") for x in row] for row in rows]
    result = Table(data, colWidths=widths, repeatRows=1, hAlign="LEFT")
    result.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), TEAL_DARK),
        ("GRID", (0, 0), (-1, -1), 0.35, GRID),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8FAFB")]),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3.2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3.2),
    ]))
    return result


def flow(items: list[tuple[str, str]]) -> Table:
    widths = [CONTENT_W / len(items)] * len(items)
    cells = [p(f"<b>{i + 1}. {name}</b><br/><font color='#596875'>{detail}</font>", "SmallCN") for i, (name, detail) in enumerate(items)]
    result = Table([cells], colWidths=widths)
    result.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), BLUE_LIGHT),
        ("BOX", (0, 0), (-1, -1), 0.55, BLUE),
        ("INNERGRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#AABCCB")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    return result


def parse(path: Path) -> tuple[str, list[str], ast.Module]:
    text = path.read_text(encoding="utf-8")
    return text, text.splitlines(), ast.parse(text)


def find_named(path: Path, name: str) -> tuple[str, int, int]:
    _, lines, tree = parse(path)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == name:
            found.append(node)
    if len(found) != 1:
        raise RuntimeError(f"Expected one {name} in {path}; found {len(found)}")
    node = found[0]
    decorators = [d.lineno for d in getattr(node, "decorator_list", [])]
    start = min([node.lineno, *decorators])
    end = node.end_lineno or node.lineno
    return "\n".join(lines[start - 1:end]), start, end


def find_assignment(path: Path, name: str) -> tuple[str, int, int]:
    _, lines, tree = parse(path)
    found = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            found.append(node)
    if len(found) != 1:
        raise RuntimeError(f"Expected one assignment {name}; found {len(found)}")
    node = found[0]
    return "\n".join(lines[node.lineno - 1:node.end_lineno]), node.lineno, node.end_lineno


def excerpt(path: Path, start_text: str, end_text: str) -> tuple[str, int, int]:
    lines = path.read_text(encoding="utf-8").splitlines()
    starts = [i for i, line in enumerate(lines) if start_text in line]
    if len(starts) != 1:
        raise RuntimeError(f"Excerpt start mismatch {start_text}: {starts}")
    start = starts[0]
    ends = [i for i in range(start, len(lines)) if end_text in lines[i]]
    if not ends:
        raise RuntimeError(f"Excerpt end missing {end_text}")
    end = ends[0]
    return "\n".join(lines[start:end + 1]), start + 1, end + 1


def excerpt_before(path: Path, start_text: str, next_text: str) -> tuple[str, int, int]:
    lines = path.read_text(encoding="utf-8").splitlines()
    starts = [i for i, line in enumerate(lines) if start_text in line]
    if len(starts) != 1:
        raise RuntimeError(f"Excerpt start mismatch {start_text}: {starts}")
    start = starts[0]
    next_lines = [i for i in range(start + 1, len(lines)) if next_text in lines[i]]
    if not next_lines:
        raise RuntimeError(f"Excerpt next marker missing {next_text}")
    end = next_lines[0] - 1
    while end >= start and not lines[end].strip():
        end -= 1
    return "\n".join(lines[start:end + 1]), start + 1, end + 1


SOURCES = {
    "add_arguments": (FILES["manage"], *find_named(FILES["manage"], "add_arguments")),
    "async_handle": (FILES["manage"], *find_named(FILES["manage"], "async_handle")),
    "get_user_conversations": (FILES["manage"], *find_named(FILES["manage"], "get_user_conversations")),
    "process_conversation_batch": (FILES["manage"], *find_named(FILES["manage"], "process_conversation_batch")),
    "get_or_create_checkpoint": (FILES["manage"], *find_named(FILES["manage"], "get_or_create_checkpoint")),
    "update_checkpoint": (FILES["manage"], *find_named(FILES["manage"], "update_checkpoint")),
    "clear_checkpoint": (FILES["manage"], *find_named(FILES["manage"], "clear_checkpoint")),
    "handle_delete_memories": (FILES["manage"], *find_named(FILES["manage"], "handle_delete_memories")),
    "construct_chat_history": (FILES["utils"], *find_named(FILES["utils"], "construct_chat_history")),
    "save_to_conversation_log": (FILES["utils"], *find_named(FILES["utils"], "save_to_conversation_log")),
    "generate_chatml_messages_with_context": (FILES["utils"], *find_named(FILES["utils"], "generate_chatml_messages_with_context")),
    "MemoryUpdates": (FILES["helpers"], *find_named(FILES["helpers"], "MemoryUpdates")),
    "extract_facts_from_query": (FILES["helpers"], *find_named(FILES["helpers"], "extract_facts_from_query")),
    "ai_update_memories": (FILES["helpers"], *find_named(FILES["helpers"], "ai_update_memories")),
    "build_conversation_context": (FILES["helpers"], *find_named(FILES["helpers"], "build_conversation_context")),
    "agenerate_chat_response": (FILES["helpers"], *find_named(FILES["helpers"], "agenerate_chat_response")),
    "ais_memory_enabled": (FILES["adapters"], *find_named(FILES["adapters"], "ais_memory_enabled")),
    "extract_prompt": (FILES["prompts"], *find_assignment(FILES["prompts"], "extract_facts_from_query")),
    "chat_retrieval_excerpt": (FILES["api_chat"], *excerpt(FILES["api_chat"], "# Get most recent memories", "relevant_memories = list({m.id: m for m in recent_memories + long_term_memories}.values())")),
    "chat_save_excerpt": (FILES["api_chat"], *excerpt_before(FILES["api_chat"], "# Save conversation once finish streaming", "# Signal end of LLM response")),
}


def rel(path: Path) -> str:
    return path.relative_to(REPO).as_posix()


def code_box(text: str) -> Table:
    code = Preformatted(text, styles["CodeCN"], maxLineLength=148, splitChars=" ,.)]}")
    box = Table([[code]], colWidths=[CONTENT_W])
    box.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), CODE_BG),
        ("BOX", (0, 0), (-1, -1), 0.5, CODE_BORDER),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    return box


def source_story(key: str, title: str, complete: bool = True, chunk_lines: int = 66) -> list:
    path, text, start, end = SOURCES[key]
    lines = text.splitlines()
    chunks = [lines[i:i + chunk_lines] for i in range(0, len(lines), chunk_lines)]
    flows: list = []
    kind = "完整源码" if complete else "调用点摘录"
    for index, chunk in enumerate(chunks):
        chunk_start = start + index * chunk_lines
        chunk_end = chunk_start + len(chunk) - 1
        suffix = f"（{index + 1}/{len(chunks)}）" if len(chunks) > 1 else ""
        label = p(f"<b>{kind}{suffix}</b>　<font color='#596875'>{rel(path)}:{chunk_start}-{chunk_end}</font>", "SmallCN")
        block = [p(title if index == 0 else f"{title}（续）", "H2CN"), label, Spacer(1, 2), code_box("\n".join(chunk)), Spacer(1, 4)]
        flows.append(KeepTogether(block))
        if index < len(chunks) - 1:
            flows.append(PageBreak())
    return flows


def header_footer(canvas, doc) -> None:
    canvas.saveState()
    canvas.setStrokeColor(colors.HexColor("#D7DEE4"))
    canvas.setLineWidth(0.4)
    canvas.line(LEFT, PAGE_H - 9.5 * mm, PAGE_W - RIGHT, PAGE_H - 9.5 * mm)
    canvas.setFont("CJK", 6.6)
    canvas.setFillColor(MUTED)
    canvas.drawString(LEFT, PAGE_H - 7.1 * mm, "Khoj 源码学习笔记 · Memory 上层机制")
    canvas.drawRightString(PAGE_W - RIGHT, PAGE_H - 7.1 * mm, f"commit {COMMIT[:10]}")
    canvas.line(LEFT, 9 * mm, PAGE_W - RIGHT, 9 * mm)
    canvas.drawString(LEFT, 5.7 * mm, "源码事实与设计解读分开标注；不补写 Khoj 未实现的机制")
    canvas.drawRightString(PAGE_W - RIGHT, 5.7 * mm, str(doc.page))
    canvas.restoreState()


def build_story() -> list:
    s: list = []
    s += [Spacer(1, 3 * mm), p("Khoj Agent Memory 源码学习笔记", "SubtitleCN"), p("Memory 上层机制", "TitleCN"), p("从 Conversation 提取事实、处理变化，到召回并注入 LLM Context", "SubtitleCN")]
    s.append(table(
        ["项目", "Commit / 日期", "主源码范围"],
        [["Khoj repository", f"{COMMIT}<br/>{ANALYSIS_DATE}", "manage_memories.py · routers/helpers.py · api_chat.py · conversation/prompts.py · conversation/utils.py"]],
        [35 * mm, 65 * mm, CONTENT_W - 100 * mm],
    ))
    s += [Spacer(1, 5), callout("本轮目标", "补齐两条生产路径和一条消费路径：实时聊天更新、管理命令批处理，以及新查询中的 Memory 召回与 Prompt 注入。UserMemory 字段与向量距离细节只作为已知前提，不重复展开。", "teal")]
    s.append(h1("01", "先看修正后的完整主链"))
    s.append(flow([
        ("新查询", "检查 Memory 开关"),
        ("召回", "recent + semantic，按 id 去重"),
        ("回答", "作为独立 user-role Memory Context"),
        ("保存", "写 Conversation 当前 turn"),
        ("提取", "当前 turn + existing memories → LLM"),
        ("落库", "create 新增，delete 按 id 删除"),
    ]))
    s.append(Spacer(1, 4))
    s.append(p("当前源码存在两种触发方式：<b>实时路径</b>在正常聊天响应完成后后台执行；<b>批处理路径</b>由 Django 管理命令按 lookback 和 batch 重放 Conversation。两者最终复用同一个 <font name='Code'>extract_facts_from_query</font> Prompt 与 create/delete 协议。"))
    s.append(table(
        ["阶段", "源码事实", "不要误解"],
        [
            ["产生", "LLM 从最新 user/assistant 消息中抽取重要用户事实。", "不是把整段聊天原样复制进 UserMemory。"],
            ["冲突", "Prompt 让 LLM 返回 create 文本与 delete 旧记录 id。", "没有数据库 UPDATE、merge、versioning 或 decay。"],
            ["召回", "近期与语义结果合并后按 id 去重。", "没有额外 rerank，也没有全局重新排序。"],
            ["注入", "Memory 形成独立 user-role ChatMessage，位于当前 query 前。", "不写进 system prompt，也不等同于 notes/RAG context。"],
        ],
        [30 * mm, 82 * mm, CONTENT_W - 112 * mm],
    ))
    s.append(callout("标注规则", "<b>源码事实</b>描述代码明确做了什么；<b>设计解读</b>只说明这种结构可能为何有用。所有边界、缺失机制与异常行为都按当前 commit 如实说明。", "blue"))
    s.append(h2("本次实际追踪的核心符号"))
    s.append(table(
        ["职责", "函数 / 模板"],
        [
            ["聊天召回与注入", "event_generator 调用点；build_conversation_context；generate_chatml_messages_with_context；agenerate_chat_response"],
            ["实时生成", "save_to_conversation_log；ai_update_memories"],
            ["事实抽取", "construct_chat_history；MemoryUpdates；extract_facts_from_query；prompts.extract_facts_from_query"],
            ["批处理生命周期", "Command.add_arguments / async_handle / get_user_conversations / process_conversation_batch / checkpoint helpers / handle_delete_memories"],
            ["开关", "ConversationAdapters.ais_memory_enabled"],
        ],
        [40 * mm, CONTENT_W - 40 * mm],
    ))

    s.append(PageBreak())
    s.append(h1("02", "新查询先召回 Memory"))
    s += source_story("chat_retrieval_excerpt", "event_generator：Memory Retrieval 调用点", complete=False)
    s.append(h2("运行过程"))
    s.append(bullet("<b>1. 双层开关的第一道门。</b> 只有 <font name='Code'>ais_memory_enabled(user)</font> 为真，聊天请求才执行召回；否则 relevant_memories 保持空列表。"))
    s.append(bullet("<b>2. 两条互补来源。</b> <font name='Code'>pull_memories</font> 提供近期活跃事实；<font name='Code'>search_memories(query=q)</font> 提供与当前 query 语义接近的长期事实。"))
    s.append(bullet("<b>3. 按主键去重。</b> 字典推导以 <font name='Code'>m.id</font> 为键。近期结果先进入，语义结果后进入；相同 id 只保留一条，最多约为两边 limit 之和。"))
    s.append(bullet("<b>4. 同一份列表有双重用途。</b> 它既进入本轮回答上下文，也在响应保存后成为事实提取的 existing_facts。"))
    s.append(callout("设计解读", "近期召回覆盖“当前生活阶段仍可能重要”的事实，语义召回覆盖“虽然久远但与本次问题直接相关”的事实。代码确实组合两者；“时间连续性 + 语义相关性”是对该结构的解释。", "blue"))
    s.append(callout("Agent 范围沿用上一节结论", "自定义 Agent 查询 user + agent；默认 Agent 或 None 只按 user，因此是用户级聚合视角。此处没有增加新的隔离规则。", "amber"))

    s.append(PageBreak())
    s.append(h1("03", "Memory 怎样进入最终 messages"))
    s += source_story("build_conversation_context", "build_conversation_context", complete=True)
    s.append(h2("整体作用"))
    s.append(p("该函数先构造 system_prompt，再把 notes、online、code、operator 等检索或执行结果拼成 context_message，最后把 relevant_memories 原样交给 <font name='Code'>generate_chatml_messages_with_context</font>。它是各种模型供应商共享的上下文入口。"))
    s.append(table(
        ["信息", "进入位置", "与 Memory 的关系"],
        [
            ["Personality / 日期 / 地点 / 姓名", "system_prompt", "Memory 不在这里。"],
            ["Notes / RAG references", "context_message", "知识证据；与用户长期事实分开。"],
            ["Online / code / operator", "context_message", "本轮外部信息或执行结果。"],
            ["relevant_memories", "单独参数", "下一函数将其转成独立 ChatMessage。"],
        ],
        [46 * mm, 52 * mm, CONTENT_W - 98 * mm],
    ))

    s.append(PageBreak())
    s += source_story("generate_chatml_messages_with_context", "generate_chatml_messages_with_context", complete=True)
    s.append(h2("Message 顺序与 Memory 的准确位置"))
    s.append(flow([
        ("system", "人格、日期、地点、姓名"),
        ("history", "普通 user/assistant 历史及历史上下文"),
        ("RAG context", "notes / web / code / operator"),
        ("assets", "生成资产与程序上下文"),
        ("memory", "独立 user-role retrieved_memories"),
        ("query", "当前 user_message"),
    ]))
    s.append(Spacer(1, 4))
    s.append(bullet("<b>格式。</b> 每条 Memory 变为 <font name='Code'>- [created_at]: raw</font>，包在 <font name='Code'>&lt;retrieved_memories&gt;</font> 标签中。embedding、id、search_model 不进入回答 Prompt。"))
    s.append(bullet("<b>角色。</b> Memory Context 是 <font name='Code'>role='user'</font> 的 ChatMessage，不是 system message。前导文本说明这些内容来自旧对话，并允许模型忽略与当前 query 无关的项。"))
    s.append(bullet("<b>没有 Memory。</b> <font name='Code'>is_none_or_empty(relevant_memories)</font> 为真时整个 Memory message 被跳过，不发送空标签。"))
    s.append(bullet("<b>Token 预算。</b> 所有 message 组装后统一进入 <font name='Code'>truncate_messages</font>。Memory 位于当前 query 之前，和普通历史、RAG 一起竞争模型上下文窗口。"))
    s.append(callout("Chat History、Memory、RAG 的结构区别", "Chat History 保持真实 user/assistant 轮次；Memory 是经过 LLM 抽取和数据库召回的用户事实；RAG/notes 是来自文档或搜索的证据上下文。三者可能都以 ChatMessage 进入模型，但来源、粒度和职责不同。", "teal"))

    s.append(PageBreak())
    s += source_story("agenerate_chat_response", "agenerate_chat_response", complete=True)
    s.append(h2("从 messages 到 LLM"))
    s.append(p("该函数选择当前 Conversation 的有效 chat model，调用 <font name='Code'>build_conversation_context</font> 得到统一 messages，再根据 OPENAI / ANTHROPIC / GOOGLE 分支交给对应 converse 函数。Memory 到这里已经不是 ORM 对象集合，而是 messages 中的一条 user-role 文本消息。"))
    s.append(callout("源码事实", "最终供应商分支都接收同一个 messages 变量。因此 Memory 注入发生在 provider dispatch 之前，不依赖某一家 LLM API。", "blue"))

    s.append(PageBreak())
    s.append(h1("04", "响应完成后，实时生成 Memory"))
    s += source_story("chat_save_excerpt", "event_generator：响应后保存调用点", complete=False)
    s.append(p("聊天流式输出完成后，源码用 <font name='Code'>asyncio.create_task</font> 启动 <font name='Code'>save_to_conversation_log</font>，不阻塞结束事件。它把本轮回答时使用的 relevant_memories 一并传入，成为下一步 existing_facts。"))
    s += source_story("save_to_conversation_log", "save_to_conversation_log", complete=True)
    s.append(h2("一轮实时 Conversation 如何触发 Memory"))
    s.append(bullet("<b>1. 形成当前 turn。</b> <font name='Code'>message_to_log</font> 用 q 和 chat_response 构造 user/assistant 两条 ChatMessageModel；这里传入空 chat_history，所以 new_messages 只代表当前 turn。"))
    s.append(bullet("<b>2. 先保存 Conversation。</b> <font name='Code'>ConversationAdapters.save_conversation</font> 把当前 turn 合并进数据库 Conversation。"))
    s.append(bullet("<b>3. 再更新 Memory。</b> 非 automation_id 分支调用 <font name='Code'>ai_update_memories</font>，输入当前两条消息、回答前召回的 existing memories，以及 Conversation 的 Agent。"))
    s.append(bullet("<b>4. 自动化意图。</b> 注释明确希望避免自动任务产生噪声；但当前仓库内三个直接调用点都未传 automation_id，因此不能仅凭这段分支断言所有自动任务实际都被排除。"))
    s.append(callout("失败边界", "event_generator 通过 create_task 后台启动保存与提取，主响应不等待 Memory 更新完成。该实时路径没有 checkpoint 或显式重试。", "amber"))

    s += source_story("ai_update_memories", "ai_update_memories", complete=True)
    s.append(h2("数据库动作"))
    s.append(p("这是实时更新的薄编排层：第二次检查 Memory 开关；调用事实抽取；依次 save create 项，再依次 delete 旧 id。没有 transaction 包裹整组动作，也没有 UPDATE。若中途失败，源码没有在此处回滚已成功的前序动作。"))

    s.append(PageBreak())
    s.append(h1("05", "Fact Extraction：Conversation 变成 create/delete"))
    s += source_story("construct_chat_history", "construct_chat_history", complete=True)
    s.append(p("事实抽取固定调用 <font name='Code'>construct_chat_history(..., n=2)</font>，这里 n 表示最后 2 条 message，而不是 2 个完整 turn。正常情况下正好是当前 User 与当前 AI 回复。输出是带 <font name='Code'>User:</font> 和 <font name='Code'>AI:</font> 标签的纯文本。"))
    s += source_story("MemoryUpdates", "MemoryUpdates 输出 Schema", complete=True)
    s += source_story("extract_facts_from_query", "extract_facts_from_query", complete=True)
    s.append(h2("输入如何变成结构化结果"))
    s.append(bullet("<b>Conversation 输入。</b> 仅取传入列表最后两条，形成 latest chat session。实时路径传当前 turn；批处理路径虽传累计 history，最终仍只保留当前 pair。"))
    s.append(bullet("<b>Existing facts。</b> <font name='Code'>UserMemoryAdapters.to_dict</font> 只导出 id、raw、updated_at，再 JSON 格式化。没有 existing facts 时传入空列表。"))
    s.append(bullet("<b>模型调用。</b> Prompt 以 <font name='Code'>response_schema=MemoryUpdates</font>、<font name='Code'>fast_model=False</font>、<font name='Code'>agent_chat_model=agent.chat_model</font> 发送。"))
    s.append(bullet("<b>解析。</b> 清理模型文本后 json.loads，再由 Pydantic 校验 create/delete。无效 JSON 或 schema 错误被记录并降级为空列表，所以该轮不写也不删。"))

    s += source_story("extract_prompt", "prompts.extract_facts_from_query", complete=True)
    s.append(h2("Prompt 明确规定了什么值得记"))
    s.append(table(
        ["Prompt 规则", "含义"],
        [
            ["about and on behalf of the user", "围绕用户本人，而不是泛化保存助手回答中的世界知识。"],
            ["important, new facts", "不是每句聊天都创建 Memory；重要性与新颖性由 LLM 判断。"],
            ["atomic, self-contained", "每条事实应独立、粒度单一，便于后续召回和替换。"],
            ["first person perspective", "事实写成“我……”视角，便于作为用户记忆注入。"],
            ["delete IDs", "delete 不是事实文本，而是 existing facts 的记录 id。"],
            ["cannot update directly", "变化通过 create 新事实 + delete 旧 id 完成。"],
        ],
        [58 * mm, CONTENT_W - 58 * mm],
    ))
    s.append(callout("源码事实 vs 设计解读", "源码明确把冲突判断交给 Prompt 驱动的 LLM，没有 Python 规则比较“Python”和“Go”。把 existing memories 放入 Prompt 有助于识别重复、过期和可增强事实，这是设计解读；结果不具备确定性保证。", "blue"))

    s.append(PageBreak())
    s.append(h1("06", "冲突、变化、重复：真实边界"))
    s.append(h2("例子：Python → Go"))
    s.append(flow([
        ("Existing fact", "id=42：I mainly use Python"),
        ("Latest turn", "User：I now mainly use Go"),
        ("LLM decision", "create Go；delete 42"),
        ("Database", "INSERT 新 Memory；DELETE user+id=42"),
    ]))
    s.append(Spacer(1, 4))
    s.append(callout("这是教学上的期望输出，不是确定规则", "Prompt 要求删除不再为真的旧事实并创建新事实，因此上述输出符合协议；但具体 create 文本和 delete id 由 LLM 生成，代码没有字符串冲突检测器。", "amber"))
    s.append(table(
        ["问题", "当前源码明确实现", "没有实现"],
        [
            ["更新", "先 save_memory(create)，再 delete_memory(delete id)。", "原地 UPDATE、字段级 patch。"],
            ["重复", "existing facts 提供上下文；Prompt 要求 important、new。", "raw 唯一约束、hash 去重、确定性相似度去重。"],
            ["冲突", "LLM Prompt 决定旧事实是否 no longer true。", "Python 规则引擎、事务性 conflict resolver。"],
            ["历史", "旧记录被物理删除。", "versioning、审计链、时间衰减、软删除。"],
            ["原子性", "逐条异步 INSERT / DELETE。", "围绕整组 create/delete 的数据库 transaction。"],
        ],
        [32 * mm, 78 * mm, CONTENT_W - 110 * mm],
    ))
    s.append(h2("为什么 existing memories 仍不能保证不重复"))
    s.append(p("existing_facts 只是 recent 与 semantic 的有限候选并集，不是用户全部 Memory；数据库也没有 raw 唯一约束。若旧事实没有被召回、LLM 忽略已有事实、或删除 id 无效，新旧记录可能并存。代码层没有后续 merge 清理。"))
    s.append(callout("动作顺序的后果", "create 循环先执行，delete 循环后执行。若新事实已保存而删除失败，系统会暂时或永久同时保留新旧事实；当前函数没有回滚。", "red"))

    s.append(PageBreak())
    s.append(h1("07", "manage_memories.py：批处理与运维入口"))
    s += source_story("add_arguments", "Command.add_arguments", complete=True)
    s.append(table(
        ["选项", "源码行为"],
        [
            ["--lookback-days", "生成默认 7 天；删除模式未指定时表示删除全部。"],
            ["--users", "按逗号分隔 username 或 email；不传则选择有 Conversation 的用户。"],
            ["--batch-size", "每批 Conversation 数，默认 10；不是每次 LLM 的消息数。"],
            ["--apply", "缺省 dry run；只有 apply 才调用 LLM 并写数据库。"],
            ["--delete", "切换到批量删除，不执行生成。"],
            ["--resume", "尝试加载 DataStore checkpoint。"],
            ["--force", "忽略 checkpoint 排除，重新处理 lookback 范围内 Conversation。"],
        ],
        [42 * mm, CONTENT_W - 42 * mm],
    ))
    s += source_story("async_handle", "Command.async_handle", complete=True)
    s.append(h2("批处理总流程"))
    s.append(p("命令初始化服务器后进入 async_handle：确定生成或删除模式；按 lookback 选用户与 Conversation；按 batch_size 分批；每个 Conversation 交给 process_conversation_batch；apply 模式持续写 checkpoint，完整成功后清除。"))
    s.append(callout("force 的准确含义", "force 只是不跳过 checkpoint 中的用户/Conversation。它不会先删除已有 UserMemory，因此“重新处理”不等于“清空后重建”，是否产生重复仍取决于 existing facts 召回和 LLM 决策。", "amber"))

    s.append(PageBreak())
    s += source_story("get_user_conversations", "Command.get_user_conversations", complete=True)
    s.append(p("候选 Conversation 以 user 为边界，按 updated_at 是否晚于 cutoff 过滤，并按 updated_at 升序处理。resume 且非 force 时，再排除 checkpoint 已记录的 conversation id。"))
    s += source_story("process_conversation_batch", "Command.process_conversation_batch", complete=True)
    s.append(h2("一条历史 Conversation 怎样被重放"))
    s.append(bullet("<b>1. 读取消息与 Agent。</b> Conversation.messages 是 JSON chat log 的 Pydantic 视图；属性访问经 sync_to_async。"))
    s.append(bullet("<b>2. 只认相邻 user/assistant pair。</b> 当前项必须 by='you'，下一项必须 by='khoj'；否则滑动一个位置继续。空 user message 也跳过。"))
    s.append(bullet("<b>3. 构造累计 history。</b> 传入 <font name='Code'>messages[:i+2]</font>，但 extract_facts 再取最后 2 条，所以本轮 Prompt 仍聚焦当前 pair。"))
    s.append(bullet("<b>4. 每个 pair 重新召回。</b> recent + semantic 按 id 去重，既包含原有 Memory，也可能包含前一个 pair 刚创建的新 Memory。"))
    s.append(bullet("<b>5. apply 才调用 LLM。</b> dry run 不做抽取，只为每个有效 pair 将计数粗略加 1；它不是 create 数量的精确预测。"))
    s.append(bullet("<b>6. Conversation 级容错。</b> 外层 try/except 记录 traceback 后继续下一个 Conversation。已经完成的单条写入不会自动回滚。"))
    s.append(callout("与实时路径的关键差异", "批处理命令没有调用 ais_memory_enabled，因此管理员显式运行时可处理关闭了实时 Memory 的用户。它是独立运维入口，不受聊天开关那两道检查约束。", "red"))

    s.append(PageBreak())
    s += source_story("get_or_create_checkpoint", "Command.get_or_create_checkpoint", complete=True)
    s += source_story("update_checkpoint", "Command.update_checkpoint", complete=True)
    s += source_story("clear_checkpoint", "Command.clear_checkpoint", complete=True)
    s.append(h2("checkpoint / resume 的真实语义"))
    s.append(p("checkpoint 存在私有 DataStore 记录中，包含 started_at、processed_users、processed_conversations。每个 Conversation 完成后写入，完整 apply 成功后删除。设计上这是中断恢复机制；源码行为还存在一个需要读者留意的边界。"))
    s.append(callout("当前实现的可观察问题", "update_checkpoint 在每次 Conversation 完成时就把 user_id 加入 processed_users；resume 的 async_handle 又先跳过任何 processed_users 中的用户。因此若一个用户处理中途终止，恢复时可能直接跳过该用户剩余 Conversation。这里陈述的是当前代码路径，不是架构意图。", "red"))
    s += source_story("handle_delete_memories", "Command.handle_delete_memories", complete=True)
    s.append(h2("批量删除与重新生成"))
    s.append(p("删除模式直接构造 UserMemory QuerySet 并 bulk adelete，不走 UserMemoryAdapters.delete_memory。未指定 lookback 时删选中用户全部 Memory；指定时只删 created_at 在 cutoff 之后的记录。--apply 缺省为 dry run。源码没有把 delete 与随后的 generate 自动串成一个“重建事务”，需要分别执行命令。"))

    s.append(PageBreak())
    s.append(h1("08", "Memory 开关：什么时候系统不工作"))
    s += source_story("ais_memory_enabled", "ConversationAdapters.ais_memory_enabled", complete=True)
    s.append(table(
        ["Server memory_mode", "无用户配置", "有用户配置"],
        [
            ["DISABLED", "False", "仍为 False，服务器覆盖用户。"],
            ["ENABLED_DEFAULT_OFF", "False", "读取 UserConversationConfig.enable_memory。"],
            ["ENABLED_DEFAULT_ON", "True", "读取 UserConversationConfig.enable_memory。"],
            ["无 ServerChatSettings", "True", "读取 UserConversationConfig.enable_memory。"],
        ],
        [50 * mm, 45 * mm, CONTENT_W - 95 * mm],
    ))
    s.append(p("实时聊天在召回前检查一次，在 ai_update_memories 写入前再检查一次。关闭后既不会为回答召回 Memory，也不会从该轮对话生成新 Memory。批量管理命令则不走此函数。"))

    s.append(h1("09", "失败、边界与没有实现的机制"))
    s.append(table(
        ["场景", "当前处理"],
        [
            ["LLM 返回无效 JSON / schema", "extract_facts_from_query 记录错误并返回 create=[], delete=[]。"],
            ["模型调用本身抛异常", "extract 函数不在调用外层统一吞掉；实时后台任务或批处理 Conversation 外层承担后果。"],
            ["批处理某 Conversation 失败", "记录 traceback，继续下一条；该 Conversation 不写 checkpoint。"],
            ["中途部分写入后失败", "没有组事务回滚；可能保留部分 create 或未完成 delete。"],
            ["没有召回到旧冲突事实", "Prompt 看不到它；源码没有全库冲突扫描。"],
            ["delete 返回不存在", "delete_memory 返回 False；调用方不检查返回值。"],
        ],
        [58 * mm, CONTENT_W - 58 * mm],
    ))
    s.append(callout("明确未实现", "Memory merge、versioning、soft delete、decay、重要度分数、确定性去重、事务化 create/delete、全库冲突扫描都不能从当前主链源码中得到。不要把 Prompt 的目标描述误当成数据库保证。", "amber"))

    s.append(PageBreak())
    s.append(h1("10", "最终完整链路与学习结论"))
    s.append(flow([
        ("Conversation turn", "用户消息 + 助手回复"),
        ("Existing Memory", "回答前召回的 recent + semantic"),
        ("Extraction Prompt", "latest chat + id/raw/updated_at"),
        ("LLM JSON", "create: facts；delete: ids"),
        ("UserMemory", "INSERT 新事实；DELETE 旧事实"),
    ]))
    s.append(Spacer(1, 5))
    s.append(flow([
        ("Later query", "先检查开关"),
        ("Recall", "recent + semantic，按 id 去重"),
        ("Formatting", "时间戳 + raw，retrieved_memories 标签"),
        ("Messages", "独立 user-role，在当前 query 前"),
        ("LLM response", "与 history / RAG 一起消费"),
    ]))
    s.append(h2("应该真正掌握什么"))
    for text in [
        "Memory 数据来自经过 LLM 事实抽取的 Conversation turn，不是整段聊天历史的副本。",
        "existing memories 既帮助判断新颖性，也为冲突替换提供旧记录 id；但只是有限召回子集。",
        "变化采用 create + delete，没有真正 UPDATE；冲突判断主要由 Prompt 和 LLM 完成。",
        "正常聊天响应后实时后台更新；manage_memories.py 提供 lookback、batch、dry-run、checkpoint、force 与批量删除。",
        "回答前召回 recent 与 semantic Memory，按 id 去重后形成独立 user-role Memory Context。",
        "普通 Chat History 负责对话连续性；Memory 负责跨会话用户事实；RAG Knowledge 负责外部文档和检索证据。",
    ]:
        s.append(bullet(text))
    s.append(table(
        ["对象", "来源", "典型粒度", "进入 LLM 的方式"],
        [
            ["Chat History", "Conversation log", "原始轮次", "user / assistant 历史 messages"],
            ["Memory", "LLM 抽取后的 UserMemory", "原子用户事实", "单独 user-role retrieved_memories"],
            ["RAG / Notes", "文档、搜索、引用", "证据片段", "context_message 或历史 context message"],
        ],
        [33 * mm, 50 * mm, 45 * mm, CONTENT_W - 128 * mm],
    ))
    s.append(callout("一句话定位", "UserMemoryAdapters 负责基础数据访问；本轮主链负责决定何时抽取、让 LLM 输出哪些 create/delete、何时召回，以及怎样把 Memory 变成模型可读的上下文。", "teal"))
    s.append(Spacer(1, 4))
    s.append(p(f"源码版本：Khoj commit <font name='Code'>{COMMIT}</font>。所有完整源码块由该 commit 的本地仓库直接按 AST 行范围提取；event_generator 仅保留标注过的连续调用点原文。超长源码行只做视觉续行，不改字符。", "SmallCN"))
    return s


def build() -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    frame = Frame(LEFT, BOTTOM, CONTENT_W, PAGE_H - TOP - BOTTOM, id="main", leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    doc = BaseDocTemplate(
        str(OUTPUT), pagesize=A4, leftMargin=LEFT, rightMargin=RIGHT, topMargin=TOP, bottomMargin=BOTTOM,
        title="Khoj Memory 上层机制源码学习笔记", author="OpenAI Codex", subject=f"Khoj commit {COMMIT}",
    )
    doc.addPageTemplates([PageTemplate(id="normal", frames=[frame], onPage=header_footer)])
    doc.build(build_story())


if __name__ == "__main__":
    build()
    print(OUTPUT)
