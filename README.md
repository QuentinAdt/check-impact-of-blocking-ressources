# 🔍 Visual Resource Blocker Tester with Playwright & Flask

This Python tool allows **technical SEOs** to visually test how third-party resources affect the rendering of a web page. It uses **Playwright** to simulate Googlebot User-Agent and takes screenshots under different blocking scenarios (script, CDN, ad tags, etc.), allowing you to **identify rendering dependencies and third-party performance impacts**.

/!\ It currently doesn't respect the robots.txt /!\
---

## 🚀 What Does It Do?

1. **Loads a page as Googlebot**.
2. **Blocks predefined or discovered resources** (scripts, CDNs, analytics, ads...).
3. **Takes full-page screenshots** of:
   - The reference version (no block).
   - Each version with one blocked resource.
   - A version with **all** listed resources blocked.
4. **(Optional)** Discovers all loaded resource URLs dynamically.
5. **Serves results in a local Flask dashboard** for easy comparison.

---

## 🛠 Technologies Used

- `Playwright` (async, headless Chromium)
- `Flask` (for result visualization)
- `asyncio` + `argparse`
- HTML snapshot visualization
- Blocking logic inspired by user-agent `"Googlebot"`

---

## 📸 Use Cases for SEOs

- Measure **visual impact** of third-party JS and CDNs.
- Understand what breaks when a **resource is blocked**.
- Emulate **search engine rendering** under failure conditions.
- Evaluate **critical rendering path** and dependency chains.
- Audit performance bottlenecks (e.g. if Googlebot renders a broken page due to external services).

---

## ⚙️ Configuration

Edit directly in the script or via `argparse`:

```python
PAGE_URL = "https://www.m6.fr/..."
PREDEFINED_BLOCK_LIST = [ ... ]  # List of resources to block
WAIT_AFTER_LOAD = 3500  # Optional wait time after page load (ms)
