# -*- coding: utf-8 -*-
import asyncio
import os
import re
import sys
import argparse # Added import for arguments
from playwright.async_api import async_playwright, Error as PlaywrightError
from urllib.parse import urlparse, urlunparse, quote as url_quote
from flask import Flask, render_template_string, url_for, send_from_directory, abort, request, redirect, jsonify
import logging
import threading
import time
from gpyrobotstxt.robots_cc import RobotsMatcher
from collections import defaultdict
from playwright.sync_api import sync_playwright
import requests

# --- Configuration ---
# DISCOVER_MODE will be set by argparse
# PAGE_URL will be set by argparse

PREDEFINED_BLOCK_LIST = []

OUTPUT_DIR = "screenshots_playwright"
os.makedirs(OUTPUT_DIR, exist_ok=True)

# --- Global Variables ---
test_results = [] # Stores results for Flask display
flask_app = Flask(__name__)
flask_app.config['OUTPUT_DIR'] = OUTPUT_DIR
DISCOVER_MODE = False # Will be set by argparse
PAGE_URL = None # Will be set by argparse or the form
test_status = "idle" # idle, running, completed, error
test_log = [] # To store logs for live updates
predefined_urls = [] # To store the list of URLs to block

# --- Utility Functions ---
def sanitize_filename(url_part):
    """Creates a safe filename from a URL or identifier."""
    if not url_part:
        return "reference"
    if url_part == "BLOCK_ALL": # Changed from TOUT_BLOQUER
        return "block_all"

    try:
        # Try parsing as URL
        parsed = urlparse(url_part)
        path_parts = [p for p in parsed.path.split('/') if p] # Get non-empty path components

        if path_parts:
            # Use the last part of the path (filename)
            name = path_parts[-1].split('?')[0].split(';')[0] # Remove query/params
            name = name[:50] # Limit length

            # If the name is short, generic, or lacks extension, add domain/path context
            if not name or len(name) < 5 or '.' not in name[-5:]:
                first_path = path_parts[0][:20] if path_parts else ''
                domain = parsed.netloc or 'local'
                # Construct name: domain_firstpath_filename or domain_filename
                name = f"{domain}_{first_path}_{name}".strip('_') if first_path else f"{domain}_{name}".strip('_')
            elif parsed.netloc:
                 # Prepend domain if filename seems reasonable
                 name = f"{parsed.netloc}_{name}"

        elif parsed.netloc:
            # No path, use domain name
            name = parsed.netloc
        else:
            # Not a standard URL, try getting last part after '/'
            name = url_part.split('/')[-1].split('?')[0] or "simple_resource"
            if not name: name = "unknown_resource" # Changed from ressource_inconnue

    except Exception:
        # Fallback for invalid URLs or other errors
        name = url_part.replace('https://','').replace('http://','').replace('/','_').replace(':','_').replace('.','_')
        if not name: name = "unknown_resource" # Changed from ressource_inconnue

    # Sanitize: remove invalid characters, replace multiple underscores, trim ends
    sanitized = re.sub(r'[^\w\-_\.]', '_', name)
    sanitized = re.sub(r'_+', '_', sanitized).strip('._')
    return sanitized[:100] # Limit final length

# --- Logging for Flask UI ---
def log_message(message):
    """Adds a timestamped message to the test log."""
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print(message) # Also print to console
    test_log.append(f"[{timestamp}] {message}")

# --- Playwright Logic ---
discovered_resource_paths = set()
page_url_parsed = None # Will be set when PAGE_URL is known
page_url_base_path = None # Will be set when PAGE_URL is known

async def handle_response_for_discovery(response):
    """Callback to discover resource URLs during the initial page load.
       Only adds full URLs (with query) for resources matching video/API patterns.
    """
    global discovered_resource_paths, page_url_base_path
    full_url = response.url # Utiliser l'URL complète dès le début
    try:
        parsed_url = urlparse(full_url)

        # Ignore non-http/https schemes
        if parsed_url.scheme not in ['http', 'https']:
            return

        # Get the base URL (scheme://netloc/path) for pattern matching only
        url_base = f"{parsed_url.scheme}://{parsed_url.netloc}{parsed_url.path}"

        # Ignore the main page URL itself (comparison based on base path)
        if page_url_base_path and url_base.rstrip('/') == page_url_base_path.rstrip('/'):
            return

        # Vérifier si l'URL complète contient des paramètres de requête
        has_query_params = '?' in full_url

        # If it's a resource with query parameters and we haven't seen this *full* URL yet, add the full URL
        if has_query_params:
             if full_url not in discovered_resource_paths:
                 content_type = await response.header_value("content-type") or "N/A"
                 log_message(f"  [Discovery] Ressource avec paramètres trouvée: {full_url} (Type: {content_type.split(';')[0]})")
                 discovered_resource_paths.add(full_url) # Ajoute l'URL complète

        # Note: Resources without query parameters are now ignored in discovery mode

    except Exception as e:
        log_message(f"  Warning: Could not parse response for {full_url}: {e}")


