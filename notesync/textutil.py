"""文本规范化工具。

这一层是双向同步能不能稳定的根基：两侧的"同一条内容"必须规范化成
**完全一样的字符串**，否则每轮同步都会认为对方改动了，内容来回震荡。

四条规范：
  1. 剥掉便笺正文里的块标记 `\\id=<uuid>`
  2. 统一换行符、去掉行尾空白、压缩连续空行、去首尾空行
  3. **小米侧的 content 是 XML**（不是 HTML！），要按 XML 解析成纯文本
  4. 反向也要能幂等还原 —— 见下面 `text_to_mi_xml`

关于小米的 content 格式（从 ceynri/mi-note-cli 的 converter.ts 得到的权威结论）
----------------------------------------------------------------------------
它是一组**扁平的元素**，用 `\\n` 连接，每行一个：

    普通段落   <text indent="1">内容</text>
    空行       <text indent="1"></text>
    一级标题   <text indent="1"><size>内容</size></text>
    二级标题   <text indent="1"><mid-size>内容</mid-size></text>
    三级标题   <text indent="1"><h3-size>内容</h3-size></text>
    无序列表   <bullet indent="N" />内容
    有序列表   <order indent="N" inputNumber="3" />内容
    复选框     <input type="checkbox" indent="N" level="3" checked="true" />内容
    引用       <quote><text indent="1">内容</text>…</quote>
    分割线     <hr />
    图片       <img fileid="xxx" imgshow="0" imgdes="" />
    行内       <b> <i> <delete> <u>

注意列表是**自闭合标签 + 后置文本**（`<bullet />文字`），不是包裹式。
`indent` 从 1 开始，不是 0。

只要两侧都走 canon()，往返就是幂等的（round-trip idempotent）。
"""

from __future__ import annotations

import hashlib
import html
import re

# 便笺正文每段开头的块标记
RE_LINE_ID = re.compile(r"\\id=[0-9a-fA-F-]{8,}")
RE_TAG = re.compile(r"<[^>]+>")
RE_BR = re.compile(r"(?i)<br\s*/?>")
RE_P_CLOSE = re.compile(r"(?i)</p\s*>")
RE_P_OPEN = re.compile(r"(?i)<p[^>]*>")
RE_LI = re.compile(r"(?i)<li[^>]*>")

# 小米 XML 里的顶层元素。
# **必须逐个元素扫**，不能用"把 <bullet/> 替换成 \n- "那种做法 ——
# 元素之间本来就有换行，再补一个就会每行前多出空行，往返立刻不幂等。
RE_MI_TOPLEVEL = re.compile(
    r"<quote\b[^>]*>(?P<q>.*?)</quote>"
    r"|<text\b[^>]*>(?P<tx>.*?)</text>"
    r"|<text\b[^>]*\s*/>"
    r"|<bullet\b[^>]*/>"
    r'|<order\b[^>]*?inputNumber="(?P<num>\d+)"[^>]*/>'
    r"|<order\b[^>]*/>"
    r"|<input\b[^>]*/>"
    r"|<hr\s*/>"
    r"|<img\b[^>]*/>",
    re.S,
)

# 标题：小米把标题包在 <text> 内的特殊标签里
RE_MI_HEADING = re.compile(r"<(size|mid-size|h3-size)>(.*?)</\1>", re.S)
HEADING_MARK = {"size": "# ", "mid-size": "## ", "h3-size": "### "}
MARK_HEADING = {"#": "size", "##": "mid-size", "###": "h3-size"}


def strip_block_markers(text: str | None) -> str:
    """剥掉便笺内部的 `\\id=<uuid>` 块标记"""
    return RE_LINE_ID.sub("", text or "")


def escape_xml(s: str) -> str:
    """转义 XML 文本节点里的保留字符。

    只转义这三个。引号不用转 —— 文本节点里引号是合法的，
    多转反而会让内容在小米客户端里显示出多余的实体。
    """
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def mi_inline_to_text(s: str | None) -> str:
    """剥掉行内标签（<b> <i> <delete> <u> <size>…）并解实体"""
    return html.unescape(RE_TAG.sub("", s or ""))


