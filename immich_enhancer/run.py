"""The job: drain the queue tag in Immich, one asset at a time.

    python -m immich_enhancer.run

Order: settings, ping, server version, the key's permissions, the five tags,
the queue. Then, for each queued asset: the skip rules, download, enhance,
upload, stack, albums, tags, and a check that the copy landed where the
original is in the timeline. One JSON summary line goes to stdout at the end.

Exit code (plan decision 12): 0 when the queue was empty or at least one asset
succeeded. 1 when every attempted asset failed, or a systemic step did:
configuration, ping, the API key, the tag upsert or reading the queue. One bad
photo is tagged `enhance-failed` and never fails the run.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import mimetypes
import os
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from time import monotonic, sleep
from typing import Any

from .config import ConfigError, Settings
from .immich import KEY_PERMISSIONS, ImmichAuthError, ImmichClient, ImmichError

log = logging.getLogger(__name__)

# (src, dst, settings) -> a result with scale, faces, in_size, out_size, seconds.
Enhance = Callable[[Path, Path, Settings], Any]

# Decision 10: JPEG and PNG only in v1. The value is the scratch file's suffix.
SUPPORTED_TYPES = {"image/jpeg": ".jpg", "image/png": ".png"}

# The permissions each mode uses, named as GET /api-keys/me names them.
NEEDED_PERMISSIONS = (
    "asset.read",
    "asset.download",
    "asset.upload",
    "stack.create",
    "album.read",
    "albumAsset.create",
    "tag.create",
    "tag.asset",
)
DRY_RUN_PERMISSIONS = ("asset.read", "asset.download", "tag.create")

# Immich stores an upload with localDateTime = fileCreatedAt, then rewrites
# both (and fileModifiedAt) when its metadata job reads the file. The timeline
# check waits up to TIMELINE_POLLS x TIMELINE_POLL_S for that read.
TIMELINE_POLLS = 10
TIMELINE_POLL_S = 2.0

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


class Systemic(Exception):
    """Not about one asset: stop the run and exit 1, leaving the rest queued."""


class Skip(Exception):
    """The pipeline cannot or need not improve this photo: a skip, not a
    failure. Either the file is not one it handles (decision 10: a WebP saved
    as .jpg reaches it labelled image/jpeg), or its output would be a plain
    re-encode of the original."""


def _is_unsupported(exc: BaseException) -> bool:
    # pipeline.UnsupportedImageError, matched by name: importing the pipeline
    # here would import torch into every run, and into the tests.
    return any(cls.__name__ == "UnsupportedImageError" for cls in type(exc).__mro__)


@dataclass
class Counts:
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    queued: int = 0

    @property
    def attempted(self) -> int:
        return self.succeeded + self.failed

    @property
    def processed(self) -> int:
        return self.attempted + self.skipped


@dataclass(frozen=True)
class Tags:
    queue: str
    done: str
    source: str
    failed: str
    skipped: str


def main(enhance: Enhance | None = None) -> int:
    """Run once and return the exit code. `enhance` defaults to the real
    pipeline, imported only when the first photo needs it."""
    started = monotonic()
    owns_logging = _configure_logging()
    counts = Counts()
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        log.error("%s", exc)
        return _finish(
            counts,
            started,
            app_version=os.environ.get("APP_VERSION"),
            dry_run=False,
            error=str(exc),
        )
    if owns_logging:
        _apply_level(settings.log_level)
    log.info(
        "immich-enhancer %s%s",
        settings.app_version,
        " (dry run: nothing in Immich changes)" if settings.dry_run else "",
    )

    error = stopped = None
    try:
        with ImmichClient(settings.immich_url, settings.immich_api_key) as client:
            stopped = Run(client, settings, enhance, counts, started).drain()
    except (ImmichError, Systemic) as exc:
        error = str(exc)
        log.error("run stopped: %s", exc)
    except Exception as exc:  # a bug: still one summary line, and a failed Job
        error = f"{type(exc).__name__}: {exc}"
        log.exception("run stopped by an unexpected error")
    return _finish(
        counts,
        started,
        app_version=settings.app_version,
        dry_run=settings.dry_run,
        error=error,
        stopped=stopped,
    )


class Run:
    def __init__(
        self,
        client: ImmichClient,
        settings: Settings,
        enhance: Enhance | None,
        counts: Counts,
        started: float,
    ) -> None:
        self.client = client
        self.settings = settings
        self.enhance = enhance
        self.counts = counts
        self.started = started
        self.labels: dict[str, str] = {}
        self.tags: Tags

    def drain(self) -> str | None:
        """Everything after the settings. Returns why the loop stopped early, if
        it did: "max_per_run" or "time_budget"."""
        client, s = self.client, self.settings
        client.ping()
        client.version()
        self._check_key()
        names = [s.queue_tag, s.done_tag, s.source_tag, s.failed_tag, s.skipped_tag]
        ids = client.upsert_tags(names)
        self.tags = Tags(*(ids[name] for name in names))
        queue = client.search_assets_by_tag(self.tags.queue)
        self.counts.queued = len(queue)
        log.info("%d asset(s) tagged %r", len(queue), s.queue_tag)

        for position, item in enumerate(queue):
            waiting = len(queue) - position
            if self.counts.attempted >= s.max_per_run:
                log.info("max_per_run %d reached: %d asset(s) wait", s.max_per_run, waiting)
                return "max_per_run"
            if monotonic() - self.started >= s.time_budget_s:
                log.info("time budget %d s spent: %d asset(s) wait", s.time_budget_s, waiting)
                return "time_budget"
            self._one(item["id"])
        return None

    def _check_key(self) -> None:
        """Stop before the first write if the key lacks a permission, rather than
        halfway through a photo (an upload left without its stack, say)."""
        try:
            granted = set(self.client.key_permissions())
        except ImmichAuthError:
            raise
        except ImmichError as exc:
            log.warning(
                "could not read the API key's permissions, so a missing one will fail "
                "its own call instead: %s",
                exc,
            )
            return
        if "all" in granted:
            return
        needed = DRY_RUN_PERMISSIONS if self.settings.dry_run else NEEDED_PERMISSIONS
        missing = [p for p in needed if p not in granted]
        if missing:
            raise Systemic(
                f"the API key lacks {', '.join(missing)} (GET /api/api-keys/me). "
                f"Create it with: {KEY_PERMISSIONS}."
            )

    # -- one asset ---------------------------------------------------------------

    def _one(self, asset_id: str) -> None:
        try:
            outcome = self._handle(asset_id)
        except (ImmichAuthError, Systemic):
            raise
        except Exception as exc:
            self.counts.failed += 1
            label = self.labels.get(asset_id, asset_id)
            if isinstance(exc, ImmichError):
                log.error("asset %s failed: %s", label, exc)
            else:
                log.exception("asset %s failed: %s: %s", label, type(exc).__name__, exc)
            self._park_failed(asset_id, label)
        else:
            if outcome == "skipped":
                self.counts.skipped += 1
            else:
                self.counts.succeeded += 1
        finally:
            self._clean(asset_id)

    def _handle(self, asset_id: str) -> str:
        client, s, t = self.client, self.settings, self.tags
        detail = client.asset(asset_id)
        label = f"{asset_id} ({detail.get('originalFileName') or 'no file name'})"
        self.labels[asset_id] = label
        carried = {tag.get("id") for tag in detail.get("tags") or []}

        # Decision 11: our own work is never re-processed, even when re-tagged.
        ours = [
            name
            for name, tag_id in ((s.source_tag, t.source), (s.done_tag, t.done))
            if tag_id in carried
        ]
        if ours:
            log.info(
                "asset %s already carries %r: not re-processing it, removing the stray %r tag",
                label,
                ours[0],
                s.queue_tag,
            )
            if not s.dry_run:
                client.untag_assets(t.queue, [asset_id])
            return "skipped"

        reason = skip_reason(detail)
        if reason:
            return self._skip(asset_id, label, reason)
        try:
            self._enhance(detail, label, self._pipeline(), carried)
        except Skip as exc:
            return self._skip(asset_id, label, str(exc))
        return "succeeded"

    def _skip(self, asset_id: str, label: str, reason: str) -> str:
        log.info("asset %s skipped: %s", label, reason)
        if not self.settings.dry_run:
            self.client.tag_assets(self.tags.skipped, [asset_id])
            self.client.untag_assets(self.tags.queue, [asset_id])
        return "skipped"

    def _enhance(self, detail: dict, label: str, enhance: Enhance, carried: set) -> None:
        client, s, t = self.client, self.settings, self.tags
        asset_id = detail["id"]
        src, dst = self._scratch(asset_id, SUPPORTED_TYPES[mime_type(detail)])
        size = client.download(asset_id, src)
        log.info("asset %s: downloaded %d bytes", label, size)
        try:
            result = enhance(src, dst, s)
        except Exception as exc:
            if _is_unsupported(exc):
                raise Skip(str(exc)) from exc
            raise
        log.info(
            "asset %s enhanced: x%s, %s face(s), %s -> %s in %s s",
            label,
            getattr(result, "scale", "?"),
            getattr(result, "faces", "?"),
            _size(getattr(result, "in_size", None)),
            _size(getattr(result, "out_size", None)),
            getattr(result, "seconds", "?"),
        )
        if getattr(result, "scale", None) == 1 and getattr(result, "faces", None) == 0:
            # No upscale (over 4 MP, or held back by max_output_mp) and no face
            # restored (none found, or the face pass is off): the output is a
            # re-encode of the original, and stacked on top it would put a
            # slightly worse copy in front of it.
            raise Skip("nothing to enhance: no upscale at this size (x1) and no face restored")
        if s.dry_run:
            log.info("asset %s: dry run, so no upload, stack, album or tag", label)
            return

        uploaded = client.upload(
            dst,
            filename=enhanced_name(detail),
            file_created_at=detail["fileCreatedAt"],
            file_modified_at=detail["fileModifiedAt"],
            is_favorite=bool(detail.get("isFavorite")),
        )
        if uploaded.get("status") == "duplicate":
            new_id = self._duplicate(dst, uploaded["id"], asset_id, label)
        else:
            new_id = uploaded["id"]
            log.info("asset %s: uploaded the enhanced copy as %s", label, new_id)
        client.create_stack([new_id, asset_id])
        self._albums(asset_id, new_id, label)
        client.tag_assets(t.done, [new_id])
        client.tag_assets(t.source, [asset_id])
        client.untag_assets(t.queue, [asset_id])
        for stale in (t.failed, t.skipped):  # left over from an earlier try
            if stale in carried:
                client.untag_assets(stale, [asset_id])
        log.info("asset %s: done, %s is stacked on top of it", label, new_id)
        self._check_timeline(new_id, asset_id)

    def _duplicate(self, path: Path, duplicate_id: str, original_id: str, label: str) -> str:
        """Immich already has these bytes, most likely from a run that died after
        its upload. Finish that asset's stack, albums and tags instead."""
        checksum = file_checksum(path)
        live = {item["id"] for item in self.client.find_by_checksum(checksum)}
        if duplicate_id not in live:
            raise ImmichError(
                "upload",
                f"Immich says the enhanced file duplicates asset {duplicate_id}, but no live "
                f"asset has checksum {checksum}: it is probably in the trash. Restore it or "
                f"empty the trash, then tag the original {self.settings.queue_tag!r} again.",
            )
        if duplicate_id == original_id:
            raise ImmichError("upload", "the enhanced file is byte-identical to the original")
        log.info(
            "asset %s: Immich already has this enhanced file as %s; finishing its stack, "
            "albums and tags",
            label,
            duplicate_id,
        )
        return duplicate_id

    def _albums(self, original_id: str, new_id: str, label: str) -> None:
        for album in self.client.albums_for_asset(original_id):
            name = album.get("albumName") or album.get("id")
            try:
                self.client.add_to_album(album["id"], [new_id])
            except ImmichAuthError:
                raise
            except ImmichError as exc:
                # A shared album Ben may only view, say. The copy is still stacked
                # on its original, so this is a warning, not a failure.
                log.warning(
                    "asset %s: could not add the enhanced copy to album %r: %s", label, name, exc
                )
            else:
                log.info("asset %s: added the enhanced copy to album %r", label, name)

    def _check_timeline(self, new_id: str, original_id: str) -> None:
        """The regression to fear: the copy must sit where its original sits in
        the timeline. Warned about, never failed. Immich rewrites localDateTime
        and fileCreatedAt when it reads the new file, so wait for that first."""
        try:
            new = self.client.asset(new_id)
            for _ in range(TIMELINE_POLLS):
                if not metadata_pending(new):
                    break
                sleep(TIMELINE_POLL_S)
                new = self.client.asset(new_id)
            if metadata_pending(new):
                log.warning(
                    "timeline not checked for %s: Immich had not read its metadata after %g s. "
                    "Compare its localDateTime and fileCreatedAt with %s's by hand.",
                    new_id,
                    TIMELINE_POLLS * TIMELINE_POLL_S,
                    original_id,
                )
                return
            original = self.client.asset(original_id)
        except ImmichError as exc:
            log.warning("timeline not checked for %s: %s", new_id, exc)
            return
        for key in ("localDateTime", "fileCreatedAt"):
            if not same_instant(original.get(key), new.get(key)):
                log.warning(
                    "timeline drift: the enhanced copy %s has %s %s, but its original %s has %s",
                    new_id,
                    key,
                    new.get(key),
                    original_id,
                    original.get(key),
                )

    def _park_failed(self, asset_id: str, label: str) -> None:
        """Decision 2: a failure leaves the queue and is tagged for Ben to look at."""
        if self.settings.dry_run:
            return
        try:
            self.client.tag_assets(self.tags.failed, [asset_id])
            self.client.untag_assets(self.tags.queue, [asset_id])
        except ImmichAuthError:
            raise
        except ImmichError as exc:
            log.error(
                "asset %s: could not tag it %r either, so it stays queued: %s",
                label,
                self.settings.failed_tag,
                exc,
            )

    def _pipeline(self) -> Enhance:
        if self.enhance is None:
            try:
                from .pipeline import enhance_file
            except Exception as exc:  # a broken image, not a bad photo
                raise Systemic(
                    f"the pipeline cannot be imported: {type(exc).__name__}: {exc}"
                ) from exc
            self.enhance = enhance_file
        return self.enhance

    def _scratch(self, asset_id: str, suffix: str) -> tuple[Path, Path]:
        work = Path(self.settings.work_dir)
        work.mkdir(parents=True, exist_ok=True)
        stem = _safe(asset_id)
        return work / f"{stem}{suffix}", work / f"{stem}.enhanced.jpg"

    def _clean(self, asset_id: str) -> None:
        work = Path(self.settings.work_dir)
        if work.is_dir():
            for path in work.glob(f"{_safe(asset_id)}.*"):
                path.unlink(missing_ok=True)


