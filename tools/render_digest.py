"""把一份日报 Markdown 渲染成可发布为 Artifact 的 HTML 页面。

用法：python3 tools/render_digest.py 2026-11-28.md site/2026-11-28.html
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
  /* 版式：左侧固定目录 + 右侧单栏正文；手机上目录收进折叠块 */
  --bg:#f6f8fa; --surface:#ffffff; --fg:#1b232c; --muted:#5b6876; --line:#dde3ea;
  --accent:#1f5f8b; --accent-soft:#e6eff6; --mark:#8a5a00; --mark-soft:#fbf3e2;
  --code-bg:#eef2f6;
  --f-body:"Noto Sans SC","PingFang SC","Microsoft YaHei",system-ui,sans-serif;
  --f-head:"Space Grotesk","Noto Sans SC","PingFang SC",system-ui,sans-serif;
  --f-mono:"JetBrains Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){
  --bg:#12171d; --surface:#181f27; --fg:#e3e8ee; --muted:#93a1b0; --line:#2a3440;
  --accent:#7db4dc; --accent-soft:#1c2c3a; --mark:#e2b65c; --mark-soft:#2c2618;
  --code-bg:#1f2832; color-scheme:dark}}
:root[data-theme="dark"]{
  --bg:#12171d; --surface:#181f27; --fg:#e3e8ee; --muted:#93a1b0; --line:#2a3440;
  --accent:#7db4dc; --accent-soft:#1c2c3a; --mark:#e2b65c; --mark-soft:#2c2618;
  --code-bg:#1f2832; color-scheme:dark}
*{box-sizing:border-box}
body{background:var(--bg);color:var(--fg);font-family:var(--f-body);font-size:16px;line-height:1.8;
  padding-left:max(16px,env(safe-area-inset-left,0px));padding-right:max(16px,env(safe-area-inset-right,0px))}
.wrap{max-width:1120px;margin:0 auto;display:grid;grid-template-columns:220px minmax(0,1fr);gap:48px;padding-block:40px 80px}
@media (max-width:900px){.wrap{grid-template-columns:minmax(0,1fr);gap:16px;padding-block:24px 64px}}
header.masthead{grid-column:1/-1;display:flex;flex-wrap:wrap;align-items:baseline;justify-content:space-between;gap:8px 24px;
  border-bottom:2px solid var(--fg);padding-bottom:16px}
.masthead h1{font-family:var(--f-head);font-size:clamp(1.6rem,3.5vw,2.3rem);line-height:1.2;margin:0;letter-spacing:-.01em;text-wrap:balance}
.masthead .meta{font-family:var(--f-mono);font-size:.8rem;color:var(--muted);display:flex;gap:16px;flex-wrap:wrap}
nav.toc{position:sticky;top:calc(env(safe-area-inset-top,0px) + 24px);align-self:start;font-size:.85rem;line-height:1.5;
  max-height:calc(100vh - 48px);overflow:auto}
nav.toc .label{font-family:var(--f-mono);font-size:.72rem;letter-spacing:.08em;color:var(--muted);margin-bottom:8px}
nav.toc ul{list-style:none;margin:0;padding:0;display:grid;gap:4px}
nav.toc li.l3{padding-left:12px}
nav.toc a{color:var(--muted);text-decoration:none;display:block;padding:3px 8px;border-left:2px solid transparent}
nav.toc a:hover,nav.toc a:focus-visible{color:var(--accent);border-left-color:var(--accent);outline:none}
nav.toc li.l2>a{color:var(--fg);font-weight:600}
@media (max-width:900px){nav.toc{position:static;max-height:none}
  nav.toc details{background:var(--surface);border:1px solid var(--line);border-radius:6px;padding:8px 12px}}
nav.toc summary{cursor:pointer;font-family:var(--f-mono);font-size:.78rem;color:var(--muted)}
@media (min-width:901px){nav.toc summary{display:none}}
@media (max-width:900px){nav.toc .label{display:none}}
article{min-width:0;max-width:72ch}
article h2{font-family:var(--f-head);font-size:1.45rem;line-height:1.3;margin:56px 0 16px;padding-top:12px;border-top:1px solid var(--line);text-wrap:balance}
article h2 .eyebrow{display:block;font-family:var(--f-mono);font-size:.72rem;font-weight:500;letter-spacing:.1em;color:var(--accent);margin-bottom:4px}
article h3{font-family:var(--f-head);font-size:1.18rem;line-height:1.4;margin:40px 0 12px;text-wrap:balance}
article p,article li{text-wrap:pretty}
article a{color:var(--accent);text-underline-offset:3px;word-break:break-all}
article strong{font-weight:650}
article ul,article ol{padding-left:1.4em}
article li{margin:4px 0}
article code{font-family:var(--f-mono);font-size:.86em;background:var(--code-bg);padding:1px 5px;border-radius:4px;word-break:break-word}
article pre{background:var(--code-bg);padding:12px 16px;border-radius:6px;overflow-x:auto}
article pre code{background:none;padding:0}
article blockquote{margin:24px 0;padding:16px 20px;background:var(--surface);border:1px solid var(--line);border-radius:8px;color:var(--fg);font-size:.95rem}
article blockquote p{margin:8px 0}
.table-scroll{overflow-x:auto;margin:20px 0;border:1px solid var(--line);border-radius:8px;background:var(--surface)}
table{border-collapse:collapse;width:100%;font-size:.9rem;line-height:1.55;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:10px 12px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-weight:650;background:var(--accent-soft)}
tr:last-child td{border-bottom:none}
.callout{margin:20px 0;padding:14px 18px;border-radius:8px;background:var(--mark-soft);border:1px solid color-mix(in srgb,var(--mark) 35%,transparent)}
.callout>.tag{display:inline-block;font-family:var(--f-mono);font-size:.72rem;letter-spacing:.06em;color:var(--mark);margin-bottom:4px}
.callout>p:last-child,.callout>ul:last-child{margin-bottom:0}
.chip{display:inline-block;font-family:var(--f-mono);font-size:.72rem;padding:1px 8px;border-radius:999px;background:var(--accent-soft);color:var(--accent);vertical-align:middle;margin-right:6px}
article>hr{display:none}
@media (prefers-reduced-motion:no-preference){html{scroll-behavior:smooth}}
"""

