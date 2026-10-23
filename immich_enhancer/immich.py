"""A small Immich API client: the calls the plan's table names, and no others.

Routes and bodies follow Immich's OpenAPI spec at v3.3.5, the deployed version.
Requests go to `<IMMICH_URL>/api` with the key in the `x-api-key` header.

A 401 or 403 raises `ImmichAuthError`, whose message names the call and the
permission it needs. Any other failure, an unreachable server included, raises
`ImmichError` naming the call. Neither message ever contains the key.
"""

from __future__ import annotations

import logging
import mimetypes
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)

# A new pod can start before kube-router has programmed its network policy, and
# its first packets are dropped. Ping is retried to ride that out. Read when a
# client is built, so a test can shorten the interval.
PING_ATTEMPTS = 6
PING_INTERVAL_S = 5.0
PING_TIMEOUT_S = 5.0

# Written against v3, OpenAPI v3.3.5. The flat `POST /search/metadata` fields
# used below (`tagIds`, `checksum`, `page`) are deprecated there and marked for
# removal in v4 (server/src/dtos/search.dto.ts).
SUPPORTED_MAJOR = 3
QUEUE_PAGE_SIZE = 100

# What the README tells Ben to grant when he creates the key.
KEY_PERMISSIONS = (
    "asset read, view, download, upload; stack create, read; "
    "tag create, read, asset; album read and asset-add"
)

TIMEOUT = httpx.Timeout(60.0, connect=10.0)


