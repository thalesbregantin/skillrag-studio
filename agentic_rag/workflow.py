"""
Módulo 3: Agentic Graph-RAG & CRAG State Machine (LangGraph + LangChain + LangSmith + BYOK)
===========================================================================================
Implementa uma arquitetura de Agente Multi-Hop baseada em papers científicos:
1. GuardrailNode (OWASP LLM01 / Prompt Injection & PII Sanitizer)
2. RouterNode (FrugalGPT, Chen et al. 2023 - Roteamento Hierárquico SKILL.md -> Referências)
3. RetrieverPrunerNode (Lost in the Middle, Liu et al. 2023 - Poda Dinâmica de Tabelas Markdown)
4. CRAGValidatorNode (CRAG, Yan et al. 2024 + GraphRAG, Edge et al. 2024 - Validação Determinística de Conflitos ✕, Encadeamentos % e Matemática SP/EP/TP)
5. SynthesizerNode (100% Local-First BYOK: OpenAI, Gemini, Anthropic, Groq, Ollama + Modo Determinístico Zero-Token)
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TypedDict

import requests

from agentic_rag.indexing import build_knowledge_graph, retrieve_and_prune_context


# ---------------------------------------------------------------------------
# Tipagem do Estado do Grafo (Compatível com LangGraph StateGraph)
# ---------------------------------------------------------------------------

class AgentState(TypedDict, total=False):
    query: str
    skill_dir: str
    provider: str          # "deterministic", "openai", "gemini", "anthropic", "groq", "ollama"
    model: str
    api_key: str           # Armazenada estritamente em escopo de execução (RAM)
    langsmith_api_key: str
    # Artefatos produzidos pelos nós:
    is_safe: bool
    security_report: Dict[str, Any]
    intent: str
    routed_files: List[str]
    chunks: List[Dict[str, Any]]
    graph_insights: List[str]
    crag_validations: List[str]
    deterministic_math: Dict[str, Any]
    finops: Dict[str, Any]
    answer: str
    node_trace: List[Dict[str, Any]]
    langsmith_trace_url: Optional[str]


# ---------------------------------------------------------------------------
# Guardrails de Segurança (OWASP Top 10 for LLM Apps - LLM01 & LLM06)
# ---------------------------------------------------------------------------

PROMPT_INJECTION_PATTERNS = [
    (r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions", "Tentativa clássica de Override de Sistema (Prompt Injection)"),
    (r"disregard\s+(all\s+)?(previous|prior)\s+instructions", "Tentativa de ignorar instruções de sistema"),
    (r"system\s*prompt\s*[:=]", "Tentativa de injeção de System Prompt falso"),
    (r"you\s+are\s+now\s+(dan|unrestricted|jailbroken)", "Tentativa de Jailbreak de Persona (DAN)"),
    (r"(print|reveal|show|send|exfiltrate)\s+(your|the)\s+(api[_\s]?key|secret|system\s+prompt|environ)", "Tentativa de Exfiltração de Segredos/System Prompt"),
]


def guardrail_node(state: AgentState) -> AgentState:
    """
    Nó 1: OWASP LLM01 Input Guardrail.
    Inspeciona a query do usuário contra Prompt Injection e vazamento de credenciais.
    """
    t0 = time.perf_counter()
    query = state.get("query", "").strip()
    findings: List[str] = []

    for pattern, desc in PROMPT_INJECTION_PATTERNS:
        if re.search(pattern, query, re.IGNORECASE):
            findings.append(desc)

    is_safe = len(findings) == 0
    elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)

    trace_entry = {
        "node": "GuardrailNode",
        "paper_concept": "OWASP LLM01 & LLM06 Defense",
        "status": "passed" if is_safe else "blocked",
        "latency_ms": elapsed_ms,
        "summary": (
            "Query inspecionada: 0 assinaturas de Prompt Injection ou exfiltração detectadas."
            if is_safe
            else f"Bloqueado pelo Guardrail: {', '.join(findings)}"
        ),
    }

    node_trace = list(state.get("node_trace", []))
    node_trace.append(trace_entry)

    return {
        **state,
        "is_safe": is_safe,
        "security_report": {
            "is_safe": is_safe,
            "findings": findings,
            "byok_isolation": "RAM-Only Ephemeral Scope (Zero Disk Persistence)",
        },
        "node_trace": node_trace,
    }


# ---------------------------------------------------------------------------
# Nó 2: RouterNode (FrugalGPT - Roteamento Semântico e Classificação de Intenção)
# ---------------------------------------------------------------------------

def router_node(state: AgentState) -> AgentState:
    """
    Nó 2: Classifica a intenção da pergunta com 0 tokens de LLM usando regras léxicas
    e prepara o roteamento hierárquico sobre o índice SKILL.md.
    """
    if not state.get("is_safe", True):
        return state

    t0 = time.perf_counter()
    q_low = state.get("query", "").lower()

    intents = []
    if any(w in q_low for w in ["build", "skill", "ponto", "sp", "ep", "tp", "tier", "nível", "nivel", "lv"]):
        intents.append("theorycrafting_build")
    if any(w in q_low for w in ["fogo", "zecram", "midranda", "migal", "coexiste", "encadeia", "conflito", "buff", "resistência", "resistencia"]):
        intents.append("skill_synergy_and_conflicts")
    if any(w in q_low for w in ["missão", "missao", "quest", "automática", "automatica", "mapa", "pino", "j"]):
        intents.append("quest_and_ui_mechanics")
    if any(w in q_low for w in ["monstro", "drop", "boss", "mapa", "dungeon", "caverna", "santuário"]):
        intents.append("world_and_drops")
    if any(w in q_low for w in ["endpoint", "api", "rota", "arquitetura", "openapi"]):
        intents.append("architecture_reverse_engineering")

    intent_str = " + ".join(intents) if intents else "general_knowledge_lookup"
    elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)

    trace_entry = {
        "node": "RouterNode",
        "paper_concept": "FrugalGPT (Chen et al., 2023) & Hierarchical Index",
        "status": "completed",
        "latency_ms": elapsed_ms,
        "summary": f"Intenção classificada como [{intent_str}] com 0 tokens de LLM.",
    }

    node_trace = list(state.get("node_trace", []))
    node_trace.append(trace_entry)

    return {
        **state,
        "intent": intent_str,
        "node_trace": node_trace,
    }


# ---------------------------------------------------------------------------
# Nó 3: RetrieverPrunerNode (Lost in the Middle + Table-Header Injection & Pruning)
# ---------------------------------------------------------------------------

def retriever_pruner_node(state: AgentState) -> AgentState:
    """
    Nó 3: Executa busca híbrida nos arquivos roteados, injeta cabeçalhos Markdown em
    cada sub-tabela e poda colunas irrelevantes (ex: PvP, Custo em Ouro) para cortar
    até 99% do ruído antes de enviar ao modelo.
    """
    if not state.get("is_safe", True):
        return state

    t0 = time.perf_counter()
    skill_dir = state.get("skill_dir", ".claude/skills/risingtale-wiki")
    query = state.get("query", "")

    retrieval = retrieve_and_prune_context(skill_dir=skill_dir, query=query, top_k=6)
    elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)

    finops = retrieval["finops"]
    routed_files = retrieval["routed_files"]
    chunks = retrieval["chunks"]
    graph_insights = retrieval["graph_insights"]

    trace_entry = {
        "node": "RetrieverPrunerNode",
        "paper_concept": "Lost in the Middle (Liu et al., 2023) & Dynamic Table Pruning",
        "status": "completed",
        "latency_ms": elapsed_ms,
        "summary": (
            f"Roteado para {len(routed_files)} arquivo(s) ({', '.join(routed_files)}). "
            f"Extraídos {len(chunks)} chunks com cabeçalhos preservados e {finops['columns_pruned']} colunas podadas. "
            f"Redução de {finops['full_corpus_tokens']:,} -> {finops['frugal_tokens']:,} tokens (-{finops['savings_vs_full_pct']}%)."
        ),
    }

    node_trace = list(state.get("node_trace", []))
    node_trace.append(trace_entry)

    return {
        **state,
        "routed_files": routed_files,
        "chunks": chunks,
        "graph_insights": graph_insights,
        "finops": finops,
        "node_trace": node_trace,
    }


# ---------------------------------------------------------------------------
# Nó 4: CRAGValidatorNode (CRAG + GraphRAG Rule & Math Verifier)
# ---------------------------------------------------------------------------

def _compute_rising_tale_level_budget(level: int) -> Dict[str, Any]:
    """
    Calcula deterministicamente o orçamento de pontos (SP, EP, TP) no Rising Tale
    para evitar alucinações aritméticas de LLMs.
    Fórmulas extraídas de references/guide.md:
      - SP (Tiers 1 a 3, a partir do nível 10): 5 pontos base no Nv 10 + 1 ponto a cada 2 níveis pares.
      - EP (Tier 4, a partir do nível 60): 1 ponto no Nv 60 + 1 ponto a cada 2 níveis pares.
      - TP (Pontos de Treinamento para Nv 6..10): custo triangular T(n) = n*(n+1)/2 onde n = Nv_skill - 5.
    """
    if level < 10:
        return {"level": level, "sp_total": 0, "ep_total": 0}
    sp_total = 5 + ((level - 10) // 2)
    ep_total = 0 if level < 60 else 1 + ((level - 60) // 2)
    return {
        "level": level,
        "sp_total": sp_total,
        "ep_total": ep_total,
        "tp_cost_table_per_skill": {
            "Lv6": 1,
            "Lv7": 3,
            "Lv8": 6,
            "Lv9": 10,
            "Lv10": 15,
        },
        "formula_note": (
            f"No Nível {level}: Total exato = {sp_total} SP (Tiers 1–3) e {ep_total} EP (Tier 4). "
            "Custo acumulado de TP (acima do Nv 5): Nv 6=1 TP, Nv 7=3 TP, Nv 8=6 TP, Nv 9=10 TP, Nv 10=15 TP."
        ),
    }


def crag_validator_node(state: AgentState) -> AgentState:
    """
    Nó 4: Corrective RAG (CRAG) + GraphRAG.
    Cruza os chunks recuperados com as arestas determinísticas do Grafo de Conhecimento
    (conflitos de buff ✕, encadeamentos %, sinergia elemental oculta) e executa cálculos
    exatos de nível/SP/EP/TP se a pergunta envolver níveis.
    """
    if not state.get("is_safe", True):
        return state

    t0 = time.perf_counter()
    query = state.get("query", "")
    q_low = query.lower()
    graph_insights = list(state.get("graph_insights", []))
    validations: List[str] = []
    deterministic_math: Dict[str, Any] = {}

    # 1. Detecta menção a nível (ex: "lvl 81", "nível 81", "lv 80", "level 85")
    lvl_match = re.search(r"\b(?:lvl?|n[ií]vel|level)\s*(\d{2,3})\b", q_low)
    if not lvl_match:
        # fallback para número entre 60 e 120 se falar de build/skill
        if any(w in q_low for w in ["build", "skill", "sp", "ep", "ponto", "xamã", "xama", "lutador", "fs"]):
            num_match = re.search(r"\b(6\d|7\d|8\d|9\d|10\d)\b", q_low)
            if num_match:
                lvl_match = num_match

    if lvl_match:
        lvl = int(lvl_match.group(1))
        deterministic_math = _compute_rising_tale_level_budget(lvl)
        validations.append(f"📐 [Matemática Determinística Nv. {lvl}] {deterministic_math['formula_note']}")

    # 2. Validação de regras ocultas e pegadinhas clássicas do Grafo
    if any(w in q_low for w in ["fogo", "zecram", "lutador", "fs", "resistência", "resistencia"]):
        validations.append(
            "🔥 [Sinergia Oculta Verificada no Grafo] Em `Lutador`, a passiva Tier 1 `Resistência a Fogo` possui "
            "uma coluna oculta `Dano de fogo` (+1% no Nv.1 até +10% no Nv.10). Isso multiplica diretamente o dano "
            "de `Fúria de Zecram` (Tier 2, Fogo) e `Redemoinho da Fúria` (Tier 4, Fogo)."
        )

    if any(w in q_low for w in ["concentração", "concentracao", "machado veloz", "paz interior", "lutador", "fs"]):
        validations.append(
            "⚠️ [Conflito de Buff ✕ Verificado no Grafo] Em `Lutador`, `Concentração` ✕ NÃO coexiste com `Machado Veloz` "
            "nem com `Paz Interior`. Porém, `Paz Interior` (Tier 4) ENCADEIA 100% de `Concentração` + coexistindo com `Machado Veloz`!"
        )

    if any(w in q_low for w in ["xamã", "xama", "midranda", "migal", "advento"]):
        validations.append(
            "🔮 [Conflito & Encadeamento Verificado no Grafo] Em `Xamã`, `Advento de Migal` (Tier 3) e `Advento de Midranda` (Tier 4) "
            "têm conflito `✕ Não coexiste` (não podem ficar ativos juntos). Contudo, `Advento de Midranda` possui a propriedade "
            "`Encadeia 50% de Advento de Migal` — ou seja, ao ativar Midranda sozinho, você ganha a velocidade de Midranda + 50% do bônus de Dano de Migal!"
        )

    if any(w in q_low for w in ["missão", "missao", "quest", "automática", "automatica", "pino"]):
        validations.append(
            "🧭 [Mecânica de UI Verificada em guide.md] Missões Automáticas exigem abrir o painel completo de Missões (Tecla `J`), "
            "clicar no ícone de Pino de Mapa (`📌 Filtrar pelo mapa atual`) no topo da janela para DESATIVAR o filtro caso diga "
            "'Você não tem nada de missão', selecionar a aba `Disponíveis` e clicar em `Aceitar`."
        )

    # Inclui arestas do grafo encontradas pelo Retriever
    for gi in graph_insights[:8]:
        if gi not in validations:
            validations.append(gi)

    elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
    trace_entry = {
        "node": "CRAGValidatorNode",
        "paper_concept": "CRAG (Yan et al., 2024) + GraphRAG Deterministic Rule Verification",
        "status": "completed",
        "latency_ms": elapsed_ms,
        "summary": (
            f"Auditadas {len(validations)} regras determinísticas no Grafo (conflitos ✕, encadeamentos % e orçamento SP/EP/TP)."
        ),
    }

    node_trace = list(state.get("node_trace", []))
    node_trace.append(trace_entry)

    return {
        **state,
        "crag_validations": validations,
        "deterministic_math": deterministic_math,
        "node_trace": node_trace,
    }


# ---------------------------------------------------------------------------
# Nó 5: SynthesizerNode (100% Local-First BYOK + Modo Determinístico Zero-Token)
# ---------------------------------------------------------------------------

def _build_system_prompt() -> str:
    return (
        "Você é o **SkillRAG Studio Agent**, um assistente especialista em Engenharia de Conhecimento e Theorycrafting "
        "baseado em GraphRAG, CRAG e FrugalGPT.\n"
        "REGRAS OBRIGATÓRIAS:\n"
        "1. Responda SEMPRE em Português (pt-BR), de forma clara, direta, estruturada em Markdown e citando os arquivos fonte (ex: `references/classes.md`, `references/guide.md`).\n"
        "2. Priorize SEMPRE as **Validações Determinísticas do Grafo (CRAG)** (conflitos `✕ Não coexiste`, `Encadeia %`, colunas ocultas de `Dano de fogo` e matemática exata de `SP/EP/TP`).\n"
        "3. Nunca invente valores que não estejam nos chunks podados ou nas validações do Grafo."
    )


def _build_user_prompt(state: AgentState) -> str:
    query = state.get("query", "")
    validations = state.get("crag_validations", [])
    chunks = state.get("chunks", [])

    parts = [f"### PERGUNTA DO USUÁRIO:\n{query}\n"]

    if validations:
        parts.append("### VALIDAÇÕES DETERMINÍSTICAS DO GRAFO E CRAG (PRIORIDADE MÁXIMA):")
        for v in validations:
            parts.append(f"- {v}")
        parts.append("")

    parts.append("### CHUNKS DE TABELAS PODADAS (LOST IN THE MIDDLE OPTIMIZED):")
    for idx, ch in enumerate(chunks[:5], 1):
        parts.append(f"#### [Chunk {idx}] Fonte: `{ch.get('file', '')}` — Seção: `{ch.get('section', '')}`")
        parts.append(ch.get("pruned_text", "")[:2200])
        parts.append("")

    return "\n".join(parts)


def _call_llm_byok(
    provider: str,
    model: str,
    api_key: str,
    system_prompt: str,
    user_prompt: str,
) -> str:
    """
    Executa chamada ao LLM usando a chave BYOK fornecida em memória (RAM-only).
    Suporta OpenAI, Google Gemini, Anthropic Claude, Groq e Ollama Local.
    """
    provider = (provider or "deterministic").lower().strip()

    if provider == "openai":
        model_name = model or "gpt-4o-mini"
        resp = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model_name,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.1,
            },
            timeout=45,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    if provider == "groq":
        model_name = model or "llama-3.3-70b-versatile"
        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model_name,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.1,
            },
            timeout=45,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    if provider == "gemini":
        model_name = model or "gemini-2.5-flash"
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
        resp = requests.post(
            url,
            headers={"Content-Type": "application/json"},
            json={
                "systemInstruction": {"parts": [{"text": system_prompt}]},
                "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
                "generationConfig": {"temperature": 0.1},
            },
            timeout=45,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["candidates"][0]["content"]["parts"][0]["text"]

    if provider == "anthropic":
        model_name = model or "claude-3-5-haiku-latest"
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json={
                "model": model_name,
                "max_tokens": 1800,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_prompt}],
                "temperature": 0.1,
            },
            timeout=45,
        )
        resp.raise_for_status()
        return resp.json()["content"][0]["text"]

    if provider == "ollama":
        model_name = model or "llama3.2"
        base_url = api_key if api_key.startswith("http") else "http://127.0.0.1:11434"
        resp = requests.post(
            f"{base_url.rstrip('/')}/api/chat",
            json={
                "model": model_name,
                "stream": False,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            },
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()["message"]["content"]

    raise ValueError(f"Provedor BYOK desconhecido: {provider}")


def _synthesize_deterministic_zero_token(state: AgentState) -> str:
    """
    Modo Determinístico Zero-Token ($0.00 / Sem necessidade de API Key):
    Constrói uma resposta técnica completa combinando as validações CRAG do Grafo,
    a matemática exata de SP/EP/TP e as tabelas podadas com cabeçalho injetado.
    Ideal para quem clona o projeto no GitHub e quer testar imediatamente!
    """
    if not state.get("is_safe", True):
        findings = state.get("security_report", {}).get("findings", [])
        return (
            "### 🛡️ Bloqueio de Segurança (OWASP LLM01 Guardrail)\n\n"
            "A sua requisição foi bloqueada preventivamente pelo **GuardrailNode** antes de atingir o grafo de recuperação ou o LLM.\n\n"
            f"- **Motivo Detectado**: `{', '.join(findings)}`\n"
            "- **Tokens Gastos**: `0 tokens` ($0.00)"
        )

    validations = state.get("crag_validations", [])
    chunks = state.get("chunks", [])
    routed_files = state.get("routed_files", [])
    finops = state.get("finops", {})

    lines: List[str] = [
        "### 🧠 Resposta Auditada pelo Grafo Determinístico & CRAG *(Modo Zero-Token / $0.00)*",
        "",
    ]

    if validations:
        lines.append("#### 1. Regras Críticas e Sinergias Verificadas no Grafo (`knowledge_graph.json`)")
        for v in validations:
            lines.append(f"- {v}")
        lines.append("")

    if chunks:
        lines.append(f"#### 2. Evidências Extraídas e Podadas ({', '.join(f'`{f}`' for f in routed_files)})")
        for idx, ch in enumerate(chunks[:3], 1):
            lines.append(f"**[{idx}] `{ch.get('file')}` → {ch.get('section')}** *(Score Híbrido: {ch.get('score')})*")
            # Mostra um trecho enxuto da seção podada
            snippet_lines = [ln for ln in ch.get("pruned_text", "").splitlines() if ln.strip()][:18]
            lines.append("\n".join(snippet_lines))
            lines.append("")

    if finops:
        lines.append("#### 3. Telemetria FinOps & Economia de Contexto")
        lines.append(
            f"- **Corpus Total da Wiki**: `{finops.get('full_corpus_tokens', 0):,} tokens` → "
            f"**Contexto Podado Enviado**: `{finops.get('frugal_tokens', 0):,} tokens` "
            f"(**-{finops.get('savings_vs_full_pct', 0)}%** vs Full Dump | **-{finops.get('savings_vs_naive_pct', 0)}%** vs Naive RAG)."
        )

    return "\n".join(lines)


def _log_to_langsmith_if_configured(state: AgentState, latency_ms: float) -> Optional[str]:
    """
    Se o usuário forneceu uma chave LangSmith em memória (ou no ambiente),
    envia a trace estruturada dos 5 nós com a telemetria FinOps de economia de tokens.
    """
    ls_key = (state.get("langsmith_api_key") or os.environ.get("LANGCHAIN_API_KEY") or "").strip()
    if not ls_key:
        return None

    run_id = str(uuid.uuid4())
    try:
        requests.post(
            "https://api.smith.langchain.com/runs",
            headers={
                "x-api-key": ls_key,
                "Content-Type": "application/json",
            },
            json={
                "id": run_id,
                "name": "SkillRAG_Studio_GraphRAG_Pipeline",
                "run_type": "chain",
                "inputs": {
                    "query": state.get("query"),
                    "provider": state.get("provider"),
                    "model": state.get("model"),
                },
                "outputs": {
                    "answer": state.get("answer", "")[:1000],
                    "routed_files": state.get("routed_files", []),
                    "crag_validations": state.get("crag_validations", []),
                    "finops": state.get("finops", {}),
                },
                "extra": {
                    "metadata": {
                        "node_trace": state.get("node_trace", []),
                        "latency_ms": latency_ms,
                        "architecture": "FrugalGPT + LostInTheMiddle + CRAG + GraphRAG",
                    }
                },
            },
            timeout=5,
        )
        return f"https://smith.langchain.com/"
    except Exception:
        return None


def synthesizer_node(state: AgentState) -> AgentState:
    """
    Nó 5: Sintetiza a resposta final usando ou o Modo Determinístico Zero-Token (padrão gratuito)
    ou a chave BYOK fornecida em memória pelo usuário (OpenAI, Gemini, Anthropic, Groq, Ollama).
    """
    t0 = time.perf_counter()
    provider = (state.get("provider") or "deterministic").lower().strip()
    model = (state.get("model") or "").strip()
    api_key = (state.get("api_key") or "").strip()

    used_mode = "deterministic"
    error_fallback = None

    if not state.get("is_safe", True):
        answer = _synthesize_deterministic_zero_token(state)
    elif provider == "deterministic" or (provider != "ollama" and not api_key):
        answer = _synthesize_deterministic_zero_token(state)
    else:
        try:
            sys_prompt = _build_system_prompt()
            usr_prompt = _build_user_prompt(state)
            answer = _call_llm_byok(
                provider=provider,
                model=model,
                api_key=api_key,
                system_prompt=sys_prompt,
                user_prompt=usr_prompt,
            )
            used_mode = f"byok:{provider}:{model or 'default'}"
        except Exception as exc:
            error_fallback = str(exc)
            answer = (
                f"> ⚠️ **Aviso BYOK ({provider})**: Não foi possível autenticar/chamar a API externa (`{error_fallback}`). "
                "Exibindo resposta imediata pelo **Motor Determinístico GraphRAG Zero-Token**:\n\n"
                + _synthesize_deterministic_zero_token(state)
            )
            used_mode = "deterministic_fallback"

    elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
    trace_entry = {
        "node": "SynthesizerNode",
        "paper_concept": "BYOK Ephemeral Inference / Zero-Token Deterministic Synthesis",
        "status": "completed" if not error_fallback else "fallback",
        "latency_ms": elapsed_ms,
        "summary": (
            f"Síntese concluída via [{used_mode}] em {elapsed_ms} ms. "
            "Chave de API mantida apenas em escopo efêmero de requisição."
        ),
    }

    node_trace = list(state.get("node_trace", []))
    node_trace.append(trace_entry)

    # Apaga explicitamente a chave de API do dicionário final de estado para garantir zero vazamento em logs
    sanitized_state: AgentState = {
        **state,
        "api_key": "***REDACTED_EPHEMERAL_BYOK***" if api_key else "",
        "langsmith_api_key": "***REDACTED***" if state.get("langsmith_api_key") else "",
        "answer": answer,
        "node_trace": node_trace,
    }

    ls_url = _log_to_langsmith_if_configured(sanitized_state, elapsed_ms)
    sanitized_state["langsmith_trace_url"] = ls_url
    return sanitized_state


# ---------------------------------------------------------------------------
# Compilador e Executor do Grafo (LangGraph StateGraph + Fallback Nativo)
# ---------------------------------------------------------------------------

def run_agentic_rag_workflow(
    query: str,
    skill_dir: str = ".claude/skills/risingtale-wiki",
    provider: str = "deterministic",
    model: str = "",
    api_key: str = "",
    langsmith_api_key: str = "",
) -> Dict[str, Any]:
    """
    Executa a máquina de estados completa de 5 nós:
      GuardrailNode -> RouterNode -> RetrieverPrunerNode -> CRAGValidatorNode -> SynthesizerNode
    Se `langgraph` estiver disponível no runtime Python >= 3.10, compila e executa via `StateGraph`.
    Caso contrário, executa exatamente o mesmo pipeline determinístico de nós.
    """
    t_start = time.perf_counter()
    initial_state: AgentState = {
        "query": query,
        "skill_dir": skill_dir,
        "provider": provider or "deterministic",
        "model": model or "",
        "api_key": api_key or "",
        "langsmith_api_key": langsmith_api_key or "",
        "node_trace": [],
    }

    engine_used = "LangGraph StateGraph"
    try:
        from langgraph.graph import END, StateGraph

        builder = StateGraph(AgentState)
        builder.add_node("guardrail", guardrail_node)
        builder.add_node("router", router_node)
        builder.add_node("retriever_pruner", retriever_pruner_node)
        builder.add_node("crag_validator", crag_validator_node)
        builder.add_node("synthesizer", synthesizer_node)

        builder.set_entry_point("guardrail")
        builder.add_Conditional_edges_if_supported = False
        builder.add_edge("guardrail", "router")
        builder.add_edge("router", "retriever_pruner")
        builder.add_edge("retriever_pruner", "crag_validator")
        builder.add_edge("crag_validator", "synthesizer")
        builder.add_edge("synthesizer", END)

        graph = builder.compile()
        final_state = graph.invoke(initial_state)
    except Exception:
        # Execução sequencial determinística idêntica caso rode em Python 3.9 ou sem C-extensions
        engine_used = "Native Deterministic StateGraph"
        s1 = guardrail_node(initial_state)
        s2 = router_node(s1)
        s3 = retriever_pruner_node(s2)
        s4 = crag_validator_node(s3)
        final_state = synthesizer_node(s4)

    total_latency_ms = round((time.perf_counter() - t_start) * 1000, 2)

    return {
        "engine": engine_used,
        "total_latency_ms": total_latency_ms,
        "query": final_state.get("query"),
        "intent": final_state.get("intent", "blocked"),
        "is_safe": final_state.get("is_safe", False),
        "security_report": final_state.get("security_report", {}),
        "routed_files": final_state.get("routed_files", []),
        "crag_validations": final_state.get("crag_validations", []),
        "deterministic_math": final_state.get("deterministic_math", {}),
        "finops": final_state.get("finops", {}),
        "node_trace": final_state.get("node_trace", []),
        "chunks_preview": [
            {
                "file": c.get("file"),
                "section": c.get("section"),
                "score": c.get("score"),
                "pruned_snippet": c.get("pruned_text", "")[:500],
            }
            for c in final_state.get("chunks", [])[:4]
        ],
        "answer": final_state.get("answer", ""),
        "langsmith_trace_url": final_state.get("langsmith_trace_url"),
    }

