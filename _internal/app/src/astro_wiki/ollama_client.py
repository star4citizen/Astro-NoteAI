from __future__ import annotations

import json
from typing import Any, Callable
from urllib.parse import quote

import httpx

from .config import (
    api_provider_config,
    chat_model,
    is_api_provider,
    llm_provider,
    ollama_base_url,
    openai_api_key,
    openai_base_url,
)


class OllamaError(RuntimeError):
    pass


JSON_ONLY_INSTRUCTION = "Return only a valid JSON object. Do not wrap it in Markdown or add extra prose."


def _string_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                if "text" in item:
                    parts.append(str(item.get("text") or ""))
                elif item.get("type") == "text":
                    parts.append(str(item.get("text") or ""))
            elif item is not None:
                parts.append(str(item))
        return "\n".join(part for part in parts if part)
    return "" if content is None else str(content)


def _json_prompt_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    updated = [dict(message) for message in messages]
    for message in reversed(updated):
        if str(message.get("role") or "").lower() == "user":
            content = _string_content(message.get("content")).rstrip()
            message["content"] = f"{content}\n\n{JSON_ONLY_INSTRUCTION}" if content else JSON_ONLY_INSTRUCTION
            return updated
    updated.append({"role": "user", "content": JSON_ONLY_INSTRUCTION})
    return updated


def _compact_error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
        text = exc.response.text.strip().replace("\n", " ")
        detail = f"HTTP {exc.response.status_code}"
        return f"{detail}: {text[:500]}" if text else detail
    return str(exc)


def _is_terminal_api_error(exc: Exception) -> bool:
    return isinstance(exc, httpx.HTTPStatusError) and exc.response is not None and exc.response.status_code in {401, 403, 429}


def _provider_setting(key: str, default: Any = None) -> Any:
    return api_provider_config().get(key, default)


def _configured_model_name(model: str | None) -> str:
    return str(model if model is not None else chat_model()).strip()


def _openai_compatible_model_name(model: str, provider: str | None = None) -> str:
    if str(provider or llm_provider()).strip() == "gemini" and model.startswith("models/"):
        return model.split("/", 1)[1]
    return model


def _is_gemini_thinking_model(model: str) -> bool:
    model_name = model.lower().removeprefix("models/")
    return model_name.startswith("gemini-3") or model_name.startswith("gemini-2.5")


def _int_setting(key: str, default: int) -> int:
    try:
        return int(_provider_setting(key, default))
    except (TypeError, ValueError):
        return default


def _request_options(provider: str, model: str | None, options: dict[str, Any] | None) -> dict[str, Any] | None:
    if provider != "gemini" or not options or "num_predict" not in options:
        return options
    resolved_model = _configured_model_name(model)
    if not _is_gemini_thinking_model(resolved_model):
        return options
    updated = dict(options)
    try:
        current = int(updated.get("num_predict") or 0)
    except (TypeError, ValueError):
        current = 0
    updated["num_predict"] = max(current, _int_setting("min_output_tokens_for_thinking", 1024))
    return updated


def _openai_options(options: dict[str, Any] | None, *, max_token_field: str = "max_tokens") -> dict[str, Any]:
    if not options:
        return {}
    payload: dict[str, Any] = {}
    if "num_predict" in options:
        payload[max_token_field] = options["num_predict"]
    if "temperature" in options:
        payload["temperature"] = options["temperature"]
    if "top_p" in options:
        payload["top_p"] = options["top_p"]
    if "stop" in options:
        payload["stop"] = options["stop"]
    if "reasoning_effort" in options:
        payload["reasoning_effort"] = options["reasoning_effort"]
    return payload


