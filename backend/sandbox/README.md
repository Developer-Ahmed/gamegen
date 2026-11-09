# Godot E2B sandbox

Generated games require the pinned Godot editor and matching web export templates in the sandbox.
The image installs only the single-threaded Web release template used by GameGen, rather than
downloading and retaining Godot's full multi-platform export-template pack.

Authenticate and build the image from the repository root with the pinned v2 CLI:

```bash
npx -y @e2b/cli@2.21.1 auth login
npx -y @e2b/cli@2.21.1 template create gamegen-godot-4-7-2 --dockerfile backend/sandbox/e2b.Dockerfile
```

Set the resulting template ID or alias before starting Django:

```bash
export E2B_TEMPLATE_ID=gamegen-godot-4-7-2
export E2B_API_KEY=<your-e2b-api-key>
cd backend
poetry run dev
```

The backend uses `GODOT_BIN=godot` by default. Override it only when the sandbox installs the editor at a different executable path.
