import json
import os
import re
from typing import Any, Callable, Dict, List, Literal, Optional, TypedDict
from pydantic import BaseModel, Field

from langchain_core.messages import HumanMessage, SystemMessage, AIMessage
from langgraph.graph import StateGraph, START, END


# =====================================================================
# 1. Pydantic Schemas for Structured Outputs
# =====================================================================

class SiteScores(BaseModel):
    mcp: int = Field(description="Aderência (0 a 100) para virar um Servidor MCP (APIs, catálogos, ações estruturadas, SaaS).")
    skill: int = Field(description="Aderência (0 a 100) para virar uma Skill SKILL.md (documentação, guias, regras de negócio, referência técnica).")
    agent: int = Field(description="Aderência (0 a 100) para virar um Agente LangGraph conversacional (atendimento, consultoria, fluxos multi-etapas).")


class SiteClassification(BaseModel):
    recommended_type: Literal["mcp", "skill", "agent"] = Field(
        description="Melhor formato de saída para este site: 'mcp', 'skill' ou 'agent'."
    )
    scores: SiteScores = Field(description="Pontuação de 0 a 100 para cada um dos 3 formatos.")
    summary: str = Field(description="Resumo executivo do que o site faz e qual conhecimento foi extraído (em português).")
    reasoning: str = Field(description="Explicação clara e técnica em português de por que o LangGraph escolheu esse formato.")
    capabilities: List[str] = Field(description="Lista de 4 a 8 capacidades práticas, ferramentas ou tópicos extraídos do site.")


class GeneratedToolSpec(BaseModel):
    func_name: str = Field(description="Nome da função em snake_case válido em Python (ex: consultar_produtos, buscar_documentacao).")
    title: str = Field(description="Título amigável da ferramenta.")
    description: str = Field(description="Docstring explicando o que a tool faz e quando o agente deve usá-la.")
    endpoint_url: Optional[str] = Field(default=None, description="URL do endpoint real caso mapeado de uma API do site, ou vazio se operar na base local.")
    http_method: str = Field(default="GET", description="Método HTTP (GET ou POST).")
    param_name: str = Field(default="query", description="Nome do parâmetro principal em snake_case.")
    param_description: str = Field(default="Termo de busca ou filtro", description="Descrição do parâmetro.")


class MCPBlueprint(BaseModel):
    server_name: str = Field(description="Nome curto do servidor MCP.")
    agensi_description: str = Field(description="Descrição comercial atraente para publicar/vender este MCP na Agensi.")
    category: str = Field(description="Categoria no marketplace (ex: E-commerce, Developer Tools, Finance, Knowledge Base).")
    suggested_tools: List[GeneratedToolSpec] = Field(description="Lista de 3 a 6 ferramentas customizadas para este site.")


class SkillBlueprint(BaseModel):
    skill_name: str = Field(description="Identificador em kebab-case da skill.")
    short_description: str = Field(description="Descrição de 1-2 frases para o frontmatter YAML indicando quando usar a skill.")
    core_instructions_markdown: str = Field(
        description="Instruções operacionais completas em Markdown (regras, conceitos-chave, fluxos de trabalho e respostas extraídas do site)."
    )


class AgentBlueprint(BaseModel):
    agent_name: str = Field(description="Nome do Agente Especialista.")
    role_description: str = Field(description="Papel e especialidade do agente.")
    system_prompt: str = Field(description="System prompt detalhado com todo o conhecimento chave e diretrizes de atendimento/resolução do site.")
    suggested_questions: List[str] = Field(description="3 a 4 perguntas de exemplo que o usuário pode fazer para testar o agente no chat.")


# =====================================================================
# 2. Multi-Provider LLM Factory
# =====================================================================

DEFAULT_MODELS = {
    "openai": "gpt-4o-mini",
    "gemini": "gemini-2.5-flash",
    "anthropic": "claude-3-5-sonnet-latest",
}


def create_llm(provider: str = "openai", model: Optional[str] = None, api_key: Optional[str] = None, temperature: float = 0.2):
    """Instantiate a LangChain ChatModel for OpenAI, Google Gemini, or Anthropic."""
    provider = (provider or "openai").lower().strip()
    model_name = (model or "").strip() or DEFAULT_MODELS.get(provider, "gpt-4o-mini")
    clean_key = (api_key or "").strip()

    if provider == "openai":
        key = clean_key or os.environ.get("OPENAI_API_KEY", "")
        if not key:
            return None
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(model=model_name, api_key=key, temperature=temperature)

    if provider in ("gemini", "google"):
        key = clean_key or os.environ.get("GOOGLE_API_KEY", "") or os.environ.get("GEMINI_API_KEY", "")
        if not key:
            return None
        from langchain_google_genai import ChatGoogleGenerativeAI
        return ChatGoogleGenerativeAI(model=model_name, google_api_key=key, temperature=temperature)

    if provider == "anthropic":
        key = clean_key or os.environ.get("ANTHROPIC_API_KEY", "")
        if not key:
            return None
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(model=model_name, anthropic_api_key=key, temperature=temperature)

    return None


