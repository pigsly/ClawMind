# Quick User Manual

## Codex CLI Quick Setup

Use this path when you want ClawMind to call the local Codex CLI.

### 1. Prepare a working folder

```powershell
mkdir D:\clawmind_demo
cd D:\clawmind_demo
```

ClawMind uses this folder as the execution directory. By default it creates:

* `run_logs\`
* `runtime_artifacts\`

### 2. Prepare Logseq and Codex CLI

Prepare your Logseq graph path, for example:

```text
D:\logseq
```

Make sure Codex CLI already works:

```powershell
codex --version
```

If `codex` is not in `PATH`, find the real executable path, for example:

```text
C:\Users\<YourName>\AppData\Local\nvm\v24.11.0\codex.cmd
```

### 3. Create `.env`

Create `D:\clawmind_demo\.env`:

```env
LLM_BRAND=codex_cli
CODEX_CLI_PATH=C:\Users\<YourName>\AppData\Local\nvm\v24.11.0\codex.cmd
LOGSEQ_PATH=D:\logseq
MAX_RETRIES=2
CODEX_TIMEOUT_SECONDS=300
JOURNAL_SCAN_DAYS=7
```

If `codex` is already in `PATH`, `CODEX_CLI_PATH` can be omitted:

```env
LLM_BRAND=codex_cli
LOGSEQ_PATH=D:\logseq
MAX_RETRIES=2
CODEX_TIMEOUT_SECONDS=300
JOURNAL_SCAN_DAYS=7
```

### 4. Verify and run

```powershell
clawmind version
clawmind install-info
clawmind run-worker
```

Expected startup signal:

```text
config_source=cwd:.env env_path=D:\clawmind_demo\.env
```

### 5. Quick checks

* If Codex CLI is not found, verify `CODEX_CLI_PATH` or confirm `codex` is in `PATH`
* If `.env` is not picked up, check whether `CLAWMIND_ENV_PATH` is set at OS level
* If runtime outputs are not where you expect, confirm you started from the intended working folder

## Gemini API Quick Setup

Use this path when you want ClawMind to call Gemini directly.

### 1. Prepare a working folder

```powershell
mkdir D:\clawmind_gemini
cd D:\clawmind_gemini
```

ClawMind uses this folder as the execution directory. By default it creates:

* `run_logs\`
* `runtime_artifacts\`

### 2. Prepare Logseq and API key

Prepare your Logseq graph path, for example:

```text
D:\logseq
```

Prepare a valid Gemini API key before continuing.

### 3. Create `.env`

Create `D:\clawmind_gemini\.env`:

```env
LLM_BRAND=gemini_api
GEMINI_API_KEY=your_api_key_here
GEMINI_FLASH_MODEL=gemini-2.5-flash
GEMINI_PRO_MODEL=gemini-2.5-pro
LOGSEQ_PATH=D:\logseq
MAX_RETRIES=2
CODEX_TIMEOUT_SECONDS=300
JOURNAL_SCAN_DAYS=7
```

Meaning of the key model settings:

* `GEMINI_FLASH_MODEL` for simpler tasks
* `GEMINI_PRO_MODEL` for heavier reasoning tasks

### 4. Verify and run

```powershell
clawmind version
clawmind install-info
clawmind run-worker
```

Expected startup signal:

```text
config_source=cwd:.env env_path=D:\clawmind_gemini\.env
```

### 5. Quick checks

* If Gemini authentication fails, re-check `GEMINI_API_KEY`
* If tasks do not use Gemini, confirm `LLM_BRAND=gemini_api`
* If runtime outputs are not where you expect, confirm you started from the intended working folder

## Upgrade

Upgrade command:

```powershell
clawmind upgrade --method auto
```

Behavior:

* ClawMind detects the install method automatically and uses the matching upgrade path
* For `uv` installs, ClawMind internally refreshes the tool install so the latest published version is picked up reliably
* ClawMind stops other running `clawmind` processes before upgrading
* On Windows installed-CLI setups, self-upgrade may run in deferred mode so a helper can replace `clawmind.exe` after the current process exits

After upgrade, verify:

```powershell
clawmind version
clawmind install-info
```

## Supplement

### Working Directory Behavior

* `.env` is resolved in this order:
  * `CLAWMIND_ENV_PATH`
  * current working directory `.env`
  * package project root fallback `.env`
* `run_logs/` and `runtime_artifacts/` are created under the current working directory

If you install ClawMind from a wheel and run it from `D:\work\demo`, the default runtime outputs will be written to `D:\work\demo\run_logs\` and `D:\work\demo\runtime_artifacts\`.

### `.env` Reference

Example:

```env
LLM_BRAND=codex_cli
CODEX_CLI_PATH=C:\Users\<YourName>\AppData\Local\nvm\v24.11.0\codex.cmd
GEMINI_API_KEY=your_api_key_here
GEMINI_FLASH_MODEL=gemini-2.5-flash
GEMINI_PRO_MODEL=gemini-2.5-pro
LOGSEQ_PATH=D:\logseq
MAX_RETRIES=2
CODEX_TIMEOUT_SECONDS=300
JOURNAL_SCAN_DAYS=7
```

Main fields:

* `LLM_BRAND` selects `codex_cli` or `gemini_api`
* `CODEX_CLI_PATH` is optional if `codex` is already in `PATH`
* `GEMINI_API_KEY` is required when `LLM_BRAND=gemini_api`
* `LOGSEQ_PATH` points to your Logseq graph root
* `MAX_RETRIES` defaults to `2`
* `CODEX_TIMEOUT_SECONDS` is the execution timeout for both Codex and Gemini paths
* `JOURNAL_SCAN_DAYS` limits how many recent journal days are scanned
* `CLAWMIND_ENV_PATH` can override the default `.env` lookup path
