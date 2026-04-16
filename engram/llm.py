"""LLM providers for analysis — OpenRouter, DeepSeek, or local Claude Code CLI."""

import json
import os
import subprocess
import urllib.request


def _parse_json(raw: str) -> dict:
    if not raw:
        raise ValueError("Empty response from LLM")
    content = raw.strip()
    if content.startswith("```"):
        content = content.split("```")[1]
        if content.startswith("json"):
            content = content[4:]
    return json.loads(content.strip())


def _resolve_provider() -> tuple[str, str, str, str, dict]:
    """Returns (provider_name, api_key, base_url, model, extra_headers)."""
    provider = os.environ.get("LLM_PROVIDER", "auto")
    openrouter_key = os.environ.get("OPENROUTER_API_KEY", "")
    deepseek_key = os.environ.get("DEEPSEEK_API_KEY", "")
    model = os.environ.get("LLM_MODEL", "")

    if provider == "claude-code":
        return "claude-code", "", "", model or "sonnet", {}
    if provider == "openrouter" or (provider == "auto" and openrouter_key):
        return "openrouter", openrouter_key, "https://openrouter.ai/api/v1", model or "deepseek/deepseek-chat", {}
    if provider == "deepseek" or (provider == "auto" and deepseek_key):
        return "deepseek", deepseek_key, "https://api.deepseek.com", model or "deepseek-chat", {}
    return "none", "", "", "", {}


def _call_claude_code(prompt: str, model: str) -> dict:
    system = "You are a precise JSON-outputting analysis engine. Output only valid JSON, no markdown fencing, no explanation."
    result = subprocess.run(
        [
            "claude", "-p", prompt,
            "--output-format", "json",
            "--system-prompt", system,
            "--model", model,
            "--dangerously-skip-permissions",
        ],
        capture_output=True, text=True, timeout=300,
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude CLI failed (exit {result.returncode}): {result.stderr[:300]}")
    try:
        wrapper = json.loads(result.stdout)
        content = wrapper.get("result", result.stdout)
        if isinstance(content, str):
            return _parse_json(content)
        return content
    except (json.JSONDecodeError, TypeError):
        return _parse_json(result.stdout)


def _call_api(prompt: str, api_key: str, base_url: str, model: str, extra_headers: dict) -> dict:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a precise JSON-outputting analysis engine. Output only valid JSON, no markdown fencing."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        "max_tokens": 8000,
    }
    headers = {
        "content-type": "application/json",
        "authorization": f"Bearer {api_key}",
        **extra_headers,
    }
    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(payload).encode(),
        headers=headers,
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        result = json.loads(r.read().decode())
    msg = result["choices"][0]["message"]
    # Reasoning models (R1 etc.) sometimes return content=null with response in reasoning_content
    content = msg.get("content") or msg.get("reasoning_content") or ""
    return _parse_json(content)


def call(prompt: str, model_override: str = "") -> dict:
    provider, api_key, base_url, model, extra = _resolve_provider()
    if model_override:
        model = model_override
    if provider == "none":
        raise RuntimeError("No LLM provider set -- use LLM_PROVIDER=claude-code, or set OPENROUTER_API_KEY or DEEPSEEK_API_KEY")
    if provider == "claude-code":
        return _call_claude_code(prompt, model)
    return _call_api(prompt, api_key, base_url, model, extra)
