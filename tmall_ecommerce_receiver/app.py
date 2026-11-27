"""HTTP-neutral validation for Tmall Patchright e-commerce snapshots."""

from __future__ import annotations

import hmac
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .config import Settings
from .store import CollectionConflictError, CollectionStore


COLLECTION_ID_RE = re.compile(r"^[A-Za-z0-9_.+:-]{1,120}$")
JOB_DATASETS = {
    "daily": {"portal", "macro", "item_rank", "on_sale"},
    "every10": {"after_sales"},
}


@dataclass(frozen=True)
class HttpResult:
    status: int
    body: bytes
    content_type: str = "application/json; charset=utf-8"


class RequestError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def _aware_datetime(value: Any, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise RequestError("invalid_timestamp", f"{field} must be ISO 8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RequestError("invalid_timestamp", f"{field} must include timezone")
    return parsed


def _nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise RequestError("invalid_dataset", f"{field} must be a non-negative integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise RequestError("invalid_dataset", f"{field} must be a non-negative integer") from exc
    if parsed < 0:
        raise RequestError("invalid_dataset", f"{field} must be a non-negative integer")
    return parsed


def _validate_datasets(job: str, state: str, data: dict[str, Any]) -> None:
    expected = JOB_DATASETS[job]
    actual = set(data)
    if not actual.issubset(expected):
        unexpected = actual - expected
        raise RequestError(
            "invalid_datasets",
            f"unexpected datasets: {', '.join(sorted(unexpected))}",
        )
    if state == "success" and actual != expected:
        raise RequestError(
            "invalid_datasets",
            f"{job} datasets must be exactly: {', '.join(sorted(expected))}",
        )
    for name in actual:
        if not isinstance(data.get(name), dict):
            raise RequestError("invalid_dataset", f"data.{name} must be an object")

    if state != "success":
        return

    if job == "daily":
        if not isinstance(data["portal"].get("metrics"), dict):
            raise RequestError("invalid_dataset", "data.portal.metrics is required")
        if not isinstance(data["macro"].get("metrics"), dict):
            raise RequestError("invalid_dataset", "data.macro.metrics is required")
        _nonnegative_int(data["on_sale"].get("total"), "data.on_sale.total")
        record_count = _nonnegative_int(
            data["item_rank"].get("record_count"), "data.item_rank.record_count"
        )
        collected_rows = _nonnegative_int(
            data["item_rank"].get("collected_rows"), "data.item_rank.collected_rows"
        )
        if record_count != collected_rows:
            raise RequestError(
                "incomplete_item_rank",
                "item rank collected_rows must equal record_count",
            )
    else:
        metrics = data["after_sales"].get("metrics")
        if not isinstance(metrics, dict):
            raise RequestError("invalid_dataset", "data.after_sales.metrics is required")
        _nonnegative_int(
            metrics.get("待审核或待商家处理数量"),
            "data.after_sales.metrics.pending",
        )
        _nonnegative_int(
            metrics.get("即将超时售后单数量"),
            "data.after_sales.metrics.imminent",
        )


class TmallEcommerceApplication:
    def __init__(self, settings: Settings, store: CollectionStore | None = None) -> None:
        self.settings = settings
        self.store = store or CollectionStore(
            settings.database_path,
            settings.output_dir,
            legacy_store_key=settings.stores[0].store_key,
        )

    @staticmethod
    def _json(payload: dict[str, Any], status: int = 200) -> HttpResult:
        return HttpResult(
            status=status,
            body=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        )

    def _resolve_store(self, headers: dict[str, str]) -> str | None:
        """Map the supplied bearer token to its store key, else ``None``.

        Every configured store owns a distinct token, so a snapshot can never be
        attributed to the wrong store.  Comparison is constant-time per store to
        avoid leaking which prefix of a token matched.
        """
        supplied = headers.get("authorization", "")
        matched: str | None = None
        for candidate in self.settings.stores:
            expected = f"Bearer {candidate.bearer_token}"
            if hmac.compare_digest(supplied.encode(), expected.encode()):
                matched = candidate.store_key
        return matched

    def _parse_collection(self, raw_body: bytes) -> dict[str, Any]:
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RequestError("invalid_json", "body must be a UTF-8 JSON object") from exc
        if not isinstance(payload, dict):
            raise RequestError("invalid_collection", "collection root must be an object")
        if payload.get("schema_version") != 1:
            raise RequestError("invalid_schema_version", "schema_version must be 1")
        if payload.get("collector") != "tmall-patchright":
            raise RequestError("invalid_collector", "collector must be tmall-patchright")
        state = str(payload.get("state") or "").strip()
        if state not in {"success", "partial", "failed"}:
            raise RequestError("invalid_state", "state must be success, partial or failed")
        errors = payload.get("errors")
        if not isinstance(errors, list):
            raise RequestError("invalid_errors", "errors must be an array")
        if state == "success" and (payload.get("partial") is True or errors):
            raise RequestError("inconsistent_state", "success collection cannot be partial or errored")
        if state != "success" and not errors:
            raise RequestError("missing_errors", "partial or failed collection must include errors")

        collection_id = str(payload.get("collection_id") or "").strip()
        if not COLLECTION_ID_RE.fullmatch(collection_id):
            raise RequestError("invalid_collection_id", "collection_id format is invalid")
        job = str(payload.get("job") or "").strip()
        if job not in JOB_DATASETS:
            raise RequestError("invalid_job", "job must be daily or every10")
        slot = _aware_datetime(payload.get("slot_time"), "slot_time")
        _aware_datetime(payload.get("collected_at"), "collected_at")
        if job == "every10" and (slot.minute % 10 or slot.second or slot.microsecond):
            raise RequestError("invalid_slot", "every10 slot_time must be on a 10-minute boundary")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise RequestError("invalid_datasets", "data must be an object")
        _validate_datasets(job, state, data)
        return payload

    def handle(
        self,
        http_method: str,
        path: str,
        headers: dict[str, str],
        raw_body: bytes,
    ) -> HttpResult:
        if http_method == "GET" and path == "/healthz":
            return self._json({
                "status": "ok",
                "service": "tmall-ecommerce-receiver",
                "stores": list(self.settings.store_keys()),
            })
        if http_method == "OPTIONS" and path == self.settings.ingest_path:
            return self._json({"status": "ok"})
        if path != self.settings.ingest_path:
            return self._json({"status": "not_found"}, 404)
        if http_method != "POST":
            return self._json({"status": "method_not_allowed"}, 405)
        store_key = self._resolve_store(headers)
        if store_key is None:
            return self._json({"status": "rejected", "code": "unauthorized"}, 401)
        if len(raw_body) > self.settings.max_body_bytes:
            return self._json({"status": "rejected", "code": "request_too_large"}, 413)
        if "application/json" not in headers.get("content-type", "").lower():
            return self._json({"status": "rejected", "code": "unsupported_content_type"}, 415)
        try:
            payload = self._parse_collection(raw_body)
            stored = self.store.save(payload, store_key)
            return self._json(
                {
                    "status": "duplicate" if stored.duplicate else "accepted",
                    "store_key": stored.store_key,
                    "collection_id": stored.collection_id,
                    "job": stored.job,
                    "slot_time": stored.slot_time,
                    "state": stored.state,
                    "complete": stored.complete,
                }
            )
        except RequestError as exc:
            return self._json({"status": "rejected", "code": exc.code}, exc.status)
        except CollectionConflictError:
            return self._json({"status": "rejected", "code": "collection_conflict"}, 409)
        except Exception:
            return self._json({"status": "error", "code": "storage_error"}, 500)
