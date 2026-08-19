"""
Fonte: TikTok Shop Prospects
Lojas no Instagram que ainda não estão no TikTok Shop.
Lógica: busca lojas no Instagram → verifica presença no TikTok → prioriza quem não tem Shop.
"""
import re
from agents.researcher import search_ddg
from agents.sources.instagram import _scrape_profile as scrape_ig, _extract_username as ig_username
from agents.sources.tiktok import _scrape_profile as scrape_tt, _extract_username as tt_username, _is_profile_url as tt_is_profile
from agents.ai import ask_json

_STORE_QUERIES = [
    'site:instagram.com loja {base} -explore -reel',
    'site:instagram.com shop {base} frete -reel',
    'site:instagram.com "loja online" {base}',
    'site:instagram.com {base} "envio para todo brasil"',
    'site:instagram.com {base} "compre pelo link"',
]


def _check_tiktok(business_name: str, ig_username: str, progress_cb=None) -> dict:
    """Verifica se o negócio tem TikTok e se tem TikTok Shop."""
    result = {"tt_status": "not_found", "tt_username": None, "tt_has_shop": False}

    queries = [
        f'site:tiktok.com "@{ig_username}"',
        f'site:tiktok.com "{business_name}"',
    ]
    for q in queries:
        for r in search_ddg(q, 5):
            url = r.get("href", "")
            if tt_is_profile(url):
                username = tt_username(url)
                if username:
                    if progress_cb:
                        progress_cb(f"[TikTok Shop] TikTok encontrado: @{username} — verificando Shop...")
                    profile = scrape_tt(username)
                    result["tt_username"] = username
                    result["tt_followers"] = profile.get("tt_followers")
                    result["tt_has_shop"] = profile.get("tt_has_shop", False)
                    result["tt_status"] = "has_shop" if result["tt_has_shop"] else "found_no_shop"
                    return result
        if result["tt_status"] != "not_found":
            break

    return result


def search(prompt: str, config: dict, progress_cb=None) -> list[dict]:
    num = config["search"]["results_per_query"]
    from agents.sources.instagram import _extract_requested_count
    max_leads = min(
        config["limits"]["max_leads_per_run"],
        _extract_requested_count(prompt) or config["limits"]["max_leads_per_run"]
    )

    clean = re.sub(r"[^\w\s]", " ", prompt.lower())
    words = [w for w in clean.split() if len(w) > 2][:5]
    base = " ".join(words)

    seen_users, profile_urls = set(), []
    for q_tpl in _STORE_QUERIES:
        q = q_tpl.format(base=base)
        if progress_cb:
            progress_cb(f"[TikTok Shop] Buscando lojas: {q}")
        for r in search_ddg(q, num):
            url = r.get("href", "")
            from agents.sources.instagram import _is_profile_url
            if _is_profile_url(url):
                user = ig_username(url)
                if user and user not in seen_users:
                    seen_users.add(user)
                    profile_urls.append((user, r))
        if len(profile_urls) >= max_leads + 3:
            break

    if not profile_urls:
        return []

    if progress_cb:
        progress_cb(f"[TikTok Shop] {len(profile_urls)} lojas Instagram encontradas. Analisando...")

    leads = []
    for i, (user, result) in enumerate(profile_urls[:max_leads + 2]):
        if progress_cb:
            progress_cb(f"[TikTok Shop] Analisando @{user} ({i+1}/{min(len(profile_urls), max_leads*2)})...")

        ig_data = scrape_ig(user)

        # Descarta perfis sem sinais de loja
        if not ig_data.get("ig_parece_loja") and not ig_data.get("ig_category"):
            continue

        business_name = ig_data.get("ig_display_name") or user
        tt_data = _check_tiktok(business_name, user, progress_cb)

        # Descarta quem já tem TikTok Shop — já convertido
        if tt_data["tt_status"] == "has_shop":
            if progress_cb:
                progress_cb(f"[TikTok Shop] @{user} já tem TikTok Shop — descartado.")
            continue

        lead = {
            "nome_empresa": business_name,
            "instagram": f"https://instagram.com/{user}",
            "segmento": ig_data.get("ig_category", ""),
            "email": ig_data.get("ig_email", ""),
            "telefone": ig_data.get("ig_phone", ""),
            "website": ig_data.get("ig_website", ""),
            "source": "tiktok_shop",
            "tt_status": tt_data["tt_status"],
            "tt_username": tt_data.get("tt_username"),
            "tt_has_shop": tt_data.get("tt_has_shop", False),
            "ig_followers": ig_data.get("ig_followers"),
            "ig_bio": ig_data.get("ig_bio", ""),
            "ig_store_signals": ig_data.get("ig_store_signals", []),
            "ig_parece_loja": ig_data.get("ig_parece_loja", False),
            "observacoes": (
                "Sem TikTok" if tt_data["tt_status"] == "not_found"
                else "TikTok sem Shop"
            ),
        }
        leads.append(lead)

        if len(leads) >= max_leads:
            break

    if not leads:
        return []

    # IA enriquece e estrutura os leads
    leads_text = "\n\n".join(
        f"[{i+1}] @{l.get('instagram','').split('/')[-1]} — {l.get('nome_empresa','')}\n"
        f"Bio: {l.get('ig_bio','')[:200]}\n"
        f"Seguidores: {l.get('ig_followers','?')} | Sinais loja: {', '.join(l.get('ig_store_signals',[]))}\n"
        f"TikTok: {l.get('tt_status','?')} | Shop: {'sim' if l.get('tt_has_shop') else 'não'}\n"
        f"Email: {l.get('email','')} | Tel: {l.get('telefone','')}"
        for i, l in enumerate(leads)
    )

    system = """Você é especialista em prospecção para TikTok Shop.
Analise lojas do Instagram que ainda não estão no TikTok Shop.
Complete os campos que conseguir inferir e adicione observacoes com o principal argumento de venda.
Retorne JSON array com: nome_empresa, segmento, cidade, email, telefone, website, instagram, observacoes."""

    try:
        result = ask_json(
            f"Prompt: {prompt}\n\nLojas Instagram sem TikTok Shop:\n{leads_text}\n\nRetorne JSON array.",
            system,
        )
        enriched = result if isinstance(result, list) else next((v for v in result.values() if isinstance(v, list)), [])
        ig_map = {l["instagram"].rstrip("/").split("/")[-1].lower(): l for l in leads if l.get("instagram")}
        for lead in enriched:
            user = lead.get("instagram", "").rstrip("/").split("/")[-1].lower()
            if user in ig_map:
                raw = ig_map[user]
                for k in ["source", "tt_status", "tt_username", "tt_has_shop", "ig_followers",
                           "ig_bio", "ig_store_signals", "ig_parece_loja"]:
                    lead[k] = raw.get(k, lead.get(k))
        return enriched[:max_leads]
    except Exception:
        return leads[:max_leads]
