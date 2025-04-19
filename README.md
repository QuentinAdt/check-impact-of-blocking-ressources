# Resource Blocking Impact Analysis Tool for Technical SEO

## Overview

This tool is designed to help technical SEO and QA teams assess the impact of blocking certain web resources on the rendering of a web page, as perceived by a simulated Googlebot Mobile user agent. It captures screenshots of the page under different blocking scenarios. It is particularly useful in pre-production environments before site changes or migrations to identify potential SEO regressions caused by blocked resources (e.g., via `robots.txt`).

By simulating how Googlebot might render a page with certain resources blocked, this tool allows you to:

* **Identify rendering dependencies:** Visually determine if blocking specific resources significantly alters the visible content or page structure.
* **Validate `robots.txt` impact:** See the rendering effect of blocking resources disallowed by `robots.txt` (the tool checks `robots.txt` rules for Googlebot against blocked resource paths).
* **Detect rendering regressions:** Compare page rendering with and without blocking specific resources to spot unexpected visual changes.
* **Facilitate SEO QA:** Provide visual evidence (screenshots) of the page's state under different blocking scenarios.

## Key Features

* **Benchmark testing:** Takes a reference screenshot of the page with no resources blocked.
* **Individual resource blocking:** Ability to block specific resource URLs based on a predefined list (substring match) or discovered resources (exact full URL match).
* **Group resource blocking:** Simultaneously block all resources identified in "Discovery Mode" or all URLs matching the patterns in the "Predefined List".
* **Auto-discovery mode:** Loads the page initially to identify resources based on specific URL patterns (targeting video/API/media-like URLs, specific paths like `/v1/`, etc.). It captures the *full URL* (including query parameters) of these discovered resources for subsequent individual blocking tests. *Note: This mode can be slower and generate many tests.*
* **Predefined list mode:** Use a custom list of URL strings. The tool will block any resource whose URL *contains* any of the strings provided in the list.
* **Robots.txt check:** For each individually blocked resource, the tool checks if its *path* is allowed or disallowed for the "Googlebot" user agent according to the site's `robots.txt` file.
* **Simple web interface:** Run tests and view results (screenshots, logs, status) via a Flask web application.
* **Comparative screenshots:** Provides side-by-side visual comparison of rendering with and without blocked resources.
* **Live logs:** Track test progress and potential errors in real-time within the web interface.
* **Concurrency:** Runs individual blocking tests in parallel batches to speed up execution.

## Usage

1.  **Installation:** Ensure Python 3 is installed. Clone the repository or save the script code (e.g., as `resource_blocker.py`).
2.  **Dependencies:** Install the necessary Python libraries:
    ```bash
    pip install playwright flask gpyrobotstxt
    ```
    Install the necessary Playwright browser binaries (only Chromium is used by the script):
    ```bash
    playwright install chromium
    ```
3.  **Launch:** Run the Python script, specifying a port:
    ```bash
    python resource_blocker.py --port 5001
    ```
    To launch directly in discovery mode for a specific URL:
    ```bash
    python resource_blocker.py --port 5001 --discover --url [https://your-target-site.com](https://your-target-site.com)
    ```
    *(Replace `resource_blocker.py` with your actual script filename)*
4.  **Access the web interface:** Open your browser and navigate to `http://localhost:<port>` or `http://<your-ip>:<port>` (e.g., `http://localhost:5001`).
5.  **Test Configuration:**
    * Enter the full URL of the page to test (including `http://` or `https://`).
    * Choose the blocking mode:
        * **Use Predefined List:** Enter URL strings (one per line) in the text area. Any resource URL *containing* one of these strings will be blocked in the corresponding tests.
        * **Discover All Resources (Slow):** The tool will first load the page to find resources matching specific patterns (video/API-like). It will then run tests blocking each discovered *full URL* individually, plus a test blocking all discovered resources.
    * Click "Start Tests".
6.  **Viewing Results:** The web interface will update with live logs. Once completed, it displays the reference screenshot and screenshots for each blocking scenario. It indicates the blocked resource (or "all"), the `robots.txt` status for Googlebot (for individual blocks), and any errors encountered.

## Technical SEO Use Case

* **Pre-migration/Pre-launch QA:** Test pre-production URLs to ensure critical rendering resources won't be blocked inadvertently post-launch.
* **`robots.txt` Rule Validation:** Visually confirm the rendering impact of resources disallowed by `robots.txt` rules.
* **Debugging Rendering Issues:** Help identify if resource blocking (intended or accidental) is causing rendering problems observed by search engines.
* **Impact Analysis:** Understand how blocking non-essential third-party scripts or assets might affect the core rendering.
* **Regression Testing:** Periodically run tests after site updates to ensure changes haven't negatively impacted resource availability for rendering.

## Test Output

Results are displayed in the web interface:

* A reference screenshot (no blocking).
* Screenshots for each individual resource blocked (if applicable).
* A screenshot for the "block all" scenario (blocking all predefined or discovered resources).
* For each test:
    * The resource URL or identifier blocked (e.g., "reference", "all", specific URL).
    * `robots.txt` status for Googlebot (Allowed/Blocked) for individually blocked resources.
    * Error messages if a test failed.

Screenshots are saved locally in the `` `screenshots_playwright` `` directory, named according to the test number and blocked resource.

## Contribution

Contributions are welcome! Feel free to submit pull requests to improve the tool.
