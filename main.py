# -*- coding: utf-8 -*-
import asyncio
import os
import re
import sys
import argparse # Ajout de l'import pour les arguments
from playwright.async_api import async_playwright, Error as PlaywrightError
from urllib.parse import urlparse
from flask import Flask, render_template_string, url_for, send_from_directory, abort
import logging

# --- Configuration (sera définie par argparse plus bas) ---
# DISCOVER_MODE = False # Valeur par défaut, sera écrasée par l'argument

PAGE_URL = "https://www.website.com"

PREDEFINED_BLOCK_LIST = [
    "cdns.eu1.gigya.com/js/gigya.js",
    "js-agent.newrelic.com/nr-spa",
    "sdk.privacy-center.org/",
    "securepubads.g.doubleclick.net/pagead/managed/js/gpt/",
    "googletagservices.com/tag/js/gpt.js",
]

OUTPUT_DIR = "screenshots_playwright"
os.makedirs(OUTPUT_DIR, exist_ok=True)

WAIT_AFTER_LOAD = 3500

# --- Variables Globales ---
test_results = []
flask_app = Flask(__name__)
flask_app.config['OUTPUT_DIR'] = OUTPUT_DIR
DISCOVER_MODE = False # Sera défini par argparse

# --- Fonctions Utilitaires ---
def sanitize_filename(url_part):
    if not url_part:
        return "reference"
    if url_part == "TOUT_BLOQUER":
        return "bloque_tout"

    try:
        parsed = urlparse(url_part)
        path_parts = [p for p in parsed.path.split('/') if p]

        if path_parts:
            name = path_parts[-1].split('?')[0].split(';')[0]
            name = name[:50]
            if not name or len(name) < 5 or '.' not in name[-5:]:
                 first_path = path_parts[0][:20] if path_parts else ''
                 domain = parsed.netloc or 'local'
                 name = f"{domain}_{first_path}_{name}".strip('_') if first_path else f"{domain}_{name}".strip('_')
            elif parsed.netloc:
                 name = f"{parsed.netloc}_{name}"

        elif parsed.netloc:
            name = parsed.netloc
        else:
            name = url_part.split('/')[-1].split('?')[0] or "ressource_simple"
            if not name: name = "ressource_inconnue"

    except Exception:
         name = url_part.replace('https://','').replace('http://','').replace('/','_').replace(':','_').replace('.','_')
         if not name: name = "ressource_inconnue"

    sanitized = re.sub(r'[^\w\-_\.]', '_', name)
    sanitized = re.sub(r'_+', '_', sanitized).strip('._')
    return sanitized[:100]

# --- Logique Playwright ---
discovered_resource_paths = set()
page_url_parsed = urlparse(PAGE_URL)
page_url_base_path = f"{page_url_parsed.scheme}://{page_url_parsed.netloc}{page_url_parsed.path}"

async def handle_response_for_discovery(response):
    url = response.url
    try:
        parsed_url = urlparse(url)

        if parsed_url.scheme not in ['http', 'https']:
            return

        url_base = f"{parsed_url.scheme}://{parsed_url.netloc}{parsed_url.path}"

        if url_base.rstrip('/') == page_url_base_path.rstrip('/'):
            return

        if url_base not in discovered_resource_paths:
             content_type = response.header_value("content-type") or "N/A"
             # print(f"  [Découverte] Ressource: {url_base} (Type: {content_type.split(';')[0]})") # Optionnel: décommenter pour voir toutes les découvertes
             discovered_resource_paths.add(url_base)

    except Exception as e:
        print(f"  Avertissement: Impossible d'analyser la réponse pour {url}: {e}")


async def block_request(route, request, blocked_reason="ressource"):
    # print(f"  >> Bloquant ({blocked_reason[:20]}...): {request.url[:80]}...") # Optionnel: décommenter pour voir chaque blocage
    try:
        await route.abort()
    except PlaywrightError as e:
        if "Target page, context or browser has been closed" not in str(e) and "Request context is destroyed" not in str(e):
            print(f"  Avertissement : Erreur lors de l'abort (peut être normal): {e}")

