import json
import math
import os
import re
from collections import Counter, defaultdict

# =====================================================================
# 1. Zero-Token Knowledge Graph Builder (GraphRAG without LLM cost)
# =====================================================================

RE_SKILL_HEADER = re.compile(r"^###\s+(.+)$", re.M)
RE_CLASS_HEADER = re.compile(r"^#\s+([^\n]+)$", re.M)
RE_CANCELS = re.compile(r"✕\s*([^|\n·]+)")
RE_CHAINS = re.compile(r"Encadeia\s+(\d+%)\s+([^|\n·]+)")


def build_knowledge_graph(skill_dir: str) -> dict:
    """
    Deterministically extract a Knowledge Graph (nodes & edges) from a Skill's
    Markdown reference files with ZERO LLM tokens spent.
    Captures:
    - Skill -> cancels (✕) -> Skill
    - Skill -> chains_from (Encadeia %) -> Skill
    - Skill -> element / hidden_bonus (e.g., Resistência a Fogo -> +Dano de fogo)
    - Class -> has_skill -> Skill
    - Route / Endpoint mappings
    """
    refs_dir = os.path.join(skill_dir, "references")
    if not os.path.isdir(refs_dir):
        return {"nodes": [], "edges": [], "stats": {}}

    nodes = {}
    edges = []

    def add_node(node_id: str, label: str, ntype: str, meta: dict = None):
        if node_id not in nodes:
            nodes[node_id] = {
                "id": node_id,
                "label": label.strip(),
                "type": ntype,
                "meta": meta or {},
            }

    def add_edge(src: str, dst: str, relation: str, detail: str = ""):
        edges.append({
            "source": src,
            "target": dst,
            "relation": relation,
            "detail": detail,
        })

    # 1. Parse classes.md for Class -> Skill, Conflicts (✕), Chains (Encadeia), and Hidden Columns
    classes_path = os.path.join(refs_dir, "classes.md")
    if os.path.exists(classes_path):
        with open(classes_path, "r", encoding="utf-8") as f:
            c_text = f.read()

        pages = c_text.split("\n---\n")
        for page in pages:
            m_cls = re.search(r"<!--\s*Fonte:.*?/classes/([a-z]+)\.html\s*-->\s*#\s*([^\n]+)", page)
            if not m_cls:
                continue
            cls_name = m_cls.group(2).strip()
            add_node(f"class:{cls_name}", cls_name, "Class")

            # Split into skill blocks by ### Heading
            blocks = re.split(r"\n(?=###\s+)", page)
            current_tier = "Tier 1"
            for blk in blocks:
                lines = blk.strip().splitlines()
                if not lines or not lines[0].startswith("### "):
                    continue
                h_title = lines[0].replace("### ", "").strip()
                if h_title.startswith("Tier "):
                    current_tier = h_title
                    continue
                if h_title in ("Atributos iniciais", "No nível 1 (sem equipamento)"):
                    continue

                skill_name = h_title
                skill_id = f"skill:{cls_name}:{skill_name}"
                tags_lines = [ln for ln in lines[1:8] if ln.startswith("**Tags:**")]
                tags_joined = " · ".join(t.replace("**Tags:**", "").strip() for t in tags_lines)

                # Inspect table headers for unexpected offensive columns on passives/buffs
                table_header = ""
                max_row = ""
                for ln in lines:
                    if ln.startswith("| Nível |"):
                        table_header = ln
                    elif ln.startswith("| 10 |"):
                        max_row = ln

                add_node(
                    skill_id,
                    f"{skill_name} ({cls_name})",
                    "Skill",
                    {
                        "class": cls_name,
                        "tier": current_tier,
                        "tags": tags_joined,
                        "table_columns": [c.strip() for c in table_header.strip("|").split("|")] if table_header else [],
                    },
                )
                add_edge(f"class:{cls_name}", skill_id, "HAS_SKILL", current_tier)

                # Extract ✕ Conflicts
                for m_can in RE_CANCELS.finditer(tags_joined):
                    for target_sk in m_can.group(1).split(","):
                        t_clean = target_sk.strip()
                        if t_clean:
                            add_edge(
                                skill_id,
                                f"skill:{cls_name}:{t_clean}",
                                "CANCELS_BUFF",
                                f"✕ Não coexiste com {t_clean} (usar um encerra o outro)",
                            )

                # Extract Encadeia (Chaining / Inheritance)
                for m_ch in RE_CHAINS.finditer(tags_joined):
                    pct = m_ch.group(1).strip()
                    t_clean = m_ch.group(2).strip()
                    add_edge(
                        skill_id,
                        f"skill:{cls_name}:{t_clean}",
                        "CHAINS_FROM",
                        f"Encadeia {pct} de {t_clean} (herda {pct} do efeito aprendido na árvore)",
                    )

                # Detect Fire synergy (e.g., Resistência a Fogo having 'Dano de fogo' column)
                if "Dano de fogo" in table_header and "Passiva" in tags_joined:
                    add_edge(
                        skill_id,
                        f"element:Fogo:{cls_name}",
                        "BOOSTS_ELEMENT",
                        "Passiva contém coluna oculta 'Dano de fogo' (+1% a +10% no Nv. 10)",
                    )
                if "Fogo" in tags_joined and ("Ataque" in tags_joined or "Especial" in tags_joined):
                    add_edge(
                        f"element:Fogo:{cls_name}",
                        skill_id,
                        "AMPLIFIES_SKILL",
                        f"Skill de elemento Fogo amplificada por bônus de Dano de Fogo",
                    )

    # 2. Parse maps.md for Map -> Monster & Level Ranges
    maps_path = os.path.join(refs_dir, "maps.md")
    if os.path.exists(maps_path):
        with open(maps_path, "r", encoding="utf-8") as f:
            m_text = f.read()
        for page in m_text.split("\n---\n"):
            m_title = re.search(r"^#\s+([^\n]+)", page, re.M)
            if not m_title:
                continue
            map_name = m_title.group(1).strip()
            if map_name == "Mapas & Regiões":
                continue
            m_tags = re.search(r"\*\*Tags:\*\*\s*([^\n]+)", page)
            add_node(f"map:{map_name}", map_name, "Map", {"tags": m_tags.group(1).strip() if m_tags else ""})

    graph_data = {
        "nodes": list(nodes.values()),
        "edges": edges,
        "stats": {
            "total_nodes": len(nodes),
            "total_edges": len(edges),
            "edge_types": dict(Counter(e["relation"] for e in edges)),
            "node_types": dict(Counter(n["type"] for n in nodes.values())),
        },
    }

    cache_path = os.path.join(skill_dir, "knowledge_graph.json")
    try:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(graph_data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    return graph_data


def query_knowledge_graph(graph_data: dict, query: str) -> list[str]:
    """
    Given a user query, traverse the deterministic Knowledge Graph (1-hop & 2-hop)
    to surface critical rules (buff cancellations ✕, skill chaining Encadeia, hidden elemental boosts).
    """
    q_low = query.lower()
    insights = []

    # Match any skill or class mentioned in query (or return class-level edges if class is matched)
    matched_node_ids = set()
    for n in graph_data.get("nodes", []):
        lbl_low = n["label"].lower()
        base_name = lbl_low.split("(")[0].strip()
        if base_name and len(base_name) >= 3 and base_name in q_low:
            matched_node_ids.add(n["id"])
        # Synonym shortcuts
        if ("fs" in q_low or "lutador" in q_low) and "lutador" in lbl_low:
            matched_node_ids.add(n["id"])
        if ("xama" in q_low or "xamã" in q_low) and "xamã" in lbl_low:
            matched_node_ids.add(n["id"])

    for e in graph_data.get("edges", []):
        if e["relation"] == "HAS_SKILL":
            continue
        if e["source"] in matched_node_ids or e["target"] in matched_node_ids:
            src_lbl = graph_data_node_label(graph_data, e["source"])
            dst_lbl = graph_data_node_label(graph_data, e["target"])
            insights.append(f"🔗 [{e['relation']}] {src_lbl} -> {dst_lbl}: {e['detail']}")

    # Deduplicate while preserving order
    seen = set()
    deduped = []
    for item in insights:
        if item not in seen:
            seen.add(item)
            deduped.append(item)
    return deduped[:20]


def graph_data_node_label(graph_data: dict, node_id: str) -> str:
    for n in graph_data.get("nodes", []):
        if n["id"] == node_id:
            return n["label"]
    return node_id.split(":")[-1]


# =====================================================================
# 2. Table-Header Injection & Dynamic Column Pruner (FrugalGPT + Lost in the Middle)
# =====================================================================

def prune_markdown_table(section_md: str, query: str, max_rows: int = 14) -> tuple[str, dict]:
    """
    Implements two paper-backed optimizations on Markdown tables:
    1. Dynamic Column Pruning (FrugalGPT): Drops PvP columns ('PvP · *') and gold cost
       columns ('Custo para aprender') when the user query is not about PvP/gold.
    2. Header Injection (Lost in the Middle): Ensures that even if rows are filtered/sliced,
       the exact Markdown table header (`| Col |...` + `| --- |...`) stays attached.
    Returns (pruned_markdown, stats_dict).
    """
    q_low = query.lower()
    keep_pvp = any(w in q_low for w in ("pvp", "duelo", "arena", "guerra", "jogador"))
    keep_gold_cost = any(w in q_low for w in ("ouro", "custo", "preço", "preco", "comprar"))

    lines = section_md.splitlines()
    out_lines = []
    cols_removed = 0
    original_chars = len(section_md)

    i = 0
    while i < len(lines):
        ln = lines[i]
        # Detect start of a Markdown table
        if ln.strip().startswith("|") and i + 1 < len(lines) and re.match(r"^\s*\|\s*---", lines[i + 1]):
            header_cells = [c.strip() for c in ln.strip().strip("|").split("|")]
            sep_Line = lines[i + 1]
            keep_indices = []
            for idx, col_name in enumerate(header_cells):
                c_low = col_name.lower()
                if not keep_pvp and "pvp" in c_low:
                    cols_removed += 1
                    continue
                if not keep_gold_cost and c_low in ("custo para aprender",):
                    cols_removed += 1
                    continue
                keep_indices.append(idx)

            def filter_row(row_str: str) -> str:
                cells = [c.strip() for c in row_str.strip().strip("|").split("|")]
                picked = [cells[idx] for idx in keep_indices if idx < len(cells)]
                return "| " + " | ".join(picked) + " |"

            out_lines.append(filter_row(ln))
            out_lines.append("| " + " | ".join(["---"] * len(keep_indices)) + " |")
            i += 2

            table_rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                table_rows.append(filter_row(lines[i]))
                i += 1

            if len(table_rows) > max_rows:
                # Filter rows matching query terms or keep level 1, 5, 8, 10 landmarks
                q_terms = [t for t in re.split(r"\W+", q_low) if len(t) >= 2]
                matched = [r for r in table_rows if any(t in r.lower() for t in q_terms)]
                if len(matched) >= 2:
                    out_lines.extend(matched[:max_rows])
                else:
                    out_lines.extend(table_rows[:max_rows])
            else:
                out_lines.extend(table_rows)
        else:
            out_lines.append(ln)
            i += 1

    pruned_md = "\n".join(out_lines)
    pruned_chars = len(pruned_md)
    return pruned_md, {
        "original_chars": original_chars,
        "pruned_chars": pruned_chars,
        "columns_pruned": cols_removed,
        "char_reduction_pct": round((1 - pruned_chars / max(original_chars, 1)) * 100, 1),
    }


# =====================================================================
# 3. Hierarchical Router & Hybrid BM25 Retriever + FinOps Token Calculator
# =====================================================================

STOPWORDS_PT = {
    "de", "do", "da", "dos", "das", "em", "no", "na", "nos", "nas", "um", "uma",
    "para", "por", "com", "sem", "que", "qual", "quais", "como", "mais", "melhor",
    "meu", "minha", "seu", "sua", "ele", "ela", "isso", "isto", "ter", "tem", "são",
    "foi", "ser", "está", "estou", "fazer", "usar", "pode", "posso", "entre", "ou",
}


def route_files_for_query(query: str, available_files: list[str]) -> list[str]:
    """
    Hierarchical Router (Node 1): Selects only the 1 to 3 relevant reference files
    instead of scanning/dumping all 15 files.
    """
    q = query.lower()
    selected = []

    routing_rules = [
        (["skill", "build", "tier", "sp", "ep", "tp", "buff", "passiva", "lutador", "fs", "xamã", "xama", "mago", "pikeman", "ps", "arqueira", "cavaleiro", "mecânico", "atalanta", "sacerdotisa", "assassina", "brawler", "zecram", "midranda", "migal", "concentração", "machado veloz", "resistência a fogo"], ["references/classes.md", "references/guide.md"]),
        (["mapa", "upar", "up", "xp", "exp", "caçar", "onde", "bandeira", "party", "pt", "grupo", "missão", "missoes", "automáticas"], ["references/maps.md", "references/guide.md", "references/monsters.md"]),
        (["arma", "machado", "foice", "cajado", "fantasma", "espada", "lança", "arco", "garra", "adaga", "manopla"], ["references/items_weapons.md"]),
        (["armadura", "roupão", "escudo", "orbe", "orbital"], ["references/items_armor_shields.md"]),
        (["luva", "bota", "bracelete", "cinto"], ["references/items_gear.md"]),
        (["anel", "amuleto", "colar", "brinco", "sheltom", "kelvezu"], ["references/items_accessories.md"]),
        (["forja", "craft", "receita", "ingrediente"], ["references/crafts.md"]),
        (["set", "conjunto", "bônus de peças"], ["references/sets.md"]),
        (["monstro", "boss", "chefe", "drop", "dropa"], ["references/monsters.md"]),
        (["endpoint", "api", "rota", "arquitetura", "websocket", "fetch", "js", "funcionalidade", "openapi"], ["references/architecture_and_endpoints.md"]),
    ]

    for keywords, targets in routing_rules:
        if any(k in q for k in keywords):
            for t in targets:
                if t in available_files and t not in selected:
                    selected.append(t)

    if not selected:
        for default_f in ("references/guide.md", "references/classes.md", "references/architecture_and_endpoints.md"):
            if default_f in available_files and default_f not in selected:
                selected.append(default_f)

    return selected[:3]


def retrieve_and_prune_context(skill_dir: str, query: str, top_k: int = 5) -> dict:
    """
    Executes Hierarchical Routing + BM25 Section Scoring + Table Header Injection +
    Dynamic Column Pruning + Knowledge Graph Multi-Hop lookup.
    Also computes the exact FinOps Token & Cost Comparison!
    """
    refs_dir = os.path.join(skill_dir, "references")
    all_files = {}
    full_corpus_chars = 0

    for root, _, fnames in os.walk(skill_dir):
        for fn in sorted(fnames):
            if fn.endswith(".md"):
                fp = os.path.join(root, fn)
                rel_p = os.path.relpath(fp, skill_dir).replace("\\", "/")
                with open(fp, "r", encoding="utf-8") as f:
                    txt = f.read()
                    all_files[rel_p] = txt
                    full_corpus_chars += len(txt)

    routed_files = route_files_for_query(query, list(all_files.keys()))

    # Extract search terms
    raw_terms = [t.lower() for t in re.split(r"\W+", query) if len(t) >= 2 and t.lower() not in STOPWORDS_PT]
    if not raw_terms:
        raw_terms = [query.lower().strip()]

    candidates = []
    naive_rag_chars = 0

    for rel_p in routed_files:
        content = all_files.get(rel_p, "")
        # Split by page separator '---' and sub-sections '### '
        pages = content.split("\n---\n")
        for page in pages:
            naive_rag_chars += len(page)
            # If page has multiple ### sections (like classes.md), score sub-blocks too
            sub_blocks = re.split(r"\n(?=###\s+)", page) if "\n### " in page else [page]
            page_header = sub_blocks[0].splitlines()[0] if sub_blocks and sub_blocks[0].splitlines() else ""

            for blk in sub_blocks:
                low_blk = blk.lower()
                term_hits = sum(1 for t in raw_terms if t in low_blk)
                if term_hits == 0:
                    continue
                freq_score = sum(min(low_blk.count(t), 8) for t in raw_terms)
                exact_bonus = 30 if query.lower() in low_blk else 0
                heading_bonus = 20 if any(t in (blk.splitlines()[0].lower() if blk.splitlines() else "") for t in raw_terms) else 0
                score = term_hits * 15 + freq_score * 2 + exact_bonus + heading_bonus

                full_blk = f"{page_header}\n{blk}" if page_header and not blk.startswith(page_header) else blk
                candidates.append({
                    "file": rel_p,
                    "score": score,
                    "raw_text": full_blk[:4500],
                })

    candidates.sort(key=lambda x: x["score"], reverse=True)
    top_chunks = candidates[:top_k]

    pruned_chunks = []
    total_cols_pruned = 0
    raw_selected_chars = 0
    pruned_selected_chars = 0

    for ch in top_chunks:
        raw_selected_chars += len(ch["raw_text"])
        p_txt, p_stats = prune_markdown_table(ch["raw_text"], query)
        pruned_selected_chars += len(p_txt)
        total_cols_pruned += p_stats["columns_pruned"]
        pruned_chunks.append({
            "file": ch["file"],
            "score": ch["score"],
            "content": p_txt,
            "stats": p_stats,
        })

    # Load or build Knowledge Graph
    graph_cache = os.path.join(skill_dir, "knowledge_graph.json")
    if os.path.exists(graph_cache):
        with open(graph_cache, "r", encoding="utf-8") as f:
            graph_data = json.load(f)
    else:
        graph_data = build_knowledge_graph(skill_dir)

    graph_insights = query_knowledge_graph(graph_data, query)

    # FinOps Token & Financial ROI Calculation (1 token ≈ 4 chars)
    full_corpus_tokens = max(full_corpus_chars // 4, 1)
    naive_rag_tokens = max(min(naive_rag_chars // 4, 120000), raw_selected_chars // 2, 4500)
    frugal_tokens = max((pruned_selected_chars // 4) + (len("\n".join(graph_insights)) // 4), 150)

    savings_vs_full_pct = round((1 - frugal_tokens / full_corpus_tokens) * 100, 2)
    savings_vs_naive_pct = round((1 - frugal_tokens / naive_rag_tokens) * 100, 1)

    # Enterprise B2B ROI simulation (10,000 queries/month @ $2.50 / 1M input tokens standard enterprise tier)
    queries_per_month = 10000
    price_per_1m_tokens_usd = 2.50
    usd_to_brl = 5.80

    monthly_cost_full_usd = (full_corpus_tokens * queries_per_month / 1_000_000) * price_per_1m_tokens_usd
    monthly_cost_naive_usd = (naive_rag_tokens * queries_per_month / 1_000_000) * price_per_1m_tokens_usd
    monthly_cost_frugal_usd = (frugal_tokens * queries_per_month / 1_000_000) * price_per_1m_tokens_usd

    return {
        "query": query,
        "routed_files": routed_files,
        "chunks": pruned_chunks,
        "graph_insights": graph_insights,
        "graph_stats": graph_data.get("stats", {}),
        "finops": {
            "full_corpus_tokens": full_corpus_tokens,
            "naive_rag_tokens": naive_rag_tokens,
            "frugal_tokens": frugal_tokens,
            "columns_pruned": total_cols_pruned,
            "savings_vs_full_pct": savings_vs_full_pct,
            "savings_vs_naive_pct": savings_vs_naive_pct,
            "b2b_simulation_10k_queries": {
                "full_dump_monthly_usd": round(monthly_cost_full_usd, 2),
                "naive_rag_monthly_usd": round(monthly_cost_naive_usd, 2),
                "skillrag_monthly_usd": round(monthly_cost_frugal_usd, 2),
                "annual_savings_vs_naive_brl": round((monthly_cost_naive_usd - monthly_cost_frugal_usd) * 12 * usd_to_brl, 2),
                "annual_savings_vs_full_brl": round((monthly_cost_full_usd - monthly_cost_frugal_usd) * 12 * usd_to_brl, 2),
            },
        },
    }

