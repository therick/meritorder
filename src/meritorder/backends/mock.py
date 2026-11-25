"""In-process mock backend for development and tests.

Speaks either wire format, streams realistically, simulates prompt caching
(prefix reuse within the TTL), and makes a tool call when asked to, so the
whole pipeline can be exercised with no provider keys.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from typing import AsyncIterator

from ..cache_shaping import has_cache_control
from ..config import BackendConfig
from ..formats.sse import SSEEvent, event
from ..formats.translate import text_of
from .base import Backend, UpstreamResponse


def _canon(v) -> bytes:
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"), default=str).encode()


def _tokens(v) -> int:
    """The mock provider's own tokenizer: four bytes of the request's JSON a token. Deliberately not the gateway's
    estimate (meritorder.tokens), which is what a real provider's count is compared with."""
    return max(1, len(_canon(v)) // 4)


class MockBackend(Backend):
    def __init__(self, cfg: BackendConfig):
        super().__init__(cfg)
        self._cache: dict[str, float] = {}
        self.targets: list[tuple[str | None, str | None]] = []  # (replica URL, session tag) of each request, for tests

    # ------------------------------------------------------------- simulation

    def _prefix_hashes(self, body: dict) -> list[tuple[str, int]]:
        """Hash + token estimate for each prefix length, shortest first."""
        h = hashlib.sha256()
        h.update(_canon(body.get("tools")))
        h.update(_canon(body.get("system")))
        tokens = _tokens([body.get("tools"), body.get("system")])
        # Tools and system alone are a cacheable prefix too (the usual breakpoint for one-shot calls).
        out = [(h.copy().hexdigest(), tokens)] if (body.get("tools") or body.get("system")) else []
        for m in body.get("messages") or []:
            h.update(_canon(m))
            tokens += _tokens(m)
            out.append((h.copy().hexdigest(), tokens))
        return out

    def _usage(self, body: dict, replica: str = "") -> tuple[int, int, int]:
        """Returns (uncached, cache_read, cache_write) prompt tokens. Each replica (URL) has its own cache."""
        prompt = _tokens(body)
        caching = has_cache_control(body) if self.format == "anthropic" else prompt >= 256
        if prompt < self.cfg.mock_min_cache_tokens:
            caching = False  # like Anthropic: shorter prompts aren't cached, and cost nothing extra
        if not caching:
            return prompt, 0, 0
        now = time.time()
        prefixes = self._prefix_hashes(body)
        cached = 0
        for digest, tokens in reversed(prefixes[:-1]):
            if self._cache.get(replica + digest, 0) > now:
                cached = tokens
                break
        for digest, _ in prefixes:
            self._cache[replica + digest] = now + self.cfg.cache_ttl_seconds
        written = prompt - cached if self.format == "anthropic" else 0
        return prompt - cached - written, cached, written

    def _reply(self, body: dict) -> tuple[str, dict | None]:
        msgs = body.get("messages") or []
        last = msgs[-1] if msgs else {}
        content = last.get("content")
        is_tool_result = last.get("role") == "tool" or (
            isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
        )
        text = text_of(content) if not is_tool_result else ""
        tools = body.get("tools") or []
        if tools and not is_tool_result and "tool" in text.lower():
            t = tools[0]
            name = t.get("name") or (t.get("function") or {}).get("name") or "tool"
            return "Calling a tool.", {"id": f"toolu_{uuid.uuid4().hex[:20]}", "name": name, "input": {"query": text[:80]}}
        if is_tool_result:
            return f"[{body.get('model')}] Got the tool result. Done.", None
        return f"[{body.get('model')}] You said: {text[:200]}", None

    async def embed(self, body: dict) -> UpstreamResponse:
        """Deterministic fake vectors (same text -> same vector) in the OpenAI embeddings shape."""
        inputs = body.get("input")
        inputs = [inputs] if isinstance(inputs, str) else list(inputs or [])
        data = []
        for i, text in enumerate(inputs):
            digest = hashlib.sha256(str(text).encode()).digest()
            data.append({"object": "embedding", "index": i, "embedding": [round(b / 255 - 0.5, 4) for b in digest[:16]]})
        tokens = sum(_tokens(t) for t in inputs)
        return UpstreamResponse(200, body={"object": "list", "data": data, "model": body.get("model"),
                                           "usage": {"prompt_tokens": tokens, "total_tokens": tokens}})

    async def ocr(self, body: dict) -> UpstreamResponse:
        """A fake OCR reply in Mistral's shape: a page per page of the document (an inline PDF's count, else one), each
        saying which page it is; a text file sent inline comes back as its first page's markdown."""
        from .. import ocr

        decoded = ocr._decoded(ocr.document_url(body))
        total = ocr.pages_to_read({k: v for k, v in body.items() if k != "pages"}) or 1
        wanted = ocr.page_list(body) or list(range(total))
        pages = []
        for i in [p for p in wanted if p < total] or [0]:
            md = f"# Page {i + 1}\n\n[{body.get('model')}] text of page {i + 1}"
            if i == 0 and decoded and decoded[0].startswith("text/"):
                md = decoded[1].decode("utf-8", "replace")[:2000]
            page: dict = {"index": i, "markdown": md, "images": [], "tables": [], "hyperlinks": [], "header": None, "footer": None,
                          "dimensions": {"dpi": 200, "height": 2200, "width": 1700}}
            if body.get("include_blocks"):
                page["blocks"] = [{"type": "text", "content": md, "top_left_x": 100, "top_left_y": 100, "bottom_right_x": 1600, "bottom_right_y": 300}]
            if body.get("confidence_scores_granularity"):
                page["confidence_scores"] = {"average_page_confidence_score": 0.98, "minimum_page_confidence_score": 0.9}
            pages.append(page)
        out: dict = {"pages": pages, "model": body.get("model"), "usage_info": {"pages_processed": len(pages), "doc_size_bytes": len(decoded[1]) if decoded else None}}
        if body.get("document_annotation_format"):
            out["document_annotation"] = json.dumps({"pages": len(pages)})
        return UpstreamResponse(200, body=out)

    def _refusal(self, body: dict, stream: bool, prompt: int) -> UpstreamResponse:
        """Simulate a safety refusal: Claude's stop_reason "refusal" / OpenAI's "content_filter"."""
        model = body.get("model")
        if self.format == "anthropic":
            usage = {"input_tokens": prompt, "output_tokens": 0}
            if stream:
                return UpstreamResponse(200, events=_events([
                    event({"type": "message_start", "message": {"id": "msg_refused", "type": "message", "role": "assistant", "model": model,
                                                                "content": [], "usage": usage}}, "message_start"),
                    event({"type": "message_delta", "delta": {"stop_reason": "refusal", "stop_sequence": None}, "usage": usage}, "message_delta"),
                    event({"type": "message_stop"}, "message_stop")]))
            return UpstreamResponse(200, body={"id": "msg_refused", "type": "message", "role": "assistant", "model": model, "content": [],
                                               "stop_reason": "refusal", "stop_sequence": None, "usage": usage})
        usage = {"prompt_tokens": prompt, "completion_tokens": 0, "total_tokens": prompt}
        if stream:
            return UpstreamResponse(200, events=_events([
                event({"choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]}),
                event({"choices": [{"index": 0, "delta": {}, "finish_reason": "content_filter"}]}),
                event({"choices": [], "usage": usage}), SSEEvent(data="[DONE]")]))
        return UpstreamResponse(200, body={"id": "chatcmpl-refused", "object": "chat.completion", "model": model,
                                           "choices": [{"index": 0, "message": {"role": "assistant", "content": None}, "finish_reason": "content_filter"}],
                                           "usage": usage})

    # ------------------------------------------------------------- formats

    async def send(self, body, stream, headers, base_url=None, session_tag=None):
        await asyncio.sleep(self.cfg.mock_latency_ms / 1000)
        self.targets.append((base_url, session_tag))
        uncached, read, written = self._usage(body, base_url or "")
        last = (body.get("messages") or [{}])[-1]
        if f"[refuse:{body.get('model')}]" in text_of(last.get("content")):
            return self._refusal(body, stream, uncached + read + written)
        text, tool = self._reply(body)
        out_tokens = max(1, len(text) // 4) + (12 if tool else 0)
        model = body.get("model", "mock")
        if self.format == "anthropic":
            usage = {
                "input_tokens": uncached,
                "cache_read_input_tokens": read,
                "cache_creation_input_tokens": written,
                "output_tokens": out_tokens,
            }
            if stream:
                return UpstreamResponse(200, events=self._anthropic_stream(model, text, tool, usage))
            content = [{"type": "text", "text": text}]
            if tool:
                content.append({"type": "tool_use", **tool})
            return UpstreamResponse(
                200,
                body={
                    "id": f"msg_{uuid.uuid4().hex[:24]}",
                    "type": "message",
                    "role": "assistant",
                    "model": model,
                    "content": content,
                    "stop_reason": "tool_use" if tool else "end_turn",
                    "stop_sequence": None,
                    "usage": usage,
                },
            )
        usage = {
            "prompt_tokens": uncached + read,
            "completion_tokens": out_tokens,
            "total_tokens": uncached + read + out_tokens,
            "prompt_tokens_details": {"cached_tokens": read},
        }
        if stream:
            return UpstreamResponse(200, events=self._openai_stream(model, text, tool, usage, body))
        message: dict = {"role": "assistant", "content": text}
        if tool:
            message["tool_calls"] = [
                {"id": tool["id"], "type": "function", "function": {"name": tool["name"], "arguments": json.dumps(tool["input"])}}
            ]
        return UpstreamResponse(
            200,
            body={
                "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if tool else "stop"}],
                "usage": usage,
            },
        )

    async def _anthropic_stream(self, model, text, tool, usage) -> AsyncIterator[SSEEvent]:
        start_usage = {**usage, "output_tokens": 1}
        yield event(
            {
                "type": "message_start",
                "message": {
                    "id": f"msg_{uuid.uuid4().hex[:24]}",
                    "type": "message",
                    "role": "assistant",
                    "model": model,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": start_usage,
                },
            },
            "message_start",
        )
        yield event({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}, "content_block_start")
        for word in text.split(" "):
            await asyncio.sleep(0)
            yield event({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": word + " "}}, "content_block_delta")
        yield event({"type": "content_block_stop", "index": 0}, "content_block_stop")
        if tool:
            yield event(
                {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": tool["id"], "name": tool["name"], "input": {}}},
                "content_block_start",
            )
            args = json.dumps(tool["input"])
            for i in range(0, len(args), 8):
                yield event(
                    {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": args[i : i + 8]}},
                    "content_block_delta",
                )
            yield event({"type": "content_block_stop", "index": 1}, "content_block_stop")
        yield event(
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use" if tool else "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": usage["output_tokens"]},
            },
            "message_delta",
        )
        yield event({"type": "message_stop"}, "message_stop")

    async def _openai_stream(self, model, text, tool, usage, body) -> AsyncIterator[SSEEvent]:
        cid = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        created = int(time.time())

        def chunk(delta: dict, finish: str | None = None) -> SSEEvent:
            return event({"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                          "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]})

        yield chunk({"role": "assistant", "content": ""})
        for word in text.split(" "):
            await asyncio.sleep(0)
            yield chunk({"content": word + " "})
        if tool:
            yield chunk({"tool_calls": [{"index": 0, "id": tool["id"], "type": "function", "function": {"name": tool["name"], "arguments": ""}}]})
            args = json.dumps(tool["input"])
            for i in range(0, len(args), 8):
                yield chunk({"tool_calls": [{"index": 0, "function": {"arguments": args[i : i + 8]}}]})
        yield chunk({}, "tool_calls" if tool else "stop")
        if (body.get("stream_options") or {}).get("include_usage"):
            yield event({"id": cid, "object": "chat.completion.chunk", "created": created, "model": model, "choices": [], "usage": usage})
        yield SSEEvent(data="[DONE]")


async def _events(items: list[SSEEvent]) -> AsyncIterator[SSEEvent]:
    for ev in items:
        yield ev
