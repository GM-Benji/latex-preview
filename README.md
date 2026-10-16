# latex-preview

Self-hosted LaTeX editor with live PDF preview — a small, single-file alternative to Overleaf for your own server.

- **Document picker** — every main `.tex` (one containing `\documentclass`) under a folder is listed.
- **Live preview** — sources are watched; on save the document is rebuilt and the PDF (rendered with PDF.js)
  reloads in place, keeping your scroll position. Errors are shown as clickable `file:line` links.
- **Source editor** — CodeMirror with LaTeX highlighting, file list, Ctrl+S / autosave, conflict detection
  when a file changes on disk, Ctrl+click on `\input{…}` to open included files.
- **Visual mode** — click text in the rendered PDF and edit it in place; the change is mapped back to the
  right source file and line via SyncTeX and only the changed words are replaced.
- **Double-click the PDF** to jump to the corresponding source line.
- Password protection (HTTP Basic).

Build pipeline (per document, output in `<doc dir>/.build/`, sources untouched):
`xelatex → biber → makeglossaries → xelatex → xelatex` (biber/makeglossaries are skipped when unused;
`--engine pdflatex|lualatex` is supported).

The server is one Python file using only the standard library; TeX Live is provided by the Docker image.

## Quick start (Docker Compose)

```sh
git clone <this repo> latex-preview && cd latex-preview
cp .env.example .env        # then edit: password, DOCS_DIR, UID/GID (`id -u`, `id -g`)
docker compose up -d --build
```

Open `http://<server>:8090`, log in with any username and the password from `.env`.

The image is Ubuntu 24.04 + `texlive-full` (TeX Live 2023) + a large set of system fonts, so the first
build downloads a few GB (image ≈ 7.5 GB). `latex-preview.py` is bind-mounted, so after editing it only
`docker compose restart` is needed.

Logs: `docker compose logs -f`.

## Without Docker

Needs Python ≥ 3.9, a TeX distribution with `xelatex`, `biber`, `makeglossaries` and `synctex`.

```sh
LATEX_PREVIEW_PASSWORD=secret ./latex-preview.py ~/latex --port 8090   # server
./latex-preview.py path/to/project --once                            # one-shot build, exit code 0/1
```

## Security notes

- Anyone with the password can **read and modify** every text file under `DOCS_DIR` (only files with
  LaTeX-related extensions inside a document's folder; path traversal is rejected). Choose a real password
  if the port is reachable by people you don't fully trust.
- HTTP Basic auth over plain HTTP sends the password unencrypted. Use it on a trusted LAN or a VPN such as
  Tailscale/WireGuard (which encrypt the traffic), or put a TLS reverse proxy (Caddy, nginx) in front.
- Documents are compiled with the TeX defaults (restricted `\write18`); still, only open documents you trust.
- The browser loads PDF.js and CodeMirror from cdnjs, so clients need internet access.

## HTTP API

| Endpoint | Description |
|---|---|
| `GET /docs` | list of documents |
| `GET /pdf?doc=` · `/status?doc=` · `/log?doc=` | last good PDF · build state (JSON) · build log |
| `POST /rebuild?doc=` | force a rebuild |
| `GET /files?doc=` · `GET/PUT /file?doc=&path=` | editable files · read/write (`X-Base-Mtime` → 409 on conflict) |
| `GET /synctex?doc=&page=&x=&y=` | PDF point → source `file:line` |
| `POST /visual-edit?doc=` | `{page,x,y,old,new}` → patch the source |

## License

MIT — see [LICENSE](LICENSE).