# -- rules and helpers --------------------------------------------------------------


def skip_reason(detail: dict) -> str | None:
    """Why an asset is not enhanced (decision 10, and the stack rule of 11), or None."""
    kind = detail.get("type")
    if kind != "IMAGE":
        return f"it is {kind or 'of unknown type'}, and only images are enhanced"
    mime = mime_type(detail)
    if mime not in SUPPORTED_TYPES:
        return f"{mime or 'an unknown file type'} is not supported in v1, only JPEG and PNG"
    stack = detail.get("stack")
    if stack:
        return f"it is already in stack {stack.get('id')}"
    if detail.get("isEdited"):
        return (
            "it was edited in Immich. The download is the unedited original, so an "
            "enhanced copy on top would undo the edit"
        )
    return None


def mime_type(detail: dict) -> str | None:
    return (
        detail.get("originalMimeType")
        or mimetypes.guess_type(detail.get("originalFileName") or "")[0]
    )


def enhanced_name(detail: dict) -> str:
    """`IMG_0001.JPG` becomes `IMG_0001-enhanced.jpg`: the pipeline writes JPEG."""
    stem = Path(detail.get("originalFileName") or "").stem or detail["id"]
    return f"{stem}-enhanced.jpg"


def file_checksum(path: Path) -> str:
    """Immich's checksum: base64 of the file's SHA-1."""
    digest = hashlib.sha1()
    with open(path, "rb") as data:
        for chunk in iter(lambda: data.read(1 << 20), b""):
            digest.update(chunk)
    return base64.b64encode(digest.digest()).decode()