async def run_test(browser, url_to_block, file_prefix, reason_suffix, is_combined_block=False, block_list_for_all=None):
    global test_results
    is_reference = url_to_block is None
    name_for_file = url_to_block or "reference"
    current_blocked_item = "Aucun (Référence)"
    if is_combined_block:
        name_for_file = "tout"
        current_blocked_item = "TOUT"
    elif url_to_block:
        current_blocked_item = url_to_block


    filename_base = sanitize_filename(name_for_file)
    screenshot_filename = f"{file_prefix}_{filename_base}{reason_suffix}.png"
    screenshot_path = os.path.join(OUTPUT_DIR, screenshot_filename)
    error_screenshot_filename = f"{file_prefix}_{filename_base}{reason_suffix}_ERREUR.png"
    error_screenshot_path = os.path.join(OUTPUT_DIR, error_screenshot_filename)

    print(f"\n--- Test {file_prefix}: Blocage de '{current_blocked_item}' ---")

    result_data = {
        'name': name_for_file,
        'screenshot_file': screenshot_filename,
        'error': False,
        'error_message': None,
        'prefix': file_prefix,
        'suffix': reason_suffix,
        'blocked_item': current_blocked_item
    }

    context = None
    page = None
    try:
        context = await browser.new_context(
             user_agent="Mozilla/5.0 (Linux; Android 6.0.1; Nexus 5X Build/MMB29P) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/W.X.Y.Z Mobile Safari/537.36 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
        )

        if not is_reference:
            if is_combined_block:
                list_to_use = block_list_for_all or []
                if not list_to_use:
                    print("  Avertissement: La liste pour le blocage combiné est vide.")
                else:
                    def should_block_all(url_str):
                         if DISCOVER_MODE:
                             return any(url_str.startswith(path) for path in list_to_use)
                         else:
                             return any(part in url_str for part in list_to_use)
                    await context.route(
                        should_block_all,
                        lambda route, request: asyncio.ensure_future(block_request(route, request, "TOUT"))
                    )
                    print(f"  Règle de blocage activée pour TOUTES les {len(list_to_use)} ressources.")
            else:
                block_condition = lambda url_str: url_str.startswith(url_to_block) if DISCOVER_MODE else (url_to_block in url_str)
                await context.route(
                    block_condition,
                    lambda route, request, ub=url_to_block: asyncio.ensure_future(block_request(route, request, ub[:30]))
                )
                print(f"  Règle de blocage activée pour: {url_to_block}")

        page = await context.new_page()

        try:
            print(f"  Navigation vers {PAGE_URL}...")
            await page.goto(PAGE_URL, wait_until="networkidle", timeout=60000)
            print(f"  Page chargée ('networkidle'). Prise de capture...")
            await page.screenshot(path=screenshot_path, full_page=True)
            print(f"  Capture d'écran sauvegardée: {screenshot_path}")

        except Exception as e_nav:
             print(f"  ERREUR pendant la navigation/capture pour {name_for_file}: {e_nav}")
             result_data['error'] = True
             result_data['error_message'] = str(e_nav)
             result_data['screenshot_file'] = error_screenshot_filename
             try:
                 if page and not page.is_closed():
                    await page.screenshot(path=error_screenshot_path, full_page=True)
                    print(f"  Capture d'écran d'erreur sauvegardée: {error_screenshot_path}")
             except Exception as e_shot:
                 print(f"  Impossible de prendre une capture d'écran même après erreur: {e_shot}")

    except Exception as e_ctx:
        print(f"  ERREUR lors de la création/gestion du contexte pour {name_for_file}: {e_ctx}")
        result_data['error'] = True
        result_data['error_message'] = f"Erreur de contexte: {e_ctx}"
        result_data['screenshot_file'] = error_screenshot_filename
    finally:
        if page and not page.is_closed():
            await page.close()
        if context:
            try:
                await context.close()
            except Exception: pass
        test_results.append(result_data)

