# -*- coding: utf-8 -*-
import asyncio
import os
import re
import sys
import argparse # Added import for arguments
from playwright.async_api import async_playwright, Error as PlaywrightError
from urllib.parse import urlparse
from flask import Flask, render_template_string, url_for, send_from_directory, abort, request, redirect
import logging
import threading
import time

# --- Configuration ---
# DISCOVER_MODE will be set by argparse
# PAGE_URL will be set by argparse

PREDEFINED_BLOCK_LIST = [
    "cdns.eu1.gigya.com/js/gigya.js",
    "js-agent.newrelic.com/nr-spa",
    "sdk.privacy-center.org/",
    "securepubads.g.doubleclick.net/pagead/managed/js/gpt/",
    "googletagservices.com/tag/js/gpt.js",
]

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
    """Callback to discover resource URLs during the initial page load."""
    global discovered_resource_paths, page_url_base_path
    url = response.url
    try:
        parsed_url = urlparse(url)

        # Ignore non-http/https schemes
        if parsed_url.scheme not in ['http', 'https']:
            return

        # Get the base URL (scheme://netloc/path)
        url_base = f"{parsed_url.scheme}://{parsed_url.netloc}{parsed_url.path}"

        # Ignore the main page URL itself
        if page_url_base_path and url_base.rstrip('/') == page_url_base_path.rstrip('/'):
            return

        # If this base URL hasn't been seen, add it
        if url_base not in discovered_resource_paths:
            content_type = response.header_value("content-type") or "N/A"
            # log_message(f"  [Discovery] Resource: {url_base} (Type: {content_type.split(';')[0]})") # Optional: uncomment to see all discoveries
            discovered_resource_paths.add(url_base)

    except Exception as e:
        log_message(f"  Warning: Could not parse response for {url}: {e}")


async def block_request_handler(route, request, blocked_reason="resource"):
    """Callback to block a request."""
    # log_message(f"  >> Blocking ({blocked_reason[:20]}...): {request.url[:80]}...") # Optional: uncomment to see every block
    try:
        await route.abort()
    except PlaywrightError as e:
        # Ignore errors caused by the page/context closing during abort
        if "Target page, context or browser has been closed" not in str(e) and "Request context is destroyed" not in str(e):
            log_message(f"  Warning: Error during abort (might be normal): {e}")

async def run_single_test(browser, url_to_block, file_prefix, reason_suffix, is_combined_block=False, block_list_for_all=None):
    """Runs a single Playwright test case (reference or blocking one/all resources)."""
    global test_results

    is_reference = url_to_block is None
    name_for_file = url_to_block or "reference"
    current_blocked_item = "None (Reference)"
    if is_combined_block:
        name_for_file = "all"
        current_blocked_item = "BLOCK_ALL" # Changed from TOUT
    elif url_to_block:
        current_blocked_item = url_to_block

    # Generate filenames
    filename_base = sanitize_filename(name_for_file)
    screenshot_filename = f"{file_prefix}_{filename_base}{reason_suffix}.png"
    screenshot_path = os.path.join(OUTPUT_DIR, screenshot_filename)
    error_screenshot_filename = f"{file_prefix}_{filename_base}{reason_suffix}_ERROR.png" # Changed from _ERREUR
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
        'blocked_item': current_blocked_item
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
                        # Exact path match (startswith)
                        def should_block_all(url_str):
                            return any(url_str.startswith(path) for path in list_to_use)
                    else:
                        # Substring match (contains)
                        def should_block_all(url_str):
                             return any(part in url_str for part in list_to_use)

                    # Route matching requests to the block handler
                    await context.route(
                        should_block_all,
                        lambda route, request: asyncio.ensure_future(block_request_handler(route, request, "BLOCK_ALL")) # Changed from TOUT
                    )
                    log_message(f"  Blocking rule enabled for ALL {len(list_to_use)} resources.")
            else:
                # Block a single URL pattern
                if DISCOVER_MODE:
                    # Exact path match (startswith)
                    block_condition = lambda url_str: url_str.startswith(url_to_block)
                else:
                    # Substring match (contains)
                    block_condition = lambda url_str: url_to_block in url_str

                # Route matching requests to the block handler
                await context.route(
                    block_condition,
                    lambda route, request, ub=url_to_block: asyncio.ensure_future(block_request_handler(route, request, ub[:30])) # Pass blocked url for logging context
                )
                log_message(f"  Blocking rule enabled for: {url_to_block}")

        # Create a new page in the context
        page = await context.new_page()

        try:
            # Navigate to the target page
            log_message(f"  Navigating to {PAGE_URL}...")
            # Wait until the network is idle (or timeout)
            await page.goto(PAGE_URL, wait_until="networkidle", timeout=60000)
            log_message(f"  Page loaded ('networkidle'). Taking screenshot...")
            # Take a full-page screenshot
            await page.screenshot(path=screenshot_path, full_page=True)
            log_message(f"  Screenshot saved: {screenshot_path}")

        except Exception as e_nav:
            # Handle navigation/screenshot errors
            log_message(f"  ERROR during navigation/screenshot for {name_for_file}: {e_nav}")
            result_data['error'] = True
            result_data['error_message'] = str(e_nav)
            result_data['screenshot_file'] = error_screenshot_filename # Use error filename
            try:
                # Try taking an error screenshot anyway
                if page and not page.is_closed():
                    await page.screenshot(path=error_screenshot_path, full_page=True)
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

            # --- Run 2: Individual Blocking Tests ---
            if not urls_to_test:
                log_message("\nWARNING: No URLs found or defined to test for blocking.")
            else:
                log_message(f"\n--- Starting {len(urls_to_test)} individual blocking tests ---")
                for i, url_to_block in enumerate(urls_to_test):
                    await run_single_test(browser, url_to_block, f"{i+1:02d}", reason)

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
                        <input type="radio" name="mode" value="predefined" {{ 'checked' if not discover_mode else '' }}> Use Predefined List
                    </label>
                    <label>
                        <input type="radio" name="mode" value="discover" {{ 'checked' if discover_mode else '' }}> Discover All Resources (Slow)
                    </label>
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
        <h2>Test Results</h2>
        <div class="test-grid">
            {% for result in results %}
            <div class="test-case {% if result.error %}error{% endif %}">
                <h3>Test {{ result.prefix }}: {{ result.name | e }}{{ result.suffix | e }}</h3>
                <p><strong>Blocked Item:</strong> <span class="blocked-item" title="{{ result.blocked_item | e if result.blocked_item else 'None (Reference)' }}">{{ result.blocked_item | e if result.blocked_item else 'None (Reference)' }}</span></p>
                {% if result.error %}
                    <div class="error-message"><strong>Error:</strong> {{ result.error_message | e }}</div>
                {% endif %}
                <div class="screenshot-container">
                    <img src="{{ url_for('serve_screenshot', filename=result.screenshot_file) }}"
                         alt="Screenshot for {{ result.name | e }}"
                         class="screenshot"
                         loading="lazy"
                         onclick="showFullscreen('{{ url_for('serve_screenshot', filename=result.screenshot_file) }}')"
                         onerror="this.alt='Screenshot not found'; this.style.display='none';"> </div>
            </div>
            {% else %}
            <p>No test results found yet.</p>
            {% endfor %}
        </div>
        {% elif test_status == 'completed' %}
         <p>Tests completed, but no results were generated (check logs).</p>
        {% elif test_status == 'idle' and not current_url %}
         <p>Enter a URL above and start the tests to see results.</p>
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
                                  log_lines=test_log)

