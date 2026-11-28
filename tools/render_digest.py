"""把一份日报 Markdown 渲染成可发布为 Artifact 的 HTML 页面（明亮单色主题）。

用法：python3 tools/render_digest.py 2026-11-28.md 2026-11-28.html
需要 Python 的 markdown 包（扩展：tables、toc、fenced_code）。
"""
from __future__ import annotations

import html
import re
import sys
from pathlib import Path

import markdown

CSS = r"""
:root{
  /* 版式：彩色导读头图 + 今日要点卡片 + 顶部吸附的栏目导航；正文按栏目分区，每个主题一张白卡 */
  --bg:#f4f7fb; --paper:#ffffff; --ink:#18212d; --muted:#5d6b7c; --line:#e3e9f1;
  --c1:#2f6fe4; --c1-soft:#e9f0fe;   /* 前沿 */
  --c2:#0f9b7a; --c2-soft:#e3f6f0;   /* 基础 */
  --c3:#d98a06; --c3-soft:#fdf3df;   /* 工具 */
  --c4:#d9476a; --c4-soft:#fde9ee;   /* 面试 */
  --c0:#6b7a8c; --c0-soft:#eef1f5;   /* 其他 */
  --code:#eef2f8;
  --f-body:"Noto Sans SC","PingFang SC","Microsoft YaHei",system-ui,sans-serif;
  --f-head:"Plus Jakarta Sans","Noto Sans SC","PingFang SC",system-ui,sans-serif;
  --f-mono:"JetBrains Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  color-scheme:light;
}
*{box-sizing:border-box}
html{background:var(--bg)}
body{background:var(--bg);color:var(--ink);font-family:var(--f-body);font-size:16px;line-height:1.85}
.gutter{padding-left:max(16px,env(safe-area-inset-left,0px));padding-right:max(16px,env(safe-area-inset-right,0px))}
.inner{max-width:820px;margin:0 auto}

/* 头图 */
.hero{margin-top:calc(-1 * env(safe-area-inset-top,0px));padding-top:calc(env(safe-area-inset-top,0px) + 48px);padding-bottom:40px;
  background:
    radial-gradient(900px 320px at 85% -10%, #d7f3ea 0%, transparent 60%),
    radial-gradient(800px 360px at 0% 0%, #dfe9ff 0%, transparent 60%),
    linear-gradient(180deg,#f9fbff 0%, var(--bg) 100%);}
.hero .date{font-family:var(--f-head);font-weight:800;font-size:clamp(2.6rem,9vw,4.2rem);line-height:1;letter-spacing:-.03em;
  color:var(--c1);font-variant-numeric:tabular-nums}
.hero h1{font-family:var(--f-head);font-size:clamp(1.25rem,3.4vw,1.6rem);font-weight:700;margin:12px 0 0;line-height:1.35;text-wrap:balance}
.hero .meta{display:flex;flex-wrap:wrap;gap:8px;margin-top:16px}
.pill{display:inline-flex;align-items:center;gap:6px;font-size:.8rem;line-height:1;padding:7px 12px;border-radius:999px;
  background:var(--paper);border:1px solid var(--line);color:var(--muted)}
.pill b{color:var(--ink);font-weight:600}
.lede{margin-top:24px;background:rgba(255,255,255,.75);backdrop-filter:blur(6px);border:1px solid var(--line);border-radius:16px;
  padding:18px 22px;font-size:.95rem;color:#2c3746}
.lede p{margin:6px 0}
.lede strong{color:var(--ink)}

/* 今日要点卡片 */
.picks{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:12px;margin-top:20px}
.pick{display:flex;flex-direction:column;gap:6px;min-width:0;text-decoration:none;color:var(--ink);background:var(--paper);
  border:1px solid var(--line);border-radius:14px;padding:14px 16px;transition:transform .15s ease, box-shadow .15s ease}
.pick:hover,.pick:focus-visible{transform:translateY(-2px);box-shadow:0 8px 24px rgba(30,60,110,.10);outline:none}
.pick .k{font-size:.72rem;font-weight:600;letter-spacing:.06em;color:var(--col)}
.pick .t{font-size:.95rem;font-weight:600;line-height:1.45}

/* 栏目导航 */
.bar{position:sticky;top:env(safe-area-inset-top,0px);z-index:5;background:rgba(244,247,251,.92);backdrop-filter:blur(8px);
  border-bottom:1px solid var(--line)}
body::before{content:"";position:fixed;inset:0 0 auto;height:env(safe-area-inset-top,0px);z-index:10;background:var(--bg)}
.bar .inner{display:flex;gap:8px;overflow-x:auto;padding-block:10px;scrollbar-width:none}
.bar .inner::-webkit-scrollbar{display:none}
.bar a{flex:none;font-size:.85rem;font-weight:600;text-decoration:none;color:var(--col);background:var(--soft);
  padding:6px 14px;border-radius:999px}
.bar a:focus-visible{outline:2px solid var(--col);outline-offset:2px}

/* 正文 */
main{padding-block:12px 80px}
section.col{margin-top:40px;scroll-margin-top:64px}
.col-head{display:flex;align-items:center;gap:12px;margin-bottom:16px}
.col-head .no{flex:none;width:40px;height:40px;border-radius:12px;display:grid;place-items:center;background:var(--col);color:#fff;
  font-family:var(--f-head);font-weight:800;font-size:1.05rem}
.col-head h2{margin:0;font-family:var(--f-head);font-size:1.45rem;line-height:1.3;text-wrap:balance}
.col-head .sub{display:block;font-size:.75rem;font-weight:600;letter-spacing:.08em;color:var(--col)}
.card{background:var(--paper);border:1px solid var(--line);border-radius:18px;padding:24px clamp(18px,4vw,32px);margin-top:16px;
  box-shadow:0 1px 2px rgba(20,40,80,.04);border-top:4px solid var(--col);min-width:0;scroll-margin-top:64px}
.card>h3{font-family:var(--f-head);font-size:1.22rem;line-height:1.45;margin:0 0 12px;text-wrap:balance}
.plain{background:var(--paper);border:1px solid var(--line);border-radius:18px;padding:20px clamp(18px,4vw,32px);margin-top:16px}
.card>:last-child,.plain>:last-child{margin-bottom:0}
.c1{--col:var(--c1);--soft:var(--c1-soft)} .c2{--col:var(--c2);--soft:var(--c2-soft)}
.c3{--col:var(--c3);--soft:var(--c3-soft)} .c4{--col:var(--c4);--soft:var(--c4-soft)} .c0{--col:var(--c0);--soft:var(--c0-soft)}

main p,main li,main td{text-wrap:pretty;overflow-wrap:break-word}
main a{color:var(--c1);text-underline-offset:3px;overflow-wrap:anywhere}
main strong{font-weight:650}
main ul,main ol{padding-left:1.3em}
main li{margin:4px 0}
main li::marker{color:var(--col)}
main code{font-family:var(--f-mono);font-size:.84em;background:var(--code);padding:1px 6px;border-radius:5px;overflow-wrap:anywhere}
main pre{background:#f1f5fb;border:1px solid var(--line);padding:14px 16px;border-radius:12px;overflow-x:auto;font-size:.85rem;line-height:1.6}
main pre code{background:none;padding:0;overflow-wrap:normal}
main blockquote{margin:16px 0;padding:12px 16px;border-left:3px solid var(--col);background:var(--soft);border-radius:0 10px 10px 0}
.table-scroll{overflow-x:auto;margin:18px 0;border:1px solid var(--line);border-radius:12px}
table{border-collapse:collapse;width:100%;font-size:.88rem;line-height:1.6;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:10px 14px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-weight:650;background:var(--soft);color:var(--ink);white-space:nowrap}
tr:last-child td{border-bottom:none}
tbody tr:nth-child(even) td{background:#fafcfe}
.callout{margin:18px 0;padding:14px 18px;border-radius:12px;background:var(--cb);border:1px solid var(--cl)}
.callout>.tag{display:inline-block;font-size:.75rem;font-weight:700;letter-spacing:.04em;color:#fff;background:var(--ct);
  padding:3px 10px;border-radius:999px;margin-bottom:6px}
.callout>p:first-of-type{margin-top:4px}
.callout>:last-child{margin-bottom:0}
.k-aletheia{--cb:#e7f7f1;--cl:#bfe8d9;--ct:#0f9b7a}
.k-opinion{--cb:#fff6e3;--cl:#f6dfae;--ct:#d98a06}
.k-limit{--cb:#fdeef1;--cl:#f5c9d3;--ct:#d9476a}
.chip{display:inline-block;font-size:.72rem;font-weight:700;padding:2px 9px;border-radius:999px;background:var(--soft);color:var(--col);
  vertical-align:.15em;margin-right:4px}
main hr{display:none}
footer{color:var(--muted);font-size:.8rem;text-align:center;padding-bottom:40px}
@media (prefers-reduced-motion:no-preference){html{scroll-behavior:smooth}}
@media (prefers-reduced-motion:reduce){.pick{transition:none}}
"""

