"""
Fonte: Instagram
IA gera queries → DDG acha perfis → Playwright extrai dados reais de cada perfil.
"""
import re
from agents.researcher import search_ddg
from agents.ai import ask_json

_SKIP_IG_PATHS = {"p", "reel", "explore", "accounts", "stories", "tv", "reels",
                   "hashtag", "tags", "about", "legal", "privacy", "help"}

_STORE_SIGNALS = [
    "loja", "shop", "store", "frete", "envio", "entrega", "compre", "pedido",
    "produto", "catálogo", "catalogo", "atacado", "varejo", "coleção", "colecao",
    "estoque", "promoção", "promocao", "r$", "pix", "pagamento", "link na bio",
    "acesse", "site", "whatsapp", "wpp", "📦", "🛍️", "🏪",
]


def _is_profile_url(url: str) -> bool:
    url = url.lower().rstrip("/")
    if "instagram.com" not in url:
        return False
    # Remove query strings
    url = url.split("?")[0]
    parts = [p for p in url.split("/") if p]
    # instagram.com/username — deve ter exatamente 1 path segment após o domínio
    try:
        ig_idx = next(i for i, p in enumerate(parts) if "instagram.com" in p)
        after = parts[ig_idx + 1:]
        return len(after) == 1 and after[0] not in _SKIP_IG_PATHS
    except (StopIteration, IndexError):
        return False


def _extract_username(url: str) -> str | None:
    m = re.search(r"instagram\.com/([a-zA-Z0-9_.]{2,40})/?(?:\?|$)", url)
    if m and m.group(1).lower() not in _SKIP_IG_PATHS:
        return m.group(1)
    return None


def _generate_queries(prompt: str) -> list[str]:
    """IA gera queries Instagram otimizadas para encontrar perfis de negócios."""
    try:
        result = ask_json(
            f"Gere 5 queries de busca no Google para encontrar perfis do Instagram de empresas/negócios. "
            f"Pedido: '{prompt}'\n\n"
            "Regras:\n"
            "- Sempre inclua 'site:instagram.com' no início de cada query\n"
            "- Inclua palavras que aparecem em bios de negócios do nicho pedido\n"
            "- Inclua cidade/estado se mencionado\n"
            "- Use português brasileiro\n"
            "- Exclua: -reel -/p/ -explore -hashtag\n"
            "- Exemplo: 'site:instagram.com salão beleza São Paulo'\n"
            "Retorne JSON array de strings.",
        )
        if isinstance(result, list):
            return [str(q).strip() for q in result if q and isinstance(q, str)][:5]
    except Exception:
        pass
    # Fallback
    clean = re.sub(r'\b(encontre|busque|liste|preciso|quero)\b', '', prompt, flags=re.I).strip()
    return [
        f"site:instagram.com {clean}",
        f"site:instagram.com {clean} loja",
        f"site:instagram.com {clean} contato",
    ]


