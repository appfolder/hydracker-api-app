#!/usr/bin/env python3
"""
Hydracker API App

Portable GUI/CLI client for Hydracker API workflows. It never reads the
Hydracker database; all operations go through the public API.
"""

from __future__ import annotations

import argparse
import io
import json
import mimetypes
import os
import platform
import re
import secrets
import shlex
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, replace as dataclasses_replace
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urlencode, urlparse
from urllib.request import Request, urlopen


APP_NAME = "HydrackerApiApp"
APP_VERSION = "0.2.0"
DEFAULT_BASE_URL = "https://hydracker.com/api/v1"
DEFAULT_USER_AGENT = f"{APP_NAME}/{APP_VERSION} (Hydracker ops client)"
DEFAULT_ONEFICHIER_BASE_URL = "https://api.1fichier.com/v1"
ONEFICHIER_HOST_ID = 5
SETTINGS_PATH = Path.home() / ".config" / "hydracker-api-app" / "settings.json"
Logger = Callable[[str], None]
NZB_PASSWORD_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz123456789"
NZB_PASSWORD_LENGTH = 10


class ApiError(RuntimeError):
    def __init__(self, status: int, body: str, headers: dict[str, str] | None = None):
        super().__init__(f"HTTP {status}: {body[:500]}")
        self.status = status
        self.body = body
        self.headers = headers or {}


class DownloadError(RuntimeError):
    pass


@dataclass
class ApiConfig:
    base_url: str
    token: str
    user_agent: str
    insecure_tls: bool = False


@dataclass
class OneFichierConfig:
    token: str
    base_url: str = DEFAULT_ONEFICHIER_BASE_URL
    user_agent: str = DEFAULT_USER_AGENT
    insecure_tls: bool = False


@dataclass
class NyuuConfig:
    enabled: bool = False
    bin: str = "npx --yes nyuu"
    host: str = ""
    port: int | None = None
    ssl: bool = True
    user: str = ""
    password: str = ""
    groups: str = "alt.binaries.multimedia"
    connections: int = 3
    from_: str = ""
    nzb_password: str = ""


@dataclass
class PackConfig:
    enabled: bool = True
    rar_bin: str = "rar"
    par2_bin: str = "par2"
    volume_size: str = "500m"
    par2_redundancy: int = 10


class HydrackerApiClient:
    def __init__(self, config: ApiConfig, logger: Logger | None = None):
        self.config = config
        self._last_request_at = 0.0
        self.logger = logger

    def get_lien(self, ids: str, streaming: bool = False) -> dict[str, Any]:
        query = "?streaming=1" if streaming else ""
        return self._request("GET", f"/content/liens/{ids}{query}")

    def random_series_missing_nzb_liens(
        self,
        *,
        limit: int = 500,
        title_id: int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if title_id:
            params["title_id"] = title_id
        return self._request("GET", f"/internal/nzb/random-series-missing-liens?{urlencode(params)}")

    def list_title_liens(
        self,
        title_id: int,
        *,
        host: int | None = ONEFICHIER_HOST_ID,
        lien_id: str | int | None = None,
        page: int = 1,
        per_page: int = 100,
        query: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"page": page, "perPage": per_page}
        if host is not None:
            params["host"] = host
        if lien_id:
            params["lien_id"] = lien_id
        if query:
            params["query"] = query
        return self._request("GET", f"/titles/{title_id}/content/liens?{urlencode(params)}")

    def list_all_title_liens(
        self,
        title_id: int,
        *,
        host: int | None = ONEFICHIER_HOST_ID,
        lien_id: str | int | None = None,
        query: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        page = 1
        liens: list[dict[str, Any]] = []
        while True:
            data = self.list_title_liens(title_id, host=host, lien_id=lien_id, page=page, per_page=100, query=query)
            batch = extract_items(data, ("liens", "data"))
            if not batch:
                break
            liens.extend(batch)
            if limit and len(liens) >= limit:
                return liens[:limit]
            if len(batch) < 100:
                break
            page += 1
        return liens

    def list_title_nzbs(self, title_id: int, *, page: int = 1, per_page: int = 100, lien_id: str | int | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"page": page, "perPage": per_page}
        if lien_id:
            params["lien_id"] = lien_id
        return self._request("GET", f"/titles/{title_id}/content/nzbs?{urlencode(params)}")

    def list_all_title_nzbs(self, title_id: int, *, lien_id: str | int | None = None) -> list[dict[str, Any]]:
        page = 1
        nzbs: list[dict[str, Any]] = []
        while True:
            data = self.list_title_nzbs(title_id, page=page, per_page=100, lien_id=lien_id)
            batch = extract_items(data, ("nzbs", "data"))
            if not batch:
                break
            nzbs.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return nzbs

    def list_titles(
        self,
        *,
        genre: str | None = None,
        type_: str | None = None,
        page: int = 1,
        per_page: int = 50,
        order: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"page": page, "perPage": per_page}
        if genre:
            params["genre"] = genre
        if type_:
            params["type"] = type_
        if order:
            params["order"] = order
        return self._request("GET", f"/titles?{urlencode(params)}")

    def search(self, query: str) -> dict[str, Any]:
        return self._request("GET", f"/search/{urlencode_path(query)}")

    def get_title(self, title_id: int, *, season_number: int | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if season_number is not None:
            params["seasonNumber"] = season_number
        query = f"?{urlencode(params)}" if params else ""
        return self._request("GET", f"/titles/{title_id}{query}")

    def list_channels(self, *, page: int = 1, per_page: int = 100) -> dict[str, Any]:
        params = {"page": page, "perPage": per_page}
        return self._request("GET", f"/channel?{urlencode(params)}")

    def get_channel_content(
        self,
        channel_id: int,
        *,
        page: int = 1,
        order: str = "last_content_added_at:desc",
        restriction: str = "",
        filters: str = "",
    ) -> dict[str, Any]:
        params = {
            "restriction": restriction,
            "order": order,
            "filters": filters,
            "page": page,
            "paginate": "lengthAware",
            "returnContentOnly": "true",
        }
        return self._request("GET", f"/channel/{channel_id}?{urlencode(params)}")

    def create_nzb(
        self,
        *,
        title_id: int,
        qualite: int,
        langues: list[str],
        lien_id: str | int | None = None,
        nzb_path: str | None = None,
        url: str | None = None,
        subs: list[str] | None = None,
        password: str = "",
        full_saison: bool = False,
        saison: int | None = None,
        episode: int | None = None,
        nfo: str = "",
    ) -> dict[str, Any]:
        fields: list[tuple[str, str]] = [
            ("title_id", str(title_id)),
            ("qualite", str(qualite)),
            ("full_saison", "1" if full_saison else "0"),
        ]
        if lien_id:
            fields.append(("lien_id", str(lien_id)))
        for lang in langues:
            fields.append(("langues[]", lang))
        for sub in subs or []:
            fields.append(("subs[]", sub))
        if password:
            fields.append(("password", password))
        if saison is not None:
            fields.append(("saison", str(saison)))
        if episode is not None:
            fields.append(("episode", str(episode)))
        if nfo:
            fields.append(("nfo", nfo))
        if url:
            fields.append(("url", url))

        files = []
        if nzb_path:
            path = Path(nzb_path)
            files.append(("nzb", path.name, "application/x-nzb", path.read_bytes()))

        if not files and not url:
            raise ValueError("Either nzb_path or url is required.")

        return self._multipart("/nzb", fields, files)

    def _request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        content_type: str | None = None,
    ) -> dict[str, Any]:
        self._throttle()
        url = self.config.base_url.rstrip("/") + path
        self._log(f"Hydracker {method} {path}")
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.config.token}",
            "User-Agent": self.config.user_agent,
        }
        if content_type:
            headers["Content-Type"] = content_type

        req = Request(url, data=body, headers=headers, method=method)
        context = ssl._create_unverified_context() if self.config.insecure_tls else None

        try:
            with urlopen(req, timeout=180, context=context) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                self._log(f"Hydracker <- {resp.status} {path}")
                return decode_json(raw)
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            self._log(f"Hydracker <- {exc.code} {path}")
            raise ApiError(exc.code, raw, dict(exc.headers)) from exc
        except URLError as exc:
            raise RuntimeError(f"Network error: {exc}") from exc

    def _multipart(
        self,
        path: str,
        fields: list[tuple[str, str]],
        files: list[tuple[str, str, str, bytes]],
    ) -> dict[str, Any]:
        boundary = f"----HydrackerBoundary{uuid.uuid4().hex}"
        chunks: list[bytes] = []

        for name, value in fields:
            chunks.extend([
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                str(value).encode(),
                b"\r\n",
            ])

        for name, filename, content_type, data in files:
            content_type = content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
            chunks.extend([
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode(),
                f"Content-Type: {content_type}\r\n\r\n".encode(),
                data,
                b"\r\n",
            ])

        chunks.append(f"--{boundary}--\r\n".encode())
        return self._request(
            "POST",
            path,
            body=b"".join(chunks),
            content_type=f"multipart/form-data; boundary={boundary}",
        )

    def _throttle(self) -> None:
        delta = time.monotonic() - self._last_request_at
        if delta < 1.05:
            time.sleep(1.05 - delta)
        self._last_request_at = time.monotonic()

    def _log(self, message: str) -> None:
        if self.logger:
            self.logger(message)


class OneFichierClient:
    def __init__(self, config: OneFichierConfig, logger: Logger | None = None):
        self.config = config
        self.logger = logger

    def get_download_token(self, url: str, password: str = "") -> dict[str, Any]:
        url = normalize_onefichier_url(url)
        self._log(f"1fichier token request for {url}")
        payload: dict[str, Any] = {"url": url, "pretty": 1}
        if password:
            payload["pass"] = password
        return self._post("/download/get_token.cgi", payload)

    def download(self, url: str, dest_dir: Path, *, password: str = "", filename: str | None = None, expected_size: int | None = None) -> Path:
        token_data = self.get_download_token(url, password=password)
        download_url = find_first_url(token_data)
        if not download_url:
            raise RuntimeError(f"1fichier did not return a downloadable URL: {token_data}")

        dest_dir.mkdir(parents=True, exist_ok=True)
        req = Request(download_url, headers={"User-Agent": self.config.user_agent})
        context = ssl._create_unverified_context() if self.config.insecure_tls else None
        with urlopen(req, timeout=3600, context=context) as resp:
            target_name = filename or filename_from_response(resp.headers.get("Content-Disposition"), download_url)
            target = dest_dir / sanitize_filename(target_name)
            reusable = reusable_existing_file(target, expected_size, response_content_length(resp.headers.get("Content-Length")))
            if reusable:
                self._log(f"1fichier reusing existing file {reusable}")
                return reusable
            target = unique_path(target)
            self._log(f"1fichier downloading to {target}")
            with target.open("wb") as fh:
                total = 0
                last_log = time.monotonic()
                while True:
                    chunk = resp.read(1024 * 1024)
                    if not chunk:
                        break
                    fh.write(chunk)
                    total += len(chunk)
                    if time.monotonic() - last_log >= 2:
                        self._log(f"1fichier downloaded {total // (1024 * 1024)} MiB")
                        last_log = time.monotonic()
        self._log(f"1fichier done {target}")
        return target

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        req = Request(
            self.config.base_url.rstrip("/") + path,
            data=body,
            headers={
                "Authorization": f"Bearer {self.config.token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": self.config.user_agent,
            },
            method="POST",
        )
        context = ssl._create_unverified_context() if self.config.insecure_tls else None
        self._log(f"1fichier POST {path}")
        try:
            with urlopen(req, timeout=120, context=context) as resp:
                self._log(f"1fichier <- {resp.status} {path}")
                return decode_json(resp.read().decode("utf-8", errors="replace"))
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            self._log(f"1fichier <- {exc.code} {path}")
            raise ApiError(exc.code, raw, dict(exc.headers)) from exc
        except URLError as exc:
            raise RuntimeError(f"1fichier network error: {exc}") from exc

    def _log(self, message: str) -> None:
        if self.logger:
            self.logger(message)


