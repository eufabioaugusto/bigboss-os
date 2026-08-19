"""
Gera emails personalizados usando os templates da empresa + IA para personalização.
"""
import json
import yaml
from pathlib import Path
from agents.ai import ask_json
from settings_store import get_company
from templates_store import get_templates

BASE_DIR = Path(__file__).parent.parent


def load_company() -> dict:
    return get_company()


def load_angles() -> dict:
    with open(BASE_DIR / "templates/angles.yaml") as f:
        return yaml.safe_load(f)["angles"]


def load_email_templates() -> list[dict]:
    return get_templates(active_only=True)


def _pick_template(lead: dict, templates: list[dict]) -> dict:
    """Escolhe o template mais adequado ao diagnóstico do lead."""
    angulo = lead.get("angulo", "default")
    for template in templates:
        if angulo in (template.get("angle_targets") or []):
            return template
    for template in templates:
        if "default" in (template.get("angle_targets") or []):
            return template
    return templates[0]


def _render_template(template: dict, lead: dict, company: dict) -> dict:
    """Preenche variáveis do template com dados do lead e da empresa."""
    vars_ = {
        "empresa": lead.get("nome_empresa", "sua empresa"),
        "segmento": lead.get("segmento", "seu segmento"),
        "cidade": lead.get("cidade", "sua cidade"),
        "sinal_principal": (lead.get("sinais") or ["presença digital"]) [0],
        "angulo_label": lead.get("angulo", ""),
        "contact_name": company.get("contact_name", company.get("name", "")),
        "agency_email": company.get("email", ""),
        "agency_name": company.get("name", ""),
    }
    assunto = template["assunto"]
    corpo = template["corpo"]
    for k, v in vars_.items():
        assunto = assunto.replace(f"{{{{{k}}}}}", str(v)).replace(f"{{{k}}}", str(v))
        corpo = corpo.replace(f"{{{{{k}}}}}", str(v)).replace(f"{{{k}}}", str(v))
    return {"assunto": assunto.strip(), "corpo": corpo.strip()}


_PERSONALIZE_SYSTEMS = {
    "google": """Você é copywriter especialista em prospecção B2B digital.
Personalize o email mencionando 1 detalhe específico do site ou presença digital do lead.
- Nunca use "eu", "percebi", "posso" — use "identificamos", "nossa análise", "encontramos"
- Tom: direto, profissional, orientado a resultado
- Máximo 140 palavras no corpo
- Retorne JSON com: assunto (string), corpo (string)""",

    "instagram": """Você é copywriter especialista em crescimento via Instagram.
Personalize o email mencionando algo específico do perfil Instagram do lead (bio, seguidores, frequência de posts, ausência de site).
- Use linguagem mais próxima e direta — o lead é empreendedor ativo no Instagram
- Mencione o Instagram deles: "vimos seu perfil @...", "sua loja no Instagram..."
- Nunca use "eu" — use "identificamos", "nossa equipe viu", "analisamos"
- Máximo 130 palavras no corpo
- Retorne JSON com: assunto (string), corpo (string)""",

    "tiktok": """Você é copywriter especialista em marketing no TikTok.
Personalize o email com base nos dados do perfil TikTok do lead.
- Fale sobre o potencial de monetização no TikTok
- Mencione algo concreto: número de vídeos, seguidores, ausência de TikTok Shop
- Tom: empolgante mas profissional — TikTok é sobre crescimento rápido
- Máximo 130 palavras no corpo
- Retorne JSON com: assunto (string), corpo (string)""",

    "tiktok_shop": """Você é copywriter especialista em TikTok Shop e e-commerce social.
O lead tem uma loja no Instagram mas ainda não está no TikTok Shop.
Personalize o email destacando essa oportunidade específica.
- Se não tem TikTok: "sua loja não existe no TikTok ainda — seus concorrentes já estão lá"
- Se tem TikTok sem Shop: "você já tem audiência no TikTok mas ainda não monetiza com o Shop"
- Tom: urgência suave, oportunidade concreta, sem ser alarmista
- Mencione o nicho/produto deles se tiver na bio
- Máximo 130 palavras no corpo
- Retorne JSON com: assunto (string), corpo (string)""",
}

PERSONALIZE_SYSTEM = _PERSONALIZE_SYSTEMS["google"]  # fallback


