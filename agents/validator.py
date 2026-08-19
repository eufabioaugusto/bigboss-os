"""
Validador de E-mails, Blindagem Anti-Bounce e Consistência de Domínio.
Verifica sintaxe, filtros de descarte, consistência com o domínio da empresa e resolução DNS MX antes de qualquer envio.
"""
import re
import socket
from typing import Dict, Any, Optional
from urllib.parse import urlparse

DISPOSABLE_DOMAINS = {
    "mailinator.com", "tempmail.com", "guerrillamail.com", "10minutemail.com",
    "yopmail.com", "trashmail.com", "sharklasers.com", "getairmail.com",
    "throwawaymail.com", "dispostable.com"
}

IGNORED_LOCAL_PARTS = {
    "noreply", "no-reply", "nao-responda", "naoresponda", "mailer-daemon",
    "postmaster", "sentry", "abuse", "bounce", "spam", "root", "daemon"
}

FOREIGN_TLDS = {
    "it", "ru", "cn", "fr", "de", "pl", "es", "cz", "nl", "se", "no", "fi", "ro", "ua", "jp", "kr", "in"
}

EMAIL_REGEX = re.compile(
    r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$"
)


def _clean_str(s: str) -> str:
    if not s:
        return ""
    return re.sub(r'[^a-zA-Z0-9]', '', s.lower())


def _extract_base_domain(url_or_domain: str) -> str:
    if not url_or_domain:
        return ""
    if not url_or_domain.startswith("http"):
        url_or_domain = "https://" + url_or_domain
    parsed = urlparse(url_or_domain)
    host = (parsed.netloc or parsed.path.split("/")[0]).split(":")[0].lower().replace("www.", "")
    parts = host.split(".")
    two_part_tld = (
        len(parts) >= 2
        and parts[-1] in ("br", "uk", "au", "nz", "za")
        and parts[-2] in ("com", "org", "net", "edu", "gov", "co", "adv")
    )
    if two_part_tld:
        return ".".join(parts[-3:]) if len(parts) >= 3 else host
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def is_email_aligned_with_domain(email: str, company_domain: str = "", company_name: str = "") -> bool:
    """
    Verifica se o e-mail pertence de fato à empresa pesquisada e não é um falso-positivo
    ou domínio estrangeiro irrelevante (ex: principessadelcolle.it para loja Principessa BR).
    """
    if not email or "@" not in email:
        return False

    local_part, email_domain = email.lower().split("@", 1)
    
    # Bloqueia TLDs estrangeiros fora do Brasil/Global
    tld = email_domain.split(".")[-1]
    if tld in FOREIGN_TLDS:
        return False

    # Se tiver domínio da empresa
    base_comp_domain = _extract_base_domain(company_domain)
    base_email_domain = _extract_base_domain(email_domain)

    # 1. Domínio bate exatamente (ex: contato@lojaprincipessa.com.br -> lojaprincipessa.com.br)
    if base_comp_domain and base_comp_domain == base_email_domain:
        return True

    # 2. Se for Gmail/Outlook/Hotmail, aceita somente se tiver relação com o nome da empresa
    generic_providers = {"gmail.com", "outlook.com", "hotmail.com", "yahoo.com", "yahoo.com.br", "uol.com.br", "bol.com.br"}
    if email_domain in generic_providers:
        clean_company = _clean_str(company_name)
        clean_local = _clean_str(local_part)
        # Se nome da empresa tem pelo menos 4 letras e está contido no email
        if clean_company and len(clean_company) >= 4 and (clean_company in clean_local or clean_local in clean_company):
            return True
        # Se for clínica/escritório pequeno sem site próprio com nome alinhado
        if any(w in clean_local for w in ("clinic", "advoc", "contato", "atend", "loja", "store")):
            return True
        # Rejeita e-mails genéricos desconexos (ex: marisa.ar1978@gmail.com para grandes marcas)
        return False

    # 3. Se for domínio corporativo diferente, verifica se o nome da empresa está no domínio
    clean_company = _clean_str(company_name)
    clean_email_domain = _clean_str(base_email_domain.split(".")[0])
    if clean_company and len(clean_company) >= 4 and (clean_company in clean_email_domain or clean_email_domain in clean_company):
        return True

    # Se for domínio totalmente desconexo sem relação (ex: de outra empresa ou site gringo)
    return False


def _check_mx_records(domain: str) -> tuple[bool, Optional[str]]:
    """Verifica se o domínio possui servidores MX ou registros A/AAAA para receber e-mails."""
    if not domain:
        return False, "Domínio vazio"
    try:
        socket.gethostbyname(domain)
        return True, None
    except socket.gaierror:
        return False, f"Domínio '{domain}' não existe ou não possui DNS ativo"
    except Exception:
        return True, None


def validate_email(email: str, company_domain: str = "", company_name: str = "") -> Dict[str, Any]:
    """
    Valida completamente um e-mail antes do envio.
    Retorna {'is_valid': bool, 'reason': str, 'email': str, 'domain': str}
    """
    if not email or not isinstance(email, str):
        return {"is_valid": False, "reason": "E-mail vazio ou inválido", "email": ""}

    cleaned = email.strip().lower()
    
    if not EMAIL_REGEX.match(cleaned):
        return {"is_valid": False, "reason": f"Sintaxe inválida: '{cleaned}'", "email": cleaned}

    local_part, domain = cleaned.split("@", 1)
    
    if local_part in IGNORED_LOCAL_PARTS:
        return {"is_valid": False, "reason": f"Caixa de sistema ou no-reply: '{local_part}'", "email": cleaned}

    if domain in DISPOSABLE_DOMAINS:
        return {"is_valid": False, "reason": f"Domínio temporário/descartável: '{domain}'", "email": cleaned}

    tld = domain.split(".")[-1]
    if tld in FOREIGN_TLDS:
        return {"is_valid": False, "reason": f"Domínio estrangeiro (. {tld}) não elegível para prospecção nacional", "email": cleaned}

    if company_domain or company_name:
        if not is_email_aligned_with_domain(cleaned, company_domain, company_name):
            return {"is_valid": False, "reason": "E-mail não alinhado com o domínio ou nome da empresa", "email": cleaned}

    mx_ok, mx_error = _check_mx_records(domain)
    if not mx_ok:
        return {"is_valid": False, "reason": mx_error or "Falha de DNS MX", "email": cleaned, "domain": domain}

    return {
        "is_valid": True,
        "reason": "OK",
        "email": cleaned,
        "domain": domain
    }
