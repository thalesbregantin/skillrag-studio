import ipaddress
import json
import re
import socket
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse

import requests
import urllib3
from bs4 import BeautifulSoup

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# =====================================================================
# 1. OWASP Security Guardrails: Anti-SSRF & Prompt Injection Sanitizer
# =====================================================================

PROMPT_INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(all\s+)?(previous|prior)\s+instructions", re.I),
    re.compile(r"system\s*prompt", re.I),
    re.compile(r"you\s+are\s+now\s+in\s+developer\s+mode", re.I),
    re.compile(r"disregard\s+(all\s+)?prior\s+rules", re.I),
]


def validate_url_no_ssrf(url: str) -> str:
    """
    Validate that a target URL uses http/https and does not resolve to
    loopback, link-local, cloud metadata (169.254.169.254), or private IPs.
    """
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Esquema de URL não permitido: {parsed.scheme}")
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("URL inválida: hostname ausente.")

    if hostname.lower() in ("localhost", "0.0.0.0", "metadata.google.internal"):
        raise ValueError(f"Bloqueado por proteção Anti-SSRF: {hostname}")

    try:
        addr_info = socket.getaddrinfo(hostname, None)
        for item in addr_info:
            ip_str = item[4][0]
            ip_obj = ipaddress.ip_address(ip_str)
            if (
                ip_obj.is_loopback
                or ip_obj.is_private
                or ip_obj.is_link_local
                or ip_obj.is_multicast
                or ip_obj.is_reserved
            ):
                raise ValueError(f"Bloqueado por proteção Anti-SSRF (IP privado/interno): {ip_str}")
    except socket.gaierror:
        # DNS resolution may be handled by proxy or fail offline; allow caller to handle connection error
        pass

    return url.strip()


def scan_html_for_prompt_injection(soup: BeautifulSoup) -> list[dict]:
    """
    Scan hidden HTML elements (display:none, visibility:hidden, opacity:0)
    and text for potential Indirect Prompt Injection (OWASP LLM01).
    """
    alerts = []
    for el in soup.find_all(style=True):
        style_str = (el.get("style") or "").lower().replace(" ", "")
        if "display:none" in style_str or "visibility:hidden" in style_str or "opacity:0" in style_str:
            hidden_text = el.get_text(" ", strip=True)
            for pat in PROMPT_INJECTION_PATTERNS:
                if pat.search(hidden_text):
                    alerts.append({
                        "type": "hidden_dom_injection",
                        "tag": el.name,
                        "snippet": hidden_text[:160],
                    })
    return alerts


# =====================================================================
# 2. Static JS & HTML Endpoint / Feature Extractor (Zero-Token AST/Regex)
# =====================================================================

REGEX_FETCH = re.compile(
    r"""\bfetch\s*\(\s*(?:[`'"])([^`'"]+)(?:[`'"])""",
    re.I,
)
REGEX_FETCH_TEMPLATE = re.compile(
    r"""\bfetch\s*\(\s*`([^`]+)`""",
    re.I,
)
REGEX_AXIOS = re.compile(
    r"""\baxios\s*\.\s*(get|post|put|delete|patch|request)\s*\(\s*[`'"]([^`'"]+)[`'"]""",
    re.I,
)
REGEX_XHR = re.compile(
    r"""\.open\s*\(\s*['"](GET|POST|PUT|DELETE|PATCH)['"]\s*,\s*[`'"]([^`'"]+)[`'"]""",
    re.I,
)
REGEX_WEBSOCKET = re.compile(
    r"""(?:new\s+WebSocket\s*\(\s*[`'"]([^`'"]+)[`'"]|(['"](?:wss?)://[^'"]+['"]))""",
    re.I,
)
REGEX_EVENTSOURCE = re.compile(
    r"""new\s+EventSource\s*\(\s*[`'"]([^`'"]+)[`'"]""",
    re.I,
)
REGEX_API_PATHS = re.compile(
    r"""[`'"]((?:/api/|/v[1-9]/|/auth/|/graphql|/ws|/socket\.io)[^`'"\s<>]{1,120})[`'"]""",
    re.I,
)
REGEX_JSON_DATA = re.compile(
    r"""[`'"]([a-zA-Z0-9_./-]+\.json(?:\?[^`'"]*)?)[`'"]""",
    re.I,
)
REGEX_STORAGE = re.compile(
    r"""\b(localStorage|sessionStorage)\s*\.\s*(getItem|setItem|removeItem)\s*\(\s*['"]([^'"]+)['"]""",
)


