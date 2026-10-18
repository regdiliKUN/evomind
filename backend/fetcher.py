"""网页抓取与正文提取：把任意 URL 变成干净的文本。"""
from __future__ import annotations

import re
from typing import Any

import httpx
from bs4 import BeautifulSoup

from . import config

Doc = dict[str, Any]

MAX_CHARS = 60000


def _headers() -> dict[str, str]:
    return {
        "User-Agent": config.get("user_agent"),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }


def _client(timeout: float | None = None) -> httpx.Client:
    return httpx.Client(
        timeout=httpx.Timeout(timeout or config.get("request_timeout", 20), connect=10.0),
        headers=_headers(),
        follow_redirects=True,
        proxy=config.get("proxy") or None,
        trust_env=False,
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
    for accept in ("application/vnd.github.raw", "application/vnd.github+json"):
        try:
            with _client(20) as c:
                r = c.get(
                    f"https://api.github.com/repos/{owner}/{repo}/readme",
                    headers={"Accept": accept},
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
    try:
        soup = BeautifulSoup(html, "lxml")
        return _clean(soup.get_text("\n", strip=True))
    except Exception:
        return _clean(re.sub(r"<[^>]+>", " ", html))


def _stackexchange_fetch(url: str) -> Doc | None:
    """StackExchange 有 Cloudflare 保护，直接抓 HTML 会被拦。

    改走开放 API：拿问题正文 + 前几条高票答案，内容质量比抓页面还好。
    """
    m = re.search(r"https?://([a-z0-9]+)\.stackexchange\.com/questions/(\d+)", url)
    if not m:
        return None
    site, qid = m.group(1), m.group(2)
    try:
        with _client(25) as c:
            r = c.get(
                f"https://api.stackexchange.com/2.3/questions/{qid}",
                params={"site": site, "filter": "withbody"},
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

    # arXiv 摘要页
    if "arxiv.org/abs/" in url:
        try:
            with _client() as c:
                r = c.get(url)
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
        with _client() as c:
            r = c.get(url)
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
