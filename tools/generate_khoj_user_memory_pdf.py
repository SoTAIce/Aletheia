from __future__ import annotations

import os
import re
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Paragraph,
    Preformatted,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "khoj-src" / "src" / "khoj" / "database" / "models" / "__init__.py"
OUTPUT = ROOT / "output" / "pdf" / "khoj_user_memory_model_notes.pdf"
COMMIT = "ae229ca894c0b80ad84664afcfdde523b5e87057"
ANALYSIS_DATE = "2026-08-31"


def register_fonts() -> tuple[str, str, str]:
    pdfmetrics.registerFont(TTFont("NotoSansSC", r"C:\Windows\Fonts\NotoSansSC-VF.ttf"))
    pdfmetrics.registerFont(TTFont("NotoSerifSC", r"C:\Windows\Fonts\NotoSerifSC-VF.ttf"))
    pdfmetrics.registerFont(TTFont("SimHei", r"C:\Windows\Fonts\simhei.ttf"))
    return "NotoSansSC", "NotoSerifSC", "SimHei"


def extract_class(source: str, class_name: str) -> str:
    lines = source.splitlines()
    start = next(i for i, line in enumerate(lines) if re.match(rf"^class {class_name}\b", line))
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("class ") or lines[i].startswith("@receiver"):
            end = i
            break
    return "\n".join(lines[start:end]).rstrip()


def extract_range(source: str, start_pat: str, end_pat: str) -> str:
    lines = source.splitlines()
    start = next(i for i, line in enumerate(lines) if start_pat in line)
    end = next(i for i in range(start + 1, len(lines)) if end_pat in lines[i])
    return "\n".join(lines[start:end]).rstrip()


def p(text: str, style: ParagraphStyle):
    return Paragraph(text, style)


def bullets(items: list[str], style: ParagraphStyle):
    return [Paragraph(f"- {item}", style) for item in items]


def h(text: str, style: ParagraphStyle):
    return Paragraph(text, style)


def label(text: str, style: ParagraphStyle):
    return Paragraph(f"<b>{text}</b>", style)


def code(text: str, style: ParagraphStyle):
    return Preformatted(text, style)


def explain_block(title: str, sections: list[tuple[str, str | list[str]]], styles) -> list:
    parts = [h(title, styles["h2"])]
    for name, content in sections:
        parts.append(label(f"【{name}】", styles["label"]))
        if isinstance(content, list):
            parts.extend(bullets(content, styles["body"]))
        else:
            parts.append(p(content, styles["body"]))
    parts.append(Spacer(1, 1.8 * mm))
    return parts


