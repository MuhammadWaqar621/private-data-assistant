"""
A small internal client for Vercel Blob, replacing the local filesystem
STORAGE_DIR this project used to write uploaded SQLite database files to.

Why this exists: a Vercel serverless Python function has no persistent or
even reliably-shared writable disk between invocations (the only writable
directory, /tmp, is wiped between cold starts and is not shared across
concurrent instances) - so an uploaded .sqlite file cannot live on "the"
filesystem the way it did under docker-compose. Vercel Blob is Vercel's own
object-storage product, so using it (instead of reaching for S3/R2/whatever
some other cloud) keeps the whole deployment on Vercel's own products, per
this project's Vercel-only constraint.

There is no official Python SDK for Vercel Blob (only the JS/TS
`@vercel/blob` package), so this wraps its REST API directly with `httpx`,
matching the same {user_id}/{connection_id}/database.sqlite pathname
convention app/api/connections.py already used for the local filesystem
path - see that module's `_sqlite_dir()`/`SQLITE_FILENAME`.

Configuration: `BLOB_READ_WRITE_TOKEN` - the env var name Vercel itself
assigns when you provision a Blob store and connect it to a project (Vercel
sets this automatically for a linked project; for local development or a
non-Vercel host, copy it from the Vercel dashboard's Storage tab). Read
directly from os.environ, same reasoning as the rest of app/engine/ (see
app/engine/__init__.py's isolation contract) - and notably, this token
works from ANYWHERE (it is a plain bearer credential against a public REST
API), not just from code actually running on Vercel, so local development
against a real Blob store needs no tunneling or emulation.

API shape (see Vercel's Blob REST API docs):
  - upload:  PUT  https://blob.vercel-storage.com/<pathname>
             headers: Authorization: Bearer <token>, x-api-version: 7
             body: raw bytes
             -> 200 JSON: {"url": "...", "pathname": "...", ...}
  - delete:  POST https://blob.vercel-storage.com/delete
             headers: Authorization: Bearer <token>, x-api-version: 7
             body: {"urls": ["<the exact url upload returned>"]}
  - download: a plain GET of the `url` upload returned (it is a public,
    directly-fetchable CDN URL - no special headers needed).

`x-add-random-suffix: 0` is passed on upload so the blob's pathname is
exactly what we asked for (matching the deterministic
{user_id}/{connection_id}/database.sqlite convention, so a re-upload
overwrites rather than accumulating same-named-but-different-URL blobs) -
Vercel Blob otherwise appends a random suffix to every pathname by default
to avoid collisions.
"""

import os
import tempfile
from typing import Optional

import httpx

BLOB_API_BASE = "https://blob.vercel-storage.com"
BLOB_API_VERSION = "7"

_REQUEST_TIMEOUT = 60.0


class BlobStorageError(RuntimeError):
    """Raised for anything that stops an upload/download/delete from
    completing - a missing token, or the Blob API itself returning an
    error. Never a raw httpx/network traceback reaches a caller."""


def _token() -> str:
    token = (os.getenv("BLOB_READ_WRITE_TOKEN") or "").strip()
    if not token:
        raise BlobStorageError(
            "BLOB_READ_WRITE_TOKEN is not set - Vercel Blob storage is not "
            "configured. Provision a Blob store for this project in the "
            "Vercel dashboard (Storage tab) and connect it, or set the "
            "token manually for local development."
        )
    return token


def _headers(extra: Optional[dict] = None) -> dict:
    headers = {
        "Authorization": f"Bearer {_token()}",
        "x-api-version": BLOB_API_VERSION,
    }
    if extra:
        headers.update(extra)
    return headers


def upload_bytes(
    pathname: str, data: bytes, content_type: str = "application/octet-stream"
) -> str:
    """PUT `data` to Vercel Blob at the given pathname (no random suffix -
    the pathname is exactly what's given, so a re-upload of the same
    connection's file overwrites the previous blob). Returns the blob's
    public URL, which is what should be persisted (e.g. in
    `extra_params["storage_url"]`) for later download/delete."""
    url = f"{BLOB_API_BASE}/{pathname.lstrip('/')}"
    headers = _headers(
        {
            "x-content-type": content_type,
            "x-add-random-suffix": "0",
        }
    )
    try:
        response = httpx.put(url, content=data, headers=headers, timeout=_REQUEST_TIMEOUT)
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise BlobStorageError(
            f"Vercel Blob rejected the upload ({exc.response.status_code}): "
            f"{exc.response.text}"
        ) from exc
    except httpx.HTTPError as exc:
        raise BlobStorageError(f"Could not reach Vercel Blob: {exc}") from exc

    body = response.json()
    blob_url = body.get("url")
    if not blob_url:
        raise BlobStorageError("Vercel Blob's response did not include a url.")
    return blob_url


def upload_file(
    pathname: str, local_path: str, content_type: str = "application/octet-stream"
) -> str:
    """Upload the file at `local_path` (e.g. an UploadFile spooled to disk,
    or a temp file this process wrote) to Vercel Blob at `pathname`."""
    with open(local_path, "rb") as handle:
        return upload_bytes(pathname, handle.read(), content_type)


def download_to_path(blob_url: str, local_path: str) -> None:
    """Stream a blob (by the public url upload_bytes/upload_file returned)
    to a local file - the download half of the "download to a local temp
    path, open with sqlite3, re-upload if modified" pattern serverless
    SQLite access needs (see app/engine/db_adapters/sqlite_adapter.py)."""
    try:
        with httpx.stream("GET", blob_url, timeout=_REQUEST_TIMEOUT) as response:
            response.raise_for_status()
            with open(local_path, "wb") as handle:
                for chunk in response.iter_bytes():
                    handle.write(chunk)
    except httpx.HTTPStatusError as exc:
        raise BlobStorageError(
            f"Could not download the database file from Vercel Blob "
            f"({exc.response.status_code})."
        ) from exc
    except httpx.HTTPError as exc:
        raise BlobStorageError(f"Could not reach Vercel Blob: {exc}") from exc


def download_to_temp(blob_url: str, suffix: str = "") -> str:
    """Download a blob to a fresh file under the platform temp dir
    (`tempfile` resolves to /tmp on Vercel's Python runtime, the one
    writable directory a serverless function gets) and return its path.
    Callers are responsible for removing it once done - see
    sqlite_adapter.py, which always cleans up in a `finally`."""
    fd, path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    try:
        download_to_path(blob_url, path)
    except Exception:
        try:
            os.remove(path)
        except OSError:
            pass
        raise
    return path


def delete_blob(blob_url: str) -> None:
    """Best-effort delete of one blob by its public URL. Callers (e.g.
    app/api/connections.py's DELETE /api/connections/{id}) treat a failure
    here the same way they always treated a failed local file removal -
    logged/swallowed, never blocking the rest of the delete."""
    try:
        response = httpx.post(
            f"{BLOB_API_BASE}/delete",
            headers=_headers({"content-type": "application/json"}),
            json={"urls": [blob_url]},
            timeout=_REQUEST_TIMEOUT,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise BlobStorageError(f"Could not delete blob from Vercel Blob: {exc}") from exc
