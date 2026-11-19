# GameGen

GameGen turns a written game brief into a tested, playable 2D browser game. It combines a language model with a composable Phaser runtime, isolated E2B build environments, automated browser verification, and durable game storage.

The active generation path targets small, single-player arcade games. The system is designed to reuse reliable gameplay systems while leaving the model responsible for composition, rules, presentation, and bounded custom behavior.

## Current Scope

GameGen can compose games such as:

- endless runners and obstacle-dodging games;
- platformers with collectibles, moving platforms, wall jumps, and dashing;
- top-down survival and projectile games;
- brick-breaking and paddle games;
- score, collection, and time-based challenges.

The current runtime does not provide network multiplayer, 3D gameplay, general-purpose card-game logic, generated image assets, or recorded music. Art, effects, and audio are produced procedurally in the browser from validated configuration and generated hooks.

## How Generation Works

1. The user creates a chat and submits a game brief.
2. The configured model proposes a component composition, gameplay rules, terminal conditions, and visual direction.
3. The backend repairs recoverable formatting problems, resolves component dependencies, and validates the plan against the component registry.
4. The Phaser SDK assembles a working game from typed, lifecycle-managed components.
5. The model may add a bounded TypeScript customization hook for behavior or presentation not covered by the declarative plan.
6. The sandbox runs TypeScript checks and creates a production Vite build.
7. Playwright loads the game and verifies readiness, component installation, gameplay progression, restart behavior, canvas dimensions, browser errors, and customization acceptance criteria.
8. A passing build is stored in the database and served from a stable `/games/<id>/` URL.

Failed candidates are not published. Generation state, repair history, model progress, and validation results are persisted per chat so the workflow can recover without treating model output as authoritative state.

## Architecture

```text
React client
    |
    v
Django REST API + Supabase authentication
    |
    v
LangGraph generation workflow
    |-- Groq / GPT-OSS planning and customization
    |-- optional local Qwen provider and context summaries
    |-- plan normalization and dependency resolution
    |-- persistent execution state and cancellation
    v
E2B sandbox
    |-- GameGen Phaser SDK
    |-- TypeScript validation
    |-- Vite production build
    |-- Playwright release gate
    v
Django game storage and stable playback route
```

### Phaser Component SDK

The active SDK is `@gamegen/arcade`, built on Phaser 4.2.1 and TypeScript. Its components have explicit dependencies, capabilities, incompatibilities, events, and cleanup responsibilities.

The catalog currently includes input, theming, movement, collision, game flow, scoring, difficulty, hazards, brick fields, HUD, effects, synthesized audio, declarative rules, entities, health, projectiles, enemies, procedural art, temporary modifiers, platforms, collectibles, and dashing.

Components are selected and combined for each request. The backend resolves required dependencies and rejects incompatible or incomplete compositions before any game is published.

### Model Providers

The default provider is Groq using `openai/gpt-oss-120b`. Requests pass through a process-wide rolling token limiter and usage calibrator to stay within the configured token-per-minute budget.

Local Qwen through Ollama is also supported by setting `GAMEGEN_MODEL_PROVIDER=qwen`. Qwen is used for local context summaries when available, while files, hashes, validation results, revisions, and workflow status remain application-owned state.

### Storage

Chats, execution state, generated game metadata, validation reports, and built files are stored through Django models in PostgreSQL. Generated artifacts are served by Django rather than by the temporary E2B preview URL.

## Repository Layout

```text
backend/
  agent/                 generation graph, tools, state, validation, and Phaser assembly
  agent/phaser_sdk/      typed composable arcade runtime
  gamegen/               authentication, chat, game storage, and HTTP endpoints
  sandbox/               E2B image definition
  scripts/               development and verification commands
frontend/my-app/         React and Vite client
scripts/                 repository-level maintenance scripts
```

The repository retains earlier Godot generation code for migration and maintenance reference. Those tools are not registered with the live agent and are not part of the active generation path.

## Prerequisites

- Python 3.13
- Poetry 2
- Node.js and npm
- PostgreSQL through a Supabase project
- Supabase authentication credentials
- an E2B account and API key
- a Groq API key, or Ollama with `qwen3:8b` for local inference
- Chromium for Playwright

## Local Setup

### 1. Backend dependencies

```bash
cd backend
poetry install
poetry run playwright install chromium
```

### 2. Environment

Create the local backend configuration from the sanitized example:

```bash
cp backend/.env.example backend/.env
```

Set the Supabase database and authentication values, E2B credentials, and at least one model provider in `backend/.env`. The default Groq configuration is:

```dotenv
GAMEGEN_MODEL_PROVIDER=groq
GROQ_API_KEY=your-key
GROQ_MODEL=openai/gpt-oss-120b
GROQ_TOKENS_PER_MINUTE=8000
GROQ_TOKEN_BUDGET=7600
GROQ_CONTEXT_WINDOW=7600
GROQ_MAX_OUTPUT_TOKENS=1200
```

For local Qwen, start Ollama separately and use:

```dotenv
GAMEGEN_MODEL_PROVIDER=qwen
```

### 3. E2B sandbox image

Build the sandbox image from the repository root:

```bash
npx -y @e2b/cli@2.21.1 auth login
npx -y @e2b/cli@2.21.1 template create gamegen-godot-4-7-2 \
  --dockerfile backend/sandbox/e2b.Dockerfile
```

Set the resulting template ID or alias as `E2B_TEMPLATE_ID`. Despite the historical template name, the image contains the active Node, Vite, TypeScript, and Phaser toolchain as well as the retained Godot toolchain.

### 4. Database

```bash
cd backend
poetry run python manage.py migrate
```

### 5. Start the backend

```bash
cd backend
poetry run start_dev
```

The API runs at `http://localhost:8000`.

### 6. Start the frontend

```bash
cd frontend/my-app
npm ci
npm run dev
```

Vite prints the local client URL, normally `http://localhost:5173`. To use a different API host, create `frontend/my-app/.env.local`:

```dotenv
VITE_API_ROOT=http://localhost:8000
```

## Verification

Run the backend test suite:

```bash
cd backend
poetry run python manage.py test agent gamegen
```

Run the Phaser SDK checks:

```bash
cd backend/agent/phaser_sdk/package
npm ci
npm run typecheck
npm test
npm run build
```

Run the frontend checks:

```bash
cd frontend/my-app
npm run lint
npm run build
```

Scan tracked files for common secret patterns before committing:

```bash
bash scripts/check-secrets.sh
```

## Security

Never commit `backend/.env`, Supabase service credentials, E2B keys, or model-provider keys. Production deployments must set `DJANGO_DEBUG=false` and provide a unique `DJANGO_SECRET_KEY`.

If a credential is committed, revoke or rotate it immediately. Removing a value from Git history does not invalidate the credential or remove copies held in forks, caches, logs, or existing clones.

## Development Status

GameGen is under active development. The component runtime and release gate are substantially more constrained than a general-purpose game engine: this improves generation reliability, but it also defines the product's present limits. New mechanics should be added as typed, independently tested components with explicit contracts rather than as prompt-only behavior.