FONTS = ('<link rel="preconnect" href="https://fonts.googleapis.com">'
         '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
         '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Noto+Sans+SC:wght@400;500;700'
         '&family=Space+Grotesk:wght@500;700&family=JetBrains+Mono:wght@400;500&display=swap">')

COLUMN = re.compile(r'^(栏目[一二三四五六七八九十]+)｜(.+)$')
CALLOUT_LEADS = ('落到 Aletheia', '我的判断', 'Aletheia 里的设计', '局限')


def render(md_text: str) -> tuple[str, str, str]:
    md = markdown.Markdown(extensions=['tables', 'fenced_code', 'toc'],
                           extension_configs={'toc': {'toc_depth': '2-3'}})
    body = md.convert(md_text)
    title_match = re.search(r'<h1[^>]*>(.*?)</h1>', body, re.S)
    title_html = title_match.group(1) if title_match else '日报'
    body = body.replace(title_match.group(0), '', 1) if title_match else body

    def h2(m):
        attrs, text = m.group(1), m.group(2)
        cm = COLUMN.match(html.unescape(re.sub('<[^>]+>', '', text)))
        if cm:
            return f'<h2{attrs}><span class="eyebrow">{cm.group(1)}</span>{html.escape(cm.group(2))}</h2>'
        return m.group(0)
    body = re.sub(r'<h2([^>]*)>(.*?)</h2>', h2, body)
    body = re.sub(r'【(巩固|拓展)】', r'<span class="chip">\1</span>', body)
    body = re.sub(r'<table>', '<div class="table-scroll"><table>', body).replace('</table>', '</table></div>')

    def callout(m):
        inner = m.group(0)
        lead = re.sub('<[^>]+>', '', m.group(1))
        return f'<div class="callout"><span class="tag">{html.escape(lead.rstrip("：:（("))}</span>{inner}</div>'
    leads = '|'.join(re.escape(x) for x in CALLOUT_LEADS)
    body = re.sub(rf'<p><strong>((?:{leads})[^<]*)</strong>.*?</p>(\s*<ul>.*?</ul>)?', callout, body, flags=re.S)

    toc_items = []
    for tok in md.toc_tokens:
        toc_items.append(f'<li class="l2"><a href="#{tok["id"]}">{re.sub(r"^栏目[一二三四五六七八九十]+｜", "", tok["name"])}</a></li>')
        for child in tok.get('children', []):
            name = re.sub(r'^(主题 \d+|题 \d+)[:：]?', r'\1 · ', child['name'])
            toc_items.append(f'<li class="l3"><a href="#{child["id"]}">{name}</a></li>')
    toc_html = '\n'.join(toc_items)
    return title_html, toc_html, body


def main(src: str, dst: str) -> None:
    text = Path(src).read_text(encoding='utf-8')
    title_html, toc_html, body = render(text)
    plain_title = html.unescape(re.sub('<[^>]+>', '', title_html))
    date = re.search(r'\d{4}-\d{2}-\d{2}', plain_title)
    chars = len(re.sub(r'\s', '', text))
    minutes = max(1, round(chars / 450))
    page = f"""<title>Agent 日报 {date.group(0) if date else ''}</title>
<meta name="claude:viewport-fit-sides" content="cover">
{FONTS}
<style>{CSS}</style>
<div class="wrap">
<header class="masthead"><h1>{title_html}</h1>
<div class="meta"><span>约 {chars:,} 字</span><span>阅读约 {minutes} 分钟</span></div></header>
<nav class="toc" aria-label="目录"><details open><summary>目录</summary><div class="label">目录</div><ul>
{toc_html}
</ul></details></nav>
<article>
{body}
</article>
</div>
<script>try{{if(matchMedia('(max-width:900px)').matches)document.querySelector('nav.toc details').removeAttribute('open');
document.querySelectorAll('nav.toc a').forEach(a=>a.addEventListener('click',()=>{{if(matchMedia('(max-width:900px)').matches)a.closest('details').removeAttribute('open')}}))}}catch(e){{}}</script>
"""
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    Path(dst).write_text(page, encoding='utf-8')


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
