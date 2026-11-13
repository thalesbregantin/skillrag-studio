import os
import re
import json
import html
import shutil
import hashlib
import requests
import urllib3
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
try:
    from playwright.sync_api import sync_playwright
except Exception:
    sync_playwright = None
import mimetypes
try:
    from markdownify import markdownify as md_convert
except ImportError:
    md_convert = None

# Suppress SSL warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

class WebsiteDownloader:
    def __init__(self, url, output_dir, log_callback=None, login_email=None, login_password=None):
        self.login_email = login_email
        self.login_password = login_password
        self.url = url
        self.output_dir = output_dir
        self.assets_dir = os.path.join(output_dir, 'assets')
        self.resource_cache = {}  # url -> local_path
        self.network_resources = {}  # url -> {'body': bytes, 'content_type': str}
        self.captured_products = {}  # prod_id -> {id, name, code}
        self.product_htmls = {}  # prod_id -> raw_html
        self.captured_apis = {}  # endpoint_key -> metadata dict
        self.site_knowledge = {}  # structured knowledge extracted from the site
        self.base_url = url
        self.session = None  # Will be set with cookies from browser
        self.log_callback = log_callback or (lambda msg: print(msg))
        
        if os.path.exists(output_dir):
            shutil.rmtree(output_dir)
        os.makedirs(self.assets_dir)

    def log(self, message):
        """Send log message to callback"""
        self.log_callback(message)

    def _get_extension(self, url, content_type=''):
        """Get file extension from URL or content-type"""
        parsed = urlparse(url)
        path = parsed.path
        _, ext = os.path.splitext(path)
        
        if ext and len(ext) <= 6:
            return ext
        
        if content_type:
            mime = content_type.split(';')[0].strip()
            guessed = mimetypes.guess_extension(mime)
            if guessed:
                return guessed
        
        return ''

    def _generate_filename(self, url, content_type=''):
        """Generate a unique filename for a resource"""
        ext = self._get_extension(url, content_type)
        url_hash = hashlib.md5(url.encode()).hexdigest()[:12]
        
        parsed = urlparse(url)
        name = os.path.basename(parsed.path)
        if name:
            name = re.sub(r'[^a-zA-Z0-9_-]', '_', name.split('.')[0])[:30]
        else:
            name = 'resource'
        
        return f"{name}_{url_hash}{ext}"

    def _save_resource(self, url, content, content_type=''):
        """Save a resource to disk and return relative path"""
        if url in self.resource_cache:
            return self.resource_cache[url]
        
        if not content:
            return None
            
        filename = self._generate_filename(url, content_type)
        filepath = os.path.join(self.assets_dir, filename)
        
        with open(filepath, 'wb') as f:
            f.write(content if isinstance(content, bytes) else content.encode('utf-8'))
        
        rel_path = f"assets/{filename}"
        self.resource_cache[url] = rel_path
        return rel_path

    def _download_fallback(self, url):
        """Download a resource that wasn't captured during page load"""
        if url in self.resource_cache:
            return self.resource_cache[url]
        
        if not url or url.startswith('data:') or url.startswith('blob:') or url.startswith('#'):
            return url
            
        try:
            response = self.session.get(url, timeout=15, verify=False)
            if response.status_code == 200:
                content_type = response.headers.get('content-type', '')
                local_path = self._save_resource(url, response.content, content_type)
                return local_path
        except Exception as e:
            pass  # Silent fail for fallback
        
        return None

    def _get_resource(self, url, base=None):
        """Get a resource - from cache, network capture, or fallback download"""
        if not url or url.startswith('data:') or url.startswith('blob:') or url.startswith('#'):
            return url
        
        # Make absolute URL
        abs_url = urljoin(base or self.base_url, url)
        
        # Check cache first
        if abs_url in self.resource_cache:
            return self.resource_cache[abs_url]
        
        # Check network captures
        if abs_url in self.network_resources:
            res = self.network_resources[abs_url]
            return self._save_resource(abs_url, res['body'], res.get('content_type', ''))
        
        # Fallback download
        local_path = self._download_fallback(abs_url)
        if local_path:
            return local_path
        
        # Return original if all fails
        return url

    def _rewrite_css_urls(self, css_content, css_url):
        """Rewrite all url() references in CSS content"""
        def replacer(match):
            full_match = match.group(0)
            url_content = match.group(1).strip()
            
            # Remove quotes if present
            if url_content.startswith(("'", '"')) and url_content.endswith(("'", '"')):
                url_content = url_content[1:-1]
            
            if not url_content or url_content.startswith('data:'):
                return full_match
            
            # Make absolute URL relative to CSS file
            abs_url = urljoin(css_url, url_content)
            local_path = self._get_resource(abs_url)
            
            if local_path and local_path.startswith('assets/'):
                # CSS is in assets/, so reference sibling files directly
                return f'url("{os.path.basename(local_path)}")'
            
            return full_match
        
        return re.sub(r'url\(\s*([^)]+)\s*\)', replacer, css_content)

    def _detect_nextjs(self, soup):
        """Detect if page is built with Next.js even without #__next"""
        # Check for Next.js data script
        for script in soup.find_all('script'):
            script_id = script.get('id', '')
            script_text = script.string or ''
            if '__NEXT_DATA__' in script_id or '__NEXT_DATA__' in script_text:
                return True
            if 'self.__next' in script_text:
                return True
        
        # Check for Next.js script patterns in src
        for script in soup.find_all('script', src=True):
            src = script['src']
            if '_next/' in src or 'webpack' in src.lower():
                return True
        
        # Check for Next.js link patterns
        for link in soup.find_all('link'):
            href = link.get('href', '')
            if '_next/' in href:
                return True
        
        return False

    def _fix_scroll_blocking(self, soup):
        """Fix CSS and HTML issues that block scrolling in offline viewing"""
        self.log("🔧 Corrigindo problemas de scroll para visualização offline...")
        
        # 1. Fix html element
        html_elem = soup.find('html')
        if html_elem:
            html_classes = html_elem.get('class', [])
            if isinstance(html_classes, str):
                html_classes = html_classes.split()
            
            # Remove Lenis-specific classes that block scroll
            lenis_classes = ['lenis', 'lenis-smooth', 'lenis-scrolling', 'lenis-stopped', 
                           'has-scroll-smooth', 'has-scroll-init', 'locomotive-scroll']
            new_classes = [c for c in html_classes if c.lower() not in [lc.lower() for lc in lenis_classes]]
            if new_classes != html_classes:
                html_elem['class'] = new_classes
                self.log("   ✅ Removidas classes Lenis/Locomotive do html")
        
        # 2. Fix body element
        body = soup.find('body')
        if body:
            body_classes = body.get('class', [])
            if isinstance(body_classes, str):
                body_classes = body_classes.split()
            
            # Remove scroll-blocking classes
            blocking_classes = ['overflow-hidden', 'no-scroll', 'scroll-lock', 'fixed', 
                              'lenis', 'lenis-smooth', 'has-scroll-smooth']
            new_classes = [c for c in body_classes if c.lower() not in [bc.lower() for bc in blocking_classes]]
            
            # Fix flex centering that cuts off content
            if 'items-center' in new_classes and 'flex' in new_classes:
                new_classes = [c if c != 'items-center' else 'items-start' for c in new_classes]
                self.log("   ✅ Corrigida centralização vertical do body")
            
            if new_classes != body_classes:
                body['class'] = new_classes
        
        # 3. Fix main containers that might have height: 100vh with overflow hidden
        problematic_selectors = [
            '[data-scroll-container]',
            '.scroll-container', 
            '.smooth-scroll',
            '[data-lenis-prevent]',
            '.lenis-wrapper',
        ]
        
        for elem in soup.find_all(class_=lambda c: c and any(
            x in str(c).lower() for x in ['scroll-container', 'smooth-scroll', 'lenis', 'locomotive']
        )):
            # Remove data attributes that control smooth scroll
            for attr in list(elem.attrs.keys()):
                if 'scroll' in attr.lower() or 'lenis' in attr.lower():
                    del elem[attr]
        
        # 4. Remove/fix inline styles that block scroll
        for elem in soup.find_all(attrs={'style': True}):
            style = elem['style']
            if 'overflow' in style.lower() and 'hidden' in style.lower():
                # Remove overflow: hidden from inline styles
                new_style = re.sub(r'overflow\s*:\s*hidden\s*;?', '', style, flags=re.IGNORECASE)
                elem['style'] = new_style.strip()
        
        # 5. Inject CSS overrides to ensure scrolling works
        scroll_fix_css = """
        /* Scroll fixes for offline viewing */
        html, body {
            overflow: auto !important;
            overflow-x: hidden !important;
            height: auto !important;
            min-height: 100% !important;
            scroll-behavior: auto !important;
            opacity: 1 !important;
            visibility: visible !important;
        }
        
        /* Force visibility - many sites use JS animations for initial display */
        body, .wrapper, main, #__next, #app, .page, .content {
            opacity: 1 !important;
            visibility: visible !important;
            transform: none !important;
        }
        
        /* Disable loader/preloader overlays */
        .loader, .preloader, .loading, [class*="loader"], [class*="preloader"] {
            display: none !important;
            opacity: 0 !important;
        }
        
        /* Show elements that might be hidden for animation */
        .word-inner, .char, .line, [data-aos], [data-scroll],
        .hero-text span, .hero-fade, [class*="hero"] span {
            opacity: 1 !important;
            transform: none !important;
            visibility: visible !important;
        }
        
        /* Reset Tailwind animation utility classes */
        .translate-y-full, .translate-x-full, .-translate-y-full, .-translate-x-full,
        .translate-y-1\/2, .-translate-y-1\/2, .translate-y-\[100\%\], .translate-y-\[110\%\] {
            transform: none !important;
        }
        
        /* Force visibility on common hidden-for-animation patterns */
        .opacity-0, [class*="opacity-0"] {
            opacity: 1 !important;
        }
        
        /* Reset scale transforms used for animations */
        .scale-0, .scale-50, .scale-75 {
            transform: none !important;
        }
        
        html.lenis, html.lenis-smooth, 
        body.lenis, body.lenis-smooth,
        .lenis-wrapper, .lenis-content,
        [data-lenis-prevent], [data-scroll-container] {
            overflow: visible !important;
            height: auto !important;
        }
        
        /* Fix flex containers that might cut off content */
        body.flex.items-center,
        body.flex.justify-center {
            align-items: flex-start !important;
            min-height: 100vh;
            height: auto !important;
        }
        
        /* Ensure main content scrolls */
        main, #__next, #__nuxt, #app, .main-content {
            overflow: visible !important;
            height: auto !important;
        }
        """
        
        # Add the fix CSS as a style tag at the end of head
        head = soup.find('head')
        if head:
            fix_style = soup.new_tag('style')
            fix_style['data-scroll-fix'] = 'true'
            fix_style.string = scroll_fix_css
            head.append(fix_style)
            self.log("   ✅ Injetado CSS para corrigir scroll")
        
        # 6. Remove Lenis/Locomotive script tags that might interfere
        scripts_removed = 0
        for script in soup.find_all('script'):
            src = script.get('src', '') or ''
            script_text = script.string or ''
            
            # Check for smooth scroll libraries
            if any(x in src.lower() for x in ['lenis', 'locomotive', 'smooth-scroll']):
                script.decompose()
                scripts_removed += 1
            elif any(x in script_text.lower() for x in ['new lenis', 'new locomotivescroll', 'smoothscroll']):
                script.decompose()
                scripts_removed += 1
        
        if scripts_removed > 0:
            self.log(f"   ✅ Removidos {scripts_removed} scripts de smooth scroll")

    def _process_srcset(self, srcset, base=None):
        """Process a srcset attribute and return the rewritten version"""
        if not srcset:
            return srcset
        
        new_parts = []
        parts = srcset.split(',')
        
        for part in parts:
            part = part.strip()
            if not part:
                continue
            
            tokens = part.split()
            if not tokens:
                continue
            
            url = tokens[0]
            descriptor = ' '.join(tokens[1:]) if len(tokens) > 1 else ''
            
            if url.startswith('data:'):
                new_parts.append(part)
                continue
            
            local_path = self._get_resource(url, base)
            if local_path and local_path != url:
                new_parts.append(f"{local_path} {descriptor}".strip())
            else:
                new_parts.append(part)
        
        return ', '.join(new_parts) if new_parts else srcset

    def _extract_iframe_content(self, page):
        """
        Check if the page content is inside an iframe (common in site builders like Aura, Webflow, etc.)
        and extract the actual content if found.
        """
        # Check for srcdoc iframes (content embedded in attribute)
        srcdoc_iframe = page.query_selector('iframe[srcdoc]')
        if srcdoc_iframe:
            self.log("🔍 Detectado iframe com srcdoc - extraindo conteúdo real...")
            srcdoc = srcdoc_iframe.get_attribute('srcdoc')
            if srcdoc:
                # Decode HTML entities
                decoded_content = html.unescape(srcdoc)
                return decoded_content, True
        
        # Check for preview frames (common in site builders)
        preview_selectors = [
            'iframe[class*="preview"]',
            'iframe[class*="site-frame"]',
            'iframe[class*="canvas"]',
            'iframe[id*="preview"]',
            '#preview-iframe',
            '.preview-frame iframe',
            '[role="tabpanel"] iframe',  # Aura-style tab panels
            '[data-testid*="preview"] iframe',
        ]
        
        for selector in preview_selectors:
            iframe = page.query_selector(selector)
            if iframe:
                # Try to get the frame content
                frames = page.frames
                for frame in frames:
                    if frame != page.main_frame and frame.url and frame.url != 'about:blank':
                        try:
                            self.log(f"🔍 Detectado iframe de preview - extraindo de {frame.url[:50]}...")
                            content = frame.content()
                            if len(content) > 500:  # Has substantial content
                                self.base_url = frame.url
                                return content, True
                        except:
                            pass
        
        # Check all frames including those with srcdoc (about:srcdoc URL)
        for frame in page.frames:
            if frame != page.main_frame:
                try:
                    frame_url = frame.url
                    # Handle frames with srcdoc (they have about:srcdoc URL)
                    if frame_url == 'about:srcdoc':
                        content = frame.content()
                        if len(content) > 1000:  # Substantial content
                            self.log("🔍 Detectado iframe srcdoc via frame - extraindo conteúdo...")
                            return content, True
                except:
                    pass
        
        # Check if main content is suspiciously small (might be a wrapper)
        main_content = page.content()
        body = page.query_selector('body')
        if body:
            # Check if body has very few elements but contains an iframe
            direct_children = page.query_selector_all('body > *')
            iframes = page.query_selector_all('iframe')

            # Treat as wrapper only when the main HTML is also small. This avoids
            # false positives on real landing pages that embed a single iframe
            # (e.g. a YouTube/Vimeo player) but still ship full content.
            is_wrapper = (
                len(direct_children) <= 5
                and len(iframes) > 0
                and len(main_content) < 8000
            )

            if is_wrapper:
                # Some providers chain cross-origin iframes (e.g. *.preview.<provider>.com
                # -> app.<provider>.com/loading -> *.static.<provider>.com),
                # so we wait for the real content frame and pick the best one.
                self.log("🔍 Detectada página wrapper. Aguardando iframes internos carregarem...")
                best = self._wait_for_real_content_frame(page)
                if best is not None:
                    frame, content = best
                    self.log(f"🔍 Conteúdo real encontrado em: {frame.url[:80]}")
                    if frame.url and frame.url not in ['about:blank', 'about:srcdoc']:
                        self.base_url = frame.url
                    return content, True

        return None, False

    def _score_frame_content(self, content):
        """
        Heuristic score for how likely a frame holds the *real* site content
        instead of being a loader / wrapper / tracking iframe.
        Higher is better. Returns negative for non-content frames.
        """
        if not content or len(content) < 500:
            return -1

        lowered = content.lower()

        # Strong negative signals: loader / preview pages from common providers
        loader_markers = [
            'loading-preview',
            'preview environment is not responding',
            'starting up',
            'cdn-cgi/challenge',
            'criteo', 'doubleclick', 'googletagmanager', 'googlesyndication',
            'analytics', 'pixel',
        ]
        if any(m in lowered for m in loader_markers):
            return -1

        # Must look like a real HTML document
        if '<head' not in lowered or '<body' not in lowered:
            return -1

        score = 0
        # Real apps usually ship CSS, scripts and many elements
        score += lowered.count('<link') * 5
        score += lowered.count('<script') * 2
        score += lowered.count('<div')
        score += lowered.count('<img') * 3
        score += lowered.count('<section') * 2

        # Bonus when it looks like a SPA root
        for marker in ['id="__next"', 'id="root"', 'id="app"', 'id="__nuxt"',
                       '__next_data__', 'data-reactroot']:
            if marker in lowered:
                score += 25
                break

        # Slight bonus for total size, but not dominant
        score += min(len(content) // 1000, 100)

        return score

    def _wait_for_real_content_frame(self, page, max_wait_ms=15000, poll_ms=1000):
        """Poll page.frames until we find a frame that scores well as real content."""
        elapsed = 0
        best_seen = None  # (score, frame, content)

        while elapsed <= max_wait_ms:
            for frame in page.frames:
                if frame == page.main_frame:
                    continue
                url = frame.url or ''
                if url in ('about:blank', 'about:srcdoc'):
                    # about:srcdoc still might have content - try it
                    if url != 'about:srcdoc':
                        continue
                try:
                    content = frame.content()
                except Exception:
                    continue
                score = self._score_frame_content(content)
                if score <= 0:
                    continue
                if best_seen is None or score > best_seen[0]:
                    best_seen = (score, frame, content)

            # Good enough? Stop polling early.
            if best_seen and best_seen[0] >= 50:
                return best_seen[1], best_seen[2]

            page.wait_for_timeout(poll_ms)
            elapsed += poll_ms

        if best_seen:
            return best_seen[1], best_seen[2]
        return None

    def _apply_basic_stealth(self, context):
        """
        Reduce obvious automation fingerprints that trigger anti-bot pages
        on some hosts when running in cloud/datacenter environments.
        """
        context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
            Object.defineProperty(navigator, 'languages', {
                get: () => ['en-US', 'en']
            });
            Object.defineProperty(navigator, 'plugins', {
                get: () => [1, 2, 3]
            });
            window.chrome = window.chrome || { runtime: {} };
        """)

    def _is_challenge_page(self, page):
        """Detect common anti-bot/challenge pages."""
        try:
            title = (page.title() or "").lower()
            content = (page.content() or "").lower()
            title_markers = [
                "just a moment",
                "verify you are human",
                "checking your browser",
                "attention required",
            ]
            hard_content_markers = [
                "/cdn-cgi/challenge",
                "cf-challenge",
                "cf-browser-verification",
                "challenge-platform",
                "datadome",
                "perimeterx",
            ]
            if any(marker in title for marker in title_markers):
                return True

            if any(marker in content for marker in hard_content_markers):
                return True

            # Extra guard: avoid false positives from regular "captcha" embeds.
            if "verify you are human" in content and "cloudflare" in content:
                return True

            return False
        except Exception:
            return False

    def _navigate_with_retries(self, page):
        """Navigate with multiple strategies for heavy/protected websites."""
        attempts = [
            ("domcontentloaded", 60000),
            ("load", 80000),
            ("networkidle", 90000),
        ]
        last_error = None

        for wait_until, timeout in attempts:
            try:
                page.goto(self.url, wait_until=wait_until, timeout=timeout)
                self.log(f"✓ Página carregada ({wait_until})")
                page.wait_for_timeout(2500)
                return
            except Exception as e:
                last_error = e
                self.log(f"⚠️ Tentativa ({wait_until}) falhou: {str(e)[:100]}")

        # Keep compatibility with old behavior when partial content exists.
        if page.url and page.url != "about:blank":
            self.log("⚠️ Continuando com conteúdo parcial carregado...")
            return

        if last_error:
            raise last_error

    def _login(self, page):
        """Login no portal B2B (Mercos/meuspedidos) antes da coleta para capturar preços."""
        login_url = self.url.rstrip('/') + '/entrar'
        self.log(f"🔐 Fazendo login em {login_url}...")
        try:
            page.goto(login_url, wait_until='domcontentloaded', timeout=60000)
            email_sel = 'input[type="email"]'
            page.wait_for_selector(email_sel, timeout=30000)
            page.fill(email_sel, self.login_email)
            page.fill('input[type="password"]', self.login_password)
            page.click('button[type="submit"]')
            page.wait_for_timeout(8000)
            if '/entrar' not in page.url:
                self.log("   ✅ Login realizado com sucesso")
            else:
                self.log(f"   ⚠️ Login pode ter falhado (ainda em {page.url}) — continuando mesmo assim")
        except Exception as e:
            self.log(f"   ⚠️ Erro no login: {str(e)[:120]} — continuando sem autenticação")

    def process(self):
        with sync_playwright() as p:
            self.log("🚀 Iniciando navegador...")
            # Launch with reduced memory footprint
            browser = p.chromium.launch(
                headless=True,
                args=[
                    '--disable-dev-shm-usage',  # Overcome limited resource problems
                    '--no-sandbox',  # Required for Docker
                    '--disable-setuid-sandbox',
                    '--disable-gpu',
                    '--disable-extensions',
                    '--disable-background-networking',
                    '--disable-default-apps',
                    '--disable-sync',
                    '--disable-translate',
                    '--metrics-recording-only',
                    '--mute-audio',
                    '--no-first-run',
                    '--safebrowsing-disable-auto-update',
                ]
            )

            context = browser.new_context(
                user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                viewport={'width': 1920, 'height': 1080},
                device_scale_factor=1,
                locale='en-US',
                timezone_id='America/New_York',
            )
            self._apply_basic_stealth(context)
            
            page = context.new_page()

            # Skip media that we can't really replay offline anyway. This both
            # speeds up captures and avoids holding huge video buffers in RAM.
            BLOCKED_RESOURCE_TYPES = {'media', 'websocket', 'eventsource'}

            def route_filter(route):
                try:
                    if route.request.resource_type in BLOCKED_RESOURCE_TYPES:
                        return route.abort()
                except Exception:
                    pass
                try:
                    return route.continue_()
                except Exception:
                    pass

            try:
                page.route('**/*', route_filter)
            except Exception:
                pass

            # Skip caching of huge bodies that don't fit our offline model
            MAX_RESOURCE_BYTES = 8 * 1024 * 1024  # 8 MB per asset

            def capture_response(response):
                try:
                    url = response.url
                    if response.status != 200:
                        return
                    if url.startswith(('data:', 'blob:')):
                        return
                    try:
                        length = int(response.headers.get('content-length', 0))
                    except Exception:
                        length = 0
                    if length and length > MAX_RESOURCE_BYTES:
                        return
                    try:
                        body = response.body()
                    except Exception:
                        return
                    if len(body) > MAX_RESOURCE_BYTES:
                        return
                    resource_data = {
                        'body': body,
                        'content_type': response.headers.get('content-type', '')
                    }
                    self.network_resources[url] = resource_data
                    try:
                        request_url = response.request.url
                        if request_url != url:
                            self.network_resources[request_url] = resource_data
                    except Exception:
                        pass
                        
                    # Intercept product list APIs
                    if 'api_b2b/v1/' in url and any(x in url for x in ['produtos', 'destaques', 'promocoes']):
                        try:
                            data = json.loads(body.decode('utf-8', errors='ignore'))
                            items = data if isinstance(data, list) else data.get('results', [])
                            for item in items:
                                pid = item.get('produto_id')
                                if pid:
                                    self.captured_products[pid] = item
                        except Exception:
                            pass

                    # Capture general JSON/XHR/Fetch API endpoints for MCP & Agent generation
                    ctype = response.headers.get('content-type', '').lower()
                    req_type = ''
                    req_method = 'GET'
                    try:
                        req_type = response.request.resource_type
                        req_method = response.request.method
                    except Exception:
                        pass

                    ignore_domains = (
                        'google-analytics', 'googletagmanager', 'doubleclick', 'facebook',
                        'clarity.ms', 'hotjar', 'sentry.io', 'datadoghq', 'intercom',
                        'segment.io', 'mixpanel', 'amplitude', 'criteo', 'tiktok',
                        'fonts.googleapis', 'fonts.gstatic', 'cloudflareinsights'
                    )
                    if (
                        len(self.captured_apis) < 35
                        and ('application/json' in ctype or req_type in ('xhr', 'fetch'))
                        and not any(d in url.lower() for d in ignore_domains)
                        and not url.lower().endswith(('.js', '.css', '.png', '.jpg', '.svg', '.woff', '.woff2', '.ico'))
                    ):
                        parsed_api = urlparse(url)
                        endpoint_key = f"{req_method} {parsed_api.scheme}://{parsed_api.netloc}{parsed_api.path}"
                        if endpoint_key not in self.captured_apis:
                            sample_preview = ''
                            sample_keys = []
                            try:
                                decoded = body.decode('utf-8', errors='ignore').strip()
                                if decoded.startswith(('{', '[')):
                                    parsed_json = json.loads(decoded)
                                    if isinstance(parsed_json, dict):
                                        sample_keys = list(parsed_json.keys())[:15]
                                    elif isinstance(parsed_json, list) and parsed_json and isinstance(parsed_json[0], dict):
                                        sample_keys = list(parsed_json[0].keys())[:15]
                                    sample_preview = decoded[:600]
                            except Exception:
                                pass

                            if sample_preview or 'json' in ctype:
                                self.captured_apis[endpoint_key] = {
                                    'method': req_method,
                                    'url': url,
                                    'endpoint': f"{parsed_api.scheme}://{parsed_api.netloc}{parsed_api.path}",
                                    'path': parsed_api.path,
                                    'query': parsed_api.query,
                                    'content_type': ctype.split(';')[0],
                                    'sample_keys': sample_keys,
                                    'sample_preview': sample_preview,
                                }
                except Exception:
                    pass

            page.on("response", capture_response)

            if self.login_email and self.login_password:
                self._login(page)

            self.log(f"🌐 Carregando {self.url}...")
            try:
                self._navigate_with_retries(page)
                # Wait a bit more for additional resources
                page.wait_for_timeout(3000)
                self.log("✓ Recursos adicionais carregados")

                # Retry once if we landed on an anti-bot challenge page
                if self._is_challenge_page(page):
                    self.log("🤖 Proteção anti-bot detectada. Aguardando e tentando novamente...")
                    page.wait_for_timeout(5000)
                    try:
                        page.reload(wait_until='domcontentloaded', timeout=60000)
                        page.wait_for_timeout(3000)
                    except Exception:
                        pass
            except Exception as e:
                self.log(f"⚠️ Aviso de carregamento: {str(e)[:100]}")
                self.log("⚠️ Tentando continuar mesmo assim...")
            
            self.base_url = page.url
            
            # Wait for dynamic content
            page.wait_for_timeout(2000)
            
            # Check for iframe content (site builders like Aura, Webflow, etc.)
            iframe_content, is_iframe = self._extract_iframe_content(page)
            
            if not is_iframe:
                self.log("📜 Rolando página para carregar conteúdo lazy...")
                self._scroll_page(page)
                page.wait_for_timeout(3000)
            
            # Get cookies from browser for fallback downloads
            cookies = context.cookies()
            
            # Setup requests session with browser cookies
            self.session = requests.Session()
            self.session.headers.update({
                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': '*/*',
                'Accept-Language': 'en-US,en;q=0.9',
                'Referer': self.base_url,
            })
            for cookie in cookies:
                self.session.cookies.set(cookie['name'], cookie['value'], domain=cookie.get('domain', ''))
            
            # Get final HTML - use iframe content if detected
            if is_iframe and iframe_content:
                html_content = iframe_content
                self.log("✨ Usando conteúdo extraído do iframe")
            else:
                html_content = page.content()
            
            # Navigate to and download details for each captured product
            if self.captured_products:
                self.log(f"📦 Encontrados {len(self.captured_products)} produtos no catálogo. Iniciando coleta das páginas de detalhes...")
                for pid, pinfo in list(self.captured_products.items()):
                    try:
                        self.log(f"📥 Coletando detalhes: {pinfo['nome'][:40]}... (ID: {pid})")
                        prod_url = urljoin(self.base_url, f"/produtos/{pid}")
                        
                        # Reuse the page to navigate to the product page
                        page.goto(prod_url, wait_until='domcontentloaded', timeout=45000)
                        page.wait_for_timeout(1500)
                        
                        # Scroll to trigger lazy loading of product details images/content
                        self._scroll_page(page)
                        page.wait_for_timeout(1000)
                        
                        # Store the raw HTML content
                        self.product_htmls[pid] = page.content()
                    except Exception as e:
                        self.log(f"⚠️ Erro ao carregar detalhes do produto {pid}: {e}")
            
            self.log(f"📦 Capturados {len(self.network_resources)} recursos de rede")

            try:
                page.close()
            except Exception:
                pass
            try:
                context.close()
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass

        # Extract structured knowledge before modifying HTML for offline links
        self.log("🧠 Extraindo conhecimento estruturado e rotas de API do site...")
        self.site_knowledge = self._extract_site_knowledge(html_content)

        # Process HTML
        self.log("🔧 Processando HTML e assets...")
        soup = BeautifulSoup(html_content, 'html.parser')
        
        # Fix scroll-blocking issues for offline viewing
        self._fix_scroll_blocking(soup)
        
        # Remove any remaining iframes that are wrappers (like Aura preview frames)
        for iframe in soup.find_all('iframe'):
            # Keep only essential iframes (videos, maps, etc.)
            src = iframe.get('src', '') or ''
            srcdoc = iframe.get('srcdoc', '')
            
            # Remove preview/wrapper iframes
            if srcdoc or 'preview' in str(iframe.get('class', '')).lower():
                iframe.decompose()
        
        # 1. Process external stylesheets
        self.log("🎨 Processando stylesheets...")
        for link in soup.find_all('link', rel='stylesheet'):
            href = link.get('href')
            if not href or href.startswith('data:'):
                continue
            
            abs_url = urljoin(self.base_url, href)
            
            # Try to get CSS content
            css_content = None
            if abs_url in self.network_resources:
                try:
                    css_content = self.network_resources[abs_url]['body'].decode('utf-8', errors='ignore')
                except:
                    pass
            if not css_content:
                # Fallback download
                try:
                    response = self.session.get(abs_url, timeout=15, verify=False)
                    if response.status_code == 200:
                        css_content = response.text
                except:
                    pass
            
            if css_content:
                css_content = self._rewrite_css_urls(css_content, abs_url)
                local_path = self._save_resource(abs_url, css_content.encode('utf-8'), 'text/css')
                if local_path:
                    link['href'] = local_path
        
        # 2. Process inline <style> tags
        self.log("✨ Processando estilos inline...")
        for style_tag in soup.find_all('style'):
            if style_tag.string:
                style_tag.string = self._rewrite_css_urls(style_tag.string, self.base_url)
        
        # 3. Process scripts
        self.log("📝 Processando scripts...")
        for script in soup.find_all('script', src=True):
            src = script.get('src')
            if not src or src.startswith('data:'):
                continue
            
            local_path = self._get_resource(src)
            if local_path and local_path != src:
                script['src'] = local_path
                for attr in ['integrity', 'crossorigin', 'nonce']:
                    if script.has_attr(attr):
                        del script[attr]
        
        # 4. Process all image-related elements
        self.log("🖼️ Processando imagens...")
        for elem in soup.find_all(['img', 'source', 'video', 'audio', 'picture', 'input']):
            # Process src
            src = elem.get('src')
            
            # Check lazy loading attributes first
            for attr in ['data-src', 'data-original', 'data-lazy-src', 'data-url', 'data-image', 'data-bg']:
                if elem.get(attr):
                    lazy_src = elem[attr]
                    local_path = self._get_resource(lazy_src)
                    if local_path and local_path != lazy_src:
                        elem['src'] = local_path
                        del elem[attr]
                        src = None  # Already handled
                    break
            
            if src and not src.startswith('data:'):
                local_path = self._get_resource(src)
                if local_path and local_path != src:
                    elem['src'] = local_path
            
            # Process srcset
            srcset = elem.get('srcset')
            if srcset:
                elem['srcset'] = self._process_srcset(srcset)
            
            # Process data-srcset
            data_srcset = elem.get('data-srcset')
            if data_srcset:
                elem['data-srcset'] = self._process_srcset(data_srcset)
            
            # Process poster for video
            if elem.name == 'video' and elem.get('poster'):
                poster = elem['poster']
                local_path = self._get_resource(poster)
                if local_path and local_path != poster:
                    elem['poster'] = local_path
        
        # 5. Process inline style attributes
        self.log("🔗 Processando atributos de estilo inline...")
        for elem in soup.find_all(attrs={'style': True}):
            style = elem['style']
            if 'url(' in style:
                elem['style'] = self._rewrite_css_urls(style, self.base_url)
        
        # 6. Process favicons and other link tags with URLs
        for link in soup.find_all('link'):
            if link.get('href') and link.get('rel'):
                rel = link['rel']
                if isinstance(rel, list):
                    rel = ' '.join(rel)
                if 'icon' in rel.lower() or 'apple-touch' in rel.lower() or 'manifest' in rel.lower():
                    href = link['href']
                    if not href.startswith('data:'):
                        local_path = self._get_resource(href)
                        if local_path and local_path != href:
                            link['href'] = local_path
        
        # 7. Process meta tags with image URLs (og:image, etc.)
        for meta in soup.find_all('meta', attrs={'content': True}):
            prop = meta.get('property', '') or meta.get('name', '')
            if 'image' in prop.lower():
                content = meta['content']
                if content and not content.startswith('data:') and ('http' in content or content.startswith('/')):
                    local_path = self._get_resource(content)
                    if local_path and local_path != content:
                        meta['content'] = local_path
        
        # 8. Process background images in divs and other elements
        for elem in soup.find_all(attrs={'data-background': True}):
            bg = elem['data-background']
            if bg and not bg.startswith('data:'):
                local_path = self._get_resource(bg)
                if local_path and local_path != bg:
                    elem['data-background'] = local_path
        
        # 9. Fix navigation links that won't work locally
        # Convert "/" to "#" or "index.html" so they don't break when opened offline
        self.log("🔗 Corrigindo links de navegação...")
        for a in soup.find_all('a', href=True):
            href = a['href']
            # Convert root links to stay on page
            if href == '/':
                a['href'] = '#'
            # Convert other relative paths to anchors (they won't work offline anyway)
            elif href.startswith('/') and not href.startswith('//'):
                # Keep as anchor to prevent navigation errors
                a['href'] = '#'

        # 9.5 Rewrite product cards to link to local HTML pages
        if self.captured_products:
            self.log("🔗 Vinculando cards de produto às páginas de detalhes locais...")
            cards = soup.find_all(class_=lambda c: c and 'CatalogItem__catalogItemContainer' in c)
            cards_linked = 0
            for card in cards:
                title_elem = card.find(class_=lambda c: c and 'title' in c.lower())
                title_text = title_elem.text.strip() if title_elem else ""
                
                code_elem = card.find(class_=lambda c: c and 'subtitle' in c.lower())
                code_text = ""
                if code_elem:
                    m = re.search(r'C\u00f3d\.\s*(\w+)', code_elem.text, re.IGNORECASE)
                    if m:
                        code_text = m.group(1).strip()
                
                matched_pid = None
                for pid, pinfo in self.captured_products.items():
                    if code_text and pinfo['codigo'] == code_text:
                        matched_pid = pid
                        break
                    if title_text and (pinfo['nome'] in title_text or title_text in pinfo['nome']):
                        matched_pid = pid
                        break
                
                if matched_pid:
                    link_tag = soup.new_tag('a', href=f"produto_{matched_pid}.html", style="text-decoration: none; color: inherit; display: block;")
                    card.wrap(link_tag)
                    cards_linked += 1
            self.log(f"   ✅ Vinculados {cards_linked} cards de produto com sucesso")
        
        # 10. Handle SPA frameworks (Gatsby, Next.js, Nuxt, React, Vite, etc.)
        # These frameworks use client-side routing that doesn't work offline
        # The HTML is already server-rendered, so we remove ALL framework scripts
        is_gatsby = soup.find(id='___gatsby') is not None
        is_nextjs = soup.find(id='__next') is not None or self._detect_nextjs(soup)
        is_nuxt = soup.find(id='__nuxt') is not None
        is_spa_react = soup.find(id='root') is not None or soup.find(id='app') is not None
        
        safe_keywords = ['google', 'analytics', 'gtm', 'gtag', 'facebook', 'pixel', 
                       'elfsight', 'hubspot', 'intercom', 'crisp', 'drift', 'hotjar',
                       'clarity', 'segment', 'mixpanel', 'amplitude', 'adobe', 'privacy']
        
        if is_gatsby or is_nextjs or is_nuxt or is_spa_react:
            framework = 'Gatsby' if is_gatsby else ('Next.js' if is_nextjs else ('Nuxt' if is_nuxt else 'React/SPA'))
            self.log(f"🛡️ Detectado {framework} - removendo scripts do framework...")
            
            scripts_removed = 0
            for script in soup.find_all('script'):
                src = script.get('src', '')
                script_text = script.string or ''
                is_safe = any(safe in src.lower() for safe in safe_keywords)
                
                if not is_safe:
                    should_remove = False
                    
                    if is_gatsby and ('framework-' in src or 'app-' in src or 
                                     'commons-' in src or 'component-' in src or
                                     'webpack-runtime' in src or 'polyfill' in src):
                        should_remove = True
                    
                    if is_nextjs:
                        if src and not src.startswith(('http://', 'https://', '//')):
                            should_remove = True
                        if '_next/' in src or 'webpack' in src or 'polyfill' in src:
                            should_remove = True
                        if '__next' in script_text or 'self.__next' in script_text:
                            should_remove = True
                        if '-' in src and src.endswith('.js') and 'assets/' in src:
                            should_remove = True
                    
                    if is_spa_react:
                        if src and not src.startswith(('http://', 'https://', '//')):
                            should_remove = True
                        if 'assets/' in src and ('index' in src or 'main' in src or '-' in src):
                            should_remove = True
                    
                    if is_nuxt and ('_nuxt/' in src or '__NUXT__' in script_text or
                                   'nuxt' in src.lower()):
                        should_remove = True
                    
                    if ('hydrate' in script_text.lower() or 
                        'window.__' in script_text or
                        'GATSBY' in script_text or
                        'pageData' in script_text or
                        'self.__next' in script_text or
                        '__NEXT_DATA__' in script_text):
                        should_remove = True
                    
                    if should_remove:
                        script.decompose()
                        scripts_removed += 1
            
            links_removed = 0
            for link in soup.find_all('link', rel=lambda r: r and any(x in r for x in ['preload', 'prefetch', 'modulepreload'])):
                href = link.get('href', '')
                if '_next/' in href or (href.startswith('assets/') and '-' in href):
                    link.decompose()
                    links_removed += 1
            
            self.log(f"   ✅ Removidos {scripts_removed} scripts e {links_removed} preloads do framework")
        
        # Save HTML
        html_output = str(soup)
        with open(os.path.join(self.output_dir, 'index.html'), 'w', encoding='utf-8') as f:
            f.write(html_output)

        # Save structured products JSON for Shopify/Fable import
        if self.captured_products:
            self.log("💾 Salvando JSON estruturado de produtos...")
            with open(os.path.join(self.output_dir, 'produtos.json'), 'w', encoding='utf-8') as f:
                json.dump(list(self.captured_products.values()), f, indent=4, ensure_ascii=False)

        # Process and save subpages
        if self.product_htmls:
            self.log(f"🔧 Processando {len(self.product_htmls)} páginas de produto...")
            for pid, prod_html in self.product_htmls.items():
                try:
                    prod_soup = BeautifulSoup(prod_html, 'html.parser')
                    self._fix_scroll_blocking(prod_soup)
                    
                    # Process stylesheets
                    for link in prod_soup.find_all('link', rel='stylesheet'):
                        href = link.get('href')
                        if not href or href.startswith('data:'):
                            continue
                        abs_url = urljoin(self.base_url, href)
                        local_path = self._get_resource(abs_url)
                        if local_path:
                            link['href'] = local_path
                            
                    # Process inline styles
                    for style_tag in prod_soup.find_all('style'):
                        if style_tag.string:
                            style_tag.string = self._rewrite_css_urls(style_tag.string, self.base_url)
                            
                    # Process scripts
                    for script in prod_soup.find_all('script', src=True):
                        src = script.get('src')
                        if src and not src.startswith('data:'):
                            local_path = self._get_resource(src)
                            if local_path and local_path != src:
                                script['src'] = local_path
                                
                    # Process images and media
                    for elem in prod_soup.find_all(['img', 'source', 'video', 'audio', 'picture', 'input']):
                        src = elem.get('src')
                        for attr in ['data-src', 'data-original', 'data-lazy-src', 'data-url', 'data-image', 'data-bg']:
                            if elem.get(attr):
                                lazy_src = elem[attr]
                                local_path = self._get_resource(lazy_src)
                                if local_path and local_path != lazy_src:
                                    elem['src'] = local_path
                                    del elem[attr]
                                    src = None
                                break
                        if src and not src.startswith('data:'):
                            local_path = self._get_resource(src)
                            if local_path and local_path != src:
                                elem['src'] = local_path
                        srcset = elem.get('srcset')
                        if srcset:
                            elem['srcset'] = self._process_srcset(srcset)
                            
                    # Process inline style attributes
                    for elem in prod_soup.find_all(attrs={'style': True}):
                        style = elem['style']
                        if 'url(' in style:
                            elem['style'] = self._rewrite_css_urls(style, self.base_url)
                            
                    # Remove SPA/React bundle scripts
                    is_gatsby_prod = prod_soup.find(id='___gatsby') is not None
                    is_nextjs_prod = prod_soup.find(id='__next') is not None or self._detect_nextjs(prod_soup)
                    is_nuxt_prod = prod_soup.find(id='__nuxt') is not None
                    is_spa_react_prod = prod_soup.find(id='root') is not None or prod_soup.find(id='app') is not None
                    
                    if is_gatsby_prod or is_nextjs_prod or is_nuxt_prod or is_spa_react_prod:
                        for script in prod_soup.find_all('script'):
                            src = script.get('src', '')
                            script_text = script.string or ''
                            is_safe = any(safe in src.lower() for safe in safe_keywords)
                            if not is_safe:
                                should_remove = False
                                if is_gatsby_prod and ('framework-' in src or 'app-' in src or 'commons-' in src or 'webpack-runtime' in src):
                                    should_remove = True
                                if is_nextjs_prod:
                                    if src and not src.startswith(('http://', 'https://', '//')):
                                        should_remove = True
                                    if '_next/' in src or 'webpack' in src:
                                        should_remove = True
                                if is_spa_react_prod:
                                    if src and not src.startswith(('http://', 'https://', '//')):
                                        should_remove = True
                                    if 'assets/' in src and ('index' in src or 'main' in src or '-' in src):
                                        should_remove = True
                                if is_nuxt_prod and ('_nuxt/' in src or 'nuxt' in src.lower()):
                                    should_remove = True
                                if ('hydrate' in script_text.lower() or 'window.__' in script_text or 'self.__next' in script_text):
                                    should_remove = True
                                if should_remove:
                                    script.decompose()
                                    
                        for link in prod_soup.find_all('link', rel=lambda r: r and any(x in r for x in ['preload', 'prefetch', 'modulepreload'])):
                            href = link.get('href', '')
                            if '_next/' in href or (href.startswith('assets/') and '-' in href):
                                link.decompose()
                    
                    # Fix links to return to index.html
                    for a in prod_soup.find_all('a', href=True):
                        href = a['href']
                        if href == '/' or 'index.html' in href or href.startswith(self.base_url):
                            a['href'] = 'index.html'
                        elif href.startswith('/') and not href.startswith('//'):
                            a['href'] = 'index.html'
                    
                    prod_filename = f"produto_{pid}.html"
                    with open(os.path.join(self.output_dir, prod_filename), 'w', encoding='utf-8') as f:
                        f.write(str(prod_soup))
                except Exception as e:
                    self.log(f"⚠️ Erro ao processar página do produto {pid}: {e}")

        assets_count = len(self.resource_cache)
        self.log(f"✅ Concluído! {assets_count} assets salvos")

        # Free large in-memory buffers ASAP. The downloader instance may
        # still be referenced briefly by the worker thread; without this,
        # captured response bodies (potentially tens of MB) linger until GC.
        self.network_resources.clear()
        self.resource_cache.clear()
        if self.session is not None:
            try:
                self.session.close()
            except Exception:
                pass
            self.session = None
        soup.decompose()

        return True

    def _extract_site_knowledge(self, raw_html):
        """Extract clean markdown, headings, forms, links, JSON-LD, and API routes for LangGraph."""
        try:
            ksoup = BeautifulSoup(raw_html, 'html.parser')

            # Title & meta description
            title = (ksoup.title.string.strip() if ksoup.title and ksoup.title.string else '') or get_site_name(self.url)
            description = ''
            for meta in ksoup.find_all('meta'):
                mname = (meta.get('name') or meta.get('property') or '').lower()
                if mname in ('description', 'og:description', 'twitter:description') and meta.get('content'):
                    description = meta.get('content').strip()
                    break

            # Structured data (JSON-LD)
            structured_data = []
            for ld in ksoup.find_all('script', type='application/ld+json'):
                if ld.string:
                    try:
                        structured_data.append(json.loads(ld.string.strip()))
                    except Exception:
                        pass

            # Headings hierarchy
            headings = []
            for h in ksoup.find_all(['h1', 'h2', 'h3']):
                txt = h.get_text(" ", strip=True)
                if txt and len(txt) < 200:
                    headings.append({'level': h.name, 'text': txt})

            # Interactive forms and inputs
            forms = []
            for form in ksoup.find_all('form')[:15]:
                inputs = []
                for inp in form.find_all(['input', 'select', 'textarea', 'button'])[:15]:
                    inputs.append({
                        'tag': inp.name,
                        'type': inp.get('type', 'text'),
                        'name': inp.get('name') or inp.get('id') or '',
                        'placeholder': inp.get('placeholder', ''),
                        'text': inp.get_text(" ", strip=True)[:60],
                    })
                forms.append({
                    'action': urljoin(self.base_url, form.get('action', '')),
                    'method': (form.get('method') or 'GET').upper(),
                    'inputs': inputs,
                })

            # Standalone inputs (common in SPAs without <form>)
            standalone_inputs = []
            for inp in ksoup.find_all(['input', 'select', 'textarea'])[:20]:
                if not inp.find_parent('form'):
                    standalone_inputs.append({
                        'tag': inp.name,
                        'type': inp.get('type', 'text'),
                        'name': inp.get('name') or inp.get('id') or '',
                        'placeholder': inp.get('placeholder', ''),
                        'aria_label': inp.get('aria-label', ''),
                    })

            # Navigation & action links
            links = []
            seen_hrefs = set()
            for a in ksoup.find_all('a', href=True):
                href = a['href'].strip()
                txt = a.get_text(" ", strip=True)
                if not href or href.startswith(('#', 'javascript:', 'mailto:', 'tel:')):
                    continue
                abs_href = urljoin(self.base_url, href)
                if abs_href not in seen_hrefs and txt:
                    seen_hrefs.add(abs_href)
                    links.append({'text': txt[:80], 'url': abs_href})
                if len(links) >= 40:
                    break

            # Remove noise tags before converting to Markdown
            for tag in ksoup.find_all(['script', 'style', 'noscript', 'svg', 'iframe']):
                tag.decompose()

            if md_convert is not None:
                markdown_text = md_convert(str(ksoup.body or ksoup), heading_style="ATX")
            else:
                markdown_text = ksoup.get_text("\n", strip=True)

            # Collapse excessive blank lines
            markdown_text = re.sub(r'\n{3,}', '\n\n', markdown_text).strip()

            apis_list = list(self.captured_apis.values())
            products_list = list(self.captured_products.values())[:20]

            return {
                'url': self.url,
                'final_url': self.base_url,
                'site_name': get_site_name(self.url),
                'title': title,
                'description': description,
                'headings': headings[:50],
                'forms': forms,
                'standalone_inputs': standalone_inputs,
                'links': links,
                'structured_data': structured_data[:5],
                'apis': apis_list,
                'products_sample': products_list,
                'markdown_content': markdown_text[:28000],
                'stats': {
                    'headings_count': len(headings),
                    'forms_count': len(forms),
                    'standalone_inputs_count': len(standalone_inputs),
                    'links_count': len(links),
                    'apis_count': len(apis_list),
                    'products_count': len(self.captured_products),
                    'markdown_chars': len(markdown_text),
                },
            }
        except Exception as e:
            self.log(f"⚠️ Aviso na extração de conhecimento: {e}")
            return {
                'url': self.url,
                'final_url': self.base_url,
                'site_name': get_site_name(self.url),
                'title': get_site_name(self.url),
                'description': '',
                'headings': [],
                'forms': [],
                'standalone_inputs': [],
                'links': [],
                'structured_data': [],
                'apis': list(self.captured_apis.values()),
                'products_sample': [],
                'markdown_content': '',
                'stats': {'apis_count': len(self.captured_apis)},
            }

    def _scroll_page(self, page):
        """Scroll the page to trigger lazy loading"""
        try:
            # First, try to disable smooth scroll libraries (Lenis, Locomotive, etc.)
            page.evaluate("""
                () => {
                    // Disable Lenis smooth scroll
                    if (window.lenis) {
                        try { window.lenis.destroy(); } catch(e) {}
                    }
                    // Disable Locomotive Scroll
                    if (window.locomotiveScroll) {
                        try { window.locomotiveScroll.destroy(); } catch(e) {}
                    }
                    // Reset any scroll-behavior smooth
                    document.documentElement.style.scrollBehavior = 'auto';
                    document.body.style.scrollBehavior = 'auto';
                    
                    // Remove overflow hidden that might prevent scrolling
                    if (getComputedStyle(document.body).overflow === 'hidden') {
                        document.body.style.overflow = 'auto';
                    }
                    if (getComputedStyle(document.documentElement).overflow === 'hidden') {
                        document.documentElement.style.overflow = 'auto';
                    }
                }
            """)
            
            # Find the actual scroll container (some sites use custom containers)
            scroll_container = page.evaluate("""
                () => {
                    // Check for common scroll container patterns
                    const selectors = [
                        '[data-scroll-container]',
                        '.scroll-container',
                        '.smooth-scroll',
                        'main',
                        '#__next',
                        '#__nuxt',
                        '#app'
                    ];
                    
                    for (const sel of selectors) {
                        const el = document.querySelector(sel);
                        if (el && el.scrollHeight > window.innerHeight) {
                            return sel;
                        }
                    }
                    return null;
                }
            """)
            
            if scroll_container:
                self.log(f"🔍 Detectado container de scroll customizado: {scroll_container}")
            
            total_height = page.evaluate("Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)")
            viewport_height = page.evaluate("window.innerHeight")
            
            # Limit scroll iterations to prevent infinite loops
            max_iterations = 20
            iteration = 0
            
            current = 0
            while current < total_height and iteration < max_iterations:
                # Scroll using multiple methods for better compatibility
                page.evaluate(f"""
                    (pos) => {{
                        window.scrollTo(0, pos);
                        document.documentElement.scrollTop = pos;
                        document.body.scrollTop = pos;
                        
                        // Also try scrolling custom containers
                        const containers = document.querySelectorAll('[data-scroll-container], .scroll-container, main');
                        containers.forEach(c => {{ c.scrollTop = pos; }});
                    }}
                """, current)
                
                page.wait_for_timeout(600)
                current += viewport_height
                iteration += 1
                
                new_height = page.evaluate("Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)")
                if new_height > total_height:
                    total_height = new_height
            
            # Scroll back to top
            page.evaluate("""
                () => {
                    window.scrollTo(0, 0);
                    document.documentElement.scrollTop = 0;
                    document.body.scrollTop = 0;
                }
            """)
            page.wait_for_timeout(1000)
        except Exception as e:
            self.log(f"⚠️ Erro no scroll: {e}")


def get_site_name(url):
    """Extract a clean site name from URL for the zip filename"""
    parsed = urlparse(url)
    # Get domain without www
    domain = parsed.netloc.replace('www.', '')
    # Clean special characters
    clean_name = re.sub(r'[^a-zA-Z0-9.-]', '_', domain)
    # Add path info if present (cleaned)
    if parsed.path and parsed.path != '/':
        path_part = re.sub(r'[^a-zA-Z0-9]', '_', parsed.path.strip('/'))[:30]
        clean_name = f"{clean_name}_{path_part}"
    return clean_name


def zip_directory(folder_path, output_path):
    """Create a zip file from a directory"""
    base_name = output_path.replace('.zip', '')
    shutil.make_archive(base_name, 'zip', folder_path)
    return base_name + '.zip'
