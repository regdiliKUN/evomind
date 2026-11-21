"""多源检索：通用网页 + 学术论文 + 代码仓库 + 百科 + 模型库。

所有源都免 API Key，失败自动跳过，不影响整体流程。
"""
from __future__ import annotations

import base64
import re
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from urllib.parse import quote, unquote, urlparse, parse_qs, urlencode, urlunparse, urljoin

from bs4 import BeautifulSoup

from . import config, httpc

Result = dict[str, Any]


def _headers() -> dict[str, str]:
    return {
        "User-Agent": config.get("user_agent"),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }


def _client(timeout: float | None = None) -> "httpc.Borrowed":
    """共享客户端（连接池复用，见 httpc）。退出 `with` 不会关闭它；超时按请求覆盖。"""
    return httpc.borrow_client(
        "search",
        timeout=timeout or config.get("request_timeout", 20),
        headers=_headers(),
        follow_redirects=True,
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
                url = canonical_url(a.get("href") or "")
                if not url.startswith("http"):
                    continue
                # 排除必应自家的跳转/聚合页
                if domain_of(url) in ("bing.com", "microsofttranslator.com"):
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
    words = re.findall(r"[A-Za-z][A-Za-z0-9]{1,}", text or "")
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
# ZBrushCentral（Discourse 论坛）：ZBrush 操作知识的核心来源
# --------------------------------------------------------------------------
# 合规说明：
#   - robots.txt 只禁了 /search、/c/*.rss、/u/、/my，未禁 /c/{id}.json 与 /t/ 路径
#   - 因此这里走「分类主题列表 + 帖子页」，不碰站内搜索和 RSS
#   - 主题列表本地缓存 30 分钟，避免重复请求
_ZBC_CATEGORIES = [
    ("zbrush-usage-questions", 6, 2),
    ("zscripting-help", 14, 1),
]
_zbc_cache: dict[str, Any] = {"ts": 0.0, "topics": []}
_zbc_lock = threading.Lock()
ZBC_BASE = "https://www.zbrushcentral.com"


def _load_zbc_topics(max_age: float = 1800.0) -> list[dict]:
    with _zbc_lock:
        if (time.time() - _zbc_cache["ts"]) < max_age:
            return _zbc_cache["topics"]

        topics: list[dict] = []
        c = _client(8)
        for slug, cid, pages in _ZBC_CATEGORIES:
            for page in range(pages):
                try:
                    r = c.get(
                        f"{ZBC_BASE}/c/{slug}/{cid}.json",
                        params={"page": page},
                        timeout=8,
                    )
                    if r.status_code != 200:
                        break
                    data = r.json()
                    batch = data.get("topic_list", {}).get("topics", [])
                    if not batch:
                        break
                    topics.extend(batch)
                except Exception:
                    break
        _zbc_cache["topics"] = topics
        _zbc_cache["ts"] = time.time()
        return topics


def search_zbrushcentral(query: str, limit: int = 8) -> list[Result]:
    """在 ZBrushCentral 的本地主题索引里按标题匹配。

    不调用站内搜索（robots 禁止），改为拉分类列表后本地过滤。
    """
    out: list[Result] = []
    try:
        topics = _load_zbc_topics()
    except Exception:
        return out
    if not topics:
        return out

    kws = _en_tokens(query)
    # 中文查询词也要参与匹配（论坛标题以英文为主，但偶有中文）
    kws |= {w for w in re.findall(r"[\u4e00-\u9fa5]{2,}", query)}

    scored: list[tuple[int, dict]] = []
    for t in topics:
        title = (t.get("title") or "").lower()
        if not title:
            continue
        hit = sum(1 for k in kws if k in title)
        if hit:
            scored.append((hit, t))
    if not scored:
        return out

    # 命中词多的优先，其次看回复数（有讨论说明有答案）
    scored.sort(key=lambda x: (-x[0], -(x[1].get("posts_count") or 0)))

    for _, t in scored[:limit]:
        tid = t.get("id")
        slug = t.get("slug") or "topic"
        out.append(
            {
                "title": t.get("title", ""),
                "url": f"{ZBC_BASE}/t/{slug}/{tid}",
                "snippet": (
                    f"ZBrushCentral · {t.get('posts_count', 0)} 回复"
                    + (f" · {t.get('views', 0)} 浏览" if t.get("views") else "")
                ),
                "source": "zbrushcentral",
                "kind": "qa",
            }
        )
    return out


def query_variants(direction: str, focus: str = "") -> list[str]:
    """Short, domain-anchored fallbacks instead of long all-AND sentences."""
    text = direction + " " + focus
    anchors = re.findall(r"[A-Za-z][A-Za-z0-9-]+", text)
    if "zbrush" in text.lower():
        anchors = ["ZBrush"] + [a for a in anchors if a.lower() != "zbrush"]
    translations = {
        "细分": "subdivision", "分辨率": "resolution", "面数": "polygon count",
        "笔刷": "brush settings", "拓扑": "retopology", "投射": "project details",
        "薄壁": "wall thickness", "定位": "alignment", "头部": "head sculpting",
        "衣褶": "cloth folds", "头发": "hair sculpting", "导出": "export scale",
        "遮罩": "masking", "贴图": "texture maps", "雕刻": "sculpting",
    }
    hints = [value for key, value in translations.items() if key in text]
    core = " ".join(dict.fromkeys(anchors[:4] + hints[:2])) or direction[:100]
    queries = [f"{core} workflow guide", f"{core} troubleshooting parameters", f"{direction[:100]} 教程 参数"]
    if "zbrush" in text.lower():
        queries.insert(0, f"site:help.maxon.net {core}")
    return queries


# --------------------------------------------------------------------------
# Brave Search API（需 Key，但质量最好，免费额度 2000 次/月）
# --------------------------------------------------------------------------
def search_brave(query: str, limit: int = 10) -> list[Result]:
    key = (config.get("search_api_key") or "").strip()
    if not key:
        return []
    out: list[Result] = []
    try:
        c = _client(20)
        r = c.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": min(limit, 20)},
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "X-Subscription-Token": key,
            },
            timeout=20,
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
DOC_ROOTS = {
    "zbrush": "https://help.maxon.net/zbr/en-us/Content/html/user-guide/3d-modeling/modeling-basics/creating-meshes/dynamesh/dynamesh.html",
    "blender": "https://docs.blender.org/manual/en/latest/",
}
_docs_cache: dict[str, tuple[float, list[Result]]] = {}
_docs_lock = threading.Lock()


