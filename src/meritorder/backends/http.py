"""HTTP backends for the two wire formats every provider we support speaks, and the native APIs (OpenAI's Responses,
Gemini's generateContent) that some models need, converted to and from Chat Completions at the backend."""

from __future__ import annotations

import asyncio
import email.utils
import json
import os
import time

import httpx

from ..config import BackendConfig
from ..formats import gemini_upstream as gemini
from ..formats.assemble import anthropic_message, openai_completion, plain_content
from ..formats.responses_upstream import PROVIDER_CHAIN, chat_to_responses_request, responses_to_chat_completion, to_chat_stream
from ..formats.sse import event, iter_sse
from .auth import BackendAuthProvider
from .base import RETRYABLE_STATUS, Backend, BackendError, UpstreamResponse
from ..guards import LARGE

# The most a backend's answer may be once decompressed: the largest body the gateway itself takes (a batch file). A
# backend at a base_url an admin chose can't make a gateway hold more than that (a gzip bomb, an endless event stream).
MAX_RESPONSE_BYTES = max(n for _, n in LARGE)


async def capped(chunks, name: str, limit: int | None = None):
    """``chunks`` (decoded bytes) until they pass ``limit`` in all; then a read error, as a connection that broke off."""
    limit = MAX_RESPONSE_BYTES if limit is None else limit
    total = 0
    async for chunk in chunks:
        total += len(chunk)
        if total > limit:
            raise httpx.ReadError(f"{name}: the response is over {limit:,} bytes")
        yield chunk


async def read_capped(resp: httpx.Response, name: str) -> bytes:
    return b"".join([c async for c in capped(resp.aiter_bytes(), name)])


async def _plain_events(events):
    """An OpenAI-format stream with each delta's content as a string (see ``plain_content``)."""
    async for ev in events:
        d = ev.json()
        choices = d.get("choices") if d else None
        if choices and any(isinstance(c, dict) and isinstance((c.get("delta") or {}).get("content"), list) for c in choices):
            yield event(plain_content(d), ev.event)
        else:
            yield ev


def _lost_previous(e: BackendError) -> bool:
    """The Responses API refused a previous_response_id it doesn't have (OpenAI: 400 "Previous response with id ... not
    found", param previous_response_id)."""
    err = (e.body or {}).get("error") if isinstance(e.body, dict) else None
    param = err.get("param") if isinstance(err, dict) else None
    return e.status in (400, 404) and (param == "previous_response_id" or "previous response" in (e.message or "").lower())


async def _note_response_id(events, resp: UpstreamResponse):
    """Pass a Responses API stream through, noting the response's id from its first event (response.created)."""
    async for ev in events:
        if resp.provider_response_id is None:
            d = ev.json()
            if isinstance(d, dict) and d.get("type") in ("response.created", "response.in_progress", "response.completed"):
                resp.provider_response_id = (d.get("response") or {}).get("id") or None
        yield ev


def _error_message(body: dict | None, raw: bytes) -> str:
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])
        if isinstance(err, str):
            return err
        if body.get("message"):
            return str(body["message"])
    return raw[:500].decode(errors="replace") or "upstream error"


def retry_after(headers: httpx.Headers) -> float | None:
    """Seconds the provider asks us to wait: Retry-After (seconds or an HTTP date) or retry-after-ms."""
    ms = headers.get("retry-after-ms")
    try:
        if ms:
            return max(0.0, float(ms) / 1000)
        value = headers.get("retry-after")
        if not value:
            return None
        if value.strip().replace(".", "", 1).isdigit():
            return max(0.0, float(value))
        when = email.utils.parsedate_to_datetime(value)
        return max(0.0, when.timestamp() - time.time())
    except (TypeError, ValueError, IndexError, OverflowError):
        return None


# Anthropic's SDKs refuse a non-streaming request that could run past 10 minutes: more than 21,333 output tokens at
# their estimate of 128,000 an hour. Past that (or with no limit at all), the gateway streams from the provider and
# assembles the reply for a client that didn't ask to stream, so a long answer isn't lost to an idle-connection timeout.
LONG_REPLY_TOKENS = 21_333


def runs_long(max_tokens) -> bool:
    try:
        return max_tokens is None or int(max_tokens) > LONG_REPLY_TOKENS
    except (TypeError, ValueError):
        return True