async def block_request_handler(route, request, blocked_reason="resource"):
    """Callback to block a request."""
    # Log the exact URL, type, and frame URL being blocked
    resource_type = request.resource_type
    frame_url = request.frame.url
    log_message(f"  >> Blocking Request (Reason: {blocked_reason}, Type: {resource_type}, Frame: {frame_url}): {request.url}")
    # Optional log (kept commented)
    # log_message(f"  >> Blocking ({blocked_reason[:20]}...): {request.url[:80]}...")
    try:
        await route.abort()
    except PlaywrightError as e:
        # Ignore errors caused by the page/context closing during abort
        if "Target page, context or browser has been closed" not in str(e) and "Request context is destroyed" not in str(e):
            log_message(f"  Warning: Error during abort (might be normal): {e}")

class RobotsChecker:
    """Classe pour gérer la vérification des robots.txt avec le parser officiel de Google."""
    
    def __init__(self):
        self.matcher = RobotsMatcher()
        self.robots_cache = {}  # Cache des contenus robots.txt par domaine
        self.results = defaultdict(dict)  # Résultats des vérifications par domaine et chemin
        
    def check_url_allowed(self, url, user_agent="Googlebot"):
        """Vérifie si une URL est autorisée pour un user-agent donné."""
        try:
            parsed_url = urlparse(url)
            if not parsed_url.netloc:
                log_message(f"  Erreur: URL invalide sans domaine: {url}")
                return True

            # Construire l'URL du robots.txt pour ce domaine
            domain = parsed_url.netloc
            scheme = parsed_url.scheme or "https"
            robots_txt_url = f"{scheme}://{domain}/robots.txt"
            
            # Vérifier si nous avons déjà le contenu du robots.txt en cache
            if domain not in self.robots_cache:
                try:
                    response = requests.get(robots_txt_url, timeout=10)
                    if response.status_code == 200:
                        self.robots_cache[domain] = response.content
                    else:
                        log_message(f"  Pas de robots.txt trouvé pour {domain} (status: {response.status_code})")
                        self.robots_cache[domain] = b""  # Cache vide si pas de robots.txt
                except Exception as e:
                    log_message(f"  Erreur lors du chargement du robots.txt pour {domain}: {e}")
                    self.robots_cache[domain] = b""  # Cache vide en cas d'erreur
            
            # Obtenir le chemin complet avec query string
            path_with_query = parsed_url.path
            if parsed_url.query:
                path_with_query += "?" + parsed_url.query
            
            # Vérifier si nous avons déjà le résultat en cache
            if path_with_query in self.results[domain]:
                return self.results[domain][path_with_query]
            
            # Vérifier l'autorisation avec le parser Google
            is_allowed = self.matcher.allowed_by_robots(
                self.robots_cache[domain],
                [user_agent],
                url  # gpyrobotstxt requiert l'URL complète
            )
            
            # Mettre en cache le résultat
            self.results[domain][path_with_query] = is_allowed
            
            log_message(f"  Vérification {robots_txt_url} pour {path_with_query}: {'autorisé' if is_allowed else 'non autorisé'}")
            return is_allowed
            
        except Exception as e:
            log_message(f"  Erreur lors de la vérification robots.txt pour {url}: {e}")
            return True

# Créer une instance globale du RobotsChecker
robots_checker = RobotsChecker()

