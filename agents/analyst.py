"""
Diagnostica leads com lógica adaptada por fonte (source).
"""
import json
from agents.ai import ask_json

_SYSTEM_BASE = """Você é consultor de vendas de serviços digitais analisando leads para prospecção outbound.

CONTEXTO: o objetivo é vender estrutura digital (site, marketing, redes sociais, identidade, tráfego) para empresas.
Por isso, o score mede OPORTUNIDADE DE VENDA — quanto mais fraca a presença digital, maior a oportunidade.

REGRA: só afirme o que está nos dados fornecidos. Não invente.
Retorne JSON com:
- sinais: list[string] — lacunas digitais identificadas que representam oportunidade (máx 3, direto ao ponto)
- angulo: string — ângulo de abordagem comercial
- score: int 0-10 — oportunidade de venda (10 = máxima oportunidade)
- prioridade: string — "alta" (8-10) | "média" (5-7) | "baixa" (0-4)
- justificativa: string — 1 frase explicando por que é uma boa oportunidade (ou não)
- dm_instagram: string — mensagem curta para Instagram Direct (máx 3 linhas, tom direto e consultivo, menciona algo específico da empresa, não soa como spam)"""

_SYSTEMS = {
    "google": _SYSTEM_BASE + """

Ângulos: sem_site | site_fraco | sem_instagram | identidade_fraca | comunicacao_generica | sem_funil | baixa_autoridade | default

Score de oportunidade (Google):
+4 sem site próprio — máxima oportunidade
+3 site existe mas é fraco (antigo, sem SEO, sem conversão)
+2 sem presença no Instagram ou redes abandonadas
+2 segmento com alto potencial de crescimento digital
+1 empresa ativa mas sem estratégia digital clara
-2 já tem equipe de marketing estruturada
-3 grande rede nacional com agência própria""",

    "instagram": _SYSTEM_BASE + """

Ângulos: sem_site | instagram_fraco | bio_sem_contato | sem_funil | identidade_fraca | pouca_atividade | default

Score de oportunidade (Instagram):
+4 sem site próprio — o Instagram é o único canal deles
+3 bio sem email, telefone ou link — não conseguem converter seguidores
+2 poucos posts ou atividade irregular — precisam de gestão de conteúdo
+2 sem categoria de negócio definida — identidade fraca
+1 seguidores entre 500-10k — base real mas sem estrutura para crescer
-2 já tem site e funil bem estruturado
-3 grande marca com presença digital consolidada

IMPORTANTE: uma loja sem site e com Instagram fraco é um lead de ALTA oportunidade (score 8-9), não baixo.""",

    "tiktok": _SYSTEM_BASE + """

Ângulos disponíveis: sem_tiktok_shop | baixo_engajamento_tt | sem_consistencia_videos | oportunidade_viral | sem_funil_tiktok | default

Score TikTok:
+3 tem produto/loja mas sem TikTok Shop
+2 vídeos regulares (negócio ativo no TikTok)
+2 seguidores entre 500-50k
+2 tem website ou Instagram também (negócio estruturado)
+1 bio com contato ou website
-1 apenas entretenimento, sem produto
-2 conta inativa (poucos vídeos)""",

    "tiktok_shop": _SYSTEM_BASE + """

Contexto: esse lead é uma loja no Instagram que ainda NÃO está no TikTok Shop.
Foco: oportunidade de entrar no TikTok Shop e monetizar uma audiência que já tem.

Ângulos disponíveis: sem_tiktok_shop | tiktok_sem_shop | oportunidade_ecommerce | loja_sem_presenca_tt | default

Score TikTok Shop:
+4 loja no Instagram sem TikTok nenhum (oportunidade máxima)
+3 tem TikTok mas sem Shop (já está lá, falta o passo final)
+2 seguidores Instagram 1k-50k (base de clientes real)
+2 tem sinais claros de loja (frete, produtos, catálogo)
+2 tem email ou telefone (contato disponível)
+1 site próprio (negócio estruturado)
-1 sem sinais de venda reais
-2 loja muito grande (já tem equipe)""",
}


def diagnose_leads(leads: list[dict], config: dict, progress_cb=None) -> list[dict]:
    diagnosed = []

    for i, lead in enumerate(leads):
        if progress_cb:
            progress_cb(f"Diagnosticando {lead.get('nome_empresa', '?')} ({i+1}/{len(leads)})...")

        source = lead.get("source", config.get("source", "google"))
        system = _SYSTEMS.get(source, _SYSTEMS["google"])

        # Resumo adaptado por source
        summary: dict = {
            "nome_empresa": lead.get("nome_empresa"),
            "segmento": lead.get("segmento"),
            "cidade": lead.get("cidade"),
            "source": source,
        }

        if source == "google":
            summary.update({
                "site_existe": lead.get("site_existe", False),
                "website": lead.get("website_verificado") or lead.get("website"),
                "email": lead.get("email_verificado") or lead.get("email"),
                "instagram": lead.get("instagram_verificado") or lead.get("instagram"),
                "tem_google_meu_negocio": lead.get("tem_google_meu_negocio", False),
                "tecnologias": lead.get("tecnologias", []),
                "gargalos_tecnicos": lead.get("gargalos_tecnicos", []),
                "observacoes": lead.get("observacoes"),
            })
        elif source == "instagram":
            summary.update({
                "ig_followers": lead.get("ig_followers"),
                "ig_posts": lead.get("ig_posts"),
                "ig_bio": (lead.get("ig_bio") or "")[:300],
                "ig_category": lead.get("ig_category"),
                "ig_website": lead.get("ig_website"),
                "ig_email": lead.get("ig_email"),
                "ig_store_signals": lead.get("ig_store_signals", []),
                "tem_site": bool(lead.get("website") or lead.get("ig_website")),
            })
        elif source == "tiktok":
            summary.update({
                "tt_followers": lead.get("tt_followers"),
                "tt_video_count": lead.get("tt_video_count"),
                "tt_likes": lead.get("tt_likes"),
                "tt_bio": (lead.get("tt_bio") or "")[:300],
                "tt_website": lead.get("tt_website"),
                "tt_has_shop": lead.get("tt_has_shop", False),
                "tem_site": bool(lead.get("website") or lead.get("tt_website")),
            })
        elif source == "tiktok_shop":
            summary.update({
                "ig_followers": lead.get("ig_followers"),
                "ig_bio": (lead.get("ig_bio") or "")[:300],
                "ig_store_signals": lead.get("ig_store_signals", []),
                "ig_parece_loja": lead.get("ig_parece_loja", False),
                "tt_status": lead.get("tt_status", "not_found"),
                "tt_has_shop": lead.get("tt_has_shop", False),
                "tem_site": bool(lead.get("website")),
                "tem_contato": bool(lead.get("email") or lead.get("telefone")),
            })

        try:
            diagnosis = ask_json(
                f"Dados do lead:\n{json.dumps(summary, ensure_ascii=False)}\n\n"
                f"Retorne o diagnóstico JSON completo incluindo dm_instagram.",
                system,
            )
        except Exception as e:
            diagnosis = {
                "sinais": ["dados insuficientes para diagnóstico completo"],
                "angulo": "default",
                "score": 5,
                "prioridade": "média",
                "justificativa": str(e),
            }

        diagnosed.append({**lead, **diagnosis})

    diagnosed.sort(key=lambda x: x.get("score", 0), reverse=True)
    return diagnosed
