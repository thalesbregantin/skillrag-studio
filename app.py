from flask import Flask, render_template, request, send_file, Response, jsonify
import os
import shutil
import uuid
import queue
import threading
import time
import zipfile
import gc
from downloader import WebsiteDownloader, zip_directory, get_site_name
from skill_builder import build_wiki_skill_package, normalize_wiki_url
from endpoint_scanner import (
    scan_website_architecture,
    scan_skill_directory_routes,
    validate_url_no_ssrf,
)
from agentic_rag.indexing import build_knowledge_graph
from agentic_rag.workflow import run_agentic_rag_workflow
from agentic_rag.evals.runner import run_golden_evaluation

app = Flask(__name__)

DOWNLOAD_FOLDER = 'downloads'
if not os.path.exists(DOWNLOAD_FOLDER):
    os.makedirs(DOWNLOAD_FOLDER)

# Tunable retention windows (seconds)
COMPLETE_TTL = 1800       # complete sessions (zip waiting for download)
ERROR_TTL = 600           # error sessions
PROCESSING_TTL = 1800     # safety net for stuck/zombie sessions
ORPHAN_FILE_TTL = 1800    # files on disk with no matching session
CLEANUP_INTERVAL = 300    # how often the janitor runs


# Per-session state. Always touch via session_lock when iterating/mutating.
message_queues = {}
download_results = {}
session_lock = threading.Lock()


def _write_skill_zip(skill_name: str, files_dict: dict, zip_path: str):
    """Write SKILL.md and references/*.md into a clean .zip archive."""
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for rel_path, content in files_dict.items():
            data = content.encode('utf-8') if isinstance(content, str) else content
            zf.writestr(f"{skill_name}/{rel_path}", data)


def _purge_session(session_id):
    """Remove a single session's in-memory state and any disk artifacts."""
    with session_lock:
        result = download_results.pop(session_id, None)
        message_queues.pop(session_id, None)

    if not result:
        return

    for key in ('zip_path', 'skill_zip_path'):
        p = result.get(key)
        if p and os.path.exists(p):
            try:
                os.remove(p)
            except Exception:
                pass

    raw_dir = os.path.join(DOWNLOAD_FOLDER, session_id)
    if os.path.isdir(raw_dir):
        try:
            shutil.rmtree(raw_dir)
        except Exception:
            pass


def cleanup_abandoned_sessions():
    """Janitor thread: removes complete/error/zombie sessions."""
    while True:
        time.sleep(CLEANUP_INTERVAL)
        try:
            now = time.time()
            to_remove = []

            with session_lock:
                snapshot = list(download_results.items())

            for session_id, result in snapshot:
                status = result.get('status')
                created_at = result.get('created_at') or result.get('started_at') or 0
                if not created_at:
                    continue
                age = now - created_at

                if status == 'complete' and age > COMPLETE_TTL:
                    to_remove.append((session_id, 'complete'))
                elif status == 'error' and age > ERROR_TTL:
                    to_remove.append((session_id, 'error'))
                elif status == 'processing' and age > PROCESSING_TTL:
                    to_remove.append((session_id, 'zombie'))

            for session_id, reason in to_remove:
                _purge_session(session_id)
                print(f"🧹 Sessão {session_id[:8]} removida ({reason})")

            gc.collect()
        except Exception as e:
            print(f"⚠️ Erro no janitor: {e}")


threading.Thread(target=cleanup_abandoned_sessions, daemon=True).start()


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/health')
def health():
    info = {'status': 'ok'}
    with session_lock:
        info['sessions'] = len(download_results)
        info['queues'] = len(message_queues)
    return jsonify(info)


@app.route('/start-download', methods=['POST'])
def start_download():
    """Start download + mechanical Skill extraction and return session ID for SSE."""
    data = request.get_json(silent=True) or {}
    raw_url = (data.get('url') or '').strip()

    if not raw_url:
        return jsonify({'error': 'URL is required'}), 400

    url = normalize_wiki_url(raw_url)
    mode = (data.get('mode') or 'skill_and_replica').strip()

    session_id = str(uuid.uuid4())
    now = time.time()

    with session_lock:
        message_queues[session_id] = queue.Queue()
        download_results[session_id] = {
            'status': 'processing',
            'zip_path': None,
            'skill_zip_path': None,
            'filename': None,
            'started_at': now,
        }

    thread = threading.Thread(
        target=process_download,
        args=(session_id, url, mode),
        daemon=True,
    )
    thread.start()

    return jsonify({'session_id': session_id})