class HTTPBackend(Backend):
    def __init__(self, cfg: BackendConfig, auth: BackendAuthProvider):
        super().__init__(cfg)
        self.auth = auth
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(cfg.timeout_seconds, connect=10.0),
            # One pool per backend, reused by every request. Each streaming response holds a connection for its
            # whole duration, so the cap is concurrent streams to this provider from one process.
            limits=httpx.Limits(max_connections=int(os.environ.get("MERITORDER_UPSTREAM_MAX_CONNECTIONS", "500")),
                                max_keepalive_connections=int(os.environ.get("MERITORDER_UPSTREAM_KEEPALIVE", "100")),
                                keepalive_expiry=30),
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def _post(self, url: str, body: dict, headers: dict[str, str], stream: bool, assemble: bool = False) -> UpstreamResponse:
        """``assemble``: a stream the gateway will read whole (see ``_collect``), so a provider that answers it with
        something other than an event stream (a JSON body, often an error reported with status 200) is read as the
        non-streaming reply it is."""
        try:
            data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
        except UnicodeEncodeError:  # a lone surrogate (half an emoji) is valid JSON but not UTF-8: send it escaped
            data = json.dumps(body, separators=(",", ":")).encode()
        hdrs = {"content-type": "application/json", **self.cfg.extra_headers, **headers}
        raw = b""
        try:
            hdrs = await self.auth.apply("POST", url, hdrs, data)
            request = self.client.build_request("POST", url, content=data, headers=hdrs)
            limit = self.cfg.first_byte_timeout() if stream else None
            # Always streamed, so every body is read through read_capped (a whole one too, below).
            resp = await (asyncio.wait_for(self.client.send(request, stream=True), limit) if limit
                          else self.client.send(request, stream=True))
            if not stream or resp.status_code >= 400:
                try:
                    raw = await read_capped(resp, self.name)
                finally:
                    await resp.aclose()
        except httpx.ConnectTimeout:
            raise BackendError(504, f"{self.name}: request timed out", retryable=True, unreachable=True)
        except (httpx.TimeoutException, asyncio.TimeoutError):
            raise BackendError(504, f"{self.name}: request timed out", retryable=True)
        except httpx.ConnectError as e:
            raise BackendError(502, f"{self.name}: {type(e).__name__}: {e}", retryable=True, unreachable=True)
        except httpx.HTTPError as e:
            raise BackendError(502, f"{self.name}: {type(e).__name__}: {e}", retryable=True)
        except Exception as e:  # credential errors and the like
            raise BackendError(502, f"{self.name}: {type(e).__name__}: {e}", retryable=True)

        if resp.status_code >= 400:
            try:
                err_body = json.loads(raw)
            except json.JSONDecodeError:
                err_body = None
            raise BackendError(
                resp.status_code,
                f"{self.name}: {_error_message(err_body, raw)}",
                body=err_body if isinstance(err_body, dict) else None,
                retryable=resp.status_code in RETRYABLE_STATUS,
                retry_after=retry_after(resp.headers),
            )
        headers_out = {k.lower(): v for k, v in resp.headers.items()}
        if stream and assemble and "event-stream" not in headers_out.get("content-type", ""):
            stream = False
            try:
                raw = await read_capped(resp, self.name)
            except httpx.HTTPError as e:
                raise BackendError(502, f"{self.name}: {type(e).__name__}: {e}", retryable=True)
            finally:
                await resp.aclose()
        elif stream:
            events = iter_sse(capped(resp.aiter_bytes(), self.name))
            return UpstreamResponse(resp.status_code, headers_out, events=_plain_events(events) if self.format == "openai" else events,
                                    _close=resp.aclose)
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            raise BackendError(502, f"{self.name}: upstream returned non-JSON body", retryable=True)
        if self.format == "openai" and isinstance(parsed, dict):
            plain_content(parsed)
        return UpstreamResponse(resp.status_code, headers_out, body=parsed)

    async def _collect(self, resp: UpstreamResponse) -> UpstreamResponse:
        """Read a whole stream and return it as the single response a non-streaming request gets."""
        from ..failover import error_in  # noqa: PLC0415 (failover imports the backend package)

        assert resp.events is not None
        events: list[dict] = []
        try:
            async for ev in resp.events:
                d = ev.json()
                if d is None:
                    continue
                found = error_in(d, self.format)
                if found:
                    status, message = found
                    raise BackendError(status, f"{self.name}: {message}", retryable=status >= 500 or status in (408, 429))
                events.append(d)
        except httpx.HTTPError as e:
            raise BackendError(502, f"{self.name}: the stream broke off: {type(e).__name__}: {e}", retryable=True)
        finally:
            await resp.aclose()
        body = anthropic_message(events) if self.format == "anthropic" else openai_completion(events)
        return UpstreamResponse(resp.status, resp.headers, body=body)