class DirectDownloadClient:
    def __init__(self, user_agent: str, insecure_tls: bool = False, logger: Logger | None = None):
        self.user_agent = user_agent
        self.insecure_tls = insecure_tls
        self.logger = logger

    def download(self, url: str, dest_dir: Path, *, filename: str | None = None, expected_size: int | None = None) -> Path:
        dest_dir.mkdir(parents=True, exist_ok=True)
        req = Request(url, headers={"User-Agent": self.user_agent})
        context = ssl._create_unverified_context() if self.insecure_tls else None
        self._log("directDL download started")
        with urlopen(req, timeout=3600, context=context) as resp:
            content_type = (resp.headers.get("Content-Type") or "").lower()
            target_name = filename or filename_from_response(resp.headers.get("Content-Disposition"), url)
            target = dest_dir / sanitize_filename(target_name)
            reusable = reusable_existing_file(target, expected_size, response_content_length(resp.headers.get("Content-Length")))
            if reusable:
                self._log(f"directDL reusing existing file {reusable}")
                return reusable
            first_chunk = resp.read(1024 * 1024)
            if looks_like_html(first_chunk, content_type):
                raise DownloadError(f"directDL returned HTML instead of a file: content-type={content_type or 'unknown'}")
            target = unique_path(target)
            with target.open("wb") as fh:
                fh.write(first_chunk)
                total = len(first_chunk)
                last_log = time.monotonic()
                while True:
                    chunk = resp.read(1024 * 1024)
                    if not chunk:
                        break
                    fh.write(chunk)
                    total += len(chunk)
                    if time.monotonic() - last_log >= 2:
                        self._log(f"directDL downloaded {total // (1024 * 1024)} MiB")
                        last_log = time.monotonic()
        if target.stat().st_size < 1024 * 1024:
            raise DownloadError(f"directDL downloaded suspiciously small file: {target.stat().st_size} bytes")
        self._log(f"directDL done {target}")
        return target

    def _log(self, message: str) -> None:
        if self.logger:
            self.logger(message)


