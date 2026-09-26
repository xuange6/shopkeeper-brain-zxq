# Production secrets

Run `powershell -File scripts/bootstrap_production_env.ps1` from the repository root.
It creates the required `*.txt` files in this directory and `deploy/.env.production`.

The generated files are ignored by Git. Do not copy their values into issues,
logs, screenshots, or committed configuration. In a managed deployment, replace
these local files with secrets supplied by the platform secret manager.
