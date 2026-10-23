# immich-enhancer

A batch job that enlarges old, low-resolution photos in [Immich](https://immich.app). You tag
a photo `enhance` in Immich. On its next run the job uploads two copies of it as new assets,
each four times as wide and as tall (twice, for a photo over 1 MP): one upscaled by a
Real-ESRGAN network, and one pixel-doubled, with every pixel of the original a square block.
It stacks both with the original, the upscale on top, so you can compare all three in Immich
and keep the one you prefer. **The original is never edited, replaced or re-encoded.**

It runs as a Kubernetes CronJob in a homelab cluster. It has no web interface and listens on
no port.

## How it works

One run, in order (`immich_enhancer/run.py`):

1. Read the settings, then check Immich. Ping it, up to 6 tries 5 s apart, so a pod that
   starts before its network policy is in place still gets through. Log the server
   version, with a warning if it is not v3. Read the API key's permissions
   (`GET /api/api-keys/me`); a key that lacks one stops the run before any write and names
   what is missing (see [The API key](#the-api-key)).
2. Create any of the six tags that do not exist yet. Read the whole `enhance` queue, every
   page, before touching any of it.
3. For each queued asset, skip what the job should not touch:
   - an asset already tagged `enhanced`, `pixel-x4` or `enhanced-source`: it is the job's
     own work, so the stray `enhance` tag is removed and nothing else changes;
   - a video, or any file type except JPEG and PNG (in v1);
   - an asset that is already in a stack;
   - a photo edited inside Immich: the download is the unedited original, so copies on top
     of it would undo the edit;
   - a file whose bytes turn out not to be JPEG or PNG, such as a WebP named `.jpg`. An MPO
     (a JPEG with extra frames) is not skipped: its first frame is used;
   - a photo too large to enlarge: over 4 MP, or held to its own size by
     `ENHANCER_MAX_OUTPUT_MP`. Both copies would only re-encode the original at its own
     size. The log says "nothing to enhance: no upscale at this size".

   The last two are known only once the job has downloaded and opened the photo. Apart from
   the first case, a skipped asset moves from `enhance` to `enhance-skipped` and the reason
   is logged.
4. Download the original, turn it upright from its EXIF orientation, and choose the scale:
   - ×4 up to 1 MP, ×2 up to 4 MP, and nothing above that (the last skip above);
   - a scale whose output would exceed `ENHANCER_MAX_OUTPUT_MP` steps down from ×4 to ×2,
     and from ×2 to that skip.
5. Make two copies at that scale, the same size as each other:
   - **the upscale**, by the `ENHANCER_SR_MODEL` network. It runs in tiles, so memory
     depends on the tile size, not the photo;
   - **the pixel copy**, enlarged by nearest neighbour: every pixel of the original becomes
     a square block. Nothing is smoothed or invented, so it shows the original as it was,
     only bigger.
6. Write each as a JPEG at `ENHANCER_JPEG_QUALITY`, carrying the original's EXIF (dates and
   GPS included) with `Orientation` reset to 1, the size tags updated and the embedded
   thumbnail dropped. A transparent PNG is composited on white, a greyscale photo stays
   greyscale, and the colour profile is kept unless it is CMYK.
7. Upload them as `<name>-enhanced.jpg` and `<name>-x4.jpg` (`<name>-x2.jpg` at ×2), each
   with the original's `fileCreatedAt`, `fileModifiedAt` and favourite flag, so they land
   at the same place in the timeline. If Immich already holds a copy's exact bytes, because
   an earlier run died after uploading it, the job uses that asset rather than failing.
8. Stack all three: the upscale on top as primary, then the pixel copy, then the original.
   Add both copies to every album the original is in. An album the job may not add to, such
   as a shared album Ben can only view, gets a warning, not a failure.
9. Move the tags. The original goes from `enhance` to `enhanced-source`, and loses any
   `enhance-failed` or `enhance-skipped` left over from an earlier try. The upscale gets
   `enhanced` and the pixel copy `pixel-x4`. A picture so flat that the network adds
   nothing, such as a blank white page, makes the two copies the same file; Immich keeps
   one asset, and it gets both tags.
10. Check the timeline. For each copy, wait up to 20 s for Immich to read its metadata, then
    compare its `localDateTime` and `fileCreatedAt` with the original's. A difference is
    logged as a WARNING; it never fails the asset.

Choosing between the three is done by hand in Immich: open the stack, keep the one you
prefer and delete the others, or make another one the primary. The job never changes a
stack once it has made it.

An asset whose enhancement fails moves from `enhance` to `enhance-failed`, and the run
carries on with the next one. The loop stops early once `ENHANCER_MAX_PER_RUN` assets have
been attempted (successes and failures; skips do not count) or once `ENHANCER_TIME_BUDGET_S`
has passed. Whatever is left stays queued for the next run.

## Logs and the exit code

Logs go to stdout, where Loki collects them. Each enhanced photo logs one line from the
pipeline:

```
enhanced scale=4 in=120x160 out=480x640 seconds=… load_seconds=… peak_rss_mib=… sr_model=RealESRGAN_x4plus
```

`out` is the size of both copies. `load_seconds` is the share of `seconds` spent loading the
model. It loads on first use and stays loaded for the rest of the run, so the run's first
photo pays most of it.

Every run ends with one JSON summary line, including a run that stops at its settings:

```json
{"event": "summary", "app_version": "sha-…", "processed": 3, "succeeded": 2, "failed": 0, "skipped": 1, "queued": 3, "seconds": 41.2, "dry_run": false, "stopped": null, "exit_code": 0}
```

- `processed` is `succeeded + failed + skipped`.
- `queued` is how many assets carried `enhance` when the run started.
- `stopped` is `"max_per_run"`, `"time_budget"` or `null`.
- `error` is added when the run stopped on one.

The exit code is **1** in two cases:

- a systemic error: configuration, ping, the API key, the tag upsert, reading the queue, a
  pipeline that cannot be imported, or an unexpected bug;
- assets were attempted and none succeeded.

Every other run exits **0**: an empty queue, a run of nothing but skips, and a run in which
one photo failed but another succeeded. A failed photo fails the Job only when nothing else
in the run succeeded.

## Configuration

Configuration comes from environment variables only (`immich_enhancer/config.py`). The run
checks all of them before it contacts Immich. If any is missing or invalid, it stops with one
error that names every bad variable. Text values are trimmed, and a text or number setting
that is set but empty is an error. Booleans are on only for the exact string `true`.

| Variable | Default | Secret | Meaning |
| --- | --- | --- | --- |
| `IMMICH_URL` | `http://immich-server.immich.svc.cluster.local:2283` | no | Immich base URL; a trailing `/` is dropped. Requests go to `$IMMICH_URL/api/…`. |
| `IMMICH_API_KEY` | required | **yes** | Immich API key, sent as `x-api-key`. Keys are per user, so the job sees only that user's assets. Permissions: see [The API key](#the-api-key). |
| `APP_VERSION` | required | no | The image tag this pod runs (`sha-<full commit sha>`), set by the CronJob. It appears in the log and the summary line. |
| `ENHANCER_QUEUE_TAG` | `enhance` | no | Tag that queues a photo. |
| `ENHANCER_DONE_TAG` | `enhanced` | no | Tag on each new upscaled copy, the network's output. |
| `ENHANCER_PIXEL_TAG` | `pixel-x4` | no | Tag on each new pixel-doubled copy. It keeps this name in the ×2 band, where the copy's file name ends `-x2`. |
| `ENHANCER_SOURCE_TAG` | `enhanced-source` | no | Tag the original carries once its enhanced copy exists. |
| `ENHANCER_FAILED_TAG` | `enhance-failed` | no | Tag for a photo whose enhancement failed. |
| `ENHANCER_SKIPPED_TAG` | `enhance-skipped` | no | Tag for a photo the job skipped (see [How it works](#how-it-works)); the reason is in the log. |
| `ENHANCER_MAX_PER_RUN` | `10` | no | Most assets attempted in one run (successes plus failures; skips do not count). At least 1. |
| `ENHANCER_TIME_BUDGET_S` | `2700` | no | No new asset is started this many seconds after the run began. At least 1. |
| `ENHANCER_MAX_OUTPUT_MP` | `16` | no | Cap on the copies' size, in megapixels. A scale that would exceed it steps down from ×4 to ×2; a photo that would exceed it even at ×2 is skipped. At least 1. |
| `ENHANCER_SR_MODEL` | `RealESRGAN_x4plus` | no | Upscaler for the `enhanced` copy: `RealESRGAN_x4plus` (quality) or `realesr-general-x4v3` (compact, about 10× faster). Both are in the image. Any other value is a configuration error. |
| `ENHANCER_TILE` | `256` | no | Tile size for upscaling, in input pixels. Each tile also takes 16 px of context from its neighbours. At least 64. |
| `ENHANCER_THREADS` | `4` | no | CPU threads for PyTorch (`torch.set_num_threads`). At least 1. |
| `ENHANCER_MODEL_DIR` | `/opt/models` | no | Where the weights are: `<ENHANCER_SR_MODEL>.pth` for each upscaler. |
| `ENHANCER_WORK_DIR` | `/tmp/enhancer` | no | Scratch space for downloads and outputs. It is created if missing and must be writable; each asset's files are deleted when it is done. |
| `ENHANCER_JPEG_QUALITY` | `92` | no | Quality of both JPEGs, 1 to 100. |
| `ENHANCER_DRY_RUN` | `false` | no | `true` creates any of the six tags that are missing (`PUT /tags`), then downloads each queued photo and makes both copies. It uploads nothing, creates no stack, adds nothing to an album and changes no asset's tags. A photo it would have uploaded counts as succeeded. |
| `ENHANCER_LOG_LEVEL` | `INFO` | no | `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`, in any case. At `DEBUG`, every HTTP request is logged too. |
| `OMP_NUM_THREADS` | not set | no | Read by PyTorch's OpenMP runtime when it is imported. The image leaves it unset on purpose; the CronJob sets it to match `ENHANCER_THREADS`. |

`IMMICH_URL`, `IMMICH_API_KEY` and `APP_VERSION` are named outright. Every other setting is
`ENHANCER_` plus its field name in `immich_enhancer/config.py`. An `ENHANCER_` variable the
job does not know is logged as a warning and ignored, so a typo in the ConfigMap is visible
rather than silently keeping the default.

The job writes only to `ENHANCER_WORK_DIR`, and the image sets `HOME=/tmp`. So it runs with
a read-only root filesystem and a writable `emptyDir` at `/tmp`.

### The API key

Create the key in Immich (Account Settings → API Keys) with: **asset.read, asset.download,
asset.upload, stack.create, album.read, albumAsset.create, tag.create, tag.asset**, and
nothing else: these are the permissions the run uses. The run's error messages quote this
same list, `KEY_PERMISSIONS` in `immich_enhancer/immich.py`.

Before its first write, the run reads the key's permissions with `GET /api/api-keys/me` and
stops, naming each one that is missing. It requires `asset.read`, `asset.download`,
`asset.upload`, `stack.create`, `album.read`, `albumAsset.create`, `tag.create` and
`tag.asset`. A dry run requires only `asset.read`, `asset.download` and `tag.create`. A key
with `all` passes.

If Immich does not answer that endpoint, the run logs a warning and carries on. A missing
permission then fails its own call with a 403, and the run stops there with an error naming
the permission.

The calls, and the permission each one needs:

| Call | Permission |
| --- | --- |
| `GET /api/server/ping`, `GET /api/server/version` | none |
| `GET /api/api-keys/me` | none: any key may read its own |
| `PUT /api/tags` (create the six tags) | `tag.create` |
| `POST /api/search/metadata` (the queue; by checksum after a duplicate upload) | `asset.read` |
| `GET /api/assets/{id}` | `asset.read` |
| `GET /api/assets/{id}/original` | `asset.download` |
| `POST /api/assets` (upload) | `asset.upload` |
| `POST /api/stacks` | `stack.create` |
| `GET /api/albums?assetId=` | `album.read` |
| `PUT /api/albums/{id}/assets` | `albumAsset.create` |
| `PUT` and `DELETE /api/tags/{id}/assets` | `tag.asset` |

## Network

Inbound: **none.** There is no listening port, no Service and no probe.

Outbound, at runtime:

| Destination | Port | Why |
| --- | --- | --- |
| `immich-server.immich.svc.cluster.local` | TCP 2283 (plain HTTP, in-cluster) | The Immich API: the only destination |
| cluster DNS | UDP/TCP 53 | To resolve the name above |

Nothing else, and **no internet access at all.** Every weight the pipeline loads is baked
into the image, and nothing in the job downloads one: a missing weight is an error that
names the file. The image build proves it: its last step runs both upscalers through the
job's own pipeline with the network switched off (`scripts/selfcheck.py`). There is no
telemetry, no update check and no font or CDN fetch.

Build time only (CI and `docker build`, never the pod): GitHub release assets (weights),
`download.pytorch.org` (CPU wheels), PyPI, `deb.debian.org`, and `ghcr.io`.

## Model weights

`scripts/fetch-weights.sh` downloads these files and checks each one against a pinned
sha256. The Dockerfile runs it in a builder stage and copies the result to `/opt/models`.
**Weights are never committed to git.**

| File (under `/opt/models`) | Size | sha256¹ | Source | Licence |
| --- | --- | --- | --- | --- |
| `RealESRGAN_x4plus.pth` | 67.0 MB | `4fa0d389…f9d682f1` | [xinntao/Real-ESRGAN v0.1.0](https://github.com/xinntao/Real-ESRGAN/releases/tag/v0.1.0) | BSD-3-Clause |
| `realesr-general-x4v3.pth` | 4.9 MB | `8dc7edb9…ece96292` | [xinntao/Real-ESRGAN v0.2.5.0](https://github.com/xinntao/Real-ESRGAN/releases/tag/v0.2.5.0) | BSD-3-Clause |

¹ The full hashes are in `scripts/fetch-weights.sh`. They were recorded on 2026-10-23 and
cross-checked against independent Hugging Face mirrors of the same files.

Both weights are from [Real-ESRGAN](https://github.com/xinntao/Real-ESRGAN)'s own releases,
under its BSD-3-Clause licence. The code that loads them is MIT:
[spandrel](https://github.com/chaiNNer-org/spandrel). The `basicsr` and `realesrgan`
packages are not used.

## The image

`ghcr.io/ralton-dev/immich-enhancer:sha-<full commit sha>`, also tagged `latest`. It is
multi-arch (`linux/amd64` and `linux/arm64`) and built on `python:3.12-slim`.

- PyTorch is a CPU-only build from `download.pytorch.org/whl/cpu`; no CUDA or `nvidia-*`
  package is installed. `pip list` inside the image lists the app's own environment
  (`PIP_PYTHON` points the base image's pip at it).
- It runs as uid/gid `1000:1000`. All files are root-owned and world-readable, and nothing
  chowns at startup.
- `HOME=/tmp`. `OMP_NUM_THREADS` is left for the CronJob to set.
- The default command is `python -m immich_enhancer.run`. It is set as `CMD`, so
  `docker run <image> <command>` still works for inspection.
- **Image size**, measured on 2026-10-23 for `linux/arm64`:
  - `docker image ls` reports **1.11 GB content size**, which is what a node pulls. It
    reports 3.59 GB disk usage, because Docker Desktop's containerd store counts the
    compressed layers and the unpacked copy together.
  - Unpacked, the filesystem is **2.4 GB** (`du -sx /`): 1.4 GB Python environment (torch
    alone is 652 MB), 615 MB of weights, and 204 MB of Mesa and LLVM pulled in by `libgl1`
    for OpenCV.
  - The amd64 image is built in CI, and its size will differ a little.

## How deployment happens

1. Push to `main`. CI tests, builds both architectures on GitHub-hosted runners, and pushes
   `sha-<full commit sha>` and `latest`. The run summary names the tag.
2. A person bumps the CronJob's image tag, and `APP_VERSION` with it, in the
   [homelab-k8s](https://github.com/ralton-dev/homelab-k8s) repo, then commits.
3. Argo CD applies it within a few minutes. The CronJob runs hourly within waking hours.

Nothing here deploys itself, and a merge to `main` is not a release, so the running version
can be behind `main`. The Kubernetes side (CronJob, ConfigMap, sealed API key,
NetworkPolicies) is planned with `/jarvis-homelab-app plan`, run from this repo. That plan
lands in homelab-k8s as `INITIAL_IMPLEMENTATION_PLANS/immich-enhancer.md`. What the cluster
requires of this repo is in [`docs/homelab-deployment.md`](docs/homelab-deployment.md).

## Development

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12.

```sh
uv sync                                        # pinned deps from uv.lock, CPU-only torch
scripts/fetch-weights.sh models                # about 72 MB, sha256-verified; models/ is gitignored
uv run ruff check . && uv run ruff format --check .
ENHANCER_MODEL_DIR="$PWD/models" uv run pytest
docker build -t immich-enhancer .              # the last build step loads every weight offline
```

`tests/test_contract.py` is the end-to-end contract. It runs a fake Immich, seeds it with
one tagged photo, runs the job with the real pipeline, and checks what the job left behind.
It and `tests/test_pipeline.py` load the real models, so fetch the weights first.

`ENHANCER_MODEL_DIR` must point at them, as in the command above. The contract test reads
the setting like the job does, and its default is `/opt/models`.

## Licence

The code in this repository is [MIT](LICENSE). The two model weights are BSD-3-Clause, as
listed above.
