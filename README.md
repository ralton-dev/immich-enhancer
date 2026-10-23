# immich-enhancer

A batch job that upscales old, low-resolution photos in [Immich](https://immich.app) and
restores the faces in them. You tag a photo `enhance` in Immich. On its next run the job
uploads an enhanced copy as a new asset and stacks it on top of the original, so the timeline
shows the better picture with the original underneath. **The original is never edited,
replaced or re-encoded.**

It runs as a Kubernetes CronJob in a homelab cluster. It has no web interface and listens on
no port.

## How it works

1. Find assets tagged `enhance`, through Immich's public API.
2. Skip what it should not touch: anything but JPEG and PNG (in v1), videos, assets already
   in a stack, and assets already tagged `enhanced` or `enhanced-source`.
3. Download the original, rotate it upright using its EXIF orientation, and upscale it:
   - ×4 up to 1 MP, ×2 up to 4 MP, ×1 (faces only) above that;
   - output is capped at `ENHANCER_MAX_OUTPUT_MP`;
   - inference is tiled, so memory stays bounded.
4. Restore faces: [facexlib](https://github.com/xinntao/facexlib) finds and aligns them
   (RetinaFace, ParseNet), and GFPGAN v1.4 restores each one.
5. Write a JPEG that carries the original's EXIF with `Orientation` reset to 1. Upload it with
   the original's `fileCreatedAt` and `fileModifiedAt`, so it lands at the same place in the
   timeline.
6. Stack the new asset on the original as primary, and add it to every album the original is
   in.
7. Move the tags: the original goes from `enhance` to `enhanced-source`, and the new asset
   gets `enhanced`. A failure is tagged `enhance-failed`; an unsupported input is tagged
   `enhance-skipped` and the reason is logged.

Every run ends with one JSON summary line (`processed, succeeded, failed, skipped, seconds`).
The exit code is 0 when the queue was empty or at least one asset succeeded. It is non-zero
only when every attempted asset failed, or a systemic step did (auth, ping, tag upsert).

## Configuration

Configuration comes from environment variables only. A missing required variable stops the
job at startup with an error naming it. Booleans are on only for the exact string `true`.

| Variable | Default | Secret | Meaning |
| --- | --- | --- | --- |
| `IMMICH_URL` | `http://immich-server.immich.svc.cluster.local:2283` | no | Immich base URL. Requests go to `$IMMICH_URL/api/…`. |
| `IMMICH_API_KEY` | required | **yes** | Immich API key, sent as `x-api-key`. Keys are per user, so the job sees only that user's assets. It needs: asset read, view, download, upload; stack create, read; tag create, read, asset; album read and asset-add. |
| `APP_VERSION` | required | no | The image tag this pod runs (`sha-<full commit sha>`), set by the CronJob. It appears in the summary line. |
| `ENHANCER_QUEUE_TAG` | `enhance` | no | Tag that queues a photo. |
| `ENHANCER_DONE_TAG` | `enhanced` | no | Tag on each new, enhanced asset. |
| `ENHANCER_SOURCE_TAG` | `enhanced-source` | no | Tag the original carries once its enhanced copy exists. |
| `ENHANCER_FAILED_TAG` | `enhance-failed` | no | Tag for a photo whose enhancement failed. |
| `ENHANCER_SKIPPED_TAG` | `enhance-skipped` | no | Tag for an input the job does not handle. |
| `ENHANCER_MAX_PER_RUN` | `10` | no | Most assets attempted in one run. |
| `ENHANCER_TIME_BUDGET_S` | `2700` | no | No new asset is started after this many seconds. |
| `ENHANCER_MAX_OUTPUT_MP` | `16` | no | Cap on output size, in megapixels. |
| `ENHANCER_SR_MODEL` | `RealESRGAN_x4plus` | no | Upscaler: `RealESRGAN_x4plus` (quality) or `realesr-general-x4v3` (compact, about 10× faster). Both are in the image. |
| `ENHANCER_FACE_RESTORE` | `true` | no | Run the face pass. |
| `ENHANCER_TILE` | `256` | no | Tile size for upscaling. |
| `ENHANCER_THREADS` | `4` | no | CPU threads for inference (`torch.set_num_threads`). |
| `ENHANCER_MODEL_DIR` | `/opt/models` | no | Where the weights are. facexlib's two are in its `facexlib/` subdirectory. |
| `ENHANCER_WORK_DIR` | `/tmp/enhancer` | no | Scratch space for downloads and outputs. It must be writable. |
| `ENHANCER_JPEG_QUALITY` | `92` | no | Quality of the enhanced JPEG. |
| `ENHANCER_DRY_RUN` | `false` | no | `true` does everything except upload, stack and tag. |
| `ENHANCER_LOG_LEVEL` | `INFO` | no | Python log level. |
| `OMP_NUM_THREADS` | not set | no | Read by PyTorch's OpenMP runtime when it is imported. The image leaves it unset on purpose; the CronJob sets it to match `ENHANCER_THREADS`. |

`IMMICH_URL`, `IMMICH_API_KEY` and `APP_VERSION` are named outright. Every other setting is
`ENHANCER_` plus its field name in `immich_enhancer/config.py`.

The job writes only under `/tmp`: it sets `HOME=/tmp` and uses `ENHANCER_WORK_DIR`. That
means it runs with a read-only root filesystem and a writable `emptyDir` at `/tmp`.

## Network

Inbound: **none.** There is no listening port, no Service and no probe.

Outbound, at runtime:

| Destination | Port | Why |
| --- | --- | --- |
| `immich-server.immich.svc.cluster.local` | TCP 2283 (plain HTTP, in-cluster) | The Immich API: the only destination |
| cluster DNS | UDP/TCP 53 | To resolve the name above |

Nothing else, and **no internet access at all.** Every weight the pipeline loads is baked
into the image. facexlib downloads a missing weight on demand, but here that path is never
reached. The image build proves it: its last step loads all five weights with the network
switched off (`scripts/selfcheck.py`). There is no telemetry, no update check and no font or
CDN fetch.

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
| `GFPGANv1.4.pth` | 348.6 MB | `e2cd4703…1b6be5ad` | [TencentARC/GFPGAN v1.3.0](https://github.com/TencentARC/GFPGAN/releases/tag/v1.3.0) | Apache-2.0, see note |
| `facexlib/detection_Resnet50_Final.pth` | 109.5 MB | `6d1de9c2…b0a5a16d` | [xinntao/facexlib v0.1.0](https://github.com/xinntao/facexlib/releases/tag/v0.1.0) | MIT (RetinaFace, from [biubug6/Pytorch_Retinaface](https://github.com/biubug6/Pytorch_Retinaface), MIT) |
| `facexlib/parsing_parsenet.pth` | 85.3 MB | `3d558d8d…72920de2` | [xinntao/facexlib v0.2.2](https://github.com/xinntao/facexlib/releases/tag/v0.2.2) | **Non-commercial**, see note |

¹ The full hashes are in `scripts/fetch-weights.sh`. They were recorded on 2026-10-23 and
cross-checked against independent Hugging Face mirrors of the same files.

Licence notes:

- **GFPGAN.** Its LICENSE grants Apache-2.0 "except for the third-party components listed
  below". Those include StyleGAN2-derived code under NVIDIA's licence and DFDNet-derived code
  under CC BY-NC-SA 4.0. v1.4 is the "clean" architecture, which needs none of StyleGAN2's
  custom CUDA operations.
- **ParseNet.** facexlib is MIT, but its `parsenet.py` says it is "modified from
  [chaofengc/PSFRGAN](https://github.com/chaofengc/PSFRGAN)". PSFR-GAN is licensed CC BY-NC-SA
  4.0, so treat these weights as **non-commercial**. They cannot be dropped:
  facexlib's `FaceRestoreHelper` loads the parsing model even when parsing is off.
- **Loaders.** The code that loads the models is MIT: [spandrel](https://github.com/chaiNNer-org/spandrel)
  and facexlib. The `basicsr`, `realesrgan` and `gfpgan` packages are not used.

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
- **Image size:** not measured yet.

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
scripts/fetch-weights.sh models                # about 615 MB, sha256-verified; models/ is gitignored
uv run ruff check . && uv run ruff format --check .
ENHANCER_MODEL_DIR="$PWD/models" uv run pytest
docker build -t immich-enhancer .              # the last build step loads every weight offline
```

`tests/test_contract.py` is the end-to-end contract. It runs a fake Immich, seeds it with
one tagged photo, runs the job, and checks what the job left behind. It is marked
`xfail(strict=True)` until the run loop exists.

## Licence

The code in this repository is [MIT](LICENSE). The model weights carry their own licences,
listed above.