def process_download(session_id, url, mode):
    """Background worker: builds the mechanical Skill package and/or downloads the site replica."""
    with session_lock:
        q = message_queues.get(session_id)
    if q is None:
        return

    download_dir = os.path.join(DOWNLOAD_FOLDER, session_id)
    zip_path = os.path.join(DOWNLOAD_FOLDER, f"{session_id}.zip")
    skill_zip_path = os.path.join(DOWNLOAD_FOLDER, f"{session_id}_skill.zip")

    def log_callback(message):
        q.put(message)

    downloader = None
    try:
        site_name = get_site_name(url)
        zip_filename = f"{site_name}.zip"
        skill_pkg = None

        # 1. Build mechanical Skill package (SKILL.md + references/*.md)
        if mode in ('skill_only', 'skill_and_replica'):
            q.put(f"📖 Extraindo páginas e tabelas de {url} para formato Skill (SKILL.md + references/*.md)...")
            skill_pkg = build_wiki_skill_package(url, log_callback=log_callback)
            _write_skill_zip(skill_pkg['skill_name'], skill_pkg['files'], skill_zip_path)
            # Persist in .claude/skills/<skill_name> as well
            local_skill_dir = os.path.join('.claude', 'skills', skill_pkg['skill_name'])
            os.makedirs(local_skill_dir, exist_ok=True)
            for rel_path, content in skill_pkg['files'].items():
                full_p = os.path.join(local_skill_dir, rel_path)
                os.makedirs(os.path.dirname(full_p), exist_ok=True)
                with open(full_p, 'w', encoding='utf-8') as f:
                    f.write(content)
            # Auto-generate Architecture & OpenAPI 3.0 Map + Knowledge Graph (Zero Tokens)
            try:
                q.put("🔌 Mapeando rotas, endpoints e especificação OpenAPI 3.0...")
                arch_res = scan_skill_directory_routes(local_skill_dir)
                arch_rel = 'references/architecture_and_endpoints.md'
                skill_pkg['files'][arch_rel] = arch_res['markdown']
                with open(os.path.join(local_skill_dir, arch_rel), 'w', encoding='utf-8') as f:
                    f.write(arch_res['markdown'])
                q.put("🕸️ Construindo Grafo de Conhecimento Determinístico (knowledge_graph.json)...")
                build_knowledge_graph(local_skill_dir)
                _write_skill_zip(skill_pkg['skill_name'], skill_pkg['files'], skill_zip_path)
            except Exception as arch_err:
                q.put(f"⚠️ Aviso no mapeamento complementar: {arch_err}")
            q.put(f"📦 Pacote Skill + Grafo gerados em .claude/skills/{skill_pkg['skill_name']} ({skill_pkg['total_pages']} páginas)!")

        # 2. Download visual offline replica if requested
        if mode in ('replica_only', 'skill_and_replica'):
            downloader = WebsiteDownloader(url, download_dir, log_callback=log_callback)
            success = downloader.process()
            if not success and mode == 'replica_only':
                q.put("❌ Falha no download da réplica")
                with session_lock:
                    download_results[session_id] = {
                        'status': 'error',
                        'error': 'Failed to download site',
                        'created_at': time.time(),
                    }
                return
            if success:
                q.put("📦 Compactando réplica visual (ZIP)...")
                zip_directory(download_dir, zip_path)
            if os.path.isdir(download_dir):
                shutil.rmtree(download_dir, ignore_errors=True)

        q.put("🎉 Pronto! Sua Skill e arquivos estão disponíveis abaixo.")
        with session_lock:
            download_results[session_id] = {
                'status': 'complete',
                'zip_path': zip_path if os.path.exists(zip_path) else None,
                'skill_zip_path': skill_zip_path if os.path.exists(skill_zip_path) else None,
                'filename': zip_filename,
                'skill_filename': f"{skill_pkg['skill_name']}-skill.zip" if skill_pkg else f"{site_name}-skill.zip",
                'site_name': site_name,
                'url': url,
                'skill_pkg': skill_pkg,
                'created_at': time.time(),
            }

    except Exception as e:
        q.put(f"❌ Erro: {str(e)}")
        with session_lock:
            download_results[session_id] = {
                'status': 'error',
                'error': str(e),
                'created_at': time.time(),
            }
        if os.path.exists(download_dir):
            shutil.rmtree(download_dir, ignore_errors=True)
    finally:
        downloader = None
        gc.collect()


