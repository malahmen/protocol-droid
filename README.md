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
protocol-droid.sh local setup --upgrade             # update marker
protocol-droid.sh local setup --no-llama            # skip the llama-server OCR backend
protocol-droid.sh local scan ./docs --depth 2       # list convertible files (default depth 3)
protocol-droid.sh local convert report.pdf          # one file -> ./converted
protocol-droid.sh local convert ./docs --output-format json --workers 4
protocol-droid.sh local convert a.pdf b.docx        # several files (batch, models load once)
protocol-droid.sh local status                      # version, torch device, OCR backend, caches
protocol-droid.sh local gui                         # marker's Streamlit GUI
protocol-droid.sh local server                      # marker's FastAPI server
protocol-droid.sh local clear-cache --yes           # delete ~/.cache/datalab (models re-download)
protocol-droid.sh local uninstall --yes             # remove marker's pipx env (caches kept)
GOOGLE_API_KEY=… protocol-droid.sh local convert report.pdf -- --force_ocr \
  --use_llm --llm_service marker.services.gemini.GoogleGeminiService
```

marker `convert` flags: `--output-format markdown|json|html|chunks`,
`--output-dir`, `--page-range` (single file), `--workers` (batch). Anything after
`--` is forwarded to marker (LLM-assist, force OCR, …). `GOOGLE_API_KEY` is
marker's own env name for its Gemini service — prefer it over putting the key
on the command line. Setup installs pipx and (on macOS/CPU) the `llama-server`
OCR backend; models download from the HF Hub on first run (several GB), then
run offline automatically.

marker's OCR engine (surya) picks its inference backend from
`SURYA_INFERENCE_BACKEND`: `llamacpp` (default without an NVIDIA GPU — needs
`llama-server` on PATH or `LLAMA_CPP_BINARY`) or `vllm` (default when a GPU is
detected). `status` shows which one is active and whether `llama-server` is
present.

**markitdown** (light, broad):

```sh
protocol-droid.sh local setup   --backend markitdown              # markitdown[all]
protocol-droid.sh local setup   --backend markitdown --extras pdf,docx,audio-transcription
protocol-droid.sh local setup   --backend markitdown --upgrade
protocol-droid.sh local scan    --backend markitdown ./corpus --depth 1
protocol-droid.sh local convert --backend markitdown talk.mp3     # -> ./converted/talk.md
protocol-droid.sh local convert --backend markitdown ./corpus     # a whole folder
protocol-droid.sh local convert --backend markitdown scan.pdf -- -d -e "$ENDPOINT"   # Azure Doc Intelligence
protocol-droid.sh local status  --backend markitdown
protocol-droid.sh local install-plugin --backend markitdown markitdown-sample-plugin
protocol-droid.sh local uninstall --backend markitdown --yes
```

`--extras` is `all` (default) or a comma-separated subset of markitdown's pip
extras: `pptx`, `docx`, `xlsx`, `xls`, `pdf`, `outlook`, `az-doc-intel`,
`az-content-understanding`, `audio-transcription`, `youtube-transcription`
(anything else is rejected). markitdown emits one `<name>.md` per input; flags
after `--` go to markitdown (`--use-plugins`, `-d`/`-e` for Document
Intelligence). mp3 transcription also needs `ffmpeg` on your system.

**auto** — convert only; routes each file by extension:

```sh
protocol-droid.sh local convert --backend auto ./mixed-corpus
```

The rule: **PDF and images** (`pdf png jpg jpeg tiff tif webp gif bmp`) go to
**marker**; **everything else** (Office, HTML, EPUB, CSV/JSON/XML, ZIP, `.msg`,
audio, …) goes to **markitdown**. Both write into the same `--output-dir`. No
per-tool flags after `--` in auto mode — pick a backend explicitly for those.

Both backends need **Python 3.10–3.13** (3.14 is refused: marker/torch and
onnxruntime don't support it yet; 3.12 is preferred, then 3.11, 3.13, 3.10 — a
plain `python3` in that range also works) and **pipx** (installed by `setup`);
each lives in its own pipx environment. Default output dir is `./converted`.
After a successful conversion the output folder is revealed in your file
manager; set `PROTOCOL_DROID_NO_OPEN=1` to suppress that (CI, cron, TUIs).

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
- **redis** — the job queue and result store. Results and failures are kept for
  `RESULT_TTL` seconds (default 24 h), then expire — poll and collect within
  that window; the converted files themselves stay in `/data/output`.
- **enqueue_batch.py** — a producer that walks a folder and enqueues every
  supported file in one shot.

One image (`marker-service:latest`), three roles (worker / api / batch). Runs on
CPU out of the box and uses an NVIDIA GPU automatically when the runtime is
present. Models are **not** baked into the image — they download on first run
into a shared `/models` volume (several GB). The container runs as an
unprivileged user (uid 1000), so host bind mounts must be writable by that uid.

> **OCR inside the container is not wired up yet.** marker's OCR engine (surya)
> needs an inference backend: on CPU it spawns `llama-server` (llama.cpp), which
> the image does not ship; on NVIDIA it wants vLLM. Text-layer PDFs and Office
> documents convert fine; scanned pages and `force_ocr` fail until `llama-server`
> is added to the image. Tune the choice with `SURYA_INFERENCE_BACKEND=llamacpp|vllm`
> on the worker. `use_llm` needs `GOOGLE_API_KEY` (marker's env name for its
> Gemini service) — set it in `.env`, compose passes it to the workers.

```sh
# Docker Compose
protocol-droid.sh service build                                      # just build the image
protocol-droid.sh service deploy --input ./input --output ./output   # build + start (2 workers)
protocol-droid.sh service scale --replicas 4
protocol-droid.sh service enqueue --dir /data/input                  # convert everything mounted
protocol-droid.sh service status
protocol-droid.sh service logs                                       # follow the workers
protocol-droid.sh service teardown --yes
```

`deploy` resolves `--input`/`--output` to absolute paths (relative to your
shell's cwd; defaults are `input/` and `output/` in the checkout), records them
as `MARKER_INPUT`/`MARKER_OUTPUT` in `.env` next to `docker-compose.yaml`, and
every later `scale`/`enqueue`/`status`/`logs` reuses them, so all compose calls
see the same mounts.

```sh