def metadata_pending(asset: dict) -> bool:
    """Immich has not read the file yet: until it does, its exif row holds only
    the file size, and dateTimeOriginal is set by every read."""
    exif = asset.get("exifInfo")
    return isinstance(exif, dict) and exif.get("dateTimeOriginal") is None


def same_instant(a: object, b: object) -> bool:
    if a == b:
        return True
    try:
        return datetime.fromisoformat(str(a)) == datetime.fromisoformat(str(b))
    except ValueError:
        return False


def _safe(asset_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", asset_id)


def _size(size: object) -> str:
    if isinstance(size, tuple | list) and len(size) == 2:
        return f"{size[0]}x{size[1]}"
    return str(size)


def _finish(
    counts: Counts,
    started: float,
    *,
    app_version: str | None,
    dry_run: bool,
    error: str | None = None,
    stopped: str | None = None,
) -> int:
    exit_code = 1 if error or (counts.attempted and not counts.succeeded) else 0
    summary: dict[str, object] = {
        "event": "summary",
        "app_version": app_version,
        "processed": counts.processed,
        "succeeded": counts.succeeded,
        "failed": counts.failed,
        "skipped": counts.skipped,
        "queued": counts.queued,
        "seconds": round(monotonic() - started, 1),
        "dry_run": dry_run,
        "stopped": stopped,
        "exit_code": exit_code,
    }
    if error:
        summary["error"] = error
    print(json.dumps(summary), flush=True)
    return exit_code


def _configure_logging() -> bool:
    """Log to stdout, where the summary goes, so Loki keeps their order. Leave
    logging alone when something (pytest, a caller) set it up already."""
    if logging.getLogger().handlers:
        return False
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, format=LOG_FORMAT)
    return True


def _apply_level(level: str) -> None:
    logging.getLogger().setLevel(level)
    if level != "DEBUG":
        # httpx logs every request at INFO.
        for name in ("httpx", "httpcore"):
            logging.getLogger(name).setLevel(logging.WARNING)


if __name__ == "__main__":
    sys.exit(main())