class AnthropicBackend(HTTPBackend):
    """Anthropic Messages API: Anthropic, Claude in Amazon Bedrock, Foundry, Vertex AI."""

    def _base(self, base_url: str | None = None) -> str:
        return (base_url or self.cfg.base_url).rstrip("/")

    def _url(self, model: str, stream: bool, base_url: str | None = None) -> str:
        if self.cfg.flavor == "vertex":
            region = self.cfg.region or "global"
            host = self.cfg.base_url.rstrip("/") or (
                "https://aiplatform.googleapis.com" if region == "global" else f"https://{region}-aiplatform.googleapis.com"
            )
            method = "streamRawPredict" if stream else "rawPredict"
            return f"{host}/v1/projects/{self.cfg.project}/locations/{region}/publishers/anthropic/models/{model}:{method}"
        return f"{self._base(base_url)}/v1/messages"

    def _headers(self, client_headers: dict[str, str]) -> dict[str, str]:
        out: dict[str, str] = {}
        if self.cfg.flavor != "vertex":
            out["anthropic-version"] = self.cfg.anthropic_version or client_headers.get("anthropic-version") or "2023-06-01"
        beta = client_headers.get("anthropic-beta")
        if beta and self.cfg.forward_anthropic_beta:
            allow = self.cfg.anthropic_betas
            if allow is not None:  # only listed betas (prefix match), e.g. to keep premium-priced features off
                beta = ",".join(b for b in (x.strip() for x in beta.split(",")) if any(b.startswith(a) for a in allow))
            if beta:
                out["anthropic-beta"] = beta
        return out

    async def send(self, body, stream, headers, base_url=None, session_tag=None):
        body = dict(body)
        model = body.get("model", "")
        if self.cfg.flavor == "vertex":
            body.pop("model", None)
            body["anthropic_version"] = "vertex-2023-10-16"
        long = not stream and body.get("max_tokens") is not None and runs_long(body["max_tokens"])  # the gateway always sets it
        if stream or long:
            body["stream"] = True
        else:
            body.pop("stream", None)
        hdrs = {**self._headers(headers), **({"x-session-id": session_tag} if session_tag else {})}
        resp = await self._post(self._url(model, stream or long, base_url), body, hdrs, stream or long, assemble=long)
        return await self._collect(resp) if long and resp.events is not None else resp

    async def count_tokens(self, body, headers):
        if self.cfg.flavor == "vertex":
            return None
        keep = ("model", "messages", "system", "tools", "tool_choice", "thinking")
        payload = {k: body[k] for k in keep if k in body}
        resp = await self._post(f"{self._base()}/v1/messages/count_tokens", payload, self._headers(headers), False)
        return resp.body


class OpenAIBackend(HTTPBackend):
    """OpenAI-compatible Chat Completions: OpenAI, Azure, OpenRouter, vLLM, SGLang, and most others."""

    async def send(self, body, stream, headers, base_url=None, session_tag=None):
        body = dict(body)
        # Provider APIs only: a self-hosted server has no idle-connection limit to worry about.
        limit = body.get("max_tokens") or body.get("max_completion_tokens") or body.get(self.cfg.max_tokens_field)
        long = not stream and self.kind == "api" and runs_long(limit)
        if self.cfg.api == "responses":
            resp = await self._send_responses(body, stream or long, assemble=long)
            if long and resp.events is not None:
                whole = await self._collect(resp)
                whole.provider_response_id, whole.provider_chained = resp.provider_response_id, resp.provider_chained
                return whole
            return resp
        if stream or long:
            body["stream"] = True
            options = dict(body.get("stream_options") or {})
            options["include_usage"] = True
            body["stream_options"] = options
        else:
            body.pop("stream", None)
            body.pop("stream_options", None)
        field = self.cfg.max_tokens_field
        if field != "max_tokens" and "max_tokens" in body:
            body[field] = body.pop("max_tokens")
        base = (base_url or self.cfg.base_url).rstrip("/")
        hdrs = {"x-session-id": session_tag} if session_tag else {}  # vLLM tags the session's KV events with it
        resp = await self._post(f"{base}/chat/completions", body, hdrs, stream or long, assemble=long)
        return await self._collect(resp) if long and resp.events is not None else resp

    async def _send_responses(self, body: dict, stream: bool, assemble: bool = False) -> UpstreamResponse:
        body["stream"] = stream
        url = f"{self.cfg.base_url.rstrip('/')}/responses"
        request = chat_to_responses_request(body)
        try:
            resp = await self._post(url, request, {}, stream, assemble)
        except BackendError as e:
            if not (request.get("previous_response_id") and _lost_previous(e)):
                raise
            # The provider no longer has the turn before (it expired there, was deleted, or the account keeps nothing):
            # the gateway holds the whole conversation, so send all of it.
            request = chat_to_responses_request({**body, PROVIDER_CHAIN: {**body[PROVIDER_CHAIN], "previous_response_id": None}})
            resp = await self._post(url, request, {}, stream, assemble)
        resp.provider_chained = bool(request.get("previous_response_id"))
        if resp.events is not None:
            resp.events = to_chat_stream(_note_response_id(resp.events, resp) if request.get("store") else resp.events)
        else:
            if request.get("store"):
                resp.provider_response_id = (resp.body or {}).get("id")
            resp.body = responses_to_chat_completion(resp.body or {})
        return resp

    async def delete_response(self, response_id: str) -> bool:
        """Delete a response this provider stored for the gateway (``responses_store``). False: it no longer has it."""
        url = f"{self.cfg.base_url.rstrip('/')}/responses/{response_id}"
        try:
            hdrs = await self.auth.apply("DELETE", url, dict(self.cfg.extra_headers), b"")
            resp = await self.client.request("DELETE", url, headers=hdrs)
        except httpx.HTTPError as e:
            raise BackendError(502, f"{self.name}: {type(e).__name__}: {e}", retryable=True)
        if resp.status_code == 404:
            return False
        if resp.status_code >= 400:
            raise BackendError(resp.status_code, f"{self.name}: deleting response {response_id}: {resp.text[:300]}",
                               retryable=resp.status_code in RETRYABLE_STATUS)
        return True

    async def embed(self, body: dict) -> UpstreamResponse:
        return await self._post(f"{self.cfg.base_url.rstrip('/')}/embeddings", body, {}, False)

    async def ocr(self, body: dict) -> UpstreamResponse:
        """Mistral's OCR API (``/ocr`` under the base URL): Mistral's own (``https://api.mistral.ai/v1``) and Mistral's models in
        Microsoft Foundry (``https://<resource>.services.ai.azure.com/providers/mistral/azure``)."""
        return await self._post(f"{self.cfg.base_url.rstrip('/')}/ocr", body, {}, False)


