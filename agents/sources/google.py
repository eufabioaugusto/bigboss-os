"""
Fonte: Google (via DuckDuckGo)
IA gera as queries → DDG acha os sites → Playwright extrai dados reais de cada página.
"""
import re
from agents.researcher import search_ddg
from agents.ai import ask_json


_SKIP_DOMAINS = {
    "facebook.com", "instagram.com", "tiktok.com", "youtube.com", "pinterest.com",
    "linkedin.com", "twitter.com", "x.com", "wikipedia.org", "globo.com", "uol.com.br",
    "ifood.com.br", "tripadvisor.com", "booking.com", "mercadolivre.com.br", "mercadolivre.com",
    "americanas.com.br", "shopee.com.br", "shopee.com", "magalu.com.br", "olx.com.br", "enjoei.com.br",
    "shein.com", "zara.com", "hm.com", "aliexpress.com", "amazon.com.br", "amazon.com",
    "dafit.com.br", "netshoes.com.br", "zattini.com.br", "marisa.com.br", "riachuelo.com.br",
    "cea.com.br", "lojasrenner.com.br", "submarino.com.br", "shoptime.com.br",
    "jusbrasil.com.br", "reclameaqui.com.br", "doctoralia.com.br", "consultaremedios.com.br",
    "google.com", "bing.com", "gov.br",
}

_EMAIL_IGNORE = {"noreply", "no-reply", "example", "test", "sentry", "wixpress", "shopify"}


def _is_business_url(url: str) -> bool:
    try:
        from urllib.parse import urlparse
        domain = urlparse(url).netloc.lower().replace("www.", "")
        return not any(skip in domain for skip in _SKIP_DOMAINS)
    except Exception:
        return False


def _generate_queries(prompt: str) -> list[str]:
    """IA gera queries de busca relevantes baseadas no prompt."""
    try:
        result = ask_json(
            f"Gere 6 queries de busca no Google para encontrar empresas reais com base neste pedido: '{prompt}'\n\n"
            "Regras:\n"
            "- Cada query: 4–8 palavras, objetiva\n"
            "- Inclua a cidade/estado se mencionado no pedido\n"
            "- Varie: uma geral, uma com 'contato', uma com 'email', uma com 'site oficial', uma com 'telefone'\n"
            "- Use português brasileiro\n"
            "- NÃO use aspas extras, colchetes, ou formatação — só o texto da query\n"
            "Retorne JSON array de strings.",
        )
        if isinstance(result, list):
            return [str(q).strip() for q in result if q and isinstance(q, str)][:6]
    except Exception:
        pass
    # Fallback sem normalização agressiva
    clean = re.sub(r'\b(encontre|busque|liste|preciso|quero|me dê)\b', '', prompt, flags=re.I).strip()
    return [clean, f"{clean} contato", f"{clean} email", f"{clean} site"]


def _extract_from_page(url: str, timeout_ms: int = 12000) -> dict:
    """Visita o site com Playwright e extrai dados de contato reais."""
    result = {"url": url, "title": "", "emails": [], "phones": [], "instagrams": []}
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                locale="pt-BR",
            )
            page = ctx.new_page()
            page.set_default_timeout(timeout_ms)
            page.goto(url, wait_until="domcontentloaded")
            result["title"] = page.title()
            html = page.content()

            # Tenta visitar /contato se existir
            for path in ["/contato", "/contact", "/fale-conosco"]:
                try:
                    r = page.goto(url.rstrip("/") + path, wait_until="domcontentloaded")
                    if r and r.status < 400:
                        html += page.content()
                        break
                except Exception:
                    pass

            browser.close()

            # Extrai emails
            raw_emails = re.findall(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}', html)
            result["emails"] = [
                e.lower() for e in dict.fromkeys(raw_emails)
                if not any(x in e.lower() for x in _EMAIL_IGNORE)
            ][:3]

            # Telefones BR
            phones = re.findall(r'(?:\+55\s?)?(?:\(?\d{2}\)?\s?)(?:9\s?)?\d{4}[\s\-]?\d{4}', html)
            result["phones"] = [phones[0].strip()] if phones else []

            # Instagram na página
            igs = re.findall(r'instagram\.com/([a-zA-Z0-9_.]{2,40})', html)
            skip_ig = {"p", "reel", "explore", "accounts", "stories", "tv", ""}
            result["instagrams"] = [ig for ig in dict.fromkeys(igs) if ig.lower() not in skip_ig][:2]

    except Exception:
        pass
    return result


