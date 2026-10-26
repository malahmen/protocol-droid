# protocol-droid

[![ci](https://github.com/malahmen/protocol-droid/actions/workflows/ci.yml/badge.svg)](https://github.com/malahmen/protocol-droid/actions/workflows/ci.yml)

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

`protocol-droid.sh` is the entry point — usable directly from a shell, a
Makefile, CI, or cron. The engine behind it is Python (`pdroid/`, standard
library only, any `python3` 3.9+); the script is a launcher that keeps the
name and calling convention everything already uses, and `protocol_droid.py`
runs the same engine directly. scomp-link ships a gum TUI that builds these
flags for you, but nothing here requires it.

The bash engine this replaced is release
[v1.0.0](https://github.com/malahmen/protocol-droid/releases/tag/v1.0.0): the
same commands, flags, messages and exit codes.

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
protocol-droid.sh local clear-cache --yes           # delete surya's model cache (models re-download)
protocol-droid.sh local uninstall --yes             # remove marker's pipx env (caches kept)
protocol-droid.sh local convert report.pdf -- --force_ocr \
  --use_llm --openai_base_url http://192.168.3.46:1234/v1 --openai_model qwen3-coder
```

marker `convert` flags: `--output-format markdown|json|html|chunks`,
`--output-dir`, `--page-range` (single file), `--workers` (batch). Anything after
`--` is forwarded to marker (LLM-assist, force OCR, …) — including the LLM
options, since the local backend hands marker's flags straight through and has
no deployment config of its own. A folder means its convertible files as deep
as `scan` goes (3 levels), staged for marker's batch CLI - which on its own
reads only the top of a folder - so what is converted and what gets a sidecar
are the same list; the output folder is never part of it, wherever it is. Each
document's output is `<out>/<stem>/`; inputs that share a stem (`x/a.pdf` and
`y/a.pdf`, or `report.pdf` and `report.docx`) land as `<dir>_<stem>_<ext>/`
(`x_a_pdf/`, `report_docx/`) - names from the inputs, never from their order.
A folder whose sidecar names a document this run gave another folder (an
earlier run's name for it) is removed. A folder an earlier
run left that this run did not write to is a failure, not output (marker's
batch CLI carries on past a file it cannot convert and exits 0), unless
`-- --skip_existing` asked marker to skip it; "this run" is read on the output
disk's own clock, so a network share's clock does not have to match yours. Setup installs pipx and (on macOS/CPU) the
`llama-server` OCR backend; models download on first run (several GB), then run
offline automatically.

There are **two** model caches, and they are filled from different places:

| Cache | Holds | Moved by |
| --- | --- | --- |
| `$HF_HOME/hub` (`~/.cache/huggingface/hub`) | marker's layout/table models, from the Hugging Face Hub | `HF_HOME` |
| `<platform cache>/datalab/models` (`~/.cache/datalab/models`) | surya's OCR models, from `models.datalab.to` over plain HTTP | `MODEL_CACHE_DIR`, or `XDG_CACHE_HOME` |

The second one is not a Hub cache, which is why `HF_HUB_OFFLINE` has no effect
on it. `status` prints both resolved paths — including when they do not exist
yet, because "empty" and "somewhere else" are different answers — and
`clear-cache --yes` names the directory it is about to delete.

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
`local convert` exits non-zero when any file failed to convert (in `auto` mode,
after both backends have run), so a cron job or CI step sees the failure.

### Container mode (marker, nothing installed)

marker from the service image, run once on local files - no pipx, no
particular Python, no torch on the host. It is the same image the service runs
(`marker-service:latest`), calling the same `convert_document`, so a container
conversion writes the same folders and the same provenance sidecar as a queued one.

```sh
protocol-droid.sh container build                         # the image (same as `service build`)
protocol-droid.sh container convert ./docs                # folders, files, or both -> ./converted
protocol-droid.sh container convert --output-format json --page-range 0-4 report.pdf
protocol-droid.sh container bench ./sample-pdfs           # the per-file overhead vs each warm document
protocol-droid.sh container status
```

- Each input's folder is mounted **read-only** and the output folder read-write, **at their own paths**, so no path is rewritten.
- The output **mirrors the input tree**, as the service's does: `2023/report.pdf` and `2024/report.pdf` go to `2023/report_pdf/` and `2024/report_pdf/`. The tree is taken from the folder given, or the common parent of the files given, so a single file lands where it always did.
- The runtime's own failures (exit `125`-`127`: the container did not start, or its command did not run) are reported as `1`, inside the exit codes below.
- The models download once into the named volume `protocol-droid-models` (`PROTOCOL_DROID_MODELS_VOLUME`), not per run.
- `--runtime podman|docker` (default: podman if present). On rootless podman the caller is mapped onto the image's `app` user, so outputs belong to the caller; docker on Linux writes them as uid 1000 and says so.
- markitdown is not in this image - it is a pip package with no models. It has a small image of its own (`Dockerfile.markitdown`, see "protocol's converter port" below), and `local --backend markitdown` isolates it on the host.

**`bench`** measures what keeping marker resident saves, per document, and
what each kind of page costs. Report schema `protocol-droid/bench/2`, written
as JSON (`--report FILE`, default `./marker-bench-<host>-<time>.json`).

The first version timed only the model load (3.6 s on corelia) as the
overhead, where a per-file run really pays several times that, and on that it
gave a wrong answer to a gating decision (Kamino item 64); and it averaged
text-layer and OCR pages, which differ ~18x (item 65). A warm sample that fails
after the timed runs no longer loses them: that path's overhead is reported as
null with the reason. Now:

| Field | Means |
| --- | --- |
| `overhead.ocr`, `overhead.text` | **Measured** per path: page 0 of one PDF in a fresh process (after a priming run, which pays for reading the models from disk) minus the same page in the warm bench process. What a per-file CLI pays per file and a resident worker pays once. `seconds: null` with a `why` where the samples cannot show it - never a guess |
| `warm.text`, `warm.ocr` | Throughput once warm, **per path, never blended**: pages, seconds, `seconds_per_page`, `pages_per_minute` |
| `resident_saving[]` | Per document: its warm time, its path's overhead, and the speedup a resident worker gives it. A text-layer document also gets `speedup_if_its_tables_are_ocrd`: OCR'd tables start llama-server, so it may pay up to the OCR overhead |
| `model_load_seconds`, `warmup_seconds` | Inside the bench process, for reference - not the overhead |
| `llama_server_extra_args` | `--no-cache-prompt`: llama-server otherwise serves a page it has seen from its prompt cache (3.0 s against 15.1 s), which would make `--runs` and the overhead's warm sample time recall instead of conversion. Pages it has not seen take the same time either way |

On corelia (marker 2.0.0, 32 CPUs, no GPU, 2026-10-16): overhead **17.0 s** on the
OCR path and **5.1 s** on a text layer; OCR at ~58 s/page.

`--page-range R` converts only those pages of every document (cut to each
document's length), which keeps a run on a scanned book to minutes;
`--force-ocr` OCRs every page, to measure that path on purpose; `--runs N`
repeats the set; `--no-cold` skips the overhead (it costs three fresh-process
conversions per path). The first run on a machine also downloads the models.

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
- **Output layout.** Each document gets its own folder that mirrors its path
  under the input root, with the extension kept in the folder name:
  `/data/input/a/report.pdf` → `/data/output/a/report_pdf/report.md` (plus its
  images). Files with the same name in different folders, or with different
  extensions, therefore never overwrite each other. A file with no extension
  (or an underscore in it) gets its full name plus `.d` - `README` →
  `README.d/` - so it can never share a folder with a look-alike such as
  `README.md`'s `README_md/`. Only case variants of one extension share a
  folder (`report.PDF`, `report.pdf`). A job's `result` is that folder.
- **enqueue_batch.py** — a producer that walks a folder and enqueues every
  supported file in one shot.

One image (`marker-service:latest`), three roles (worker / api / batch). Runs on
CPU out of the box and uses an NVIDIA GPU automatically when the runtime is
present. Models are **not** baked into the image — they download on first run
into a shared `/models` volume (several GB). The container runs as an
unprivileged user (uid 1000), so host bind mounts must be writable by that uid.

> **Non-root and your mounts.** On Linux the first login account is usually uid
> 1000, so `./input` / `./output` (created by `deploy` as you) are already
> writable by the container; Docker Desktop on macOS maps ownership for you. If
> your host uid is *not* 1000, either `chown -R 1000:1000 ./input ./output` or
> add `user: "$(id -u):$(id -g)"` to the `api` and `worker` services in
> `docker-compose.yaml` (the image's `/data` and `/models` are world-traversable,
> and the `models` named volume inherits the image's ownership). On Kubernetes
> the manifests set a pod `securityContext` (`runAsUser`/`runAsGroup`/`fsGroup`
> 1000, `runAsNonRoot: true`) so the kubelet hands the PVCs to gid 1000 —
> required, or the workers cannot write `/data/output` or the model cache.
> `fsGroup` is only honoured by drivers that manage volume ownership; on an
> NFS-backed RWX share set the ownership on the server instead.

**OCR in the containers.** marker's OCR engine (surya) does not run inference
in-process: it spawns a `llama-server` (llama.cpp) and drives it over loopback.
The image builds that binary itself (multi-stage, from `ggml-org/llama.cpp`
`v0.4.0`, CPU-portable) and points surya at it, so scanned pages and
`force_ocr` work in the service — no extra setup. The knobs:

- `SURYA_INFERENCE_BACKEND` — `llamacpp` or `vllm`. Surya defaults to `vllm`
  when it sees an NVIDIA GPU, and vLLM is *not* installed in this image, so the
  Dockerfile pins `llamacpp`; the bundled server is CPU-only, which is the
  slower but always-available path. Set `vllm` (and install it) only if you
  build your own GPU image.
- `LLAMA_CPP_BINARY` — path to the server, preset to
  `/usr/local/bin/llama-server`. Also honoured by `local setup`, which installs
  llama.cpp via Homebrew on the host (`setup --no-llama` skips it).
- Optional: `LLAMA_CPP_NGL`, `LLAMA_CPP_EXTRA_ARGS`, and surya's
  `SURYA_INFERENCE_URL` (attach to an already-running server instead of
  spawning one) / `SURYA_INFERENCE_KEEP_ALIVE`.
- On first OCR run surya downloads its own GGUF weights (`datalab-to/surya-ocr-2-gguf`,
  several GB) into `HF_HOME=/models`, and the server holds them resident next to
  marker's models — hence the 12Gi worker memory limit in `k8s/worker.yaml`.
- Surya's other OCR models come from `models.datalab.to`, not the Hub, into
  `MODEL_CACHE_DIR=/models/datalab` — on the PVC, so they are downloaded once
  and shared. Left at its default that directory is inside the pod's own
  writable layer: every new pod re-downloaded them, and an air-gapped cluster
  could not convert at all. `HF_HUB_OFFLINE` does not cover this cache.
  Scale the workers up **after** one conversion has warmed `/models`: the
  download is "fetch into a temp dir, then move", so several cold replicas
  racing on a shared RWX volume can see a manifest whose files are still
  arriving.

`service build` and `service deploy` probe the freshly built image with
`llama-server --version` and say whether OCR will work; `service status` repeats
the check, mirroring what `local status` reports for a host install. A missing
binary is reported, not fatal — text-layer PDFs still convert. `service deploy
--target k8s` cannot build as part of `kubectl apply`, so it builds the image
first when it is not already present locally.

#### Which LLM `use_llm` uses

marker's own default is Google Gemini, which needs a Google API key and reaches
the internet. To point it at a model on your own network instead, configure the
deployment — in both the API and the worker, since the worker re-validates
every job it runs:

| Variable | Meaning |
| --- | --- |
| `LLM_SERVICE` | `ollama`, or `openai` for anything **OpenAI-compatible** — llama.cpp's server, vLLM, LM Studio, LocalAI |
| `LLM_BASE_URL` | its endpoint. Defaults are loopback (`:11434`, `:8080/v1`), not a cloud API |
| `LLM_MODEL` | optional; marker's own default otherwise |
| `LLM_API_KEY` | optional — an OpenAI-compatible server usually ignores it, but the client requires one to be present |

Set them in `.env` for Compose, or in `k8s/llm-configmap.yaml` for Kubernetes.
With `LLM_SERVICE` unset, `use_llm` is **refused** — a `400` from the API, and
the same check again in the worker for a job that arrived another way — rather
than accepted and then failed on a missing Gemini key.

Two things are deliberately **not** request fields:

- **the service.** marker's `llm_service` option is a dotted class path that it
  imports, so accepting one from a request would be an arbitrary-class import.
  A short name is mapped to a fixed path instead.
- **the endpoint.** A caller who could set the base URL could point a worker at
  any host it can reach. It is a property of where the service runs, not of one
  document.

A request may only say `use_llm: true`.

Note what `localhost` means to a worker in a pod: the pod. If the models run on
another host, name that host. A worker running under podman *on* the machine
with the models is the case where loopback is right — and on this network that
machine has four times the cores of the cluster node, so it is also the faster
one.

```sh
# Docker Compose
protocol-droid.sh service build                                      # just build the image (+ verify OCR)
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
  `401`. `/docs` carries the schema in the page itself, so with a token set the
  browser does not need to fetch `/openapi.json` (which it would without the
  token). With no token the API is open, so compose binds it to
  `127.0.0.1:8000` only. To reach it from other hosts set **both** a token and
  `API_BIND=0.0.0.0` in `.env`; on k8s the Service is ClusterIP — create the
  Secret before adding an Ingress/LoadBalancer.
- **Redis is closed too.** RQ jobs are pickled callables, so whoever can write
  to the queue can run code in the workers, past the API's path checks. Redis
  therefore requires AUTH. With compose, `service deploy` generates
  `REDIS_PASSWORD` into `.env` (mode 600) on first deploy and keeps it; Redis
  is never published outside the compose network. On k8s, `service deploy`
  creates the `marker-redis` Secret when missing (pods refuse to start without
  it), and `k8s/networkpolicy.yaml` lets only the api, worker and batch pods
  reach Redis. That policy needs a CNI that enforces NetworkPolicy (Calico,
  Cilium, kindnet in kind ≥ 0.24); AUTH applies either way. Applying the
  manifests by hand: `kubectl -n marker create secret generic marker-redis
  --from-literal=password="$(openssl rand -hex 24)"` first.
- **The queue carries data, never code.** An RQ job names a function and the
  worker calls it, so a plain worker runs whatever a job in Redis names - a job
  naming `os.system` ran, pickled or as JSON. Jobs here are JSON and name
  `tasks.convert_document` only, and the worker (`GuardedWorker`) refuses any
  job that names anything else, passes anything but a path and an options dict,
  carries a callback, a webhook or a dependency, has a path outside
  `INPUT_DIR` or an `output_dir` outside `OUTPUT_DIR`, or has an option other
  than `output_format`, `output_dir`, `use_llm`, `force_ocr`, `page_range` -
  without running it. marker builds classes named in its options
  (`processors`, `converter_cls`), so those are refused everywhere, the CLI
  included. RQ also reaches a job's callbacks and webhooks outside the run
  (its registry cleanup, and failing a job), so the worker loads jobs as
  `GuardedJob`, whose callbacks and webhooks do nothing. Jobs queued before
  this change were pickled and are refused the same way: re-enqueue them.
- **Path confinement.** `path` must resolve (after symlinks and `..`) under
  `/data/input` and `output_dir` under `/data/output`; anything else is `400`.
  Workers therefore only ever read the input mount and write the output mount.
- **Result TTL.** Job results/failures live in Redis for `RESULT_TTL` seconds
  (default 86400). `GET /jobs/{id}` returns `404` after that (and `503` when
  Redis is unreachable). Failed jobs report a generic error; the traceback is in
  the worker logs (`service logs`).

The `k8s/` manifests deploy into the `marker` namespace (`marker-api` /
`marker-worker`, backed by `marker-input` / `marker-output` / `marker-models`
PVCs). The image is not published anywhere, so make it reachable by the
cluster yourself (registry push, or `kind load docker-image`). `service build`
also tags each build with its image ID (`marker-service:<id>`) and `service
deploy` applies the manifests pinned to that tag (and `service enqueue` runs its
batch job on the image the deployed workers run, not this machine's newest
build), so a rebuild is a new pod
spec and rolls out - with plain `:latest` and `IfNotPresent` the pods kept
the old image. Load the tag `deploy` names. Without docker on the deploying
machine the ID cannot be read: `:latest` is applied and it says how to roll
by hand.

**Storage on Kubernetes.** Two settings, read by `service deploy --target k8s`
from the environment or `.env` (the environment wins):

| Setting | Applies to | Default |
| --- | --- | --- |
| `K8S_STORAGE_CLASS` | all three claims | empty: the cluster's default class |
| `K8S_ACCESS_MODE` | `marker-input`, `marker-output` | `ReadWriteOnce` |

The model cache (`marker-models`) is always `ReadWriteOnce`. `ReadWriteOnce`
means pods on **one node**, not one pod, so on a single-node cluster (k3s, kind,
anything on `local-path`) every replica shares it. That is also all `local-path`
can provision: a `ReadWriteMany` request there never binds, and the deployment
sits `Pending`. When the cluster gains a node, add an RWX-capable class (an NFS
export, CephFS, ...) and set `K8S_STORAGE_CLASS=<it>` plus
`K8S_ACCESS_MODE=ReadWriteMany`. Give the model cache that class too, or accept
one cache, and one model download, per node. `k8s/pvc.yaml` on its own is a
valid manifest with the defaults; `kubectl apply -f` it by hand and it binds on
a single node. Claims are immutable: changing an existing claim's class or mode
means deleting it first (a `Pending` one holds no data).

### protocol's converter port (two images)

[protocol](https://github.com/malahmen/protocol), the conversion API, runs these
images as its workers and calls each converter with the document as bytes:

| Image | Built from | Converter | Port |
| --- | --- | --- | --- |
| `marker-service:latest` | `Dockerfile` | marker: PDF and images | `tasks.convert_bytes` |
| `markitdown-service:latest` | `Dockerfile.markitdown` | markitdown: DOCX, PPTX, XLSX, XLS, Outlook, HTML, EPUB, CSV... | `markitdown_tasks.convert_bytes` |

Both take `(data, original_name, options)` and return `(files, tool_versions)`:
every output file as bytes, keyed by its relative name. Each converts in a
scratch folder of its own (`port.py`) that a path in the name cannot steer, and
removes it. The engine's `provenance.json` is not among the files - protocol
writes its own under the same key - so its versions come back as
`tool_versions` instead.

markitdown has an image of its own because it needs nothing of marker's: 430 MB
against 6.8 GB, the same split protocol makes with a queue per converter. It
installs only the extras protocol routes to it (docx, pptx, xlsx, xls, outlook),
keeps plugins off, converts local files only (never a URL), and takes no options.

```sh
podman build -f Dockerfile.markitdown -t markitdown-service:latest .
podman run --rm markitdown-service:latest              # what is installed
podman run --rm --security-opt label=disable -v "$PWD:/w:ro" \
  markitdown-service:latest python -m markitdown_tasks /w/report.docx
```

## Exit codes

Every `convert` path ends on one of these. They exist because a scripted caller
could not tell a failed batch from an empty one — both left with `0`, as did a
run whose input file was not there at all.

| Code | Means |
| ---: | --- |
| `0` | everything asked for was converted |
| `1` | at least one conversion failed, or its provenance sidecar could not be written. Also every usage error |
| `2` | a named input does not exist, or is not a file — nothing was attempted for it |
| `3` | nothing to convert: the folder held no file the backend handles |

`2` and `3` are deliberately separate: "you named a file that is not there" and
"this folder has nothing I convert" need different responses from a cron job.
`--backend auto` reports the **worst** of its two backends in the order
`1 > 2 > 3`, so an empty second backend cannot hide the first one's failure.

## Provenance

Every conversion writes a JSON sidecar next to its output, recording what was
converted, by which versions, with which options, and when:

| Layout | Sidecar |
| --- | --- |
| marker — one folder per document | `<out>/<stem>/provenance.json` |
| markitdown — one `.md` per input in a shared folder | `<out>/<name>.md.provenance.json` |

markitdown's is named after its output on purpose: a single `provenance.json`
in a shared folder would be overwritten by the next document in the same run,
leaving one sidecar for the whole batch and nothing to say so.

markitdown writes `<stem>.md`, except where several inputs of one run share a
stem - `a/notes.html` and `b/notes.html`, or `report.docx` and `report.pptx` -
which used to write one file, the last input winning. Each of those gets
`<dir>/<stem>_<ext>.md` (`a/notes_html.md`, `report_pptx.md`), the directory
relative to the folder given; a name another input already has this way
(`report_docx.html` next to `report.docx`) takes the same form
(`report_docx_html.md`). The names depend only on the inputs, so a re-run
replaces its own output; an output this run already wrote is never replaced by
another of its inputs - that input fails instead. An output folder inside the
input folder is skipped, and a sidecar is never an input.

`conversion.command` is the argv that ran (the local marker backend used to
record a rebuilt one), with the value of any credential-looking flag
(`--openai_api_key …`) redacted as options are, and any URL's
`user:password@` replaced (`--openai_base_url http://[redacted]@host/v1`);
marker's `versions` include
`surya-ocr` and `torch`.

```json
{
  "schema": "protocol-droid/provenance/1",
  "source": { "path": "…/report.pdf", "relative_path": "a/report.pdf",
              "sha256": "…", "bytes": 182344, "modified": "2026-10-09T11:02:15Z" },
  "conversion": { "backend": "marker", "started": "…", "finished": "…",
                  "seconds": 41.8, "output_format": "markdown",
                  "command": ["marker", "…"], "output_dir": "…",
                  "outputs": ["report.md", "report_meta.json"] },
  "versions": { "marker-pdf": "2.0.0", "surya-ocr": "0.22.1", "torch": "2.9.0" },
  "options": { "use_llm": true, "openai_api_key": "[redacted]" }
}
```

The point is **re-conversion**. Without a sidecar, a marker bump is a
corpus-wide re-run, because nothing on disk says which documents were converted
by the old version — and the same goes for a changed option or an edited
source. With one, the question is a diff: this source hash and these versions
against the sidecar. It is also cheap now and impossible to backfill: a sidecar
cannot be reconstructed for a document that was already converted.

Two deliberate choices:

- **Credentials are redacted** by key name — `openai_api_key` and anything else
  matching `key|token|secret|password|credential`. The resolved options carry
  the deployment's LLM key, and `output/` is the directory meant to be handed to
  the next pipeline stage. A URL's userinfo is replaced in any value.
- **A sidecar that cannot be written is a failure** (exit `1`), not a warning.
  The output is still on disk, but a conversion with no provenance is only
  discovered at the moment somebody needs to decide what to re-convert — which
  is the one thing the sidecar is for.

`PROTOCOL_DROID_NO_PROVENANCE=1` turns it off. All three execution modes — the
two local backends and the containerized worker — share one implementation
(`provenance.py`), so the sidecars are identical whichever ran.

## Requirements

- **the engine**: `python3` 3.9+, any version — it runs on the host's own
  interpreter and never imports marker or markitdown.
- **local**: for the converters' own environments, a Python 3.10–3.13 (3.14
  refused, 3.12 preferred) and pipx (installed by `setup`). The
  `marker` backend also wants plenty of disk/RAM for its models and, on
  CPU, `llama-server` for OCR; the `markitdown` backend additionally wants
  `ffmpeg` for mp3 transcription.
- **service**: Docker with the Compose plugin, or kubectl + a cluster. The image
  build also compiles `llama-server` from source (surya's OCR backend), so the
  build needs network access to GitHub and a few minutes of CPU; nothing extra
  is required at runtime.

## Tests

```sh
python3 -m unittest discover -s tests -v   # the Python suites
tests/test-exit-codes.sh                   # end to end through protocol-droid.sh
tests/test-container.sh
tests/test-k8s-storage.sh
```

Standard library only — no network, no models, no worker, no queue, and no
converter: the exit-code suite puts a stub `markitdown` on `PATH`, which writes a
predictable `.md` and fails on any file whose name contains `boom`.

| File | Checks | What it pins |
| --- | ---: | --- |
| `tests/test_options.py` | 12 | `--page-range` and `--output-format` validation |
| `tests/test_output_folder.py` | 14 | which name an output file gets, including collisions and symlinks |
| `tests/test_llm_config.py` | 12 | which LLM `use_llm` reaches, and that a request cannot name one |
| `tests/test_provenance.py` | 41 | what the sidecar records, what it redacts (credential-named values, a URL's `user:password@`), and its script interface |
| `tests/test_local_convert.py` | 13 | local runs in process against the stubs: the guard that fails an input whose output another already wrote, case-folded and order-free output names, the output folder and sidecars never scanned (also by `enqueue_batch.py`, with rq), a client clock ahead of the output disk's, earlier copies of a document removed |
| `tests/test-exit-codes.sh` | 50 | exit codes, overwrite behaviour and sidecars, end to end through `protocol-droid.sh` |
| `tests/test_cli.py` | 29 | the container entry: `bench`'s measurements (the overhead cold minus warm and its order, per-path throughput, per-document saving, page ranges cut per document, prompt recall off), `convert`'s batch and exit codes - marker replaced, never loaded |
| `tests/test-container.sh` | 31 | container mode against a stub runtime: which files a folder means, read-only inputs at their own paths, the model volume, podman's uid mapping, the bench report written on the host, the input tree kept, the runtime's own exit codes |
| `tests/test-one-output-per-input.sh` | 60 | every input of a run gets its own output or fails: markitdown inputs sharing a stem; local marker (stubs in `tests/stubs-marker`) with two `a.pdf`, a stale output folder, `--skip_existing`, the argv and versions recorded and redacted, nested files in a folder; an output folder inside the input folder, run again (markitdown, marker, auto); inputs added and removed, a leftover `a_2`/`a_3`, `report_docx.html` next to `report.docx`, a URL with credentials; auto's missing-input code |
| `tests/test-k8s-storage.sh` | 22 | the volume claims `service deploy --target k8s` renders: access mode and storage class from the environment or `.env` |
| `tests/test_job_timeout.py` | 18 | each job's timeout sized from its pages (a 166-page scan no longer killed at an hour), and the worker's warm-up |
| `tests/test_convert_bytes.py` | 9 | marker at protocol's port: bytes in, files out, the sidecar kept out, no path from the name |
| `tests/test_markitdown_tasks.py` | 10 | markitdown at protocol's port, and `port.py`: `convert_local` only, plugins off, no options |
| `tests/test_api.py` | 7 | what the API refuses before anything is queued (a folder, a missing file), and its docs behind the token - needs fastapi and rq (`requirements-ci.txt`) |
| `tests/test_worker_guard.py` | 18 | the worker runs only `tasks.convert_document`: hostile jobs refused - another function, a pickled job, callbacks (also one planted in the started registry, which RQ's cleanup reaches), webhooks, marker classes in the options, paths outside the roots - against a real Redis (`PROTOCOL_DROID_TEST_REDIS_URL`) |
| `tests/test_k8s_image.py` | 10 | `service deploy --target k8s` pins the image to its build ID, so a rebuild rolls out |
| `tests/test_engine.py` | 9 | the folder scanner, output naming and exit-code severity, pinned to v1.0.0's bash answers |

`tests/test-exit-codes.sh` is the one that would have caught this round's bugs:
run against the previous commit it fails 22 of its checks. Every case in it is
a way of asking the same question — does this run report what actually
happened. A missing input, an empty folder, a failed conversion and a
successful one all used to be `0`.

The page-range tests exist because of a specific incident, and one of them is
named after it: a range the worker accepted and then died on. Validation now
rejects it up front, with two separate caps — on the **total pages** a range
can ask for and on the **highest index** it may name — so neither
`1-999999999` nor a thousand small pieces adding up to the same thing gets
through. Both caps are environment variables rather than constants:

| Variable | Default | Limits |
| --- | ---: | --- |
| `PAGE_RANGE_MAX_PAGES` | `2000` | how many pages one range may ask for in total |
| `PAGE_RANGE_MAX_INDEX` | `100000` | the highest page number it may name |

Two of the tests are about not being clever: that validation **normalises and
leaves the rest alone**, and that it does **not mutate the caller's dict**. And
one is about Unicode digits, which `str.isdigit()` accepts and `int()` then
refuses — so the pattern is an explicit `[0-9]+` match rather than a character
class that means more than it looks like.

The output-folder tests are all about the same question asked different ways:
two inputs with the same basename in different folders, the same stem with
different extensions, a symlinked file, a symlinked root on either side, and a
path outside the root or with no root at all.

## CI

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs on every push to
`main`, every pull request, and on demand:

- `python3 -m compileall` over every module — a syntax check, which is as far as
  a runner can go without the model weights.
- `shellcheck -S warning` over the `protocol-droid.sh` launcher and the shell
  suites. The severity floor is pinned rather than left to the default: SC2002 is
  off by default in shellcheck 0.11+ and on in older releases, so without `-S`
  the runner and the workstation disagree about what passes.
- `python3 -m unittest discover -s tests -v`.
- `tests/test-exit-codes.sh`, `tests/test-k8s-storage.sh`, `tests/test-container.sh`.
- a second job, `markitdown-image`: builds `Dockerfile.markitdown` and runs
  `tests/markitdown_image_check.py` in it with the network off - one generated
  DOCX, EPUB, PPTX, XLSX, HTML and CSV through the port protocol calls.

`marker` is not installed in CI: it needs several GB of model weights, and a
conversion is only meaningfully checked by converting something - so the
suite covers the logic that decides *what* to convert rather than marker's
conversion. markitdown is the exception because its image is small: built
without the audio extra, it needs no ffmpeg, and converting one document of
each format takes seconds. A markitdown bump that breaks a reader fails that
job, naming the format.

## License

[MIT](LICENSE) © 2026 malahmen. The
converters it drives are licensed separately:
[marker](https://github.com/datalab-to/marker) by Datalab and
[markitdown](https://github.com/microsoft/markitdown) by Microsoft — see their
repositories for terms.