def _scrape_profile(username: str) -> dict:
    """Visita perfil Instagram e extrai dados disponíveis publicamente."""
    url = f"https://www.instagram.com/{username}/"
    data = {
        "ig_username": username,
        "ig_url": url,
        "ig_display_name": "",
        "ig_bio": "",
        "ig_followers": None,
        "ig_posts": None,
        "ig_category": "",
        "ig_website": "",
        "ig_email": "",
        "ig_phone": "",
        "ig_store_signals": [],
        "ig_parece_loja": False,
    }
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                locale="pt-BR",
                viewport={"width": 1280, "height": 800},
            )
            page = ctx.new_page()
            page.set_default_timeout(18000)

            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_timeout(2000)  # deixa JS carregar

            # Meta tags (mais confiáveis)
            og_title = page.get_attribute('meta[property="og:title"]', "content") or ""
            og_desc = page.get_attribute('meta[property="og:description"]', "content") or ""
            meta_desc = page.get_attribute('meta[name="description"]', "content") or og_desc

            # Nome de exibição
            data["ig_display_name"] = og_title.split("•")[0].strip().split("(")[0].strip()

            # Seguidores do og:description: "1.234 Followers, 567 Following, 89 Posts"
            followers_m = re.search(r"([\d,.]+)\s*[Ff]ollower", og_desc)
            if followers_m:
                raw = followers_m.group(1).replace(".", "").replace(",", "")
                try:
                    data["ig_followers"] = int(raw)
                except ValueError:
                    pass

            posts_m = re.search(r"([\d,.]+)\s*[Pp]osts?", og_desc)
            if posts_m:
                try:
                    data["ig_posts"] = int(posts_m.group(1).replace(".", "").replace(",", ""))
                except ValueError:
                    pass

            # Bio — tenta extrair do meta description
            bio_m = re.search(r"Posts?\s*[-–]\s*(.+)", meta_desc, re.DOTALL)
            data["ig_bio"] = bio_m.group(1).strip()[:400] if bio_m else meta_desc[:400]

            # HTML completo para extração adicional
            html = page.content()

            # Website no perfil (JSON embeddado)
            for pattern in [
                r'"external_url"\s*:\s*"([^"]+)"',
                r'"website"\s*:\s*"([^"]+)"',
            ]:
                m = re.search(pattern, html)
                if m and m.group(1) not in ("null", ""):
                    data["ig_website"] = m.group(1)
                    # Se o site da bio for link de WhatsApp, extrai o telefone diretamente
                    import urllib.parse
                    decoded_ws = urllib.parse.unquote(m.group(1))
                    if any(k in decoded_ws.lower() for k in ("wa.me", "api.whatsapp.com", "whatsapp.com/send")):
                        phone_m = re.search(r'(?:phone=|\/)(\d{10,15})', decoded_ws)
                        if phone_m:
                            phone_digits = phone_m.group(1)
                            if not phone_digits.startswith("55") and len(phone_digits) in (10, 11):
                                phone_digits = "55" + phone_digits
                            data["ig_phone"] = f"+{phone_digits}"
                    break

            # Categoria
            for pattern in [
                r'"category_name"\s*:\s*"([^"]+)"',
                r'"category"\s*:\s*"([^"]+)"',
            ]:
                m = re.search(pattern, html)
                if m:
                    data["ig_category"] = m.group(1)
                    break

            # Email público
            m = re.search(r'"public_email"\s*:\s*"([^"]+)"', html)
            if m and m.group(1) not in ("null", ""):
                data["ig_email"] = m.group(1)

            # Telefone público
            m = re.search(r'"public_phone_number"\s*:\s*"([^"]+)"', html)
            if m and m.group(1) not in ("null", ""):
                data["ig_phone"] = m.group(1)

            # Também tenta extrair email da bio (comum em pequenas lojas)
            if not data["ig_email"]:
                emails_bio = re.findall(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}', data["ig_bio"])
                if emails_bio:
                    data["ig_email"] = emails_bio[0]

            browser.close()

    except Exception as e:
        data["ig_scrape_error"] = str(e)

    # Detecta sinais de loja/negócio na bio
    bio_lower = (data.get("ig_bio") or "").lower()
    data["ig_store_signals"] = [s for s in _STORE_SIGNALS if s in bio_lower]
    data["ig_parece_loja"] = len(data["ig_store_signals"]) >= 1 or bool(data.get("ig_category"))

    return data


