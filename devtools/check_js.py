#!/usr/bin/env python3
"""检查 web/index.html 里的内联 JS。

做两件事：

  1. **语法检查**（node --check）。页面脚本是一整块，任何一处语法错误都会让
     **整个页面的 JS 全挂**（不是局部报错）—— 表现成"点了没反应"，很难查。
  2. **扫"相邻字符串缺连接符"**。JS 不像 Python 会把相邻字符串自动拼接，
     少一个 `+` 就是语法错误。这个模式 IDE 不一定报，但特别容易在
     多行提示文案里手滑写出来。

用法（在**项目根目录**执行）：

    python devtools/check_js.py

退出码：0 = 通过，1 = 语法错误。
"""
import glob
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent      # 项目根目录


def find_node() -> str:
    """找 node 可执行文件。

    先信 PATH —— 装了 Node.js 的环境（含官方镜像）都能直接用。
    再退回 WorkBuddy 的托管目录，并自动挑版本号最大的那个：
    版本目录会变（22.22.2-3 → -5 …），**写死过一次，换版本就 FileNotFoundError**。
    """
    p = shutil.which("node")
    # PATH 里可能命中 "22.22.2.old.25944" 这种**旧版本备份目录**。
    # 它能跑，但不该被主动选中 —— 跳过它，继续往下找正式的。
    if p and ".old." not in p:
        return p

    home = pathlib.Path.home()
    pats = [
        str(home / ".workbuddy/binaries/node/versions/*/node.exe"),
        str(home / ".workbuddy/binaries/node/*/node.exe"),
        str(home / ".workbuddy/binaries/node/versions/*/bin/node"),
        "/usr/local/bin/node",
        "/usr/bin/node",
    ]
    hits: list[str] = []
    for pat in pats:
        hits += glob.glob(pat)
    # 排除 "xxx.old.<数字>" 这种旧版本备份目录（按字典序会排在最后，容易误选）
    fresh = [x for x in hits if ".old." not in x]
    hits = fresh or hits
    if not hits:
        # 实在只剩 PATH 里那个 .old 版本可用，也认 —— 总比报错强
        if p:
            return p
        raise SystemExit("找不到 node —— 请先安装 Node.js，或把它加进 PATH")

    def rank(x: str):
        m = re.search(r"/([0-9][^/]*)", x)
        return m.group(1) if m else x
    return sorted(hits, key=rank)[-1]


NODE = find_node()

html_path = ROOT / "web" / "index.html"
if not html_path.exists():
    raise SystemExit(f"找不到 {html_path} —— 请在项目根目录执行本脚本")

html = html_path.read_text(encoding="utf-8")
# 取第一个**内联** script（跳过带 src= 的外链）
script = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)[0]

# 写临时文件而不是项目目录里 —— 免得留下 _chk.js 这种垃圾文件
fd = tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8")
fd.write(script)
fd.close()
tmp = pathlib.Path(fd.name)

try:
    rc = subprocess.run([NODE, "--check", str(tmp)]).returncode
    print("语法检查:", "OK" if rc == 0 else "失败",
          "(%s)" % pathlib.Path(NODE).parts[-2])
    if rc != 0:
        sys.exit(1)

    # 扫：一行以字符串结尾、下一行以字符串开头，但行尾没有任何连接符
    STR_END = re.compile(r"""["'`]\s*$""")
    STR_START = re.compile(r"""^\s*["'`]""")
    CONNECT = re.compile(
        r"""[+,({[:=?&|]|\|\||&&|\breturn\b|=>|\bcase\b|\bthrow\b""")

    bad = []
    lines = script.split("\n")
    for i, line in enumerate(lines):
        t = line.strip()
        if not t or t.startswith("//"):
            continue
        if (STR_END.search(t) and not CONNECT.search(t)
                and i + 1 < len(lines) and STR_START.match(lines[i + 1])):
            bad.append((i + 1, t[:58], lines[i + 1].strip()[:40]))

    if bad:
        print("疑似缺连接符:")
        for ln, a, b in bad:
            print("   L%-5d %s   ->  %s" % (ln, a, b))
        print("\n提示：JS 的相邻字符串**不会**自动拼接，行尾补一个 + 试试。")
    else:
        print("相邻字符串: OK，没发现缺连接符")
finally:
    tmp.unlink(missing_ok=True)