def _mi_indent(tag: str) -> int:
    """从元素里取 indent（小米的首层是 1，不是 0）"""
    m = re.search(r'indent="(\d+)"', tag or "")
    try:
        return max(1, int(m.group(1))) if m else 1
    except (TypeError, ValueError):
        return 1


def _mi_prefix(tag: str, mark: str) -> str:
    """把 indent 折算成前导空格（2 空格一级），这样嵌套层级能还原"""
    return "  " * (_mi_indent(tag) - 1) + mark


def mi_xml_to_text(raw: str | None) -> str:
    """小米笔记 content（XML）→ 纯文本。

    逐个顶层元素扫描，**每个元素产出一行**。
    列表类元素（bullet / order / checkbox）是「自闭合标签 + 紧随其后的文本」，
    文本在标签**外面**，所以用一个 pending 前缀把它和后面的文本拼成一行。

    列表符号和标题记号都保留（`- ` / `1. ` / `- [x] ` / `# `），
    否则往返不幂等：读的时候丢掉记号、写回去降级成普通段落，每同步一次掉一级。
    """
    if not raw:
        return ""
    out: list[str] = []
    pending = ""            # 自闭合列表元素留下的行前缀，等后置文本
    pos = 0

    for m in RE_MI_TOPLEVEL.finditer(raw):
        gap = raw[pos:m.start()]
        pos = m.end()
        s = m.group(0)

        if pending:
            # 列表项：前缀 + 紧跟的文本
            out.append((pending + gap.strip("\n").strip()).rstrip())
            pending = ""
        elif gap.strip():
            out.append(mi_inline_to_text(gap).strip("\n").rstrip())

        if s.startswith("<quote"):
            # 引用：内部再解析一次，逐行加 > 前缀
            for line in mi_xml_to_text(m.group("q") or "").split("\n"):
                out.append(("> " + line).rstrip())
        elif s.startswith("<text"):
            inner = m.group("tx")
            if inner is None:
                out.append("")                       # <text .../> 空段落
            else:
                hm = RE_MI_HEADING.fullmatch(inner.strip())
                if hm:
                    out.append(HEADING_MARK[hm.group(1)]
                               + mi_inline_to_text(hm.group(2)))
                else:
                    out.append(mi_inline_to_text(inner))
        elif s.startswith("<bullet"):
            pending = _mi_prefix(s, "- ")
        elif s.startswith("<order"):
            pending = _mi_prefix(s, f"{m.group('num') or '1'}. ")
        elif s.startswith("<input"):
            pending = _mi_prefix(
                s, "- [x] " if 'checked="true"' in s else "- [ ] ")
        elif s.startswith("<hr"):
            out.append("---")
        elif s.startswith("<img"):
            out.append("[图片]")

    # 收尾：最后一项列表元素的文本在**所有元素之后**，得和 pending 拼起来。
    # 分开处理会把 `- [ ] 未完成` 拆成两行 —— 之前就踩了这个。
    tail_raw = raw[pos:]
    tail = mi_inline_to_text(tail_raw).strip("\n").rstrip() if tail_raw.strip() else ""
    if pending:
        out.append((pending + tail).rstrip())
    elif tail:
        out.append(tail)
    return "\n".join(out)