async def run_single_test(browser, url_to_block, file_prefix, reason_suffix, is_combined_block=False, block_list_for_all=None):
    """Runs a single Playwright test case (reference or blocking one/all resources)."""
    global test_results

    is_reference = url_to_block is None
    name_for_file = url_to_block or "reference"
    current_blocked_item = "None (Reference)"
    if is_combined_block:
        name_for_file = "all"
        current_blocked_item = "BLOCK_ALL"
    elif url_to_block:
        current_blocked_item = url_to_block
        # Vérifier si la ressource est autorisée pour Googlebot
        is_allowed = robots_checker.check_url_allowed(url_to_block)
        log_message(f"  Ressource {url_to_block} {'autorisée' if is_allowed else 'non autorisée'} pour Googlebot")

    # Generate filenames
    filename_base = sanitize_filename(name_for_file)
    screenshot_filename = f"{file_prefix}_{filename_base}{reason_suffix}.png"
    screenshot_path = os.path.join(OUTPUT_DIR, screenshot_filename)
    error_screenshot_filename = f"{file_prefix}_{filename_base}{reason_suffix}_ERROR.png"
    error_screenshot_path = os.path.join(OUTPUT_DIR, error_screenshot_filename)

    log_message(f"\n--- Test {file_prefix}: Blocking '{current_blocked_item}' ---")

    # Data structure to store results for this test
    result_data = {
        'name': name_for_file,
        'screenshot_file': screenshot_filename,
        'error': False,
        'error_message': None,
        'prefix': file_prefix,
        'suffix': reason_suffix,
        'blocked_item': current_blocked_item,
        'googlebot_allowed': True if is_reference else (robots_checker.check_url_allowed(url_to_block) if url_to_block else None)
    }

    context = None
    page = None
    try:
        # Create a new browser context with a specific user agent
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Linux; Android 6.0.1; Nexus 5X Build/MMB29P) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/W.X.Y.Z Mobile Safari/537.36 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
        )

        # Set up request blocking if not the reference run
        if not is_reference:
            if is_combined_block:
                # Block all URLs specified in the list
                list_to_use = block_list_for_all or []
                if not list_to_use:
                    log_message("  Warning: The list for combined blocking is empty.")
                else:
                    # Define the blocking condition based on DISCOVER_MODE
                    if DISCOVER_MODE:
                        # Exact path match (startswith) and pattern matching
                        def should_block_all(url_str):
                            # Vérifier les correspondances exactes
                            if any(url_str.startswith(path) for path in list_to_use):
                                return True
                            # Vérifier les patterns pour les ressources vidéo/API
                            parsed_url = urlparse(url_str)
                            url_base = f"{parsed_url.scheme}://{parsed_url.netloc}{parsed_url.path}"
                            video_patterns = ['video', 'media', 'player', 'stream', 'api', 'layout.6cloud.fr',
                                           'token', 'clip', '/v1/', '/v2/', '/v3/']
                            return any(pattern in url_base.lower() for pattern in video_patterns)
                    else:
                        # Substring match (contains)
                        def should_block_all(url_str):
                             return any(part in url_str for part in list_to_use)

                    # Route matching requests to the block handler
                    await context.route(
                        should_block_all,
                        lambda route, request: asyncio.ensure_future(block_request_handler(route, request, "BLOCK_ALL"))
                    )
                    log_message(f"  Blocking rule enabled for ALL {len(list_to_use)} resources.")
            else:
                # Block a single URL pattern
                if DISCOVER_MODE:
                    # Compare base paths, ignoring query parameters
                    def block_condition(url_str):
                         # Direct comparison since url_to_block now includes query params
                         return url_str == url_to_block
                    # ====> FIN DU REMPLACEMENT <====
                else:
                    # Substring match (contains)
                    block_condition = lambda url_str: url_to_block in url_str

                # Route matching requests to the block handler
                await context.route(
                    block_condition,
                    lambda route, request, ub=url_to_block: asyncio.ensure_future(block_request_handler(route, request, ub[:30]))
                )
                log_message(f"  Blocking rule enabled for: {url_to_block}")

        # Create a new page in the context
        page = await context.new_page()

        try:
            # Navigate to the target page
            log_message(f"  Navigating to {PAGE_URL}...")
            # Wait until the network is idle (or timeout)
            await page.goto(PAGE_URL, wait_until="networkidle", timeout=90000) # Augmentation du timeout de goto à 90s
            log_message(f"  Page loaded ('networkidle'). Waiting briefly before screenshot...") # Log avant attente
            # >> Délai réduit <<
            await page.wait_for_timeout(2000) # Attendre 2 secondes (réduction)
            log_message(f"  Taking screenshot...")
            # Take a full-page screenshot with increased timeout
            await page.screenshot(path=screenshot_path, full_page=True, timeout=90000)
            log_message(f"  Screenshot saved: {screenshot_path}")

        except Exception as e_nav:
            # Handle navigation/screenshot errors
            log_message(f"  ERROR during navigation/screenshot for {name_for_file}: {e_nav}")
            result_data['error'] = True
            result_data['error_message'] = str(e_nav)
            result_data['screenshot_file'] = error_screenshot_filename # Use error filename
            try:
                # Try taking an error screenshot anyway, also with timeout
                if page and not page.is_closed():
                    await page.screenshot(path=error_screenshot_path, full_page=True, timeout=90000)
                    log_message(f"  Error screenshot saved: {error_screenshot_path}")
            except Exception as e_shot:
                log_message(f"  Could not take screenshot even after error: {e_shot}")

    except Exception as e_ctx:
        # Handle errors during context creation/management
        log_message(f"  ERROR during context creation/management for {name_for_file}: {e_ctx}")
        result_data['error'] = True
        result_data['error_message'] = f"Context error: {e_ctx}"
        result_data['screenshot_file'] = error_screenshot_filename # Use error filename
    finally:
        # Ensure page and context are closed
        if page and not page.is_closed():
            await page.close()
        if context:
            try:
                await context.close()
            except Exception: pass # Ignore errors during close
        test_results.append(result_data) # Add result to the global list