def search_documentation(query: str, limit: int = 8) -> list[Result]:
    """Search official navigation indexes directly, independent of web search availability."""
    product = next((name for name in DOC_ROOTS if name in query.lower()), None)
    if not product:
        return []
    root = DOC_ROOTS[product]
    with _docs_lock:
        ts, links = _docs_cache.get(root, (0, []))
        # Cache failures briefly as well, so four parallel queries don't hammer a dead host.
        if time.monotonic() - ts > (3600 if links else 60):
            links = []
            try:
                with _client(10) as client:
                    response = client.get(root, timeout=10)
                if response.status_code == 200:
                    soup = BeautifulSoup(response.text, "lxml")
                    for a in soup.select("a[href]"):
                        url = canonical_url(urljoin(root, a.get("href", "")))
                        title = a.get_text(" ", strip=True)
                        if not title or domain_of(url) != domain_of(root) or not urlparse(url).path.endswith((".html", "/")):
                            continue
                        links.append({"title": f"{product.title()} · {title}", "url": url,
                                      "snippet": f"{product} 官方文档 · " + unquote(urlparse(url).path),
                                      "source": "official_docs", "kind": "documentation"})
            except Exception:
                pass
            _docs_cache[root] = (time.monotonic(), dedupe(links))
    # Product-only matches aren't enough: require a matching operation/feature.
    terms = _en_tokens(query) - {product, "site", "help", "maxon", "net", "official", "documentation", "guide", "workflow", "tutorial"}
    if terms:
        links = [r for r in links if terms & _en_tokens(r["title"] + " " + r["url"])]
    return rank_results(links, query)[:limit]


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


# --------------------------------------------------------------------------
# 媒体检索（图片 / 音频 / 视频）
#
# 全部免 API Key，并且只返回「能直接交给 media.extract 的地址」：
#   - Commons / Openverse 直接给文件 URL
#   - Internet Archive 给 details 页，由 media 侧解析成直链
#   - YouTube 等平台页给页面 URL，由 yt-dlp 取字幕/音轨
# 任何一个源失败都只返回空列表，不影响整轮学习。
# --------------------------------------------------------------------------
_COMMONS_FILETYPE = {
    "image": "filetype:bitmap|drawing",
    "audio": "filetype:audio",
    "video": "filetype:video",
}