# Kubernetes (add --target k8s to any service command)
protocol-droid.sh service build  --target k8s        # then push the image to a registry the cluster can reach
protocol-droid.sh service deploy --target k8s
protocol-droid.sh service scale  --target k8s --replicas 6
protocol-droid.sh service enqueue --target k8s       # runs the batch Job (docs must be on the marker-input PVC)
```

Or enqueue a single document over HTTP:

```sh
curl -s localhost:8000/jobs -H 'content-type: application/json' \
  -H "authorization: Bearer $PROTOCOL_DROID_TOKEN" \
  -d '{"path":"/data/input/report.pdf","output_format":"markdown"}'
curl -s -H "authorization: Bearer $PROTOCOL_DROID_TOKEN" localhost:8000/jobs/<job_id>
```

**API access and limits**

- **Token.** Set `PROTOCOL_DROID_TOKEN` (in `.env`, or the `marker-api` Secret on
  k8s) and every request must carry `Authorization: Bearer <token>`; otherwise
  `401`. With no token the API is open, so compose binds it to
  `127.0.0.1:8000` only. To reach it from other hosts set **both** a token and
  `API_BIND=0.0.0.0` in `.env`; on k8s the Service is ClusterIP — create the
  Secret before adding an Ingress/LoadBalancer.
- **Path confinement.** `path` must resolve (after symlinks and `..`) under
  `/data/input` and `output_dir` under `/data/output`; anything else is `400`.
  Workers therefore only ever read the input mount and write the output mount.
- **Result TTL.** Job results/failures live in Redis for `RESULT_TTL` seconds
  (default 86400). `GET /jobs/{id}` returns `404` after that (and `503` when
  Redis is unreachable). Failed jobs report a generic error; the traceback is in
  the worker logs (`service logs`).

The `k8s/` manifests deploy into the `marker` namespace (`marker-api` /
`marker-worker`, backed by `marker-input` / `marker-output` / `marker-models`
PVCs). The image is not published anywhere — make `marker-service:latest`
reachable by the cluster yourself (registry push, or `kind load docker-image`).

## Requirements

- **local**: Bash 4.4+ (macOS ships 3.2 — `brew install bash`), Python
  3.10–3.13 (3.14 refused, 3.12 preferred), pipx (installed by `setup`). The
  `marker` backend also wants plenty of disk/RAM for its models and, on
  CPU, `llama-server` for OCR; the `markitdown` backend additionally wants
  `ffmpeg` for mp3 transcription.
- **service**: Docker with the Compose plugin, or kubectl + a cluster. OCR in
  the containers additionally needs `llama-server` in the image (not yet shipped,
  see above).

## License

Released into the public domain — see [LICENSE](LICENSE) (Unlicense). The
converters it drives are licensed separately:
[marker](https://github.com/datalab-to/marker) by Datalab and
[markitdown](https://github.com/microsoft/markitdown) by Microsoft — see their
repositories for terms.
