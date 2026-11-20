"""HTTP layer: retries with exponential backoff, per-run call limits, monthly dollar caps, call and spend logging."""
from __future__ import annotations

import os
import time

import requests

import budget
from config import BudgetExceeded, ConfigError, get_runtime, optional_secret

_REAL_REQUEST = requests.request
DATAFORSEO_BASE = "https://api.dataforseo.com/v3/"
SERPAPI_URL = "https://serpapi.com/search.json"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MAX_ATTEMPTS = 3


class ApiError(Exception):
    pass


def _request(config, service, label, method, url, retry_statuses=(429, 500, 502, 503, 504), **kwargs):
    """One logical API call = up to 3 attempts. Each attempt counts against the budget."""
    rt = get_runtime(config)
    backoff = config.get("retry_backoff_seconds", 1.0)
    last_err = None
    timeout = kwargs.pop("timeout", 60)  # read once so every retry keeps the caller's timeout
    if os.environ.get("SEO_ENGINE_OFFLINE") == "1" and requests.request is _REAL_REQUEST:
        # set by the test suite unless RUN_LIVE_TESTS=1; a test that swaps in a fake transport still runs
        raise ApiError(f"{service} {label}: not sent, SEO_ENGINE_OFFLINE=1 (paid APIs are off in tests)")
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if service in budget.SERVICES:
            budget.check(config, service)  # monthly dollar caps; raises BudgetExceeded
        if service in rt.calls:
            rt.spend(service)  # per-run call limit; raises BudgetExceeded
        started = time.time()
        try:
            resp = requests.request(method, url, timeout=timeout, **kwargs)
            ms = int((time.time() - started) * 1000)
            rt.log("api_call", service=service, endpoint=label, attempt=attempt, status=resp.status_code, ms=ms)
            if resp.status_code in retry_statuses:
                last_err = ApiError(f"{service} {label}: HTTP {resp.status_code}")
            elif resp.status_code >= 400:
                raise ApiError(f"{service} {label}: HTTP {resp.status_code}: {resp.text[:200]}")
            else:
                return resp
        except (requests.ConnectionError, requests.Timeout) as e:
            rt.log("api_call", service=service, endpoint=label, attempt=attempt, status="network_error", error=str(e)[:200])
            last_err = ApiError(f"{service} {label}: {type(e).__name__}")
        if attempt < MAX_ATTEMPTS:
            time.sleep(backoff * (2 ** (attempt - 1)))
    raise ApiError(f"{last_err} (failed after {MAX_ATTEMPTS} attempts)")


def dataforseo_post(config, endpoint: str, payload: dict) -> list:
    """POST one task to DataForSEO; return the task's `result` list ([] when empty)."""
    s = get_runtime(config).get_secrets()
    resp = _request(config, "dataforseo", endpoint, "POST", DATAFORSEO_BASE + endpoint,
                    auth=(s["DATAFORSEO_LOGIN"], s["DATAFORSEO_PASSWORD"]), json=[payload])
    data = resp.json()
    budget.record(config, "dataforseo", data.get("cost"), endpoint)
    if data.get("status_code") != 20000:
        raise ApiError(f"dataforseo {endpoint}: {data.get('status_code')} {data.get('status_message')}")
    task = (data.get("tasks") or [{}])[0]
    if task.get("status_code") != 20000:
        raise ApiError(f"dataforseo {endpoint}: task {task.get('status_code')} {task.get('status_message')}")
    return task.get("result") or []


def serpapi_get(config, params: dict) -> dict:
    s = get_runtime(config).get_secrets()
    label = f"search.json engine={params.get('engine')}"
    resp = _request(config, "serpapi", label, "GET", SERPAPI_URL, params={**params, "api_key": s["SERPAPI_KEY"]})
    data = resp.json()
    budget.record(config, "serpapi", None, label)  # SerpAPI bills per search: serpapi_usd_per_search
    if data.get("error") and not (data.get("organic_results") or data.get("local_results")):
        raise ApiError(f"serpapi: {data['error']}")
    return data


class TruncatedError(ApiError):
    """The model stopped because it reached max_tokens; the reply is incomplete."""


def openrouter_chat(config, model: str, system: str, user: str, role: str,
                    max_tokens: int = 16000, json_mode: bool = False) -> str:
    """One chat completion through OpenRouter. Returns the assistant text."""
    key = optional_secret("OPENROUTER_API_KEY")
    if not key:
        raise ConfigError("OPENROUTER_API_KEY is not set in .env")
    body = {"model": model, "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    resp = _request(config, "llm", f"openrouter {role} {model}", "POST", OPENROUTER_URL, timeout=600,
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, json=body)
    data = resp.json()
    budget.record(config, "llm", (data.get("usage") or {}).get("cost"), f"{role} {model}")
    if data.get("error"):
        raise ApiError(f"openrouter {model}: {str(data['error'].get('message', data['error']))[:200]}")
    choice = (data.get("choices") or [{}])[0]
    text = (choice.get("message") or {}).get("content") or ""
    usage = data.get("usage") or {}
    get_runtime(config).log("llm_call", role=role, model=data.get("model", model),
                            finish_reason=choice.get("finish_reason"), input_tokens=usage.get("prompt_tokens"),
                            output_tokens=usage.get("completion_tokens"), cost_usd=usage.get("cost"))
    if choice.get("finish_reason") == "length":
        raise TruncatedError(f"openrouter {model}: response cut off at max_tokens ({max_tokens})")
    if not text.strip():
        raise ApiError(f"openrouter {model}: empty response (finish_reason={choice.get('finish_reason')})")
    return text


def fetch_page(config, url: str):
    """Fetch a competitor page (not an API call; no budget). Returns HTML or None."""
    rt = get_runtime(config)
    try:
        resp = requests.get(url, timeout=15, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml"})
        ctype = resp.headers.get("content-type", "")
        ok = resp.status_code == 200 and "html" in ctype
        rt.log("page_fetch", url=url, status=resp.status_code, ok=ok)
        return resp.text if ok else None
    except requests.RequestException as e:
        rt.log("page_fetch", url=url, status="error", ok=False, error=str(e)[:150])
        return None


__all__ = ["ApiError", "BudgetExceeded", "dataforseo_post", "serpapi_get", "openrouter_chat", "fetch_page"]
