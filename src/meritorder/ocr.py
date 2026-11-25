"""Document OCR: ``POST /v1/ocr`` (decision 77, docs/03-routing.md#ocr).

The request and the reply are Mistral's OCR API's (``OCRRequest`` and ``OCRResponse`` in its OpenAPI spec, read 25 November
2026), so Mistral's SDK and LiteLLM's ``ocr()`` work unchanged. Two additions:

* a multipart upload (``-F file=@scan.pdf -F model=ocr``), sent on as a base64 data URI, for clients that don't speak the API;
* ``text`` in the reply, every page's markdown joined, and ``Accept: text/markdown`` for the text alone.

The rest of the detail a model gives is Mistral's own options: ``include_blocks`` (paragraph boxes), ``confidence_scores_granularity``,
``table_format``, ``extract_header``/``extract_footer``, ``include_image_base64``, and the annotation formats (structured output
read from the page, priced higher).

Priced per page (``Price.per_page``, ``per_annotated_page``). The pages a request will read are counted before it's sent
where that's possible (a PDF or image sent inline, or ``pages``), so the per-request cost limit applies; what the provider
says it processed is what's charged. A document given by URL is passed to the provider as it is: the gateway never fetches
it (a caller could otherwise make the gateway reach addresses only it can).
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import zlib
from typing import Any

MAX_PAGES = 1000  # Mistral's limit for one request: the worst case for a document whose pages can't be counted
# The fields of Mistral's OCRRequest; anything else is refused, so a typo isn't silently ignored.
FIELDS = {"model", "id", "document", "pages", "include_image_base64", "image_limit", "image_min_size", "bbox_annotation_format",
          "document_annotation_format", "document_annotation_prompt", "table_format", "extract_header", "extract_footer",
          "include_blocks", "confidence_scores_granularity"}
_BOOLS = ("include_image_base64", "extract_header", "extract_footer", "include_blocks")
_INTS = ("image_limit", "image_min_size")
_JSON = ("bbox_annotation_format", "document_annotation_format")
_DATA_URI = re.compile(r"^data:([\w.+-]+/[\w.+-]+)?(?:;[\w-]+=[^;,]*)*;base64,", re.I)
# What the bytes of an uploaded file say it is, when the upload doesn't.
_MAGIC = ((b"%PDF", "application/pdf"), (b"\x89PNG", "image/png"), (b"\xff\xd8\xff", "image/jpeg"), (b"GIF8", "image/gif"),
          (b"RIFF", "image/webp"), (b"II*\x00", "image/tiff"), (b"MM\x00*", "image/tiff"), (b"BM", "image/bmp"))
_ZIP = b"PK\x03\x04"  # docx, pptx and xlsx are zip files
_OFFICE = {"word/": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
           "ppt/": "application/vnd.openxmlformats-officedocument.presentationml.presentation"}


class OcrError(Exception):
    def __init__(self, status: int, message: str, kind: str = "invalid_request_error"):
        super().__init__(message)
        self.status, self.message, self.kind = status, message, kind


def sniff(data: bytes, declared: str | None = None) -> str:
    """A file's media type: the upload's own unless it's missing or generic, else from its first bytes."""
    if declared and declared not in ("application/octet-stream", "binary/octet-stream"):
        return declared
    for magic, mime in _MAGIC:
        if data.startswith(magic):
            return mime
    if data.startswith(_ZIP):
        head = data[:4096]
        return next((m for marker, m in _OFFICE.items() if marker.encode() in head), "application/octet-stream")
    return declared or "application/octet-stream"


def document_for(data: bytes, mime: str) -> dict:
    """An uploaded file as the document Mistral's API takes: an image or a document, as a data URI."""
    uri = f"data:{mime};base64,{base64.b64encode(data).decode()}"
    if mime.startswith("image/"):
        return {"type": "image_url", "image_url": uri}
    return {"type": "document_url", "document_url": uri}


def _form_value(name: str, value: str) -> Any:
    if name in _BOOLS:
        return value.strip().lower() in ("1", "true", "yes", "on")
    if name in _INTS:
        try:
            return int(value)
        except ValueError:
            raise OcrError(400, f"'{name}' must be a whole number")
    if name in _JSON or (name == "pages" and value.strip().startswith("[")):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            raise OcrError(400, f"'{name}' must be JSON")
    return value


