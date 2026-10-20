"""网页抓取与正文提取：把任意 URL 变成干净的文本。"""
from __future__ import annotations

import re
from typing import Any

from bs4 import BeautifulSoup
from lxml import html as lxml_html

from . import config, httpc

Doc = dict[str, Any]

MAX_CHARS = 60000

#: HTML→文本解析路径开关（仅用于基准 A/B）。True = 旧 BS4 包装路径，
#: False = 纯 lxml 路径（默认）。两者共用同一个 libxml2 解析器，输出字节一致。
_USE_BS4 = False

#: 纯文本提取时整棵跳过的标签（与 BS4 get_text 的默认行为对齐：
#: 不返回 script/style/template 内的文本，也不返回注释）。
_SKIP_TEXT_TAGS = frozenset({"script", "style", "template", "noscript"})


def _headers() -> dict[str, str]:
    return {
        "User-Agent": config.get("user_agent"),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }


def _client(timeout: float | None = None) -> "httpc.Borrowed":
    """共享客户端（连接池复用，见 httpc）。退出 `with` 不会关闭它；超时按请求覆盖。"""
    return httpc.borrow_client(
        "fetch",
        timeout=timeout or config.get("request_timeout", 20),
        headers=_headers(),
        follow_redirects=True,
    )


def _clean(text: str) -> str:
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _github_readme(url: str) -> str:
    """GitHub 仓库页：直接走 API 拿 README。"""
    m = re.search(r"github\.com/([^/]+)/([^/#?]+)", url)
    if not m:
        return ""
    owner, repo = m.group(1), m.group(2)
    c = _client(20)
    for accept in ("application/vnd.github.raw", "application/vnd.github+json"):
        try:
            r = c.get(
                f"https://api.github.com/repos/{owner}/{repo}/readme",
                headers={"Accept": accept},
                timeout=20,
            )
            if r.status_code != 200:
                continue
            if accept.endswith("raw"):
                return _clean(r.text)[:MAX_CHARS]
        except Exception:
            continue
    return ""


def _html_to_text(html: str) -> str:
    if not html:
        return ""
    if _USE_BS4:
        try:
            soup = BeautifulSoup(html, "lxml")
            return _clean(soup.get_text("\n", strip=True))
        except Exception:
            return _clean(re.sub(r"<[^>]+>", " ", html))
    # 纯 lxml 路径：BS4 只是 libxml2 外面的一层 Python 包装，包装本身要慢
    # 一倍以上。这里直接走 lxml.html，按文档序遍历文本节点，
    # 跳过 script/style/template/noscript 子树与注释——与 BS4 get_text
    # 的默认行为逐字节对齐（两者共用同一个 libxml2 解析器，树结构相同）。
    try:
        doc = lxml_html.fromstring(html)
    except Exception:
        return _clean(re.sub(r"<[^>]+>", " ", html))
    # 文档序遍历：每个元素先输出 el.text，再递归子节点，最后输出 el.tail
    # （tail 属于父级流，被跳过的 script/style/注释的 tail 同样要输出）。
    parts: list[str] = []
    stack: list[tuple[str, Any]] = [("node", doc)]
    while stack:
        kind, payload = stack.pop()
        if kind == "text":
            parts.append(payload)
            continue
        el = payload
        if el.tail:
            t = el.tail.strip()
            if t:
                stack.append(("text", t))
        # el.tag 非 str 即注释/PI：BS4 get_text 不含其文本（无 .text 概念）
        if not isinstance(el.tag, str) or el.tag in _SKIP_TEXT_TAGS:
            continue
        for child in reversed(el):
            stack.append(("node", child))
        if el.text:
            t = el.text.strip()
            if t:
                stack.append(("text", t))
    return _clean("\n".join(parts))


def _stackexchange_fetch(url: str) -> Doc | None:
    """StackExchange 有 Cloudflare 保护，直接抓 HTML 会被拦。

    改走开放 API：拿问题正文 + 前几条高票答案，内容质量比抓页面还好。
    """
    m = re.search(r"https?://([a-z0-9]+)\.stackexchange\.com/questions/(\d+)", url)
    if not m:
        return None
    site, qid = m.group(1), m.group(2)
    c = _client(25)
    try:
        r = c.get(
            f"https://api.stackexchange.com/2.3/questions/{qid}",
            params={"site": site, "filter": "withbody"},
            timeout=25,
        )
        if r.status_code != 200:
            return None
        items = (r.json() or {}).get("items") or []
        if not items:
            return None
        q = items[0]
        title = q.get("title", "")
        parts = [f"# {title}", _html_to_text(q.get("body", ""))]

        ra = c.get(
            f"https://api.stackexchange.com/2.3/questions/{qid}/answers",
            params={
                "site": site,
                "filter": "withbody",
                "sort": "votes",
                "order": "desc",
                "pagesize": 3,
            },
            timeout=25,
        )
        if ra.status_code == 200:
            for a in (ra.json() or {}).get("items") or []:
                body = _html_to_text(a.get("body", ""))
                if len(body) > 60:
                    parts.append(f"## 回答（{a.get('score', 0)} 赞）\n{body}")

        text = _clean("\n\n".join(p for p in parts if p))
        if len(text) < 120:
            return None
        return {
            "ok": True,
            "url": url,
            "title": title or url,
            "text": text[:MAX_CHARS],
            "error": "",
        }
    except Exception:
        return None


