from __future__ import annotations

import ast
import math
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
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
SOURCE = ROOT / "khoj-src" / "src" / "khoj" / "database" / "adapters" / "__init__.py"
OUTPUT = ROOT / "output" / "pdf" / "khoj_user_memory_adapters_notes.pdf"
COMMIT = "ae229ca894c0b80ad84664afcfdde523b5e87057"
ANALYSIS_DATE = "2026-08-31"


PAGE_W, PAGE_H = A4
LEFT = 13 * mm
RIGHT = 13 * mm
TOP = 13 * mm
BOTTOM = 14 * mm
CONTENT_W = PAGE_W - LEFT - RIGHT

INK = colors.HexColor("#17202A")
MUTED = colors.HexColor("#52606D")
TEAL = colors.HexColor("#0B6E69")
TEAL_DARK = colors.HexColor("#07514E")
TEAL_LIGHT = colors.HexColor("#E8F4F2")
BLUE_LIGHT = colors.HexColor("#EAF1F8")
AMBER = colors.HexColor("#B76000")
AMBER_LIGHT = colors.HexColor("#FFF3DF")
RED = colors.HexColor("#A63434")
RED_LIGHT = colors.HexColor("#FDEEEE")
GRID = colors.HexColor("#C9D2D9")
PAPER = colors.HexColor("#FFFFFF")
CODE_BG = colors.HexColor("#F4F6F8")
CODE_BORDER = colors.HexColor("#BFC9D2")


def register_fonts() -> None:
    candidates = {
        "CJK": [
            Path(r"C:\Windows\Fonts\NotoSansSC-VF.ttf"),
            Path(r"C:\Windows\Fonts\msyh.ttc"),
        ],
        "CJKSerif": [
            Path(r"C:\Windows\Fonts\NotoSerifSC-VF.ttf"),
            Path(r"C:\Windows\Fonts\simsun.ttc"),
        ],
        "Code": [
            Path(r"C:\Windows\Fonts\consola.ttf"),
            Path(r"C:\Windows\Fonts\cour.ttf"),
        ],
    }
    for font_name, paths in candidates.items():
        for path in paths:
            if path.exists():
                pdfmetrics.registerFont(TTFont(font_name, str(path)))
                break
        else:
            raise FileNotFoundError(f"No usable font for {font_name}: {paths}")


register_fonts()


styles = getSampleStyleSheet()
styles.add(
    ParagraphStyle(
        name="TitleCN",
        fontName="CJKSerif",
        fontSize=20,
        leading=25,
        textColor=TEAL_DARK,
        alignment=TA_CENTER,
        spaceAfter=5,
    )
)
styles.add(
    ParagraphStyle(
        name="SubtitleCN",
        fontName="CJK",
        fontSize=9.2,
        leading=13,
        textColor=MUTED,
        alignment=TA_CENTER,
        spaceAfter=6,
    )
)
styles.add(
    ParagraphStyle(
        name="H1CN",
        fontName="CJKSerif",
        fontSize=14,
        leading=18,
        textColor=TEAL_DARK,
        spaceBefore=5,
        spaceAfter=4,
        keepWithNext=True,
    )
)
styles.add(
    ParagraphStyle(
        name="H2CN",
        fontName="CJK",
        fontSize=10.5,
        leading=14,
        textColor=INK,
        spaceBefore=4,
        spaceAfter=3,
        keepWithNext=True,
    )
)
styles.add(
    ParagraphStyle(
        name="BodyCN",
        fontName="CJK",
        fontSize=8.35,
        leading=12.2,
        textColor=INK,
        spaceAfter=3.2,
    )
)
styles.add(
    ParagraphStyle(
        name="SmallCN",
        fontName="CJK",
        fontSize=7.35,
        leading=10.2,
        textColor=INK,
    )
)
styles.add(
    ParagraphStyle(
        name="TableHeaderCN",
        fontName="CJK",
        fontSize=7.35,
        leading=10.2,
        textColor=colors.white,
    )
)
styles.add(
    ParagraphStyle(
        name="TinyCN",
        fontName="CJK",
        fontSize=6.75,
        leading=9.1,
        textColor=INK,
    )
)
styles.add(
    ParagraphStyle(
        name="CalloutCN",
        fontName="CJK",
        fontSize=8,
        leading=11.6,
        textColor=INK,
    )
)
styles.add(
    ParagraphStyle(
        name="CodeBlock",
        fontName="Code",
        fontSize=6.45,
        leading=8.05,
        textColor=colors.HexColor("#18222C"),
        leftIndent=0,
        rightIndent=0,
        spaceAfter=0,
    )
)