class GeminiBackend(HTTPBackend):
    """Gemini's native API (``api: gemini``): the Gemini API with a key, or Vertex AI (``flavor: vertex``) with Google
    sign-in. The gateway hands it Chat Completions like any OpenAI-format backend; formats/gemini_upstream.py converts
    the request, the reply and the stream. On the Gemini API it also takes embeddings (``batchEmbedContents``)."""

    async def embed(self, body: dict) -> UpstreamResponse:
        """An OpenAI embeddings request through the Gemini API's ``batchEmbedContents``: one Gemini request per input, the
        reply as OpenAI's. Without a token count in the reply, the gateway's estimate of the input stands in for it."""
        from ..tokens import count

        if self.cfg.flavor == "vertex":
            raise BackendError(400, f"{self.name}: embeddings on Vertex AI aren't built here; use the Gemini API")
        model = gemini.model_id(str(body.get("model") or ""))
        reqs = [gemini.embed_request(text, {**body, "model": model}) for text in gemini.embed_inputs(body)]
        resp = await self._post(f"{self.cfg.base_url.rstrip('/')}/models/{model}:batchEmbedContents", {"requests": reqs}, {}, False)
        d = resp.body or {}
        out = d.get("embeddings") or []
        tokens = (d.get("usageMetadata") or {}).get("promptTokenCount")
        resp.body = gemini.embeddings_reply(out, str(body.get("model") or model), int(tokens) if tokens else count(body.get("input")))
        return resp

    def _url(self, model: str, stream: bool) -> str:
        method = "streamGenerateContent?alt=sse" if stream else "generateContent"
        model = gemini.model_id(model)
        if self.cfg.flavor == "vertex":
            region = self.cfg.region or "global"
            host = self.cfg.base_url.rstrip("/") or (
                "https://aiplatform.googleapis.com" if region == "global" else f"https://{region}-aiplatform.googleapis.com"
            )
            return f"{host}/v1/projects/{self.cfg.project}/locations/{region}/publishers/google/models/{model}:{method}"
        return f"{self.cfg.base_url.rstrip('/')}/models/{model}:{method}"

    async def send(self, body, stream, headers):
        model = str(body.get("model") or "")
        long = not stream and runs_long(body.get("max_completion_tokens") or body.get("max_tokens"))
        try:
            resp = await self._post(self._url(model, stream or long), gemini.chat_to_gemini_request(body), {}, stream or long, assemble=long)
        except BackendError as e:
            if e.retry_after is None:
                e.retry_after = gemini.retry_delay(e.body)  # Google puts it in the error's details, not a header
            raise
        if resp.events is not None:
            resp.events = gemini.to_chat_stream(resp.events, model)
            return await self._collect(resp) if long else resp
        resp.body = gemini.gemini_to_chat_completion(resp.body or {}, model)
        return resp