def _discourse_fetch(url: str) -> Doc | None:
    """Discourse 论坛（ZBrushCentral 等）：走 .json 端点拿结构化楼层。

    比抓 HTML 干净得多 —— 直接拿到每层楼的 cooked HTML，不用猜正文在哪。
    robots.txt 未禁止 /t/ 路径，属于允许范围。
    """
    base = url.split("?")[0].rstrip("/")
    if base.endswith(".json"):
        base = base[:-5]
    c = _client(25)
    try:
        r = c.get(base + ".json", timeout=25)
        if r.status_code != 200:
            return None
        j = r.json()
        title = j.get("title", "")
        posts = (j.get("post_stream") or {}).get("posts") or []
        if not posts:
            return None

        parts = [f"# {title}"]
        for p in posts[:6]:
            body = _html_to_text(p.get("cooked", ""))
            if len(body) > 80:
                n = p.get("post_number", "?")
                parts.append(f"## 楼层 {n}\n{body}")

        text = _clean("\n\n".join(parts))
        if len(text) < 120:
            return None
        return {
            "ok": True,
            "url": url,
            "title": title or url,
            "text": text[:MAX_CHARS],
            "error": "",
        }
    except Exception:
        return None


def fetch(url: str) -> Doc:
    """抓取单页并提取正文。返回 {ok, url, title, text, error}。"""
    doc: Doc = {"ok": False, "url": url, "title": "", "text": "", "error": ""}

    # StackExchange 问答：走 API
    if "stackexchange.com/questions/" in url:
        got = _stackexchange_fetch(url)
        if got:
            return got
        doc["error"] = "StackExchange API 未返回内容"
        return doc

    # Discourse 论坛（ZBrushCentral 等）：走 .json
    if "zbrushcentral.com/t/" in url:
        got = _discourse_fetch(url)
        if got:
            return got
        doc["error"] = "Discourse JSON 未返回内容"
        return doc

    # arXiv 摘要页
    if "arxiv.org/abs/" in url:
        try:
            c = _client()
            r = c.get(url, timeout=config.get("request_timeout", 20))
            if r.status_code == 200:
                soup = BeautifulSoup(r.text, "lxml")
                title = soup.select_one("h1.title")
                abst = soup.select_one("blockquote.abstract")
                if abst:
                    doc.update(
                        ok=True,
                        title=_clean(title.get_text(" ", strip=True)) if title else url,
                        text=_clean(abst.get_text(" ", strip=True)),
                    )
                    return doc
        except Exception:
            pass

    # GitHub 仓库
    if "github.com/" in url and "/blob/" not in url:
        readme = _github_readme(url)
        if readme:
            doc.update(ok=True, title=url, text=readme[:MAX_CHARS])
            return doc

    # 通用抓取
    try:
        c = _client()
        r = c.get(url, timeout=config.get("request_timeout", 20))
        if r.status_code >= 400:
            doc["error"] = f"HTTP {r.status_code}"
            return doc
        ctype = r.headers.get("content-type", "")
        if "html" not in ctype and "xml" not in ctype and "text" not in ctype:
            doc["error"] = f"非文本内容: {ctype}"
            return doc
        html = r.text
    except Exception as e:  # noqa: BLE001
        doc["error"] = str(e)[:200]
        return doc

    title = ""
    body = ""
    try:
        import trafilatura

        body = trafilatura.extract(
            html, include_comments=False, include_tables=True, favor_recall=True
        ) or ""
    except Exception:
        body = ""

    if len(body) < 200:
        try:
            soup = BeautifulSoup(html, "lxml")
            for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form"]):
                tag.decompose()
            if not title:
                title = (soup.title.get_text(strip=True) if soup.title else "") or ""
            main = soup.find("article") or soup.find("main") or soup.body or soup
            body = main.get_text("\n", strip=True)
        except Exception:
            pass
    else:
        try:
            soup = BeautifulSoup(html, "lxml")
            title = (soup.title.get_text(strip=True) if soup.title else "") or ""
        except Exception:
            title = ""

    body = _clean(body)
    if len(body) < 120:
        doc["error"] = "正文过短（可能是动态渲染页面）"
        doc["text"] = body
        return doc

    doc.update(ok=True, title=title or url, text=body[:MAX_CHARS])
    return doc


def fetch_many(urls: list[str], workers: int = 6) -> list[Doc]:
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(fetch, urls))


def chunk(text: str, size: int = 1800, overlap: int = 200) -> list[str]:
    """按段落边界切块。"""
    text = _clean(text)
    if len(text) <= size:
        return [text] if text else []
    paras = [p for p in text.split("\n") if p.strip()]
    chunks: list[str] = []
    buf = ""
    for p in paras:
        if len(buf) + len(p) + 1 <= size:
            buf = f"{buf}\n{p}" if buf else p
        else:
            if buf:
                chunks.append(buf)
            if len(p) > size:
                for i in range(0, len(p), size - overlap):
                    chunks.append(p[i : i + size])
                buf = ""
            else:
                buf = p
    if buf:
        chunks.append(buf)
    return [c for c in chunks if len(c.strip()) > 80]