def p(text: str, style: str = "BodyCN") -> Paragraph:
    return Paragraph(text, styles[style])


def section(title: str, number: str | None = None) -> Paragraph:
    prefix = f"{number}  " if number else ""
    return p(f"{prefix}{title}", "H1CN")


def sub(title: str) -> Paragraph:
    return p(title, "H2CN")


def callout(title: str, text: str, tone: str = "teal") -> Table:
    palette = {
        "teal": (TEAL_LIGHT, TEAL),
        "amber": (AMBER_LIGHT, AMBER),
        "red": (RED_LIGHT, RED),
        "blue": (BLUE_LIGHT, colors.HexColor("#315C85")),
    }
    background, accent = palette[tone]
    body = p(f"<b>{title}</b>　{text}", "CalloutCN")
    table = Table([[body]], colWidths=[CONTENT_W])
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), background),
                ("BOX", (0, 0), (-1, -1), 0.6, accent),
                ("LINEBEFORE", (0, 0), (0, -1), 3, accent),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    return table


def bullet(text: str) -> Paragraph:
    return p(f"<font color='#0B6E69'>●</font>　{text}", "BodyCN")


def compact_table(headers: list[str], rows: list[list[str]], widths: list[float], font_size: float = 7.1) -> Table:
    if not math.isclose(sum(widths), CONTENT_W, abs_tol=1):
        raise ValueError(f"Column widths must sum to content width: {sum(widths)} vs {CONTENT_W}")
    data = [[p(f"<b>{cell}</b>", "TableHeaderCN") for cell in headers]]
    data.extend([[p(cell, "TinyCN") for cell in row] for row in rows])
    table = Table(data, colWidths=widths, repeatRows=1, hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), TEAL_DARK),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "CJK"),
                ("FONTSIZE", (0, 0), (-1, -1), font_size),
                ("GRID", (0, 0), (-1, -1), 0.35, GRID),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [PAPER, colors.HexColor("#F8FAFB")]),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 3.2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3.2),
            ]
        )
    )
    return table


def method_sources() -> dict[str, tuple[str, int, int]]:
    text = SOURCE.read_text(encoding="utf-8")
    lines = text.splitlines()
    tree = ast.parse(text)
    target = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "UserMemoryAdapters")
    methods: dict[str, tuple[str, int, int]] = {}
    for node in target.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            decorator_lines = [dec.lineno for dec in node.decorator_list]
            start = min([node.lineno, *decorator_lines])
            end = node.end_lineno or node.lineno
            methods[node.name] = ("\n".join(lines[start - 1 : end]), start, end)
    expected = {"pull_memories", "save_memory", "search_memories", "delete_memory", "to_dict"}
    if set(methods) != expected:
        raise RuntimeError(f"UserMemoryAdapters changed. Found {sorted(methods)}; expected {sorted(expected)}")
    return methods


METHODS = method_sources()


def code_panel(method_name: str) -> KeepTogether:
    source, start, end = METHODS[method_name]
    label = p(
        f"<b>完整源码</b>　<font color='#52606D'>src/khoj/database/adapters/__init__.py:{start}-{end}</font>",
        "SmallCN",
    )
    code = Preformatted(source, styles["CodeBlock"])
    box = Table([[code]], colWidths=[CONTENT_W])
    box.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), CODE_BG),
                ("BOX", (0, 0), (-1, -1), 0.55, CODE_BORDER),
                ("LEFTPADDING", (0, 0), (-1, -1), 7),
                ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    return KeepTogether([label, Spacer(1, 2), box, Spacer(1, 4)])


def flow_strip(items: list[tuple[str, str]]) -> Table:
    cell_w = CONTENT_W / len(items)
    cells = []
    for index, (name, detail) in enumerate(items, 1):
        cells.append(p(f"<b>{index}. {name}</b><br/><font color='#52606D'>{detail}</font>", "SmallCN"))
    table = Table([cells], colWidths=[cell_w] * len(cells), hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), BLUE_LIGHT),
                ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#6384A3")),
                ("INNERGRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#A8BBCD")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    return table