FONTS = ('<link rel="preconnect" href="https://fonts.googleapis.com">'
         '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
         '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Noto+Sans+SC:wght@400;500;700'
         '&family=Plus+Jakarta+Sans:wght@600;700;800&family=JetBrains+Mono:wght@400;500&display=swap">')

COLUMN = re.compile(r'^栏目([一二三四五六七八九十]+)｜(.+)$')
NUM = {'一': 1, '二': 2, '三': 3, '四': 4}
CALLOUTS = (('落到 Aletheia', 'k-aletheia'), ('Aletheia 里的设计', 'k-aletheia'),
            ('我的判断', 'k-opinion'), ('局限', 'k-limit'))


def strip_tags(s: str) -> str:
    return html.unescape(re.sub(r'<[^>]+>', '', s))


def linkify(chunk: str) -> str:
    # 把正文里的裸网址变成链接；只处理标签之外、不在 <a>/<code> 里的文字
    out, depth = [], 0
    for piece in re.split(r'(<[^>]+>)', chunk):
        if piece.startswith('<'):
            tag = re.match(r'</?(\w+)', piece)
            if tag and tag.group(1) in ('a', 'code', 'pre'):
                depth += -1 if piece.startswith('</') else 1
            out.append(piece)
        elif depth == 0:
            out.append(re.sub(r'https?://[^\s<>（）()，。；、]+',
                              lambda m: f'<a href="{m.group(0)}">{m.group(0)}</a>', piece))
        else:
            out.append(piece)
    return ''.join(out)