# =====================================================================
# 3. LangGraph State Definition
# =====================================================================

class FactoryGraphState(TypedDict):
    knowledge: Dict[str, Any]
    provider: str
    model: str
    api_key: str
    forced_type: str  # "auto" | "mcp" | "skill" | "agent"
    classification: Dict[str, Any]
    active_type: str
    artifacts: Dict[str, Dict[str, Any]]
    logs: List[str]


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", (text or "site").lower()).strip("-")
    return slug[:40] or "site"


def _snake_case(text: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "_", (text or "tool").lower()).strip("_")
    if not s or s[0].isdigit():
        s = "tool_" + s
    return s[:40]


def _build_knowledge_digest(knowledge: Dict[str, Any]) -> str:
    """Create a compact prompt-friendly digest of the extracted site knowledge."""
    stats = knowledge.get("stats", {})
    apis = knowledge.get("apis", [])
    headings = [f"{h.get('level', 'h2').upper()}: {h.get('text', '')}" for h in knowledge.get("headings", [])[:25]]
    forms = knowledge.get("forms", [])
    products = knowledge.get("products_sample", [])
    md_preview = (knowledge.get("markdown_content") or "")[:9000]

    api_lines = []
    for a in apis[:15]:
        keys_str = ", ".join(a.get("sample_keys", [])[:8])
        api_lines.append(f"- {a.get('method', 'GET')} {a.get('endpoint', a.get('url', ''))} (keys: [{keys_str}])")

    return (
        f"URL: {knowledge.get('url')}\n"
        f"Título: {knowledge.get('title')}\n"
        f"Descrição: {knowledge.get('description')}\n"
        f"Estatísticas: {json.dumps(stats, ensure_ascii=False)}\n\n"
        f"--- Principais Títulos (Headings) ---\n" + ("\n".join(headings) or "Nenhum") + "\n\n"
        f"--- Endpoints de API / JSON Interceptados ({len(apis)}) ---\n" + ("\n".join(api_lines) or "Nenhuma API JSON dedicada detectada") + "\n\n"
        f"--- Formulários Detectados ({len(forms)}) ---\n" + json.dumps(forms[:5], ensure_ascii=False) + "\n\n"
        f"--- Amostra de Produtos ({len(products)}) ---\n" + json.dumps(products[:3], ensure_ascii=False) + "\n\n"
        f"--- Conteúdo Markdown Extraído (Trecho) ---\n{md_preview}"
    )


