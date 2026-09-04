# protocol-droid

> "I am C-3PO, human–cyborg relations." — fluent in over six million forms of
> communication, and now a few document formats too.

A gum-free, flag-driven **multi-backend** engine for turning documents into
**Markdown** (or JSON). It runs on this machine (`local`) or as a scalable
containerized service (`service`), and — locally — it can drive either of two
converters, because no single tool is best at everything:

| Backend | Tool | Best for | Weight |
| --- | --- | --- | --- |
| `marker` (default) | [datalab-to/marker](https://github.com/datalab-to/marker) | high-fidelity PDF / OCR / layout, tables | heavy (PyTorch + GB of models) |
| `markitdown` | [Microsoft markitdown](https://github.com/microsoft/markitdown) | breadth + speed; audio transcription, YouTube, ZIP, Outlook `.msg` | light (pip, no ML models) |
| `auto` | routes per file | PDF/images → marker, everything else → markitdown | — |

Either backend **prepares documents for ingestion** by a downstream RAG / LLM
pipeline — it converts, it does **not** ingest: no chunking, embedding, or
indexing happens here (that's the next stage's job).

## Credits — it drives other people's converters

protocol-droid is the automation *around* two open-source tools; the conversion
quality is theirs:

- **[marker](https://github.com/datalab-to/marker)** (Datalab) — the PDF/document
  → Markdown converter behind the `marker` backend and the containerized
  `service` (where models load **once per worker** and work fans out across
  replicas). Heavy but high-fidelity.
- **[markitdown](https://github.com/microsoft/markitdown)** (Microsoft) — the
  light, broad-format converter behind the `markitdown` backend, adding audio
  transcription, YouTube transcripts, ZIP and Outlook support.

This repo installs each in its own isolated pipx environment and gives them one
consistent, scriptable interface.

## Install / usage

`protocol-droid.sh` is a dependency-light Bash script (an entry script plus
`lib/` backend adapters) — usable directly from a shell, a Makefile, CI, or cron.
scomp-link ships a gum TUI that builds these flags for you, but nothing here
requires it.

```sh
protocol-droid.sh local   <command> [--backend marker|markitdown|auto] [flags]
protocol-droid.sh service <command> [--target docker|k8s] [flags]
protocol-droid.sh --help
```

### Local mode

`--backend` selects the converter (default `marker`). Commands vary by backend.

**marker** (heavy, high-fidelity):

```sh
protocol-droid.sh local setup                       # install marker + OCR backend (large)
protocol-droid.sh local convert report.pdf          # one file -> ./converted
protocol-droid.sh local convert ./docs --output-format json --workers 4
protocol-droid.sh local convert a.pdf b.docx        # several files (batch, models load once)
protocol-droid.sh local status                      # version, torch device, OCR backend, caches
protocol-droid.sh local gui                         # marker's Streamlit GUI
protocol-droid.sh local server                      # marker's FastAPI server
protocol-droid.sh local convert report.pdf -- --force_ocr \
  --use_llm --llm_service marker.services.gemini.GoogleGeminiService --gemini_api_key "$GEMINI_API_KEY"
```

marker `convert` flags: `--output-format markdown|json|html|chunks`,
`--output-dir`, `--page-range` (single file), `--workers` (batch). Anything after
`--` is forwarded to marker (LLM-assist, force OCR, …). Setup installs pipx and
(on macOS/CPU) the `llama-server` OCR backend; models download from the HF Hub on
first run (several GB), then run offline automatically.

**markitdown** (light, broad):

```sh
protocol-droid.sh local setup   --backend markitdown              # markitdown[all]
protocol-droid.sh local setup   --backend markitdown --extras pdf,docx,audio-transcription
protocol-droid.sh local convert --backend markitdown talk.mp3     # -> ./converted/talk.md
protocol-droid.sh local convert --backend markitdown ./corpus     # a whole folder
protocol-droid.sh local convert --backend markitdown scan.pdf -- -d -e "$ENDPOINT"   # Azure Doc Intelligence
protocol-droid.sh local status  --backend markitdown
protocol-droid.sh local install-plugin --backend markitdown markitdown-sample-plugin
```

markitdown emits one `<name>.md` per input; flags after `--` go to markitdown
(`--use-plugins`, `-d`/`-e` for Document Intelligence). mp3 transcription also
needs `ffmpeg` on your system.

**auto** — convert only; routes each file to the fitting backend:

```sh
protocol-droid.sh local convert --backend auto ./mixed-corpus   # PDFs/images -> marker, rest -> markitdown
```

Both backends need **Python 3.10+** and **pipx** (installed by `setup`); each
lives in its own pipx environment. Default output dir is `./converted`.

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

- **local**: Bash 4+, Python 3.10–3.13, pipx (installed by `setup`). The
  `marker` backend also wants plenty of disk/RAM for its models; the `markitdown`
  backend additionally wants `ffmpeg` for mp3 transcription.
- **service**: Docker with the Compose plugin, or kubectl + a cluster.

## License

Released into the public domain — see [LICENSE](LICENSE) (Unlicense). The
converters it drives are licensed separately:
[marker](https://github.com/datalab-to/marker) by Datalab and
[markitdown](https://github.com/microsoft/markitdown) by Microsoft — see their
repositories for terms.
