"""
Estrategista Autônomo de Segmentação & Geração Proativa de Rotinas do BigBoss OS.
Cobre todo o ecossistema de serviços da agência (Varejo, Atacado, Clínicas, Escritórios, Experts, Vídeo e Tráfego).
"""
import logging
from typing import Dict, Any, List
from datetime import datetime

from tasks_store import list_tasks, create_task
from templates_store import get_templates

logger = logging.getLogger(__name__)

# Catálogo completo de nichos e serviços ordenados por prioridade
HIGH_TICKET_NICHES_KB = [
    {
        "category": "Varejo Físico & Local",
        "name_template": "🎯 Auto-Pilot: Varejo & Lojas Físicas ({city})",
        "cities": ["São Paulo SP", "Campinas SP", "Curitiba PR", "Belo Horizonte MG", "Ribeirão Preto SP"],
        "queries": ["loja de moveis decoracao {city}", "loja de roupas calcados boutique {city}", "loja materiais construcao acabamentos {city}"],
        "default_template": "posicionamento_local",
        "description": "Foco em criar e-commerce, presença no Instagram e atrair clientes da cidade com tráfego pago."
    },
    {
        "category": "Atacado & Distribuidoras",
        "name_template": "🎯 Auto-Pilot: Atacado & Distribuidoras ({city})",
        "cities": ["São Paulo SP", "Brusque SC", "Maringá PR", "Goiânia GO", "Franca SP", "Brasil"],
        "queries": ["distribuidora atacado {city}", "confeccao atacado moda {city}", "fabrica distribuidora b2b {city}"],
        "default_template": "diagnostico_conversao",
        "description": "Foco em estruturar departamento de marketing digital para vendas B2B e captação de revendedores."
    },
    {
        "category": "Clínicas de Estética",
        "name_template": "🎯 Auto-Pilot: Clínicas de Estética ({city})",
        "cities": ["São Paulo SP", "Curitiba PR", "Belo Horizonte MG", "Campinas SP", "Rio de Janeiro RJ"],
        "queries": ["clinica estetica avancada harmonizacao {city}", "dermatologia estetica {city}"],
        "default_template": "posicionamento_local",
        "description": "Foco em Top 3 no Google Maps, Meta Ads e agendamentos diretos no WhatsApp."
    },
    {
        "category": "Odontologia",
        "name_template": "🎯 Auto-Pilot: Clínicas Odontológicas ({city})",
        "cities": ["São Paulo SP", "Curitiba PR", "Santo André SP", "Santos SP", "Belo Horizonte MG"],
        "queries": ["clinica odontologia implantes {city}", "consultorio odontologico invisalign {city}"],
        "default_template": "posicionamento_local",
        "description": "Foco em atração de pacientes particulares de alto ticket para implantes e alinhadores."
    },
    {
        "category": "Clínicas Médicas & Saúde",
        "name_template": "🎯 Auto-Pilot: Clínicas Médicas & Saúde ({city})",
        "cities": ["São Paulo SP", "Curitiba PR", "Campinas SP", "Brasília DF", "Porto Alegre RS"],
        "queries": ["clinica medica integrada {city}", "clinica ortopedia fisioterapia {city}", "centro medico especialidades {city}"],
        "default_template": "posicionamento_local",
        "description": "Foco em presença no Google, Google Meu Negócio e captação de consultas."
    },
    {
        "category": "Arquitetura & Interiores",
        "name_template": "🎯 Auto-Pilot: Arquitetura & Design ({city})",
        "cities": ["São Paulo SP", "Curitiba PR", "Belo Horizonte MG", "Balneário Camboriú SC", "Florianópolis SC"],
        "queries": ["escritorio arquitetura interiores {city}", "arquiteto alto padrao {city}"],
        "default_template": "diagnostico_conversao",
        "description": "Foco em portfólio de alta conversão, autoridade no Instagram e captação de projetos de alto padrão."
    },
    {
        "category": "Advocacia & Jurídico",
        "name_template": "🎯 Auto-Pilot: Advocacia Especializada ({city})",
        "cities": ["São Paulo SP", "Curitiba PR", "Brasília DF", "Belo Horizonte MG", "Rio de Janeiro RJ"],
        "queries": ["advocacia empresarial {city}", "escritorio advocacia tributaria {city}", "advocacia imobiliaria {city}"],
        "default_template": "diagnostico_conversao",
        "description": "Foco em autoridade digital, Google Ads institucional e captação de clientes qualificados."
    },
    {
        "category": "Contabilidade & BPO",
        "name_template": "🎯 Auto-Pilot: Escritórios de Contabilidade ({city})",
        "cities": ["São Paulo SP", "Curitiba PR", "Campinas SP", "Belo Horizonte MG", "Joinville SC"],
        "queries": ["escritorio contabilidade consultiva {city}", "bpo financeiro gestao {city}"],
        "default_template": "diagnostico_conversao",
        "description": "Foco em aquisição de clientes PJ e posicionamento consultivo."
    },
    {
        "category": "Experts & Infoprodutos",
        "name_template": "🎯 Auto-Pilot: Infoprodutores & Mentores ({city})",
        "cities": ["Brasil", "São Paulo SP", "Curitiba PR", "Florianópolis SC", "Belo Horizonte MG"],
        "queries": ["mentor curso online infoproduto {city}", "consultor estrategista lancamento {city}"],
        "default_template": "diagnostico_conversao",
        "description": "Foco em Landing Pages de alta conversão, VSLs, funil de vendas e tráfego direto."
    },
    {
        "category": "Edição de Vídeo & Criativos",
        "name_template": "🎯 Auto-Pilot: Produção de Vídeos & Criativos ({city})",
        "cities": ["Brasil", "São Paulo SP", "Rio de Janeiro RJ", "Curitiba PR"],
        "queries": ["empresa canal youtube reels criadores {city}", "podcast estudio gravacao {city}"],
        "default_template": "diagnostico_conversao",
        "description": "Foco em edição profissional para Reels, TikToks, Shorts e Criativos de Anúncios de alta performance."
    },
    {
        "category": "Tráfego Pago & Performance",
        "name_template": "🎯 Auto-Pilot: Gestão de Tráfego Pago ({city})",
        "cities": ["São Paulo SP", "Campinas SP", "Curitiba PR", "Goiânia GO", "Salvador BA"],
        "queries": ["empresa servicos comerciais {city}", "industria fabricante {city}"],
        "default_template": "diagnostico_conversao",
        "description": "Foco em empresas que precisam gerar leads qualificados todos os dias pelo Google e Meta Ads."
    }
]


