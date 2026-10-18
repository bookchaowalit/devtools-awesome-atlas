"""Bounded public README fetches and a fair, non-overlapping in-process scheduler."""

import asyncio
import threading
from datetime import UTC, datetime, timedelta
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from ._sentry import capture
from .config import Config
from .daily import DAILY_SOURCE, parse_daily, validate_date
from .lake import Lake, utc_now
from .parser import ROOT_ID, canonical_repository, parse_catalog, parse_projects
from .store import Store


class SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        parsed = urlsplit(newurl)
        if parsed.scheme != "https" or parsed.netloc != "raw.githubusercontent.com":
            raise ValueError("redirect host ไม่อยู่ใน allowlist")
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def fetch_readme(source_id: str, config: Config) -> bytes:
    if canonical_repository(f"https://github.com/{source_id}") != source_id:
        raise ValueError("repository id ไม่ถูกต้อง")
    branch = "main" if source_id == ROOT_ID else "HEAD"
    opener = build_opener(SafeRedirect())
    for filename in ("readme.md", "README.md"):
        request = Request(
            f"https://raw.githubusercontent.com/{source_id}/{branch}/{filename}",
            headers={
                "User-Agent": "AwesomeExplore/0.1 public-readme",
                "Accept": "text/plain",
            },
        )
        try:
            with opener.open(request, timeout=config.fetch_timeout_seconds) as response:
                content_length = response.headers.get("Content-Length")
                if content_length and int(content_length) > config.max_readme_bytes:
                    raise ValueError("README ใหญ่เกิน 4 MiB")
                raw = response.read(config.max_readme_bytes + 1)
                if len(raw) > config.max_readme_bytes:
                    raise ValueError("README ใหญ่เกิน 4 MiB")
                if content_length and len(raw) != int(content_length):
                    raise ValueError("ดาวน์โหลด README ไม่ครบ")
                return raw
        except HTTPError as error:
            if error.code == 404 and filename == "readme.md":
                continue
            raise
    raise ValueError("ไม่พบ README")


def fetch_daily(source_id: str, requested_date: str, config: Config) -> bytes | None:
    if source_id != DAILY_SOURCE["id"]:
        raise ValueError("daily source ไม่อยู่ใน allowlist")
    validate_date(requested_date)
    url = (
        f"https://raw.githubusercontent.com/{source_id}/HEAD/archive/repository/"
        f"{requested_date[:4]}/{requested_date}/(null).json"
    )
    request = Request(
        url,
        headers={
            "User-Agent": "AwesomeExplore/0.1 public-daily",
            "Accept": "application/json",
        },
    )
    try:
        with build_opener(SafeRedirect()).open(
            request, timeout=config.fetch_timeout_seconds
        ) as response:
            declared = response.headers.get("Content-Length")
            if declared and int(declared) > config.max_readme_bytes:
                raise ValueError("daily payload ใหญ่เกิน 4 MiB")
            raw = response.read(config.max_readme_bytes + 1)
            if len(raw) > config.max_readme_bytes:
                raise ValueError("daily payload ใหญ่เกิน 4 MiB")
            if declared and len(raw) != int(declared):
                raise ValueError("ดาวน์โหลด daily payload ไม่ครบ")
            return raw
    except HTTPError as error:
        if error.code == 404:
            return None
        raise