async def run_playwright_tests():
    global discovered_resource_paths, test_results
    test_results = []
    discovered_resource_paths = set()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        print(f"Navigateur lancé. Tests sur: {PAGE_URL}")

        await run_test(browser, None, "00", "_reference")

        urls_to_test = []
        list_for_all_block = []
        reason = ""

        if DISCOVER_MODE:
            print("\n--- Phase de Découverte de TOUTES les ressources ---")
            print("ATTENTION: Ce mode peut être très long et générer beaucoup de captures.")
            context_discover = None
            page_discover = None
            try:
                context_discover = await browser.new_context(
                    user_agent="Mozilla/5.0 (Linux; Android 6.0.1; Nexus 5X Build/MMB29P) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/W.X.Y.Z Mobile Safari/537.36 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
                )
                page_discover = await context_discover.new_page()
                page_discover.on("response", handle_response_for_discovery)
                print(f"  Navigation vers {PAGE_URL} pour découverte...")
                await page_discover.goto(PAGE_URL, wait_until="networkidle", timeout=90000)
                print(f"  Page chargée ('networkidle'). Fin de la découverte.")
                page_discover.remove_listener("response", handle_response_for_discovery)
                print(f"--- Découverte terminée : {len(discovered_resource_paths)} URL(s) de base de ressources trouvée(s) ---")
                urls_to_test = sorted(list(discovered_resource_paths))
                list_for_all_block = urls_to_test
                reason = "_decouvert"

            except Exception as e_discover:
                print(f"  ERREUR pendant la phase de découverte : {e_discover}")
            finally:
                 if page_discover and not page_discover.is_closed():
                     await page_discover.close()
                 if context_discover:
                     try:
                         await context_discover.close()
                     except Exception: pass
        else:
             print("\n--- Utilisation de la liste prédéfinie PREDEFINED_BLOCK_LIST ---")
             urls_to_test = PREDEFINED_BLOCK_LIST
             list_for_all_block = PREDEFINED_BLOCK_LIST
             reason = "_predefini"

        if not urls_to_test:
             print("\nATTENTION: Aucune URL à tester n'a été trouvée ou définie.")
        else:
            print(f"\n--- Lancement des {len(urls_to_test)} tests de blocage individuel ---")
            for i, url_to_block in enumerate(urls_to_test):
                 await run_test(browser, url_to_block, f"{i+1:02d}", reason)

            await run_test(browser, "TOUT_BLOQUER", "99", "_tout", is_combined_block=True, block_list_for_all=list_for_all_block)

        await browser.close()
        print("\n--- Tests Playwright terminés ---")
        print(f"Captures sauvegardées dans le dossier: {OUTPUT_DIR}")
        test_results.sort(key=lambda x: (int(x['prefix']) if x['prefix'].isdigit() else 999, x['suffix']))
        return True

# --- Logique Flask ---