def _extract_js_intelligence(js_text: str, source_name: str) -> dict:
    """Extract API calls, WebSockets, JSON data files, and storage keys from JS code."""
    endpoints = []
    websockets = set()
    json_files = set()
    storage_keys = set()

    for m in REGEX_FETCH.finditer(js_text):
        endpoints.append({"method": "FETCH", "path": m.group(1), "source": source_name})
    for m in REGEX_FETCH_TEMPLATE.finditer(js_text):
        endpoints.append({"method": "FETCH (Template)", "path": m.group(1), "source": source_name})
    for m in REGEX_AXIOS.finditer(js_text):
        endpoints.append({"method": m.group(1).upper(), "path": m.group(2), "source": source_name})
    for m in REGEX_XHR.finditer(js_text):
        endpoints.append({"method": m.group(1).upper(), "path": m.group(2), "source": source_name})
    for m in REGEX_EVENTSOURCE.finditer(js_text):
        endpoints.append({"method": "SSE (EventSource)", "path": m.group(1), "source": source_name})
    for m in REGEX_API_PATHS.finditer(js_text):
        endpoints.append({"method": "API_PATH", "path": m.group(1), "source": source_name})

    for m in REGEX_WEBSOCKET.finditer(js_text):
        ws_val = (m.group(1) or m.group(2) or "").strip("'\"`")
        if ws_val:
            websockets.add((ws_val, source_name))

    for m in REGEX_JSON_DATA.finditer(js_text):
        val = m.group(1)
        if not val.endswith("package.json") and not val.endswith("tsconfig.json"):
            json_files.add((val, source_name))

    for m in REGEX_STORAGE.finditer(js_text):
        storage_keys.add(f"{m.group(1)}.{m.group(2)}('{m.group(3)}')")

    return {
        "endpoints": endpoints,
        "websockets": list(websockets),
        "json_files": list(json_files),
        "storage_keys": sorted(storage_keys),
    }


