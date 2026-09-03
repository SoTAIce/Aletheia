from __future__ import annotations

import ast
import hashlib
import textwrap
from datetime import date
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
    Flowable,
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


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output" / "pdf" / "rag_full_chain_source_notes_zh.pdf"
PAGE_W, PAGE_H = A4

INK = colors.HexColor("#17212B")
MUTED = colors.HexColor("#5C6873")
BLUE = colors.HexColor("#135E96")
BLUE_PALE = colors.HexColor("#EAF3F8")
TEAL = colors.HexColor("#0B6B64")
TEAL_PALE = colors.HexColor("#E8F5F2")
AMBER = colors.HexColor("#A35B00")
AMBER_PALE = colors.HexColor("#FFF3DC")
RED = colors.HexColor("#A13737")
LINE = colors.HexColor("#CBD5DC")
PAPER = colors.HexColor("#FBFCFD")
CODE_BG = colors.HexColor("#F3F6F8")


def register_fonts() -> None:
    pdfmetrics.registerFont(TTFont("CN", r"C:\Windows\Fonts\msyh.ttc"))
    pdfmetrics.registerFont(TTFont("CN-Bold", r"C:\Windows\Fonts\msyhbd.ttc"))
    pdfmetrics.registerFont(TTFont("CodeCN", r"C:\Windows\Fonts\simfang.ttf"))


register_fonts()


styles = getSampleStyleSheet()
ST = {
    "title": ParagraphStyle(
        "TitleCN", fontName="CN-Bold", fontSize=25, leading=31, textColor=INK,
        alignment=TA_LEFT, spaceAfter=10,
    ),
    "subtitle": ParagraphStyle(
        "SubtitleCN", fontName="CN", fontSize=11, leading=17, textColor=MUTED,
        spaceAfter=10,
    ),
    "h1": ParagraphStyle(
        "H1CN", fontName="CN-Bold", fontSize=17, leading=22, textColor=INK,
        spaceBefore=4, spaceAfter=7, keepWithNext=True,
    ),
    "h2": ParagraphStyle(
        "H2CN", fontName="CN-Bold", fontSize=12.2, leading=16, textColor=BLUE,
        spaceBefore=8, spaceAfter=4, keepWithNext=True,
    ),
    "h3": ParagraphStyle(
        "H3CN", fontName="CN-Bold", fontSize=9.8, leading=13, textColor=TEAL,
        spaceBefore=5, spaceAfter=3, keepWithNext=True,
    ),
    "body": ParagraphStyle(
        "BodyCN", fontName="CN", fontSize=8.65, leading=13.1, textColor=INK,
        spaceAfter=4.2, wordWrap="CJK",
    ),
    "small": ParagraphStyle(
        "SmallCN", fontName="CN", fontSize=7.6, leading=10.6, textColor=MUTED,
        spaceAfter=3, wordWrap="CJK",
    ),
    "bullet": ParagraphStyle(
        "BulletCN", fontName="CN", fontSize=8.35, leading=12.4, textColor=INK,
        leftIndent=10, firstLineIndent=-7, bulletIndent=2, spaceAfter=2.3,
        wordWrap="CJK",
    ),
    "callout": ParagraphStyle(
        "CalloutCN", fontName="CN", fontSize=8.4, leading=12.5, textColor=INK,
        leftIndent=7, rightIndent=7, borderWidth=0.7, borderColor=LINE,
        borderPadding=7, backColor=BLUE_PALE, spaceBefore=4, spaceAfter=6,
        wordWrap="CJK",
    ),
    "code": ParagraphStyle(
        "CodeCN", fontName="CodeCN", fontSize=5.55, leading=7.05,
        textColor=colors.HexColor("#24313A"), leftIndent=6, rightIndent=6,
        spaceBefore=2, spaceAfter=5, backColor=CODE_BG, borderColor=LINE,
        borderWidth=0.45, borderPadding=5,
    ),
    "table_head": ParagraphStyle(
        "TableHeadCN", fontName="CN-Bold", fontSize=7.1, leading=9,
        textColor=colors.white, alignment=TA_CENTER, wordWrap="CJK",
    ),
    "table": ParagraphStyle(
        "TableCN", fontName="CN", fontSize=6.7, leading=9.1,
        textColor=INK, wordWrap="CJK",
    ),
    "table_code": ParagraphStyle(
        "TableCodeCN", fontName="CodeCN", fontSize=6.2, leading=8.3,
        textColor=INK, wordWrap="CJK",
    ),
    "footer": ParagraphStyle(
        "FooterCN", fontName="CN", fontSize=6.8, leading=8, textColor=MUTED,
    ),
}


def esc(text: object) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\n", "<br/>")
    )


def P(text: str, style: str = "body") -> Paragraph:
    return Paragraph(text, ST[style])


def bullet(text: str) -> Paragraph:
    return Paragraph("• " + text, ST["bullet"])


def label(text: str) -> Paragraph:
    return Paragraph(text, ParagraphStyle(
        "SourceLabel", parent=ST["small"], fontName="CN-Bold", textColor=AMBER,
        spaceBefore=4, spaceAfter=2, keepWithNext=True,
    ))