FLASK_TEMPLATE = """
<!DOCTYPE html>

<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Résultats Tests Playwright+</title>
    <style>
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; line-height: 1.5; margin: 0; padding: 0; background-color: #f8f9fa; color: #212529; }
        .container { max-width: 95%; margin: 20px auto; background: white; padding: 25px; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
        h1 { text-align: center; color: #0b0426; margin-bottom: 10px; }
        h2 { text-align: center; color: #495057; font-weight: 400; margin-top: 0; margin-bottom: 30px; font-size: 1.1em; word-wrap: break-word;}
        .test-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(350px, 1fr)); gap: 20px; }
        .test-case { border: 1px solid #dee2e6; border-radius: 5px; background-color: #fff; padding: 15px; transition: box-shadow 0.3s ease; overflow: hidden;}
        .test-case:hover { box-shadow: 0 4px 12px rgba(0,0,0,0.1); }
        .test-case.error { border-left: 5px solid #dc3545; background-color: #f8d7da; border-color: #f5c6cb;}
        .test-case h3 { margin-top: 0; margin-bottom: 8px; color: #0b0426; font-size: 1em; word-wrap: break-word; }
        .test-case p { margin: 5px 0; font-size: 0.9em; color: #6c757d; }
        .test-case strong { color: #495057; }
        .screenshot-container { margin-top: 10px; text-align: center; background-color: #e9ecef; padding: 5px; border-radius: 4px;}
        .screenshot { max-width: 100%; height: auto; border: 1px solid #ced4da; border-radius: 4px; cursor: pointer; transition: transform 0.2s ease; display: block; }
        .screenshot:hover { transform: scale(1.03); }
        .error-message { color: #721c24; background-color: #f8d7da; border: 1px solid #f5c6cb; padding: 5px 8px; border-radius: 4px; font-size: 0.85em; margin-top: 10px; word-wrap: break-word; }
        .blocked-item { font-family: monospace; font-size: 0.85em; color: #0056b3; display: block; max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: normal; word-wrap:break-word; line-height: 1.2;}
        .fullscreen-overlay { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background-color: rgba(0,0,0,0.9); justify-content: center; align-items: center; z-index: 1000; cursor: zoom-out; padding: 10px; box-sizing: border-box;}
        .fullscreen-image { max-width: 100%; max-height: 100%; object-fit: contain; box-shadow: 0 0 30px rgba(0,0,0,0.5); }
        .mode-info { text-align: center; margin-bottom: 20px; font-size: 0.95em; color: #6c757d; padding: 10px; background-color: #e9ecef; border-radius: 4px;}
    </style>
</head>
<body>
    <div class="container">
        <h1>Résultats des Tests de Blocage</h1>
        <h2>URL Testée: {{ page_url }}</h2>
        <p class="mode-info">Mode utilisé: <strong>{{ 'Découverte de toutes les ressources' if discover_mode else 'Liste Prédéfinie' }}</strong></p>

        <div class="test-grid">
            {% for result in results %}
            <div class="test-case {% if result.error %}error{% endif %}">
                <h3>Test {{ result.prefix }}: {{ result.name | e }}{{ result.suffix | e }}</h3>
                <p><strong>Élément bloqué:</strong> <span class="blocked-item" title="{{ result.blocked_item | e if result.blocked_item else 'Aucun (Référence)' }}">{{ result.blocked_item | e if result.blocked_item else 'Aucun (Référence)' }}</span></p>
                {% if result.error %}
                    <div class="error-message"><strong>Erreur:</strong> {{ result.error_message | e }}</div>
                {% endif %}
                <div class="screenshot-container">
                    <img src="{{ url_for('serve_screenshot', filename=result.screenshot_file) }}"
                         alt="Capture pour {{ result.name | e }}"
                         class="screenshot"
                         loading="lazy"
                         onclick="showFullscreen('{{ url_for('serve_screenshot', filename=result.screenshot_file) }}')">
                 </div>
            </div>
            {% else %}
            <p>Aucun résultat de test trouvé.</p>
            {% endfor %}
        </div>
    </div>

    <div id="fullscreen-overlay" class="fullscreen-overlay" onclick="hideFullscreen()">
        <img id="fullscreen-image" src="" alt="Fullscreen Screenshot" class="fullscreen-image">
    </div>

    <script>
        const overlay = document.getElementById('fullscreen-overlay');
        const fsImage = document.getElementById('fullscreen-image');
        function showFullscreen(src) {
            fsImage.src = src;
            overlay.style.display = 'flex';
            document.body.style.overflow = 'hidden';
        }
        function hideFullscreen() {
            overlay.style.display = 'none';
            fsImage.src = '';
            document.body.style.overflow = '';
        }
        document.addEventListener('keydown', function(event) {
            if (event.key === 'Escape') {
                hideFullscreen();
            }
        });
    </script>
</body>
</html>
"""

@flask_app.route('/')
def index():
    # Les résultats sont triés à la fin de run_playwright_tests
    return render_template_string(FLASK_TEMPLATE, results=test_results, page_url=PAGE_URL, discover_mode=DISCOVER_MODE)