def decorate(chunk: str) -> str:
    chunk = linkify(chunk)
    chunk = re.sub(r'【(巩固|拓展)】', r'<span class="chip">\1</span>', chunk)
    chunk = chunk.replace('<table>', '<div class="table-scroll"><table>').replace('</table>', '</table></div>')
    leads = '|'.join(re.escape(x) for x, _ in CALLOUTS)

    def callout(m):
        lead = strip_tags(m.group(1))
        kind = next(k for x, k in CALLOUTS if lead.startswith(x))
        label = re.split(r'[：:（(]', lead)[0].strip()
        return f'<div class="callout {kind}"><span class="tag">{html.escape(label)}</span>{m.group(0)}</div>'
    return re.sub(rf'<p><strong>((?:{leads})[^<]*)</strong>.*?</p>(\s*<ul>.*?</ul>)?', callout, chunk, flags=re.S)


def render(md_text: str):
    md = markdown.Markdown(extensions=['tables', 'fenced_code', 'toc'])
    body = md.convert(md_text)
    h1 = re.search(r'<h1[^>]*>(.*?)</h1>', body, re.S)
    title = strip_tags(h1.group(1)) if h1 else '日报'
    if h1:
        body = body.replace(h1.group(0), '', 1)
    parts = re.split(r'(?=<h2[ >])', body)
    intro, sections = parts[0], parts[1:]
    lede = ''.join(re.findall(r'<blockquote>(.*?)</blockquote>', intro, re.S))

    nav, picks, out = [], [], []
    for sec in sections:
        m = re.match(r'<h2 id="([^"]*)">(.*?)</h2>', sec, re.S)
        sid, name = m.group(1), strip_tags(m.group(2))
        rest = sec[m.end():]
        cm = COLUMN.match(name)
        n = NUM.get(cm.group(1), 0) if cm else 0
        cls = f'c{n}'
        label = cm.group(2) if cm else name
        sub = f'栏目{cm.group(1)}' if cm else '附记'
        nav.append(f'<a class="{cls}" href="#{sid}">{html.escape(label)}</a>')
        cards = re.split(r'(?=<h3[ >])', rest)
        pieces = []
        lead = cards[0].strip()
        if lead and strip_tags(lead).strip():
            pieces.append(f'<div class="plain">{decorate(lead)}</div>')
        for card in cards[1:]:
            hm = re.match(r'<h3 id="([^"]*)">(.*?)</h3>', card, re.S)
            cid, ctitle = hm.group(1), strip_tags(hm.group(2))
            short = re.sub(r'^(主题 \d+|题 \d+)[:：]?\s*', '', ctitle)
            short = re.sub(r'【(巩固|拓展)】', '', short)
            picks.append(f'<a class="pick {cls}" href="#{cid}"><span class="k">{html.escape(label)}</span>'
                         f'<span class="t">{html.escape(short)}</span></a>')
            pieces.append(f'<article class="card" id="{cid}">{decorate(card).replace(hm.group(0), f"<h3>{decorate(hm.group(2))}</h3>", 1)}</article>')
        no = str(n) if n else '·'
        out.append(f'<section class="col {cls}" id="{sid}"><div class="col-head"><span class="no">{no}</span>'
                   f'<h2><span class="sub">{sub}</span>{html.escape(label)}</h2></div>{"".join(pieces)}</section>')
    return title, lede, nav, picks, out


