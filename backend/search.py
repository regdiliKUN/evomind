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
    """DuckDuckGo HTML 版，免 Key。高频请求会被限流，失效时返回空。"""
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


def search_bing(query: str, limit: int = 10) -> list[Result]:
    """必应（国内可直连）。比 DDG 稳，对中文查询也友好。"""
    out: list[Result] = []
    try:
        with _client() as c:
            r = c.get(
                "https://cn.bing.com/search",
                params={"q": query, "count": max(limit, 10)},
            )
            if r.status_code != 200:
                return out
            soup = BeautifulSoup(r.text, "lxml")
            for block in soup.select("li.b_algo"):
                a = block.select_one("h2 a")
                if not a:
                    continue
                url = (a.get("href") or "").strip()
                if not url.startswith("http"):
                    continue
                # 排除必应自家的跳转/聚合页
                if "bing.com" in url or "microsofttranslator" in url:
                    continue
                p = block.select_one(".b_caption p") or block.select_one("p")
                out.append(
                    {
                        "title": a.get_text(" ", strip=True),
                        "url": url,
                        "snippet": p.get_text(" ", strip=True) if p else "",
                        "source": "bing",
                        "kind": "web",
                    }
                )
                if len(out) >= limit:
                    break
    except Exception:
        pass
    return out


STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "these", "those",
    "are", "was", "were", "will", "would", "into", "such", "using", "based",
    "can", "could", "has", "have", "had", "its", "their", "not", "but",
    "all", "any", "how", "what", "when", "which", "than", "then", "there",
    "about", "more", "most", "some", "other", "only", "also", "been", "being",
    "between", "through", "under", "over", "after", "before", "during",
    "vs", "versus", "best", "good", "better", "high", "low", "level",
}


def _en_tokens(text: str) -> set[str]:
    words = re.findall(r"[A-Za-z]{4,}", text or "")
    return {w.lower() for w in words} - STOPWORDS


def _lexically_relevant(text: str, query: str) -> bool:
    """查询里有英文词时，要求结果文本也含至少一个，过滤掉完全不沾边的。

    这不是语义判断，只是粗筛 —— 真正靠不靠谱由后面的模型预筛把关。
    """
    q = _en_tokens(query)
    if not q:
        return True  # 纯中文查询，不做英文词匹配
    return bool(q & _en_tokens(text))


def search_arxiv(query: str, limit: int = 6) -> list[Result]:
    """arXiv 论文检索。加了词面粗筛，避免中文查询被乱匹配到无关论文。"""
    out: list[Result] = []
    try:
        q = quote(f"all:{query}")
        url = (
            "https://export.arxiv.org/api/query?"
            f"search_query={q}&start=0&max_results={limit * 3}&sortBy=relevance"
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
                if not title:
                    continue
                if not _lexically_relevant(f"{title} {summary}", query):
                    continue
                out.append(
                    {
                        "title": re.sub(r"\s+", " ", title),
                        "url": link,
                        "snippet": re.sub(r"\s+", " ", summary)[:600],
                        "source": "arxiv",
                        "kind": "paper",
                    }
                )
                if len(out) >= limit:
                    break
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
# StackExchange：免 Key 的技术问答，覆盖 Blender / 3D Printing 等专门站
# --------------------------------------------------------------------------
SE_SITE_HINTS = {
    "blender": ["blender", "retopo", "topology", "topolog", "sculpt", "mesh",
                "subdiv", "subdivision", "uv ", "uvmap", "modifier", "zbrush",
                "dynamesh", "zremesher", "normal map", "bake", "shader"],
    "3dprinting": ["print", "resin", "sla ", "fdm", "slicer", "cura", "support",
                   "layer height", "wall thickness", "infill", "nozzle", "curing"],
    "graphicdesign": ["texture", "material", "render", "lighting", "composit"],
    "superuser": ["windows", "driver", "gpu", "vram"],
}


def _pick_se_site(query: str) -> str:
    q = f" {query.lower()} "
    for site, kws in SE_SITE_HINTS.items():
        if any(k in q for k in kws):
            return site
    return "stackoverflow"


def _simplify_query(query: str) -> list[str]:
    """StackExchange 的 q 是全文 AND 搜索，词太多会一条都匹配不上。

    取前几个实词（检索式的核心词通常在最前面），按长度递减返回候选，
    调用方从长到短重试，直到有结果为止。
    """
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9\-\.]{1,}", query)
    kept = [w for w in words if w.lower() not in STOPWORDS]
    if not kept:
        return [query]
    # 只试两个候选：前 3 个实词（最可能的组合）、最核心的第 1 个词。
    # 配额 300 次/天，不能盲试。
    out: list[str] = []
    if len(kept) >= 2:
        out.append(" ".join(kept[:3]))
    out.append(kept[0])
    return out