def search_commons(query: str, kind: str = "image", limit: int = 6) -> list[Result]:
    """维基共享资源：CC 授权，量大且带结构化元数据。"""
    out: list[Result] = []
    ft = _COMMONS_FILETYPE.get(kind)
    if not ft:
        return out
    try:
        with _client(25) as c:
            r = c.get(
                "https://commons.wikimedia.org/w/api.php",
                params={
                    "action": "query", "format": "json", "generator": "search",
                    "gsrsearch": f"{query} {ft}", "gsrnamespace": 6,
                    "gsrlimit": max(1, min(limit, 12)),
                    "prop": "imageinfo", "iiprop": "url|mime|size|extmetadata",
                    "iiurlwidth": 640,
                },
                timeout=25,
            )
            if r.status_code != 200:
                return out
            pages = ((r.json() or {}).get("query") or {}).get("pages") or {}
    except Exception:  # noqa: BLE001
        return out

    for page in pages.values():
        info = (page.get("imageinfo") or [{}])[0]
        url = info.get("url") or ""
        if not url:
            continue
        meta = info.get("extmetadata") or {}
        desc = re.sub(r"<[^>]+>", " ", str((meta.get("ImageDescription") or {}).get("value") or ""))
        out.append({
            "title": str(page.get("title") or "").removeprefix("File:")[:160],
            "url": url,
            "snippet": re.sub(r"\s+", " ", desc).strip()[:180],
            "source": "commons",
            "kind": kind,
            "thumb": info.get("thumburl") or "",
            "license": str((meta.get("LicenseShortName") or {}).get("value") or ""),
            "page": str(info.get("descriptionurl") or ""),
        })
    return out


def search_openverse(query: str, kind: str = "image", limit: int = 6) -> list[Result]:
    """Openverse（原 CC Search）：图片与音频，免 Key，匿名有速率限制。"""
    out: list[Result] = []
    bucket = "images" if kind == "image" else "audio" if kind == "audio" else ""
    if not bucket:
        return out
    try:
        with _client(25) as c:
            r = c.get(
                f"https://api.openverse.org/v1/{bucket}/",
                params={"q": query, "page_size": max(1, min(limit, 12))},
                timeout=25,
            )
            if r.status_code != 200:
                return out
            items = (r.json() or {}).get("results") or []
    except Exception:  # noqa: BLE001
        return out
    for it in items:
        url = it.get("url") or ""
        if not url:
            continue
        out.append({
            "title": str(it.get("title") or it.get("name") or "")[:160],
            "url": url,
            "snippet": str(it.get("description") or it.get("creator") or "")[:180],
            "source": "openverse",
            "kind": kind,
            "thumb": it.get("thumbnail") or "",
            "license": f"{it.get('license','')} {it.get('license_version','')}".strip(),
            "page": it.get("foreign_landing_url") or "",
        })
    return out


def search_archive(query: str, kind: str = "audio", limit: int = 6) -> list[Result]:
    """Internet Archive：讲座录音、会议录像、老教材，免 Key。"""
    out: list[Result] = []
    mediatype = "audio" if kind == "audio" else "movies" if kind == "video" else ""
    if not mediatype:
        return out
    try:
        # Archive 默认是宽松 OR 匹配，「3d printing tutorial」会返回一堆
        # Art of Illusion 教程。改成逐词 AND，精度提升明显。
        words = [w for w in re.split(r"\s+", q.strip()) if w]
        strict = " AND ".join(words) if len(words) > 1 else (words[0] if words else q)
        with _client(25) as c:
            r = c.get(
                "https://archive.org/advancedsearch.php",
                params={
                    "q": f"({strict}) AND mediatype:({mediatype})",
                    "fl[]": ["identifier", "title", "description", "runtime"],
                    "rows": max(1, min(limit, 12)),
                    "page": 1, "output": "json",
                },
                timeout=25,
            )
            if r.status_code != 200:
                return out
            docs = (((r.json() or {}).get("response") or {}).get("docs")) or []
    except Exception:  # noqa: BLE001
        return out
    for d in docs:
        ident = d.get("identifier")
        if not ident:
            continue
        desc = d.get("description")
        if isinstance(desc, list):
            desc = " ".join(str(x) for x in desc)
        out.append({
            "title": str(d.get("title") or ident)[:160],
            "url": f"https://archive.org/details/{ident}",
            "snippet": re.sub(r"<[^>]+>", " ", str(desc or ""))[:180],
            "source": "archive",
            "kind": kind,
            "thumb": f"https://archive.org/services/img/{ident}",
            "license": "archive.org",
            "page": f"https://archive.org/details/{ident}",
        })
    return out