def text_to_mi_xml(text: str | None) -> str:
    """纯文本 → 小米笔记 content（XML）。

    逐行生成一个元素，并识别纯文本里的记号，好让往返幂等：
      `# ` / `## ` / `### ` → <size> / <mid-size> / <h3-size>
      `- ` / `* `            → <bullet>
      `1. `                  → <order inputNumber="1">
      `- [x] ` / `- [ ] `    → <input type="checkbox">
      `---`                  → <hr />
      `> `                   → <quote>（连续的 > 行合并成一个引用块）

    缩进按「2 空格一级」折算成 indent（首层是 1，不是 0 —— 这点和很多人直觉相反）。
    """
    if text is None:
        return ""
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    parts: list[str] = []

    def one(line: str) -> str:
        stripped = line.strip()

        if stripped in ("---", "***", "___"):
            return "<hr />"

        m = re.match(r"^(#{1,3})\s+(.*)$", stripped)
        if m:
            tag = MARK_HEADING[m.group(1)]
            return (f'<text indent="1"><{tag}>'
                    f'{escape_xml(m.group(2))}</{tag}></text>')

        m = re.match(r"^(\s*)[-*]\s+\[([xX ])\]\s+(.*)$", line)
        if m:
            level = 1 + len(m.group(1).expandtabs(2)) // 2
            checked = ' checked="true"' if m.group(2).lower() == "x" else ""
            return (f'<input type="checkbox" indent="{level}" '
                    f'level="3"{checked} />{escape_xml(m.group(3))}')

        m = re.match(r"^(\s*)[-*]\s+(.*)$", line)
        if m:
            level = 1 + len(m.group(1).expandtabs(2)) // 2
            return f'<bullet indent="{level}" />{escape_xml(m.group(2))}'

        m = re.match(r"^(\s*)(\d+)\.\s+(.*)$", line)
        if m:
            level = 1 + len(m.group(1).expandtabs(2)) // 2
            return (f'<order indent="{level}" inputNumber="{m.group(2)}" />'
                    f'{escape_xml(m.group(3))}')

        return f'<text indent="1">{escape_xml(line)}</text>'

    i = 0
    while i < len(lines):
        # 连续的 `> ` 行合并成一个 <quote>，与 mi_xml_to_text 的解析对称
        if lines[i].lstrip().startswith("> "):
            ql: list[str] = []
            while i < len(lines) and lines[i].lstrip().startswith(">"):
                ql.append(lines[i].lstrip()[1:].lstrip())
                i += 1
            inner = "\n".join(f'<text indent="1">{escape_xml(x)}</text>'
                              for x in ql)
            parts.append(f"<quote>{inner}</quote>")
            continue
        parts.append(one(lines[i]))
        i += 1
    return "\n".join(parts)


def html_to_text(raw: str | None) -> str:
    """兜底：把 HTML 转纯文本（便笺 body 就是这种形态）

    顺序很重要：先把块级标签换成换行，再剥剩下的标签，最后解 HTML 实体。
    反过来会把 `&lt;p&gt;` 这类被转义的内容误当标签剥掉。

    **最后必须剥掉首尾空白**：微软便笺的 body 固定是
    `<html><head>\\r\\n<meta …></head><body>正文`，
    `<head>` 后那个换行剥完标签后会留在文本最前面，
    于是"首行"变成空的 —— 列表里的标题就全空了（这个现象排查过一次）。
    """
    if not raw:
        return ""
    t = RE_BR.sub("\n", raw)
    t = RE_P_CLOSE.sub("\n", t)
    t = RE_P_OPEN.sub("", t)
    t = RE_LI.sub("\n- ", t)
    t = RE_TAG.sub("", t)
    t = html.unescape(t)
    # 统一换行 + 只剥首尾（内部空行要留给 canon 去压缩）
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    return t.strip("\n").strip()


def mi_content_to_text(raw: str | None) -> str:
    """小米 content 的统一入口：XML 优先，认不出来再当 HTML 兜底。

    判据是小米 XML 的特征标签。没有特征就直接走 HTML 分支 ——
    这样即使小米哪天改了格式，也不会把内容整段丢掉。
    """
    if not raw:
        return ""
    if re.search(r"<(text|bullet|order|quote|input)\b", raw):
        return mi_xml_to_text(raw)
    return html_to_text(raw)


def first_nonempty_line(xml: str | None) -> str:
    """从小米 XML 里取首个非空行（给 snippet / subject 用）"""
    for line in mi_xml_to_text(xml).split("\n"):
        if line.strip():
            return line.strip()
    return ""


# --------------------------------------------------------------------- 便笺（微软）
def note_html(text: str | None) -> str:
    """把纯文本包成**微软便笺** body 的 HTML。

    为什么要单独一个函数：真便笺的 body 有固定形态（实测自 Outlook 便笺）：

        <html><head><meta http-equiv="Content-Type"
          content="text/html; charset=utf-8"></head><body>正文…</body></html>

    空行写成 `<p>&nbsp; </p>`（一个不换行空格），这是真便笺自己的写法。
    照抄它的形态，服务端才更可能把创建出来的东西当成便笺而不是草稿邮件。
    """
    parts: list[str] = []
    for line in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line.strip():
            parts.append(f"<p>{html.escape(line)}</p>")
        else:
            parts.append("<p>&nbsp; </p>")
    return ('<html><head><meta http-equiv="Content-Type" '
            'content="text/html; charset=utf-8"></head><body>'
            + "".join(parts) + "</body></html>")