def _personalize_with_ai(draft: dict, lead: dict, company: dict) -> dict:
    """Usa IA para personalizar o email conforme a fonte do lead."""
    source = lead.get("source", "google")
    system = _PERSONALIZE_SYSTEMS.get(source, _PERSONALIZE_SYSTEMS["google"])

    # Contexto adaptado por source
    source_context = ""
    if source == "instagram":
        source_context = (
            f"- Instagram: @{lead.get('instagram','').split('/')[-1]}\n"
            f"- Seguidores: {lead.get('ig_followers','?')}\n"
            f"- Bio: {(lead.get('ig_bio') or '')[:200]}\n"
            f"- Categoria: {lead.get('ig_category','')}\n"
            f"- Sinais de loja: {', '.join(lead.get('ig_store_signals',[]))}\n"
            f"- Tem site: {'sim' if lead.get('ig_website') or lead.get('website') else 'não'}"
        )
    elif source == "tiktok":
        source_context = (
            f"- TikTok: @{lead.get('tt_username','')}\n"
            f"- Seguidores: {lead.get('tt_followers','?')}\n"
            f"- Vídeos: {lead.get('tt_video_count','?')}\n"
            f"- Likes totais: {lead.get('tt_likes','?')}\n"
            f"- Bio: {(lead.get('tt_bio') or '')[:200]}\n"
            f"- TikTok Shop: {'sim' if lead.get('tt_has_shop') else 'não'}"
        )
    elif source == "tiktok_shop":
        tt_status = lead.get("tt_status", "not_found")
        source_context = (
            f"- Instagram: @{lead.get('instagram','').split('/')[-1]}\n"
            f"- Seguidores IG: {lead.get('ig_followers','?')}\n"
            f"- Bio: {(lead.get('ig_bio') or '')[:200]}\n"
            f"- Sinais de loja: {', '.join(lead.get('ig_store_signals',[]))}\n"
            f"- Status TikTok: {'sem TikTok algum' if tt_status=='not_found' else 'tem TikTok mas sem Shop'}\n"
            f"- TikTok: @{lead.get('tt_username','sem conta')}"
        )
    else:
        techs_str = ", ".join(lead.get("tecnologias", [])) or "Nenhuma detectada"
        gargalos_str = ", ".join(lead.get("gargalos_tecnicos", [])) or "Nenhum detectado"
        source_context = (
            f"- Site: {lead.get('website','')}\n"
            f"- Tecnologias no site: {techs_str}\n"
            f"- Gargalos técnicos: {gargalos_str}\n"
            f"- Instagram: {lead.get('instagram','')}\n"
            f"- Sinais: {', '.join(lead.get('sinais',[]))}\n"
            f"- Google Meu Negócio: {lead.get('tem_google_meu_negocio','?')}"
        )

    context = f"""Email template:
Assunto: {draft['assunto']}
Corpo: {draft['corpo']}

Dados do lead ({source}):
- Empresa: {lead.get('nome_empresa')}
- Segmento: {lead.get('segmento')}
- Cidade: {lead.get('cidade')}
{source_context}

Empresa remetente: {company.get('name')} — {company.get('description','')[:200]}

Personalize o email com 1 detalhe específico. Retorne JSON com assunto e corpo."""

    try:
        return ask_json(context, system)
    except Exception:
        return draft


def generate_emails_for_leads(leads: list, config: dict, templates_dir: str, progress_cb=None) -> list:
    company = load_company()
    
    spec_template_id = config.get("template_id")
    spec_template = None
    if spec_template_id:
        import templates_store
        try:
            spec_template = templates_store.get_template(spec_template_id)
        except Exception as e:
            if progress_cb:
                progress_cb(f"Aviso: Template especificado '{spec_template_id}' não pôde ser carregado ({e}). Usando dinâmico.")
                
    templates = load_email_templates()
    min_score = config["scoring"]["min_score_to_send"] if not spec_template_id else 0

    result = []
    for i, lead in enumerate(leads):
        if lead.get("score", 0) < min_score:
            lead["email_status"] = "skip_score"
            result.append(lead)
            continue

        if progress_cb and (lead.get("email") or not lead.get("email_status")):
            progress_cb(f"Gerando email para {lead.get('nome_empresa', '?')} ({i+1}/{len(leads)})...")

        has_email = bool(lead.get("email"))
        if not has_email:
            lead["email_status"] = "draft_no_email"

        try:
            # 1. Escolhe e renderiza template
            if spec_template:
                template = spec_template
            else:
                template = _pick_template(lead, templates)
                
            draft = _render_template(template, lead, company)
            lead["email_template_id"] = template["id"]
            lead["email_template_label"] = template["label"]

            # 2. Personaliza com IA apenas se NÃO especificou um template fixo
            if spec_template:
                # Bypassa a IA de personalização (prospecção por template fixo sem custos e gargalo)
                lead["email_assunto"] = draft["assunto"]
                lead["email_corpo"] = draft["corpo"]
            else:
                final = _personalize_with_ai(draft, lead, company)
                lead["email_assunto"] = final.get("assunto", draft["assunto"])
                lead["email_corpo"] = final.get("corpo", draft["corpo"])

            if has_email and not lead.get("email_status") == "draft_no_email":
                lead["email_status"] = "ready"
            elif not has_email:
                lead["email_status"] = "draft_no_email"

        except Exception as e:
            lead["email_status"] = f"error: {e}"

        result.append(lead)

    return result

