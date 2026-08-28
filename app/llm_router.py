"""
LLMRouter: single entry point for running a node's persona against an LLM.
Supports Claude via the Anthropic SDK; DeepSeek/Ollama can be wired in the
same pattern (both expose OpenAI-compatible chat endpoints).

If no API key is configured, falls back to a stub responder so the backend
is runnable out of the box without any keys set.

Every provider also has a streaming variant (run_stream) yielding text
deltas as they arrive, used by the node run-stream endpoint so the UI
can show tokens live instead of a spinner-then-dump.
"""
import os
import time
from typing import Iterator


DEFAULT_MAX_TOKENS = 4096


class LLMRouter:
    def __init__(self):
        self.provider = os.environ.get("LLM_PROVIDER", "stub")  # claude | deepseek | ollama | stub
        self.anthropic_key = os.environ.get("ANTHROPIC_API_KEY")

    def run(self, persona_md: str, input_text: str, max_tokens: int | None = None) -> str:
        """Non-streaming — collects the full response. Kept around for
        any caller that just wants the final text (e.g. epic/task
        extraction) without dealing with a generator."""
        return "".join(self.run_stream(persona_md, input_text, max_tokens))

    def run_stream(self, persona_md: str, input_text: str, max_tokens: int | None = None) -> Iterator[str]:
        max_tokens = max_tokens or DEFAULT_MAX_TOKENS
        if self.provider == "claude" and self.anthropic_key:
            yield from self._stream_claude(persona_md, input_text, max_tokens)
            return
        if self.provider == "deepseek" and os.environ.get("DEEPSEEK_API_KEY"):
            yield from self._stream_openai_compatible(
                persona_md, input_text,
                base_url="https://api.deepseek.com/v1",
                api_key=os.environ["DEEPSEEK_API_KEY"],
                model=os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash"),
                max_tokens=max_tokens,
            )
            return
        if self.provider == "ollama":
            yield from self._stream_ollama(persona_md, input_text, max_tokens)
            return
        yield from self._stream_stub(persona_md, input_text)

    def _stream_claude(self, persona_md: str, input_text: str, max_tokens: int) -> Iterator[str]:
        import anthropic
        client = anthropic.Anthropic(api_key=self.anthropic_key)
        with client.messages.stream(
            model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
            max_tokens=max_tokens,
            system=persona_md,
            messages=[{"role": "user", "content": input_text}],
        ) as stream:
            for text in stream.text_stream:
                yield text

    def _stream_openai_compatible(self, persona_md, input_text, base_url, api_key, model, max_tokens: int) -> Iterator[str]:
        import urllib.request, urllib.error, json
        payload = json.dumps({
            "model": model,
            "max_tokens": max_tokens,
            "stream": True,
            "messages": [
                {"role": "system", "content": persona_md},
                {"role": "user", "content": input_text},
            ],
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{base_url}/chat/completions", data=payload, method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        )
        try:
            resp = urllib.request.urlopen(req, timeout=120)
        except urllib.error.HTTPError as e:
            # e's own str() is just "HTTP Error 400: Bad Request" — the
            # provider's actual reason (bad model name, context length
            # exceeded, malformed message, etc.) is in the response body.
            body = e.read().decode("utf-8", "ignore")
            raise RuntimeError(f"{base_url} returned {e.code}: {body[:500]}") from None
        with resp:
            for raw_line in resp:
                line = raw_line.decode("utf-8", "ignore").strip()
                if not line or not line.startswith("data:"):
                    continue
                data = line[len("data:"):].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except ValueError:
                    continue
                delta = chunk.get("choices", [{}])[0].get("delta", {}).get("content")
                if delta:
                    yield delta

    def _stream_ollama(self, persona_md: str, input_text: str, max_tokens: int) -> Iterator[str]:
        import urllib.request, json
        host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
        model = os.environ.get("OLLAMA_MODEL", "llama3")
        payload = json.dumps({
            "model": model,
            "messages": [
                {"role": "system", "content": persona_md},
                {"role": "user", "content": input_text},
            ],
            "stream": True,
            "options": {"num_predict": max_tokens},
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{host}/api/chat", data=payload, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            for raw_line in resp:
                line = raw_line.decode("utf-8", "ignore").strip()
                if not line:
                    continue
                try:
                    chunk = json.loads(line)
                except ValueError:
                    continue
                delta = chunk.get("message", {}).get("content")
                if delta:
                    yield delta
                if chunk.get("done"):
                    break

    def _stream_stub(self, persona_md: str, input_text: str) -> Iterator[str]:
        """No API key configured — still stream, word by word, with a
        tiny delay, so the streaming UI is exercisable (and demoable)
        without needing real credentials."""
        first_line = next(
            (l for l in persona_md.splitlines()
             if l.strip() and not l.strip().startswith("---")),
            "Agent"
        )
        full = (
            f"[STUB OUTPUT — no LLM_PROVIDER/API key configured]\n\n"
            f"Persona head: {first_line}\n\n"
            f"Received input ({len(input_text)} chars):\n"
            f"{input_text[:500]}\n\n"
            f"Set LLM_PROVIDER=claude and ANTHROPIC_API_KEY to get real output."
        )
        words = full.split(" ")
        for i, w in enumerate(words):
            yield w + (" " if i < len(words) - 1 else "")
            time.sleep(0.012)


llm_router = LLMRouter()