async def read_request(request) -> dict:
    """The request's body: JSON (Mistral's API), or a multipart form with the file in ``file`` and the other fields as
    form fields (JSON for the annotation formats)."""
    ctype = (request.headers.get("content-type") or "").lower()
    if ctype.startswith("multipart/form-data"):
        try:
            form = await request.form(max_files=1, max_fields=len(FIELDS) + 1, max_part_size=1024 * 1024)
        except Exception as e:  # a malformed form (python-multipart's errors), or too many parts
            raise OcrError(400, f"couldn't read the form: {e}")
        body: dict = {}
        upload = form.get("file")
        for name, value in form.multi_items():
            if name == "file":
                continue
            if name not in FIELDS or name == "document":
                raise OcrError(400, f"unknown form field '{name}'; send the file as 'file'")
            body[name] = _form_value(name, str(value))
        if upload is None or isinstance(upload, str):
            raise OcrError(400, "send the file to read as the form field 'file' (curl -F file=@scan.pdf)")
        data = await upload.read()
        if not data:
            raise OcrError(400, "the uploaded file is empty")
        body["document"] = document_for(data, sniff(data, getattr(upload, "content_type", None)))
        return body
    try:
        body = json.loads(await request.body())
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise OcrError(400, "request body must be valid JSON, or a multipart form with the file as 'file'")
    if not isinstance(body, dict):
        raise OcrError(400, "request body must be a JSON object")
    return body


def validate(body: dict) -> None:
    unknown = sorted(set(body) - FIELDS)
    if unknown:
        raise OcrError(400, f"unknown field{'s' if len(unknown) > 1 else ''}: {', '.join(unknown)} (Mistral's OCR API fields only)")
    doc = body.get("document")
    if not isinstance(doc, dict):
        raise OcrError(400, "'document' is required: {\"type\": \"document_url\", \"document_url\": \"https://… or data:…;base64,…\"} "
                            "or {\"type\": \"image_url\", \"image_url\": …}")
    kind = doc.get("type")
    if kind == "file":
        raise OcrError(400, "a file id belongs to one provider's account; send the file itself (a data URI, or a multipart upload)")
    if kind not in ("document_url", "image_url"):
        raise OcrError(400, "'document.type' must be document_url or image_url")
    url = doc.get(kind)
    if isinstance(url, dict):  # Mistral also takes {"image_url": {"url": …}}
        url = url.get("url")
    if not isinstance(url, str) or not url:
        raise OcrError(400, f"'document.{kind}' must be a URL or a base64 data URI")
    if not (url.startswith(("https://", "http://")) or _DATA_URI.match(url)):
        raise OcrError(400, f"'document.{kind}' must be an http(s) URL or a base64 data URI (data:<type>;base64,…)")
    if body.get("confidence_scores_granularity") not in (None, "page", "word", "block"):
        raise OcrError(400, "'confidence_scores_granularity' must be page, word or block")
    for name in _BOOLS:
        if name in body and not isinstance(body[name], bool):
            raise OcrError(400, f"'{name}' must be true or false")
    page_list(body)  # a malformed `pages` is refused here, not by the provider


def document_url(body: dict) -> str:
    doc = body.get("document") or {}
    url = doc.get(doc.get("type") or "")
    return str(url.get("url") if isinstance(url, dict) else url or "")


def inline(body: dict) -> bool:
    """Whether the document is in the request (a data URI) rather than at a URL the provider fetches."""
    return document_url(body).startswith("data:")


def annotated(body: dict) -> bool:
    """A request asking for annotations (structured output from the page), which providers price higher."""
    return bool(body.get("bbox_annotation_format") or body.get("document_annotation_format"))