class LinkToNzbWorkflow:
    def __init__(
        self,
        hydra: HydrackerApiClient,
        onefichier: OneFichierClient | None,
        *,
        download_dir: Path,
        poster_command: str = "",
        nyuu: NyuuConfig | None = None,
        nzb_dir: Path | None = None,
        dry_run: bool = False,
        fallback_qualite: int | None = None,
        fallback_langues: list[str] | None = None,
        cleanup_downloads: bool = True,
        pack: PackConfig | None = None,
        logger: Logger | None = None,
    ):
        self.hydra = hydra
        self.onefichier = onefichier
        self.download_dir = download_dir
        self.poster_command = poster_command
        self.nyuu = nyuu or NyuuConfig()
        self.nzb_dir = nzb_dir
        self.dry_run = dry_run
        self.fallback_qualite = fallback_qualite
        self.fallback_langues = fallback_langues or []
        self.cleanup_downloads = cleanup_downloads
        self.pack = pack or PackConfig()
        self.logger = logger

    def sync_title(self, title_ref: str, *, query: str | None = None, limit: int | None = None) -> dict[str, Any]:
        title_id = parse_title_id(title_ref)
        self._log(f"sync-title title_id={title_id}")
        liens = self.hydra.list_all_title_liens(title_id, host=ONEFICHIER_HOST_ID, query=query, limit=limit)
        self._log(f"sync-title found {len(liens)} 1fichier links")
        existing_lien_ids = {
            str(nzb["lien_id"])
            for nzb in self.hydra.list_all_title_nzbs(title_id)
            if isinstance(nzb, dict) and nzb.get("lien_id")
        }
        return self._sync_liens(title_id, liens, existing_lien_ids)

    def sync_lien(self, lien_id: str) -> dict[str, Any]:
        lien_id = str(lien_id).strip()
        if not lien_id:
            raise ValueError("Lien ID is required")
        self._log(f"sync-lien lien_id={lien_id}")
        source = self.hydra.get_lien(lien_id, streaming=False)
        source_lien = source.get("lien") if isinstance(source.get("lien"), dict) else {}
        title_id = parse_optional_int(source_lien.get("title_id"))
        if not title_id:
            raise ValueError(f"Cannot resolve title_id for lien_id={lien_id}")

        existing_nzbs = self.hydra.list_all_title_nzbs(title_id, lien_id=lien_id)
        existing_lien_ids = {
            str(nzb["lien_id"])
            for nzb in existing_nzbs
            if isinstance(nzb, dict) and nzb.get("lien_id")
        }
        if lien_id in existing_lien_ids:
            self._log(f"skip lien_id={lien_id}: NZB already exists")
            return {
                "title_id": title_id,
                "count": 1,
                "results": [{"status": "skipped", "reason": "nzb_already_exists_for_lien_id", "lien_id": lien_id}],
            }

        liens = self.hydra.list_all_title_liens(title_id, host=ONEFICHIER_HOST_ID, lien_id=lien_id, limit=1)
        lien = next((item for item in liens if str(item.get("id") or item.get("lien_id") or "") == lien_id), None)
        if lien is None:
            lien = dict(source_lien)
        lien["_source_response"] = source
        return self._sync_liens(title_id, [lien], existing_lien_ids)

    def sync_category(
        self,
        *,
        genre: str | None = None,
        type_: str | None = None,
        pages: int = 1,
        per_page: int = 50,
        limit_per_title: int | None = None,
    ) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        for page in range(1, pages + 1):
            self._log(f"sync-category page={page} genre={genre or '*'} type={type_ or '*'}")
            titles_data = self.hydra.list_titles(genre=genre, type_=type_, page=page, per_page=per_page)
            titles = extract_items(titles_data, ("titles", "data"))
            if not titles:
                break
            for title in titles:
                title_id = title.get("id") if isinstance(title, dict) else None
                if not title_id:
                    continue
                try:
                    result = self.sync_title(str(title_id), limit=limit_per_title)
                    result["title"] = title
                    results.append(result)
                except Exception as exc:
                    results.append({"title": title, "error": f"{type(exc).__name__}: {exc}"})
        return {"results": results}

    def sync_channel(
        self,
        channel_id: int,
        *,
        start_page: int = 1,
        max_pages: int | None = None,
        limit_per_title: int | None = None,
        keep_results: bool = False,
    ) -> dict[str, Any]:
        page = max(1, start_page)
        processed_pages = 0
        totals = {"titles": 0, "uploaded": 0, "skipped": 0, "failed": 0}
        results: list[dict[str, Any]] = []
        last_page_result: dict[str, Any] | None = None
        while True:
            if max_pages is not None and processed_pages >= max_pages:
                break
            self._log(f"sync-channel channel_id={channel_id} page={page}")
            try:
                channel_data = self.hydra.get_channel_content(channel_id, page=page)
            except Exception as exc:
                self._log(f"sync-channel page={page} failed: {type(exc).__name__}: {exc}")
                page_error = {"page": page, "error": f"{type(exc).__name__}: {exc}"}
                last_page_result = page_error
                if keep_results:
                    results.append(page_error)
                page += 1
                processed_pages += 1
                continue

            titles = extract_pagination_items(channel_data)
            if not titles:
                self._log(f"sync-channel no titles on page={page}; stopping")
                break

            page_result: dict[str, Any] = {"page": page, "titles": len(titles), "results": []}
            for index, title in enumerate(titles, start=1):
                title_id = title.get("id") if isinstance(title, dict) else None
                if not title_id:
                    page_result["results"].append({"status": "skipped", "reason": "missing_title_id", "title": title})
                    totals["skipped"] += 1
                    continue
                totals["titles"] += 1
                self._log(f"sync-channel page={page} title {index}/{len(titles)} title_id={title_id}")
                try:
                    result = self.sync_title(str(title_id), limit=limit_per_title)
                    page_result["results"].append({"title_id": title_id, "result": result})
                    for item in result.get("results", []):
                        status = item.get("status") if isinstance(item, dict) else None
                        if status == "uploaded":
                            totals["uploaded"] += 1
                        elif status == "failed":
                            totals["failed"] += 1
                        elif status == "skipped":
                            totals["skipped"] += 1
                except Exception as exc:
                    self._log(f"sync-channel title_id={title_id} failed: {type(exc).__name__}: {exc}")
                    page_result["results"].append({"title_id": title_id, "error": f"{type(exc).__name__}: {exc}"})
                    totals["failed"] += 1
            last_page_result = page_result
            if keep_results:
                results.append(page_result)

            pagination = channel_data.get("pagination") if isinstance(channel_data, dict) else None
            next_page = parse_optional_int(pagination.get("next_page")) if isinstance(pagination, dict) else None
            processed_pages += 1
            if not next_page:
                break
            page = next_page
        payload: dict[str, Any] = {"channel_id": channel_id, "start_page": start_page, "processed_pages": processed_pages, "totals": totals}
        if keep_results:
            payload["results"] = results
        elif last_page_result is not None:
            payload["last_page"] = last_page_result
        return payload

    def sync_random_series(
        self,
        *,
        titles: int = 1,
        limit: int = 500,
        title_id: int | None = None,
        sleep: float = 0.0,
    ) -> dict[str, Any]:
        titles = max(1, titles)
        totals = {"titles": 0, "uploaded": 0, "skipped": 0, "failed": 0, "empty": 0}
        results: list[dict[str, Any]] = []

        for index in range(1, titles + 1):
            payload = self.hydra.random_series_missing_nzb_liens(limit=limit, title_id=title_id)
            title = payload.get("title") if isinstance(payload, dict) else None
            current_title_id = parse_optional_int(title.get("id") if isinstance(title, dict) else None)
            liens = payload.get("liens") if isinstance(payload, dict) else []
            if not current_title_id or not isinstance(liens, list) or not liens:
                self._log(f"sync-random-series {index}/{titles}: no missing liens")
                totals["empty"] += 1
                results.append({"status": "empty", "response": payload})
                if title_id:
                    break
                if sleep > 0:
                    time.sleep(sleep)
                continue

            title_name = title.get("name") if isinstance(title, dict) else ""
            self._log(
                f"sync-random-series {index}/{titles}: "
                f"title_id={current_title_id} links={len(liens)} title={title_name!r}"
            )
            existing_lien_ids = {
                str(nzb["lien_id"])
                for nzb in self.hydra.list_all_title_nzbs(current_title_id)
                if isinstance(nzb, dict) and nzb.get("lien_id")
            }
            result = self._sync_liens(current_title_id, liens, existing_lien_ids)
            result["title"] = title
            results.append(result)
            totals["titles"] += 1

            for item in result.get("results", []):
                status = item.get("status") if isinstance(item, dict) else None
                if status == "uploaded":
                    totals["uploaded"] += 1
                elif status == "skipped":
                    totals["skipped"] += 1
                elif status == "failed":
                    totals["failed"] += 1

            if title_id:
                break
            if sleep > 0 and index < titles:
                time.sleep(sleep)

        return {"totals": totals, "results": results}

    def _sync_liens(
        self,
        title_id: int,
        liens: list[dict[str, Any]],
        existing_lien_ids: set[str],
    ) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        total = len(liens)
        for lien in liens:
            lien_id = str(lien.get("id") or lien.get("lien_id") or "")
            self._log(f"link {len(results) + 1}/{total} lien_id={lien_id or '?'}")
            if not lien_id:
                results.append({"status": "skipped", "reason": "missing_lien_id", "lien": lien})
                continue
            if lien_id in existing_lien_ids:
                self._log(f"skip lien_id={lien_id}: NZB already exists")
                results.append({"status": "skipped", "reason": "nzb_already_exists_for_lien_id", "lien_id": lien_id})
                continue

            qualite = infer_quality(lien, self.fallback_qualite)
            langues = infer_langues(lien, self.fallback_langues)
            if not qualite or not langues:
                results.append({"status": "skipped", "reason": "missing_quality_or_languages", "lien_id": lien_id, "lien": lien})
                continue

            if self.dry_run:
                self._log(f"dry-run lien_id={lien_id}")
                results.append({"status": "dry_run", "lien_id": lien_id, "title_id": title_id, "qualite": qualite, "langues": langues})
                continue

            try:
                if self.cleanup_downloads:
                    self._cleanup_stale_download_artifacts()
                source = lien.get("_source_response") if isinstance(lien.get("_source_response"), dict) else self.hydra.get_lien(lien_id, streaming=False)
                direct_url = extract_direct_dl_url(source)
                raw_url = extract_raw_url(source)
                expected_size = parse_optional_int(lien.get("taille") or source.get("taille") or (source.get("lien") or {}).get("taille"))
                downloaded: Path | None = None
                if direct_url:
                    self._log(f"download lien_id={lien_id} via directDL")
                    try:
                        downloaded = DirectDownloadClient(
                            self.hydra.config.user_agent,
                            insecure_tls=self.hydra.config.insecure_tls,
                            logger=self.logger,
                        ).download(direct_url, self.download_dir, expected_size=expected_size)
                    except (DownloadError, HTTPError, URLError, TimeoutError) as exc:
                        self._log(f"directDL rejected lien_id={lien_id}: {exc}")
                        if not raw_url:
                            results.append({"status": "failed", "reason": "directdl_invalid_no_raw_url", "lien_id": lien_id, "error": str(exc)})
                            continue
                if downloaded is None and raw_url:
                    self._log(f"download lien_id={lien_id} via 1fichier API raw_url")
                    if not self.onefichier:
                        results.append({"status": "failed", "reason": "missing_1fichier_token", "lien_id": lien_id})
                        continue
                    downloaded = self.onefichier.download(raw_url, self.download_dir, password=str(lien.get("password") or ""), expected_size=expected_size)
                if downloaded is None:
                    results.append({"status": "failed", "reason": "no_directdl_or_raw_url_from_api", "lien_id": lien_id, "source": source})
                    continue
                nzb_password = generate_nzb_password()
                self._log(f"build NZB lien_id={lien_id}")
                nzb_path, cleanup_paths = self._build_nzb(downloaded, lien_id, password=nzb_password)
                self._log(f"upload NZB lien_id={lien_id} path={nzb_path}")
                created = self.hydra.create_nzb(
                    title_id=title_id,
                    qualite=qualite,
                    langues=langues,
                    lien_id=lien_id,
                    nzb_path=str(nzb_path),
                    subs=infer_subs(lien),
                    password=nzb_password,
                    full_saison=bool(lien.get("full_saison")),
                    saison=parse_optional_int(lien.get("saison")),
                    episode=parse_optional_int(lien.get("episode")),
                )
                if self.cleanup_downloads:
                    self._cleanup_paths([downloaded, *cleanup_paths])
                existing_lien_ids.add(lien_id)
                results.append({
                    "status": "uploaded",
                    "lien_id": lien_id,
                    "downloaded": str(downloaded),
                    "nzb_path": str(nzb_path),
                    "created": created,
                })
            except Exception as exc:
                self._log(f"failed lien_id={lien_id}: {type(exc).__name__}: {exc}")
                results.append({
                    "status": "failed",
                    "reason": "exception",
                    "lien_id": lien_id,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                })
        return {"title_id": title_id, "count": len(liens), "results": results}

    def _cleanup_stale_download_artifacts(self) -> None:
        download_root = self.download_dir.resolve()
        pack_root = (self.download_dir / "_usenet_packs").resolve()
        if not download_root.exists():
            return

        paths: list[Path] = []
        for path in self.download_dir.iterdir():
            if path.name == "_usenet_packs":
                continue
            if path.is_file():
                paths.append(path)

        if pack_root.exists() and pack_root.is_dir():
            for path in pack_root.iterdir():
                if path.is_dir():
                    paths.append(path)
                elif path.is_file():
                    paths.append(path)

        if not paths:
            return

        total_bytes = 0
        for path in paths:
            try:
                if path.is_dir():
                    total_bytes += sum(child.stat().st_size for child in path.rglob("*") if child.is_file())
                elif path.is_file():
                    total_bytes += path.stat().st_size
            except OSError:
                pass

        self._log(
            "cleanup stale downloads before next link: "
            f"{len(paths)} item(s), about {total_bytes // (1024 * 1024)} MiB"
        )
        self._cleanup_paths(paths)

    def _cleanup_paths(self, paths: list[Path]) -> None:
        roots = [self.download_dir.resolve()]
        if self.nzb_dir:
            roots.append(self.nzb_dir.resolve())
        for path in paths:
            try:
                target = path.resolve()
                if all(root != target and root not in target.parents for root in roots):
                    self._log(f"cleanup skipped outside managed dirs: {path}")
                    continue
                if target.is_dir():
                    shutil.rmtree(target)
                    self._log(f"cleanup deleted directory {path}")
                else:
                    target.unlink(missing_ok=True)
                    self._log(f"cleanup deleted file {path}")
            except Exception as exc:
                self._log(f"cleanup failed for {path}: {type(exc).__name__}: {exc}")

    def _build_nzb(self, downloaded: Path, lien_id: str, *, password: str = "") -> tuple[Path, list[Path]]:
        payload_path = downloaded
        cleanup_paths: list[Path] = []
        if self.pack.enabled:
            payload_path = self._build_archive_pack(downloaded, lien_id, password=password)
            cleanup_paths.append(payload_path)
        if self.nyuu.enabled:
            output_dir = self.nzb_dir or downloaded.parent
            output_dir.mkdir(parents=True, exist_ok=True)
            nzb_path = unique_path(output_dir / f"{downloaded.stem}.nzb")
            config = dataclasses_replace(self.nyuu, nzb_password=password)
            config_path = write_nyuu_config(config, downloaded, nzb_path, title=downloaded.stem)
            try:
                command = build_nyuu_command(self.nyuu, [payload_path], config_path)
                self._log(f"Nyuu posting {payload_path.name}")
                completed = subprocess.run(command, check=True, capture_output=True, text=True)
                if completed.stdout:
                    self._log(f"Nyuu stdout: {completed.stdout[-2000:]}")
                if completed.stderr:
                    self._log(f"Nyuu stderr: {completed.stderr[-2000:]}")
            except subprocess.CalledProcessError as exc:
                if exc.stdout:
                    self._log(f"Nyuu stdout: {exc.stdout[-4000:]}")
                if exc.stderr:
                    self._log(f"Nyuu stderr: {exc.stderr[-4000:]}")
                raise
            finally:
                Path(config_path).unlink(missing_ok=True)
            if nzb_path.exists():
                return nzb_path, cleanup_paths
            raise RuntimeError(f"Nyuu completed but did not create {nzb_path}")

        if self.poster_command:
            output_dir = self.nzb_dir or downloaded.parent
            output_dir.mkdir(parents=True, exist_ok=True)
            before = set(output_dir.glob("*.nzb"))
            command = self.poster_command.format(
                input=shlex.quote(str(payload_path)),
                output_dir=shlex.quote(str(output_dir)),
                lien_id=shlex.quote(lien_id),
            )
            subprocess.run(command, shell=True, check=True)
            after = set(output_dir.glob("*.nzb"))
            created = sorted(after - before, key=lambda p: p.stat().st_mtime, reverse=True)
            if created:
                return created[0], cleanup_paths

        if self.nzb_dir:
            candidates = sorted(
                self.nzb_dir.glob(f"{downloaded.stem}*.nzb"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if candidates:
                return candidates[0], cleanup_paths

        raise RuntimeError("No NZB was produced. Configure --poster-command or --nzb-dir.")

    def _build_archive_pack(self, downloaded: Path, lien_id: str, *, password: str) -> Path:
        if not password:
            raise RuntimeError("RAR packaging requires an NZB password.")
        pack_root = self.download_dir / "_usenet_packs"
        pack_root.mkdir(parents=True, exist_ok=True)
        pack_dir = unique_path(pack_root / f"{downloaded.stem}-{lien_id}")
        pack_dir.mkdir(parents=True, exist_ok=False)
        archive_base = pack_dir / f"{downloaded.stem}.rar"
        volume_size = self.pack.volume_size.strip() or "500m"
        redundancy = max(1, min(100, int(self.pack.par2_redundancy or 10)))

        rar_command = [
            self.pack.rar_bin,
            "a",
            "-idq",
            "-m0",
            "-ep",
            f"-v{volume_size}",
            f"-hp{password}",
            str(archive_base),
            str(downloaded),
        ]
        self._log(f"RAR packing {downloaded.name} volume={volume_size}")
        subprocess.run(rar_command, check=True, capture_output=True, text=True)

        rar_files = sorted(
            path for path in pack_dir.iterdir()
            if path.is_file() and re.search(r"(\.part\d+\.rar|\.rar|\.r\d{2,3})$", path.name, re.IGNORECASE)
        )
        if not rar_files:
            raise RuntimeError(f"RAR completed but produced no archive files in {pack_dir}")

        par2_base = pack_dir / f"{downloaded.stem}.par2"
        par2_command = build_par2_command(self.pack.par2_bin, par2_base, rar_files, redundancy)
        self._log(f"PAR2 creating recovery={redundancy}% files={len(rar_files)}")
        subprocess.run(par2_command, check=True, capture_output=True, text=True)

        pack_files = sorted(path for path in pack_dir.iterdir() if path.is_file())
        if not any(path.suffix.lower() == ".par2" for path in pack_files):
            raise RuntimeError(f"PAR2 completed but produced no .par2 files in {pack_dir}")
        self._log(f"archive pack ready {pack_dir} files={len(pack_files)}")
        return pack_dir

    def _log(self, message: str) -> None:
        if self.logger:
            self.logger(message)


def config_from_env(args: argparse.Namespace | None = None) -> ApiConfig:
    base_url = getattr(args, "base_url", None) or os.getenv("HYDRACKER_API_BASE_URL", DEFAULT_BASE_URL)
    token = getattr(args, "token", None) or os.getenv("HYDRACKER_API_TOKEN", "")
    user_agent = getattr(args, "user_agent", None) or os.getenv("HYDRACKER_USER_AGENT", DEFAULT_USER_AGENT)
    insecure_tls = bool(getattr(args, "insecure_tls", False) or os.getenv("HYDRACKER_INSECURE_TLS") == "1")
    if not token:
        raise SystemExit("Missing token. Set HYDRACKER_API_TOKEN or pass --token.")
    return ApiConfig(base_url=base_url, token=token, user_agent=user_agent, insecure_tls=insecure_tls)


def onefichier_from_env(args: argparse.Namespace | None = None, required: bool = False) -> OneFichierClient | None:
    token = getattr(args, "onefichier_token", None) or os.getenv("ONEFICHIER_API_TOKEN", "")
    if not token:
        if required:
            raise SystemExit("Missing 1fichier token. Set ONEFICHIER_API_TOKEN or pass --onefichier-token.")
        return None
    base_url = getattr(args, "onefichier_base_url", None) or os.getenv("ONEFICHIER_API_BASE_URL", DEFAULT_ONEFICHIER_BASE_URL)
    user_agent = getattr(args, "user_agent", None) or os.getenv("HYDRACKER_USER_AGENT", DEFAULT_USER_AGENT)
    insecure_tls = bool(getattr(args, "insecure_tls", False) or os.getenv("ONEFICHIER_INSECURE_TLS") == "1")
    return OneFichierClient(OneFichierConfig(token=token, base_url=base_url, user_agent=user_agent, insecure_tls=insecure_tls))


def nyuu_from_env(args: argparse.Namespace | None = None) -> NyuuConfig:
    poster = getattr(args, "poster", None) or os.getenv("HYDRACKER_POSTER", "custom")
    return NyuuConfig(
        enabled=poster == "nyuu",
        bin=getattr(args, "nyuu_bin", None) or os.getenv("NYUU_BIN", "npx --yes nyuu"),
        host=getattr(args, "usenet_host", None) or os.getenv("HYDRA_USENET_HOST", ""),
        port=parse_optional_int(getattr(args, "usenet_port", None) or os.getenv("HYDRA_USENET_PORT", "")),
        ssl=not bool(getattr(args, "usenet_no_ssl", False) or os.getenv("HYDRA_USENET_SSL", "1") == "0"),
        user=getattr(args, "usenet_user", None) or os.getenv("HYDRA_USENET_USER", ""),
        password=getattr(args, "usenet_password", None) or os.getenv("HYDRA_USENET_PASSWORD", ""),
        groups=getattr(args, "usenet_groups", None) or os.getenv("HYDRA_USENET_GROUPS", "alt.binaries.multimedia"),
        connections=parse_optional_int(getattr(args, "usenet_connections", None) or os.getenv("HYDRA_USENET_CONNECTIONS", "")) or 3,
        from_=getattr(args, "usenet_from", None) or os.getenv("HYDRA_USENET_FROM", ""),
    )


def gui_available() -> bool:
    if os.getenv("HYDRACKER_FORCE_CLI") == "1":
        return False
    if sys.platform.startswith("linux") and not (os.getenv("DISPLAY") or os.getenv("WAYLAND_DISPLAY")):
        return False
    try:
        import tkinter  # noqa: F401
    except Exception:
        return False
    return True


def pretty(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def decode_json(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}
    return data if isinstance(data, dict) else {"data": data}


def split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def urlencode_path(value: str) -> str:
    return urlencode({"q": value})[2:]


def parse_title_id(value: str) -> int:
    value = value.strip()
    if value.isdigit():
        return int(value)
    parsed = urlparse(value)
    candidates = re.findall(r"/(?:titles|title|films|series)/(\d+)(?:[/?#]|$)", parsed.path)
    if candidates:
        return int(candidates[-1])
    qs = parse_qs(parsed.query)
    for key in ("title_id", "id"):
        if qs.get(key) and qs[key][0].isdigit():
            return int(qs[key][0])
    numbers = re.findall(r"\d+", value)
    if numbers:
        return int(numbers[-1])
    raise ValueError(f"Cannot parse Hydracker title ID from: {value}")


def parse_optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def extract_items(data: dict[str, Any], preferred_keys: tuple[str, ...]) -> list[dict[str, Any]]:
    candidates: list[Any] = []
    cursor: Any = data
    for key in ("data", *preferred_keys):
        if isinstance(cursor, dict) and key in cursor:
            candidates.append(cursor[key])
    if isinstance(data.get("data"), dict):
        for key in preferred_keys:
            if key in data["data"]:
                candidates.append(data["data"][key])
        if isinstance(data["data"].get("pagination"), dict):
            candidates.append(data["data"]["pagination"].get("data"))
    if isinstance(data.get("pagination"), dict):
        candidates.append(data["pagination"].get("data"))
    if isinstance(data.get("pagination"), list):
        candidates.append(data["pagination"])
    candidates.append(data)

    for candidate in candidates:
        if isinstance(candidate, list):
            return [item for item in candidate if isinstance(item, dict)]
        if isinstance(candidate, dict):
            for key in (*preferred_keys, "items", "results", "pagination"):
                value = candidate.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
                if isinstance(value, dict) and isinstance(value.get("data"), list):
                    return [item for item in value["data"] if isinstance(item, dict)]
            value = candidate.get("data")
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def extract_pagination_items(data: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[Any] = []
    if isinstance(data.get("pagination"), dict):
        candidates.append(data["pagination"].get("data"))
    if isinstance(data.get("data"), dict) and isinstance(data["data"].get("pagination"), dict):
        candidates.append(data["data"]["pagination"].get("data"))
    if isinstance(data.get("pagination"), list):
        candidates.append(data["pagination"])
    candidates.append(data.get("data"))
    for candidate in candidates:
        if isinstance(candidate, list):
            return [item for item in candidate if isinstance(item, dict)]
    return []


def pagination_current_page(data: dict[str, Any]) -> int | None:
    for pagination in (data.get("pagination"), data.get("data", {}).get("pagination") if isinstance(data.get("data"), dict) else None):
        if isinstance(pagination, dict):
            return parse_optional_int(pagination.get("current_page"))
    return None


def parse_channel_id(value: str) -> int:
    value = value.strip()
    if value.isdigit():
        return int(value)
    match = re.match(r"^(\d+)\b", value)
    if match:
        return int(match.group(1))
    raise ValueError(f"Cannot parse channel ID from: {value}")


def extract_lien_url(data: dict[str, Any]) -> str:
    return extract_raw_url(data) or extract_direct_dl_url(data)


def normalize_onefichier_url(url: str) -> str:
    parsed = urlparse(url)
    if "1fichier.com" not in parsed.netloc.lower() or not parsed.query:
        return url
    file_code = parsed.query.split("&", 1)[0]
    if not file_code:
        return url
    return f"{parsed.scheme or 'https'}://{parsed.netloc}/?{file_code}"


def extract_direct_dl_url(data: dict[str, Any]) -> str:
    cursor: Any = data.get("data", data)
    if isinstance(cursor, dict):
        for key in ("directDL",):
            if isinstance(cursor.get(key), str) and cursor[key].startswith(("http://", "https://")):
                return cursor[key]
    return ""


def extract_raw_url(data: dict[str, Any]) -> str:
    cursor: Any = data.get("data", data)
    if isinstance(cursor, dict):
        for key in ("raw_url", "url"):
            if isinstance(cursor.get(key), str) and cursor[key].startswith(("http://", "https://")):
                return cursor[key]
        lien = cursor.get("lien")
        if isinstance(lien, dict):
            for key in ("url", "link", "raw_url"):
                if isinstance(lien.get(key), str) and lien[key].startswith(("http://", "https://")):
                    return lien[key]
    return ""


def infer_quality(lien: dict[str, Any], fallback: int | None) -> int | None:
    for key in ("qualite", "qualite_id", "quality_id", "qual_id"):
        value = parse_optional_int(lien.get(key))
        if value:
            return value
    qual = lien.get("qual") or lien.get("quality")
    if isinstance(qual, dict):
        for key in ("id", "id_qual"):
            value = parse_optional_int(qual.get(key))
            if value:
                return value
    return fallback


def infer_langues(lien: dict[str, Any], fallback: list[str]) -> list[str]:
    for key in ("langues", "languages", "langues_compact"):
        value = lien.get(key)
        if isinstance(value, list):
            langs: list[str] = []
            for item in value:
                if isinstance(item, str):
                    langs.append(item)
                elif isinstance(item, dict):
                    name = item.get("name") or item.get("lang") or item.get("value")
                    if name:
                        langs.append(str(name))
            if langs:
                return langs
    return fallback


def infer_subs(lien: dict[str, Any]) -> list[str]:
    for key in ("subs", "subtitles", "subs_compact"):
        value = lien.get(key)
        if isinstance(value, list):
            subs: list[str] = []
            for item in value:
                if isinstance(item, str):
                    subs.append(item)
                elif isinstance(item, dict):
                    name = item.get("name") or item.get("sub") or item.get("value")
                    if name:
                        subs.append(str(name))
            return subs
    return []


def find_first_url(data: Any) -> str:
    if isinstance(data, str):
        return data if data.startswith(("http://", "https://")) else ""
    if isinstance(data, dict):
        for key in ("url", "download_url", "download", "link"):
            value = data.get(key)
            found = find_first_url(value)
            if found:
                return found
        for value in data.values():
            found = find_first_url(value)
            if found:
                return found
    if isinstance(data, list):
        for item in data:
            found = find_first_url(item)
            if found:
                return found
    return ""


def generate_nzb_password(length: int = NZB_PASSWORD_LENGTH) -> str:
    length = max(1, min(10, length))
    return "".join(secrets.choice(NZB_PASSWORD_ALPHABET) for _ in range(length))


def filename_from_response(content_disposition: str | None, url: str) -> str:
    if content_disposition:
        match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', content_disposition)
        if match:
            return unquote(match.group(1))
    parsed = urlparse(url)
    name = Path(unquote(parsed.path)).name
    return name or f"download-{int(time.time())}.bin"


def response_content_length(value: str | None) -> int | None:
    if not value:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def reusable_existing_file(path: Path, expected_size: int | None = None, response_size: int | None = None) -> Path | None:
    if not path.exists() or not path.is_file():
        return None
    actual_size = path.stat().st_size
    for size in (expected_size, response_size):
        if size and actual_size == size:
            return path
    return None


def looks_like_html(chunk: bytes, content_type: str = "") -> bool:
    if "text/html" in content_type or "application/xhtml" in content_type:
        return True
    sample = chunk[:512].lstrip().lower()
    return sample.startswith((b"<!doctype html", b"<html", b"<head", b"<body"))


def sanitize_filename(name: str) -> str:
    name = name.replace("/", "_").replace("\\", "_").strip()
    return re.sub(r"[^A-Za-z0-9._() \[\]-]+", "_", name) or "download.bin"


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for index in range(1, 10000):
        candidate = path.with_name(f"{stem}.{index}{suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Cannot find a free filename for {path}")


def write_nyuu_config(config: NyuuConfig, input_path: Path, nzb_path: Path, *, title: str | None = None) -> str:
    host = config.host.strip()
    user = config.user.strip()
    password = config.password.strip()
    groups = config.groups.strip() or "alt.binaries.multimedia"
    if not host:
        raise RuntimeError("Nyuu requires --usenet-host or HYDRA_USENET_HOST.")
    if not user:
        raise RuntimeError("Nyuu requires --usenet-user or HYDRA_USENET_USER.")
    if not password:
        raise RuntimeError("Nyuu requires --usenet-password or HYDRA_USENET_PASSWORD.")
    if "@" in host:
        raise RuntimeError("Usenet host looks like an email. Use host=reader2.newsxs.nl and user=your account email.")
    if "." in user and "@" not in user:
        raise RuntimeError("Usenet user looks like a hostname. Check that host and user are not swapped.")
    data: dict[str, Any] = {
        "host": host,
        "ssl": config.ssl,
        "user": user,
        "password": password,
        "connections": config.connections,
        "groups": groups,
        "out": str(nzb_path),
        "overwrite": True,
        "nzb-title": title or input_path.stem,
    }
    if config.port:
        data["port"] = config.port
    if config.from_:
        data["from"] = config.from_
    if config.nzb_password:
        data["nzb-password"] = [config.nzb_password]
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".nyuu.json", delete=False) as fh:
        json.dump(data, fh)
        fh.write("\n")
        return fh.name


def build_nyuu_command(config: NyuuConfig, input_paths: list[Path], config_path: str) -> list[str]:
    command = shlex.split(config.bin)
    command.extend(["--config", config_path])
    command.extend(str(input_path) for input_path in input_paths)
    return command


def build_par2_command(par2_bin: str, output_base: Path, input_paths: list[Path], redundancy: int) -> list[str]:
    binary_name = Path(shlex.split(par2_bin)[0]).name.lower()
    command = shlex.split(par2_bin)
    if "par2create" not in binary_name:
        command.append("create")
    command.extend(["-q", f"-r{redundancy}", str(output_base)])
    command.extend(str(path) for path in input_paths)
    return command


def load_settings() -> dict[str, Any]:
    try:
        with SETTINGS_PATH.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def save_settings(data: dict[str, Any]) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with SETTINGS_PATH.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    try:
        SETTINGS_PATH.chmod(0o600)
    except OSError:
        pass


def setting_value(settings: dict[str, Any], key: str, default: str = "") -> str:
    value = settings.get(key, default)
    return "" if value is None else str(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Hydracker API GUI/CLI app.")
    parser.add_argument("--base-url", default=os.getenv("HYDRACKER_API_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--token", default=os.getenv("HYDRACKER_API_TOKEN", ""))
    parser.add_argument("--user-agent", default=os.getenv("HYDRACKER_USER_AGENT", DEFAULT_USER_AGENT))
    parser.add_argument("--insecure-tls", action="store_true")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("env", help="Print runtime/environment information.")
    sub.add_parser("gui", help="Force GUI mode.")

    get_lien = sub.add_parser("get-lien", help="GET /content/liens/{id}")
    get_lien.add_argument("id", help="Single ID or comma-separated IDs.")
    get_lien.add_argument("--streaming", action="store_true")

    title_links = sub.add_parser("title-links", help="List 1fichier links for a Hydracker title URL or ID.")
    title_links.add_argument("title", help="Hydracker title URL or numeric title ID.")
    title_links.add_argument("--query")
    title_links.add_argument("--limit", type=int)

    search = sub.add_parser("search", help="GET /search/{query}")
    search.add_argument("query")

    download = sub.add_parser("download-1f", help="Download one 1fichier URL through the 1fichier API.")
    add_1f_args(download)
    download.add_argument("url")
    download.add_argument("--out", default="downloads")
    download.add_argument("--password", default="")

    create = sub.add_parser("create-nzb", help="POST /nzb")
    add_nzb_args(create)

    sync_title = sub.add_parser("sync-title", help="Download 1fichier links for one title, build NZBs, upload with lien_id dedupe.")
    sync_title.add_argument("title", help="Hydracker title URL or numeric title ID.")
    add_workflow_args(sync_title)
    sync_title.add_argument("--query")
    sync_title.add_argument("--limit", type=int)

    sync_lien = sub.add_parser("sync-lien", help="Download one 1fichier link by lien_id, build NZB, upload with lien_id dedupe.")
    sync_lien.add_argument("id", help="Hydracker direct-link ID.")
    add_workflow_args(sync_lien)

    sync_liens_file = sub.add_parser("sync-liens-file", help="Run sync-lien for newline-separated lien IDs from a file.")
    sync_liens_file.add_argument("file", help="Path to a newline-separated lien_id queue.")
    add_workflow_args(sync_liens_file)
    sync_liens_file.add_argument("--done-file", help="Append processed lien IDs here and skip them on resume.")
    sync_liens_file.add_argument("--sleep", type=float, default=1.0, help="Seconds to wait between lien IDs.")
    sync_liens_file.add_argument("--limit", type=int, help="Maximum number of pending IDs to process.")

    sync_category = sub.add_parser("sync-category", help="Run sync-title for titles from a category/filter.")
    add_workflow_args(sync_category)
    sync_category.add_argument("--genre")
    sync_category.add_argument("--type", dest="type_")
    sync_category.add_argument("--pages", type=int, default=1)
    sync_category.add_argument("--per-page", type=int, default=50)
    sync_category.add_argument("--limit-per-title", type=int)

    sync_channel = sub.add_parser("sync-channel", help="Run sync-title for every title in a channel, page by page.")
    sync_channel.add_argument("channel", type=int, help="Hydracker channel ID.")
    add_workflow_args(sync_channel)
    sync_channel.add_argument("--start-page", type=int, default=1)
    sync_channel.add_argument("--max-pages", type=int)
    sync_channel.add_argument("--limit-per-title", type=int)
    sync_channel.add_argument("--store-results", action="store_true", help="Keep full per-title results in memory and final JSON output.")

    sync_random_series = sub.add_parser(
        "sync-random-series",
        help="Fetch a random series with missing NZBs from Hydracker, then finish all its lien IDs before another title.",
    )
    add_workflow_args(sync_random_series)
    sync_random_series.add_argument("--titles", type=int, default=1, help="Number of random series titles to process.")
    sync_random_series.add_argument("--limit", type=int, default=500, help="Maximum missing lien IDs returned per series.")
    sync_random_series.add_argument("--title-id", type=int, help="Debug/process a specific title instead of random.")
    sync_random_series.add_argument("--sleep", type=float, default=2.0, help="Seconds to wait between titles.")

    return parser


def add_1f_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--onefichier-token", default=os.getenv("ONEFICHIER_API_TOKEN", ""))
    parser.add_argument("--onefichier-base-url", default=os.getenv("ONEFICHIER_API_BASE_URL", DEFAULT_ONEFICHIER_BASE_URL))


def add_workflow_args(parser: argparse.ArgumentParser) -> None:
    add_1f_args(parser)
    parser.add_argument("--download-dir", default="downloads")
    parser.add_argument("--nzb-dir")
    parser.add_argument("--poster", choices=["custom", "nyuu"], default=os.getenv("HYDRACKER_POSTER", "custom"))
    parser.add_argument("--poster-command", default=os.getenv("HYDRACKER_POSTER_COMMAND", ""))
    parser.add_argument("--nyuu-bin", default=os.getenv("NYUU_BIN", "npx --yes nyuu"))
    parser.add_argument("--usenet-host", default=os.getenv("HYDRA_USENET_HOST", ""))
    parser.add_argument("--usenet-port", default=os.getenv("HYDRA_USENET_PORT", ""))
    parser.add_argument("--usenet-no-ssl", action="store_true")
    parser.add_argument("--usenet-user", default=os.getenv("HYDRA_USENET_USER", ""))
    parser.add_argument("--usenet-password", default=os.getenv("HYDRA_USENET_PASSWORD", ""))
    parser.add_argument("--usenet-groups", default=os.getenv("HYDRA_USENET_GROUPS", "alt.binaries.multimedia"))
    parser.add_argument("--usenet-connections", default=os.getenv("HYDRA_USENET_CONNECTIONS", "3"))
    parser.add_argument("--usenet-from", default=os.getenv("HYDRA_USENET_FROM", ""))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--keep-downloads", action="store_true", help="Do not delete downloaded files after a successful Hydracker NZB upload.")
    parser.add_argument("--no-pack-archives", action="store_true", help="Post the downloaded file directly instead of RAR/PAR2 packaging.")
    parser.add_argument("--rar-bin", default=os.getenv("RAR_BIN", "rar"))
    parser.add_argument("--par2-bin", default=os.getenv("PAR2_BIN", "par2"))
    parser.add_argument("--rar-volume-size", default=os.getenv("RAR_VOLUME_SIZE", "500m"))
    parser.add_argument("--par2-redundancy", type=int, default=parse_optional_int(os.getenv("PAR2_REDUNDANCY", "")) or 10)
    parser.add_argument("--qualite", type=int, help="Fallback quality ID when the link metadata has no quality.")
    parser.add_argument("--langues", default="", help="Fallback comma-separated languages, e.g. TrueFrench,English.")


def add_nzb_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--title-id", required=True, type=int)
    parser.add_argument("--qualite", required=True, type=int)
    parser.add_argument("--langues", required=True, help="Comma-separated language names, e.g. TrueFrench,English")
    parser.add_argument("--nzb", dest="nzb_path", help="Path to local .nzb file.")
    parser.add_argument("--lien-id", help="Optional source direct-link ID for nzb.lien_id dedupe.")
    parser.add_argument("--url", help="Existing storage/files .nzb URL/path.")
    parser.add_argument("--subs", default="", help="Comma-separated subtitle names.")
    parser.add_argument("--password", default="")
    parser.add_argument("--full-saison", action="store_true")
    parser.add_argument("--saison", type=int)
    parser.add_argument("--episode", type=int)
    parser.add_argument("--nfo", default="")


def workflow_from_args(args: argparse.Namespace) -> LinkToNzbWorkflow:
    return LinkToNzbWorkflow(
        HydrackerApiClient(config_from_env(args)),
        onefichier_from_env(args, required=not args.dry_run),
        download_dir=Path(args.download_dir),
        nzb_dir=Path(args.nzb_dir) if args.nzb_dir else None,
        poster_command=args.poster_command,
        nyuu=nyuu_from_env(args),
        dry_run=args.dry_run,
        fallback_qualite=args.qualite,
        fallback_langues=split_csv(args.langues),
        cleanup_downloads=not args.keep_downloads,
        pack=PackConfig(
            enabled=not args.no_pack_archives,
            rar_bin=args.rar_bin,
            par2_bin=args.par2_bin,
            volume_size=args.rar_volume_size,
            par2_redundancy=args.par2_redundancy,
        ),
        logger=print,
    )


def read_lien_id_file(path: Path) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        lien_id = line.split()[0].strip()
        if not lien_id or lien_id in seen:
            continue
        seen.add(lien_id)
        ids.append(lien_id)
    return ids


def run_sync_liens_file(args: argparse.Namespace) -> int:
    queue_path = Path(args.file)
    done_path = Path(args.done_file) if args.done_file else queue_path.with_suffix(queue_path.suffix + ".done")
    all_ids = read_lien_id_file(queue_path)
    done_ids = set(read_lien_id_file(done_path)) if done_path.exists() else set()
    pending_ids = [lien_id for lien_id in all_ids if lien_id not in done_ids]
    if args.limit:
        pending_ids = pending_ids[:args.limit]

    workflow = workflow_from_args(args)
    done_path.parent.mkdir(parents=True, exist_ok=True)
    processed = 0
    uploaded = 0
    skipped = 0
    failed = 0

    with done_path.open("a", encoding="utf-8") as done_fh:
        for lien_id in pending_ids:
            status = "failed"
            payload: dict[str, Any]
            try:
                result = workflow.sync_lien(lien_id)
                item_statuses = [
                    str(item.get("status"))
                    for item in result.get("results", [])
                    if isinstance(item, dict) and item.get("status")
                ]
                status = item_statuses[0] if item_statuses else "unknown"
                payload = {"lien_id": lien_id, "status": status, "result": result}
            except Exception as exc:
                payload = {"lien_id": lien_id, "status": "failed", "error_type": type(exc).__name__, "error": str(exc)}

            processed += 1
            if status == "uploaded":
                uploaded += 1
            elif status == "skipped":
                skipped += 1
            else:
                failed += 1

            print(json.dumps(payload, ensure_ascii=False), flush=True)
            done_fh.write(f"{lien_id}\n")
            done_fh.flush()
            if args.sleep > 0:
                time.sleep(args.sleep)

    print(pretty({
        "queue": str(queue_path),
        "done_file": str(done_path),
        "total_ids": len(all_ids),
        "already_done": len(done_ids),
        "processed": processed,
        "uploaded": uploaded,
        "skipped": skipped,
        "failed": failed,
    }), end="")
    return 0


def cli(argv: list[str]) -> int:
    parser = build_parser()
    if not argv:
        if gui_available():
            return launch_gui()
        parser.print_help()
        return 0

    args = parser.parse_args(argv)
    if args.command == "gui":
        return launch_gui(force=True)
    if args.command == "env":
        print(pretty({
            "app": APP_NAME,
            "version": APP_VERSION,
            "platform": platform.platform(),
            "python": sys.version.split()[0],
            "base_url": args.base_url,
            "gui_available": gui_available(),
            "token_present": bool(args.token),
            "onefichier_token_present": bool(os.getenv("ONEFICHIER_API_TOKEN")),
        }), end="")
        return 0

    if args.command == "download-1f":
        onefichier = onefichier_from_env(args, required=True)
        assert onefichier is not None
        path = onefichier.download(args.url, Path(args.out), password=args.password)
        print(pretty({"downloaded": str(path)}), end="")
        return 0

    client = HydrackerApiClient(config_from_env(args))

    if args.command == "get-lien":
        print(pretty(client.get_lien(args.id, streaming=args.streaming)), end="")
        return 0
    if args.command == "title-links":
        title_id = parse_title_id(args.title)
        print(pretty({"title_id": title_id, "liens": client.list_all_title_liens(title_id, query=args.query, limit=args.limit)}), end="")
        return 0
    if args.command == "search":
        print(pretty(client.search(args.query)), end="")
        return 0
    if args.command == "create-nzb":
        print(pretty(client.create_nzb(
            title_id=args.title_id,
            qualite=args.qualite,
            langues=split_csv(args.langues),
            lien_id=args.lien_id,
            nzb_path=args.nzb_path,
            url=args.url,
            subs=split_csv(args.subs),
            password=args.password,
            full_saison=args.full_saison,
            saison=args.saison,
            episode=args.episode,
            nfo=args.nfo,
        )), end="")
        return 0
    if args.command == "sync-title":
        print(pretty(workflow_from_args(args).sync_title(args.title, query=args.query, limit=args.limit)), end="")
        return 0
    if args.command == "sync-lien":
        print(pretty(workflow_from_args(args).sync_lien(args.id)), end="")
        return 0
    if args.command == "sync-liens-file":
        return run_sync_liens_file(args)
    if args.command == "sync-category":
        print(pretty(workflow_from_args(args).sync_category(
            genre=args.genre,
            type_=args.type_,
            pages=args.pages,
            per_page=args.per_page,
            limit_per_title=args.limit_per_title,
        )), end="")
        return 0
    if args.command == "sync-channel":
        print(pretty(workflow_from_args(args).sync_channel(
            args.channel,
            start_page=args.start_page,
            max_pages=args.max_pages,
            limit_per_title=args.limit_per_title,
            keep_results=args.store_results,
        )), end="")
        return 0
    if args.command == "sync-random-series":
        print(pretty(workflow_from_args(args).sync_random_series(
            titles=args.titles,
            limit=args.limit,
            title_id=args.title_id,
            sleep=args.sleep,
        )), end="")
        return 0

    parser.print_help()
    return 0


def launch_gui(force: bool = False) -> int:
    if not force and not gui_available():
        print("GUI unavailable. Use CLI commands.", file=sys.stderr)
        return 2

    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk
        from PIL import Image, ImageTk
    except Exception as exc:
        print(f"Tkinter unavailable: {exc}", file=sys.stderr)
        return 2

    settings = load_settings()
    root = tk.Tk()
    root.title(f"{APP_NAME} {APP_VERSION}")
    root.geometry("1120x780")
    root.minsize(960, 680)

    token = tk.StringVar(value=setting_value(settings, "token", os.getenv("HYDRACKER_API_TOKEN", "")))
    onef_token = tk.StringVar(value=setting_value(settings, "onef_token", os.getenv("ONEFICHIER_API_TOKEN", "")))
    base_url = tk.StringVar(value=setting_value(settings, "base_url", os.getenv("HYDRACKER_API_BASE_URL", DEFAULT_BASE_URL)))
    user_agent = tk.StringVar(value=setting_value(settings, "user_agent", os.getenv("HYDRACKER_USER_AGENT", DEFAULT_USER_AGENT)))
    theme = tk.StringVar(value=setting_value(settings, "theme", "System"))
    mode = tk.StringVar(value=setting_value(settings, "mode", "title"))
    title_ref = tk.StringVar(value=setting_value(settings, "title_ref", ""))
    search_query = tk.StringVar(value=setting_value(settings, "search_query", ""))
    genre = tk.StringVar(value=setting_value(settings, "genre", ""))
    type_ = tk.StringVar(value=setting_value(settings, "type", ""))
    channel_ref = tk.StringVar(value=setting_value(settings, "channel_ref", ""))
    channel_page = tk.StringVar(value=setting_value(settings, "channel_page", "1"))
    link_id = tk.StringVar(value=setting_value(settings, "link_id", ""))
    title_id = tk.StringVar(value=setting_value(settings, "title_id", ""))
    selected_season = tk.StringVar(value=setting_value(settings, "selected_season", ""))
    nzb_path = tk.StringVar(value=setting_value(settings, "nzb_path", ""))
    download_dir = tk.StringVar(value=setting_value(settings, "download_dir", str(Path.cwd() / "downloads")))
    nzb_dir = tk.StringVar(value=setting_value(settings, "nzb_dir", ""))
    poster_command = tk.StringVar(value=setting_value(settings, "poster_command", os.getenv("HYDRACKER_POSTER_COMMAND", "")))
    poster = tk.StringVar(value=setting_value(settings, "poster", os.getenv("HYDRACKER_POSTER", "nyuu")))
    nyuu_bin = tk.StringVar(value=setting_value(settings, "nyuu_bin", os.getenv("NYUU_BIN", "npx --yes nyuu")))
    usenet_host = tk.StringVar(value=setting_value(settings, "usenet_host", os.getenv("HYDRA_USENET_HOST", "")))
    usenet_port = tk.StringVar(value=setting_value(settings, "usenet_port", os.getenv("HYDRA_USENET_PORT", "")))
    usenet_user = tk.StringVar(value=setting_value(settings, "usenet_user", os.getenv("HYDRA_USENET_USER", "")))
    usenet_password = tk.StringVar(value=setting_value(settings, "usenet_password", os.getenv("HYDRA_USENET_PASSWORD", "")))
    usenet_groups = tk.StringVar(value=setting_value(settings, "usenet_groups", os.getenv("HYDRA_USENET_GROUPS", "alt.binaries.multimedia")))
    usenet_connections = tk.StringVar(value=setting_value(settings, "usenet_connections", os.getenv("HYDRA_USENET_CONNECTIONS", "3")))
    usenet_from = tk.StringVar(value=setting_value(settings, "usenet_from", os.getenv("HYDRA_USENET_FROM", "")))
    pack_archives = tk.BooleanVar(value=bool(settings.get("pack_archives", True)))
    rar_bin = tk.StringVar(value=setting_value(settings, "rar_bin", os.getenv("RAR_BIN", "rar")))
    par2_bin = tk.StringVar(value=setting_value(settings, "par2_bin", os.getenv("PAR2_BIN", "par2")))
    rar_volume_size = tk.StringVar(value=setting_value(settings, "rar_volume_size", os.getenv("RAR_VOLUME_SIZE", "500m")))
    par2_redundancy = tk.StringVar(value=setting_value(settings, "par2_redundancy", os.getenv("PAR2_REDUNDANCY", "10")))
    dry_run = tk.BooleanVar(value=bool(settings.get("dry_run", True)))
    status = tk.StringVar(value="Ready")

    root.columnconfigure(0, weight=1)
    root.rowconfigure(0, weight=1)

    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    notebook = ttk.Notebook(root)
    notebook.grid(row=0, column=0, sticky="nsew")

    workflow_tab = ttk.Frame(notebook, padding=12)
    options_tab = ttk.Frame(notebook, padding=12)
    logs_tab = ttk.Frame(notebook, padding=12)
    notebook.add(workflow_tab, text="Workflow")
    notebook.add(options_tab, text="Options")
    notebook.add(logs_tab, text="Logs")

    for tab in (workflow_tab, options_tab, logs_tab):
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(0, weight=1)

    log_text = tk.Text(logs_tab, wrap=tk.WORD, height=12)
    log_text.grid(row=0, column=0, sticky="nsew")
    log_scroll = ttk.Scrollbar(logs_tab, orient="vertical", command=log_text.yview)
    log_scroll.grid(row=0, column=1, sticky="ns")
    log_text.configure(yscrollcommand=log_scroll.set)

    image_refs: list[Any] = []
    result_canvas = tk.Text(workflow_tab, wrap=tk.NONE, height=12, borderwidth=0, highlightthickness=0, padx=0, pady=0)

    def is_dark_theme() -> bool:
        selected = theme.get()
        if selected == "Dark":
            return True
        if selected == "Light":
            return False
        return sys.platform.startswith("linux") and os.getenv("GTK_THEME", "").lower().find("dark") >= 0

    def apply_theme() -> None:
        dark = is_dark_theme()
        bg = "#111827" if dark else "#f6f7f9"
        panel = "#1f2937" if dark else "#ffffff"
        fg = "#e5e7eb" if dark else "#111827"
        muted = "#9ca3af" if dark else "#4b5563"
        field = "#0f172a" if dark else "#ffffff"
        selected = "#374151" if dark else "#e5e7eb"
        root.configure(bg=bg)
        style.configure(".", background=bg, foreground=fg, fieldbackground=field)
        style.configure("TFrame", background=bg)
        style.configure("TLabelframe", background=bg, foreground=fg)
        style.configure("TLabelframe.Label", background=bg, foreground=fg)
        style.configure("TLabel", background=bg, foreground=fg)
        style.configure("TCheckbutton", background=bg, foreground=fg)
        style.configure("TRadiobutton", background=bg, foreground=fg)
        style.configure("TButton", background=panel, foreground=fg)
        style.map("TButton", background=[("active", selected)])
        style.configure("TNotebook", background=bg)
        style.configure("TNotebook.Tab", background=panel, foreground=fg, padding=(12, 6))
        style.map("TNotebook.Tab", background=[("selected", selected)])
        style.configure("TEntry", fieldbackground=field, foreground=fg, selectbackground=selected, selectforeground=fg)
        style.configure(
            "TCombobox",
            fieldbackground=field,
            background=field,
            foreground=fg,
            selectbackground=selected,
            selectforeground=fg,
            arrowcolor=fg,
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", field), ("focus", field)],
            background=[("readonly", field), ("active", selected)],
            foreground=[("readonly", fg), ("focus", fg), ("disabled", muted)],
            selectbackground=[("readonly", selected), ("focus", selected)],
            selectforeground=[("readonly", fg), ("focus", fg)],
            arrowcolor=[("readonly", fg), ("active", fg)],
        )
        root.option_add("*TCombobox*Listbox.background", field)
        root.option_add("*TCombobox*Listbox.foreground", fg)
        root.option_add("*TCombobox*Listbox.selectBackground", selected)
        root.option_add("*TCombobox*Listbox.selectForeground", fg)
        style.configure("Horizontal.TProgressbar", background="#2563eb" if dark else "#1d4ed8")
        log_text.configure(bg=field, fg=fg, insertbackground=fg, selectbackground=selected)
        result_canvas.configure(bg=bg, fg=fg, insertbackground=fg, selectbackground=selected)
        status_label.configure(foreground=muted)

    def append_log(message: str) -> None:
        safe = message
        for secret in (token.get(), onef_token.get(), usenet_password.get()):
            if secret:
                safe = safe.replace(secret, "***")
        line = f"[{time.strftime('%H:%M:%S')}] {safe}\n"
        root.after(0, lambda: (log_text.insert(tk.END, line), log_text.see(tk.END)))

    def clear_results() -> None:
        result_canvas.configure(state=tk.NORMAL)
        result_canvas.delete("1.0", tk.END)
        result_canvas.configure(state=tk.DISABLED)
        image_refs.clear()
        result_canvas.yview_moveto(0)

    def add_result_widget(widget) -> None:
        result_canvas.configure(state=tk.NORMAL)
        result_canvas.window_create(tk.END, window=widget, padx=0, pady=0)
        result_canvas.insert(tk.END, "\n")
        result_canvas.configure(state=tk.DISABLED)

    def scroll_results_top() -> None:
        result_canvas.yview_moveto(0)

    def show_message(text: str) -> None:
        clear_results()
        add_result_widget(ttk.Label(result_canvas, text=text, padding=12))
        root.after_idle(scroll_results_top)

    def render_result(data: Any) -> None:
        clear_results()
        titles = extract_display_titles(data)
        if titles:
            for index, title in enumerate(titles[:50]):
                render_title_card(title, index)
            if len(titles) > 50:
                add_result_widget(ttk.Label(result_canvas, text=f"{len(titles) - 50} resultats supplementaires non affiches.", padding=10))
            root.after_idle(scroll_results_top)
            return
        render_data_summary(data)
        root.after_idle(scroll_results_top)

    def parse_int(value: str, name: str, required: bool = False) -> int | None:
        value = value.strip()
        if not value:
            if required:
                raise ValueError(f"{name} is required")
            return None
        return int(value)

    def row(parent: ttk.Frame, label: str, var: tk.StringVar, r: int, show: str | None = None) -> ttk.Entry:
        ttk.Label(parent, text=label).grid(row=r, column=0, sticky="w", padx=(0, 8), pady=4)
        entry = ttk.Entry(parent, textvariable=var, show=show)
        entry.grid(row=r, column=1, sticky="ew", pady=4)
        return entry

    def title_name(title: dict[str, Any]) -> str:
        return str(title.get("name") or title.get("title") or title.get("original_title") or f"Title #{title.get('id', '?')}")

    def title_date(title: dict[str, Any]) -> str:
        value = title.get("release_date") or title.get("released") or title.get("created_at") or ""
        return str(value)[:10] if value else ""

    def title_type(title: dict[str, Any]) -> str:
        if title.get("is_series") is True:
            return "Series"
        if title.get("is_series") is False:
            return "Movie"
        return str(title.get("type") or title.get("model_type") or "").title()

    def title_genres(title: dict[str, Any]) -> str:
        genres = title.get("genres") or title.get("genre") or []
        if isinstance(genres, str):
            return genres
        names: list[str] = []
        if isinstance(genres, list):
            for genre_item in genres:
                if isinstance(genre_item, str):
                    names.append(genre_item)
                elif isinstance(genre_item, dict):
                    name = genre_item.get("display_name") or genre_item.get("name")
                    if name:
                        names.append(str(name))
        return ", ".join(names)

    def title_image_url(title: dict[str, Any]) -> str:
        for key in ("poster", "poster_url", "image", "image_url", "thumbnail"):
            value = title.get(key)
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                return value
        images = title.get("images")
        if isinstance(images, list):
            poster = None
            first = None
            for image in images:
                if not isinstance(image, dict):
                    continue
                url = image.get("url")
                if not isinstance(url, str):
                    continue
                first = first or url
                if str(image.get("type", "")).lower() == "poster":
                    poster = url
            return poster or first or ""
        return ""

    def extract_display_titles(data: Any) -> list[dict[str, Any]]:
        if not isinstance(data, dict):
            return []
        title = data.get("title")
        if not isinstance(title, dict) and isinstance(data.get("data"), dict):
            title = data["data"].get("title")
        if title:
            return [title]
        candidates: list[Any] = [
            data.get("titles"),
            data.get("results"),
            data.get("data"),
        ]
        if isinstance(data.get("data"), dict):
            candidates.extend([
                data["data"].get("titles"),
                data["data"].get("results"),
                data["data"].get("pagination", {}).get("data") if isinstance(data["data"].get("pagination"), dict) else None,
            ])
        if isinstance(data.get("pagination"), dict):
            candidates.append(data["pagination"].get("data"))
        for candidate in candidates:
            if isinstance(candidate, list):
                titles = [item for item in candidate if isinstance(item, dict)]
                if titles:
                    return titles
        return []

    def load_poster(url: str, size: tuple[int, int] = (96, 144)):
        try:
            if url:
                req = Request(url, headers={"User-Agent": user_agent.get().strip() or DEFAULT_USER_AGENT})
                with urlopen(req, timeout=5) as resp:
                    raw = resp.read(2 * 1024 * 1024)
                image = Image.open(io.BytesIO(raw)).convert("RGB")
            else:
                image = Image.new("RGB", size, "#374151" if is_dark_theme() else "#e5e7eb")
            image.thumbnail(size)
            canvas = Image.new("RGB", size, "#111827" if is_dark_theme() else "#ffffff")
            canvas.paste(image, ((size[0] - image.width) // 2, (size[1] - image.height) // 2))
            photo = ImageTk.PhotoImage(canvas)
            image_refs.append(photo)
            return photo
        except Exception:
            image = Image.new("RGB", size, "#374151" if is_dark_theme() else "#e5e7eb")
            photo = ImageTk.PhotoImage(image)
            image_refs.append(photo)
            return photo

    def render_title_card(title: dict[str, Any], index: int) -> None:
        card = ttk.Frame(result_canvas, padding=10)
        card.columnconfigure(1, weight=1)
        poster = load_poster(title_image_url(title) if index < 24 else "")
        tk.Label(card, image=poster, bd=0).grid(row=0, column=0, rowspan=6, sticky="nw", padx=(0, 14))
        ttk.Label(card, text=title_name(title), font=("TkDefaultFont", 13, "bold")).grid(row=0, column=1, sticky="ew")
        details = [
            ("ID", title.get("id")),
            ("Type", title_type(title)),
            ("Sortie", title_date(title)),
            ("Genres", title_genres(title)),
            ("Note", title.get("rating") or title.get("score")),
        ]
        row_idx = 1
        for label, value in details:
            if value in (None, ""):
                continue
            ttk.Label(card, text=f"{label}: {value}").grid(row=row_idx, column=1, sticky="w", pady=1)
            row_idx += 1
        description = title.get("description") or title.get("overview")
        if description:
            ttk.Label(card, text=str(description), wraplength=760, justify="left").grid(row=row_idx, column=1, sticky="ew", pady=(6, 0))
        add_result_widget(card)

    def render_data_summary(data: Any) -> None:
        card = ttk.Frame(result_canvas, padding=12)
        if isinstance(data, dict):
            rows = []
            for key, value in data.items():
                if key in {"raw"}:
                    continue
                if isinstance(value, (dict, list)):
                    rows.append(f"{key}: {len(value)} item(s)")
                else:
                    rows.append(f"{key}: {value}")
            text = "\n".join(rows) or "Aucun resultat."
        else:
            text = str(data)
        ttk.Label(card, text=text, justify="left", wraplength=900).grid(row=0, column=0, sticky="w")
        add_result_widget(card)

    top = ttk.Frame(workflow_tab)
    top.grid(row=0, column=0, sticky="nsew")
    top.columnconfigure(0, weight=1)
    top.rowconfigure(3, weight=1)

    mode_box = ttk.LabelFrame(top, text="Source", padding=10)
    mode_box.grid(row=0, column=0, sticky="ew")
    for value, label in (("search", "Search"), ("category", "Categorie"), ("title", "Title ID/URL"), ("lien", "Lien ID")):
        ttk.Radiobutton(mode_box, text=label, value=value, variable=mode).pack(side=tk.LEFT, padx=(0, 18))

    source_box = ttk.LabelFrame(top, text="Selection", padding=10)
    source_box.grid(row=1, column=0, sticky="ew", pady=(10, 0))
    source_box.columnconfigure(1, weight=1)
    source_rows: dict[str, ttk.Frame] = {}

    def source_row(name: str, label: str, var: tk.StringVar, show: str | None = None) -> ttk.Entry:
        frame = ttk.Frame(source_box)
        frame.columnconfigure(1, weight=1)
        ttk.Label(frame, text=label, width=12).grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
        entry_width = 92 if name == "title" else 60
        entry = ttk.Entry(frame, textvariable=var, show=show, width=entry_width)
        entry.grid(row=0, column=1, sticky="ew", pady=4)
        source_rows[name] = frame
        return entry

    search_entry = source_row("search", "Search", search_query)
    category_frame = ttk.Frame(source_box)
    category_frame.columnconfigure(1, weight=1)
    ttk.Label(category_frame, text="Channel").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
    channel_combo = ttk.Combobox(category_frame, textvariable=channel_ref)
    channel_combo.grid(row=0, column=1, sticky="ew", pady=4)
    ttk.Button(category_frame, text="Refresh", command=lambda: run_async("channels", do_load_channels)).grid(row=0, column=2, padx=(8, 0))
    ttk.Label(category_frame, text="Page").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=4)
    category_entry = ttk.Entry(category_frame, textvariable=channel_page, width=8)
    category_entry.grid(row=1, column=1, sticky="w", pady=4)
    ttk.Button(category_frame, text="Prev", command=lambda: run_channel_page(-1)).grid(row=1, column=2, padx=(8, 0))
    ttk.Button(category_frame, text="Next", command=lambda: run_channel_page(1)).grid(row=1, column=3, padx=(8, 0))
    source_rows["category"] = category_frame
    title_entry = source_row("title", "Title URL/ID", title_ref)
    lien_entry = source_row("lien", "Lien ID", link_id)

    season_box = ttk.Frame(source_box)
    season_box.columnconfigure(1, weight=1)
    ttk.Label(season_box, text="Season").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
    season_combo = ttk.Combobox(season_box, textvariable=selected_season, values=[], state="readonly")
    season_combo.grid(row=0, column=1, sticky="ew", pady=4)

    action_box = ttk.Frame(top)
    action_box.grid(row=2, column=0, sticky="ew", pady=10)
    status_label = ttk.Label(action_box, textvariable=status)
    status_label.pack(side=tk.RIGHT)
    progress = ttk.Progressbar(action_box, mode="indeterminate", length=180)
    progress.pack(side=tk.RIGHT, padx=(0, 12))

    result_canvas.grid(row=3, column=0, sticky="nsew")
    result_scroll = ttk.Scrollbar(top, orient="vertical", command=result_canvas.yview)
    result_scroll.grid(row=3, column=1, sticky="ns")
    result_canvas.configure(yscrollcommand=result_scroll.set)

    def on_result_mousewheel(event) -> str:
        if event.num == 4:
            result_canvas.yview_scroll(-3, "units")
        elif event.num == 5:
            result_canvas.yview_scroll(3, "units")
        else:
            result_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        return "break"

    result_canvas.bind("<MouseWheel>", on_result_mousewheel)
    result_canvas.bind("<Button-4>", on_result_mousewheel)
    result_canvas.bind("<Button-5>", on_result_mousewheel)

    options = ttk.Frame(options_tab)
    options.grid(row=0, column=0, sticky="nsew")
    options.columnconfigure(0, weight=1)
    options.columnconfigure(1, weight=1)

    api_box = ttk.LabelFrame(options, text="API", padding=10)
    api_box.grid(row=0, column=0, sticky="nsew", padx=(0, 6), pady=(0, 10))
    api_box.columnconfigure(1, weight=1)
    row(api_box, "Hydracker API base", base_url, 0)
    row(api_box, "Hydracker bearer token", token, 1, show="*")
    row(api_box, "1fichier API token", onef_token, 2, show="*")
    row(api_box, "User-Agent", user_agent, 3)
    ttk.Label(api_box, text="Theme").grid(row=4, column=0, sticky="w", padx=(0, 8), pady=4)
    theme_combo = ttk.Combobox(api_box, textvariable=theme, values=["System", "Dark", "Light"], state="readonly")
    theme_combo.grid(row=4, column=1, sticky="ew", pady=4)
    theme_combo.bind("<<ComboboxSelected>>", lambda _event: apply_theme())

    paths_box = ttk.LabelFrame(options, text="Paths / defaults", padding=10)
    paths_box.grid(row=1, column=0, sticky="nsew", padx=(0, 6))
    paths_box.columnconfigure(1, weight=1)
    row(paths_box, "Download dir", download_dir, 0)
    ttk.Button(paths_box, text="Browse", command=lambda: download_dir.set(filedialog.askdirectory())).grid(row=0, column=2, padx=(8, 0))
    row(paths_box, "NZB dir", nzb_dir, 1)
    ttk.Button(paths_box, text="Browse", command=lambda: nzb_dir.set(filedialog.askdirectory())).grid(row=1, column=2, padx=(8, 0))
    ttk.Checkbutton(paths_box, text="Dry run by default", variable=dry_run).grid(row=2, column=1, sticky="w", pady=4)

    nyuu_box = ttk.LabelFrame(options, text="Poster / Nyuu", padding=10)
    nyuu_box.grid(row=0, column=1, rowspan=2, sticky="nsew", padx=(6, 0))
    nyuu_box.columnconfigure(1, weight=1)
    ttk.Label(nyuu_box, text="Poster").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
    poster_combo = ttk.Combobox(nyuu_box, textvariable=poster, values=["nyuu", "custom"], state="readonly")
    poster_combo.grid(row=0, column=1, sticky="ew", pady=4)
    row(nyuu_box, "Nyuu bin", nyuu_bin, 1)
    row(nyuu_box, "Usenet host", usenet_host, 2)
    row(nyuu_box, "Usenet port", usenet_port, 3)
    row(nyuu_box, "Usenet user", usenet_user, 4)
    row(nyuu_box, "Usenet password", usenet_password, 5, show="*")
    row(nyuu_box, "Usenet groups", usenet_groups, 6)
    row(nyuu_box, "Connections", usenet_connections, 7)
    row(nyuu_box, "From", usenet_from, 8)
    row(nyuu_box, "Custom command", poster_command, 9)
    ttk.Checkbutton(nyuu_box, text="RAR/PAR2 packaging", variable=pack_archives).grid(row=10, column=1, sticky="w", pady=4)
    row(nyuu_box, "RAR bin", rar_bin, 11)
    row(nyuu_box, "PAR2 bin", par2_bin, 12)
    row(nyuu_box, "RAR volume", rar_volume_size, 13)
    row(nyuu_box, "PAR2 recovery %", par2_redundancy, 14)

    options_actions = ttk.Frame(options)
    options_actions.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(12, 0))

    def client_from_form() -> HydrackerApiClient:
        if not token.get().strip():
            raise ValueError("Hydracker bearer token is required")
        return HydrackerApiClient(ApiConfig(
            base_url=base_url.get().strip() or DEFAULT_BASE_URL,
            token=token.get().strip(),
            user_agent=user_agent.get().strip() or DEFAULT_USER_AGENT,
        ), logger=append_log)

    def onef_from_form(required: bool) -> OneFichierClient | None:
        if not onef_token.get().strip():
            if required:
                raise ValueError("1fichier API token is required")
            return None
        return OneFichierClient(OneFichierConfig(
            token=onef_token.get().strip(),
            user_agent=user_agent.get().strip() or DEFAULT_USER_AGENT,
        ), logger=append_log)

    def workflow_from_form() -> LinkToNzbWorkflow:
        return LinkToNzbWorkflow(
            client_from_form(),
            onef_from_form(required=not dry_run.get()),
            download_dir=Path(download_dir.get().strip() or "downloads"),
            nzb_dir=Path(nzb_dir.get().strip()) if nzb_dir.get().strip() else None,
            poster_command=poster_command.get().strip(),
            nyuu=NyuuConfig(
                enabled=poster.get().strip().lower() == "nyuu",
                bin=nyuu_bin.get().strip() or "npx --yes nyuu",
                host=usenet_host.get().strip(),
                port=parse_optional_int(usenet_port.get().strip()),
                user=usenet_user.get().strip(),
                password=usenet_password.get().strip(),
                groups=usenet_groups.get().strip() or "alt.binaries.multimedia",
                connections=parse_optional_int(usenet_connections.get().strip()) or 3,
                from_=usenet_from.get().strip(),
            ),
            dry_run=dry_run.get(),
            pack=PackConfig(
                enabled=pack_archives.get(),
                rar_bin=rar_bin.get().strip() or "rar",
                par2_bin=par2_bin.get().strip() or "par2",
                volume_size=rar_volume_size.get().strip() or "500m",
                par2_redundancy=parse_optional_int(par2_redundancy.get().strip()) or 10,
            ),
            logger=append_log,
        )

    def collect_settings() -> dict[str, Any]:
        return {
            "token": token.get(),
            "onef_token": onef_token.get(),
            "base_url": base_url.get(),
            "user_agent": user_agent.get(),
            "theme": theme.get(),
            "mode": mode.get(),
            "title_ref": title_ref.get(),
            "search_query": search_query.get(),
            "genre": genre.get(),
            "type": type_.get(),
            "channel_ref": channel_ref.get(),
            "channel_page": channel_page.get(),
            "link_id": link_id.get(),
            "title_id": title_id.get(),
            "selected_season": selected_season.get(),
            "nzb_path": nzb_path.get(),
            "download_dir": download_dir.get(),
            "nzb_dir": nzb_dir.get(),
            "poster": poster.get(),
            "poster_command": poster_command.get(),
            "nyuu_bin": nyuu_bin.get(),
            "usenet_host": usenet_host.get(),
            "usenet_port": usenet_port.get(),
            "usenet_user": usenet_user.get(),
            "usenet_password": usenet_password.get(),
            "usenet_groups": usenet_groups.get(),
            "usenet_connections": usenet_connections.get(),
            "usenet_from": usenet_from.get(),
            "pack_archives": pack_archives.get(),
            "rar_bin": rar_bin.get(),
            "par2_bin": par2_bin.get(),
            "rar_volume_size": rar_volume_size.get(),
            "par2_redundancy": par2_redundancy.get(),
            "dry_run": dry_run.get(),
        }

    def do_save_settings() -> dict[str, Any]:
        save_settings(collect_settings())
        return {"saved": str(SETTINGS_PATH)}

    sync_lock = threading.Lock()

    def run_async(label: str, fn, *, exclusive: bool = False) -> None:
        def worker() -> None:
            locked = False
            if exclusive:
                locked = sync_lock.acquire(blocking=False)
                if not locked:
                    append_log(f"skip {label}: another sync is already running")
                    root.after(0, lambda: show_message("Un upload est deja en cours. Attendez la fin du title en cours."))
                    return
            root.after(0, lambda: (status.set(f"Running {label}"), progress.start(10), show_message(f"Chargement: {label}")))
            append_log(f"start {label}")
            try:
                result = fn()
                append_log(pretty(result)[:4000])
                root.after(0, lambda: render_result(result))
            except Exception as exc:
                root.after(0, lambda: show_message(f"{type(exc).__name__}: {exc}"))
                append_log(f"error {label}: {type(exc).__name__}: {exc}")
            finally:
                append_log(f"done {label}")
                root.after(0, lambda: (progress.stop(), status.set("Ready")))
                if locked:
                    sync_lock.release()

        threading.Thread(target=worker, daemon=True).start()

    def do_get_lien() -> dict[str, Any]:
        return client_from_form().get_lien(link_id.get().strip())

    def do_search() -> dict[str, Any]:
        return client_from_form().search(search_query.get().strip())

    def do_load_channels() -> dict[str, Any]:
        data = client_from_form().list_channels(page=1, per_page=100)
        channels = extract_pagination_items(data)
        values: list[str] = []
        for channel in channels:
            channel_id = channel.get("id")
            name = channel.get("name") or channel.get("slug") or channel.get("display_name") or "Channel"
            if channel_id:
                values.append(f"{channel_id} - {name}")
        root.after(0, lambda: channel_combo.configure(values=values))
        if values and not channel_ref.get().strip():
            root.after(0, lambda: channel_ref.set(values[0]))
        return {"channels": channels}

    def do_browse_category() -> dict[str, Any]:
        channel_id = parse_channel_id(channel_ref.get())
        page = parse_optional_int(channel_page.get()) or 1
        data = client_from_form().get_channel_content(
            channel_id,
            page=page,
            order="last_content_added_at:desc",
            restriction="",
            filters="",
        )
        current = pagination_current_page(data)
        if current:
            root.after(0, lambda: channel_page.set(str(current)))
        return data

    def run_channel_page(delta: int) -> None:
        page = max(1, (parse_optional_int(channel_page.get()) or 1) + delta)
        channel_page.set(str(page))
        run_async(f"channel-page-{page}", do_browse_category)

    def title_from_response(data: dict[str, Any]) -> dict[str, Any]:
        title = data.get("title")
        if isinstance(title, dict):
            return title
        payload = data.get("data")
        if isinstance(payload, dict) and isinstance(payload.get("title"), dict):
            return payload["title"]
        return {}

    def seasons_from_title(title: dict[str, Any]) -> list[str]:
        seasons = title.get("seasons")
        if not isinstance(seasons, list):
            return []
        values: list[str] = []
        for season in seasons:
            if not isinstance(season, dict):
                continue
            number = season.get("number")
            if number is None:
                continue
            count = season.get("episode_count")
            label = f"{number}"
            if count is not None:
                label = f"{number} ({count} episodes)"
            values.append(label)
        return values

    def selected_season_number() -> int | None:
        value = selected_season.get().strip()
        if not value:
            return None
        return parse_optional_int(value.split(" ", 1)[0])

    def do_get_title() -> dict[str, Any]:
        tid = parse_title_id(title_ref.get().strip())
        if title_id.get().strip() != str(tid):
            selected_season.set("")
        title_id.set(str(tid))
        data = client_from_form().get_title(tid, season_number=selected_season_number())
        title = title_from_response(data)
        seasons = seasons_from_title(title)
        root.after(0, lambda: update_seasons(seasons))
        return data

    def do_title_links() -> dict[str, Any]:
        tid = parse_title_id(title_ref.get().strip())
        return {"title_id": tid, "liens": client_from_form().list_all_title_liens(tid)}

    def do_sync_title() -> dict[str, Any]:
        return workflow_from_form().sync_title(title_ref.get().strip())

    def do_sync_lien() -> dict[str, Any]:
        return workflow_from_form().sync_lien(link_id.get().strip())

    def do_sync_channel_page() -> dict[str, Any]:
        channel_data = do_browse_category()
        titles = extract_display_titles(channel_data)
        workflow = workflow_from_form()
        results: list[dict[str, Any]] = []
        for index, title in enumerate(titles, start=1):
            tid = title.get("id")
            if not tid:
                results.append({"status": "skipped", "reason": "missing_title_id", "title": title})
                continue
            append_log(f"sync channel title {index}/{len(titles)} title_id={tid} start")
            try:
                result = workflow.sync_title(str(tid))
                results.append(result)
                append_log(f"sync channel title {index}/{len(titles)} title_id={tid} done")
            except Exception as exc:
                results.append({"title_id": tid, "error": f"{type(exc).__name__}: {exc}"})
                append_log(f"sync channel title {index}/{len(titles)} title_id={tid} failed: {type(exc).__name__}: {exc}")
        return {"channel": channel_ref.get(), "page": channel_page.get(), "titles": len(titles), "results": results}

    def do_sync_selected() -> dict[str, Any]:
        selected = mode.get()
        if selected == "title":
            return do_sync_title()
        if selected == "category":
            return do_sync_channel_page()
        if selected == "lien":
            return do_sync_lien()
        raise ValueError("Le sync auto est disponible en mode Title ID/URL, Categorie ou Lien ID.")

    def do_run_selected() -> dict[str, Any]:
        selected = mode.get()
        if selected == "search":
            return do_search()
        if selected == "category":
            return do_browse_category()
        if selected == "title":
            return do_get_title()
        if selected == "lien":
            return do_get_lien()
        raise ValueError(f"Unknown mode: {selected}")

    def update_seasons(values: list[str]) -> None:
        season_combo.configure(values=values)
        if values:
            season_box.grid(row=1, column=0, sticky="ew", pady=(8, 0))
            if selected_season.get() not in values:
                selected_season.set(values[0])
        else:
            selected_season.set("")
            season_box.grid_remove()

    def update_source_visibility(*_args) -> None:
        for frame in source_rows.values():
            frame.grid_remove()
        current = mode.get()
        source_rows[current].grid(row=0, column=0, sticky="ew")
        if current != "title":
            season_box.grid_remove()
        if current == "category" and token.get().strip() and not channel_combo.cget("values"):
            run_async("channels", do_load_channels)
        clear_results()
        status.set("Ready")

    def auto_fetch_from_event(_event=None) -> None:
        run_async(f"run-{mode.get()}", do_run_selected)

    auto_job: list[str | None] = [None]

    def schedule_auto_fetch(*_args) -> None:
        if auto_job[0]:
            root.after_cancel(auto_job[0])
            auto_job[0] = None
        current = mode.get()
        if current == "search" and len(search_query.get().strip()) < 3:
            return
        if current == "title":
            try:
                parse_title_id(title_ref.get().strip())
            except ValueError:
                return
        if current == "lien" and not link_id.get().strip():
            return
        if current == "category" and not channel_ref.get().strip():
            return
        if not token.get().strip():
            return
        auto_job[0] = root.after(900, lambda: run_async(f"auto-{mode.get()}", do_run_selected))

    mode.trace_add("write", update_source_visibility)
    search_query.trace_add("write", schedule_auto_fetch)
    title_ref.trace_add("write", schedule_auto_fetch)
    link_id.trace_add("write", schedule_auto_fetch)
    channel_ref.trace_add("write", schedule_auto_fetch)
    search_entry.bind("<Return>", auto_fetch_from_event)
    title_entry.bind("<Return>", auto_fetch_from_event)
    lien_entry.bind("<Return>", auto_fetch_from_event)
    category_entry.bind("<Return>", auto_fetch_from_event)
    channel_combo.bind("<<ComboboxSelected>>", auto_fetch_from_event)
    season_combo.bind("<<ComboboxSelected>>", lambda _event: run_async("title-season", do_get_title))

    def clear_logs() -> None:
        log_text.delete("1.0", tk.END)

    ttk.Button(action_box, text="Run", command=lambda: run_async(f"run-{mode.get()}", do_run_selected)).pack(side=tk.LEFT, padx=(0, 8))
    ttk.Button(action_box, text="Download + NZB + Upload", command=lambda: run_async(f"sync-{mode.get()}", do_sync_selected, exclusive=True)).pack(side=tk.LEFT, padx=(0, 8))
    ttk.Button(action_box, text="Clear result", command=clear_results).pack(side=tk.LEFT, padx=(0, 8))
    ttk.Button(options_actions, text="Save options", command=lambda: run_async("save-options", do_save_settings)).pack(side=tk.LEFT)
    ttk.Button(logs_tab, text="Clear logs", command=clear_logs).grid(row=1, column=0, sticky="w", pady=(8, 0))

    def on_close() -> None:
        try:
            save_settings(collect_settings())
        except Exception as exc:
            messagebox.showwarning(APP_NAME, f"Could not save settings: {exc}")
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    apply_theme()
    update_source_visibility()
    append_log(f"settings path {SETTINGS_PATH}")

    root.mainloop()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(cli(sys.argv[1:]))
    except ApiError as exc:
        print(f"API error {exc.status}:\n{exc.body}", file=sys.stderr)
        raise SystemExit(1)