def auto_discover_and_create_routines(max_create: int = 1) -> List[Dict[str, Any]]:
    """Identifica nichos não prospectados e cria rotinas inteligentes prontas para rodar."""
    existing_tasks = list_tasks()
    
    existing_prompts = set()
    existing_names = set()
    for t in existing_tasks:
        p = (t.get("prompt") or "").strip().lower()
        if p:
            existing_prompts.add(p)
        name = (t.get("name") or "").strip().lower()
        if name:
            existing_names.add(name)

    templates = get_templates(active_only=True)
    template_ids = [t["id"] for t in templates]

    created = []

    for niche_info in HIGH_TICKET_NICHES_KB:
        if len(created) >= max_create:
            break

        niche_already_active = any(niche_info["category"].lower() in n for n in existing_names)
        if niche_already_active:
            continue

        for city in niche_info["cities"]:
            if len(created) >= max_create:
                break

            task_name = niche_info["name_template"].format(city=city)
            if task_name.lower() in existing_names:
                continue

            for q_tpl in niche_info["queries"]:
                query = q_tpl.format(city=city).strip()
                if query.lower() in existing_prompts:
                    continue

                preferred_template = niche_info["default_template"]
                if preferred_template not in template_ids and template_ids:
                    preferred_template = template_ids[0]

                commercial_times = ["09:30", "11:00", "14:30", "16:00"]
                slot_time = commercial_times[(len(existing_tasks) + len(created)) % len(commercial_times)]
                today_str = datetime.now().strftime("%Y-%m-%d")

                payload = {
                    "name": task_name,
                    "prompt": query,
                    "task_type": "full_cycle",
                    "source": "google",
                    "status": "active",
                    "interval_minutes": 1440,
                    "schedule_time": slot_time,
                    "schedule_days": "weekdays",
                    "start_date": today_str,
                    "end_date": None,
                    "auto_send": False,
                    "max_leads_per_run": 15,
                    "min_score_to_send": 6,
                    "results_per_query": 10,
                    "template_id": preferred_template,
                }

                try:
                    new_task = create_task(payload)
                    created.append(new_task)
                    existing_prompts.add(query.lower())
                    existing_names.add(task_name.lower())
                    logger.info(f"✨ Nova rotina de nicho da agência criada: {task_name}")
                except Exception as exc:
                    logger.error(f"Erro ao criar rotina autônoma: {exc}")

                break
            break

    return created
