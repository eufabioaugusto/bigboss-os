# Motor de Prospecção por Email

MVP local de prospecção outbound orientado por prompt. Já existe uma base boa aqui: pesquisa, enriquecimento, diagnóstico, geração de copy, envio e CRM local.

## Arquitetura atual dos agentes

```text
Prompt
  ↓
Researcher  -> encontra empresas candidatas na web
  ↓
Enricher    -> valida site, email, Instagram e telefone
  ↓
Analyst     -> pontua, prioriza e escolhe o ângulo
  ↓
Copywriter  -> gera email personalizado por lead
  ↓
Sender      -> envia com limite diário e logs
  ↓
CRM local   -> evita recontato e guarda histórico
```

## Melhorias já aplicadas nesta iteração

- CLI, UI e API agora seguem o mesmo fluxo de qualificação, com enriquecimento antes do diagnóstico.
- Leads duplicados dentro da rodada são removidos antes de consumir esforço da IA.
- Leads já contatados em rodadas anteriores são filtrados via CRM local.
- Envio passa a priorizar provider gerenciado pela plataforma antes de qualquer fallback técnico.
- Limite diário de emails passa a ser respeitado no lote de envio.
- Pastas de dados são criadas automaticamente na primeira execução.

## Setup

```bash
cd email-prospection
pip install -r requirements.txt
cp .env.example .env
# preencha .env e config.yaml
```

**Provider gerenciado pela plataforma**: configure `RESEND_API_KEY` e `PLATFORM_FROM_EMAIL` no backend.

**Playwright**: necessário para enriquecimento real de sites.

```bash
playwright install chromium
```

## Uso

```bash
# Painel web local (recomendado)
python prospection.py --ui

# CLI — só pesquisa e gera emails
python prospection.py --prompt "Prospectar contabilidades em Curitiba com baixa presença digital"

# CLI — pesquisa, gera e envia (modo manual por padrão)
python prospection.py --prompt "..." --send
```

## Fluxo

```
Prompt → pesquisa web → enriquecimento real → diagnóstico → score → email personalizado → revisão → envio
```

## Modos de Envio

| Modo | Comportamento |
|------|---------------|
| `manual` | Mostra cada email, pede confirmação um a um |
| `assisted` | Mostra resumo, pede confirmação única para o lote |
| `auto` | Envia sem confirmação (cuidado) |

Configure em `config.yaml` → `mode`.

## Estrutura

```
email-prospection/
├── prospection.py          # entrypoint CLI + UI
├── config.yaml             # configuração
├── requirements.txt
├── .env                    # API keys (não commitar)
├── agents/
│   ├── researcher.py       # pesquisa leads
│   ├── analyst.py          # diagnóstico + score
│   ├── copywriter.py       # gera e revisa emails
│   └── sender.py           # envia e loga
├── templates/
│   └── angles.yaml         # ângulos de abordagem por diagnóstico
└── data/
    ├── leads/              # CSVs por rodada
    ├── emails/             # JSONs com emails gerados
    └── logs/               # logs de envio
```

## Próximas melhorias recomendadas

- Adicionar agente `qualifier` com ICP explícito por segmento, ticket e região.
- Separar `Researcher` em descoberta e `Enricher` em validação concorrente.
- Criar fila de follow-up com cadência de 3 a 5 tentativas por lead.
- Registrar motivo de descarte para aprender quais prompts trazem melhores leads.
- Adicionar aprovação humana por checklist antes do envio automático.
- Integrar fontes melhores de dados, como Google Maps, Apollo, Hunter ou LinkedIn.

## Fora do escopo desta fase

- CRM / banco de dados relacional
- Follow-up automático
- Instagram Direct
- Multiagente coordenado
- Painel com histórico visual
- Integração com LinkedIn
