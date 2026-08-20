# BigBoss OS — Outbound & Prospecção B2B (v3.0)

O **BigBoss OS** é um sistema completo e autônomo de prospecção comercial outbound. Ele foi desenhado para atuar de ponta a ponta na geração e conversão de novos clientes B2B através de múltiplos canais (Email, WhatsApp, Instagram e TikTok DMs) integrados a um CRM local e a um motor inteligente de IA.

---

## 📣 Arquitetura Orientada a Campanhas (Meta Ads Style)

O sistema segue a lógica estrutural de grandes plataformas de anúncios digitais para segmentar a prospecção e otimizar copies sem poluir as configurações globais:

1. **Campanhas (Oferta / Produto)**: Define o produto ofertado, preço original, preço promocional, escassez (limite de vagas) e tom de voz específico da copy.
2. **Rotinas (Público / Scraper)**: Define quem prospectar (Google Maps, Instagram, TikTok ou TikTok Shop), a frequência de busca, limite de leads por rodada e qual campanha está vinculada a essa rotina.
3. **Abordagem / Criativos (Copies Dinâmicos)**: O motor de IA gera o e-mail personalizado e os scripts de DMs de forma totalmente dinâmica, herdando os dados da campanha vinculada para injetar argumentos de preço e urgência na copy.

---

## 🚀 Principais Funcionalidades

- **Scrapers Integrados**: Descoberta em tempo real através do Google Meu Negócio, Instagram e TikTok (incluindo identificação de lojas elegíveis para o TikTok Shop).
- **Enriquecimento Inteligente**: Validação automática de sites, e-mails corporativos, perfis sociais e telefones reais.
- **Redação com IA e Contexto de Campanha**: E-mails frios e DMs escritos de forma personalizada mencionando gargalos específicos do lead e apresentando a oferta promocional ativa.
- **CRM Completo Local (Kanban & Fichas)**:
  - **Visualização Kanban**: Leads separados em colunas visuais (*Novos Leads, Fila de Envio, Enviados, Responderam, Reunião*).
  - **Ficha do Lead (Drawer)**: Dados de contato detalhados, tecnologias utilizadas no site, histórico de interações, anotações e disparos rápidos.
- **Caixa de Entrada Unificada (Inbox)**: Receba as respostas de e-mail dos leads e responda de forma integrada, mantendo o histórico na mesma conversa.
- **Fila Diária de WhatsApp**: Sugestão automática de abordagem de 1 clique no WhatsApp para leads que receberam e-mail no dia, aumentando em até 3x a taxa de resposta.
- **Cadência de Follow-ups**: Agendamento automático e inteligente de e-mails de acompanhamento de acordo com a campanha ativa do lead.
- **White-Label**: Sistema 100% livre de marcas fixas ou códigos hardcoded, adaptando-se completamente ao nome e descrição da agência do usuário configurados na tela de configurações/onboarding.

---

## ⚙️ Setup & Instalação

### 1. Clonar o projeto e instalar dependências
```bash
git clone https://github.com/eufabioaugusto/bigboss-os.git
cd bigboss-os
pip install -r requirements.txt
```

### 2. Configurar APIs (.env)
Crie um arquivo `.env` na raiz do projeto com as suas credenciais:
```env
# Conexão com o motor de IA (Codex, Claude ou OpenAI)
AI_API_KEY=sua_chave_aqui

# Provider de envio de e-mails (Resend)
RESEND_API_KEY=sua_chave_aqui
PLATFORM_FROM_EMAIL=contato@seu-dominio.com.br
```

### 3. Instalar o Playwright (necessário para enriquecimento)
```bash
playwright install chromium
```

---

## 🛠️ Como Executar

### Usando o atalho rápido (macOS)
Basta dar duplo clique ou executar o arquivo de inicialização pelo terminal:
```bash
chmod +x start.command
./start.command
```
O script iniciará o servidor backend e abrirá o aplicativo no Chrome (em modo app).

### Executando manualmente
```bash
python3 server.py
```
Acesse o painel local pelo navegador em: [http://localhost:7860](http://localhost:7860)

---

## 📁 Estrutura de Pastas

```text
bigboss-os/
├── server.py              # Backend FastAPI, Endpoints e WebSocket
├── crm.py                 # Banco de Dados SQLite do CRM e Migrações
├── tasks_store.py         # Persistência de Rotinas e Agendamento
├── templates_store.py     # Controle de Templates de E-mail
├── ui.html                # Single-Page Application (Frontend SPA)
├── start.command          # Launcher rápido para macOS
├── company.yaml           # Configurações iniciais de marca/onboarding
├── templates/
│   └── email_templates.yaml   # Templates B2B "Cavalo de Tróia" base
├── agents/
│   ├── researcher.py      # Agente de busca e scraping
│   ├── copywriter.py      # Agente de copy dinâmico por campanha
│   ├── copy_innovator.py  # Agente autônomo de otimização de copy
│   ├── whatsapp_queue.py  # Gerador de fila de WhatsApp e alertas
│   └── strategist.py      # Sugestões autônomas de novos nichos
└── data/
    └── crm.db             # Banco de dados SQLite persistente
```