async def run_playwright_test_suite():
    """Runs the complete suite of Playwright tests (reference, individual blocks, all blocks)."""
    global discovered_resource_paths, test_results, page_url_parsed, page_url_base_path, test_status, test_log

    # --- Input Validation ---
    if not PAGE_URL or not (PAGE_URL.startswith("http://") or PAGE_URL.startswith("https://")):
        log_message("ERROR: Invalid or missing URL. Please provide a valid URL starting with http:// or https://")
        test_status = "error"
        return False

    # Reset state for a new run
    test_status = "running"
    test_results = []
    discovered_resource_paths = set()
    test_log = [] # Clear previous logs

    # Parse the main URL once
    try:
        page_url_parsed = urlparse(PAGE_URL)
        page_url_base_path = f"{page_url_parsed.scheme}://{page_url_parsed.netloc}{page_url_parsed.path}"
    except Exception as e:
        log_message(f"ERROR: Could not parse the provided URL '{PAGE_URL}': {e}")
        test_status = "error"
        return False

    log_message(f"--- Starting Playwright Tests ---")
    log_message(f"Target URL: {PAGE_URL}")
    log_message(f"Mode: {'Discovery' if DISCOVER_MODE else 'Predefined List'}")
    log_message(f"Output Directory: {OUTPUT_DIR}")

    async with async_playwright() as p:
        browser = None
        try:
            browser = await p.chromium.launch(headless=True)
            log_message("Browser launched.")

            # --- Run 1: Reference Screenshot (no blocking) ---
            await run_single_test(browser, None, "00", "_reference")

            urls_to_test = []
            list_for_all_block = []
            reason = "" # Suffix for filenames based on mode
            CONCURRENCY = 5 # Nombre de tests à exécuter en parallèle

            # --- Determine URLs to block ---
            if DISCOVER_MODE:
                log_message("\n--- Discovery Phase: Finding all resources ---")
                log_message("WARNING: Discovery mode can be very slow and generate many screenshots.")
                context_discover = None
                page_discover = None
                try:
                    # Create context/page specifically for discovery
                    context_discover = await browser.new_context(
                         user_agent="Mozilla/5.0 (Linux; Android 6.0.1; Nexus 5X Build/MMB29P) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/W.X.Y.Z Mobile Safari/537.36 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
                    )
                    page_discover = await context_discover.new_page()
                    # Attach the discovery listener
                    page_discover.on("response", handle_response_for_discovery)
                    log_message(f"  Navigating to {PAGE_URL} for discovery...")
                    # Load the page fully to capture all resources
                    await page_discover.goto(PAGE_URL, wait_until="networkidle", timeout=90000) # Longer timeout for discovery
                    log_message(f"  Page loaded ('networkidle'). Discovery finished.")
                    # Detach listener *before* closing
                    page_discover.remove_listener("response", handle_response_for_discovery)
                    log_message(f"--- Discovery complete: Found {len(discovered_resource_paths)} base resource URL(s) ---")
                    # Use discovered paths for testing
                    urls_to_test = sorted(list(discovered_resource_paths))
                    list_for_all_block = urls_to_test
                    reason = "_discovered" # Changed from _decouvert

                except Exception as e_discover:
                    log_message(f"  ERROR during discovery phase: {e_discover}")
                    # Optionally: Fallback to predefined list or stop? Currently continues without discovered URLs.
                finally:
                    # Ensure discovery page/context are closed
                    if page_discover and not page_discover.is_closed():
                        await page_discover.close()
                    if context_discover:
                        try:
                            await context_discover.close()
                        except Exception: pass
            else:
                # Use the hardcoded list
                log_message("\n--- Using the predefined PREDEFINED_BLOCK_LIST ---")
                urls_to_test = PREDEFINED_BLOCK_LIST
                list_for_all_block = PREDEFINED_BLOCK_LIST
                reason = "_predefined" # Changed from _predefini

            # --- Run 2: Individual Blocking Tests (in parallel) ---
            if not urls_to_test:
                log_message("\nWARNING: No URLs found or defined to test for blocking.")
            else:
                log_message(f"\n--- Starting {len(urls_to_test)} individual blocking tests (Concurrency: {CONCURRENCY}) ---")
                tasks = []
                for i, url_to_block in enumerate(urls_to_test):
                    # Crée une tâche pour chaque test individuel sans l'attendre immédiatement
                    task = asyncio.create_task(run_single_test(browser, url_to_block, f"{i+1:02d}", reason))
                    tasks.append(task)

                # Exécuter les tâches par lots
                for i in range(0, len(tasks), CONCURRENCY):
                    batch = tasks[i:i + CONCURRENCY]
                    log_message(f"  Running batch {i // CONCURRENCY + 1} ({len(batch)} tests)...")
                    await asyncio.gather(*batch) # Attend la fin du lot actuel avant de passer au suivant
                    log_message(f"  Batch {i // CONCURRENCY + 1} finished.")

                # --- Run 3: Block All Test ---
                await run_single_test(browser, "BLOCK_ALL", "99", "_all", is_combined_block=True, block_list_for_all=list_for_all_block) # Changed from TOUT_BLOQUER, _tout

            await browser.close()
            log_message("\n--- Playwright tests finished ---")
            log_message(f"Screenshots saved in directory: {OUTPUT_DIR}")
            # Sort results for consistent display
            test_results.sort(key=lambda x: (int(x['prefix']) if x['prefix'].isdigit() else 999, x['suffix']))
            test_status = "completed"
            return True

        except Exception as e_main:
             log_message(f"\n--- CRITICAL ERROR during Playwright execution: {e_main} ---")
             if browser and browser.is_connected():
                 await browser.close()
             test_status = "error"
             return False