def page_list(body: dict) -> list[int] | None:
    """The pages a request asks for (0-based), from a list or Mistral's string form ("0-3,7"); None for all of them."""
    pages = body.get("pages")
    if pages is None or pages == "" or pages == []:
        return None
    out: list[int] = []
    try:
        if isinstance(pages, list):
            out = [int(p) for p in pages]
        elif isinstance(pages, str):
            for part in pages.split(","):
                part = part.strip()
                if "-" in part:
                    a, b = (int(x) for x in part.split("-", 1))
                    if b < a or b - a > MAX_PAGES:
                        raise ValueError
                    out += range(a, b + 1)
                elif part:
                    out.append(int(part))
        else:
            raise ValueError
    except (TypeError, ValueError):
        raise OcrError(400, "'pages' must be a list of page numbers (from 0) or a string like \"0-3,7\"")
    if any(p < 0 for p in out):
        raise OcrError(400, "'pages' are numbered from 0")
    return sorted(set(out))


def _decoded(url: str) -> tuple[str, bytes] | None:
    m = _DATA_URI.match(url)
    if not m:
        return None
    try:
        return (m.group(1) or "").lower(), base64.b64decode(url[m.end():], validate=False)
    except (binascii.Error, ValueError):
        raise OcrError(400, "the document's data URI isn't valid base64")


_PAGE = re.compile(rb"/Type\s*/Page(?![a-zA-Z])")
_OBJSTM = re.compile(rb"/Type\s*/ObjStm\b")
_STREAM = re.compile(rb"stream\r?\n")
_INFLATE_BUDGET = 64 * 1024 * 1024  # the most object-stream data read to count pages: a compressed bomb stops here


def pdf_pages(data: bytes) -> int | None:
    """How many pages a PDF has, near enough to price it before it's sent: its page objects, including those packed in
    compressed object streams (PDF 1.5 and later). A page rewritten by an incremental update counts twice, so this can be
    high, never low for a well-formed file. None when nothing is found (an encrypted or broken file)."""
    count = len(_PAGE.findall(data))
    budget = _INFLATE_BUDGET
    for m in _OBJSTM.finditer(data):
        s = _STREAM.search(data, m.end(), m.end() + 4096)
        end = data.find(b"endstream", s.end()) if s else -1
        if not s or end < 0:
            continue
        try:
            d = zlib.decompressobj()
            out = d.decompress(data[s.end():end], budget)
        except zlib.error:
            continue
        budget -= len(out)
        count += len(_PAGE.findall(out))
        if budget <= 0:
            break
    return count or None


def pages_to_read(body: dict) -> int | None:
    """The pages the provider will read, as near as can be told before sending: the ``pages`` asked for, an image's one,
    or an inline PDF's count. None when it can't be told (a document at a URL; an Office file)."""
    asked = page_list(body)
    total = None
    decoded = _decoded(document_url(body))
    if decoded is not None:
        mime, data = decoded
        if mime.startswith("image/") or (body.get("document") or {}).get("type") == "image_url":
            total = 1
        elif mime == "application/pdf" or data.startswith(b"%PDF"):
            total = pdf_pages(data)
    if asked is not None:
        return len([p for p in asked if total is None or p < total]) or len(asked)
    return total


def text(resp: dict) -> str:
    """Every page's markdown, in order, separated by a blank line."""
    pages = sorted((p for p in resp.get("pages") or [] if isinstance(p, dict)), key=lambda p: p.get("index") or 0)
    return "\n\n".join(str(p.get("markdown") or "").strip() for p in pages).strip()


def pages_processed(resp: dict, fallback: int | None) -> int:
    """What the provider says it read (``usage_info.pages_processed``), else the pages in the reply, else the estimate."""
    n = (resp.get("usage_info") or {}).get("pages_processed")
    if isinstance(n, int) and n >= 0:
        return n
    return len(resp.get("pages") or []) or (fallback or 0)


def finish(resp: dict, model: str) -> dict:
    """The provider's reply as the gateway's: its model name, and ``text``."""
    return {**resp, "model": model, "text": text(resp)}


INLINE_ONLY = ("/providers/mistral/azure",)  # Mistral's models in Microsoft Foundry read a document only from a data URI
CACHE_MAX_BYTES = 4 * 1024 * 1024  # a reply bigger than this (page images, say) isn't kept in the response cache


def takes_urls(backend_cfg) -> bool:
    """Whether a backend fetches a document from a URL itself. Foundry's Mistral endpoint doesn't: a document at a URL
    isn't sent there (the gateway never fetches it on the caller's behalf)."""
    return not any(part in (backend_cfg.base_url or "") for part in INLINE_ONLY)