@flask_app.route('/start', methods=['POST'])
def start_tests():
    """Handles the form submission to start a new test run."""
    global PAGE_URL, DISCOVER_MODE, test_status, test_log, test_results
    if test_status == 'running':
        # Prevent starting new tests if already running
        return "Tests are already in progress.", 429 # Too Many Requests

    url = request.form.get('page_url')
    mode = request.form.get('mode') # 'discover' or 'predefined'

    if not url:
        return "URL is required.", 400

    # Update global config
    PAGE_URL = url
    DISCOVER_MODE = (mode == 'discover')
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

# --- Execution ---
if __name__ == "__main__":
    # --- Argument Handling ---
    parser = argparse.ArgumentParser(description="Test page rendering by blocking resources and display results via Flask.")
    # Remove the URL argument from here as it's handled by the Flask form
    # parser.add_argument(
    #     '--url',
    #     type=str,
    #     required=False, # Make it optional if we want a default or rely solely on Flask UI
    #     help="The URL of the page to test (e.g., https://example.com)."
    # )
    parser.add_argument(
        '--discover-all',
        action='store_true',
        default=False, # Default is predefined list
        help="Enable discovery mode to block all loaded resources one by one (can be very slow). Default uses the predefined list."
    )
    parser.add_argument(
        '--port',
        type=int,
        default=5001,
        help="Port for the Flask server (default: 5001)."
    )
    args = parser.parse_args()

    # Set initial mode from args (can be overridden by Flask UI later)
    # We don't set PAGE_URL here anymore
    DISCOVER_MODE = args.discover_all
    FLASK_PORT = args.port

    # --- Launch Flask ---
    # Playwright tests are now triggered via the Flask UI '/start' endpoint
    print("\n--- Starting the Flask web interface ---")
    print(f"Open your browser and go to: http://127.0.0.1:{FLASK_PORT}")
    print("Use the web form to enter the URL and start the tests.")
    print("Press CTRL+C in this terminal to stop the server.")

    try:
        # Check if waitress is available for a more production-ready server
        try:
            from waitress import serve
            print(f"Using Waitress server on port {FLASK_PORT}.")
            # Use more threads for potentially better handling of concurrent requests (polling + screenshot serving)
            serve(flask_app, host='127.0.0.1', port=FLASK_PORT, threads=8)
        except ImportError:
            print(f"Waitress not found, using Flask's development server on port {FLASK_PORT} (less stable).")
            # debug=False and use_reloader=False are important for running async tasks in threads correctly
            flask_app.run(host='127.0.0.1', port=FLASK_PORT, debug=False, use_reloader=False)

    except OSError as e:
        if "address already in use" in str(e).lower():
            print(f"\nERROR: Port {FLASK_PORT} is already in use.")
            print("Try stopping the application using this port or use a different port via the --port option.")
            print(f"Example: python {sys.argv[0]} --port 5002")
        else:
            print(f"\nError starting Flask: {e}")
    except Exception as e:
        print(f"\nError starting Flask: {e}")

    print("\nScript finished.")