def _openai_headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    api_key = openai_api_key()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _openai_chat_once(
    messages: list[dict[str, str]],
    model: str | None,
    *,
    format_json: bool,
    timeout: float,
    options: dict[str, Any] | None,
    reasoning_effort: str | None = None,
    max_token_field: str = "max_tokens",
    include_response_format: bool = True,
    minimal_options: bool = False,
) -> str:
    provider = llm_provider()
    resolved_model = _openai_compatible_model_name(_configured_model_name(model), provider)
    payload_options = {} if minimal_options else _openai_options(options, max_token_field=max_token_field)
    if reasoning_effort and "reasoning_effort" not in payload_options:
        payload_options["reasoning_effort"] = reasoning_effort
    payload: dict[str, Any] = {
        "messages": messages,
        "stream": False,
        **payload_options,
    }
    if resolved_model:
        payload["model"] = resolved_model
    if format_json and include_response_format:
        payload["response_format"] = {"type": "json_object"}
    with httpx.Client(timeout=timeout) as client:
        response = client.post(
            f"{openai_base_url().rstrip('/')}/chat/completions",
            headers=_openai_headers(),
            json=payload,
        )
        response.raise_for_status()
    data = response.json()
    try:
        choice = data["choices"][0]
        content = str(choice["message"].get("content") or "")
    except (KeyError, IndexError, TypeError) as exc:
        raise OllamaError(f"Unexpected OpenAI-compatible response: {json.dumps(data)[:500]}") from exc
    finish_reason = str(choice.get("finish_reason") or "").lower()
    if provider == "gemini" and finish_reason in {"length", "max_tokens"}:
        raise OllamaError("Gemini response hit the output token limit before a complete answer was returned.")
    if format_json:
        try:
            json.loads(content)
        except json.JSONDecodeError as exc:
            raise OllamaError("OpenAI-compatible response was not valid JSON.") from exc
    return content


def _gemini_native_base_url() -> str:
    configured = str(_provider_setting("native_base_url") or "").strip()
    if configured:
        return configured.rstrip("/")
    base_url = openai_base_url().rstrip("/")
    if base_url.endswith("/openai"):
        return base_url[: -len("/openai")]
    return "https://generativelanguage.googleapis.com/v1beta"


def _gemini_model_path(model: str) -> str:
    model = model.strip()
    if model.startswith("models/"):
        return quote(model, safe="/")
    return f"models/{quote(model, safe='')}"


def _gemini_native_contents(messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    contents: list[dict[str, Any]] = []
    system_parts: list[dict[str, str]] = []
    for message in messages:
        role = str(message.get("role") or "user").lower()
        text = _string_content(message.get("content")).strip()
        if not text:
            continue
        if role == "system":
            system_parts.append({"text": text})
            continue
        contents.append(
            {
                "role": "model" if role == "assistant" else "user",
                "parts": [{"text": text}],
            }
        )
    if not contents:
        contents.append({"role": "user", "parts": [{"text": ""}]})
    system_instruction = {"parts": system_parts} if system_parts else None
    return contents, system_instruction


def _gemini_thinking_config(model: str) -> dict[str, Any]:
    lower_model = model.lower()
    if "2.5" in lower_model:
        budget = _provider_setting("thinking_budget", None)
        if budget is None:
            budget = 1024
        try:
            budget = int(budget)
        except (TypeError, ValueError):
            budget = 1024
        return {"thinkingBudget": budget}
    return {"thinkingLevel": str(_provider_setting("thinking_level", "low"))}


def _gemini_native_generation_config(
    model: str,
    *,
    format_json: bool,
    options: dict[str, Any] | None,
) -> dict[str, Any]:
    config: dict[str, Any] = {"thinkingConfig": _gemini_thinking_config(model)}
    options = options or {}
    if "num_predict" in options:
        config["maxOutputTokens"] = options["num_predict"]
    if "temperature" in options:
        config["temperature"] = options["temperature"]
    if "top_p" in options:
        config["topP"] = options["top_p"]
    if "stop" in options:
        config["stopSequences"] = options["stop"]
    if format_json:
        config["responseMimeType"] = "application/json"
    return config


def _gemini_generate_content(
    messages: list[dict[str, Any]],
    model: str | None,
    *,
    format_json: bool,
    timeout: float,
    options: dict[str, Any] | None,
) -> str:
    resolved_model = _configured_model_name(model)
    if not resolved_model:
        raise OllamaError("Gemini model is not configured.")
    request_messages = _json_prompt_messages(messages) if format_json else [dict(message) for message in messages]
    contents, system_instruction = _gemini_native_contents(request_messages)
    payload: dict[str, Any] = {
        "contents": contents,
        "generationConfig": _gemini_native_generation_config(
            resolved_model,
            format_json=format_json,
            options=options,
        ),
    }
    if system_instruction:
        payload["systemInstruction"] = system_instruction
    headers = {"Content-Type": "application/json"}
    api_key = openai_api_key()
    if api_key:
        headers["x-goog-api-key"] = api_key
    url = f"{_gemini_native_base_url()}/{_gemini_model_path(resolved_model)}:generateContent"
    with httpx.Client(timeout=timeout) as client:
        response = client.post(url, headers=headers, json=payload)
        response.raise_for_status()
    data = response.json()
    try:
        candidate = data["candidates"][0]
        parts = candidate["content"].get("parts", [])
        content = "".join(str(part.get("text") or "") for part in parts if isinstance(part, dict))
    except (KeyError, IndexError, TypeError) as exc:
        raise OllamaError(f"Unexpected Gemini response: {json.dumps(data)[:500]}") from exc
    if str(candidate.get("finishReason") or "").upper() == "MAX_TOKENS":
        raise OllamaError("Gemini response hit the output token limit before a complete answer was returned.")
    if format_json:
        try:
            json.loads(content)
        except json.JSONDecodeError as exc:
            raise OllamaError("Gemini response was not valid JSON.") from exc
    return content


def _responses_input(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, str]]]:
    instructions: list[str] = []
    input_messages: list[dict[str, str]] = []
    for message in messages:
        role = str(message.get("role") or "user").lower()
        content = _string_content(message.get("content")).strip()
        if not content:
            continue
        if role == "system":
            instructions.append(content)
            continue
        input_messages.append(
            {
                "role": "assistant" if role == "assistant" else "user",
                "content": content,
            }
        )
    return "\n\n".join(instructions), input_messages