def _heuristic_classification(knowledge: Dict[str, Any]) -> SiteClassification:
    """Fast heuristic classification when running without an LLM API key or on fallback."""
    stats = knowledge.get("stats", {})
    apis_count = stats.get("apis_count", 0)
    products_count = stats.get("products_count", 0)
    forms_count = stats.get("forms_count", 0) + stats.get("standalone_inputs_count", 0)
    md_chars = stats.get("markdown_chars", 0)
    headings_count = stats.get("headings_count", 0)

    mcp_score = min(95, 30 + apis_count * 12 + (25 if products_count > 0 else 0) + min(forms_count * 5, 20))
    skill_score = min(95, 35 + min(md_chars // 400, 40) + min(headings_count * 2, 20))
    agent_score = min(95, 40 + min(forms_count * 6, 25) + min(md_chars // 600, 20) + (10 if apis_count > 0 else 0))

    if mcp_score >= skill_score and mcp_score >= agent_score and (apis_count >= 2 or products_count > 0):
        rec: Literal["mcp", "skill", "agent"] = "mcp"
        reason = (
            f"Foram interceptados {apis_count} endpoints de API/JSON na rede"
            + (f" e {products_count} produtos estruturados" if products_count else "")
            + ". Isso torna o site ideal para ser encapsulado como um Servidor FastMCP com tools executáveis para venda na Agensi."
        )
    elif skill_score >= agent_score and md_chars > 2500:
        rec = "skill"
        reason = (
            f"O site possui conteúdo rico e estruturado ({md_chars} caracteres de texto limpo e {headings_count} seções), "
            "sendo excelente para empacotamento como uma Skill (SKILL.md + base de referência) que ensina qualquer agente a dominar esse tema."
        )
    else:
        rec = "agent"
        reason = (
            f"Pela combinação de páginas interativas ({forms_count} entradas/formulários) e conteúdo de domínio, "
            "um Agente LangGraph conversacional com ferramentas de busca no site oferece a melhor experiência."
        )

    caps = [f"Consulta ao conteúdo de '{knowledge.get('title', knowledge.get('site_name'))}'"]
    for h in knowledge.get("headings", [])[:4]:
        caps.append(f"Domínio sobre: {h.get('text')}")
    for a in knowledge.get("apis", [])[:3]:
        caps.append(f"Integração via API: {a.get('method')} {a.get('path')}")

    return SiteClassification(
        recommended_type=rec,
        scores=SiteScores(mcp=mcp_score, skill=skill_score, agent=agent_score),
        summary=knowledge.get("description") or f"Base de conhecimento e recursos extraídos de {knowledge.get('title') or knowledge.get('url')}.",
        reasoning=reason,
        capabilities=caps[:7],
    )


# =====================================================================
# 4. Code & Package Generators (MCP / Skill / LangGraph Agent)
# =====================================================================

def _render_mcp_package(knowledge: Dict[str, Any], classification: Dict[str, Any], blueprint: MCPBlueprint) -> Dict[str, Any]:
    site_slug = _slugify(knowledge.get("site_name") or blueprint.server_name)
    title = knowledge.get("title") or blueprint.server_name

    # Build @mcp.tool functions
    tool_blocks = []
    manifest_tools = [
        {
            "name": "search_site_knowledge",
            "description": f"Pesquisa textual e semântica no conteúdo extraído de {title}",
        },
        {
            "name": "list_discovered_apis",
            "description": f"Lista todos os endpoints de API descobertos em {title}",
        },
    ]

    seen_names = {"search_site_knowledge", "list_discovered_apis"}
    for t in blueprint.suggested_tools[:6]:
        fn = _snake_case(t.func_name)
        if fn in seen_names:
            fn = f"{fn}_tool"
        seen_names.add(fn)
        pname = _snake_case(t.param_name or "query")
        manifest_tools.append({"name": fn, "description": t.description})

        if t.endpoint_url:
            tool_blocks.append(f'''
@mcp.tool()
def {fn}({pname}: str = "") -> str:
    """{t.description}

    Args:
        {pname}: {t.param_description}
    """
    target_url = {json.dumps(t.endpoint_url)}
    try:
        with httpx.Client(timeout=15.0, follow_redirects=True) as client:
            params = {{"{pname}": {pname}}} if {pname} else None
            resp = client.request({json.dumps(t.http_method.upper())}, target_url, params=params)
            return f"Status {{resp.status_code}}:\\n{{resp.text[:4000]}}"
    except Exception as exc:
        return f"Erro ao consultar endpoint {{target_url}}: {{exc}}"
''')
        else:
            tool_blocks.append(f'''
@mcp.tool()
def {fn}({pname}: str = "") -> str:
    """{t.description}

    Args:
        {pname}: {t.param_description}
    """
    return _search_knowledge({pname} or {json.dumps(t.title)})
''')

    custom_tools_code = "\n".join(tool_blocks)

    server_py = f'''"""
Servidor FastMCP gerado automaticamente para: {title}
URL de origem: {knowledge.get("url")}
Pronto para execução via uvx / FastMCP e publicação na Agensi.
"""
import json
import os
from pathlib import Path
import httpx

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    from fastmcp import FastMCP

mcp = FastMCP({json.dumps(blueprint.server_name)})

KNOWLEDGE_PATH = Path(__file__).parent / "site_knowledge.json"


def _load_knowledge() -> dict:
    if KNOWLEDGE_PATH.exists():
        return json.loads(KNOWLEDGE_PATH.read_text(encoding="utf-8"))
    return {{}}


def _search_knowledge(query: str) -> str:
    data = _load_knowledge()
    md = data.get("markdown_content", "")
    if not query.strip():
        return md[:4000]
    terms = [t.lower() for t in query.split() if len(t) > 2]
    paragraphs = [p.strip() for p in md.split("\\n\\n") if p.strip()]
    scored = []
    for p in paragraphs:
        low = p.lower()
        hits = sum(1 for term in terms if term in low)
        if hits > 0:
            scored.append((hits, p))
    scored.sort(key=lambda x: x[0], reverse=True)
    if scored:
        return "\\n\\n---\\n\\n".join(p for _, p in scored[:6])[:5000]
    return md[:3500]


@mcp.resource("site://{site_slug}/knowledge")
def get_full_site_knowledge() -> str:
    """Retorna o resumo e conteúdo completo extraído de {title}."""
    data = _load_knowledge()
    return f"# {{data.get('title', '')}}\\nURL: {{data.get('url', '')}}\\n\\n{{data.get('markdown_content', '')[:15000]}}"


@mcp.tool()
def search_site_knowledge(query: str) -> str:
    """Busca informações, regras, textos e dados extraídos do site {title}.

    Args:
        query: Termo ou pergunta para pesquisar na base de conhecimento do site.
    """
    return _search_knowledge(query)


@mcp.tool()
def list_discovered_apis() -> str:
    """Lista todas as rotas de API (XHR/Fetch/JSON) e catálogos interceptados em {title}."""
    data = _load_knowledge()
    apis = data.get("apis", [])
    if not apis:
        return "Nenhuma rota JSON externa dedicada foi capturada; utilize search_site_knowledge."
    return json.dumps(apis, indent=2, ensure_ascii=False)
{custom_tools_code}

if __name__ == "__main__":
    mcp.run()
'''

    agensi_manifest = {
        "name": blueprint.server_name,
        "slug": f"{site_slug}-mcp",
        "version": "1.0.0",
        "description": blueprint.agensi_description,
        "category": blueprint.category,
        "source_url": knowledge.get("url"),
        "runtime": "python-fastmcp",
        "entrypoint": "server.py",
        "command": "uv",
        "args": ["run", "server.py"],
        "tools": manifest_tools,
        "capabilities": classification.get("capabilities", []),
    }

    pyproject_toml = f'''[project]
name = "{site_slug}-mcp"
version = "1.0.0"
description = {json.dumps(blueprint.agensi_description)}
readme = "README.md"
requires-python = ">=3.11"
dependencies = [
    "mcp[cli]>=1.3.0",
    "fastmcp>=0.4.0",
    "httpx>=0.28.0",
    "pydantic>=2.10.0",
]

[project.scripts]
{site_slug}-mcp = "server:mcp.run"
'''

    readme_md = f'''# 🔌 {blueprint.server_name} (FastMCP Server)

{blueprint.agensi_description}

- **Site de Origem:** {knowledge.get("url")}
- **Categoria Agensi:** `{blueprint.category}`
- **Gerado por:** Website Downloader + LangGraph Agent Factory

## 🛠️ Ferramentas (`@mcp.tool`)

{chr(10).join(f"- **`{t['name']}`**: {t['description']}" for t in manifest_tools)}

## 🚀 Como Rodar Localmente

```bash
uv sync
uv run python server.py
```

## 📦 Configuração MCP (Claude Desktop / Cursor / Antigravity / Agensi)

```json
{{
  "mcpServers": {{
    "{site_slug}": {{
      "command": "uv",
      "args": ["--directory", "/caminho/para/{site_slug}-mcp", "run", "server.py"]
    }}
  }}
}}
```
'''

    return {
        "type": "mcp",
        "title": f"Servidor FastMCP: {blueprint.server_name}",
        "description": blueprint.agensi_description,
        "files": {
            "server.py": server_py,
            "agensi.json": json.dumps(agensi_manifest, indent=2, ensure_ascii=False),
            "pyproject.toml": pyproject_toml,
            "README.md": readme_md,
            "site_knowledge.json": json.dumps(knowledge, indent=2, ensure_ascii=False),
        },
    }


def _render_skill_package(knowledge: Dict[str, Any], classification: Dict[str, Any], blueprint: SkillBlueprint) -> Dict[str, Any]:
    site_slug = _slugify(blueprint.skill_name or knowledge.get("site_name", "site-skill"))
    title = knowledge.get("title") or site_slug

    apis = knowledge.get("apis", [])
    api_section = ""
    if apis:
        api_rows = "\n".join(
            f"| `{a.get('method')}` | `{a.get('endpoint')}` | `{', '.join(a.get('sample_keys', [])[:6])}` |"
            for a in apis[:15]
        )
        api_section = f"\n## Endpoints de API Descobertos\n\n| Método | Endpoint | Campos JSON |\n|---|---|---|\n{api_rows}\n"

    skill_md = f'''---
name: {site_slug}
description: {blueprint.short_description}
---

# {title} — Guia Operacional da Skill

> **Fonte original:** {knowledge.get("url")}

## Visão Geral
{classification.get("summary", "")}

## Capacidades Principais
{chr(10).join(f"- {c}" for c in classification.get("capabilities", []))}

---

## Instruções e Conhecimento do Domínio

{blueprint.core_instructions_markdown}
{api_section}
---

## Arquivos de Referência e Scripts
- Leia `references/knowledge_base.md` para consultar o conteúdo integral extraído da página.
- Execute `python3 scripts/search_knowledge.py "<termo>"` para buscar trechos específicos rapidamente.
'''

    reference_md = f'''# Base de Conhecimento Completa: {title}

- **URL:** {knowledge.get("url")}
- **Descrição:** {knowledge.get("description")}

## Estrutura de Tópicos
{chr(10).join(f"- **{h.get('level', 'h2').upper()}**: {h.get('text')}" for h in knowledge.get("headings", []))}

---

## Conteúdo Extraído (Markdown)

{knowledge.get("markdown_content", "")}
'''

    search_script_py = '''#!/usr/bin/env python3
"""Script utilitário para pesquisar na base de conhecimento da Skill."""
import sys
from pathlib import Path

REF_FILE = Path(__file__).resolve().parent.parent / "references" / "knowledge_base.md"

def search(query: str) -> None:
    if not REF_FILE.exists():
        print("Arquivo de referência não encontrado.")
        return
    content = REF_FILE.read_text(encoding="utf-8")
    blocks = [b.strip() for b in content.split("\\n\\n") if b.strip()]
    terms = [t.lower() for t in query.split()]
    matches = [b for b in blocks if any(t in b.lower() for t in terms)]
    print(f"=== Resultados para '{query}' ({len(matches)} blocos) ===\\n")
    for block in matches[:10]:
        print(block)
        print("-" * 40)

if __name__ == "__main__":
    q = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else ""
    search(q)
'''

    return {
        "type": "skill",
        "title": f"Skill: {site_slug}",
        "description": blueprint.short_description,
        "files": {
            "SKILL.md": skill_md,
            "references/knowledge_base.md": reference_md,
            "scripts/search_knowledge.py": search_script_py,
        },
    }


def _render_agent_package(knowledge: Dict[str, Any], classification: Dict[str, Any], blueprint: AgentBlueprint) -> Dict[str, Any]:
    site_slug = _slugify(knowledge.get("site_name") or blueprint.agent_name)
    title = knowledge.get("title") or blueprint.agent_name

    agent_py = f'''"""
Agente LangGraph autônomo construído com o conhecimento de: {title}
URL: {knowledge.get("url")}
"""
import json
import os
from pathlib import Path
from typing import Annotated, TypedDict
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_core.tools import tool
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition

KNOWLEDGE_FILE = Path(__file__).parent / "site_knowledge.json"
 KNOWLEDGE_DATA = json.loads(KNOWLEDGE_FILE.read_text(encoding="utf-8")) if KNOWLEDGE_FILE.exists() else {{}}

SYSTEM_PROMPT = {json.dumps(blueprint.system_prompt, ensure_ascii=False)}


@tool
def search_site_content(query: str) -> str:
    """Pesquisa informações detalhadas, textos e seções extraídas do site {title}."""
    md = KNOWLEDGE_DATA.get("markdown_content", "")
    if not query.strip():
        return md[:3500]
    terms = [t.lower() for t in query.split() if len(t) > 2]
    chunks = [c.strip() for c in md.split("\\n\\n") if c.strip()]
    ranked = sorted(
        chunks,
        key=lambda c: sum(1 for term in terms if term in c.lower()),
        reverse=True,
    )
    return "\\n\\n---\\n\\n".join(ranked[:6])[:4500]


@tool
def list_site_links_and_apis() -> str:
    """Retorna os links de navegação, formulários e APIs descobertas no site {title}."""
    return json.dumps({{
        "links": KNOWLEDGE_DATA.get("links", [])[:20],
        "apis": KNOWLEDGE_DATA.get("apis", [])[:15],
        "forms": KNOWLEDGE_DATA.get("forms", [])[:5],
    }}, ensure_ascii=False, indent=2)


TOOLS = [search_site_content, list_site_links_and_apis]


class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]


def build_agent(provider: str = "openai", model: str = "gpt-4o-mini"):
    if provider == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI
        llm = ChatGoogleGenerativeAI(model=model)
    elif provider == "anthropic":
        from langchain_anthropic import ChatAnthropic
        llm = ChatAnthropic(model=model)
    else:
        from langchain_openai import ChatOpenAI
        llm = ChatOpenAI(model=model)

    llm_with_tools = llm.bind_tools(TOOLS)

    def assistant_node(state: AgentState):
        msgs = [SystemMessage(content=SYSTEM_PROMPT)] + state["messages"]
        return {{"messages": [llm_with_tools.invoke(msgs)]}}

    builder = StateGraph(AgentState)
    builder.add_node("assistant", assistant_node)
    builder.add_node("tools", ToolNode(TOOLS))
    builder.add_edge(START, "assistant")
    builder.add_conditional_edges("assistant", tools_condition)
    builder.add_edge("tools", "assistant")
    return builder.compile()


if __name__ == "__main__":
    agent = build_agent()
    print("🤖 Agente {blueprint.agent_name} pronto! Digite sua pergunta (ou 'sair'):")
    while True:
        q = input("\\nVocê: ").strip()
        if q.lower() in ("sair", "exit", "quit"):
            break
        res = agent.invoke({{"messages": [HumanMessage(content=q)]}})
        print("\\nAgente:", res["messages"][-1].content)
'''.replace(" KNOWLEDGE_DATA =", "KNOWLEDGE_DATA =")

    pyproject_toml = f'''[project]
name = "{site_slug}-langgraph-agent"
version = "1.0.0"
description = {json.dumps(blueprint.role_description, ensure_ascii=False)}
readme = "README.md"
requires-python = ">=3.11"
dependencies = [
    "langchain>=0.3.20",
    "langchain-core>=0.3.40",
    "langgraph>=0.3.5",
    "langchain-openai>=0.3.7",
    "langchain-google-genai>=2.0.11",
    "langchain-anthropic>=0.3.8",
]
'''

    readme_md = f'''# 🤖 {blueprint.agent_name} (LangGraph Agent)

{blueprint.role_description}

- **Site Base:** {knowledge.get("url")}
- **Arquitetura:** LangGraph `StateGraph` + `ToolNode`

## 💬 Exemplos de Perguntas
{chr(10).join(f"- *{q}*" for q in blueprint.suggested_questions)}

## 🚀 Como Executar

```bash
export OPENAI_API_KEY="sua-chave"
uv sync
uv run python agent.py
```
'''

    return {
        "type": "agent",
        "title": f"Agente LangGraph: {blueprint.agent_name}",
        "description": blueprint.role_description,
        "suggested_questions": blueprint.suggested_questions,
        "system_prompt": blueprint.system_prompt,
        "files": {
            "agent.py": agent_py,
            "pyproject.toml": pyproject_toml,
            "README.md": readme_md,
            "site_knowledge.json": json.dumps(knowledge, indent=2, ensure_ascii=False),
        },
    }


# =====================================================================
# 5. LangGraph Nodes & Orchestrator
# =====================================================================

def make_factory_graph(log_callback: Optional[Callable[[str], None]] = None):
    """Build and compile the LangGraph StateGraph that classifies and generates artifacts."""
    log = log_callback or (lambda msg: None)

    def classify_site_node(state: FactoryGraphState) -> Dict[str, Any]:
        knowledge = state["knowledge"]
        # If classification already exists (e.g. user clicked override on frontend), reuse it!
        if state.get("classification") and state.get("forced_type") in ("mcp", "skill", "agent"):
            active = state["forced_type"]
            log(f"🎯 Forçando geração no formato selecionado pelo usuário: {active.upper()}...")
            updated_cls = dict(state["classification"])
            updated_cls["active_type"] = active
            return {"classification": updated_cls, "active_type": active}

        log("🧠 [LangGraph] Analisando estrutura do site, conteúdo Markdown e rotas de API...")
        llm = create_llm(state.get("provider", "openai"), state.get("model"), state.get("api_key"))
        digest = _build_knowledge_digest(knowledge)

        classification_obj: Optional[SiteClassification] = None
        if llm is not None:
            try:
                structured_llm = llm.with_structured_output(SiteClassification)
                prompt = (
                    "Você é um arquiteto especialista em Agentes de IA, Skills e Servidores MCP (Model Context Protocol).\n"
                    "Analise os dados extraídos deste site e decida qual é a melhor transformação:\n"
                    "1. 'mcp': Quando o site possui APIs JSON/REST interceptadas, catálogos de produtos, buscas estruturadas ou ações que podem virar @mcp.tool() para vender na Agensi.\n"
                    "2. 'skill': Quando o site é focado em documentação, artigos técnicos, regras de negócio, manuais ou conhecimento denso de referência (SKILL.md).\n"
                    "3. 'agent': Quando o site representa um serviço interativo, atendimento, consultoria ou fluxo guiado ideal para um Agente Conversacional LangGraph.\n\n"
                    f"Dados extraídos do site:\n{digest}"
                )
                classification_obj = structured_llm.invoke(prompt)
            except Exception as exc:
                log(f"⚠️ Aviso no classificador LLM ({str(exc)[:80]}). Usando classificador heurístico...")

        if classification_obj is None:
            classification_obj = _heuristic_classification(knowledge)

        rec_type = classification_obj.recommended_type
        forced = state.get("forced_type", "auto")
        active_type = forced if forced in ("mcp", "skill", "agent") else rec_type

        cls_dict = {
            "recommended_type": rec_type,
            "active_type": active_type,
            "scores": {
                "mcp": classification_obj.scores.mcp,
                "skill": classification_obj.scores.skill,
                "agent": classification_obj.scores.agent,
            },
            "summary": classification_obj.summary,
            "reasoning": classification_obj.reasoning,
            "capabilities": classification_obj.capabilities,
        }

        log(
            f"✅ [LangGraph Decisão] Recomendado: {rec_type.upper()} "
            f"(MCP: {cls_dict['scores']['mcp']}% | Skill: {cls_dict['scores']['skill']}% | Agente: {cls_dict['scores']['agent']}%)"
        )
        return {"classification": cls_dict, "active_type": active_type}

    def route_by_active_type(state: FactoryGraphState) -> Literal["generate_mcp", "generate_skill", "generate_agent"]:
        active = state.get("active_type", "agent")
        if active == "mcp":
            return "generate_mcp"
        if active == "skill":
            return "generate_skill"
        return "generate_agent"

    def generate_mcp_node(state: FactoryGraphState) -> Dict[str, Any]:
        knowledge = state["knowledge"]
        classification = state["classification"]
        log("🔌 [LangGraph] Construindo Servidor FastMCP (Python) + Manifesto Agensi...")

        llm = create_llm(state.get("provider", "openai"), state.get("model"), state.get("api_key"))
        digest = _build_knowledge_digest(knowledge)
        blueprint: Optional[MCPBlueprint] = None

        if llm is not None:
            try:
                structured_llm = llm.with_structured_output(MCPBlueprint)
                prompt = (
                    "Projete um servidor FastMCP comercializável na Agensi para este site.\n"
                    "Crie ferramentas (@mcp.tool) úteis baseadas nas APIs interceptadas e nas seções de conteúdo do site.\n"
                    "Se houver endpoints de API listados, mapeie-os em `endpoint_url`.\n\n"
                    f"{digest}"
                )
                blueprint = structured_llm.invoke(prompt)
            except Exception as exc:
                log(f"⚠️ Fallback na síntese de tools MCP: {str(exc)[:80]}")

        if blueprint is None:
            tools_fallback = []
            for idx, api in enumerate(knowledge.get("apis", [])[:4], 1):
                path_part = _snake_case(api.get("path", "").split("/")[-1] or f"endpoint_{idx}")
                tools_fallback.append(
                    GeneratedToolSpec(
                        func_name=f"call_{path_part}",
                        title=f"Consultar {api.get('path')}",
                        description=f"Consulta o endpoint {api.get('method')} {api.get('endpoint')} capturado no site.",
                        endpoint_url=api.get("endpoint"),
                        http_method=api.get("method", "GET"),
                        param_name="filtro",
                        param_description="Parâmetro opcional de consulta",
                    )
                )
            if not tools_fallback:
                tools_fallback.append(
                    GeneratedToolSpec(
                        func_name="consultar_secoes_principais",
                        title="Consultar Seções Principais",
                        description=f"Busca informações estruturadas sobre {knowledge.get('title')}.",
                        endpoint_url=None,
                        http_method="GET",
                        param_name="topico",
                        param_description="Tópico ou seção desejada",
                    )
                )
            blueprint = MCPBlueprint(
                server_name=knowledge.get("title") or knowledge.get("site_name", "Site MCP"),
                agensi_description=classification.get("summary") or f"Servidor MCP para integração com {knowledge.get('url')}.",
                category="Knowledge & Automation",
                suggested_tools=tools_fallback,
            )

        pkg = _render_mcp_package(knowledge, classification, blueprint)
        artifacts = dict(state.get("artifacts") or {})
        artifacts["mcp"] = pkg
        log("✨ [LangGraph] Pacote FastMCP gerado com sucesso!")
        return {"artifacts": artifacts}

    def generate_skill_node(state: FactoryGraphState) -> Dict[str, Any]:
        knowledge = state["knowledge"]
        classification = state["classification"]
        log("📚 [LangGraph] Sintetizando pacote de Skill (SKILL.md + referências)...")

        llm = create_llm(state.get("provider", "openai"), state.get("model"), state.get("api_key"))
        digest = _build_knowledge_digest(knowledge)
        blueprint: Optional[SkillBlueprint] = None

        if llm is not None:
            try:
                structured_llm = llm.with_structured_output(SkillBlueprint)
                prompt = (
                    "Crie uma Skill completa (SKILL.md) a partir do conhecimento extraído deste site.\n"
                    "Organize as regras de negócio, fluxos, conceitos principais, tabelas e instruções práticas em Markdown claro.\n\n"
                    f"{digest}"
                )
                blueprint = structured_llm.invoke(prompt)
            except Exception as exc:
                log(f"⚠️ Fallback na síntese da Skill: {str(exc)[:80]}")

        if blueprint is None:
            headings_md = "\n".join(f"- **{h.get('text')}**" for h in knowledge.get("headings", [])[:20])
            blueprint = SkillBlueprint(
                skill_name=_slugify(knowledge.get("site_name", "site-skill")),
                short_description=f"Especialista no conteúdo, processos e dados de {knowledge.get('title') or knowledge.get('url')}.",
                core_instructions_markdown=(
                    f"### Tópicos Cobertos\n{headings_md}\n\n"
                    f"### Resumo Estruturado do Conteúdo\n{(knowledge.get('markdown_content') or '')[:6000]}"
                ),
            )

        pkg = _render_skill_package(knowledge, classification, blueprint)
        artifacts = dict(state.get("artifacts") or {})
        artifacts["skill"] = pkg
        log("✨ [LangGraph] Pacote Skill gerado com sucesso!")
        return {"artifacts": artifacts}

    def generate_agent_node(state: FactoryGraphState) -> Dict[str, Any]:
        knowledge = state["knowledge"]
        classification = state["classification"]
        log("🤖 [LangGraph] Construindo Agente Especialista LangGraph + Base de Conhecimento...")

        llm = create_llm(state.get("provider", "openai"), state.get("model"), state.get("api_key"))
        digest = _build_knowledge_digest(knowledge)
        blueprint: Optional[AgentBlueprint] = None

        if llm is not None:
            try:
                structured_llm = llm.with_structured_output(AgentBlueprint)
                prompt = (
                    "Projete um Agente LangGraph especialista neste site.\n"
                    "Escreva um `system_prompt` rico em português contendo os fatos principais, produtos/serviços, "
                    "regras e como responder dúvidas sobre o domínio do site, além de 4 perguntas sugeridas.\n\n"
                    f"{digest}"
                )
                blueprint = structured_llm.invoke(prompt)
            except Exception as exc:
                log(f"⚠️ Fallback na síntese do Agente: {str(exc)[:80]}")

        if blueprint is None:
            title = knowledge.get("title") or knowledge.get("site_name", "Site")
            blueprint = AgentBlueprint(
                agent_name=f"Agente {title[:40]}",
                role_description=classification.get("summary") or f"Agente especialista nos dados e serviços de {title}.",
                system_prompt=(
                    f"Você é o Agente Especialista oficial de {title} ({knowledge.get('url')}).\n"
                    f"Resumo do domínio: {classification.get('summary', '')}\n\n"
                    "Use a ferramenta `search_site_content` e o contexto abaixo para responder com precisão em português.\n\n"
                    f"Trecho principal do site:\n{(knowledge.get('markdown_content') or '')[:6000]}"
                ),
                suggested_questions=[
                    f"O que é e como funciona o {title}?",
                    "Quais são os principais serviços, produtos ou seções disponíveis?",
                    "Quais endpoints ou integrações foram encontrados neste site?",
                ],
            )

        pkg = _render_agent_package(knowledge, classification, blueprint)
        artifacts = dict(state.get("artifacts") or {})
        artifacts["agent"] = pkg
        log("✨ [LangGraph] Agente LangGraph gerado com sucesso!")
        return {"artifacts": artifacts}

    workflow = StateGraph(FactoryGraphState)
    workflow.add_node("classify_site", classify_site_node)
    workflow.add_node("generate_mcp", generate_mcp_node)
    workflow.add_node("generate_skill", generate_skill_node)
    workflow.add_node("generate_agent", generate_agent_node)

    workflow.add_edge(START, "classify_site")
    workflow.add_conditional_edges(
        "classify_site",
        route_by_active_type,
        {
            "generate_mcp": "generate_mcp",
            "generate_skill": "generate_skill",
            "generate_agent": "generate_agent",
        },
    )
    workflow.add_edge("generate_mcp", END)
    workflow.add_edge("generate_skill", END)
    workflow.add_edge("generate_agent", END)

    return workflow.compile()


def run_agent_factory(
    knowledge: Dict[str, Any],
    provider: str = "openai",
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    forced_type: str = "auto",
    existing_classification: Optional[Dict[str, Any]] = None,
    existing_artifacts: Optional[Dict[str, Dict[str, Any]]] = None,
    log_callback: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Execute the LangGraph pipeline and return classification + generated artifacts."""
    graph = make_factory_graph(log_callback=log_callback)
    initial_state: FactoryGraphState = {
        "knowledge": knowledge,
        "provider": provider or "openai",
        "model": model or "",
        "api_key": api_key or "",
        "forced_type": forced_type or "auto",
        "classification": existing_classification or {},
        "active_type": "",
        "artifacts": existing_artifacts or {},
        "logs": [],
    }
    final_state = graph.invoke(initial_state)
    return {
        "classification": final_state["classification"],
        "active_type": final_state["active_type"],
        "artifacts": final_state["artifacts"],
    }


def chat_with_site_knowledge(
    knowledge: Dict[str, Any],
    classification: Dict[str, Any],
    user_message: str,
    history: List[Dict[str, str]],
    provider: str = "openai",
    model: Optional[str] = None,
    api_key: Optional[str] = None,
) -> str:
    """Answer user questions in the frontend using the extracted site knowledge and LangChain LLM."""
    md = knowledge.get("markdown_content", "")
    terms = [t.lower() for t in user_message.split() if len(t) > 2]
    paragraphs = [p.strip() for p in md.split("\n\n") if p.strip()]
    scored = sorted(
        paragraphs,
        key=lambda p: sum(1 for term in terms if term in p.lower()),
        reverse=True,
    )
    relevant_excerpts = "\n\n---\n\n".join(scored[:8])[:8000] or md[:6000]

    llm = create_llm(provider=provider, model=model, api_key=api_key, temperature=0.3)
    if llm is None:
        return (
            f"💡 **(Modo Sem API Key Configurada — Busca Direta na Base Extraída)**\n\n"
            f"Encontrei estes trechos relevantes em **{knowledge.get('title', knowledge.get('url'))}**:\n\n"
            f"{relevant_excerpts[:1800]}\n\n"
            f"*(Dica: informe sua API Key da OpenAI, Gemini ou Anthropic no topo da página para respostas geradas pelo LLM)*"
        )

    sys_prompt = (
        f"Você é o Agente Especialista construído a partir do site '{knowledge.get('title')}' ({knowledge.get('url')}).\n"
        f"Resumo do site: {classification.get('summary', '')}\n"
        f"APIs detectadas no site: {json.dumps(knowledge.get('apis', [])[:8], ensure_ascii=False)}\n\n"
        f"Trechos mais relevantes do site para a pergunta atual:\n{relevant_excerpts}\n\n"
        "Responda em português de forma clara, objetiva e baseada nos dados reais extraídos do site."
    )

    messages = [SystemMessage(content=sys_prompt)]
    for h in (history or [])[-6:]:
        role = h.get("role", "user")
        content = h.get("content", "")
        if role == "assistant":
            messages.append(AIMessage(content=content))
        else:
            messages.append(HumanMessage(content=content))
    messages.append(HumanMessage(content=user_message))

    try:
        res = llm.invoke(messages)
        return res.content if isinstance(res.content, str) else str(res.content)
    except Exception as exc:
        return f"⚠️ Erro ao consultar o provedor LLM ({provider}): {exc}\n\nTrecho relevante extraído do site:\n{relevant_excerpts[:1500]}"

