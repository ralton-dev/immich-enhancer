# The homelab deployment contract, as it applies to this repo

This job runs on a small self-hosted Kubernetes cluster (k3s, arm64 and amd64 nodes). You
will not have access to that cluster from here, and you do not need it. Everything below is
a property of *this* repo. Deployment is GitOps: a separate repo, `homelab-k8s`, holds the
manifests and Argo CD reconciles them. Nothing here deploys itself.

Unlike most apps in the cluster, `immich-enhancer` is a **CronJob** with no port, no
database and no inbound traffic. Every part of the contract that applies to it is met, and
each section below names the file that meets it.

## 1. Image and registry: met

- Published to `ghcr.io/ralton-dev/immich-enhancer`, for `linux/amd64` **and**
  `linux/arm64`. Each platform builds on its own native runner and pushes by digest, and a
  merge job assembles the manifest list (`.github/workflows/ci.yml`, jobs `image` and
  `manifest`).
- Tags are `sha-<full 40-character commit sha>` and `latest` (`ci.yml`, the
  `metadata-action` step). Tags are immutable: never re-push an existing one. A deployment
  is a hand-written tag bump in the manifest repo; a mutated tag would mean the running
  image no longer matches the manifest, and nothing would say so.

## 2. CI runners: no runner-group step

CI uses GitHub-hosted runners only: `ubuntu-latest`, and `ubuntu-24.04-arm` for arm64
(free for public repos). Nothing selects the in-cluster `homelab-arm64` runner, so the repo
does **not** need adding to the `homelab` runner group. If that ever changes, add the repo
to the group first, and select the label only on push to `main`, never on `pull_request`.

## 3. Health endpoints: do not apply

A batch job has no `/healthz` or `/readyz`. What stands in for them:

- **The exit code** (`immich_enhancer/run.py`, `_finish`). It is 1 on a systemic error:
  configuration, ping, the API key, the tag upsert, reading the queue, a pipeline that cannot
  be imported, or an unexpected bug. It is also 1 when assets were attempted and none
  succeeded. Otherwise it is 0, which covers an empty queue, a run of nothing but skips, and
  a run in which one photo failed but another succeeded. The cluster's stock `KubeJobFailed`
  alert pages on a 1, so a failed photo pages only when nothing else in its run succeeded.
- **`APP_VERSION`.** It is set by the manifest equal to the image tag. The run logs it at
  start, and its single JSON summary line carries it as `app_version`.
- **The Job deadline** (`activeDeadlineSeconds`, set in the manifest). Before it,
  `ENHANCER_TIME_BUDGET_S` stops new work and `ENHANCER_MAX_PER_RUN` caps the attempts.

## 4. It runs locked down: met

The pod runs non-root as uid/gid 1000, with seccomp `RuntimeDefault`, every capability
dropped, a **read-only root filesystem** and an `emptyDir` at `/tmp`. Its namespace
*enforces* Pod Security Standard `restricted`, which is stricter than the cluster's usual
`baseline`.

The image already meets that (`Dockerfile`):

- final stage `USER 1000:1000`, with no `USER root` and no startup chown or chmod;
- every file root-owned and world-readable;
- `HOME=/tmp`;
- a final build step that runs as uid 1000, with no network, and runs both upscalers through
  the job's own pipeline.

The code keeps to it:

- It writes only under `ENHANCER_WORK_DIR` (default `/tmp/enhancer`). That is where the
  download and the two JPEGs go (`run.py`, `_scratch`); each JPEG is written to a `.part`
  file first and renamed (`pipeline.py`, `_write`). Each asset's files are deleted when it is
  done (`run.py`, `_clean`). Anything outside `/tmp` would fail on the read-only filesystem.
- It loads weights only from `ENHANCER_MODEL_DIR`, and a missing one is an error that names
  the file (`pipeline.py`, `_load_spandrel`). Nothing in the job tries a download, which
  would hang: the pod has no internet.

## 5. Configuration is environment variables only: met

All config arrives as environment variables, from a ConfigMap and a Secret. There is no way
to ship a config file. The variables, their defaults, and which is secret
(`IMMICH_API_KEY` only) are in the [README](../README.md#configuration). That list is what
the ConfigMap is built from.

`immich_enhancer/config.py` reads exactly those names. Before the job touches Immich, it
raises one error naming every missing or invalid variable; the required ones are
`IMMICH_API_KEY` and `APP_VERSION`. A boolean is on only for the exact string `"true"`. An
unknown `ENHANCER_*` variable is logged as a warning, so a typo in the ConfigMap shows up
rather than silently keeping a default.

Environment injected wholesale does not roll pods when it changes. For a CronJob that does
not matter: each run is a fresh pod that reads the config then. The manifest still carries a
`config-rev` annotation, by convention.

## 8. Networking is deny-by-default: met, enumerated in the README

The namespace denies all ingress and egress until each path is allowed by hand. This job's
complete list is in the [README](../README.md#network):

- outbound to `immich-server.immich.svc.cluster.local:2283` (plain HTTP, in-cluster);
- cluster DNS;
- nothing inbound at all.

There is no internet access at runtime. Any outbound call not on that list is dropped
**silently**: no error, just a hang until the deadline. Do not add one: no telemetry, no
update check, and no model download. The weights are in the image.

## 9. Tracing: not emitted

The job emits no OpenTelemetry. Its observability is its stdout, which Loki collects: one
log line per enhanced photo (`enhanced scale=… in=… out=… seconds=… peak_rss_mib=…`) and one
JSON summary line per run. The Job's exit status completes the picture.

## 11. How deployment actually happens: met

1. Push to `main`. CI builds and pushes `sha-<full-sha>`, and the run's summary names the
   tag.
2. **A person edits the manifest repo** to point the CronJob at the new tag (and
   `APP_VERSION` with it), and commits.
3. Argo CD picks it up within a few minutes.

There is no automatic promotion. The running version can be behind `main`, which is normal.
The build must succeed and the tag must exist before the bump commit.

Sections 6 (Postgres), 7 (migrations) and 10 (reverse proxies) of the general contract do
not apply: this job has no database and receives no traffic.

## Checklist

- [x] Multi-arch `linux/amd64` + `linux/arm64` image
- [x] Tagged `sha-<full-sha>`, immutable, on `ghcr.io/ralton-dev/…`
- [x] No `homelab-arm64`, so no runner-group step
- [x] Exit code per the rule in §3, and `APP_VERSION` in the summary line
- [x] Image runs as uid 1000, tolerates a read-only root and an unwritable `$HOME`
- [x] Code writes only under `/tmp`, and loads weights only from the image, never downloading
- [x] Every setting documented as an env var, with defaults and secret flag
- [x] `config.py` fails loudly on a missing required variable; booleans are exactly `"true"`
- [x] Every inbound and outbound network path enumerated in the README
- [x] No outbound calls beyond Immich: weights baked in, checked offline at build time
