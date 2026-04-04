
# Quick User manual

## Environment Requirements

- WINDOWS OS 10 or 11
- Install Codex CLI and connect your ChatGPT account (Plus plan or higher)
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
 - put .env into your project
 - `clawmind run-worker`
 
## .env Parameters

Create a `.env` file in your project root.

Example:

```env
CODEX_CLI_PATH=C:\Users\<<YourName>>\AppData\Local\nvm\v24.11.0\codex.cmd
LOGSEQ_PATH=C:\logseq\
MAX_RETRIES=2
CODEX_TIMEOUT_SECONDS=300
JOURNAL_SCAN_DAYS=7
```

- `CODEX_CLI_PATH`
  Path to the Codex CLI executable. If not set, ClawMind falls back to `codex` from `PATH`.
- `LOGSEQ_PATH`
  Path to your Logseq graph root directory. If not set, ClawMind falls back to `./logseq` under the project root.
- `MAX_RETRIES`
  Maximum retry count for task execution. Default is `2`.
- `CODEX_TIMEOUT_SECONDS`
  Timeout in seconds for each Codex execution call. Leave empty to use the internal default behavior.
- `JOURNAL_SCAN_DAYS`
  Number of recent journal days to scan when looking for tasks. Leave empty to disable this limit.

Optional:

- `CLAWMIND_ENV_PATH`
  Use this OS environment variable to point ClawMind to a specific `.env` file path instead of the default project `.env`.