class Coordinator:
    def __init__(
        self, config: Config, store: Store, lake: Lake, fetch=None, daily_fetch=None
    ):
        self.config, self.store, self.lake = config, store, lake
        self.fetch = fetch or (lambda source_id: fetch_readme(source_id, config))
        self.daily_fetch = daily_fetch or (
            lambda source_id, requested_date: fetch_daily(
                source_id, requested_date, config
            )
        )
        self.today = lambda: datetime.now(UTC).date()
        self.lock = threading.Lock()
        self._reported = False  # one Sentry event per sync round, not one per source
        self.next_sync_at = utc_now() if config.sync_enabled else None

    def _capture(self, source_id: str):
        self.store.attempted(source_id)
        try:
            source = self.store.source_detail(source_id)
            raw = self.fetch(source_id)
            if not raw or len(raw) > self.config.max_readme_bytes:
                raise ValueError("README ว่างหรือเกินขนาดที่อนุญาต")
            text = raw.decode("utf-8", errors="replace")
            if text.lstrip().lower().startswith(("<!doctype html", "<html")):
                raise ValueError("upstream ส่ง HTML แทน Markdown")
            if source_id == ROOT_ID:
                parsed = parse_catalog(text)
                records, names, role = parsed.records, parsed.category_names, "root"
                if not records:
                    raise ValueError("ไม่พบ repository lists ใน root README")
            else:
                records, names, role = (
                    parse_projects(text, source_id, source["categories"]),
                    {},
                    "list",
                )
                if not records:
                    raise ValueError(
                        "ไม่พบ GitHub repository links ที่รองรับใน list README; รองรับ Markdown bullets/reference links"
                    )
            public_source = {
                key: source[key]
                for key in ("id", "name", "owner", "url", "category", "categories")
            }
            manifest = self.lake.commit(
                source=public_source,
                raw=raw,
                records=records,
                role=role,
                category_names=names,
            )
            self.store.replace_capture(self.lake, manifest)
            return True
        except Exception as error:  # noqa: BLE001 - record the failure and keep syncing other sources
            self._report(error)
            self.store.failed(source_id, str(error) or type(error).__name__)
            return False

    def _capture_daily(self, requested_date: str):
        try:
            raw = self.daily_fetch(DAILY_SOURCE["id"], requested_date)
            if raw is None:
                return None  # Missing archive day: never replace a retained snapshot.
            if not raw or len(raw) > self.config.max_readme_bytes:
                raise ValueError("daily payload ว่างหรือเกินขนาดที่อนุญาต")
            records = parse_daily(raw, requested_date)
            manifest = self.lake.commit(
                source=DAILY_SOURCE,
                raw=raw,
                records=records,
                role="daily",
                snapshot_date=requested_date,
            )
            self.store.replace_capture(self.lake, manifest)
            return True
        except Exception as error:  # noqa: BLE001 - record the failure and keep syncing other sources
            self._report(error)
            self.store.daily_error(
                f"{requested_date}: {str(error) or type(error).__name__}"
            )
            return False

    def _sync_daily(self, stop=None):
        errors, any_success = [], False
        today = self.today()
        for days_ago in range(7):
            if stop is not None and stop.is_set():
                break
            requested = (today - timedelta(days=days_ago)).isoformat()
            result = self._capture_daily(requested)
            if result is False:
                errors.append(self.store.daily_sources()[0]["error"])
            any_success = any_success or result is True
        self.store.daily_error("; ".join(errors) if errors else None)
        return not errors, any_success

    def _report(self, error: Exception) -> None:
        if not self._reported:
            self._reported = True
            capture(error)

    def sync(self, stop=None) -> bool:
        if not self.lock.acquire(blocking=False):
            return False
        self._reported = False
        try:
            success = self._capture(ROOT_ID)
            any_success = success
            for source_id in self.store.candidates(self.config.max_sources):
                if stop is not None and stop.is_set():
                    break
                result = self._capture(source_id)
                success = success and result
                any_success = any_success or result
            daily_success, daily_captured = self._sync_daily(stop)
            success = success and daily_success
            any_success = any_success or daily_captured
            if any_success:
                self.store.metadata("last_synced_at", utc_now(), write=True)
            return success
        finally:
            if self.config.sync_enabled:
                self.next_sync_at = (
                    datetime.now(UTC)
                    + timedelta(seconds=self.config.sync_interval_seconds)
                ).isoformat()
            self.lock.release()

    async def run(self, stop: asyncio.Event):
        while not stop.is_set():
            await asyncio.to_thread(self.sync, stop)
            try:
                await asyncio.wait_for(
                    stop.wait(), timeout=self.config.sync_interval_seconds
                )
            except TimeoutError:
                pass

    def status(self):
        sources = self.store.sources()
        errors = self.store.errors()
        return {
            "state": "syncing"
            if self.lock.locked()
            else "degraded"
            if errors
            else "idle",
            "last_synced_at": self.store.metadata("last_synced_at"),
            "next_sync_at": self.next_sync_at,
            "sync_interval_seconds": self.config.sync_interval_seconds,
            "project_count": self.store.projects(limit=1)["total"],
            "list_count": self.store.projects(kind="list", limit=1)["total"],
            "category_count": len(self.store.categories()),
            "sources_synced": sum(
                source["last_synced_at"] is not None for source in sources
            ),
            "sources_total": len(sources),
            "last_error": "; ".join(errors)[:1000] or None,
        }