def soft_wrap_code(source: str, width: int = 100) -> str:
    out: list[str] = []
    for raw in source.rstrip().splitlines():
        line = raw.expandtabs(4)
        if len(line) <= width:
            out.append(line)
            continue
        indent = len(line) - len(line.lstrip(" "))
        remaining = line
        first = True
        while len(remaining) > width:
            cut = remaining.rfind(" ", 0, width + 1)
            if cut <= indent + 8:
                cut = width
            out.append(remaining[:cut].rstrip() + "  ↩")
            remaining = " " * (indent + 4) + remaining[cut:].lstrip()
            first = False
        if remaining or first:
            out.append(remaining)
    return "\n".join(out)


def code(source: str) -> Preformatted:
    return Preformatted(soft_wrap_code(source), ST["code"], maxLineLength=110)


def source_file(rel: str) -> tuple[str, ast.Module]:
    text = (ROOT / rel).read_text(encoding="utf-8")
    return text, ast.parse(text)


def _node_source(rel: str, node: ast.AST) -> str:
    text = (ROOT / rel).read_text(encoding="utf-8")
    lines = text.splitlines()
    starts = [getattr(d, "lineno", node.lineno) for d in getattr(node, "decorator_list", [])]
    start = min(starts + [node.lineno]) - 1
    return "\n".join(lines[start: node.end_lineno])


def function_source(rel: str, name: str) -> str:
    _, tree = source_file(rel)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return _node_source(rel, node)
    raise KeyError(f"{rel}: {name}")


def method_source(rel: str, class_name: str, method_name: str) -> str:
    _, tree = source_file(rel)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == method_name:
                    return _node_source(rel, child)
    raise KeyError(f"{rel}: {class_name}.{method_name}")


def class_source(rel: str, class_name: str) -> str:
    _, tree = source_file(rel)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return _node_source(rel, node)
    raise KeyError(f"{rel}: {class_name}")


def exact_lines(rel: str, start: int, end: int) -> str:
    lines = (ROOT / rel).read_text(encoding="utf-8").splitlines()
    return "\n".join(lines[start - 1:end])


class FlowBox(Flowable):
    def __init__(self, title: str, lines: list[str], width: float = 166 * mm):
        super().__init__()
        self.title = title
        self.lines = lines
        self.width = width
        self.box_h = 14 * mm
        self.gap = 3.2 * mm
        self.height = len(lines) * (self.box_h + self.gap) - self.gap + 9 * mm

    def wrap(self, availWidth, availHeight):
        return min(self.width, availWidth), self.height

    def draw(self):
        c = self.canv
        c.setFont("CN-Bold", 9.2)
        c.setFillColor(INK)
        c.drawString(0, self.height - 7 * mm, self.title)
        y = self.height - 12 * mm - self.box_h
        palette = [BLUE_PALE, TEAL_PALE, AMBER_PALE]
        for idx, line in enumerate(self.lines):
            c.setFillColor(palette[idx % len(palette)])
            c.setStrokeColor(LINE)
            c.roundRect(0, y, self.width, self.box_h, 2 * mm, fill=1, stroke=1)
            c.setFillColor(INK)
            c.setFont("CN-Bold", 7.8)
            parts = line.split("｜", 1)
            c.drawString(4 * mm, y + 9.5 * mm, parts[0])
            if len(parts) > 1:
                c.setFont("CN", 7.2)
                c.setFillColor(MUTED)
                c.drawString(4 * mm, y + 4.3 * mm, parts[1])
            if idx < len(self.lines) - 1:
                c.setStrokeColor(BLUE)
                c.setLineWidth(1.1)
                c.line(self.width / 2, y - 1.5 * mm, self.width / 2, y - self.gap + 1.5 * mm)
                c.line(self.width / 2, y - self.gap + 1.5 * mm, self.width / 2 - 1.5 * mm, y - self.gap + 3 * mm)
                c.line(self.width / 2, y - self.gap + 1.5 * mm, self.width / 2 + 1.5 * mm, y - self.gap + 3 * mm)
            y -= self.box_h + self.gap


def table(data: list[list[object]], widths: list[float], repeat_rows: int = 1) -> Table:
    rows: list[list[object]] = []
    for r, row in enumerate(data):
        rows.append([
            cell if isinstance(cell, Flowable) else P(esc(cell), "table_head" if r == 0 else "table")
            for cell in row
        ])
    t = Table(rows, colWidths=widths, repeatRows=repeat_rows, hAlign="LEFT")
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), BLUE),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.35, LINE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, PAPER]),
    ]))
    return t


def sha_snapshot(files: list[str]) -> str:
    h = hashlib.sha256()
    for rel in files:
        h.update(rel.encode())
        h.update((ROOT / rel).read_bytes())
    return h.hexdigest()[:16]


FILES = [
    "app/services/rag_agent_service.py",
    "app/tools/__init__.py",
    "app/tools/knowledge_tool.py",
    "app/services/rag_query_rewrite_service.py",
    "app/services/rag_search_service.py",
    "app/services/keyword_retriever.py",
    "app/services/reciprocal_rank_fusion.py",
    "app/services/rag_reranker_service.py",
    "app/services/local_cross_encoder_reranker_provider.py",
    "app/services/rag_context_builder.py",
    "app/services/vector_store_manager.py",
    "app/services/vector_embedding_service.py",
    "app/core/milvus_client.py",
    "app/schemas/rag.py",
    "app/config.py",
]