def header_footer(canvas, doc) -> None:
    canvas.saveState()
    canvas.setStrokeColor(colors.HexColor("#D7DEE4"))
    canvas.setLineWidth(0.4)
    canvas.line(LEFT, PAGE_H - 9.5 * mm, PAGE_W - RIGHT, PAGE_H - 9.5 * mm)
    canvas.setFont("CJK", 6.7)
    canvas.setFillColor(MUTED)
    canvas.drawString(LEFT, PAGE_H - 7.2 * mm, "Khoj 源码学习笔记 · UserMemoryAdapters")
    canvas.drawRightString(PAGE_W - RIGHT, PAGE_H - 7.2 * mm, f"commit {COMMIT[:10]}")
    canvas.line(LEFT, 9 * mm, PAGE_W - RIGHT, 9 * mm)
    canvas.drawString(LEFT, 5.7 * mm, "范围：数据库适配器层，不展开 Memory 生成策略与上下文注入")
    canvas.drawRightString(PAGE_W - RIGHT, 5.7 * mm, f"{doc.page}")
    canvas.restoreState()


def build_story() -> list:
    story: list = []

    story.append(Spacer(1, 3 * mm))
    story.append(p("Khoj Agent Memory 源码学习笔记", "SubtitleCN"))
    story.append(p("UserMemoryAdapters", "TitleCN"))
    story.append(p("保存、近期读取、语义检索与删除的数据库边界", "SubtitleCN"))
    meta = compact_table(
        ["项目", "源码文件", "Commit / 分析日期"],
        [[
            "Khoj repository",
            "src/khoj/database/adapters/__init__.py",
            f"{COMMIT}<br/>{ANALYSIS_DATE}",
        ]],
        [36 * mm, 78 * mm, CONTENT_W - 114 * mm],
    )
    story.append(meta)
    story.append(Spacer(1, 5))
    story.append(callout(
        "本节边界",
        "只研究 <b>UserMemoryAdapters</b> 如何把调用意图转成 embedding 计算与 Django ORM 操作。"
        "不进入记忆内容如何产生、冲突如何更新、Prompt 如何注入，也不展开管理命令。",
        "teal",
    ))
    story.append(section("先建立整体模型", "01"))
    story.append(p(
        "上一节的 <b>UserMemory</b> 定义一条 Memory 在数据库里长什么样；本节的 <b>UserMemoryAdapters</b> "
        "定义应用代码怎样对这些记录执行五类操作。它把异步业务调用、同步 embedding 推理、Django QuerySet "
        "以及 pgvector 距离表达式收束在一个薄而关键的数据库访问边界中。"
    ))
    story.append(flow_strip([
        ("调用参数", "user / agent / query / memory"),
        ("适配器", "校验、分支、embedding、ORM"),
        ("数据库", "UserMemory + pgvector"),
        ("返回值", "模型对象、列表、布尔值、字典"),
    ]))
    story.append(Spacer(1, 4))
    story.append(compact_table(
        ["方法", "核心问题", "关键机制", "返回"],
        [
            ["pull_memories", "近期有哪些记忆？", "时间窗口 + created_at 倒序 + LIMIT", "list[UserMemory]"],
            ["save_memory", "怎样存一条可检索记忆？", "生成 embedding + acreate", "UserMemory"],
            ["search_memories", "哪些历史记忆语义相关？", "CosineDistance + 阈值 + 排序", "list[UserMemory]"],
            ["delete_memory", "能否按 id 删除本用户记忆？", "user + id 定位 + adelete", "bool"],
            ["to_dict", "怎样形成轻量输出？", "模型列表到字典列表", "list[dict]"],
        ],
        [32 * mm, 48 * mm, 64 * mm, CONTENT_W - 144 * mm],
    ))
    story.append(sub("直接依赖，只读到理解所需深度"))
    story.append(compact_table(
        ["依赖", "在本类中的作用", "必须抓住的真实语义"],
        [
            ["require_valid_user", "四个数据库方法的装饰器", "只检查参数中存在 KhojUser 实例；不是权限系统，也不查询用户状态。"],
            ["state.embeddings_model", "按 model.name 取得已加载 embedding 实例", "embed_query 是同步调用，因此通过 sync_to_async 离开事件循环。"],
            ["aget_default_search_model", "异步取得名为 default 的 SearchModelConfig", "不存在配置时会创建或回退到第一条配置。"],
            ["AgentAdapters.aget_default_agent", "判断传入 agent 是否为默认 Agent", "默认 Agent 与 agent=None 走同一分支；自定义 Agent 走隔离分支。"],
            ["CosineDistance", "把向量距离作为 ORM 表达式注入 SQL", "pgvector 余弦距离，值越小越相似。"],
        ],
        [40 * mm, 63 * mm, CONTENT_W - 103 * mm],
    ))
    story.append(callout(
        "先记住 Agent 范围不是对称隔离",
        "自定义 Agent 分支使用 <b>user + agent</b>；默认 Agent 或 agent=None 分支只使用 <b>user</b>。"
        "因此后者会看到该用户名下也包括自定义 Agent 绑定的记录。用户之间严格隔离，但默认视角是用户级聚合视角。",
        "amber",
    ))

    story.append(PageBreak())
    story.append(section("pull_memories：按时间窗口拉取近期记忆", "02"))
    story.append(code_panel("pull_memories"))
    story.append(sub("解决什么问题"))
    story.append(p(
        "它不计算语义相似度，只回答“这个用户最近一段时间内有哪些 Memory 仍处于活跃窗口”。"
        "docstring 将其明确称为 <b>Medium term memory</b>：时间窗口默认 7 天，最多返回 10 条。"
    ))
    story.append(compact_table(
        ["参数", "默认值", "运行语义"],
        [
            ["user", "无", "所有查询都先按 user 过滤，是跨用户数据隔离的硬边界。"],
            ["agent", "None", "非默认 Agent 时额外过滤 agent；默认或 None 时采用用户级聚合。"],
            ["limit", "10", "QuerySet 切片生成 SQL LIMIT；限制最终物化数量。"],
            ["window", "7", "从当前 UTC 时间回退的天数，用于 updated_at__gte。"],
        ],
        [30 * mm, 26 * mm, CONTENT_W - 56 * mm],
    ))
    story.append(sub("实际运行过程"))
    story.append(bullet(
        "<b>1. 建立时间下界。</b> <font name='Code'>datetime.now(timezone.utc) - timedelta(days=window)</font> "
        "得到带 UTC 时区的 cutoff，避免用 naive datetime 与 Django 时区字段比较。"
    ))
    story.append(bullet(
        "<b>2. 判定 Agent 视角。</b> 先 await 默认 Agent。只有传入 Agent 且它不是默认 Agent 时，基础条件才是 "
        "<font name='Code'>user=user, agent=agent</font>；否则仅为 <font name='Code'>user=user</font>。"
    ))
    story.append(bullet(
        "<b>3. 构造惰性 QuerySet。</b> <font name='Code'>updated_at__gte</font> 变成 SQL 的大于等于条件；"
        "<font name='Code'>order_by('-created_at')</font> 变成创建时间降序；<font name='Code'>[:limit]</font> 变成 LIMIT。"
    ))
    story.append(bullet(
        "<b>4. 在线程敏感的同步 ORM 边界物化。</b> QuerySet 在构造时通常不访问数据库；"
        "<font name='Code'>sync_to_async(list)(memories)</font> 才执行查询并把结果变成真正的 Python 列表。"
    ))
    story.append(callout(
        "两个“最近”字段各司其职",
        "入选条件看 <b>updated_at</b>：最近 window 天被创建或修改过；返回顺序看 <b>created_at</b>：在入选记录中，新创建的排在前面。"
        "所以它不是按最后更新时间排序。",
        "blue",
    ))
    story.append(sub("为什么偏向 Medium-term Memory"))
    story.append(p(
        "这个方法依靠滚动时间窗口和数量上限控制上下文规模，不需要 query，也不比较 embedding。"
        "它适合把近期仍活跃的事实直接带回上层：时间相关性是第一筛选标准，语义相关性不是。"
        "返回值保持为 <b>list[UserMemory]</b>，后续调用者仍能访问 raw、时间戳与归属信息。"
    ))
    story.append(callout(
        "例子",
        "若 window=7、limit=2，有三条记录在 7 天内更新，创建时间依次为今天、昨天、上月；"
        "查询先保留三条，再按 created_at 降序取前两条。上月创建但今天刚更新的记录能入选，却可能因创建较早被截掉。",
        "teal",
    ))

    story.append(PageBreak())
    story.append(section("save_memory：文本与向量一起落库", "03"))
    story.append(code_panel("save_memory"))
    story.append(sub("输入、输出与数据流"))
    story.append(compact_table(
        ["输入", "角色"],
        [
            ["user: KhojUser", "记录归属的用户，也是后续所有读取与删除的隔离键。"],
            ["memory: str", "原始长期记忆文本；既写入 raw，也作为 embed_query 的输入。"],
            ["agent: Agent = None", "可选作用域；自定义 Agent 写入 FK，默认 Agent 则省略 agent 字段。"],
            ["返回 UserMemory", "acreate 已插入数据库的模型实例，包括数据库生成的 id 与时间字段。"],
        ],
        [48 * mm, CONTENT_W - 48 * mm],
    ))
    story.append(flow_strip([
        ("Memory 文本", "memory"),
        ("默认模型配置", "aget_default_search_model"),
        ("Query Embedding", "embed_query(memory)"),
        ("ORM 写入", "UserMemory.objects.acreate"),
    ]))
    story.append(Spacer(1, 4))
    story.append(sub("实际运行过程"))
    story.append(bullet(
        "<b>1. 取得 embedding 运行时与配置。</b> <font name='Code'>state.embeddings_model</font> 是以配置名索引的已加载模型集合；"
        "<font name='Code'>model</font> 是数据库中的 SearchModelConfig。两者通过 <font name='Code'>model.name</font> 对接。"
    ))
    story.append(bullet(
        "<b>2. 把文本编码成向量。</b> <font name='Code'>embed_query(memory)</font> 是同步计算，"
        "<font name='Code'>sync_to_async(...)</font> 让 async 方法等待其结果，而不直接阻塞事件循环。"
    ))
    story.append(bullet(
        "<b>3. 判定归属范围。</b> 自定义 Agent 写入 <font name='Code'>agent=agent</font>；默认 Agent 或 None 不传 agent，"
        "由 UserMemory 字段默认值保存为 NULL。这使“默认 Agent 记忆”在物理记录上表现为用户级记忆。"
    ))
    story.append(bullet(
        "<b>4. 异步插入。</b> <font name='Code'>acreate</font> 同时写入 user、raw、embeddings、search_model，"
        "并按分支决定是否写 agent。Django 完成 INSERT 后返回模型实例。"
    ))
    story.append(callout(
        "embedding 在保存阶段承担什么",
        "它不是 Memory 内容本身，而是 raw 的数值检索表示。保存时预计算一次，搜索时数据库可直接比较 Query Embedding 与已存向量，"
        "避免每次搜索都重新编码全部 Memory。",
        "teal",
    ))
    story.append(sub("async / await 的边界"))
    story.append(compact_table(
        ["操作", "为何 await", "执行性质"],
        [
            ["aget_default_search_model", "异步 ORM 查询配置", "数据库 I/O"],
            ["sync_to_async(embed_query)", "同步模型调用包装为 awaitable", "CPU 或外部推理调用"],
            ["aget_default_agent", "异步 ORM 查询默认 Agent", "数据库 I/O"],
            ["UserMemory.objects.acreate", "Django 异步 INSERT", "数据库 I/O"],
        ],
        [48 * mm, 67 * mm, CONTENT_W - 115 * mm],
    ))
    story.append(callout(
        "来源追踪",
        "同一次写入把 <b>embeddings</b> 与 <b>search_model=model</b> 同时保存：前者是向量值，后者说明该向量由哪套配置产生。"
        "这两个字段不是同一件事。",
        "blue",
    ))

    story.append(PageBreak())
    story.append(section("search_memories：语义相关的 Long-term Retrieval", "04"))
    story.append(code_panel("search_memories"))
    story.append(sub("一条完整检索链路"))
    story.append(flow_strip([
        ("Query", "自然语言问题"),
        ("Query Embedding", "当前默认模型编码"),
        ("UserMemory", "按 user / agent 建立候选集"),
        ("Vector Distance", "CosineDistance"),
        ("Threshold + Order", "过滤并升序"),
        ("Relevant Memories", "LIMIT 后物化"),
    ]))
    story.append(Spacer(1, 4))
    story.append(p(
        "该方法没有时间窗口。只要历史 Memory 仍在候选集中，就可以凭语义距离被找回，因此 docstring 将它称为 "
        "<b>Long term memory</b>。它回答的不是“最近发生了什么”，而是“哪些已存事实与当前 query 最接近”。"
    ))
    story.append(compact_table(
        ["参数", "作用"],
        [
            ["query: str", "要检索的自然语言；先转换为 embedded_query。"],
            ["user: KhojUser", "候选集永远先按 user 过滤，阻断跨用户向量比较。"],
            ["agent: Agent = None", "自定义 Agent 时缩小到 user + agent；默认或 None 时是用户级聚合。"],
            ["limit: int = 10", "只返回距离最小的前 limit 条；切片在数据库查询中形成 LIMIT。"],
        ],
        [48 * mm, CONTENT_W - 48 * mm],
    ))
    story.append(sub("阶段 1：Query → Query Embedding"))
    story.append(p(
        "先取得当前默认 SearchModelConfig，再读取其 <font name='Code'>bi_encoder_confidence_threshold</font>。"
        "表达式 <font name='Code'>threshold or math.inf</font> 的真实效果是：正常非零阈值照用；None 或 0.0 都会变成正无穷，"
        "相当于关闭距离过滤。随后使用 <font name='Code'>state.embeddings_model[model.name]</font> 编码 query。"
    ))
    story.append(callout(
        "查询向量必须与记忆向量处于同一语义空间",
        "余弦距离只有在向量维度和坐标语义兼容时才有意义。源码使用“当前默认模型”编码 query；"
        "本方法本身没有按 UserMemory.search_model 过滤候选记录，因此模型切换后的兼容性不是这里自动保证的。",
        "amber",
    ))

    story.append(PageBreak())
    story.append(section("search_memories：ORM 与 pgvector 逐阶段拆解", "05"))
    story.append(sub("阶段 2：建立 UserMemory 候选集"))
    story.append(p(
        "自定义 Agent：<font name='Code'>filter(user=user, agent=agent)</font>；默认 Agent 或 None："
        "<font name='Code'>filter(user=user)</font>。这里首先缩小 SQL 的候选行，再计算向量距离。"
        "用户条件在两条分支中都存在，所以不会拿 Alice 的 query 去比较 Bob 的 Memory。"
    ))
    story.append(callout(
        "Agent 隔离的准确说法",
        "自定义 Agent 的读取是隔离的；默认/None 读取是聚合的。代码并未写 "
        "<font name='Code'>agent__isnull=True</font>，所以不能把默认分支解释为“只读 user 级记录”。",
        "red",
    ))
    story.append(sub("阶段 3：annotate distance"))
    story.append(p(
        "<font name='Code'>annotate(distance=CosineDistance('embeddings', embedded_query))</font> "
        "为每个候选行增加一个计算列 distance。pgvector 的余弦距离可理解为："
    ))
    formula = Table(
        [[p("<b>cosine_distance = 1 - cosine_similarity</b>", "H2CN")]],
        colWidths=[CONTENT_W],
    )
    formula.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), TEAL_LIGHT),
        ("BOX", (0, 0), (-1, -1), 0.6, TEAL),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(formula)
    story.append(Spacer(1, 3))
    story.append(p(
        "因此 <b>distance 越小越相关</b>：方向完全一致时接近 0；正交时约为 1；方向相反时约为 2。"
        "它关注方向而非向量长度，适合比较文本语义表示。源码锁定 pgvector 0.2.4，并通过其 Django 表达式生成 PostgreSQL 向量距离运算。"
    ))
    story.append(sub("阶段 4：Threshold、Order、Limit"))
    story.append(compact_table(
        ["机制", "源码表达", "真实语义"],
        [
            ["Threshold", "distance__lte=max_distance", "丢弃距离大于阈值的结果。默认阈值 0.18 时，按公式等价于相似度至少约 0.82。"],
            ["Order", "order_by('distance')", "按距离升序，最相似的 Memory 排在最前。"],
            ["Limit", "relevant_memories[:limit]", "SQL LIMIT，只物化前 N 条，不是取回全部后再用 Python 截断。"],
        ],
        [35 * mm, 57 * mm, CONTENT_W - 92 * mm],
    ))
    story.append(callout(
        "链式调用顺序不等于数据库逐行执行顺序",
        "Python 代码先写 order_by 再写 filter，但 QuerySet 仍是惰性查询描述。最终通常编译为一条包含距离表达式、WHERE 条件、"
        "ORDER BY 与 LIMIT 的 SQL，由数据库统一规划执行。不要想象成“先把全表排好序，再在 Python 中过滤”。",
        "blue",
    ))
    story.append(sub("阶段 5：物化并返回"))
    story.append(p(
        "直到 <font name='Code'>sync_to_async(list)(relevant_memories[:limit])</font>，QuerySet 才真正执行。"
        "返回的是按距离升序排列的 <b>list[UserMemory]</b>，不是 distance 数字列表；annotate 的 distance 作为运行时属性附在每个模型对象上。"
    ))
    story.append(sub("数值例子"))
    story.append(compact_table(
        ["候选 Memory", "distance", "阈值 0.18", "结果位置"],
        [
            ["用户偏好详细源码讲解", "0.06", "通过", "第 1"],
            ["用户主要使用 Python", "0.14", "通过", "第 2"],
            ["用户下周去旅行", "0.52", "淘汰", "不返回"],
        ],
        [80 * mm, 28 * mm, 32 * mm, CONTENT_W - 140 * mm],
    ))
    story.append(p(
        "这是教学示例，不是 Khoj 真实数据库值。它展示的是先按阈值保留，再以小距离优先并应用 limit 的结果。",
        "SmallCN",
    ))

    story.append(PageBreak())
    story.append(section("delete_memory：按用户与 id 精确删除", "06"))
    story.append(code_panel("delete_memory"))
    story.append(sub("实际运行过程"))
    story.append(bullet(
        "<b>1. 用户参数检查。</b> 装饰器要求调用参数中有 KhojUser 实例。"
    ))
    story.append(bullet(
        "<b>2. 异步定位。</b> <font name='Code'>aget(user=user, id=memory_id)</font> 同时匹配主键和 user。"
        "即使知道另一用户的 memory_id，也无法通过这条查询取得其记录。"
    ))
    story.append(bullet(
        "<b>3. 异步删除。</b> 找到后调用实例的 <font name='Code'>adelete()</font>，成功返回 True。"
    ))
    story.append(bullet(
        "<b>4. 幂等式接口结果。</b> 查不到时捕获 UserMemory.DoesNotExist 并返回 False，不把“本来就不存在”作为异常抛给上层。"
    ))
    story.append(callout(
        "删除范围",
        "该方法没有 agent 参数，也不按 agent 过滤。它保护的是跨用户边界；在同一用户内部，只要 id 匹配，就能删除 user 级或任意 Agent 绑定的 Memory。",
        "amber",
    ))
    story.append(p(
        "返回值是 bool：True 只说明本次找到并执行了删除；False 表示在该用户范围内没有这条 id。"
        "这里不返回被删除对象，也不处理内容层面的冲突或替换。"
    ))

    story.append(section("to_dict：形成轻量输出结构", "07"))
    story.append(code_panel("to_dict"))
    story.append(p(
        "这是类中的辅助方法，不访问数据库，也不需要 user 校验。它遍历已取得的 UserMemory 对象，只导出 id、raw、updated_at。"
        "id 被显式转成字符串；updated_at 先转为 UTC，再输出精确到秒的 ISO 8601 文本。embeddings、search_model、user、agent "
        "都没有进入该轻量表示，避免把内部向量和关系对象暴露到常规输出中。"
    ))
    story.append(compact_table(
        ["输出键", "来源", "格式"],
        [
            ["id", "memory.id", "字符串"],
            ["raw", "memory.raw", "原始 Memory 文本"],
            ["updated_at", "memory.updated_at", "UTC ISO 8601，精确到秒"],
        ],
        [36 * mm, 52 * mm, CONTENT_W - 88 * mm],
    ))

    story.append(PageBreak())
    story.append(section("方法对照与职责边界", "08"))
    story.append(sub("pull_memories 与 search_memories"))
    story.append(compact_table(
        ["维度", "pull_memories", "search_memories"],
        [
            ["核心问题", "最近一段时间有哪些活跃 Memory？", "哪些历史 Memory 与 query 语义最相关？"],
            ["主要筛选", "updated_at 时间窗口", "余弦距离阈值"],
            ["排序", "created_at 降序", "distance 升序"],
            ["是否计算 query embedding", "否", "是"],
            ["时间跨度", "默认 7 天", "无时间窗口"],
            ["Memory 定位", "Medium-term：近期活跃", "Long-term：跨时间语义找回"],
            ["共同点", "按 user 建立边界；自定义 Agent 分支额外按 agent；最后 LIMIT 并返回模型列表。", "同左"],
        ],
        [38 * mm, (CONTENT_W - 38 * mm) / 2, (CONTENT_W - 38 * mm) / 2],
    ))
    story.append(sub("四个数据库方法的 ORM 语义总表"))
    story.append(compact_table(
        ["方法", "数据库动作", "隔离条件", "异步边界", "结果"],
        [
            ["pull", "SELECT + 时间过滤 + ORDER + LIMIT", "user；自定义 Agent 加 agent", "sync_to_async(list)", "模型列表"],
            ["save", "INSERT", "写入 user；自定义 Agent 写 agent", "aget / embed 包装 / acreate", "模型实例"],
            ["search", "SELECT + 向量距离 + WHERE + ORDER + LIMIT", "user；自定义 Agent 加 agent", "aget / embed 包装 / list", "模型列表"],
            ["delete", "SELECT 单条 + DELETE", "user + id；不含 agent", "aget / adelete", "bool"],
        ],
        [23 * mm, 50 * mm, 46 * mm, 42 * mm, CONTENT_W - 161 * mm],
    ))
    story.append(sub("UserMemory 与 UserMemoryAdapters 的职责边界"))
    story.append(flow_strip([
        ("UserMemory", "定义字段、关系、向量列与生命周期"),
        ("UserMemoryAdapters", "把保存、读取、检索、删除翻译为 ORM 操作"),
        ("上层 Memory 逻辑", "决定何时调用；本节不展开"),
    ]))
    story.append(Spacer(1, 4))
    story.append(p(
        "<b>UserMemory 解决“存成什么样”</b>：它是数据库记录的 schema。"
        "<b>UserMemoryAdapters 解决“怎样对这些记录做基础数据访问”</b>：生成保存向量、拉取近期记录、执行语义距离查询、按用户删除，并提供轻量序列化。"
        "它是模型层与更高层 Memory 决策逻辑之间的数据库边界，但不决定什么值得记、冲突如何合并，也不负责把 Memory 注入 Agent 上下文。"
    ))
    story.append(callout(
        "学完本节应真正掌握",
        "保存链路中 raw 与 embedding 同步落库；pull 以时间为中心；search 以语义距离为中心；Django QuerySet 惰性构造并在 async 边界物化；"
        "user 始终隔离，agent 则采用“自定义隔离、默认聚合”的非对称策略；CosineDistance 小值优先，阈值、排序和 LIMIT 共同决定最终 Relevant Memories。",
        "teal",
    ))
    story.append(Spacer(1, 3))
    story.append(p(
        "源码版本说明：本文以 Khoj GitHub 仓库 commit "
        f"<font name='Code'>{COMMIT}</font> 为准。核心方法源码由该 commit 的目标文件直接提取，未改写。",
        "SmallCN",
    ))
    return story


def build_pdf() -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    frame = Frame(
        LEFT,
        BOTTOM,
        CONTENT_W,
        PAGE_H - TOP - BOTTOM,
        id="main",
        leftPadding=0,
        rightPadding=0,
        topPadding=0,
        bottomPadding=0,
    )
    template = PageTemplate(id="normal", frames=[frame], onPage=header_footer)
    doc = BaseDocTemplate(
        str(OUTPUT),
        pagesize=A4,
        leftMargin=LEFT,
        rightMargin=RIGHT,
        topMargin=TOP,
        bottomMargin=BOTTOM,
        title="Khoj UserMemoryAdapters 源码学习笔记",
        author="OpenAI Codex",
        subject=f"Khoj commit {COMMIT}",
    )
    doc.addPageTemplates([template])
    doc.build(build_story())


if __name__ == "__main__":
    build_pdf()
    print(OUTPUT)