def search_ytdlp(query: str, kind: str = "video", limit: int = 4) -> list[Result]:
    """YouTube 等平台的站内搜索（需要 yt-dlp，未安装则返回空）。"""
    try:
        import yt_dlp
    except Exception:  # noqa: BLE001
        return []
    n = max(1, min(limit, 8))
    opts = {"quiet": True, "no_warnings": True, "skip_download": True,
            "noplaylist": True, "extract_flat": True, "socket_timeout": 20}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch{n}:{query}", download=False) or {}
    except Exception:  # noqa: BLE001
        return []
    out: list[Result] = []
    for e in (info.get("entries") or []):
        if not e:
            continue
        vid = e.get("id") or ""
        url = e.get("url") or (f"https://www.youtube.com/watch?v={vid}" if vid else "")
        if not url.startswith("http"):
            continue
        out.append({
            "title": str(e.get("title") or "")[:160],
            "url": url,
            "snippet": str(e.get("description") or "")[:180],
            "source": "youtube",
            "kind": kind,
            "thumb": e.get("thumbnail") or (
                f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg" if vid else ""),
            "license": "平台内容",
            "page": url,
            "duration": e.get("duration") or 0,
        })
    return out


MEDIA_SOURCES = {
    "commons": search_commons,
    "openverse": search_openverse,
    "archive": search_archive,
    "youtube": search_ytdlp,
}


def search_media(
    query: str,
    *,
    kinds: list[str] | tuple[str, ...] = ("image", "audio", "video"),
    limit: int = 6,
    sources: list[str] | None = None,
) -> list[Result]:
    """并发检索多个媒体源，按模态去重。

    每种模态至少配两个源：Commons/Openverse 偏静态素材，
    Archive/YouTube 偏讲座与演示视频，互为补充。
    """
    q = (query or "").strip()
    if len(q) < 2:
        return []
    wanted = [k for k in kinds if k in ("image", "audio", "video")]
    if not wanted:
        return []

    plan: list[tuple[str, str]] = []
    names = set(sources or MEDIA_SOURCES.keys())
    for kind in wanted:
        for name in ("commons", "openverse", "archive", "youtube"):
            if name not in names:
                continue
            # 源能力与模态匹配：Openverse 无视频、Archive 无图片、YouTube 只有视频
            if name == "openverse" and kind == "video":
                continue
            if name == "archive" and kind == "image":
                continue
            if name == "youtube" and kind != "video":
                continue
            plan.append((name, kind))

    results: list[Result] = []
    if not plan:
        return results
    with ThreadPoolExecutor(max_workers=min(8, len(plan))) as ex:
        futs = {
            ex.submit(
                MEDIA_SOURCES[name], q, kind,
                max(2, limit // max(1, len(wanted)) + 1),
            ): f"{name}:{kind}"
            for name, kind in plan
        }
        for f in as_completed(futs):
            try:
                results.extend(f.result(timeout=40))
            except Exception:  # noqa: BLE001
                continue

    seen: set[str] = set()
    out: list[Result] = []
    for r in results:
        key = (r.get("url") or "").split("?")[0]
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out[: limit * max(1, len(wanted))]


SOURCES = {
    "docs": search_documentation,
    "brave": search_brave,
    "se": search_stackexchange,
    "zbc": search_zbrushcentral,
    "arxiv": search_arxiv,
    "github": search_github,
    "wiki": search_wiki,
    "bing": search_bing,
    "web": search_web,
}

# 通用与专用源并行；文档目录直连不依赖搜索引擎的可用性。
DEFAULT_SOURCES = ["docs", "brave", "bing", "web", "se", "zbc", "arxiv", "github", "wiki"]


def search(
    query: str,
    *,
    limit: int = 10,
    sources: list[str] | None = None,
    with_hf: bool = False,
) -> list[Result]:
    """并发检索多个源并去重。

    检索源现状（实测）：
      - Brave API：需 Key，质量最好，免费 2000 次/月
      - StackExchange：免 Key，建模/3D打印方向质量很高
      - arXiv：只对学术方向启用，否则会误匹配
      - GitHub / Wikipedia：稳定可用
      - Bing / DuckDuckGo：并行参与；无结果时可配置 Brave Key 或网络代理
    """
    names = list(sources or DEFAULT_SOURCES)
    if "arxiv" in names and not should_use_arxiv(query):
        names.remove("arxiv")

    # 空查询、纯标点查询直接返回：否则会并发去打 6 个源，
    # 实测空查询要耗 20 秒，而且拿回的全是无关内容
    q = (query or "").strip()
    if len(q) < 2 or not re.search(r"[0-9A-Za-z\u4e00-\u9fa5]", q):
        return []

    # Forum indexes and code/model repositories are specialized sources, not a
    # replacement for general web search. Avoid unrelated hits and wasted calls.
    if "zbrush" not in q.lower() and "dynamesh" not in q.lower():
        names = [n for n in names if n != "zbc"]
    if "zbrush" in q.lower():
        names = [n for n in names if n != "se"]
    if not should_use_arxiv(q) and not re.search(r"code|python|script|api|github|plugin|代码|脚本", q, re.I):
        names = [n for n in names if n != "github"]

    tasks = [(n, SOURCES[n]) for n in names if n in SOURCES]
    results: list[Result] = []
    with ThreadPoolExecutor(max_workers=len(tasks) + 2) as ex:
        futs = {ex.submit(fn, query, limit): name for name, fn in tasks}
        if with_hf:
            futs[ex.submit(search_hf, query, 4, "model")] = "hf_model"
            futs[ex.submit(search_hf, query, 3, "dataset")] = "hf_dataset"
        for f in as_completed(futs):
            try:
                results.extend(f.result(timeout=45))
            except Exception:
                continue

    # 一个结果都没有时才动用通用搜索兜底（它的结果质量不可靠）
    if not results:
        for name, fn in (("bing", search_bing), ("web", search_web)):
            if name in names:
                continue
            try:
                got = fn(query, limit)
                if got:
                    results.extend(got)
                    break
            except Exception:
                continue

    return rank_results(results, q)


def canonical_url(url: str) -> str:
    """Unwrap search-engine links and remove tracking parameters for deduplication."""
    try:
        parsed = urlparse(url.strip())
        params = parse_qs(parsed.query)
        host = (parsed.hostname or "").lower()
        if host.endswith("duckduckgo.com") and params.get("uddg"):
            return canonical_url(params["uddg"][0])
        if host in ("www.bing.com", "bing.com", "cn.bing.com") and parsed.path.startswith("/ck/"):
            target = params.get("u", [""])[0]
            if target.startswith("a1"):
                encoded = target[2:]
                target = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
            return canonical_url(target)
        if parsed.scheme not in ("http", "https") or not host:
            return ""
        query = {k: v for k, v in params.items() if not k.lower().startswith("utm_") and k.lower() not in ("fbclid", "gclid", "msclkid")}
        return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or "/", "", urlencode(query, doseq=True), ""))
    except (ValueError, UnicodeError):
        return ""