@app.route('/session-result/<session_id>')
def get_session_result(session_id):
    """Return generated Skill files preview and download links."""
    with session_lock:
        result = download_results.get(session_id)

    if not result:
        return jsonify({'error': 'Session not found'}), 404
    if result.get('status') != 'complete':
        return jsonify({'status': result.get('status'), 'error': result.get('error')}), 202

    skill_pkg = result.get('skill_pkg') or {}
    files_preview = {}
    for fname, content in (skill_pkg.get('files') or {}).items():
        files_preview[fname] = content[:15000] + (f"\n\n... [truncado no preview — arquivo completo tem {len(content):,} caracteres no ZIP]" if len(content) > 15000 else "")

    return jsonify({
        'session_id': session_id,
        'status': 'complete',
        'url': result.get('url'),
        'title': skill_pkg.get('title') or result.get('site_name'),
        'description': skill_pkg.get('description') or '',
        'total_pages': skill_pkg.get('total_pages', 0),
        'categories_count': skill_pkg.get('categories_count', 0),
        'has_skill_zip': bool(result.get('skill_zip_path')),
        'has_replica_zip': bool(result.get('zip_path')),
        'skill_download_url': f"/download-artifact/{session_id}/skill",
        'replica_download_url': f"/download-artifact/{session_id}/replica",
        'files': files_preview,
    })


@app.route('/stream/<session_id>')
def stream(session_id):
    """SSE endpoint for log streaming."""
    def generate():
        with session_lock:
            q = message_queues.get(session_id)

        if q is None:
            yield "data: ❌ Sessão não encontrada\n\n"
            yield "event: done\ndata: error\n\n"
            return

        deadline = time.time() + 30 * 60

        while True:
            if time.time() > deadline:
                yield "data: ⏱️ Conexão encerrada por inatividade\n\n"
                yield "event: done\ndata: timeout\n\n"
                return

            try:
                message = q.get(timeout=30)
                yield f"data: {message}\n\n"

                with session_lock:
                    result = download_results.get(session_id, {})
                if result.get('status') in ('complete', 'error') and q.empty():
                    yield f"event: done\ndata: {result['status']}\n\n"
                    return

            except queue.Empty:
                with session_lock:
                    result = download_results.get(session_id, {})
                if result.get('status') in ('complete', 'error'):
                    yield f"event: done\ndata: {result['status']}\n\n"
                    return
                yield ": keepalive\n\n"

    return Response(generate(), mimetype='text/event-stream')


@app.route('/download-artifact/<session_id>/<kind>')
def download_artifact(session_id, kind):
    with session_lock:
        result = download_results.get(session_id)

    if not result or result.get('status') != 'complete':
        return "File not ready", 404

    if kind == 'skill':
        target_path = result.get('skill_zip_path')
        download_name = result.get('skill_filename') or 'skill.zip'
    elif kind == 'replica':
        target_path = result.get('zip_path')
        download_name = result.get('filename') or 'replica.zip'
    else:
        return "Invalid kind", 400

    if not target_path or not os.path.exists(target_path):
        return "File not found", 404

    return send_file(target_path, as_attachment=True, download_name=download_name)


@app.route('/download-file/<session_id>')
def download_file(session_id):
    return download_artifact(session_id, 'skill')


def _load_saved_skill_files(skill_name: str) -> dict:
    """Read all .md files from .claude/skills/<skill_name>."""
    base_dir = os.path.join('.claude', 'skills', skill_name)
    if not os.path.isdir(base_dir):
        return {}
    files = {}
    for root, _, fnames in os.walk(base_dir):
        for fn in sorted(fnames):
            if fn.endswith('.md'):
                full_p = os.path.join(root, fn)
                rel_p = os.path.relpath(full_p, base_dir).replace('\\', '/')
                try:
                    with open(full_p, 'r', encoding='utf-8') as f:
                        files[rel_p] = f.read()
                except Exception:
                    pass
    return files


@app.route('/saved-skills')
def list_saved_skills():
    """Return already generated skills on disk so the UI loads them immediately."""
    skills_root = os.path.join('.claude', 'skills')
    if not os.path.isdir(skills_root):
        return jsonify({'skills': []})

    out = []
    for name in sorted(os.listdir(skills_root)):
        sdir = os.path.join(skills_root, name)
        if not os.path.isdir(sdir) or not os.path.exists(os.path.join(sdir, 'SKILL.md')):
            continue
        files = _load_saved_skill_files(name)
        preview = {}
        total_chars = 0
        for rel_p, content in files.items():
            total_chars += len(content)
            preview[rel_p] = content[:20000] + (
                f"\n\n... [truncado na visualização rápida — arquivo completo possui {len(content):,} caracteres. Use a barra de Busca acima para achar qualquer item/monstro!]"
                if len(content) > 20000 else ""
            )
        out.append({
            'skill_name': name,
            'title': f"Skill Salva: {name}",
            'description': f"{len(files)} arquivos Markdown ({total_chars:,} caracteres prontos para consulta)",
            'download_url': f"/download-saved-skill/{name}",
            'files': preview,
        })
    return jsonify({'skills': out})