def search_stackexchange(query: str, limit: int = 6, site: str | None = None) -> list[Result]:
    """StackExchange 开放 API（免 Key，300 次/天/IP）。

    对建模、3D 打印这类软件/工艺方向，问答质量远高于通用搜索。
    """
    target = site or _pick_se_site(query)
    for simple in _simplify_query(query)[:2]:
        out: list[Result] = []
        try:
            with _client() as c:
                r = c.get(
                    "https://api.stackexchange.com/2.3/search/advanced",
                    params={
                        "order": "desc",
                        "sort": "votes",
                        "q": simple,
                        "site": target,
                        "pagesize": limit,
                        "filter": "default",
                    },
                )
                if r.status_code != 200:
                    continue
                for it in r.json().get("items", [])[:limit]:
                    tags = it.get("tags") or []
                    out.append(
                        {
                            "title": it.get("title", ""),
                            "url": it.get("link", ""),
                            "snippet": (
                                f"{it.get('score', 0)} 赞 · {it.get('answer_count', 0)} 回答"
                                + (" · 已采纳" if it.get("is_answered") else "")
                                + (f" · 标签 {', '.join(tags[:4])}" if tags else "")
                            ),
                            "source": "stackexchange",
                            "kind": "qa",
                            "site": target,
                        }
                    )
        except Exception:
            continue
        if out:
            return out
    return []


# --------------------------------------------------------------------------
# Brave Search API（需 Key，但质量最好，免费额度 2000 次/月）
# --------------------------------------------------------------------------
def search_brave(query: str, limit: int = 10) -> list[Result]:
    key = (config.get("search_api_key") or "").strip()
    if not key:
        return []
    out: list[Result] = []
    try:
        with httpx.Client(timeout=20, trust_env=False) as c:
            r = c.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": query, "count": min(limit, 20)},
                headers={
                    "Accept": "application/json",
                    "Accept-Encoding": "gzip",
                    "X-Subscription-Token": key,
                },
            )
            if r.status_code != 200:
                return out
            for it in (r.json().get("web", {}) or {}).get("results", [])[:limit]:
                out.append(
                    {
                        "title": it.get("title", ""),
                        "url": it.get("url", ""),
                        "snippet": (it.get("description") or "")[:400],
                        "source": "brave",
                        "kind": "web",
                    }
                )
    except Exception:
        pass
    return out


# --------------------------------------------------------------------------
# 聚合
# --------------------------------------------------------------------------
# 只有查询里出现这些词，才认为该方向值得查 arXiv。
# 注意排除 "mesh" / "generation" / "architecture" 这类通用词 ——
# 「3D Mesh Generation」论文和 ZBrush 建模操作是两回事，会把噪音塞满抓取配额。
ACADEMIC_HINTS = {
    "paper", "research", "benchmark", "theorem", "survey",
    "transformer", "diffusion", "llm", "rag", "embedding", "retrieval",
    "reranking", "attention", "finetune", "lora", "prompt", "reasoning",
    "quantization", "distillation", "alignment", "classification",
    "segmentation", "neural", "algorithm",
}


def should_use_arxiv(query: str) -> bool:
    return bool(_en_tokens(query) & ACADEMIC_HINTS)


SOURCES = {
    "brave": search_brave,
    "se": search_stackexchange,
    "arxiv": search_arxiv,
    "github": search_github,
    "wiki": search_wiki,
    "bing": search_bing,
    "web": search_web,
}

# 主力源。通用搜索（bing/web）在国内网络下容易被反爬污染，放最后兜底。
DEFAULT_SOURCES = ["brave", "se", "arxiv", "github", "wiki"]


def search(
    query: str,
    *,
    limit: int = 10,
    sources: list[str] | None = None,
    with_hf: bool = True,
) -> list[Result]:
    """并发检索多个源并去重。

    检索源现状（实测）：
      - Brave API：需 Key，质量最好，免费 2000 次/月
      - StackExchange：免 Key，建模/3D打印方向质量很高
      - arXiv：只对学术方向启用，否则会误匹配
      - GitHub / Wikipedia：稳定可用
      - Bing / DuckDuckGo：被反爬污染，只作最后兜底
    """
    names = list(sources or DEFAULT_SOURCES)
    if "arxiv" in names and not should_use_arxiv(query):
        names.remove("arxiv")

    tasks = [(n, SOURCES[n]) for n in names if n in SOURCES]
    results: list[Result] = []
    with ThreadPoolExecutor(max_workers=len(tasks) + 2) as ex:
        futs = {ex.submit(fn, query, limit): name for name, fn in tasks}
        if with_hf:
            futs[ex.submit(search_hf, query, 4, "model")] = "hf_model"
            futs[ex.submit(search_hf, query, 3, "dataset")] = "hf_dataset"
        for f, _name in futs.items():
            try:
                results.extend(f.result(timeout=45))
            except Exception:
                continue

    # 一个结果都没有时才动用通用搜索兜底（它的结果质量不可靠）
    if not results:
        for fn in (search_bing, search_web):
            try:
                got = fn(query, limit)
                if got:
                    results.extend(got)
                    break
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
