"""
Dispatcher de pesquisa por fonte/intent.
Roteia para o source correto baseado no campo 'source' do config.
"""


def search_ddg(query: str, num_results: int = 10) -> list[dict]:
    """Busca via DuckDuckGo usando a biblioteca oficial — muito mais confiável que scraping HTML."""
    try:
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS
        with DDGS() as ddgs:
            raw = ddgs.text(query, max_results=num_results, region="br-pt")
            return [
                {"title": r.get("title", ""), "href": r.get("href", ""), "body": r.get("body", "")}
                for r in (raw or [])
            ]
    except Exception:
        return []


def run_research(prompt: str, config: dict, progress_cb=None) -> list[dict]:
    source = config.get("source", "google")

    if source == "instagram":
        from agents.sources.instagram import search
    elif source == "tiktok":
        from agents.sources.tiktok import search
    elif source == "tiktok_shop":
        from agents.sources.tiktok_shop import search
    else:
        from agents.sources.google import search

    return search(prompt, config, progress_cb=progress_cb)