def scan_website_architecture(entry_url: str, max_pages: int = 35, log_callback=None) -> dict:
    """
    Scan a target website to map its pages, UI features, JavaScript bundles,
    REST/Fetch/XHR/SSE/WebSocket endpoints, security headers, and OpenAPI schema.
    """
    def log(msg: str):
        if log_callback:
            log_callback(msg)

    safe_url = validate_url_no_ssrf(entry_url)
    parsed_entry = urlparse(safe_url)
    base_origin = f"{parsed_entry.scheme}://{parsed_entry.netloc}"

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) SkillRAG-Scanner/1.0"
    })

    # Seed common entry points (root + entry_url)
    seed_urls = [base_origin + "/", safe_url]
    visited_pages = {}
    js_urls = set()
    to_visit = []
    for u in seed_urls:
        if u not in to_visit:
            to_visit.append(u)

    security_headers = {}
    prompt_injection_alerts = []
    all_forms = []
    ui_features = []
    inline_js_intel = []

    log(f"🔍 Iniciando mapeamento de arquitetura e endpoints em {base_origin}...")

    while to_visit and len(visited_pages) < max_pages:
        batch = []
        while to_visit and len(batch) < 10 and (len(visited_pages) + len(batch)) < max_pages:
            u = to_visit.pop(0)
            if u not in visited_pages:
                visited_pages[u] = None
                batch.append(u)

        if not batch:
            break

        def fetch_html(u):
            r = session.get(u, timeout=12, verify=False)
            r.encoding = "utf-8"
            return u, r

        with ThreadPoolExecutor(max_workers=8) as pool:
            futs = [pool.submit(fetch_html, u) for u in batch]
            for fut in as_completed(futs):
                try:
                    u, resp = fut.result()
                except Exception:
                    continue

                if not security_headers and resp.headers:
                    security_headers = {
                        "Server": resp.headers.get("Server", "N/A"),
                        "Content-Security-Policy": "Presente" if "Content-Security-Policy" in resp.headers else "Ausente",
                        "X-Frame-Options": resp.headers.get("X-Frame-Options", "Ausente"),
                        "Strict-Transport-Security": "Presente" if "Strict-Transport-Security" in resp.headers else "Ausente",
                        "Access-Control-Allow-Origin": resp.headers.get("Access-Control-Allow-Origin", "N/A"),
                    }

                ctype = resp.headers.get("Content-Type", "")
                if "text/html" not in ctype and not u.endswith(".html"):
                    continue

                soup = BeautifulSoup(resp.text, "html.parser")
                title = soup.title.get_text(strip=True) if soup.title else urlparse(u).path or "/"
                rel_path = urlparse(u).path or "/"

                # Check for Indirect Prompt Injection
                inj = scan_html_for_prompt_injection(soup)
                if inj:
                    prompt_injection_alerts.extend(inj)

                # Collect UI features on this page
                forms = soup.find_all("form")
                inputs = soup.find_all(["input", "select", "textarea"])
                canvases = soup.find_all("canvas")
                tables = soup.find_all("table")
                buttons = soup.find_all("button")
                dialogs = soup.find_all(["dialog"]) + soup.find_all(class_=re.compile(r"modal|popup|drawer", re.I))

                feat_tags = []
                if canvases:
                    feat_tags.append(f"Canvas 2D/3D ({len(canvases)})")
                if forms:
                    feat_tags.append(f"Formulários ({len(forms)})")
                if inputs:
                    feat_tags.append(f"Inputs/Filtros ({len(inputs)})")
                if tables:
                    feat_tags.append(f"Tabelas ({len(tables)})")
                if buttons:
                    feat_tags.append(f"Botões ({len(buttons)})")
                if dialogs:
                    feat_tags.append(f"Modais/Dialogs ({len(dialogs)})")

                visited_pages[u] = {
                    "url": u,
                    "path": rel_path,
                    "title": title,
                    "features": ", ".join(feat_tags) if feat_tags else "Página de Conteúdo",
                }

                for f in forms:
                    action = f.get("action") or rel_path
                    method = (f.get("method") or "GET").upper()
                    field_names = [
                        inp.get("name") or inp.get("id") or inp.get("type") or "unnamed"
                        for inp in f.find_all(["input", "select", "textarea"])
                    ]
                    all_forms.append({
                        "page": rel_path,
                        "method": method,
                        "action": action,
                        "fields": field_names,
                    })

                # Collect interactive IDs/widgets
                for el in soup.find_all(id=True):
                    el_id = el.get("id")
                    if el.name in ("canvas", "form", "input", "select", "dialog") or any(
                        k in el_id.lower() for k in ("search", "filter", "login", "auth", "chat", "map", "calc", "rank", "market", "game")
                    ):
                        ui_features.append({
                            "page": rel_path,
                            "tag": f"<{el.name} id='{el_id}'>",
                            "placeholder_or_label": el.get("placeholder") or el.get("aria-label") or el.get_text(" ", strip=True)[:50],
                        })

                # Collect external and inline scripts
                for sc in soup.find_all("script"):
                    src = sc.get("src")
                    if src:
                        full_js = urljoin(u, src)
                        if urlparse(full_js).netloc == parsed_entry.netloc:
                            js_urls.add(full_js)
                    else:
                        code = sc.string or sc.get_text() or ""
                        if len(code.strip()) > 20:
                            inline_js_intel.append(_extract_js_intelligence(code, f"inline@{rel_path}"))

                # Discover top-level navigation links (avoiding 1,500+ individual item subpages)
                for a in soup.find_all("a", href=True):
                    href = a["href"].split("#")[0].strip()
                    if not href or href.startswith(("javascript:", "mailto:", "tel:")):
                        continue
                    full_link = urljoin(u, href)
                    p_link = urlparse(full_link)
                    if p_link.netloc != parsed_entry.netloc:
                        continue
                    # Prioritize top-level pages and category indices to map site breadth
                    depth = len([seg for seg in p_link.path.strip("/").split("/") if seg])
                    if depth <= 1 and full_link not in visited_pages and full_link not in to_visit:
                        to_visit.append(full_link)

    # Fetch and scan all discovered JavaScript files
    log(f"📜 Analisando {len(js_urls)} pacotes JavaScript em busca de endpoints, APIs e WebSockets...")
    js_bundles_report = []
    all_endpoints = []
    all_websockets = set()
    all_json_files = set()
    all_storage_keys = set()

    for item in inline_js_intel:
        all_endpoints.extend(item["endpoints"])
        all_websockets.update(item["websockets"])
        all_json_files.update(item["json_files"])
        all_storage_keys.update(item["storage_keys"])

    def fetch_js(js_u):
        r = session.get(js_u, timeout=15, verify=False)
        r.encoding = "utf-8"
        return js_u, r.text

    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = [pool.submit(fetch_js, ju) for ju in sorted(js_urls)]
        for fut in as_completed(futs):
            try:
                ju, js_code = fut.result()
                short_name = urlparse(ju).path
                intel = _extract_js_intelligence(js_code, short_name)
                js_bundles_report.append({
                    "path": short_name,
                    "size_kb": round(len(js_code) / 1024, 1),
                    "endpoints_found": len(intel["endpoints"]),
                    "websockets_found": len(intel["websockets"]),
                })
                all_endpoints.extend(intel["endpoints"])
                all_websockets.update(intel["websockets"])
                all_json_files.update(intel["json_files"])
                all_storage_keys.update(intel["storage_keys"])
            except Exception:
                continue

    # Deduplicate endpoints
    unique_endpoints = {}
    for ep in all_endpoints:
        key = (ep["method"], ep["path"])
        if key not in unique_endpoints:
            unique_endpoints[key] = {"method": ep["method"], "path": ep["path"], "sources": {ep["source"]}}
        else:
            unique_endpoints[key]["sources"].add(ep["source"])

    deduped_endpoints = [
        {
            "method": v["method"],
            "path": v["path"],
            "sources": ", ".join(sorted(v["sources"])),
        }
        for v in sorted(unique_endpoints.values(), key=lambda x: (x["path"], x["method"]))
    ]

    # Build OpenAPI 3.0 skeleton from discovered endpoints
    openapi_paths = {}
    for ep in deduped_endpoints:
        p = ep["path"]
        if not p.startswith("/") and not p.startswith("http"):
            p = "/" + p
        m_low = ep["method"].split()[0].lower()
        if m_low not in ("get", "post", "put", "delete", "patch"):
            m_low = "get"
        openapi_paths.setdefault(p, {})[m_low] = {
            "summary": f"Discovered via static analysis in {ep['sources']}",
            "responses": {"200": {"description": "Discovered endpoint response"}},
        }

    openapi_spec = {
        "openapi": "3.0.3",
        "info": {
            "title": f"Discovered API Architecture — {parsed_entry.netloc}",
            "version": "1.0.0",
            "description": f"Automatically reverse-engineered from HTML/JS bundles of {base_origin} (Zero-Token AST/Regex Scanner).",
        },
        "servers": [{"url": base_origin}],
        "paths": openapi_paths,
    }

    # Generate Markdown report (`architecture_and_endpoints.md`)
    md_lines = [
        "# 🗺️ Arquitetura do Site, Funcionalidades & Endpoints Descobertos",
        "",
        f"> Mapeamento estático automatizado (**Zero-Token Scanner**) de `{base_origin}`.",
        f"> Total de páginas raiz analisadas: **{len([v for v in visited_pages.values() if v])}** | Pacotes JS analisados: **{len(js_bundles_report)}** | Endpoints/Chamadas descobertas: **{len(deduped_endpoints)}**",
        "",
        "---",
        "",
        "## 1. 🔌 Endpoints de API, Fetch & Rotas de Dados Descobertos no Front-End",
        "",
    ]

    if deduped_endpoints:
        md_lines.extend([
            "| Método / Tipo | Endpoint / Rota | Encontrado em (Script / Página) |",
            "| :--- | :--- | :--- |",
        ])
        for ep in deduped_endpoints:
            md_lines.append(f"| `{ep['method']}` | `{ep['path']}` | `{ep['sources']}` |")
    else:
        md_lines.append("*Nenhuma chamada explícita `fetch`/`axios`/`XHR` detectada nos pacotes públicos.*")

    md_lines.extend([
        "",
        "---",
        "",
        "## 2. ⚡ Conexões Real-Time (WebSockets) & Arquivos JSON",
        "",
    ])
    if all_websockets:
        md_lines.append("### WebSockets (`ws://` / `wss://`)")
        for ws_url, src in sorted(all_websockets):
            md_lines.append(f"- **`{ws_url}`** *(em `{src}`)*")
    else:
        md_lines.append("- *Nenhum WebSocket hardcoded nos scripts analisados.*")

    if all_json_files:
        md_lines.append("")
        md_lines.append("### Arquivos / Endpoints `.json` Referenciados")
        for jf, src in sorted(all_json_files):
            md_lines.append(f"- **`{jf}`** *(em `{src}`)*")

    md_lines.extend([
        "",
        "---",
        "",
        "## 3. 📜 Pacotes JavaScript (Bundles Client-Side)",
        "",
        "| Arquivo JS | Tamanho (KB) | Chamadas de Rede | WebSockets |",
        "| :--- | :---: | :---: | :---: |",
    ])
    for jb in sorted(js_bundles_report, key=lambda x: x["size_kb"], reverse=True):
        md_lines.append(
            f"| `{jb['path']}` | `{jb['size_kb']} KB` | `{jb['endpoints_found']}` | `{jb['websockets_found']}` |"
        )

    md_lines.extend([
        "",
        "---",
        "",
        "## 4. 🖥️ Mapa de Telas & Funcionalidades Interativas (DOM)",
        "",
        "| Rota | Título da Página | Componentes Detectados |",
        "| :--- | :--- | :--- |",
    ])
    for _, pinfo in sorted(visited_pages.items()):
        if not pinfo:
            continue
        md_lines.append(f"| `{pinfo['path']}` | {pinfo['title']} | {pinfo['features']} |")

    if ui_features:
        md_lines.extend([
            "",
            "### Componentes & Widgets Interativos Principais",
            "",
            "| Página | Elemento DOM | Descrição / Placeholder |",
            "| :--- | :--- | :--- |",
        ])
        seen_ui = set()
        for uf in ui_features:
            k = (uf["page"], uf["tag"])
            if k in seen_ui:
                continue
            seen_ui.add(k)
            md_lines.append(f"| `{uf['page']}` | `{uf['tag']}` | {uf['placeholder_or_label'] or '-'} |")

    if all_storage_keys:
        md_lines.extend([
            "",
            "---",
            "",
            "## 5. 💾 Estado no Navegador (`localStorage` / `sessionStorage`)",
            "",
        ])
        for sk in sorted(all_storage_keys)[:50]:
            md_lines.append(f"- `{sk}`")

    md_lines.extend([
        "",
        "---",
        "",
        "## 6. 🛡️ Postura de Segurança & Guardrails (OWASP LLM01 / Web)",
        "",
        "### Headers HTTP do Servidor",
    ])
    for hk, hv in security_headers.items():
        md_lines.append(f"- **{hk}:** `{hv}`")

    md_lines.append("")
    md_lines.append("### Varredura Anti-Indirect Prompt Injection (DOM Oculto)")
    if prompt_injection_alerts:
        for al in prompt_injection_alerts:
            md_lines.append(f"- ⚠️ **Alerta (`{al['type']}`):** `<{al['tag']}>` -> `{al['snippet']}`")
    else:
        md_lines.append("- ✅ **Limpo:** Nenhuma instrução oculta maliciosa (`display:none` / `opacity:0` com comando de override) encontrada no HTML.")

    markdown_report = "\n".join(md_lines) + "\n"

    return {
        "base_origin": base_origin,
        "pages_scanned": len([v for v in visited_pages.values() if v]),
        "js_bundles_count": len(js_bundles_report),
        "endpoints_count": len(deduped_endpoints),
        "endpoints": deduped_endpoints,
        "websockets": [w[0] for w in sorted(all_websockets)],
        "security_headers": security_headers,
        "prompt_injection_alerts": prompt_injection_alerts,
        "markdown": markdown_report,
        "openapi": openapi_spec,
    }


