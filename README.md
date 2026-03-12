# Overleaf Sync

Overleaf Sync is a two-way sync CLI for local folders and Overleaf projects.

It is built for people who want reproducible local editing, exact remote reconciliation, and a simple command line workflow without manually downloading zip files from Overleaf.

## What it does

- Syncs a local folder to an existing Overleaf project.
- Pulls an Overleaf project back to the local folder.
- Compares against the real remote file tree and the remote zip payload before applying changes.
- Uses OT updates for text documents when possible and falls back to file upload when needed.
- Prints the real remote Overleaf file tree.
- Lists compile artifacts and downloads logs, aux files, or all outputs on demand.
- Downloads the compiled PDF for a project.
- Supports browser-based login and `.olignore` filtering.

## Why this exists

Most lightweight Overleaf sync scripts stop at "upload files". Overleaf Sync goes further:

- it reads the actual Overleaf project tree
- it reconciles local files against the remote zip snapshot
- it can delete remote files that should no longer exist
- it avoids common environment issues such as broken proxy-driven uploads

## Install

```bash
git clone https://github.com/GaryOAO/overleaf-sync.git
cd overleaf-sync
python -m venv .venv
source .venv/bin/activate
pip install -e .
pip install PySide6
playwright install chromium
```

`PySide6` is only required for the `login` command.

## Commands

```bash
# Login and persist auth
overleaf-sync login --store-path .overleaf-sync-auth

# List projects
overleaf-sync list --store-path .overleaf-sync-auth

# Push local files to Overleaf
overleaf-sync -l --name "My Overleaf Project" --store-path .overleaf-sync-auth

# Pull remote files to local
overleaf-sync -r --name "My Overleaf Project" --store-path .overleaf-sync-auth

# Show the real remote file tree
overleaf-sync tree --name "My Overleaf Project" --store-path .overleaf-sync-auth

# List compile artifacts
overleaf-sync artifacts --name "My Overleaf Project" --store-path .overleaf-sync-auth

# Download selected compile artifacts
overleaf-sync artifacts --name "My Overleaf Project" --store-path .overleaf-sync-auth --artifact output.log --artifact output.stderr

# Download all compile artifacts
overleaf-sync artifacts --name "My Overleaf Project" --store-path .overleaf-sync-auth --all --download-path output

# Download compiled PDF
overleaf-sync download --name "My Overleaf Project" --store-path .overleaf-sync-auth --download-path output
```

If `--name` is omitted, Overleaf Sync uses the current directory name.

## `.olignore`

Overleaf Sync reads `.olignore` from the sync root and excludes matching paths before reconciliation.

Example:

```gitignore
*.aux
*.bbl
*.blg
*.log
*.out
*.pdf
output/*
.overleaf-sync-auth
.olauth
```

## Security

Do not commit these files:

- `.overleaf-sync-auth`
- `.olauth`
- `.olignore` if it contains project-specific private paths
- downloaded PDFs or private project source trees
- compile logs or artifacts if they contain private project contents

This repository intentionally does not include any Overleaf auth store, cookies, private project data, or local export artifacts.

## Notes

- The tool syncs against an existing Overleaf project. It does not create Overleaf projects.
- Browser login requires a desktop environment.
- For large local workspaces, keep backup archives and generated outputs in `.olignore`.

## License

MIT.

Inspired by the original `olsync` and Overleaf browser-login/client work by Moritz Glöckl. Portions of the browser login flow and older client code were adapted from prior MIT-licensed tooling and kept under MIT-compatible terms.
