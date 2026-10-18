"""多源检索：通用网页 + 学术论文 + 代码仓库 + 百科 + 模型库。

所有源都免 API Key，失败自动跳过，不影响整体流程。
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import quote, unquote, urlparse

import httpx
from bs4 import BeautifulSoup

from . import config

Result = dict[str, Any]


def _headers() -> dict[str, str]:
    return {
        "User-Agent": config.get("user_agent"),
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


# --------------------------------------------------------------------------
# 各检索源
# --------------------------------------------------------------------------
def search_web(query: str, limit: int = 10) -> list[Result]:
    """DuckDuckGo HTML 版，免 Key。"""
    out: list[Result] = []
    try:
        with _client() as c:
            r = c.post("https://html.duckduckgo.com/html/", data={"q": query, "kl": "wt-wt"})
            if r.status_code != 200:
                return out
            soup = BeautifulSoup(r.text, "lxml")
            for node in soup.select("div.result")[: limit * 2]:
                a = node.select_one("a.result__a")
                if not a:
                    continue
                href = a.get("href", "")
                m = re.search(r"uddg=([^&]+)", href)
                url = unquote(m.group(1)) if m else href
                if not url.startswith("http"):
                    continue
                snippet_node = node.select_one(".result__snippet")
                out.append(
                    {
                        "title": a.get_text(" ", strip=True),
                        "url": url,
                        "snippet": snippet_node.get_text(" ", strip=True) if snippet_node else "",
                        "source": "web",
                        "kind": "web",
                    }
                )
                if len(out) >= limit:
                    break
    except Exception:
        pass
    return out


def search_arxiv(query: str, limit: int = 6) -> list[Result]:
    """arXiv 论文检索。"""
    out: list[Result] = []
    try:
        q = quote(f"all:{query}")
        url = (
            "https://export.arxiv.org/api/query?"
            f"search_query={q}&start=0&max_results={limit}&sortBy=relevance"
        )
        with _client() as c:
            r = c.get(url)
            if r.status_code != 200:
                return out
            ns = {"a": "http://www.w3.org/2005/Atom"}
            root = ET.fromstring(r.text)
            for entry in root.findall("a:entry", ns):
                title = (entry.findtext("a:title", default="", namespaces=ns) or "").strip()
                summary = (entry.findtext("a:summary", default="", namespaces=ns) or "").strip()
                link = entry.findtext("a:id", default="", namespaces=ns) or ""
                out.append(
                    {
                        "title": re.sub(r"\s+", " ", title),
                        "url": link,
                        "snippet": re.sub(r"\s+", " ", summary)[:600],
                        "source": "arxiv",
                        "kind": "paper",
                    }
                )
    except Exception:
        pass
    return out


def search_github(query: str, limit: int = 5) -> list[Result]:
    """GitHub 仓库检索（按星数排序）。"""
    out: list[Result] = []
    try:
        with _client() as c:
            r = c.get(
                "https://api.github.com/search/repositories",
                params={"q": query, "sort": "stars", "order": "desc", "per_page": limit},
                headers={"Accept": "application/vnd.github+json"},
            )
            if r.status_code != 200:
                return out
            for item in r.json().get("items", [])[:limit]:
                out.append(
                    {
                        "title": item.get("full_name", ""),
                        "url": item.get("html_url", ""),
                        "snippet": (item.get("description") or "")[:400],
                        "source": "github",
                        "kind": "repo",
                        "stars": item.get("stargazers_count"),
                    }
                )
    except Exception:
        pass
    return out


def search_wiki(query: str, limit: int = 3) -> list[Result]:
    """中英文维基百科。"""
    out: list[Result] = []
    for lang in ("zh", "en"):
        try:
            with _client() as c:
                r = c.get(
                    f"https://{lang}.wikipedia.org/w/api.php",
                    params={
                        "action": "query",
                        "list": "search",
                        "srsearch": query,
                        "format": "json",
                        "srlimit": limit,
                    },
                )
                if r.status_code != 200:
                    continue
                for item in r.json().get("query", {}).get("search", [])[:limit]:
                    title = item.get("title", "")
                    snippet = re.sub(r"<[^>]+>", "", item.get("snippet", ""))
                    out.append(
                        {
                            "title": title,
                            "url": f"https://{lang}.wikipedia.org/wiki/{quote(title.replace(' ', '_'))}",
                            "snippet": snippet,
                            "source": "wikipedia",
                            "kind": "wiki",
                        }
                    )
        except Exception:
            continue
    return out


def search_hf(query: str, limit: int = 6, kind: str = "model") -> list[Result]:
    """HuggingFace 模型 / 数据集检索。"""
    out: list[Result] = []
    try:
        endpoint = "models" if kind == "model" else "datasets"
        with _client() as c:
            r = c.get(
                f"https://huggingface.co/api/{endpoint}",
                params={"search": query, "limit": limit, "sort": "downloads", "direction": -1},
            )
            if r.status_code != 200:
                return out
            for item in r.json()[:limit]:
                rid = item.get("id") or item.get("modelId") or ""
                out.append(
                    {
                        "title": rid,
                        "url": f"https://huggingface.co/{endpoint}/{rid}",
                        "snippet": f"下载量 {item.get('downloads', 0)} · 点赞 {item.get('likes', 0)}",
                        "source": "huggingface",
                        "kind": kind,
                        "downloads": item.get("downloads", 0),
                    }
                )
    except Exception:
        pass
    return out


# --------------------------------------------------------------------------
# 聚合
# --------------------------------------------------------------------------
SOURCES = {
    "web": search_web,
    "arxiv": search_arxiv,
    "github": search_github,
    "wiki": search_wiki,
}


def search(
    query: str,
    *,
    limit: int = 10,
    sources: list[str] | None = None,
    with_hf: bool = True,
) -> list[Result]:
    """并发检索多个源并去重。"""
    names = sources or ["web", "arxiv", "github", "wiki"]
    tasks = [(n, SOURCES[n]) for n in names if n in SOURCES]
    results: list[Result] = []
    with ThreadPoolExecutor(max_workers=len(tasks) + 2) as ex:
        futs = [ex.submit(fn, query, limit) for _, fn in tasks]
        if with_hf:
            futs.append(ex.submit(search_hf, query, 4, "model"))
            futs.append(ex.submit(search_hf, query, 3, "dataset"))
        for f in futs:
            try:
                results.extend(f.result(timeout=45))
            except Exception:
                continue
    return dedupe(results)


def dedupe(items: list[Result]) -> list[Result]:
    seen: set[str] = set()
    out: list[Result] = []
    for it in items:
        url = (it.get("url") or "").strip().rstrip("/")
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(it)
    return out


def domain_of(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().replace("www.", "")
    except Exception:
        return ""


# --------------------------------------------------------------------------
# 从任意 URL 识别可下载资源
# --------------------------------------------------------------------------
HF_MODEL_RE = re.compile(r"huggingface\.co/(?!datasets/|spaces/)([\w.\-]+)/([\w.\-]+)")
HF_DATASET_RE = re.compile(r"huggingface\.co/datasets/([\w.\-]+)/([\w.\-]+)")
OLLAMA_RE = re.compile(r"ollama\.com/library/([\w.\-]+)|ollama\s+run\s+([\w.\-]+:[\w.\-]+)")


def detect_resource(url: str, text: str = "") -> dict[str, str] | None:
    """识别 URL 中指向的模型 / 数据集资源。"""
    blob = f"{url} {text}"
    m = HF_DATASET_RE.search(blob)
    if m:
        return {"type": "hf_dataset", "repo": f"{m.group(1)}/{m.group(2)}"}
    m = HF_MODEL_RE.search(blob)
    if m:
        return {"type": "hf_model", "repo": f"{m.group(1)}/{m.group(2)}"}
    m = OLLAMA_RE.search(blob)
    if m:
        return {"type": "ollama", "repo": m.group(1) or m.group(2)}
    return None