def scan_skill_directory_routes(skill_dir: str) -> dict:
    """
    Zero-Token Offline/Instant Route & Endpoint Mapper:
    Scans all <!-- Fonte: URL --> markers and internal routes across .claude/skills/<skill>/references/*.md
    to build a complete map of the website's URL structure, route families, and parameterized endpoints.
    """
    import os
    from collections import Counter, defaultdict

    refs_dir = os.path.join(skill_dir, "references")
    if not os.path.isdir(refs_dir):
        return {"total_urls": 0, "route_families": {}, "markdown": "", "openapi": {}}

    source_re = re.compile(r"<!--\s*Fonte:\s*(https?://[^\s>]+)\s*-->")
    all_urls = []
    route_families = defaultdict(list)
    base_origin = "https://www.risingtale.com"

    for fn in sorted(os.listdir(refs_dir)):
        if not fn.endswith(".md"):
            continue
        fp = os.path.join(refs_dir, fn)
        with open(fp, "r", encoding="utf-8") as f:
            text = f.read()
        for m in source_re.finditer(text):
            u = m.group(1).strip()
            all_urls.append((u, fn))
            p = urlparse(u)
            base_origin = f"{p.scheme}://{p.netloc}"
            parts = [seg for seg in p.path.strip("/").split("/") if seg]
            if len(parts) <= 1:
                pattern = "/" + (parts[0] if parts else "")
            else:
                pattern = f"/{parts[0]}/{{slug}}.html"
            route_families[pattern].append({"url": u, "path": p.path, "ref_file": fn})

    # Build OpenAPI 3.0 spec for all discovered route patterns
    openapi_paths = {}
    for pattern, items in sorted(route_families.items()):
        sample = items[0]
        params = []
        if "{slug}" in pattern:
            params.append({
                "name": "slug",
                "in": "path",
                "required": True,
                "schema": {"type": "string"},
                "description": f"Entity slug ({len(items)} routes mapped in `{sample['ref_file']}`)",
            })
        openapi_paths[pattern] = {
            "get": {
                "summary": f"Route family {pattern} ({len(items)} pages mapped -> {sample['ref_file']})",
                "parameters": params,
                "responses": {
                    "200": {
                        "description": f"HTML/Data view mapped into references/{sample['ref_file']}",
                    }
                },
            }
        }

    openapi_spec = {
        "openapi": "3.0.3",
        "info": {
            "title": f"Complete Site Route & Endpoint Map — {urlparse(base_origin).netloc}",
            "version": "1.0.0",
            "description": f"Zero-Token mapping of {len(all_urls)} routes across {len(route_families)} route families.",
        },
        "servers": [{"url": base_origin}],
        "paths": openapi_paths,
    }

    md_lines = [
        "# 🗺️ Mapa Completo de Rotas, Endpoints & Arquitetura do Site",
        "",
        f"> Mapeamento determinístico (**Zero-Token Scanner**) de `{base_origin}`.",
        f"> Total de páginas/rotas mapeadas: **{len(all_urls)}** | Famílias de Endpoints/Rotas: **{len(route_families)}**",
        "",
        "## 1. 🔌 Famílias de Rotas & Endpoints Mapeados (OpenAPI Ready)",
        "",
        "| Padrão de Rota (Endpoint) | Quantidade de Páginas | Arquivo de Referência (Skill) | Exemplo Real |",
        "| :--- | :---: | :--- | :--- |",
    ]
    for pattern, items in sorted(route_families.items(), key=lambda x: len(x[1]), reverse=True):
        ex_path = items[0]["path"]
        ref_f = items[0]["ref_file"]
        md_lines.append(f"| `{pattern}` | **{len(items)}** | `references/{ref_f}` | `{ex_path}` |")

    return {
        "base_origin": base_origin,
        "total_urls": len(all_urls),
        "route_families_count": len(route_families),
        "route_families": {k: len(v) for k, v in route_families.items()},
        "markdown": "\n".join(md_lines) + "\n",
        "openapi": openapi_spec,
    }


if __name__ == "__main__":
    res = scan_skill_directory_routes(".claude/skills/risingtale-wiki")
    print(f"✅ Rotas totais mapeadas na Skill: {res['total_urls']} em {res['route_families_count']} famílias de endpoints!")


