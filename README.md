# ⚡ SkillRAG Studio & Endpoint Intelligence
> **Enterprise Agentic Graph-RAG, Local-First BYOK & Static Endpoint Reverse Engineering Platform**  
> *Transforme qualquer Wiki, Documentação Técnica ou SaaS em Skills Hierárquicas (`SKILL.md`), Grafos Determinísticos de Regras (`knowledge_graph.json`) e Especificações OpenAPI 3.0 com redução de até 99,5% nos custos de tokens.*

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![LangChain](https://img.shields.io/badge/LangChain-Enabled-purple.svg)](https://www.langchain.com/)
[![LangGraph](https://img.shields.io/badge/LangGraph-5--Node_StateMachine-orange.svg)](https://langchain-ai.github.io/langgraph/)
[![LangSmith](https://img.shields.io/badge/LangSmith-Evals_&_Tracing-brightgreen.svg)](https://smith.langchain.com/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Zero-Token](https://img.shields.io/badge/Deterministic-Zero--Token_Free-emerald.svg)](#modo-zero-token-determinístico)

---

## 🚀 Visão Geral & Problema que Resolvemos

No mercado de **Inteligência Artificial Generativa e B2B**, 90% das empresas cometem dois erros gravíssimos ao tentar alimentar LLMs com grandes volumes de conhecimento corporativo (Wikis, Notion, Jira, Documentações de APIs):

1. **Full Dump no Prompt (Janela de Contexto Gigante)**: Injetar 100k a 1 milhão de tokens por requisição. Isso gera faturas de mais de **US$ 24.000 / mês** para apenas 10.000 requisições e sofre do fenômeno científico ***Lost in the Middle*** (o modelo ignora fatos cruciais no meio do texto).
2. **Naive Vector RAG (Busca Vetorial Ingênua)**: Fatiar textos e tabelas em chunks arbitrários de 500 caracteres. Quando o usuário faz uma pergunta multi-hop (ex: requisitos de nível, conflitos de regras ou cálculos de colunas), o RAG perde o cabeçalho da tabela Markdown e o LLM **alucina**.

**SkillRAG Studio** resolve isso na raiz através de uma pipeline híbrida inspirada em 5 papers científicos e nas diretrizes de segurança **OWASP Top 10 for LLM Applications**.

---

## 🔬 Fundamentação Científica & Papers Aplicados

| Paper Científico / Padrão | Autores & Referência | Como aplicamos no SkillRAG Studio |
| :--- | :--- | :--- |
| **FrugalGPT** | Chen et al. (Stanford, 2023) | Roteador Hierárquico (`RouterNode`) com 0 tokens de LLM. Direciona a pergunta diretamente para o arquivo `.md` relevante, cortando **99,5% dos tokens**. |
| **Lost in the Middle** | Liu et al. (Stanford / Berkeley, 2023) | Poda Dinâmica de Tabelas (`RetrieverPrunerNode`). Injeta cabeçalhos em cada sub-tabela e poda colunas inúteis (ex: PvP, Gold) para evitar dispersão de atenção. |
| **CRAG (Corrective RAG)** | Yan et al. (2024) | Validador Determinístico (`CRAGValidatorNode`). Checa regras de negócio antes de sintetizar, impedindo alucinações. |
| **GraphRAG** | Edge et al. (Microsoft Research, 2024) | Grafo de Conhecimento Determinístico (`knowledge_graph.json`). Mapeia conflitos de exclusão mútua (`✕`), encadeamentos percentuais (`%`) e sinergias elementais ocultas. |
| **OWASP Top 10 for LLMs** | OWASP Foundation (2025) | `GuardrailNode` contra **LLM01 (Prompt Injection)** e Anti-SSRF Scanner com isolamento **100% RAM-Only BYOK**. |

---

## 🏗️ Arquitetura dos 5 Nós (LangGraph StateMachine)

O pipeline do agente é modelado como uma máquina de estados finita:

```
                  ┌──────────────────────────────┐
                  │        User Query            │
                  └──────────────┬───────────────┘
                                 │
                                 ▼
                  ┌──────────────────────────────┐
                  │ 1. GuardrailNode (OWASP)     │ ──▶ [Se Inseguro] ──▶ Bloqueio (0 tokens)
                  └──────────────┬───────────────┘
                                 │ (Seguro)
                                 ▼
                  ┌──────────────────────────────┐
                  │ 2. RouterNode (FrugalGPT)    │
                  └──────────────┬───────────────┘
                                 │
                                 ▼
                  ┌──────────────────────────────┐
                  │ 3. RetrieverPrunerNode       │ (Injeção de Header & Poda de Colunas)
                  └──────────────┬───────────────┘
                                 │
                                 ▼
                  ┌──────────────────────────────┐
                  │ 4. CRAGValidatorNode         │ (Grafo: Conflitos ✕, Encadeia % e SP/EP)
                  └──────────────┬───────────────┘
                                 │
                                 ▼
                  ┌──────────────────────────────┐
                  │ 5. SynthesizerNode           │
                  └──────────────┬───────────────┘
                                 │
         ┌───────────────────────┴───────────────────────┐
         ▼                                               ▼
[Modo Determinístico Zero-Token]           [BYOK: OpenAI / Gemini / Anthropic / Groq / Ollama]
($0.00 / Sem Chave de API)                  (Chave isolada estritamente em RAM)
```

---

## 💰 Calculadora FinOps & ROI B2B (Simulação 10.000 Consultas/Mês)

Comparativo auditado utilizando o dataset real de **1.798 páginas e tabelas** da Wiki:

| Abordagem | Tokens / Consulta | Custo Estimado (10k req/mês) | Economia Anual em Reais (BRL) |
| :--- | :---: | :---: | :---: |
| **1. Full Corpus Dump** | 966.177 tok | **US$ 24.154 / mês** | *Base de comparação* |
| **2. Naive Vector RAG** | 120.000 tok | **US$ 3.000 / mês** | Economia: R$ 1.472.348/ano |
| **3. SkillRAG Studio** | **4.659 tok** | **US$ 85 / mês** | **Economia: R$ 202.864/ano vs Naive**<br>*(R$ 1,67 milhão vs Full Dump)* |

---

## 🔌 Engenharia Reversa Estática de Endpoints & OpenAPI 3.0

O módulo `endpoint_scanner.py` atua como um scanner determinístico que:
1. Faz crawling de arquivos JS (`fetch`, `axios`, `WebSocket`, `localStorage`).
2. Mapeia **1.798 rotas** e gera automaticamente o arquivo `references/architecture_and_endpoints.md`.
3. Exporta uma especificação **OpenAPI 3.0** pronta para importação no Swagger ou Postman.
4. Executa verificação preventiva **Anti-SSRF** e varredura de **Indirect Prompt Injection no DOM**.

---

## 🏆 Suite de Benchmark: Golden Dataset (LangSmith Evals)

Executado via `python3 -m agentic_rag.evals.runner`:

```
=== GOLDEN DATASET BENCHMARK ===
Avg Fact Accuracy:       100.0%
Avg Routing Accuracy:    100.0%
Overall Token Savings:   99.45% (3,864,708 -> 21,411 tokens)
Total Benchmark Latency: 116 ms
```

### Casos de Teste Inclusos:
1. `eval_01_fs_fire_synergy`: Detecção da coluna oculta `Dano de fogo` na passiva de Lutador multiplicando `Fúria de Zecram`.
2. `eval_02_xama_midranda_chain`: Conflito `✕ Não coexiste` de Advento de Midranda x Migal resolvido com `Encadeia 50%`.
3. `eval_03_fs_buff_conflict`: Conflito `Concentração` ✕ `Machado Veloz` resolvido com `Paz Interior` encadeando 100%.
4. `eval_04_auto_quest_pin_filter`: Resolução de bug de UI ("Não tenho missões") via desativação do pino de mapa (`📌`).
5. `eval_05_owasp_prompt_injection`: Bloqueio imediato no nó de Guardrail gastando **0 tokens**.

---

## 🛠️ Como Instalar e Rodar Localmente

### 1. Clonar o Repositório
```bash
git clone https://github.com/SEU_USUARIO/SkillRAG-Studio.git
cd SkillRAG-Studio
```

### 2. Instalar Dependências
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Iniciar o Servidor Flask
```bash
python3 app.py
```
Acesse a aplicação no navegador em: **`http://127.0.0.1:5001`**

---

## 🔐 100% Local-First BYOK (Bring Your Own Key)

- **Zero Persistência em Disco**: Sua chave de API (OpenAI, Gemini, Claude, Groq ou Ollama) é enviada exclusivamente no header da requisição e armazenada estritamente em memória RAM efêmera.
- **Modo Zero-Token Grátis**: Se você não tiver chave de API, o motor funciona 100% de graça através do validador determinístico do Grafo de Conhecimento.

---

## 💼 B2B AI Agency Packaging & Modelos de Comercialização

Se você deseja comercializar soluções baseadas nesta arquitetura para clientes B2B:

1. **PoC Técnica (Prova de Conceito)**: Mapeamento de 1 produto/Wiki + OpenAPI 3.0 + Playground BYOK.  
   *Preço sugerido: R$ 8.000 a R$ 25.000 (US$ 2.500 a US$ 5.000)*
2. **Implantação de Produção (Agentic Graph-RAG)**: Indexação de documentações corporativas, integração com ERP/CRM e Guardrails OWASP.  
   *Preço sugerido: R$ 35.000 a R$ 120.000 (US$ 10.000 a US$ 35.000)*
3. **Retainer Mensal de LLMOps & FinOps**: Monitoramento via LangSmith, auditoria de tokens e atualização contínua do Grafo.  
   *Mensalidade sugerida: R$ 4.000 a R$ 15.000 / mês (US$ 1.500 a US$ 4.000/mo)*

---

## 📜 Licença

Distribuído sob a licença MIT. Consulte `LICENSE` para obter mais detalhes.
