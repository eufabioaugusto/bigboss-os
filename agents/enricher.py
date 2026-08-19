"""
Enriquece leads com Playwright (JS renderizado) + fallback urllib.
Extrai email, instagram, telefone do site real.
"""
import re
import ssl
import urllib.request
import urllib.error
from urllib.parse import urlparse

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

CONTACT_PATHS = ["/contato", "/contact", "/fale-conosco", "/sobre", "/atendimento"]
_EMAIL_IGNORE = {"nuvempago", "example", "test", "noreply", "no-reply", "sentry", "wix", "shopify"}


def _main_domain(url: str) -> str:
    if not url:
        return ""
    if not url.startswith("http"):
        url = "https://" + url
    parsed = urlparse(url)
    host = (parsed.netloc or parsed.path.split("/")[0]).split(":")[0]
    parts = host.split(".")
    two_part_tld = (
        len(parts) >= 2
        and parts[-1] in ("br", "uk", "au", "nz", "za")
        and parts[-2] in ("com", "org", "net", "edu", "gov", "co")
    )
    if two_part_tld:
        return ".".join(parts[-3:]) if len(parts) >= 3 else host
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def _clean_emails(raw: list) -> list:
    skip_prefixes = tuple(_EMAIL_IGNORE)
    result = []
    for e in raw:
        e = e.lower().strip(".,;")
        if not any(e.startswith(s) for s in skip_prefixes) and "@" in e and e not in result:
            result.append(e)
    return result


def _extract_instagram(html: str) -> str:
    matches = re.findall(r'instagram\.com/([a-zA-Z0-9_.]{2,40})', html)
    skip = {"p", "reel", "explore", "accounts", "stories", "tv", ""}
    for m in matches:
        if m.lower() not in skip:
            return f"https://instagram.com/{m}"
    return None


def _is_mobile_or_whatsapp(phone: str) -> bool:
    if not phone:
        return False
    digits = "".join(filter(str.isdigit, phone))
    if digits.startswith("55"):
        digits = digits[2:]
    
    # Celulares no Brasil têm 11 dígitos no total (DDD + 9 + 8 dígitos) ou 9 dígitos (sem DDD, começando com 9)
    if len(digits) == 11 and digits[2] == "9":
        return True
    if len(digits) == 9 and digits[0] == "9":
        return True
    return False


def _extract_whatsapp_from_html(html: str) -> str | None:
    import urllib.parse
    # 1. Procura por links wa.me ou api.whatsapp.com
    wa_matches = re.findall(r'(?:wa\.me|whatsapp\.com/send|api\.whatsapp\.com/send)[^\'"\s>]+', html)
    for match in wa_matches:
        decoded = urllib.parse.unquote(match)
        phone_match = re.search(r'(?:phone=|\/)(\d{10,15})', decoded)
        if phone_match:
            phone = phone_match.group(1)
            if phone.startswith("55") and len(phone) >= 12:
                return f"+{phone}"
            elif len(phone) >= 10:
                return f"+55{phone}"
                
    # 2. Se não achou links de WhatsApp, procura por tags <a href="tel:..."> ou <a href="phone:..."> que tenham números de celular
    tel_matches = re.findall(r'href=["\']tel:([^"\']+)["\']', html)
    for match in tel_matches:
        digits = "".join(filter(str.isdigit, match))
        if len(digits) >= 10:
            if len(digits) == 11 and digits[2] == '9':
                return f"+55{digits}"
            elif len(digits) == 13 and digits.startswith("55") and digits[4] == '9':
                return f"+{digits}"
    return None


def _extract_phone(html: str) -> str:
    # Tenta extrair WhatsApp do HTML primeiro
    whatsapp_phone = _extract_whatsapp_from_html(html)
    if whatsapp_phone:
        return whatsapp_phone
    # Fallback para regex genérico
    phones = re.findall(r'(?:\+55\s?)?(?:\(?\d{2}\)?\s?)(?:9\s?)?\d{4}[\s\-]?\d{4}', html)
    return phones[0].strip() if phones else None


def _fetch_simple(url: str, timeout: int = 8):
    """Fetch estático via urllib — rápido, sem JS."""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
            "Accept-Language": "pt-BR,pt;q=0.9",
        })
        with urllib.request.urlopen(req, context=_CTX, timeout=timeout) as r:
            return r.status, r.read(100_000).decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception:
        return 0, ""


