# protocol-droid

> "I am C-3PO, human–cyborg relations." — fluent in over six million forms of
> communication, and now a few document formats too.

A gum-free, flag-driven engine for turning documents
(PDF / DOCX / PPTX / XLSX / HTML / EPUB / images) into **Markdown or JSON**, in
two modes:

- **`local`** — run the conversion on this machine, isolated in a pipx
  environment.
- **`service`** — deploy a containerized service (a Redis queue, a pool of
  scalable workers, and a small enqueue API) to convert a whole corpus at scale.

Either way it **prepares documents for ingestion** by a downstream RAG / LLM
pipeline — it converts, it does **not** ingest: no chunking, embedding, or
indexing happens here (that's the next stage's job). protocol-droid just gets
your messy binary documents into clean, structured text.

## Powered by marker

All the actual conversion is done by
**[marker](https://github.com/datalab-to/marker)** (datalab-to/marker) — the
open-source PDF/document → Markdown converter. protocol-droid is the automation
around it: in `local` mode it installs marker in an isolated pipx environment and
drives its CLIs; in `service` mode it loads marker's models **once per worker**
and reuses them across every job (marker's own CLI reloads them per file), then
fans work out across as many workers as you scale to. Credit for the conversion
quality belongs to marker and Datalab; this repo makes it easy to run.

## Install / usage

`protocol-droid.sh` is a single, dependency-light Bash script — usable directly
from a shell, a Makefile, CI, or cron. scomp-link ships a gum TUI that builds
these flags for you, but nothing here requires it.

```sh
protocol-droid.sh <mode> <command> [flags]
protocol-droid.sh --help
```

### Local mode (pipx marker on this machine)

```sh
protocol-droid.sh local setup                 # install marker + OCR backend (large)
protocol-droid.sh local status                # version, torch device, OCR backend, caches
protocol-droid.sh local convert report.pdf    # one file -> ./marker-output
protocol-droid.sh local convert ./docs --output-format json --workers 4   # a folder (batch)
protocol-droid.sh local convert a.pdf b.docx  # several files (batch, models load once)
protocol-droid.sh local scan ./docs           # list convertible files (for a picker)
protocol-droid.sh local gui                   # marker's Streamlit GUI
protocol-droid.sh local server                # marker's FastAPI server
protocol-droid.sh local uninstall --yes       # remove the pipx env (caches kept)
```

`convert` flags: `--output-format markdown|json|html|chunks`, `--output-dir`,
`--page-range` (single file), `--workers` (batch). Anything after `--` is passed
straight to marker, which is how you enable LLM-assisted conversion or force OCR:

```sh
protocol-droid.sh local convert report.pdf -- --force_ocr \
  --use_llm --llm_service marker.services.gemini.GoogleGeminiService --gemini_api_key "$GEMINI_API_KEY"
```

marker needs **Python 3.10–3.13** and **pipx**; `local setup` installs pipx and
(on macOS/CPU) marker's OCR backend `llama-server` (Homebrew's `llama.cpp`) for
you. Models download from the Hugging Face Hub on first run into
`~/.cache/huggingface` (several GB), then run offline automatically.

### Service mode (containerized, scalable)

```
                 POST /jobs
  caller ───────────────────────▶  api  ──┐
  (RAG pipeline, curl, batch)             │  enqueue
                                          ▼
                                    Redis queue ("marker")
                                          │
                        ┌─────────────────┼─────────────────┐
                        ▼                 ▼                 ▼
                    worker            worker            worker      (scale ↕)
                   (marker)          (marker)          (marker)
                        └─────────────────┼─────────────────┘
                                          ▼
                             /data/output  (Markdown / JSON)
```

- **api** — FastAPI front door. `POST /jobs` enqueues one document; `GET
  /jobs/{id}` polls status; `GET /healthz` pings Redis. It does no conversion.
- **worker** — an RQ `SimpleWorker` that loads marker's model dict once and runs
  `marker` on each queued document. Add more for more throughput.
- **redis** — the job queue and result store.
- **enqueue_batch.py** — a producer that walks a folder and enqueues every
  supported file in one shot.

One image (`marker-service:latest`), three roles (worker / api / batch). Runs on
CPU out of the box and uses an NVIDIA GPU automatically when the runtime is
present. Models are **not** baked into the image — they download on first run
into a shared `/models` volume (several GB).

```sh
# Docker Compose
protocol-droid.sh service deploy --input ./input --output ./output   # build + start (2 workers)
protocol-droid.sh service scale --replicas 4
protocol-droid.sh service enqueue --dir /data/input                  # convert everything mounted
protocol-droid.sh service status
protocol-droid.sh service logs
protocol-droid.sh service teardown --yes

# Kubernetes (add --target k8s to any service command)
protocol-droid.sh service build  --target k8s        # then push the image to a registry the cluster can reach
protocol-droid.sh service deploy --target k8s
protocol-droid.sh service scale  --target k8s --replicas 6
protocol-droid.sh service enqueue --target k8s       # runs the batch Job (docs must be on the marker-input PVC)
```

Or enqueue a single document over HTTP:

```sh
curl -s localhost:8000/jobs -H 'content-type: application/json' \
  -d '{"path":"/data/input/report.pdf","output_format":"markdown"}'
curl -s localhost:8000/jobs/<job_id>
```

The `k8s/` manifests deploy into the `marker` namespace (`marker-api` /
`marker-worker`, backed by `marker-input` / `marker-output` / `marker-models`
PVCs). The image is not published anywhere — make `marker-service:latest`
reachable by the cluster yourself (registry push, or `kind load docker-image`).

## Requirements

- **local**: Bash 4+, Python 3.10–3.13, pipx (installed by `local setup`), and
  enough disk/RAM for marker's models.
- **service**: Docker with the Compose plugin, or kubectl + a cluster.

## License

Released into the public domain — see [LICENSE](LICENSE) (Unlicense). marker is
licensed separately by Datalab; see its
[repository](https://github.com/datalab-to/marker) for its terms.
