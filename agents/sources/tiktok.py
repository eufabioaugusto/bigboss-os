"""
Fonte: TikTok
Descobre perfis de negócios via DDG + extrai dados com Playwright.
"""
import re
from agents.researcher import search_ddg
from agents.ai import ask_json


def _is_profile_url(url: str) -> bool:
    url = url.lower()
    if "tiktok.com" not in url:
        return False
    bad = ["/video/", "/tag/", "/music/", "/discover/", "/trending/", "?", "#"]
    return "@" in url and not any(b in url for b in bad)


def _extract_username(url: str) -> str | None:
    m = re.search(r"tiktok\.com/@([a-zA-Z0-9_.]{2,40})", url)
    return m.group(1) if m else None


def _scrape_profile(username: str) -> dict:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {}

    url = f"https://www.tiktok.com/@{username}"
    data = {"tt_username": username, "tt_url": url}

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                locale="pt-BR",
            )
            page = ctx.new_page()
            page.set_default_timeout(18000)
            page.goto(url, wait_until="networkidle")

            og_title = page.get_attribute('meta[property="og:title"]', "content") or ""
            og_desc = page.get_attribute('meta[property="og:description"]', "content") or ""

            data["tt_display_name"] = og_title.split("|")[0].strip()
            data["tt_bio"] = og_desc[:400]

            html = page.content()

            # Seguidores, seguindo, likes
            followers_m = re.search(r'"followerCount"\s*:\s*(\d+)', html)
            following_m = re.search(r'"followingCount"\s*:\s*(\d+)', html)
            likes_m = re.search(r'"heartCount"\s*:\s*(\d+)', html)
            videos_m = re.search(r'"videoCount"\s*:\s*(\d+)', html)

            if followers_m: data["tt_followers"] = int(followers_m.group(1))
            if following_m: data["tt_following"] = int(following_m.group(1))
            if likes_m: data["tt_likes"] = int(likes_m.group(1))
            if videos_m: data["tt_video_count"] = int(videos_m.group(1))

            # Website na bio
            website_m = re.search(r'"bioLink"\s*:\s*\{[^}]*"link"\s*:\s*"([^"]+)"', html)
            if website_m:
                data["tt_website"] = website_m.group(1)

            # TikTok Shop ativo?
            data["tt_has_shop"] = bool(
                re.search(r'"hasShopTab"\s*:\s*true', html) or
                page.query_selector('[data-e2e="tiktok-shop"]') or
                "/shop/" in html
            )

            browser.close()
    except Exception as e:
        data["tt_scrape_error"] = str(e)

    return data


def search(prompt: str, config: dict, progress_cb=None) -> list[dict]:
    num = config["search"]["results_per_query"]
    max_leads = config["limits"]["max_leads_per_run"]

    clean = re.sub(r"[^\w\s]", " ", prompt.lower())
    words = [w for w in clean.split() if len(w) > 2][:6]
    base = " ".join(words)
    queries = [
        f'site:tiktok.com "@" {base}',
        f'site:tiktok.com {base} loja',
        f'site:tiktok.com {base} negocio',
    ]

    seen_users, profile_urls = set(), []
    for q in queries:
        if progress_cb:
            progress_cb(f"[TikTok] Buscando: {q}")
        for r in search_ddg(q, num):
            url = r.get("href", "")
            if _is_profile_url(url):
                username = _extract_username(url)
                if username and username not in seen_users:
                    seen_users.add(username)
                    profile_urls.append((username, r))
        if len(profile_urls) >= max_leads * 2:
            break

    if not profile_urls:
        return []

    raw_profiles = []
    for i, (username, _) in enumerate(profile_urls[:max_leads * 2]):
        if progress_cb:
            progress_cb(f"[TikTok] Analisando @{username} ({i+1}/{len(profile_urls)})...")
        raw_profiles.append(_scrape_profile(username))

    profiles_text = "\n\n".join(
        f"[{i+1}] @{p.get('tt_username')} — {p.get('tt_display_name','')}\n"
        f"Bio: {p.get('tt_bio','')[:200]}\n"
        f"Seguidores: {p.get('tt_followers','?')} | Vídeos: {p.get('tt_video_count','?')} | Likes: {p.get('tt_likes','?')}\n"
        f"Website: {p.get('tt_website','')}\n"
        f"TikTok Shop: {'sim' if p.get('tt_has_shop') else 'não'}"
        for i, p in enumerate(raw_profiles)
    )

    system = """Você é especialista em prospecção via TikTok. Analise perfis e extraia leads qualificados.
Retorne JSON array com: nome_empresa, responsavel, segmento, cidade, email, telefone, website, tiktok, observacoes.
Descarte perfis pessoais puros, grandes marcas nacionais e criadores de entretenimento sem produto."""

    result = ask_json(
        f"Prompt: {prompt}\n\nPerfis TikTok:\n{profiles_text}\n\nRetorne JSON array de leads.",
        system,
    )

    leads = result if isinstance(result, list) else next((v for v in result.values() if isinstance(v, list)), [])
    tt_map = {p["tt_username"]: p for p in raw_profiles}
    for lead in leads:
        tt_user = _extract_username(lead.get("tiktok") or "")
        if tt_user and tt_user in tt_map:
            lead.update({k: v for k, v in tt_map[tt_user].items() if k not in lead or not lead[k]})
        lead["source"] = "tiktok"

    return leads[:max_leads]