def _detect_technologies(html_content: str) -> list[str]:
    """Detecta CMS, ferramentas de ads e scripts instalados no HTML."""
    techs = []
    lower = html_content.lower()
    
    if any(k in lower for k in ("cdn.shopify.com", "myshopify", "shopify.theme")):
        techs.append("Shopify")
    if any(k in lower for k in ("woocommerce", "wp-content/plugins/woocommerce", "wc-ajax")):
        techs.append("WooCommerce")
    if any(k in lower for k in ("nuvemshop", "tiendanube", "d26lpennugtm8s")):
        techs.append("Nuvemshop")
    if "wp-content" in lower or "wp-includes" in lower:
        techs.append("WordPress")
    if "vtexassets" in lower or "vteximg" in lower:
        techs.append("VTEX")
    if "wix.com" in lower or "wixstatic.com" in lower:
        techs.append("Wix")
        
    # Pixels & Analytics
    if any(k in lower for k in ("connect.facebook.net", "fbevents.js", "fbq(")):
        techs.append("Meta Pixel")
    if "googletagmanager.com/gtm.js" in lower:
        techs.append("Google Tag Manager")
    if "googletagmanager.com/gtag" in lower or "google-analytics.com" in lower or "gtag(" in lower:
        techs.append("Google Analytics (GA4)")
    if "analytics.tiktok.com" in lower or "ttq." in lower:
        techs.append("TikTok Pixel")
    if "hotjar.com" in lower or "clarity.ms" in lower:
        techs.append("Heatmaps (CRO)")
        
    return list(dict.fromkeys(techs))


def _diagnose_bottlenecks(techs: list[str], site_exists: bool, has_ig: bool) -> list[str]:
    """Gera diagnósticos de negócio reais para a IA usar na copy."""
    bottlenecks = []
    is_ecommerce = any(t in ("Shopify", "WooCommerce", "Nuvemshop", "VTEX") for t in techs)
    has_meta_pixel = "Meta Pixel" in techs
    has_ga4 = any(t in ("Google Analytics (GA4)", "Google Tag Manager") for t in techs)
    
    if not site_exists:
        bottlenecks.append("sem_site_proprio")
    else:
        if is_ecommerce and not has_meta_pixel:
            bottlenecks.append("ecommerce_sem_pixel_meta")
        if not has_ga4:
            bottlenecks.append("site_sem_analytics")
        if "WordPress" in techs and not is_ecommerce:
            bottlenecks.append("site_institucional_antigo")
            
    return bottlenecks


def _fetch_js(url: str, extra_paths: list = None) -> dict:
    """Fetch com Playwright — renderiza JS, extrai email/instagram/telefone e tecnologias."""
    from playwright.sync_api import sync_playwright

    result = {"emails": [], "instagram": None, "telefone": None, "status": 0, "techs": []}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                locale="pt-BR",
            )
            page = ctx.new_page()
            page.set_default_timeout(15000)

            resp = page.goto(url, wait_until="networkidle")
            result["status"] = resp.status if resp else 0

            pages_to_scrape = [page.content()]

            # visita páginas de contato
            if extra_paths:
                for path in extra_paths:
                    try:
                        r2 = page.goto(url.rstrip("/") + path, wait_until="networkidle")
                        if r2 and r2.status < 400:
                            pages_to_scrape.append(page.content())
                    except Exception:
                        pass

            browser.close()

            all_techs = []
            for html in pages_to_scrape:
                raw = re.findall(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}', html)
                result["emails"].extend(_clean_emails(raw))
                if not result["instagram"]:
                    result["instagram"] = _extract_instagram(html)
                if not result["telefone"]:
                    result["telefone"] = _extract_phone(html)
                all_techs.extend(_detect_technologies(html))

            result["emails"] = list(dict.fromkeys(result["emails"]))  # deduplica mantendo ordem
            result["techs"] = list(dict.fromkeys(all_techs))

    except Exception as e:
        result["error"] = str(e)

    return result