def main(src: str, dst: str) -> None:
    text = Path(src).read_text(encoding='utf-8')
    title, lede, nav, picks, sections = render(text)
    dm = re.search(r'\d{4}-\d{2}-\d{2}', title)
    date = dm.group(0) if dm else ''
    name = re.sub(r'\s*·\s*\d{4}-\d{2}-\d{2}\s*$', '', title)
    chars = len(re.sub(r'\s', '', text))
    minutes = max(1, round(chars / 450))
    page = f"""<title>Agent 日报 {date}</title>
<meta name="claude:viewport-fit-sides" content="cover">
{FONTS}
<style>{CSS}</style>
<header class="hero gutter"><div class="inner">
<div class="date">{date}</div>
<h1>{html.escape(name)}</h1>
<div class="meta"><span class="pill">阅读约 <b>{minutes} 分钟</b></span><span class="pill">约 <b>{chars:,}</b> 字</span>
<span class="pill"><b>{len(picks)}</b> 个主题</span></div>
<div class="lede">{lede}</div>
<div class="picks">{''.join(picks)}</div>
</div></header>
<nav class="bar gutter" aria-label="栏目"><div class="inner">{''.join(nav)}</div></nav>
<main class="gutter"><div class="inner">
{''.join(sections)}
</div></main>
<footer class="gutter"><div class="inner">Agent 前沿与工程日报 · {date}</div></footer>
"""
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    Path(dst).write_text(page, encoding='utf-8')


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