def canon(text: str | None) -> str:
    """规范化。两侧比较和哈希都只认这个结果。

    ★ 为什么最后要把**所有空白序列折叠成单个空格**：
    小米侧存笔记时会**规范化空白** —— 实测（拿两侧真实数据逐条对比出来的）：
      · 连续换行被压扁：便笺 `a\\n\\n再次` ↔ 小米 `a\\n再次`
                       便笺 `a\\n\\n\\n\\n个体` ↔ 小米 `a\\n个体`
      · 空格和换行互转：便笺 `A 126 密码` ↔ 小米 `A \\n126 密码 `
    结果是同一篇笔记在两侧的 `content_hash` 不同 → 配不上对、
    而且每轮同步还会互相认为"对方改了"，来回覆盖。

    折掉空白之后这些差异就消失了，而**换行结构本来也不该用来判断
    "这是不是同一条笔记"**。注意这只影响比较与哈希 ——
    真正写入另一端时用的始终是**原始文本**，不会把换行丢掉。
    """
    t = strip_block_markers(text)
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    out: list[str] = []
    for line in t.split("\n"):
        line = line.rstrip()
        # 连续空行压成一个
        if not line.strip() and out and not out[-1]:
            continue
        out.append(line)
    while out and not out[0]:
        out.pop(0)
    while out and not out[-1]:
        out.pop()
    joined = "\n".join(out).strip()
    # 所有空白序列（换行、连续空格、制表）→ 单个空格
    return " ".join(joined.split())


def content_hash(text: str | None) -> str:
    """规范化后的内容指纹。用它判断"变了没有"，绝不用时间戳"""
    return hashlib.sha1(canon(text).encode("utf-8")).hexdigest()[:16]


def pair_key(text: str | None) -> str:
    """**配对专用**的宽松指纹：额外忽略**中文字符之间**的空格。

    为什么要单独一层（不能直接改 content_hash）：
      1. content_hash 是"变更检测"的判据，放宽了会把真实编辑也判成"没变"；
      2. 实测两侧剩下的差异就是这一种 ——
           便笺 `全屋智能旅游规划劳资关系`
           小米 `全屋智能 旅游规划 劳资关系`
         相似度 0.985~0.997，差异**全是词间多出来的空格**（0x20）。
         中文里这种空格通常只是输入/分词习惯，不影响语义，
         但对哈希来说就是"两条不同的内容"，于是永远配不上。

    规则：**整句含中文 → 所有空格都不参与比较**；纯 ASCII 仍严格。

    ★ 为什么不是"只看空格两侧"：
      实测剩下配不上的，空格出现在各种位置，按"两侧字符"判会漏：
        `4+2江西`     ↔ `4+2 江西`        （数字与中文之间）
        `v2ray&url=`  ↔ `v2ray & url=`    （两侧都是 ASCII，但整句是中英混排）
      中文笔记里"中英之间加不加空格"纯粹是输入习惯，不承载语义 ——
      只要整句是中文语境，空格就别参与比较，规则也更好解释。
      而 `hello world` ↔ `helloworld`（纯英文）仍必须判为不同，单测锁了这条。
    """
    s = canon(text)
    if not any(ord(c) > 127 for c in s):
        return s                    # 纯 ASCII：原样，空格敏感
    return s.replace(" ", "")       # 含中文：空格一律不参与比较


def pair_hash(text: str | None) -> str:
    """配对用的指纹（比 content_hash 宽松）"""
    return hashlib.sha1(pair_key(text).encode("utf-8")).hexdigest()[:16]


def first_line(text: str | None, limit: int = 60) -> str:
    """取第一个非空行做标题（小米笔记有 subject 字段，便笺没有）。

    ⚠ 这里**故意不走 canon** —— canon 现在会把所有空白折叠掉（见上面的说明），
    用它就取不到"第一行"了，会把整篇内容的前 60 个字当成标题。
    """
    t = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    for line in t.split("\n"):
        if line.strip():
            return line.strip()[:limit]
    return ""


def same(a: str | None, b: str | None) -> bool:
    """两侧内容是否等价（只比规范化结果）"""
    return canon(a) == canon(b)