def _responses_options(options: dict[str, Any] | None) -> dict[str, Any]:
    if not options:
        return {}
    payload: dict[str, Any] = {}
    if "num_predict" in options:
        payload["max_output_tokens"] = options["num_predict"]
    if "temperature" in options:
        payload["temperature"] = options["temperature"]
    if "top_p" in options:
        payload["top_p"] = options["top_p"]
    return payload


def _openai_responses_once(
    messages: list[dict[str, Any]],
    model: str | None,
    *,
    format_json: bool,
    timeout: float,
    options: dict[str, Any] | None,
    reasoning_effort: str | None = None,
) -> str:
    resolved_model = _configured_model_name(model)
    request_messages = _json_prompt_messages(messages) if format_json else messages
    instructions, input_messages = _responses_input(request_messages)
    payload: dict[str, Any] = {
        "input": input_messages or [{"role": "user", "content": ""}],
        **_responses_options(options),
    }
    if resolved_model:
        payload["model"] = resolved_model
    if instructions:
        payload["instructions"] = instructions
    if reasoning_effort:
        payload["reasoning"] = {"effort": reasoning_effort}
    with httpx.Client(timeout=timeout) as client:
        response = client.post(
            f"{openai_base_url().rstrip('/')}/responses",
            headers=_openai_headers(),
            json=payload,
        )
        response.raise_for_status()
    data = response.json()
    if data.get("output_text"):
        return str(data.get("output_text") or "")
    output_parts: list[str] = []
    for item in data.get("output", []):
        if not isinstance(item, dict):
            continue
        for content in item.get("content", []):
            if isinstance(content, dict) and "text" in content:
                output_parts.append(str(content.get("text") or ""))
    if output_parts:
        return "".join(output_parts)
    raise OllamaError(f"Unexpected OpenAI Responses response: {json.dumps(data)[:500]}")


def _run_api_attempts(attempts: list[tuple[str, Callable[[], str]]], provider: str, model: str | None) -> str:
    errors: list[str] = []
    for label, attempt in attempts:
        try:
            return attempt()
        except Exception as exc:
            errors.append(f"{label}: {_compact_error(exc)}")
            if _is_terminal_api_error(exc):
                break
    model_name = str(model if model is not None else chat_model()).strip() or "(default model)"
    details = "\n".join(f"- {error}" for error in errors)
    raise OllamaError(f"API call failed for provider '{provider}' and model '{model_name}' after fallback attempts:\n{details}")