@flask_app.route('/screenshots/<path:filename>')
def serve_screenshot(filename):
    safe_dir = os.path.abspath(flask_app.config['OUTPUT_DIR'])
    file_path = os.path.abspath(os.path.join(safe_dir, filename))
    if not file_path.startswith(safe_dir):
        print(f"Tentative d'accès non autorisé bloquée : {filename}")
        abort(404)
    try:
        # Utilise max_age=0 pour éviter la mise en cache agressive du navigateur
        return send_from_directory(safe_dir, filename, max_age=0)
    except FileNotFoundError:
        print(f"Fichier non trouvé: {filename}")
        abort(404)

# --- Exécution ---
if __name__ == "__main__":
    # --- Gestion des Arguments ---
    parser = argparse.ArgumentParser(description="Teste le rendu d'une page en bloquant des ressources et affiche les résultats via Flask.")
    parser.add_argument(
        '--discover-all',
        action='store_true',
        default=False,
        help="Active le mode découverte pour bloquer toutes les ressources chargées une par une (peut être très long). Par défaut, utilise la liste prédéfinie."
    )
    parser.add_argument(
        '--port',
        type=int,
        default=5001,
        help="Port pour le serveur Flask (défaut: 5001)."
    )
    args = parser.parse_args()

    # Définit le mode global basé sur l'argument
    DISCOVER_MODE = args.discover_all
    FLASK_PORT = args.port

    # --- Vérification URL ---
    if PAGE_URL == "URL_DE_LA_PAGE_PLUS_A_TESTER":
         print("\n!!! ERREUR CRITIQUE !!!")
         print("L'URL de la page à tester n'a pas été définie dans le script.")
         print("Veuillez modifier la variable 'PAGE_URL' et relancer.")
         sys.exit(1)

    print(f"--- MODE SELECTIONNE: {'DECOUVERTE DE TOUTES RESSOURCES' if DISCOVER_MODE else 'LISTE PREDEFINIE'} ---")
    if DISCOVER_MODE:
        print("--- NOTE: Le mode Découverte peut être très long et générer beaucoup de captures. ---")

    # --- Lancement Playwright ---
    tests_completed = asyncio.run(run_playwright_tests())

    # --- Lancement Flask ---
    if tests_completed and test_results:
        print("\n--- Lancement de l'interface web Flask ---")
        print(f"Ouvrez votre navigateur et allez à l'adresse: http://127.0.0.1:{FLASK_PORT}")
        print("Appuyez sur CTRL+C dans ce terminal pour arrêter le serveur.")
        try:
             try:
                 from waitress import serve
                 print(f"Utilisation du serveur Waitress sur le port {FLASK_PORT}.")
                 serve(flask_app, host='127.0.0.1', port=FLASK_PORT, threads=6)
             except ImportError:
                 print(f"Waitress non trouvé, utilisation du serveur de développement Flask sur le port {FLASK_PORT} (moins stable).")
                 flask_app.run(host='127.0.0.1', port=FLASK_PORT, debug=False, use_reloader=False)

        except OSError as e:
            if "address already in use" in str(e).lower():
                 print(f"\nERREUR: Le port {FLASK_PORT} est déjà utilisé.")
                 print("Essayez d'arrêter l'application qui utilise ce port ou utilisez un autre port avec l'option --port.")
                 print("Exemple: python votre_script.py --port 5002")
            else:
                print(f"\nErreur lors du lancement de Flask : {e}")
            print("Les captures d'écran sont disponibles dans le dossier:", OUTPUT_DIR)
        except Exception as e:
            print(f"\nErreur lors du lancement de Flask : {e}")
            print("Les captures d'écran sont disponibles dans le dossier:", OUTPUT_DIR)

    elif not test_results:
         print("\nAucun résultat de test à afficher. Le serveur Flask ne sera pas lancé.")
    else:
        print("\nLes tests Playwright n'ont pas pu démarrer correctement. Le serveur Flask ne sera pas lancé.")

    print("\nScript terminé.")