@app.route('/download-saved-skill/<skill_name>')
def download_saved_skill(skill_name):
    """Download a .zip of a saved skill from .claude/skills/<skill_name>."""
    safe_name = os.path.basename(skill_name)
    files = _load_saved_skill_files(safe_name)
    if not files:
        return "Skill not found", 404

    zip_path = os.path.join(DOWNLOAD_FOLDER, f"{safe_name}-skill.zip")
    _write_skill_zip(safe_name, files, zip_path)
    return send_file(zip_path, as_attachment=True, download_name=f"{safe_name}-skill.zip")


@app.route('/search-skill', methods=['POST'])
def search_skill():
    """Search across all Markdown sections and tables inside a saved skill."""
    data = request.get_json(silent=True) or {}
    skill_name = os.path.basename((data.get('skill_name') or 'risingtale-wiki').strip())
    query = (data.get('query') or '').strip()
    if not query:
        return jsonify({'results': []})

    files = _load_saved_skill_files(skill_name)
    terms = [t.lower() for t in query.split() if len(t) >= 2]
    if not terms:
        terms = [query.lower()]

    matches = []
    for rel_p, content in files.items():
        if rel_p == 'SKILL.md':
            continue
        # Split by page separator '---' or level-1/2 headings
        sections = content.split('\n---\n')
        for sec in sections:
            low = sec.lower()
            # Also check individual table rows if it's a big table
            hits = sum(1 for t in terms if t in low)
            if hits == 0:
                continue

            # If section is huge (e.g. a table with 375 weapons), filter matching lines + context
            if len(sec) > 2500:
                lines = sec.splitlines()
                header_lines = [ln for ln in lines[:12] if ln.startswith('#') or ln.startswith('|')]
                matched_lines = [ln for ln in lines if any(t in ln.lower() for t in terms)]
                snippet = "\n".join(header_lines[:4] + ["..."] + matched_lines[:25])
            else:
                snippet = sec.strip()

            # Bonus score if exact query string is in the snippet
            score = hits * 10 + (25 if query.lower() in low else 0)
            matches.append({
                'file': rel_p,
                'score': score,
                'snippet': snippet[:2500],
            })

    matches.sort(key=lambda x: x['score'], reverse=True)
    return jsonify({'query': query, 'count': len(matches), 'results': matches[:15]})


@app.route('/install-global-claude/<skill_name>', methods=['POST'])
def install_global_claude(skill_name):
    """Copy the skill into ~/.claude/skills/<skill_name> so Claude Code can use it in any folder."""
    safe_name = os.path.basename(skill_name)
    src_dir = os.path.join('.claude', 'skills', safe_name)
    if not os.path.isdir(src_dir):
        return jsonify({'error': 'Skill não encontrada'}), 404

    dest_dir = os.path.expanduser(os.path.join('~', '.claude', 'skills', safe_name))
    os.makedirs(os.path.dirname(dest_dir), exist_ok=True)
    if os.path.exists(dest_dir):
        shutil.rmtree(dest_dir)
    shutil.copytree(src_dir, dest_dir)
    return jsonify({'ok': True, 'path': dest_dir})


# ===========================================================================
# MÓDULO 4: SkillRAG Studio API (Endpoint Scanner, Graph Stats, Agent & Eval)
# ===========================================================================

@app.route('/api/scan-endpoints', methods=['POST'])
def api_scan_endpoints():
    """
    Executa engenharia reversa estática (Zero-Token) das rotas, chamadas de API (fetch/axios/WS),
    auditoria anti-SSRF / anti-Indirect Prompt Injection e gera especificação OpenAPI 3.0.
    """
    data = request.get_json(silent=True) or {}
    skill_name = os.path.basename((data.get('skill_name') or 'risingtale-wiki').strip())
    target_url = (data.get('url') or '').strip()

    skill_dir = os.path.join('.claude', 'skills', skill_name)
    if os.path.isdir(skill_dir) and not target_url:
        res = scan_skill_directory_routes(skill_dir)
        return jsonify({
            'ok': True,
            'mode': 'skill_directory_reverse_engineering',
            'skill_name': skill_name,
            'total_routes_discovered': res.get('total_urls', 0),
            'route_families_count': res.get('route_families_count', 0),
            'route_families': res.get('route_families', {}),
            'openapi_spec': res.get('openapi', {}),
            'markdown': res.get('markdown', ''),
        })

    if not target_url:
        return jsonify({'error': 'Informe uma skill salva ou uma URL pública válida.'}), 400

    res = scan_website_architecture(target_url, max_pages=15)
    return jsonify({
        'ok': True,
        'mode': 'live_website_architecture_scan',
        'total_routes_discovered': res.get('pages_scanned', 0) + res.get('endpoints_count', 0),
        'route_families_count': len(res.get('openapi', {}).get('paths', {})),
        'openapi_spec': res.get('openapi', {}),
        'markdown': res.get('markdown', ''),
        'security_headers': res.get('security_headers', {}),
        'prompt_injection_alerts': res.get('prompt_injection_alerts', []),
    })