def _openai_chat(
    messages: list[dict[str, str]],
    model: str | None,
    *,
    format_json: bool,
    timeout: float,
    options: dict[str, Any] | None,
) -> str:
    provider = llm_provider()
    request_options = _request_options(provider, model, options)
    reasoning_effort = str(_provider_setting("reasoning_effort") or "").strip() or None
    attempts: list[tuple[str, Callable[[], str]]] = [
        (
            "openai-compatible",
            lambda: _openai_chat_once(
                messages,
                model,
                format_json=format_json,
                timeout=timeout,
                options=request_options,
                reasoning_effort=reasoning_effort,
            ),
        )
    ]
    if format_json:
        prompted_messages = _json_prompt_messages(messages)
        attempts.append(
            (
                "openai-compatible-json-prompt",
                lambda: _openai_chat_once(
                    prompted_messages,
                    model,
                    format_json=False,
                    timeout=timeout,
                    options=request_options,
                    reasoning_effort=reasoning_effort,
                    include_response_format=False,
                ),
            )
        )
    if provider == "gemini":
        attempts.append(
            (
                "gemini-generateContent",
                lambda: _gemini_generate_content(
                    messages,
                    model,
                    format_json=format_json,
                    timeout=timeout,
                    options=request_options,
                ),
            )
        )
    if request_options and "num_predict" in request_options:
        attempts.append(
            (
                "openai-compatible-max-completion-tokens",
                lambda: _openai_chat_once(
                    messages,
                    model,
                    format_json=format_json,
                    timeout=timeout,
                    options=request_options,
                    reasoning_effort=reasoning_effort,
                    max_token_field="max_completion_tokens",
                ),
            )
        )
    if provider == "openai":
        attempts.append(
            (
                "openai-responses",
                lambda: _openai_responses_once(
                    messages,
                    model,
                    format_json=format_json,
                    timeout=timeout,
                    options=request_options,
                    reasoning_effort=reasoning_effort,
                ),
            )
        )
    attempts.append(
        (
            "openai-compatible-minimal",
            lambda: _openai_chat_once(
                _json_prompt_messages(messages) if format_json else messages,
                model,
                format_json=False,
                timeout=timeout,
                options=request_options,
                reasoning_effort=reasoning_effort,
                include_response_format=False,
                minimal_options=True,
            ),
        )
    )
    return _run_api_attempts(attempts, provider, model)


def _ollama_chat(
    messages: list[dict[str, str]],
    model: str | None,
    *,
    format_json: bool,
    timeout: float,
    options: dict[str, Any] | None,
    think: bool | None,
) -> str:
    payload: dict[str, Any] = {
        "model": model or chat_model(),
        "messages": messages,
        "stream": False,
    }
    if format_json:
        payload["format"] = "json"
    if options:
        payload["options"] = options
    if think is not None:
        payload["think"] = think
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(f"{ollama_base_url().rstrip('/')}/api/chat", json=payload)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise OllamaError(str(exc)) from exc
    data = response.json()
    try:
        message = data["message"]
        content = str(message.get("content") or "")
        if content.strip():
            return content
        if message.get("thinking"):
            raise OllamaError("Ollama returned thinking text but no final answer. The request should disable thinking or allow more output tokens.")
        return content
    except KeyError as exc:
        raise OllamaError(f"Unexpected Ollama response: {json.dumps(data)[:500]}") from exc


def chat(
    messages: list[dict[str, str]],
    model: str | None = None,
    *,
    format_json: bool = False,
    timeout: float = 120.0,
    options: dict[str, Any] | None = None,
    think: bool | None = False,
) -> str:
    if is_api_provider():
        return _openai_chat(
            messages,
            model,
            format_json=format_json,
            timeout=timeout,
            options=options,
        )
    return _ollama_chat(
        messages,
        model,
        format_json=format_json,
        timeout=timeout,
        options=options,
        think=think,
    )