def header_footer(canvas, doc):
    canvas.saveState()
    canvas.setStrokeColor(LINE)
    canvas.setLineWidth(0.35)
    canvas.line(18 * mm, PAGE_H - 13 * mm, PAGE_W - 18 * mm, PAGE_H - 13 * mm)
    canvas.setFont("CN", 6.8)
    canvas.setFillColor(MUTED)
    canvas.drawString(18 * mm, PAGE_H - 10 * mm, "RAG 整体链路学习笔记｜本地源码快照")
    canvas.drawRightString(PAGE_W - 18 * mm, 9 * mm, f"{doc.page}")
    canvas.restoreState()


def add_source(story: list, title: str, rel: str, src: str) -> None:
    story.append(label(f"【真实源码】{title}  ·  {rel}  ·  长行仅显示层软换行"))
    story.append(code(src))


def add_logic(story: list, title: str, paragraphs: list[str], bullets: list[str] | None = None) -> None:
    story.append(P(title, "h3"))
    for para in paragraphs:
        story.append(P(para))
    for item in bullets or []:
        story.append(bullet(item))


def build_story() -> list:
    story: list = []
    fingerprint = sha_snapshot(FILES)

    story += [
        Spacer(1, 10 * mm),
        P("RAG 整体链路学习笔记", "title"),
        P("从一个 Query 到 BuiltContext、Citation，再回到 Agent", "subtitle"),
        Spacer(1, 4 * mm),
        P(
            "<b>分析对象：</b>D:\\tries\\agent 的当前磁盘源码<br/>"
            f"<b>分析日期：</b>{date.today().isoformat()}<br/>"
            f"<b>源码快照指纹：</b>{fingerprint}（所列核心文件内容的 SHA-256 前 16 位）<br/>"
            "<b>版本说明：</b>该目录不是 Git 工作树，因此没有可核验的 commit/hash；本笔记严格对应生成时的本地磁盘快照。",
            "callout",
        ),
        Spacer(1, 3 * mm),
        FlowBox("当前默认主链", [
            "RagAgentService｜Agent 接收用户问题，并决定调用 retrieve_knowledge",
            "knowledge_tool｜提取可用聊天历史，启动异步 RAG",
            "Query Rewrite｜保留原问题，生成至多 2 个改写问题",
            "Dense + BM25｜Milvus L2 与词法召回独立产生候选与名次",
            "RRF Fusion｜只融合名次，按文档键去重并保留 provenance",
            "BGE Rerank｜原问题 × 候选正文交叉编码，重排有界候选池",
            "Context Builder｜top-k + token budget 选择证据并生成 [ref:n]",
            "Agent｜工具内容进入模型上下文，最终回答引用对应证据",
        ]),
        Spacer(1, 4 * mm),
        P("阅读边界", "h2"),
        P("只追踪已实现的核心 RAG 运行链路。Planner、AgentState、Memory、MCP、Verifier、评测代码与未来优化均不展开。同步的 <font name='CodeCN'>search_knowledge</font> / <font name='CodeCN'>dense_search_compat</font> 仅作为兼容路径点到为止；默认完整链以异步 <font name='CodeCN'>retrieve_knowledge → asearch_knowledge → RagSearchService.asearch</font> 为准。"),
        PageBreak(),
    ]

    story += [P("1. Agent 如何触发 Knowledge Tool", "h1")]
    add_source(story, "默认工具注册", "app/tools/__init__.py", (ROOT / "app/tools/__init__.py").read_text(encoding="utf-8"))
    add_source(story, "Agent 初始化", "app/services/rag_agent_service.py", method_source("app/services/rag_agent_service.py", "RagAgentService", "_initialize_agent"))
    add_source(story, "一次非流式查询入口", "app/services/rag_agent_service.py", method_source("app/services/rag_agent_service.py", "RagAgentService", "query"))
    add_logic(story, "运行语义", [
        "<font name='CodeCN'>RagAgentService</font> 把 <font name='CodeCN'>retrieve_knowledge</font> 放进 <font name='CodeCN'>DEFAULT_LOCAL_AGENT_TOOLS</font>，再交给 LangChain <font name='CodeCN'>create_agent</font>。因此“触发知识库”不是普通 Python 直接调用，而是 Agent 根据系统提示与当前问题选择工具。",
        "<font name='CodeCN'>query()</font> 只把用户问题包装成 <font name='CodeCN'>HumanMessage</font> 并调用 Agent。工具运行产生的文本结果随后成为 Agent 可见的工具消息；模型据此生成最终 <font name='CodeCN'>AIMessage</font>。checkpointer 通过同一 <font name='CodeCN'>thread_id</font> 维持会话消息状态。",
    ], [
        "输入：<font name='CodeCN'>question</font>、会话级 <font name='CodeCN'>thread_id</font>。",
        "调用者与检索实现解耦：Agent 只认识工具契约，不认识 Milvus、BM25 或 reranker。",
        "返回：最终 Agent 文本；引用是否正确使用，还受系统提示中“关键事实附 [ref:n]”约束。",
    ])
    story.append(Spacer(1, 5 * mm))

    story += [P("2. knowledge_tool：把会话状态接到 RAG", "h1")]
    add_source(story, "过滤可用于改写的历史", "app/tools/knowledge_tool.py", function_source("app/tools/knowledge_tool.py", "extract_rewrite_history"))
    add_source(story, "Agent 工具入口", "app/tools/knowledge_tool.py", function_source("app/tools/knowledge_tool.py", "retrieve_knowledge"))
    add_source(story, "异步完整检索入口", "app/tools/knowledge_tool.py", function_source("app/tools/knowledge_tool.py", "asearch_knowledge"))
    add_logic(story, "历史与当前问题", [
        "工具参数 <font name='CodeCN'>query</font> 是 Agent 为本次检索提供的当前搜索表达；<font name='CodeCN'>runtime.state['messages']</font> 是同一会话中的 LangChain 消息。历史提取只保留用户消息，以及“不带 tool_calls 的助手自然语言消息”；工具协议消息被排除，避免把 JSON、工具调用参数或工具结果误当成对话语义。",
        "若最后一条用户消息与当前 <font name='CodeCN'>query</font> 相同，它会被移除。这样 rewrite prompt 中“历史”与“当前问题”不会重复。最后只保留 6 条，控制改写提示的噪声与成本。",
    ])
    add_logic(story, "content_and_artifact 双通道", [
        "装饰器声明 <font name='CodeCN'>response_format='content_and_artifact'</font>。成功时，content 是 <font name='CodeCN'>BuiltContext.context</font>，直接给 Agent 阅读；artifact 是 <font name='CodeCN'>KnowledgeArtifact</font>，保存 documents、trace 与 citations，便于程序侧观察和渲染。两者来自同一次选择结果。",
        "rewrite 失败会降级为只搜原问题；RAG 整体失败会返回稳定的中文提示及 error artifact；无证据返回“没有找到相关信息”。这些是运行边界，不会伪造上下文。",
    ])
    story.append(Spacer(1, 5 * mm))

    story += [P("3. History-aware Query Rewrite", "h1")]
    add_source(story, "历史文本化", "app/services/rag_query_rewrite_service.py", method_source("app/services/rag_query_rewrite_service.py", "QueryRewriteService", "_history_text"))
    add_source(story, "结构化改写", "app/services/rag_query_rewrite_service.py", method_source("app/services/rag_query_rewrite_service.py", "QueryRewriteService", "rewrite"))
    add_source(story, "多查询构造", "app/services/rag_search_service.py", method_source("app/services/rag_search_service.py", "RagSearchService", "build_queries"))
    add_logic(story, "数据如何变化", [
        "历史先变成 <font name='CodeCN'>&lt;role&gt;: &lt;text&gt;</font> 行，再与 original query 一起发送给零温度改写模型。Prompt 限定：只根据历史消解指代，保留实体、版本号、错误码、类名与函数名；至多返回两个不同的搜索查询；不能原样返回 original。",
        "模型输出由 <font name='CodeCN'>with_structured_output(QueryRewriteResult)</font> 约束。服务端仍做二次清洗：去空、去重、排除 original、截断到 2 条。任何超时或解析异常都会返回 <font name='CodeCN'>warning</font>，而不是中断检索。",
        "真正进入召回的 <font name='CodeCN'>queries</font> 由 <font name='CodeCN'>build_queries</font> 生成：original 永远在首位，再追加不重复 rewrites，最多 3 条。这里把“用户原意保底”和“搜索表达扩展”同时保留。",
    ])
    story.append(P("<b>核心结构：</b> <font name='CodeCN'>Query → QueryRewriteResult(rewrites, reason, warning) → [original, rewrite_1, rewrite_2]</font>", "callout"))
    story.append(Spacer(1, 5 * mm))

    story += [P("4. Retrieval 编排：一组 Query 进入两路召回", "h1")]
    add_source(story, "按模式组织候选", "app/services/rag_search_service.py", method_source("app/services/rag_search_service.py", "RagSearchService", "_retrieve_candidates"))
    add_source(story, "完整异步主流程", "app/services/rag_search_service.py", method_source("app/services/rag_search_service.py", "RagSearchService", "asearch"))
    add_logic(story, "默认 hybrid + rerank", [
        "默认 <font name='CodeCN'>retrieval_mode='hybrid'</font>：Dense 与 BM25 分别接收同一组 queries，各自建立候选与 rank，随后才进入 RRF。两个检索器的原始分数不在此处归一化，也不互相覆盖。",
        "RRF 结果若存在 reranker，则以 <b>original query</b> 调用 rerank；rewrites 用于扩大召回面，但交叉编码器的最终相关性判断回到用户原问题。rerank 后先截取 final top-k，再交给 Context Builder；token budget 仍可能让最终入选少于 top-k。",
        "<font name='CodeCN'>RagSearchResult.documents</font> 最后被替换为真正进入上下文的 selected documents，<font name='CodeCN'>ranked</font> 仍保存候选排序对象，<font name='CodeCN'>built_context</font> 保存文本、引用、token 与跳过数量。",
    ])
    story.append(Spacer(1, 5 * mm))

    story += [P("5. Dense Retrieval：Embedding → Milvus → L2", "h1")]
    add_source(story, "多查询 Dense 召回与去重", "app/services/rag_search_service.py", method_source("app/services/rag_search_service.py", "RagSearchService", "_dense_search"))
    add_source(story, "Milvus 带分数检索", "app/services/vector_store_manager.py", method_source("app/services/vector_store_manager.py", "VectorStoreManager", "search_with_scores"))
    add_source(story, "Query embedding", "app/services/vector_embedding_service.py", method_source("app/services/vector_embedding_service.py", "DashScopeEmbeddings", "embed_query"))
    add_source(story, "向量索引度量", "app/core/milvus_client.py", method_source("app/core/milvus_client.py", "MilvusClientManager", "_create_collection"))
    add_logic(story, "Dense 数据流", [
        "每个 query 都调用 <font name='CodeCN'>similarity_search_with_score</font>。LangChain Milvus 使用初始化时注入的 <font name='CodeCN'>vector_embedding_service</font> 把查询文本编码为向量，再在 Milvus 的 <font name='CodeCN'>FLOAT_VECTOR</font> 字段上搜索。索引明确配置 <font name='CodeCN'>metric_type='L2'</font>，因此返回的 <font name='CodeCN'>distance</font> 越小越相近。",
        "所有 query 的结果先按 distance 全局升序，再按文档键去重。若同一 chunk 被多个 query 命中，保留最小 distance，并把命中的 query 合并进 <font name='CodeCN'>query_hits</font>；之后赋予 1-based <font name='CodeCN'>dense_rank</font>。",
        "文档键优先使用 <font name='CodeCN'>doc_id</font>，其次 <font name='CodeCN'>chunk_id</font>，再退化到 source/chunk_index 或 source/content。它是整个多查询与多路融合去重的一致身份策略。",
    ], [
        "distance 不是概率，也不是“越大越相关”的 similarity；在本项目中它是 L2 距离，越小越好。",
        "Embedding 服务默认模型与维度来自配置；Milvus collection 的向量维度必须与之匹配。",
    ])
    story.append(Spacer(1, 5 * mm))

    story += [P("6. BM25 Retrieval：词法召回保持独立", "h1")]
    add_source(story, "BM25 分词", "app/services/keyword_retriever.py", function_source("app/services/keyword_retriever.py", "tokenize_for_bm25"))
    add_source(story, "BM25 检索", "app/services/keyword_retriever.py", method_source("app/services/keyword_retriever.py", "InMemoryBM25Retriever", "search"))
    add_source(story, "多查询 BM25 聚合", "app/services/rag_search_service.py", method_source("app/services/rag_search_service.py", "RagSearchService", "_bm25_search"))
    add_logic(story, "词法路径的真实语义", [
        "BM25 使用 <font name='CodeCN'>rank_bm25.BM25Okapi</font>。分词器保留完整代码标识符，同时增加下划线与 camelCase 子词；英文转小写；中文按单字进入 token 序列。这让错误码、函数名、配置键等精确词法信号不必依赖向量语义。",
        "索引是从 <font name='CodeCN'>vector_store_manager.list_documents()</font> 读取的 Milvus 文档快照，按进程惰性创建。搜索分数越大越好；结果先按 score 降序，再用原文档序号稳定打破平局。无词汇交集时直接返回空。",
        "多 query 聚合时，同一文档收集全部 <font name='CodeCN'>query_hits</font>；代表项优先选择更好的 query 内 rank，平 rank 再选更高 score。最终赋予全局 <font name='CodeCN'>bm25_rank</font>，distance 保持 None。",
    ])
    story.append(P("<b>重要边界：</b>Dense 的 L2 distance 与 BM25 score 量纲完全不同。当前实现不做分数加权，而是等到 RRF 只融合 rank。", "callout"))
    story.append(Spacer(1, 5 * mm))

    story += [P("7. RRF：只融合名次，保留所有 provenance", "h1")]
    add_source(story, "Reciprocal Rank Fusion", "app/services/reciprocal_rank_fusion.py", function_source("app/services/reciprocal_rank_fusion.py", "reciprocal_rank_fuse"))
    add_logic(story, "公式与去重", [
        "每个检索器对同一文档贡献 <font name='CodeCN'>1 / (rrf_k + rank)</font>。默认 <font name='CodeCN'>rrf_k=60</font>；文档同时出现在 Dense 与 BM25 时，两项相加。RRF 只看相对名次，因此不需要把 L2 与 BM25 score 映射到共同刻度。",
        "融合池仍使用统一 document key 去重。Dense 分支写入 distance、dense_rank 与 query_hits；BM25 分支补充 bm25_score、bm25_rank 与 query_hits。最终 <font name='CodeCN'>RetrievedDocument</font> 同时保留原始指标、RRF 分数和 <font name='CodeCN'>matched_retrievers</font>，这就是 provenance。",
        "排序先按 rrf_score 降序；平局时依次偏向更好的 dense_rank、更好的 bm25_rank、稳定 key。输出限制为 hybrid candidate limit。",
    ])
    story.append(P("<b>例：</b>若某 chunk 的 dense_rank=2、bm25_rank=5，则默认 RRF 分数为 <font name='CodeCN'>1/(60+2)+1/(60+5)≈0.03152</font>。原始 distance 与 bm25_score 仍原样保留，但不进入公式。", "callout"))
    story.append(Spacer(1, 5 * mm))

    story += [P("8. BGE Rerank：候选重排，不再负责召回", "h1")]
    add_source(story, "有界 rerank 与降级", "app/services/rag_reranker_service.py", method_source("app/services/rag_reranker_service.py", "RerankerService", "rerank"))
    add_source(story, "本地 Cross-Encoder 打分", "app/services/local_cross_encoder_reranker_provider.py", method_source("app/services/local_cross_encoder_reranker_provider.py", "TransformersCrossEncoderProvider", "_score_sync"))
    add_source(story, "异步线程封装", "app/services/local_cross_encoder_reranker_provider.py", method_source("app/services/local_cross_encoder_reranker_provider.py", "TransformersCrossEncoderProvider", "score"))
    add_logic(story, "pair、score 与 async", [
        "Reranker 只处理候选池前 <font name='CodeCN'>candidate_limit</font> 条。Provider 构造的是 <font name='CodeCN'>(question, document.page_content)</font> pair：tokenizer 的第一序列是重复的原问题，第二序列是各候选正文；模型 logits 展平后成为 <font name='CodeCN'>rerank_score</font>，越大越相关。",
        "本地 Transformers 推理是同步 CPU/GPU 工作，<font name='CodeCN'>asyncio.to_thread</font> 把它移出事件循环；外层再用 <font name='CodeCN'>asyncio.wait_for</font> 施加超时。分数数量、类型与有限性都被校验。",
        "成功时只按 rerank_score 降序重排已打分 pool，tail 原顺序接在后面。<font name='CodeCN'>dataclasses.replace</font> 只增加 rerank_score，不改 distance、BM25、RRF 或 provenance。失败、禁用或 provider 不可用时回退到 RRF/BM25/L2 的既有顺序。",
    ])
    story.append(P("默认模型：<font name='CodeCN'>BAAI/bge-reranker-v2-m3</font>，本地缓存、offline 加载。它解决“候选之间谁更贴近原问题”，不解决“遗漏文档如何被召回”。", "callout"))
    story.append(Spacer(1, 5 * mm))

    story += [P("9. Context Builder：把排序结果变成可消费证据", "h1")]
    add_source(story, "上下文预算与 Citation 同步生成", "app/services/rag_context_builder.py", method_source("app/services/rag_context_builder.py", "ContextBuilder", "build"))
    add_source(story, "核心产物结构", "app/schemas/rag.py", "\n\n".join([
        class_source("app/schemas/rag.py", "Citation"),
        class_source("app/schemas/rag.py", "BuiltContext"),
    ]))
    add_logic(story, "选择、预算、格式", [
        "输入已经是最终排序候选。循环同时受两道门约束：<font name='CodeCN'>final_top_k</font> 限制最多选几条；<font name='CodeCN'>token_budget</font> 限制这些证据块合计多大。默认混合语言估算：CJK 字符按 1 token，其余字符约每 4 个算 1 token。",
        "每条证据先被格式化为 <font name='CodeCN'>[ref:n] / file / source / heading / content</font>。若整块加入后超预算，该条被跳过，循环继续尝试后续候选；不会截断正文。因而 selected count 可能小于 top-k，也可能跳过一个大块后纳入一个较小块。",
        "<font name='CodeCN'>ref_id=len(selected)+1</font> 只在真正入选时增长。文本 block 与 Citation 在同一个分支追加，所以 <font name='CodeCN'>[ref:n]</font>、<font name='CodeCN'>citations[n-1]</font> 和 selected_documents[n-1] 一一对应。source/file/heading/line 只读取已有 metadata，不会编造。",
    ])
    story.append(Spacer(1, 5 * mm))

    story += [P("10. 核心数据结构一路如何变化", "h1")]
    add_source(story, "检索与上下文核心 schema", "app/schemas/rag.py", "\n\n".join([
        class_source("app/schemas/rag.py", "QueryRewriteResult"),
        class_source("app/schemas/rag.py", "RetrievedDocument"),
        class_source("app/schemas/rag.py", "RagSearchResult"),
    ]))
    story.append(table([
        ["阶段", "主要结构", "新增/保留的信息", "交给下一步"],
        ["用户输入", "str Query + messages", "原问题、thread 会话历史", "history filter"],
        ["Rewrite", "QueryRewriteResult", "rewrites / reason / warning；原问题未丢失", "最多 3 个 queries"],
        ["Dense", "RetrievedDocument", "distance、dense_rank、query_hits、dense provenance", "Dense 候选表"],
        ["BM25", "RetrievedDocument", "bm25_score、bm25_rank、query_hits、bm25 provenance", "BM25 候选表"],
        ["RRF", "RetrievedDocument", "rrf_score、双路原始指标、matched_retrievers", "融合候选池"],
        ["BGE", "RetrievedDocument", "rerank_score；其余值不改", "重排候选池"],
        ["Context", "BuiltContext", "context、citations、token_count、selected/skipped", "工具 content + artifact"],
        ["Agent", "ToolMessage / AIMessage", "证据文本可读；artifact 可观察", "带 [ref:n] 的最终回答"],
    ], [23*mm, 34*mm, 70*mm, 39*mm]))
    story.append(Spacer(1, 5 * mm))
    story.append(P("五种“分数/名次”不能混为一谈", "h2"))
    story.append(table([
        ["字段", "产生阶段", "方向", "真实含义", "是否直接进入下一公式"],
        ["distance", "Milvus Dense", "越小越好", "Query embedding 与 chunk vector 的 L2 距离", "RRF 不用；保留作 provenance"],
        ["bm25_score", "BM25", "越大越好", "词项匹配与文档统计得到的词法相关分", "RRF 不用；保留作 provenance"],
        ["dense_rank / bm25_rank", "各自召回", "1 最好", "各路内部相对次序", "RRF 的直接输入"],
        ["rrf_score", "RRF", "越大越好", "各路倒数名次贡献之和", "决定送入 rerank 的融合次序"],
        ["rerank_score", "BGE", "越大越好", "Cross-Encoder 对原问题/正文 pair 的 logit", "决定最终候选次序"],
    ], [27*mm, 28*mm, 20*mm, 68*mm, 23*mm]))
    story.append(Spacer(1, 5 * mm))

    story += [P("11. 一个 Query 的完整运行实例", "h1")]
    story.append(P("以下是结构示例，用来映射代码路径，不代表项目中的真实检索数据。用户在已有对话里问：<b>“那它的超时怎么配置？”</b>，上文正在讨论 reranker。", "callout"))
    story.append(table([
        ["步骤", "示例数据", "结构变化/判断"],
        ["Agent", "HumanMessage('那它的超时怎么配置？')", "模型选择 retrieve_knowledge"],
        ["History filter", "human/AI 自然语言消息；剔除 tool calls 与当前重复问题", "得到至多 6 条 rewrite history"],
        ["Rewrite", "original + 'reranker timeout configuration' + 'rag_rerank_timeout_seconds setting'", "原问题首位，至多 2 条扩展"],
        ["Dense", "每个 query → embedding → Milvus L2", "同一 chunk 去重，保留最小 distance 与全部 query_hits"],
        ["BM25", "timeout / reranker / config 等词法命中", "保留 bm25_score、bm25_rank"],
        ["RRF", "同一 chunk 的 Dense/BM25 rank 相加", "得到 rrf_score 与 matched_retrievers"],
        ["BGE", "('那它的超时怎么配置？', chunk正文)", "写入 rerank_score 并重排"],
        ["Context", "[ref:1] file/source/heading/content", "top-k 内再受 1800 token 预算约束；同步生成 Citation(1)"],
        ["Agent", "工具 content 进入上下文", "依据证据回答，并在关键事实后附 [ref:1]"],
    ], [25*mm, 75*mm, 66*mm]))
    story.append(Spacer(1, 6 * mm))
    add_logic(story, "三个职责边界", [
        "Query Rewrite 只改善“用什么表达去搜”，不决定文档相关性；Dense/BM25 只扩大和建立候选，不负责最终上下文预算；BGE 只重排候选，不生成引用。",
        "Context Builder 不重新检索，也不重新判断语义；它把已经排好序的候选放进可控预算，并将证据位置固化为 citation。Agent 才负责基于这些证据生成自然语言回答。",
        "Citation 是证据定位协议，不是事实正确性的独立证明。它保证 [ref:n] 能回到入选 chunk 的 metadata 和分数，但模型仍必须按系统提示正确引用。",
    ])
    story.append(Spacer(1, 5 * mm))

    story += [P("12. 当前默认配置与运行边界", "h1")]
    story.append(table([
        ["配置", "当前默认", "链路影响"],
        ["rag_retrieval_mode", "hybrid", "Dense 与 BM25 同时召回，随后 RRF"],
        ["rag_top_k", "3", "rerank 后最多 3 条交给 Context Builder"],
        ["rag_bm25_top_k", "20", "每个 query 的 BM25 候选上限"],
        ["rag_hybrid_candidate_k", "20", "融合候选池上限"],
        ["rag_rrf_k", "60", "RRF 公式中的平滑常数"],
        ["rag_enable_rerank", "True", "启用本地 BGE 交叉编码重排"],
        ["rag_rerank_top_k", "12", "进入 reranker 的候选池目标上限"],
        ["rag_rerank_model", "BAAI/bge-reranker-v2-m3", "本地 offline Transformers 模型"],
        ["rag_context_token_budget", "1800", "最终证据上下文预算"],
        ["embedding model / dim", "text-embedding-v4 / 1024", "Query 与文档向量维度；Milvus schema 必须一致"],
        ["Milvus metric / index", "L2 / IVF_FLAT", "Dense distance 越小越好"],
    ], [46*mm, 49*mm, 71*mm]))
    story.append(Spacer(1, 6 * mm))
    story.append(P("失败与降级", "h2"))
    for item in [
        "Rewrite 超时/解析失败：warning 记录，继续只搜 original query。",
        "Reranker 失败/超时：回到 RRF（hybrid）、BM25 rank（纯 BM25）或 L2（Dense）的既有顺序。",
        "检索主流程异常：工具返回“知识库检索暂时不可用”与 error artifact。",
        "没有候选或预算后无上下文：工具返回“没有找到相关信息”，不伪造 citation。",
        "BM25 是进程内惰性快照；创建后 Milvus 数据变化不会自动刷新该实例。",
    ]:
        story.append(bullet(item))
    story.append(P("<b>实现细节提醒：</b><font name='CodeCN'>RerankerService</font> 将候选上限至少提升到 10、最多受 <font name='CodeCN'>max_candidate_limit=15</font> 约束；因此默认配置 12 最终实际为 12。", "callout"))
    story.append(Spacer(1, 5 * mm))

    story += [P("13. 完整 RAG 数据流图", "h1")]
    story.append(FlowBox("从 Query 到 Citation，再回到 Agent", [
        "① User Query｜HumanMessage + thread_id 进入 RagAgentService",
        "② Agent Tool Choice｜retrieve_knowledge 已注册为默认本地工具",
        "③ History Filter｜保留 Human/普通 AI 消息，剔除 tool protocol 与重复当前问题",
        "④ Rewrite｜QueryRewriteResult；original + ≤2 rewrites，最多 3 个 queries",
        "⑤ Dense｜每个 query → embedding → Milvus L2 → distance/dense_rank/query_hits",
        "⑥ BM25｜每个 query → 代码友好分词 → bm25_score/bm25_rank/query_hits",
        "⑦ RRF｜Σ 1/(60+rank)；按 document key 去重；保留双路 provenance",
        "⑧ BGE Rerank｜original query × candidate.page_content → rerank_score",
        "⑨ Final Slice｜按 rerank 次序取 top-k（默认 3）",
        "⑩ Context Builder｜token budget（默认 1800）选择完整块，不截断正文",
        "11. BuiltContext｜context + Citation[] + selected_documents + token/skipped",
        "12. knowledge_tool｜content 给 Agent；artifact 保存 documents/trace/citations",
        "13. Agent Answer｜基于证据生成回答，关键事实使用对应 [ref:n]",
    ]))
    story.append(Spacer(1, 4 * mm))
    story.append(P("各阶段谁负责什么", "h2"))
    story.append(table([
        ["阶段", "唯一核心职责", "明确不负责"],
        ["Query Rewrite", "解决搜索表达、指代与多查询覆盖", "不召回、不排序文档"],
        ["Dense / BM25", "从语义与词法两侧召回候选", "不跨量纲混分、不控制最终预算"],
        ["RRF", "用名次融合多路候选并保留来源", "不读取正文做深层相关性判断"],
        ["BGE", "对原问题/正文 pair 做候选重排", "不扩大候选、不生成上下文"],
        ["Context Builder", "控制 top-k/token 预算，建立 ref 与证据映射", "不重新检索或改写事实"],
        ["Citation", "定位入选 chunk 的文件、来源、标题、行与分数", "不替代事实核验"],
        ["Agent", "基于工具证据生成最终自然语言回答", "不应编造未提供的引用"],
    ], [34*mm, 72*mm, 60*mm]))
    story.append(Spacer(1, 6 * mm))
    story.append(P("主调用链（按实际实现）", "h2"))
    story.append(P(
        "<font name='CodeCN'>RagAgentService.query/query_stream → LangChain Agent → retrieve_knowledge → extract_rewrite_history → asearch_knowledge → QueryRewriteService.rewrite → RagSearchService.asearch → (_dense_search + _bm25_search) → reciprocal_rank_fuse → RerankerService.rerank → LocalCrossEncoderRerankerProvider.score → ContextBuilder.build → BuiltContext → KnowledgeArtifact/content → Agent final AIMessage</font>",
        "callout",
    ))
    story.append(Spacer(1, 5 * mm))

    story += [P("14. 核心模块索引", "h1")]
    story.append(table([
        ["模块", "主链职责"],
        ["app/services/rag_agent_service.py", "建立 Agent、注册工具、承接用户消息与最终 AIMessage"],
        ["app/tools/knowledge_tool.py", "从 runtime 取历史，协调 rewrite/search，返回 content + artifact"],
        ["app/services/rag_query_rewrite_service.py", "结构化 history-aware rewrite 与降级"],
        ["app/services/rag_search_service.py", "多查询、两路召回、融合、rerank、context 的总编排"],
        ["app/services/vector_store_manager.py", "LangChain Milvus 初始化与带 L2 distance 检索"],
        ["app/services/vector_embedding_service.py", "Query/document embedding API"],
        ["app/core/milvus_client.py", "Milvus schema、1024 维向量约束与 L2 IVF_FLAT 索引"],
        ["app/services/keyword_retriever.py", "代码友好 BM25 分词、进程内索引与词法召回"],
        ["app/services/reciprocal_rank_fusion.py", "rank-only 融合、跨路去重与 provenance 汇总"],
        ["app/services/rag_reranker_service.py", "有界候选重排、超时、校验与确定性降级"],
        ["app/services/local_cross_encoder_reranker_provider.py", "本地 BGE pair 编码和异步线程封装"],
        ["app/services/rag_context_builder.py", "top-k/token 预算、证据格式与 Citation 映射"],
        ["app/schemas/rag.py", "RetrievedDocument、RagSearchResult、BuiltContext、Citation 等契约"],
        ["app/config.py", "hybrid、RRF、rerank、预算、embedding 等默认参数"],
    ], [68*mm, 98*mm]))
    story.append(Spacer(1, 7 * mm))
    story.append(P("学完这条链应该真正掌握什么", "h2"))
    for item in [
        "RAG 不是一次 search 调用，而是表达改写、候选召回、名次融合、深排、预算与证据协议的连续数据变换。",
        "original query 始终保留：rewrites 扩大召回，BGE 又以 original question 校准最终相关性。",
        "L2、BM25、RRF、BGE 是四种不同语义的量；当前实现用 rank-only RRF 避免直接混合异构原始分数。",
        "RetrievedDocument 是主链的“信息载体”：文档不变，distance/rank/provenance/rerank_score 逐阶段累积。",
        "BuiltContext 才是给模型消费的最终证据；Citation 与 context 在同一选择循环中生成，确保编号同步。",
        "Agent 的职责是基于证据回答；Context Builder 的职责是提供有限、可定位、可追踪的证据集合。",
    ]:
        story.append(bullet(item))
    return story


def build_pdf() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    frame = Frame(
        18 * mm, 15 * mm, PAGE_W - 36 * mm, PAGE_H - 31 * mm,
        leftPadding=0, rightPadding=0, topPadding=2 * mm, bottomPadding=0,
    )
    doc = BaseDocTemplate(
        str(OUT), pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
        topMargin=16 * mm, bottomMargin=15 * mm, title="RAG 整体链路学习笔记",
        author="Codex",
    )
    doc.addPageTemplates([PageTemplate(id="main", frames=[frame], onPage=header_footer)])
    doc.build(build_story())
    print(OUT)


if __name__ == "__main__":
    build_pdf()