def enrich_lead(lead: dict) -> dict:
    raw_url = lead.get("website") or ""
    domain = _main_domain(raw_url)

    result = {
        "site_existe": False,
        "website_verificado": None,
        "email_verificado": lead.get("email"),
        "instagram_verificado": lead.get("instagram"),
        "telefone_verificado": lead.get("telefone"),
        "tem_google_meu_negocio": False,
        "tecnologias": [],
        "gargalos_tecnicos": [],
    }

    if not domain:
        result["gargalos_tecnicos"].append("sem_site_proprio")
        lead.update(result)
        return lead

    base_url = f"https://{domain}"

    # 1. Playwright — renderiza JS, visita home + /contato
    pw = _fetch_js(base_url, extra_paths=CONTACT_PATHS[:3])
    result["site_existe"] = pw.get("status", 0) in range(200, 400)
    result["tecnologias"] = pw.get("techs", [])

    if result["site_existe"]:
        result["website_verificado"] = base_url
        lead["website"] = base_url
        if pw.get("telefone"):
            result["telefone_verificado"] = pw["telefone"]

    from agents.validator import is_email_aligned_with_domain, FOREIGN_TLDS

    company_name = lead.get("nome_empresa", "")

    # Valida e-mail extraído do Playwright
    if result["site_existe"] and pw["emails"]:
        for candidate_email in pw["emails"]:
            if is_email_aligned_with_domain(candidate_email, domain, company_name):
                result["email_verificado"] = candidate_email
                break

    # 2. Busca complementar para Instagram e GMN
    from agents.researcher import search_ddg
    if company_name and (not result["instagram_verificado"] or not result["email_verificado"]):
        ddg = search_ddg(f'"{company_name}" site oficial contato', 6)
        for r in ddg:
            href = r.get("href", "")
            text = r.get("body", "") + " " + r.get("title", "")

            if not result["tem_google_meu_negocio"]:
                if "google.com/maps" in href or "maps.google" in href:
                    result["tem_google_meu_negocio"] = True

            if not result["instagram_verificado"] and "instagram.com" in href:
                ig = re.search(r'instagram\.com/([a-zA-Z0-9_.]{2,40})', href)
                if ig and ig.group(1).lower() not in ("p","reel","explore","accounts","stories"):
                    result["instagram_verificado"] = f"https://instagram.com/{ig.group(1)}"

            if not result["email_verificado"]:
                emails = _clean_emails(re.findall(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}', text))
                for candidate_email in emails:
                    if is_email_aligned_with_domain(candidate_email, domain, company_name):
                        result["email_verificado"] = candidate_email
                        break

    # Se o email pré-existente no lead era inválido/desconexo, limpa
    if lead.get("email") and not is_email_aligned_with_domain(lead["email"], domain, company_name):
        lead["email"] = None

    # aplica dados verificados e consistentes ao lead
    if result["email_verificado"]:
        lead["email"] = result["email_verificado"]
    if result["instagram_verificado"]:
        lead["instagram"] = result["instagram_verificado"]
        
    new_phone = result.get("telefone_verificado")
    old_phone = lead.get("telefone")
    if new_phone:
        if not old_phone or _is_mobile_or_whatsapp(new_phone) or not _is_mobile_or_whatsapp(old_phone):
            lead["telefone"] = new_phone
            result["telefone_verificado"] = new_phone

    lead.update(result)
    return lead


def enrich_leads(leads: list, progress_cb=None) -> list:
    enriched = []
    for i, lead in enumerate(leads):
        name = lead.get("nome_empresa") or f"lead {i+1}"
        source = lead.get("source", "google")

        # Instagram/TikTok: dados já foram extraídos pelo researcher — pula scraping de site
        if source in ("instagram", "tiktok", "tiktok_shop"):
            if progress_cb:
                progress_cb(f"[{source}] {name} — dados já extraídos do perfil.")
            # Marca campos de enriquecimento como concluídos para não bloquear o pipeline
            lead.setdefault("site_existe", bool(lead.get("website") or lead.get("ig_website") or lead.get("tt_website")))
            lead.setdefault("website_verificado", lead.get("website") or lead.get("ig_website") or lead.get("tt_website"))
            lead.setdefault("email_verificado", lead.get("email") or lead.get("ig_email"))
            lead.setdefault("instagram_verificado", lead.get("instagram"))
            enriched.append(lead)
            continue

        if progress_cb:
            progress_cb(f"Verificando {name} ({i+1}/{len(leads)})...")
        enriched.append(enrich_lead(lead))
    return enriched
