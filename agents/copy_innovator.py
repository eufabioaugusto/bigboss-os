"""
Motor de Inovação Contínua de Copy & Templates.
Gera periodicamente novas abordagens de alta conversão ('Cavalo de Tróia')
com base nos serviços e nos segmentos de mercado mais lucrativos.
"""
import json
import logging
from typing import Dict, Any, List, Optional
from datetime import datetime

from agents.ai import ask_json
from templates_store import get_templates, create_template
from settings_store import get_company

logger = logging.getLogger(__name__)

SERVICES_KNOWLEDGE = [
    {
        "service": "Otimização de Conversão (CRO) & E-commerce",
        "target_niche": "Lojas Virtuais, Shopify, Moda, Cosméticos, Varejo Online",
        "angle": "Gargalo de checkout mobile, perda de carrinhos, velocidade de página e Pixel não configurado.",
        "offer": "Gravação de vídeo de 2 min com análise visual do checkout + checklist de 3 correções de conversão."
    },
    {
        "service": "Posicionamento Local no Google Maps & GMN",
        "target_niche": "Clínicas Odontológicas, Médicos, Estética, Escritórios de Advocacia, Negócios Locais",
        "angle": "Empresa bem avaliada mas que perde clientes nas buscas locais do Google para concorrentes inferiores.",
        "offer": "Checklist de 2 correções de tags e SEO local para colocar a empresa no Top 3 do Google Maps da cidade."
    },
    {
        "service": "Gestão de Tráfego Pago (Google Ads & Meta Ads 360°)",
        "target_niche": "Empresas B2B, Consultorias, Energia Solar, Indústrias, Serviços Especializados",
        "angle": "Ausência de captação ativa por palavras-chave com alta intenção de compra no Google.",
        "offer": "Mapeamento das 5 principais palavras-chave buscadas na região que a concorrência está comprando."
    },
    {
        "service": "Redesign de Sites & Landing Pages de Alta Performance",
        "target_niche": "Empresas consolidadas com site antigo ou sem adaptação mobile",
        "angle": "Site institucional que passa imagem desatualizada ou lenta, afastando clientes de alto ticket.",
        "offer": "Diagnóstico comparativo de velocidade e percepção de autoridade frente aos líderes de mercado."
    },
    {
        "service": "Automação de Atendimento & Funil WhatsApp",
        "target_niche": "Empresas com alto volume de atendimento comercial (imobiliárias, clínicas, cursos)",
        "angle": "Demora no primeiro contato e perda de leads qualificados gerados no Instagram/Google.",
        "offer": "Estrutura de fluxo de qualificação e agendamento instantâneo no WhatsApp."
    }
]


def generate_innovative_template(service_focus: Optional[str] = None) -> Dict[str, Any]:
    """Gera uma nova abordagem inteligente de e-mail 'Cavalo de Tróia' usando IA."""
    company = get_company()
    co_name = company.get("name") or "BigBoss OS"
    co_desc = company.get("description") or "empresa de tecnologia e prospecção de negócios"
    co_sender = company.get("contact_name") or company.get("name") or "Fabio"
    current_templates = get_templates()
    existing_labels = [t.get("label") for t in current_templates]

    prompt = f"""Você é o copywriter principal e estrategista de vendas B2B da {co_name}.
{co_name} atua com {co_desc}.

SEU OBJETIVO:
Criar um NOVO modelo de e-mail frio de prospecção no formato "Cavalo de Tróia" (Oferta de Diagnóstico / Lead Magnet de alto valor e baixíssimo atrito).

REGRAS OBRIGATÓRIAS:
1. O e-mail NÃO PODE tentar vender serviço ou agendar reunião de primeira.
2. O gancho deve apontar um gargalo técnico ou oportunidade real da empresa do lead.
3. A oferta deve ser um diagnóstico rápido, checklist objetivo de 3 pontos ou vídeo curto de 2 min.
4. O Call-to-Action (CTA) deve ser suave ("Faz sentido eu te enviar o link por aqui?", "Posso te mandar o checklist por aqui?").
5. O corpo deve ter no MÁXIMO 90 a 110 palavras.
6. Use as variáveis de interpolação: {{empresa}}, {{segmento}}, {{cidade}}, {{sinal_principal}}.
7. Templates já existentes (NÃO REPITA): {json.dumps(existing_labels, ensure_ascii=False)}

RETORNE APENAS UM OBJETO JSON COM A SEGUINTE ESTRUTURA:
{{
  "id": "slug_curto_sem_espacos",
  "label": "Cavalo de Tróia: [Nome Atraente da Abordagem]",
  "description": "Explicação de quando usar e para qual tipo de empresa",
  "assunto": "{{empresa}} — [assunto curioso e sem spam]",
  "corpo": "Olá, {{empresa}}!\\n\\n[Corpo persuasivo com variáveis]\\n\\n[CTA suave e respeitoso]\\n\\nAtt,\\n{co_sender} | {co_name}",
  "tags": ["tag1", "tag2", "tag3"],
  "angle_targets": ["angulo1", "angulo2"]
}}"""

    system_prompt = "Você é o melhor estrategista de Outbound B2B do Brasil. Crie copies diretas, elegantes e de altíssima conversão. Retorne exclusivamente JSON."
    
    result = ask_json(prompt, system_prompt)
    if not result or not result.get("label") or not result.get("corpo"):
        raise ValueError("Falha ao gerar novo template com IA.")

    # Salva no banco de dados
    saved = create_template(result)
    logger.info(f"✨ Novo template autônomo gerado e salvo: {saved.get('label')}")
    return saved


def auto_innovate_templates_cycle(max_new: int = 1) -> List[Dict[str, Any]]:
    """Ciclo autônomo periódico que garante o arsenal de templates sempre atualizado."""
    created = []
    for _ in range(max_new):
        try:
            tpl = generate_innovative_template()
            created.append(tpl)
        except Exception as exc:
            logger.error(f"Erro ao gerar template autônomo: {exc}")
    return created