def rank_results(items: list[Result], query: str) -> list[Result]:
    """Lexical relevance + documentation priority + bounded domain diversity."""
    words = _en_tokens(query) - {"official", "documentation", "tutorial", "guide", "site"}
    chinese = set(re.findall(r"[\u4e00-\u9fff]{2}", query))
    scored = []
    for item in dedupe(items):
        text = f"{item.get('title', '')} {item.get('snippet', '')} {unquote(item['url'])}".lower()
        matches = len(words & _en_tokens(text)) + sum(w in text for w in chinese)
        if (words or chinese) and not matches:
            continue
        # Require the explicitly named product, instead of loosely related words.
        if "zbrush" in words and "zbrush" not in text and "pixologic" not in text:
            continue
        score = matches / max(1, len(words) + len(chinese))
        host = domain_of(item["url"])
        official = host in ("help.maxon.net", "docs.blender.org", "docs.python.org", "learn.microsoft.com") or host.endswith(".edu")
        score += .35 if official else .1 if item.get("kind") == "qa" else 0
        if re.search(r"crack|keygen|破解版|激活码|购买|download free", text):
            continue
        scored.append(dict(item, rank_score=round(score, 4)))
    scored.sort(key=lambda r: -r["rank_score"])
    # Interleave domains so one forum or mirror cannot fill the reading budget.
    counts, first, rest = {}, [], []
    for item in scored:
        host = domain_of(item["url"])
        counts[host] = counts.get(host, 0) + 1
        (first if counts[host] <= 3 else rest).append(item)
    return first + rest


def dedupe(items: list[Result]) -> list[Result]:
    seen: set[str] = set()
    out: list[Result] = []
    for it in items:
        url = canonical_url(it.get("url") or "").rstrip("/")
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(dict(it, url=url))
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