def _extract_requested_count(prompt: str) -> int | None:
    """Extrai número pedido no prompt, ex: 'encontre 5 lojas' → 5."""
    m = re.search(r'\b(\d+)\s+(?:lead|empresa|perfil|loja|negócio|resultado|conta)', prompt, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _qualify_batch(profiles: list[dict], prompt: str, progress_cb=None) -> list[dict]:
    """Qualifica um lote de até 5 perfis via IA."""
    profiles_text = "\n\n".join(
        f"[{i+1}] @{p['ig_username']} — {p['ig_display_name']}\n"
        f"Bio: {p['ig_bio'][:150]}\n"
        f"Seg: {p['ig_followers'] or '?'} seguidores | {p['ig_posts'] or '?'} posts | Categ: {p['ig_category']}\n"
        f"Site: {p['ig_website'] or '-'} | Email: {p['ig_email'] or '-'} | Tel: {p['ig_phone'] or '-'}\n"
        f"Sinais: {', '.join(p['ig_store_signals'][:4]) or '-'}"
        for i, p in enumerate(profiles)
    )

    system = (
        "Você é especialista em prospecção via Instagram. Analise os perfis e extraia leads de negócios reais.\n"
        "Seja inclusivo: retorne qualquer perfil que pareça um negócio real, mesmo sem email.\n"
        "Retorne JSON array. Cada lead: nome_empresa, segmento, cidade, email, telefone, website, instagram, observacoes.\n"
        "Descarte apenas: perfis claramente pessoais sem produto, shoppings/centros comerciais, redes nacionais grandes."
    )

    try:
        result = ask_json(
            f"Pedido: {prompt}\n\nPerfis:\n{profiles_text}\n\nRetorne JSON array de leads qualificados.",
            system,
        )
        return result if isinstance(result, list) else next(
            (v for v in result.values() if isinstance(v, list)), []
        )
    except Exception as e:
        if progress_cb:
            progress_cb(f"[Instagram] Aviso qualificação: {e}")
        return []


def search(prompt: str, config: dict, progress_cb=None) -> list[dict]:
    num = config["search"]["results_per_query"]
    max_leads = min(
        config["limits"]["max_leads_per_run"],
        _extract_requested_count(prompt) or config["limits"]["max_leads_per_run"]
    )

    if progress_cb:
        progress_cb("[Instagram] Gerando queries de busca...")

    queries = _generate_queries(prompt)

    # Discovery de perfis
    seen_users, profiles_to_visit = set(), []
    for q in queries:
        if progress_cb:
            progress_cb(f"[Instagram] Buscando: {q}")
        for r in search_ddg(q, num):
            url = r.get("href", "")
            if not _is_profile_url(url):
                continue
            username = _extract_username(url)
            if username and username not in seen_users:
                seen_users.add(username)
                profiles_to_visit.append((username, r))
        if len(profiles_to_visit) >= max_leads + 3:
            break

    if not profiles_to_visit:
        return []

    # Analisa no máximo max_leads + 2 para ter margem de qualificação sem explodir o log
    profiles_to_visit = profiles_to_visit[:max_leads + 2]

    if progress_cb:
        progress_cb(f"[Instagram] {len(profiles_to_visit)} perfis encontrados. Extraindo dados...")

    # Extração de dados reais de cada perfil
    raw_profiles = []
    for i, (username, ddg_r) in enumerate(profiles_to_visit):
        if progress_cb:
            progress_cb(f"[Instagram] Analisando @{username} ({i+1}/{len(profiles_to_visit)})...")
        profile = _scrape_profile(username)
        profile["_ddg_snippet"] = ddg_r.get("body", "")
        raw_profiles.append(profile)

    if progress_cb:
        progress_cb("[Instagram] Qualificando leads com IA...")

    # Qualifica em lotes de 5 para evitar prompts gigantes
    leads = []
    batch_size = 5
    for i in range(0, len(raw_profiles), batch_size):
        batch = raw_profiles[i:i + batch_size]
        batch_leads = _qualify_batch(batch, prompt, progress_cb)
        leads.extend(batch_leads)
        if len(leads) >= max_leads:
            break

    # Mescla dados brutos de volta aos leads estruturados
    ig_map = {p["ig_username"]: p for p in raw_profiles}
    for lead in leads:
        ig_raw = _extract_username(lead.get("instagram") or "")
        if ig_raw and ig_raw in ig_map:
            p = ig_map[ig_raw]
            if not lead.get("email") and p.get("ig_email"):
                lead["email"] = p["ig_email"]
            if not lead.get("telefone") and p.get("ig_phone"):
                lead["telefone"] = p["ig_phone"]
            if not lead.get("website") and p.get("ig_website"):
                lead["website"] = p["ig_website"]
            # Campos extras do perfil
            for k in ["ig_followers", "ig_bio", "ig_category", "ig_store_signals",
                       "ig_parece_loja", "ig_posts", "ig_username"]:
                lead[k] = p.get(k, lead.get(k))
        lead["source"] = "instagram"

    return leads[:max_leads]