# --- Flask Logic ---

# Updated HTML Template with English text and live log area
FLASK_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Playwright Test Results</title>
    <style>
        /* Basic Styling */
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; line-height: 1.6; margin: 0; padding: 0; background-color: #f8f9fa; color: #212529; }
        .container { max-width: 95%; margin: 20px auto; background: white; padding: 25px; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
        h1 { text-align: center; color: #0b0426; margin-bottom: 10px; }
        h2 { text-align: center; color: #495057; font-weight: 400; margin-top: 0; margin-bottom: 15px; font-size: 1.1em; word-wrap: break-word;}
        a { color: #007bff; text-decoration: none; }
        a:hover { text-decoration: underline; }

        /* Form Styling */
        .form-container { margin-bottom: 30px; padding: 20px; background-color: #e9ecef; border-radius: 5px; }
        .form-container label { font-weight: bold; margin-right: 10px; display: block; margin-bottom: 5px;}
        .form-container input[type="url"], .form-container input[type="submit"] {
            padding: 10px; margin-right: 10px; border: 1px solid #ced4da; border-radius: 4px; font-size: 1em;
        }
        .form-container input[type="url"] { width: calc(100% - 220px); min-width: 250px; }
        .form-container input[type="submit"] { background-color: #007bff; color: white; cursor: pointer; border-color: #007bff; }
        .form-container input[type="submit"]:hover { background-color: #0056b3; border-color: #0056b3; }
        .form-options { margin-top: 10px; }
        .form-options label { display: inline-block; margin-right: 15px; font-weight: normal;}

        /* Status and Log */
        .status-box { padding: 15px; margin-bottom: 20px; border-radius: 5px; text-align: center; font-weight: bold; }
        .status-idle { background-color: #e9ecef; color: #495057; }
        .status-running { background-color: #cfe2ff; color: #084298; border: 1px solid #b6d4fe;}
        .status-completed { background-color: #d1e7dd; color: #0f5132; border: 1px solid #badbcc;}
        .status-error { background-color: #f8d7da; color: #842029; border: 1px solid #f5c2c7;}
        .log-container {
            max-height: 300px; overflow-y: auto; background-color: #f8f9fa; border: 1px solid #dee2e6;
            padding: 15px; border-radius: 5px; margin-top: 20px; font-family: monospace; font-size: 0.9em;
            white-space: pre-wrap; /* Wrap long lines */
            word-wrap: break-word; /* Break words if necessary */
        }
        .log-container p { margin: 0 0 5px 0; line-height: 1.4; }

        /* Test Results Grid */
        .test-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(350px, 1fr)); gap: 20px; margin-top: 20px;}
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

        /* Fullscreen Image Overlay */
        .fullscreen-overlay { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background-color: rgba(0,0,0,0.9); justify-content: center; align-items: center; z-index: 1000; cursor: zoom-out; padding: 10px; box-sizing: border-box;}
        .fullscreen-image { max-width: 100%; max-height: 100%; object-fit: contain; box-shadow: 0 0 30px rgba(0,0,0,0.5); }
        .mode-info { text-align: center; margin-bottom: 20px; font-size: 0.95em; color: #6c757d; padding: 10px; background-color: #e9ecef; border-radius: 4px;}

        /* Googlebot Status Styling */
        .googlebot-status {
            margin: 8px 0;
            padding: 8px 12px;
            border-radius: 4px;
            display: flex;
            align-items: center;
            gap: 8px;
            font-size: 0.95em;
            font-weight: 500;
        }
        .googlebot-status::before {
            content: '';
            display: inline-block;
            width: 20px;
            height: 20px;
            background-size: contain;
            background-repeat: no-repeat;
            background-position: center;
        }
        .googlebot-status.allowed {
            background-color: #d4edda;
            color: #155724;
            border: 1px solid #c3e6cb;
        }
        .googlebot-status.allowed::before {
            background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='%23155724'%3E%3Cpath d='M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41L9 16.17z'/%3E%3C/svg%3E");
        }
        .googlebot-status.blocked {
            background-color: #f8d7da;
            color: #721c24;
            border: 1px solid #f5c6cb;
        }
        .googlebot-status.blocked::before {
            background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='%23721c24'%3E%3Cpath d='M19 6.41L17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12 19 6.41z'/%3E%3C/svg%3E");
        }
        .test-case {
            position: relative;
        }
        .googlebot-badge {
            position: absolute;
            top: 8px;
            right: 8px;
            padding: 4px 8px;
            border-radius: 12px;
            font-size: 0.8em;
            font-weight: bold;
        }
        .googlebot-badge.allowed {
            background-color: #d4edda;
            color: #155724;
        }
        .googlebot-badge.blocked {
            background-color: #f8d7da;
            color: #721c24;
        }

        .blocked-resources-summary {
            background-color: #fff3cd;
            color: #856404;
            padding: 15px;
            border-radius: 8px;
            margin: 20px 0;
            text-align: center;
            border: 1px solid #ffeeba;
        }
        
        .no-blocked-resources {
            background-color: #d4edda;
            color: #155724;
            padding: 20px;
            border-radius: 8px;
            margin: 20px 0;
            text-align: center;
            font-size: 1.2em;
            border: 1px solid #c3e6cb;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>Resource Blocking Test</h1>

        <div class="form-container">
            <form method="POST" action="/start">
                <label for="page_url">URL to Test:</label>
                <input type="url" id="page_url" name="page_url" placeholder="https://example.com" required value="{{ current_url if current_url else '' }}">
                <div class="form-options">
                    <label>
                        <input type="radio" name="mode" value="predefined" {{ 'checked' if not discover_mode else '' }} onclick="toggleUrlList(this)"> Use Predefined List
                    </label>
                    <label>
                        <input type="radio" name="mode" value="discover" {{ 'checked' if discover_mode else 'checked' }} onclick="toggleUrlList(this)"> Discover All Resources (Slow)
                    </label>
                    <div id="urlListContainer" style="margin-top: 15px; {{ 'display: none;' if discover_mode else 'display: none;' }}">
                        <label for="url_list">URLs à bloquer (une par ligne) :</label>
                        <textarea id="url_list" name="url_list" rows="5" style="width: 100%; margin-top: 8px; padding: 8px; border: 1px solid #ced4da; border-radius: 4px;" placeholder="https://example.com/script.js&#10;https://example.com/style.css">{{ '\n'.join(predefined_urls) if predefined_urls else '' }}</textarea>
                    </div>
                </div>
                <input type="submit" value="Start Tests" {{ 'disabled' if test_status == 'running' else '' }}>
            </form>
        </div>

        <div class="status-box status-{{ test_status }}">
            Status: {{ test_status.capitalize() }}
            {% if test_status == 'running' %}
                (Tests are running in the background...)
            {% elif test_status == 'completed' %}
                (Tests completed successfully)
            {% elif test_status == 'error' %}
                (An error occurred during testing)
            {% else %}
                 (Enter a URL and click 'Start Tests')
            {% endif %}
        </div>

        {% if current_url %}
        <h2>Tested URL: <a href="{{ current_url }}" target="_blank">{{ current_url }}</a></h2>
        <p class="mode-info">Mode Used: <strong>{{ 'Discover All Resources' if discover_mode else 'Predefined List' }}</strong></p>
        {% endif %}

        {% if test_status != 'idle' %}
        <h3>Live Log</h3>
        <div class="log-container" id="log-output">
            {% for line in log_lines %}
                <p>{{ line }}</p>
            {% endfor %}
        </div>
        {% endif %}

        {% if results %}
        <h2>Résultats des Tests</h2>
        
        {# Filtrer les résultats pour ne garder que les ressources bloquées #}
        {% set blocked_resources = [] %}
        {% for result in results %}
            {% if result.googlebot_allowed is defined and result.googlebot_allowed == false %}
                {% set _ = blocked_resources.append(result) %}
            {% endif %}
        {% endfor %}

        {% if blocked_resources|length > 0 %}
            <div class="blocked-resources-summary">
                <h3>⚠️ {{ blocked_resources|length }} ressource(s) bloquée(s) pour Googlebot</h3>
            </div>
            <div class="test-grid">
                {% for result in blocked_resources %}
                <div class="test-case {% if result.error %}error{% endif %}">
                    <div class="googlebot-badge blocked">
                        🚫 Bloqué
                    </div>
                    <h3>Test {{ result.prefix }}: {{ result.name | e }}{{ result.suffix | e }}</h3>
                    <p><strong>Ressource :</strong> <span class="blocked-item" title="{{ result.blocked_item | e }}">{{ result.blocked_item | e }}</span></p>
                    <p class="googlebot-status blocked">
                        <strong>Accès Googlebot :</strong> Cette ressource est BLOQUÉE pour Googlebot
                    </p>
                    {% if result.error %}
                        <div class="error-message"><strong>Error:</strong> {{ result.error_message | e }}</div>
                    {% endif %}
                    <div class="screenshot-container">
                        <img src="{{ url_for('serve_screenshot', filename=result.screenshot_file) }}"
                             alt="Screenshot for {{ result.name | e }}"
                             class="screenshot"
                             loading="lazy"
                             onclick="showFullscreen('{{ url_for('serve_screenshot', filename=result.screenshot_file) }}')"
                             onerror="this.alt='Screenshot not found'; this.style.display='none';">
                    </div>
                </div>
                {% endfor %}
            </div>
        {% else %}
            <div class="no-blocked-resources">
                ✅ Aucune ressource n'a été bloquée pour les robots de Google.
            </div>
        {% endif %}
        {% endif %}
    </div>

    <div id="fullscreen-overlay" class="fullscreen-overlay" onclick="hideFullscreen()">
        <img id="fullscreen-image" src="" alt="Fullscreen Screenshot" class="fullscreen-image">
    </div>

    <script>
        const overlay = document.getElementById('fullscreen-overlay');
        const fsImage = document.getElementById('fullscreen-image');
        const logOutput = document.getElementById('log-output');
        const statusBox = document.querySelector('.status-box'); // Assuming only one status box

        function showFullscreen(src) {
            fsImage.src = src;
            overlay.style.display = 'flex';
            document.body.style.overflow = 'hidden'; // Prevent background scroll
        }
        function hideFullscreen() {
            overlay.style.display = 'none';
            fsImage.src = '';
            document.body.style.overflow = ''; // Restore scroll
        }
        // Close fullscreen with Escape key
        document.addEventListener('keydown', function(event) {
            if (event.key === 'Escape' && overlay.style.display === 'flex') {
                hideFullscreen();
            }
        });

        // Function to fetch status and logs periodically
        async function updateStatus() {
            try {
                const response = await fetch('/status');
                if (!response.ok) {
                    console.error("Failed to fetch status:", response.statusText);
                    return; // Stop polling on error
                }
                const data = await response.json();

                // Update Status Box
                if (statusBox) {
                    statusBox.className = `status-box status-${data.status}`; // Update class for styling
                    let statusText = `Status: ${data.status.charAt(0).toUpperCase() + data.status.slice(1)}`; // Capitalize
                     if (data.status === 'running') statusText += ' (Tests are running...)';
                     else if (data.status === 'completed') statusText += ' (Tests completed)';
                     else if (data.status === 'error') statusText += ' (Error occurred)';
                     else if (data.status === 'idle') statusText += ' (Ready to start)';
                    statusBox.textContent = statusText;
                }


                // Update Log Output
                if (logOutput) {
                    // Clear existing logs and add new ones
                    logOutput.innerHTML = '';
                    data.log.forEach(line => {
                        const p = document.createElement('p');
                        p.textContent = line;
                        logOutput.appendChild(p);
                    });
                    // Scroll to the bottom of the log
                    logOutput.scrollTop = logOutput.scrollHeight;
                }


                // If tests are completed or errored, reload the page to show results grid
                if (data.status === 'completed' || data.status === 'error') {
                     // Optionally add a small delay before reloading
                     setTimeout(() => {
                         window.location.reload();
                     }, 1500); // Reload after 1.5 seconds to ensure results are processed
                } else if (data.status === 'running') {
                    // If still running, schedule the next update
                    setTimeout(updateStatus, 2000); // Poll every 2 seconds
                }

            } catch (error) {
                console.error("Error fetching status:", error);
                 // Optionally stop polling or retry after a delay
            }
        }

        // Start polling if the page indicates tests might be running or just finished
        // Check initial status passed from template or assume polling needed if status is 'running'
        const initialStatus = "{{ test_status }}";
        if (initialStatus === 'running') {
             // Disable form submit button while running
             const submitButton = document.querySelector('.form-container input[type="submit"]');
             if (submitButton) submitButton.disabled = true;
             updateStatus(); // Start polling immediately
        }

         // Add event listener to the form to disable button on submit and start polling
         const testForm = document.querySelector('.form-container form');
         if (testForm) {
             testForm.addEventListener('submit', () => {
                 const submitButton = testForm.querySelector('input[type="submit"]');
                 if (submitButton) {
                     submitButton.disabled = true;
                     submitButton.value = 'Starting...';
                 }
                 // Small delay to allow form submission before starting status checks
                 setTimeout(updateStatus, 500);
             });
         }

        function toggleUrlList(radio) {
            const container = document.getElementById('urlListContainer');
            container.style.display = radio.value === 'predefined' ? 'block' : 'none';
        }

    </script>
</body>
</html>
"""

@flask_app.route('/', methods=['GET'])
def index():
    """Renders the main page with current results and status."""
    # Results are sorted at the end of run_playwright_test_suite
    return render_template_string(FLASK_TEMPLATE,
                                  results=test_results,
                                  current_url=PAGE_URL, # Pass the currently tested URL
                                  discover_mode=DISCOVER_MODE,
                                  test_status=test_status,
                                  log_lines=test_log,
                                  predefined_urls=predefined_urls) # Pass the predefined URLs

@flask_app.route('/start', methods=['POST'])
def start_tests():
    """Handles the form submission to start a new test run."""
    global PAGE_URL, DISCOVER_MODE, test_status, test_log, test_results, PREDEFINED_BLOCK_LIST, predefined_urls
    if test_status == 'running':
        # Prevent starting new tests if already running
        return "Tests are already in progress.", 429 # Too Many Requests

    url = request.form.get('page_url')
    mode = request.form.get('mode') # 'discover' or 'predefined'
    url_list = request.form.get('url_list', '').strip()

    if not url:
        return "URL is required.", 400

    # Vérification si mode prédéfini et liste vide
    if mode == 'predefined' and not url_list:
        return "En mode 'Predefined List', vous devez fournir au moins une URL à bloquer. Veuillez remplir la liste des URLs avant de lancer les tests.", 400

    # Update global config
    PAGE_URL = url
    DISCOVER_MODE = (mode == 'discover')
    
    # Update predefined URLs if in predefined mode
    if not DISCOVER_MODE and url_list:
        # Split the textarea content into lines and filter out empty lines
        predefined_urls = [line.strip() for line in url_list.split('\n') if line.strip()]
        PREDEFINED_BLOCK_LIST = predefined_urls
    else:
        predefined_urls = []
        PREDEFINED_BLOCK_LIST = []

    test_status = "starting" # Indicate that tests are about to run
    test_log = ["Test run requested..."]
    test_results = [] # Clear previous results

    # Run Playwright tests in a separate thread to avoid blocking Flask
    def run_async_tests():
        global test_status
        try:
            asyncio.run(run_playwright_test_suite())
            # Status (completed/error) is set within run_playwright_test_suite
        except Exception as e:
            log_message(f"FATAL ERROR running test thread: {e}")
            test_status = "error"

    thread = threading.Thread(target=run_async_tests)
    thread.start()

    # Redirect back to the main page, which will show the 'running' status and start polling
    return redirect(url_for('index'))


@flask_app.route('/status')
def get_status():
    """API endpoint for the frontend to poll test status and logs."""
    return {"status": test_status, "log": test_log}


@flask_app.route('/screenshots/<path:filename>')
def serve_screenshot(filename):
    """Serves the screenshot files from the output directory."""
    safe_dir = os.path.abspath(flask_app.config['OUTPUT_DIR'])
    file_path = os.path.abspath(os.path.join(safe_dir, filename))

    # Security check: ensure the requested path is within the safe directory
    if not file_path.startswith(safe_dir):
        print(f"Attempted unauthorized access blocked: {filename}")
        abort(404) # Not Found

    try:
        # Use max_age=0 to prevent aggressive browser caching during tests
        return send_from_directory(safe_dir, filename, max_age=0)
    except FileNotFoundError:
        print(f"File not found: {filename}")
        abort(404)

@flask_app.route('/check_impact', methods=['GET'])
def check_impact():
    url = request.args.get('url')
    if not url:
        return jsonify({"error": "URL parameter is required"}), 400

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        
        # Mesurer le temps de chargement sans bloquer les ressources
        page.goto(url)
        normal_timing = page.evaluate('() => ({loadTime: window.performance.timing.loadEventEnd - window.performance.timing.navigationStart})')
        
        # Mesurer le temps de chargement en bloquant les ressources
        context = browser.new_context()
        page = context.new_page()
        page.route("**/*.{png,jpg,jpeg,gif,webp,css,js}", lambda route: route.abort())
        page.goto(url)
        blocked_timing = page.evaluate('() => ({loadTime: window.performance.timing.loadEventEnd - window.performance.timing.navigationStart})')
        
        browser.close()
        
        return jsonify({
            "normal_load_time": normal_timing["loadTime"],
            "blocked_load_time": blocked_timing["loadTime"],
            "difference": normal_timing["loadTime"] - blocked_timing["loadTime"]
        })

# --- Execution ---
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Check Impact of Blocking Resources')
    parser.add_argument('--port', type=int, default=5001, help='Port to run the server on')
    parser.add_argument('--discover', action='store_true', help='Enable discovery mode')
    parser.add_argument('--url', type=str, help='URL to test in discovery mode')
    args = parser.parse_args()

    DISCOVER_MODE = args.discover
    PAGE_URL = args.url

    if DISCOVER_MODE and not PAGE_URL:
        print("Error: --url is required when --discover is enabled")
        sys.exit(1)

    # Activation du mode debug pour le rechargement automatique
    flask_app.debug = True
    flask_app.run(host='0.0.0.0', port=args.port, debug=True)