def search(prompt: str, config: dict, progress_cb=None) -> list[dict]:
    num = config["search"]["results_per_query"]
    max_leads = config["limits"]["max_leads_per_run"]

    if progress_cb:
        progress_cb("[Google] Gerando estratégia de busca...")

    queries = _generate_queries(prompt)

    # Discovery: coleta URLs únicas de negócios reais
    seen_urls, candidate_urls, ddg_results = set(), [], []
    for q in queries:
        if progress_cb:
            progress_cb(f"[Google] Buscando: {q}")
        results = search_ddg(q, num)
        ddg_results.extend(results)
        for r in results:
            url = r.get("href", "")
            if url and url not in seen_urls and _is_business_url(url):
                seen_urls.add(url)
                candidate_urls.append((url, r))
        if len(candidate_urls) >= max_leads * 3:
            break

    if not candidate_urls:
        return []

    if progress_cb:
        progress_cb(f"[Google] {len(candidate_urls)} sites encontrados. Extraindo dados reais...")

    # Enriquecimento: visita cada site com Playwright
    enriched = []
    for i, (url, ddg_r) in enumerate(candidate_urls[:max_leads * 2]):
        if progress_cb:
            progress_cb(f"[Google] Visitando site {i+1}/{min(len(candidate_urls), max_leads*2)}...")
        data = _extract_from_page(url)
        data["ddg_title"] = ddg_r.get("title", "")
        data["ddg_snippet"] = ddg_r.get("body", "")
        enriched.append(data)

    if not enriched:
        return []

    # IA estrutura os leads com base nos dados reais coletados
    sites_text = "\n\n".join(
        f"[{i+1}] {d['ddg_title'] or d['url']}\n"
        f"URL: {d['url']}\n"
        f"Emails: {', '.join(d['emails']) or 'nenhum'}\n"
        f"Telefone: {', '.join(d['phones']) or 'nenhum'}\n"
        f"Instagram: {', '.join(d['instagrams']) or 'nenhum'}\n"
        f"Snippet: {d['ddg_snippet'][:200]}"
        for i, d in enumerate(enriched)
    )

    if progress_cb:
        progress_cb("[Google] Qualificando leads com IA...")

    system = (
        "Você é especialista em prospecção comercial para agência de marketing 360°, tráfego pago e crescimento digital.\n"
        "Analise os sites encontrados e extraia leads qualificados de empresas reais de qualquer segmento (varejo físico, atacado/distribuidoras B2B, clínicas médicas/estética/odonto, escritórios de arquitetura/advocacia/contabilidade, experts/infoprodutos, criadores/vídeo ou qualquer negócio local/regional).\n"
        "Regras Críticas:\n"
        "1. Descarte apenas: marketplaces gigantes de consumo (como Mercado Livre, Shopee, SHEIN), diretórios de telefonia, portais de notícias e órgãos governamentais.\n"
        "2. Valide a localização: empresas brasileiras ou com operação no Brasil.\n"
        "3. Contato: extraia dados oficiais (site, e-mails comerciais, WhatsApp/telefone e redes sociais).\n"
        "Para cada empresa real identificada, retorne um objeto JSON com:\n"
        "nome_empresa, responsavel (null se não encontrado), segmento, cidade, estado, "
        "email, telefone, website, instagram, linkedin, observacoes.\n"
        "Use null para campos indisponíveis. Retorne JSON array."
    )

    try:
        result = ask_json(
            f"Pedido de prospecção: {prompt}\n\nSites analisados:\n{sites_text}\n\nRetorne JSON array de leads qualificados.",
            system,
        )
        leads = result if isinstance(result, list) else next((v for v in result.values() if isinstance(v, list)), [])
    except Exception:
        leads = []

    for lead in leads:
        lead["source"] = "google"

    return leads[:max_leads]