def serves_ocr(backend) -> bool:
    """A backend the gateway can send Mistral's OCR API to: an API (or a mock) taking OpenAI-format requests through its
    own API (not Gemini's, nor the Responses API)."""
    return (hasattr(backend, "ocr") and backend.format == "openai" and backend.kind in ("api", "mock")
            and backend.cfg.api == "chat")


async def serve(gw, request):
    """``POST /v1/ocr``: an OCR model's routes, the cheapest healthy one first, with fallback, as for embeddings: a request
    names a model (or the ``ocr`` tier, the cheapest), and only backends serving that same model are tried."""
    import time
    import uuid
    from datetime import UTC, datetime

    from fastapi.responses import JSONResponse, Response

    from . import alerts, datapolicy
    from .auth import AuthError
    from .backends import BackendError
    from .config import needs_price, other_version, unpriced_message
    from .failover import check_body
    from .proxy import NEXT_BACKEND_ON, GatewayError, error_response, kind_for_status, request_tags
    from .response_cache import cache_key, wanted
    from .store import SAVINGS_FIELDS

    t0 = time.monotonic()
    headers = {k.lower(): v for k, v in request.headers.items()}
    try:
        principal = await gw.authn.authenticate(headers)
    except AuthError as e:
        return gw.reject("openai", e.status, e.message, "authentication_error")
    try:
        body = await read_request(request)
        validate(body)
    except OcrError as e:
        return gw.reject("openai", e.status, e.message, e.kind)
    as_text = "text/markdown" in (headers.get("accept") or "") or "text/plain" in (headers.get("accept") or "")
    team = gw.cfg.team(principal.team)
    requested = str(body.get("model") or "ocr")
    model = gw.cfg.model(requested)
    annotate = annotated(body)
    try:
        if model is None or model.kind != "ocr":
            raise GatewayError(404, f"OCR model '{requested}' is not configured" + ("" if body.get("model") else
                                    " (no OCR model has a price yet: see the console's Models page)"), "not_found_error")
        if not team.allows_model(model.name):
            raise GatewayError(403, f"model '{model.name}' is not allowed for team '{team.name}'", "permission_error")
        estimate = pages_to_read(body)
        await gw.admit(principal, team, 0)
    except OcrError as e:
        return gw.reject("openai", e.status, e.message, e.kind)
    except GatewayError as e:
        return gw.reject("openai", e.status, e.message, e.kind, e.headers)

    now = time.time()
    usable = [r for r in model.routes if r.backend in gw.backends and serves_ocr(gw.backends[r.backend])
              and gw.backends[r.backend].healthy(now) and team.allows_backend(r.backend)]
    if not inline(body):
        local = [r for r in usable if not takes_urls(gw.backends[r.backend].cfg)]
        usable = [r for r in usable if r not in local]
        if local and not usable:
            return gw.reject("openai", 400, f"{', '.join(r.backend for r in local)} read a document only from the request itself: send "
                                            "the file (a base64 data URI, or a multipart upload) rather than a URL", "invalid_request_error")
    if (policy := datapolicy.from_headers(headers)) is not None:  # the caller's data policy (decision 72)
        why = {r.backend: no for r in usable if (no := policy.refuses(gw.backends[r.backend].cfg))}
        usable = [r for r in usable if r.backend not in why]
        if why and not usable:
            return gw.reject("openai", 403, datapolicy.refusal(policy, why), "permission_error")
    unpriced = [r for r in usable if needs_price(r, gw.backends[r.backend].kind)]
    usable = [r for r in usable if r not in unpriced]
    if unpriced and not usable:
        return gw.reject("openai", 503, unpriced_message(model.name, unpriced), "overloaded_error")
    # The per-request limit, on the worst case: a document whose pages can't be counted may have the provider's most.
    if (max_usd := gw.cfg.safeguards.max_request_usd) is not None and usable:
        worst = estimate if estimate is not None else MAX_PAGES

        def cost_of(r) -> float:
            return worst * r.cost_price().page(annotate)

        within = [r for r in usable if cost_of(r) <= max_usd]
        if not within:
            cheapest = min(cost_of(r) for r in usable)
            how = (f"{worst:,} pages" if estimate is not None else
                   f"up to {MAX_PAGES:,} pages, since its pages can't be counted before it's read; name the 'pages' to read")
            return gw.reject("openai", 400, f"this document could cost ${cheapest:,.2f} ({how}), above the ${max_usd:,.2f} per-request "
                                            "limit", "invalid_request_error")
        usable = within
    routes = sorted(usable, key=lambda r: (not r.priced, r.cost_price().page(annotate)))

    upgraded_from = requested if body.get("model") and other_version(model, requested) else None
    out_headers = {"x-meritorder-model": model.name, **({"x-meritorder-upgraded-from": requested} if upgraded_from else {})}
    tags = request_tags(headers, principal)
    if annotate:
        tags["ocr_annotated"] = "1"

    def row(**kw) -> dict:
        r = {f: 0 for f in ("input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens", "actual_cost",
                            "served_api_cost", "baseline_cost", "list_cost", *SAVINGS_FIELDS)}
        r.update(id=uuid.uuid4(), ts=datetime.now(UTC), user_id=principal.user_id, team=principal.team, key_id=principal.key_id,
                 session_id=None, tags=tags, client_format="ocr", requested_model=requested, floor=None, served_model=model.name,
                 status=200, error=None, stream=False, upgraded=False, proxy_cache=False,
                 latency_ms=int((time.monotonic() - t0) * 1000), ttft_ms=None)
        r.update(kw)
        return r

    def reply(data: dict, extra: dict) -> Response:
        hdrs = {**out_headers, **extra}
        if as_text:
            return Response(data.get("text") or "", media_type="text/markdown; charset=utf-8", headers=hdrs)
        return JSONResponse(data, headers=hdrs)

    rc_key = None
    if wanted(team.response_cache, headers):
        rc_key = cache_key("ocr", {**body, "model": model.name}, principal.team, principal.user_id, team.response_cache)
        if (hit := await gw.state.get_json(rc_key)):
            saved = float(hit.get("cost") or 0) if "eval" not in tags else 0.0
            r = row(backend="response-cache", backend_kind="cache", upstream_model=None, pages=0, baseline_cost=saved,
                    savings_response_cache=saved)
            gw.observe(r)
            gw.spawn(gw.store.record(r))
            return reply(hit["response"], {"x-meritorder-backend": "response-cache", "x-meritorder-request-id": str(r["id"]),
                                           "x-meritorder-cost-usd": "0.000000", "x-meritorder-savings-usd": f"{saved:.6f}"})

    last: BackendError | None = None
    for route in routes[: gw.cfg.routing.max_attempts]:
        backend = gw.backends[route.backend]
        try:
            resp = await backend.ocr({**body, "model": route.model})
            check_body(resp, "openai", backend.name)
        except BackendError as e:
            last = e
            if e.retryable or e.status in NEXT_BACKEND_ON:
                gw.backend_failed(backend, route.model, e, model)
                continue
            break
        if backend.record_success(route.model):
            gw.spawn(alerts.recovered(gw.state, backend.name, None))
            gw.spawn(alerts.recovered(gw.state, backend.name, route.model))
        data = finish(resp.body or {}, model.name)
        pages = pages_processed(data, estimate)
        cost = pages * route.cost_price().page(annotate)
        r = row(upstream_model=route.model, backend=backend.name, backend_kind=backend.kind, pages=pages, actual_cost=cost,
                served_api_cost=cost, baseline_cost=cost, list_cost=cost, latency_ms=int((time.monotonic() - t0) * 1000))
        gw.observe(r, spend_keys=gw.spend_keys(principal))
        gw.spawn(gw.store.record(r))
        if rc_key and len(json.dumps(data)) <= CACHE_MAX_BYTES:
            gw.spawn(gw.state.set_json(rc_key, {"response": data, "cost": cost, "model": model.name}, team.response_cache.ttl_seconds))
        return reply(data, {"x-meritorder-backend": backend.name, "x-meritorder-request-id": str(r["id"]),
                            "x-meritorder-cost-usd": f"{cost:.8f}", "x-meritorder-pages": str(pages)})
    if last is None:
        return gw.reject("openai", 503, f"no healthy backend serves '{model.name}'", "overloaded_error")
    return error_response("openai", last.status, last.message, kind_for_status(last.status))