@app.route('/api/graph-stats/<skill_name>')
def api_graph_stats(skill_name):
    """
    Retorna estatísticas do Grafo de Conhecimento Determinístico (knowledge_graph.json),
    incluindo conflitos de buff (✕), encadeamentos (%) e sinergias elementais ocultas.
    """
    safe_name = os.path.basename(skill_name)
    skill_dir = os.path.join('.claude', 'skills', safe_name)
    if not os.path.isdir(skill_dir):
        return jsonify({'error': 'Skill não encontrada'}), 404

    kg = build_knowledge_graph(skill_dir)
    interesting_edges = [
        e for e in kg.get('edges', [])
        if e.get('relation') in ('CANCELS_BUFF', 'CHAINS_FROM', 'BOOSTS_ELEMENT', 'AMPLIFIES_SKILL')
    ]
    return jsonify({
        'skill_name': safe_name,
        'stats': kg.get('stats', {}),
        'interesting_edges': interesting_edges[:40],
    })


@app.route('/api/agent/ask', methods=['POST'])
def api_agent_ask():
    """
    Executa o pipeline Agentic Graph-RAG de 5 nós:
      GuardrailNode -> RouterNode -> RetrieverPrunerNode -> CRAGValidatorNode -> SynthesizerNode
    Suporta Modo Determinístico Zero-Token ($0.00) e BYOK Efêmero (OpenAI, Gemini, Anthropic, Groq, Ollama).
    A chave de API NUNCA é salva em disco nem em variáveis globais.
    """
    data = request.get_json(silent=True) or {}
    query = (data.get('query') or '').strip()
    if not query:
        return jsonify({'error': 'A pergunta (query) é obrigatória.'}), 400

    skill_name = os.path.basename((data.get('skill_name') or 'risingtale-wiki').strip())
    skill_dir = os.path.join('.claude', 'skills', skill_name)
    if not os.path.isdir(skill_dir):
        return jsonify({'error': f'Skill {skill_name} não encontrada.'}), 404

    provider = (data.get('provider') or 'deterministic').strip()
    model = (data.get('model') or '').strip()
    # Escopo estritamente local à requisição HTTP atual (RAM-only)
    ephemeral_api_key = (data.get('api_key') or '').strip()
    ephemeral_langsmith_key = (data.get('langsmith_api_key') or '').strip()

    result = run_agentic_rag_workflow(
        query=query,
        skill_dir=skill_dir,
        provider=provider,
        model=model,
        api_key=ephemeral_api_key,
        langsmith_api_key=ephemeral_langsmith_key,
    )
    return jsonify(result)


@app.route('/api/agent/eval', methods=['POST'])
def api_agent_eval():
    """
    Executa o Golden Dataset Benchmark (5 cenários multi-hop + OWASP LLM01)
    e retorna acurácia factual, acurácia de roteamento e economia FinOps de tokens.
    """
    data = request.get_json(silent=True) or {}
    skill_name = os.path.basename((data.get('skill_name') or 'risingtale-wiki').strip())
    skill_dir = os.path.join('.claude', 'skills', skill_name)
    provider = (data.get('provider') or 'deterministic').strip()
    model = (data.get('model') or '').strip()
    ephemeral_api_key = (data.get('api_key') or '').strip()
    ephemeral_langsmith_key = (data.get('langsmith_api_key') or '').strip()

    eval_report = run_golden_evaluation(
        skill_dir=skill_dir,
        provider=provider,
        model=model,
        api_key=ephemeral_api_key,
        langsmith_api_key=ephemeral_langsmith_key,
    )
    return jsonify(eval_report)


if __name__ == '__main__':
    app.run(debug=True, port=5001, threaded=True)
