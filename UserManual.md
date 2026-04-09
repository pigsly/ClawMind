
# Quick User manual

## Environment Requirements

- WINDOWS OS 10 or 11
- Install Codex CLI and connect your ChatGPT account (Plus plan or higher)
- Prepare a Gemini API key if you want to use the Gemini API path
- Install Logseq 0.10.15
- Python 3.13+

## Let's start...

 - `python -m pip install --user pipx`
 - `python -m pipx ensurepath`
 - close terminal，reopen PowerShell / cmd
 - `pipx --version`
 - `pipx install clawmind`
 - `uv tool install clawmind`
 - `clawmind upgrade --method auto`
 - `clawmind version`
 - put `.env` into your project
 - `clawmind run-worker`

## Gemini API Quick Setup

If you want to use the Gemini API path, the minimum setup is:

```env
# codex_cli, gemini_api
LLM_BRAND=gemini_api
GEMINI_API_KEY=your_api_key_here
LOGSEQ_PATH=C:\logseq\
```

Optional model overrides:

```env
GEMINI_FLASH_MODEL=gemini-2.5-flash
GEMINI_PRO_MODEL=gemini-2.5-pro
```

With this setup, simpler tasks can use `gemini-2.5-flash`, while heavier reasoning tasks can use `gemini-2.5-pro`.

Note: users with Gemini API paid tier can use `gemini-2.5-pro` directly, while free-tier users may automatically fall back to `gemini-2.5-flash` when pro quota is unavailable.

## .env Parameters

Create a `.env` file in your project root.

Example:

```env
# codex_cli, gemini_api
LLM_BRAND=codex_cli
CODEX_CLI_PATH=C:\Users\<<YourName>>\AppData\Local\nvm\v24.11.0\codex.cmd
GEMINI_API_KEY=your_api_key_here
GEMINI_FLASH_MODEL=gemini-2.5-flash
GEMINI_PRO_MODEL=gemini-2.5-pro
LOGSEQ_PATH=C:\logseq\
MAX_RETRIES=2
CODEX_TIMEOUT_SECONDS=300
JOURNAL_SCAN_DAYS=7
```

- `LLM_BRAND`
  Selects the active LLM path. Use `codex_cli` for Codex CLI or `gemini_api` for Gemini API.
- `CODEX_CLI_PATH`
  Path to the Codex CLI executable. If not set, ClawMind falls back to `codex` from `PATH`.
- `GEMINI_API_KEY`
  Gemini API key used by `GeminiApiAdapter`. Required when `LLM_BRAND=gemini_api`.
- `GEMINI_FLASH_MODEL`
  Model used for simpler Gemini API tasks. Default is `gemini-2.5-flash`.
- `GEMINI_PRO_MODEL`
  Model used for harder Gemini API tasks. Default is `gemini-2.5-pro`.
- `LOGSEQ_PATH`
  Path to your Logseq graph root directory. If not set, ClawMind falls back to `./logseq` under the project root.
- `MAX_RETRIES`
  Maximum retry count for task execution. Default is `2`.
- `CODEX_TIMEOUT_SECONDS`
  Timeout in seconds for each Codex or Gemini API execution call. Leave empty to use the internal default behavior.
- `JOURNAL_SCAN_DAYS`
  Number of recent journal days to scan when looking for tasks. Leave empty to disable this limit.

Optional:

- `CLAWMIND_ENV_PATH`
  Use this OS environment variable to point ClawMind to a specific `.env` file path instead of the default project `.env`.