def build_pdf() -> None:
    font, serif, mono = register_fonts()
    base = getSampleStyleSheet()
    base.add(ParagraphStyle("TitleCN", fontName=serif, fontSize=17.5, leading=21, alignment=TA_CENTER, spaceAfter=4))
    base.add(ParagraphStyle("MetaCN", fontName=font, fontSize=7.8, leading=9.5, alignment=TA_CENTER, textColor=colors.HexColor("#4b5563")))
    base.add(ParagraphStyle("H1CN", fontName=font, fontSize=12.2, leading=14.6, spaceBefore=3, spaceAfter=3, textColor=colors.HexColor("#111827")))
    base.add(ParagraphStyle("H2CN", fontName=font, fontSize=10.3, leading=12.5, spaceBefore=3, spaceAfter=2.2, textColor=colors.HexColor("#1f2937")))
    base.add(ParagraphStyle("LabelCN", fontName=font, fontSize=8.2, leading=9.8, spaceBefore=1.4, spaceAfter=0.8, textColor=colors.HexColor("#111827")))
    base.add(ParagraphStyle("BodyCN", fontName=font, fontSize=7.35, leading=9.25, alignment=TA_LEFT, spaceAfter=0.8))
    base.add(ParagraphStyle("SmallCN", fontName=font, fontSize=6.45, leading=7.95, alignment=TA_LEFT))
    base.add(ParagraphStyle("CodeCN", fontName=mono, fontSize=5.35, leading=6.05, leftIndent=2.0, rightIndent=2.0, backColor=colors.HexColor("#f6f7f9"), borderColor=colors.HexColor("#d9dee7"), borderWidth=0.35, borderPadding=2.0))
    styles = {
        "title": base["TitleCN"],
        "meta": base["MetaCN"],
        "h1": base["H1CN"],
        "h2": base["H2CN"],
        "label": base["LabelCN"],
        "body": base["BodyCN"],
        "small": base["SmallCN"],
        "code": base["CodeCN"],
    }

    source = SOURCE.read_text(encoding="utf-8")
    db_base = extract_class(source, "DbBaseModel")
    user_memory = extract_class(source, "UserMemory")
    khoj_user = extract_class(source, "KhojUser")
    agent_header = "class Agent(DbBaseModel):"
    agent_fields = extract_range(source, "    creator = models.ForeignKey(", "    def save(self, *args, **kwargs):")
    search_model = extract_class(source, "SearchModelConfig")

    doc = SimpleDocTemplate(
        str(OUTPUT),
        pagesize=A4,
        leftMargin=10.5 * mm,
        rightMargin=10.5 * mm,
        topMargin=10 * mm,
        bottomMargin=10.5 * mm,
    )

    def footer(canvas, doc_obj):
        canvas.saveState()
        canvas.setFont(font, 6.8)
        canvas.setFillColor(colors.HexColor("#6b7280"))
        canvas.drawString(10.5 * mm, 7.1 * mm, "Khoj UserMemory 数据模型学习笔记")
        canvas.drawRightString(A4[0] - 10.5 * mm, 7.1 * mm, f"{doc_obj.page}")
        canvas.restoreState()

    story = [
        p("Khoj UserMemory 数据模型学习笔记", styles["title"]),
        p(f"Khoj repository: https://github.com/khoj-ai/khoj | commit: {COMMIT} | 分析日期: {ANALYSIS_DATE}", styles["meta"]),
        Spacer(1, 2.0 * mm),
        p("学习边界: 只学习 UserMemory 以及理解其字段所需的直接父类和字段依赖。不展开 Memory 的产生、检索、更新、删除、上下文注入或 UserMemoryAdapters。", styles["body"]),
        Spacer(1, 1.2 * mm),
        h("UserMemory 完整源码", styles["h1"]),
        label("DbBaseModel 完整相关源码", styles["label"]),
        code(db_base, styles["code"]),
        label("UserMemory 完整源码", styles["label"]),
        code(user_memory, styles["code"]),
        h("直接依赖定义摘录", styles["h1"]),
        p("以下仍然属于源码阅读区域，集中展示 UserMemory 字段理解所需的真实源码行；后面的逻辑讲解不再重复粘贴源码。", styles["body"]),
        label("KhojUser 身份相关定义", styles["label"]),
        code(khoj_user, styles["code"]),
        label("Agent 类声明", styles["label"]),
        code(agent_header, styles["code"]),
        label("Agent 与归属关系相关字段摘录", styles["label"]),
        code(agent_fields, styles["code"]),
        label("SearchModelConfig 与 embedding 来源相关定义", styles["label"]),
        code(search_model, styles["code"]),
        Spacer(1, 1.5 * mm),
        h("UserMemory 整体结构", styles["h1"]),
    ]

    tree = [
        ["UserMemory", "长期 Memory 的数据库记录，来源于用户与 Agent 的对话，但这里只描述“存成什么样”。"],
        ["├── 生命周期信息", "继承自 DbBaseModel: created_at、updated_at。"],
        ["├── Memory 归属信息", "user 是强制账户边界；agent 是可选的 Agent 上下文边界。"],
        ["├── Memory 原始内容", "raw 保存可读的长期事实或偏好文本。"],
        ["├── Memory 向量表示", "embeddings 保存 raw 的语义检索表示。"],
        ["└── Embedding 模型来源", "search_model 记录向量来自哪套 SearchModelConfig。"],
    ]
    tree_table = Table(
        [[Paragraph(a, styles["small"]), Paragraph(b, styles["small"])] for a, b in tree],
        colWidths=[42 * mm, 131 * mm],
    )
    tree_table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#d4dae4")),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#edf1f6")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 2.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
    ]))
    story.append(tree_table)
    story.append(Spacer(1, 1.2 * mm))
    story.append(p("mental model: UserMemory 把一条自然语言长期记忆拆成“谁的、在哪个 Agent 场景下、原文是什么、语义向量是什么、向量由什么模型配置产生、记录处于什么生命周期”。", styles["body"]))

    story.extend(explain_block("逻辑块 1: 继承关系与生命周期", [
        ("整体作用", "前面源码中 UserMemory 继承 DbBaseModel，使每条 Memory 自动拥有 created_at 与 updated_at。这个继承不是为了业务多态，而是为了把所有数据库记录通用的时间元数据统一混入具体表。"),
        ("关键语义", [
            "abstract model: DbBaseModel 的 Meta.abstract=True 表示它不会生成独立数据库表；字段会直接出现在 UserMemory 对应表中。",
            "created_at: auto_now_add=True，只在对象第一次插入时自动赋值，表示这条长期 Memory 首次进入数据库的时间。",
            "updated_at: auto_now=True，每次保存模型实例时自动刷新，表示这条 Memory 最近一次被数据库层更新的时间。",
            "auto_now_add 与 auto_now 的核心区别: 前者强调“创建时刻”，后者强调“最后保存时刻”。",
        ]),
        ("Agent Memory 设计意义", "长期 Memory 需要生命周期信息，因为偏好和事实会随时间变化。时间字段未来可支持按新旧排序、判断陈旧、审计何时形成、观察何时被修订，或在维护任务中决定哪些 Memory 需要复核。"),
        ("具体例子", "如果 2026-08-31 保存“用户主要使用 Python”，created_at 记录首次保存时间；若后来同一条记录被修订为“用户主要使用 Python 和 Django”，updated_at 会前进，而 created_at 仍保留最初形成时间。"),
    ], styles))

    story.extend(explain_block("逻辑块 2: Memory 的归属关系", [
        ("整体作用", "前面 UserMemory 中的 user 和 agent 字段共同回答“这条 Memory 属于谁、属于哪个对话人格或工具场景”。user 是硬边界；agent 是更细的可选上下文边界。"),
        ("字段语义", [
            "ForeignKey 在这里表达多对一关系: 一个 KhojUser 可以对应多条 UserMemory；一个 Agent 也可以对应多条 UserMemory。",
            "user 强制存在: 源码没有 null=True、blank=True 或 default，因此每条 Memory 必须有确定用户，防止长期记忆脱离账户主体。",
            "agent 可以为空: default=None、null=True、blank=True 共同允许用户级 Memory 不绑定某个具体 Agent。",
            "CASCADE: 当 user 被删除时，其 Memory 随之删除；当被绑定的 agent 删除时，agent 级 Memory 也删除，避免留下失去上下文主体的记录。",
        ]),
        ("user 级与 agent 级区别", "user 级 Memory 描述跨 Agent 都成立的长期偏好，例如“用户喜欢详细源码讲解”。agent 级 Memory 描述只在某个 Agent 场景中成立的偏好，例如在 Django tutor Agent 中“先看 model 字段，再看调用关系”。"),
        ("Django ORM 设计含义", "null=True 是数据库层允许 NULL；blank=True 是表单/验证层允许空值；default=None 是创建对象时未传 agent 的默认值。三者一起说明 agent 归属是可选维度，而 user 归属不是。"),
    ], styles))

    story.extend(explain_block("逻辑块 3: Memory 内容与向量表示", [
        ("整体作用", "前面 UserMemory 中的 raw 和 embeddings 把“Memory 内容”和“Memory 的检索表示”分开保存。raw 是可读事实，embeddings 是可计算的语义坐标。"),
        ("字段语义", [
            "raw 保存被抽取后的长期记忆文本，不是整段聊天历史。TextField 适合这类长度不固定、需要完整保留语义的文本。",
            "embeddings 保存 raw 经过 embedding 模型得到的向量。VectorField 来自 pgvector.django，使 PostgreSQL 可以存储向量并进行相似度相关计算。",
            "dimensions=None 表示 Django 模型层不固定向量维度；实际维度由写入的向量和所用 embedding 模型配置决定。",
        ]),
        ("为什么二者都要保存", [
            "不能只保存 raw: 只靠文本字段很难高效找到语义相近但字面不同的 Memory。",
            "不能只保存 embedding: 向量不可读，难以审计、展示、解释，也难以在模型更换后重新生成。",
            "raw 负责“记住了什么”；embeddings 负责“如何通过语义相似找到它”。",
        ]),
        ("Agent Memory 设计意义", "长期 Memory 是给 Agent 后续对话使用的知识片段，但召回时用户不会总是使用相同措辞。把内容和检索表示拆开，既保留可解释性，又为语义召回留下数据库级结构。"),
    ], styles))

    story.extend(explain_block("逻辑块 4: Embedding 来源追踪", [
        ("整体作用", "前面 UserMemory 中的 search_model 指向 SearchModelConfig。它保存的是 embedding 来源配置的引用，不是 embedding 向量本身。真正的向量仍在 embeddings 字段。"),
        ("字段语义", [
            "SearchModelConfig 描述文本 embedding/搜索相关配置: name、model_type、bi_encoder、encode config、inference endpoint、threshold 等。",
            "search_model 让一条 UserMemory 可以说明“我的 embeddings 是按哪套配置生成或解释的”。",
            "该字段允许为空，兼容默认配置、历史数据或配置迁移阶段。",
        ]),
        ("SET_NULL 与 CASCADE", "search_model 使用 SET_NULL，而不是 CASCADE，说明模型配置被删除时，Memory 的文本事实仍有价值，不应整条删除；只是丢失了明确的向量来源指针。相比之下 user/agent 使用 CASCADE，是因为失去所属主体后 Memory 本身也失去归属语义。"),
        ("Agent Memory 设计意义", "不同 embedding 模型可能产生不同维度、不同分布、不同距离尺度的向量，不能默认直接比较。模型变化时，search_model 能帮助识别旧向量来源，判断是否需要重新嵌入、过滤不同向量空间的数据，或解释召回质量差异。"),
    ], styles))

    story.append(h("完整实例串联", styles["h1"]))
    story.extend(bullets([
        "示例 Memory: “用户主要使用 Python，并偏好详细的源码逻辑讲解。” 这只是学习样例，不是 Khoj 真实数据库数据。",
        "user = 当前账户对应的 KhojUser；它决定这条长期 Memory 的账户归属。",
        "agent = 如果该偏好跨所有 Agent 成立，则可以是 NULL；如果只在某个源码讲解 Agent 中成立，则指向那个 Agent。",
        "raw = 示例句子的自然语言文本，保存 Memory 的可读内容。",
        "embeddings = 示例句子经过文本 embedding 模型生成的向量，保存 Memory 的语义检索表示。",
        "search_model = 生成或解释该向量时使用的 SearchModelConfig，例如默认文本 embedding 配置。",
        "created_at = 该 UserMemory 首次插入数据库的时间；updated_at = 该记录最近一次保存或修订的时间。",
    ], styles["body"]))

    story.append(h("数据结构总结", styles["h1"]))
    rows = [
        ["字段", "类型", "是否允许为空", "关联对象", "删除策略", "保存内容", "设计目的"],
        ["created_at", "DateTimeField", "否", "-", "-", "创建时间", "记录 Memory 首次入库时间"],
        ["updated_at", "DateTimeField", "否", "-", "-", "更新时间", "记录最近保存或修订时间"],
        ["user", "ForeignKey", "否", "KhojUser", "CASCADE", "所属用户", "账户级隔离与归属"],
        ["agent", "ForeignKey", "是", "Agent", "CASCADE", "可选 Agent", "区分用户级和 Agent 级 Memory"],
        ["raw", "TextField", "否", "-", "-", "记忆原文", "可读、可审计、可重新嵌入"],
        ["embeddings", "VectorField", "否", "-", "-", "语义向量", "支持相似度召回"],
        ["search_model", "ForeignKey", "是", "SearchModelConfig", "SET_NULL", "模型配置引用", "追踪 embedding 来源与兼容性"],
    ]
    table = Table(
        [[Paragraph(str(cell), styles["small"]) for cell in row] for row in rows],
        colWidths=[20 * mm, 24 * mm, 22 * mm, 28 * mm, 18 * mm, 27 * mm, 34 * mm],
        repeatRows=1,
    )
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e5eaf1")),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#cbd5e1")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 2.4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2.4),
        ("TOPPADDING", (0, 0), (-1, -1), 2.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
    ]))
    story.append(table)

    story.append(Spacer(1, 1.5 * mm))
    story.append(h("本节总结", styles["h1"]))
    story.extend(bullets([
        "UserMemory 解决的是 Memory 在数据库中如何被表示和保存: 它把归属、原文、向量、模型来源和生命周期放进一条结构化记录。",
        "它不负责 Memory 如何产生、如何检索、如何更新、如何删除，也不负责如何进入 Agent 上下文。",
        "这些后续问题属于 UserMemoryAdapters 和其他模块。本节只需要牢牢记住: UserMemory 是长期 Memory 的数据形状。",
    ], styles["body"]))

    doc.build(story, onFirstPage=footer, onLaterPages=footer)


if __name__ == "__main__":
    os.makedirs(OUTPUT.parent, exist_ok=True)
    build_pdf()
    print(OUTPUT)