class ImmichError(RuntimeError):
    """An Immich call failed. The message names the call."""

    def __init__(self, call: str, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.call = call
        self.status = status


class ImmichAuthError(ImmichError):
    """Immich rejected the API key (401), or the key lacks a permission (403)."""


class ImmichClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        sleep: Callable[[float], object] | None = None,
        timeout: httpx.Timeout | float = TIMEOUT,
    ) -> None:
        self._http = httpx.Client(
            base_url=base_url.rstrip("/") + "/api",
            headers={"x-api-key": api_key, "accept": "application/json"},
            timeout=timeout,
        )
        self._sleep = time.sleep if sleep is None else sleep
        self._ping_attempts = PING_ATTEMPTS
        self._ping_interval_s = PING_INTERVAL_S

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> ImmichClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- the calls -------------------------------------------------------------

    def ping(self) -> None:
        """`GET /server/ping` until it answers pong: 6 tries, 5 s apart.

        Transport errors and 5xx are retried; a 4xx is not, since waiting will
        not fix a wrong URL."""
        last = ""
        for attempt in range(1, self._ping_attempts + 1):
            try:
                response = self._http.get("/server/ping", timeout=PING_TIMEOUT_S)
            except httpx.HTTPError as exc:
                last = f"{type(exc).__name__}: {exc}"
            else:
                if 400 <= response.status_code < 500:
                    self._raise_for("ping", "GET", "/server/ping", None, response)
                if response.is_success and _json(response).get("res") == "pong":
                    log.info(
                        "Immich answered ping%s", f" on attempt {attempt}" if attempt > 1 else ""
                    )
                    return
                last = f"HTTP {response.status_code} {_server_message(response)}"
            log.info("ping %d/%d failed: %s", attempt, self._ping_attempts, last)
            if attempt < self._ping_attempts:
                self._sleep(self._ping_interval_s)
        raise ImmichError(
            "ping",
            f"ping (GET /api/server/ping): no pong after {self._ping_attempts} tries "
            f"{self._ping_interval_s:g} s apart; last: {last}",
        )

    def version(self) -> dict[str, Any]:
        """`GET /server/version`, logged; a warning when the major is not 3."""
        data = self._request("version", "GET", "/server/version").json()
        text = "{}.{}.{}".format(data.get("major"), data.get("minor"), data.get("patch"))
        log.info("Immich server v%s", text)
        if data.get("major") != SUPPORTED_MAJOR:
            log.warning(
                "Immich server is v%s, but this job was written against v%d (OpenAPI v3.3.5). "
                "The flat search fields it uses are removed in v4: check the plan's call "
                "table against this version before trusting the run.",
                text,
                SUPPORTED_MAJOR,
            )
        return data

    def key_permissions(self) -> list[str]:
        """The permissions of the key in use. `GET /api-keys/me` is open to any
        key, so this works whatever the key was granted."""
        data = self._request("key_permissions", "GET", "/api-keys/me").json()
        return list(data.get("permissions") or [])

    def upsert_tags(self, names: Iterable[str]) -> dict[str, str]:
        """`PUT /tags`: create the tags that are missing, and map each name to its
        id, existing tags included."""
        names = list(names)
        tags = self._request(
            "upsert_tags", "PUT", "/tags", permission="tag.create", json={"tags": names}
        ).json()
        # Immich answers each leaf tag: `value` is the full path, `name` only its
        # last segment. It drops empty path segments, so the lookup does too.
        by_value = {tag.get("value") or tag.get("name"): tag["id"] for tag in tags}
        found = {name: by_value.get(_tag_path(name)) for name in names}
        missing = [name for name, tag_id in found.items() if tag_id is None]
        if missing:
            raise ImmichError(
                "upsert_tags",
                f"upsert_tags (PUT /api/tags): no id came back for {', '.join(missing)}",
            )
        return {name: tag_id for name, tag_id in found.items() if tag_id is not None}

    def search_assets_by_tag(self, tag_id: str, *, size: int = QUEUE_PAGE_SIZE) -> list[dict]:
        """Every asset carrying `tag_id`, stacked ones included, every page read
        before returning.

        Immich pages by offset, and the run loop removes the tag as it goes:
        paging lazily while untagging would skip assets."""
        items: list[dict] = []
        seen: set[str] = set()
        page = 1
        while True:
            body = {"tagIds": [tag_id], "withStacked": True, "size": size, "page": page}
            data = self._request(
                "search_assets_by_tag",
                "POST",
                "/search/metadata",
                permission="asset.read",
                json=body,
            ).json()
            assets = data.get("assets") or {}
            for item in assets.get("items") or []:
                if item["id"] not in seen:
                    seen.add(item["id"])
                    items.append(item)
            next_page = assets.get("nextPage")
            if not next_page:
                return items
            # nextPage is a string holding the next page number (search.service.ts).
            try:
                following = int(next_page)
            except (TypeError, ValueError):
                following = 0
            if following <= page:
                raise ImmichError(
                    "search_assets_by_tag",
                    f"search_assets_by_tag (POST /api/search/metadata): nextPage {next_page!r} "
                    f"does not follow page {page}",
                )
            page = following

    def find_by_checksum(self, checksum: str) -> list[dict]:
        """Live assets whose file has this checksum, base64 of the SHA-1 as
        Immich gives it. Trashed assets are not searched."""
        data = self._request(
            "find_by_checksum",
            "POST",
            "/search/metadata",
            permission="asset.read",
            json={"checksum": checksum},
        ).json()
        return list((data.get("assets") or {}).get("items") or [])

    def asset(self, asset_id: str) -> dict:
        """`GET /assets/{id}`: type, MIME type, timestamps, favourite flag, stack,
        tags and checksum, among others."""
        return self._request("asset", "GET", f"/assets/{asset_id}", permission="asset.read").json()

    def download(self, asset_id: str, dest: Path) -> int:
        """Stream the original file to `dest`; return its size in bytes."""
        path = f"/assets/{asset_id}/original"
        written = 0
        try:
            with self._http.stream("GET", path) as response:
                if not response.is_success:
                    response.read()
                    self._raise_for("download", "GET", path, "asset.download", response)
                with open(dest, "wb") as out:
                    for chunk in response.iter_bytes():
                        out.write(chunk)
                        written += len(chunk)
        except httpx.HTTPError as exc:
            raise ImmichError(
                "download", f"download (GET /api{path}) failed: {type(exc).__name__}: {exc}"
            ) from exc
        return written

    def upload(
        self,
        path: Path,
        *,
        filename: str,
        file_created_at: str,
        file_modified_at: str,
        is_favorite: bool,
    ) -> dict:
        """`POST /assets`, multipart. Answers `{id, status}`, where status is
        `created`, or `duplicate` with the id of the asset that already has
        these bytes."""
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        form = {
            "filename": filename,
            "fileCreatedAt": file_created_at,
            "fileModifiedAt": file_modified_at,
            "isFavorite": "true" if is_favorite else "false",
        }
        with open(path, "rb") as data:
            response = self._request(
                "upload",
                "POST",
                "/assets",
                permission="asset.upload",
                data=form,
                files={"assetData": (filename, data, content_type)},
            )
        return response.json()

    def create_stack(self, asset_ids: list[str]) -> dict:
        """`POST /stacks`; the first id becomes the primary."""
        return self._request(
            "create_stack",
            "POST",
            "/stacks",
            permission="stack.create",
            json={"assetIds": list(asset_ids)},
        ).json()

    def albums_for_asset(self, asset_id: str) -> list[dict]:
        """`GET /albums?assetId=`: the albums, owned or shared, that hold the asset."""
        return self._request(
            "albums_for_asset",
            "GET",
            "/albums",
            permission="album.read",
            params={"assetId": asset_id},
        ).json()

    def add_to_album(self, album_id: str, asset_ids: list[str]) -> list[dict]:
        path = f"/albums/{album_id}/assets"
        results = self._request(
            "add_to_album",
            "PUT",
            path,
            permission="albumAsset.create",
            json={"ids": list(asset_ids)},
        ).json()
        _check_bulk("add_to_album", f"PUT /api{path}", results, harmless="duplicate")
        return results

    def tag_assets(self, tag_id: str, asset_ids: list[str]) -> list[dict]:
        path = f"/tags/{tag_id}/assets"
        results = self._request(
            "tag_assets", "PUT", path, permission="tag.asset", json={"ids": list(asset_ids)}
        ).json()
        _check_bulk("tag_assets", f"PUT /api{path}", results, harmless="duplicate")
        return results

    def untag_assets(self, tag_id: str, asset_ids: list[str]) -> list[dict]:
        path = f"/tags/{tag_id}/assets"
        results = self._request(
            "untag_assets", "DELETE", path, permission="tag.asset", json={"ids": list(asset_ids)}
        ).json()
        _check_bulk("untag_assets", f"DELETE /api{path}", results, harmless="not_found")
        return results

    # -- plumbing --------------------------------------------------------------

    def _request(
        self, call: str, method: str, path: str, *, permission: str | None = None, **kwargs: Any
    ) -> httpx.Response:
        try:
            response = self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise ImmichError(
                call, f"{call} ({method} /api{path}) failed: {type(exc).__name__}: {exc}"
            ) from exc
        if not response.is_success:
            self._raise_for(call, method, path, permission, response)
        return response

    @staticmethod
    def _raise_for(
        call: str, method: str, path: str, permission: str | None, response: httpx.Response
    ) -> None:
        status = response.status_code
        where = f"{call} ({method} /api{path}): HTTP {status} {_server_message(response)}"
        if status == 401:
            raise ImmichAuthError(
                call,
                f"{where}. Immich rejected the API key: it is wrong, revoked or expired. "
                f"Create one with: {KEY_PERMISSIONS}.",
                status,
            )
        if status == 403:
            needs = f"the {permission!r} permission" if permission else "a permission"
            raise ImmichAuthError(
                call,
                f"{where}. The API key lacks {needs}. Create it with: {KEY_PERMISSIONS}.",
                status,
            )
        raise ImmichError(call, where, status)


def _tag_path(name: str) -> str:
    return "/".join(part for part in name.split("/") if part)


def _check_bulk(call: str, where: str, results: list[dict], *, harmless: str) -> None:
    """A bulk call answers 200 with a result per id. `harmless` is the error that
    means the asset is already in the wanted state; any other is a failure."""
    refused = [r for r in results if not r.get("success") and r.get("error") != harmless]
    if refused:
        detail = ", ".join(
            f"{r.get('id')}: {r.get('error')}"
            + (f" ({r['errorMessage']})" if r.get("errorMessage") else "")
            for r in refused
        )
        raise ImmichError(call, f"{call} ({where}): Immich refused {detail}")


def _json(response: httpx.Response) -> dict:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _server_message(response: httpx.Response) -> str:
    """Immich's own error message, which names a missing permission."""
    message = _json(response).get("message")
    if isinstance(message, list):
        message = "; ".join(str(m) for m in message)
    if message:
        return str(message)
    return (response.text or response.reason_phrase or "").strip()[:200]
