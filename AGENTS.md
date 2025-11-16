# Repository Guidelines
总是使用中文回答
## Project Structure & Module Organization
The repo stays intentionally lean: `main.py` in the root hosts browser setup, trend ingestion, and rewards automation. Keep the Edge driver binary (e.g., `msedgedriver.exe`) next to `main.py` so `EdgeService` can resolve it without extra config. If you add reusable helpers, group them under `utils/`, and mirror new modules with tests in `tests/` (example: `main.py` → `tests/test_main.py`).

## Build, Test, and Development Commands
Use Python 3.10+: `python -m venv .venv && source .venv/bin/activate` (PowerShell: `.venv\Scripts\Activate`). Install dependencies explicitly because no requirements file is tracked: `pip install selenium requests selenium-stealth tenacity tqdm`. Run the workflow with `python main.py`; pass `headless` to reuse the silent flag already supported by `init_browser` and `init_mobile_edge_appium`. When you need a custom driver path, export `EDGEWEBDRIVER=/path/to/msedgedriver.exe` and read it before constructing `EdgeService`.

## Coding Style & Naming Conventions
Stick to PEP 8: 4-space indents, snake_case functions, short verb-led helper names, and uppercase constants. Selenium actions should live in clearly named wrappers (`open_daily_set`, `switch_to_new_tab`) so retries and waits stay centralized. Keep stdout text in Simplified Chinese to match the current CLI behavior, and only add comments when timing or anti-detection tweaks need context.

## Testing Guidelines
Add pytest coverage for every new helper or data-transform function (run with `pytest -q`). Mock Selenium drivers and HTTP calls via `unittest.mock` or `responses` to avoid live traffic and keep runs deterministic. Name tests `test_<function>_<case>` and attach screenshots/logs when validating full browser flows manually.

## Commit & Pull Request Guidelines
Recent commits stick to brief Mandarin summaries (`修修bug`, `优化`), so keep that pattern: one short verb phrase describing the change in the imperative. Commit only one logical change at a time and reference related issues in the body if needed. Pull requests need a problem/solution paragraph, test or manual-run evidence, and screenshots/log excerpts whenever UI timing or automation reliability is touched.

## Security & Configuration Tips
Load Microsoft credentials and API tokens from environment variables or a `.env` ignored by Git; never store them inline. Keep `msedgedriver.exe` aligned with the installed Edge version and favor headless runs in CI to reduce detection risk.
