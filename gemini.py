import os
import time
import hashlib
import json
import subprocess
import requests
from typing import Optional

# ---------------------------------------------------------------------------
# Optional: tiny .env loader (no dependency). Only used if GEMINI_API_KEY
# isn't already set in the environment.
# ---------------------------------------------------------------------------
def _load_dotenv_minimal(path: str = ".env") -> None:
    if "GEMINI_API_KEY" in os.environ:
        return
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip("'").strip('"')
                os.environ.setdefault(k, v)
    except Exception:
        # Non-fatal: just skip .env if unreadable
        pass

_load_dotenv_minimal()

# ---------------------------------------------------------------------------
# Simple on-disk cache for Gemini responses
# ---------------------------------------------------------------------------
CACHE_DIR = os.path.join(os.path.dirname(__file__), "geminicache")
os.makedirs(CACHE_DIR, exist_ok=True)

def _cache_key(params: dict) -> str:
    canonical = json.dumps(params, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

def _load_cache(key: str) -> Optional[str]:
    path = os.path.join(CACHE_DIR, f"{key}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("response")
    except Exception as e:
        print(f"Warning: failed to read cache {path}: {e}")
        return None

def _save_cache(key: str, params: dict, response: str) -> None:
    path = os.path.join(CACHE_DIR, f"{key}.json")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"params": params, "response": response, "timestamp": time.time()}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Warning: failed to write cache {path}: {e}")

# ---------------------------------------------------------------------------
# Logging webhook (optional)
# ---------------------------------------------------------------------------
def _send_log_webhook(message: str) -> None:
    webhook = os.getenv("LOG_WEBHOOK")
    if not webhook:
        return
    try:
        requests.post(webhook, json={"content": message}, timeout=8)
    except Exception:
        pass

# ---------------------------------------------------------------------------
# REST call via `curl`
# ---------------------------------------------------------------------------
def _curl_generate_content(api_key: str, model: str, prompt: str, generation_config: Optional[dict] = None) -> str:
    """
    Calls: POST https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent
    Auth:  x-goog-api-key: <api_key>
    Body:  {"contents":[{"parts":[{"text": prompt}]}], "generationConfig": {...}}
    Returns: concatenated text from candidates.
    """

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    payload = {
        "contents": [
            {
                "parts": [{"text": prompt}]
            }
        ]
    }
    if generation_config:
        payload["generationConfig"] = generation_config

    # Use -sS for quiet+show-errors, --fail-with-body to nonzero-exit on HTTP>=400 (keeps body),
    # and read JSON from stdin (-d @-). Avoid shell=True; pass bytes via input=.
    cmd = [
        "curl",
        "-sS",
        "--fail-with-body",
        "-X", "POST",
        url,
        "-H", f"x-goog-api-key: {api_key}",
        "-H", "Content-Type: application/json",
        "-d", "@-",
    ]

    proc = subprocess.run(
        cmd,
        input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    if proc.returncode != 0:
        # Bubble up both stderr and stdout to help debugging
        raise RuntimeError(
            f"curl request failed (exit {proc.returncode}).\nSTDERR:\n{proc.stderr.decode('utf-8', 'ignore')}\nSTDOUT:\n{proc.stdout.decode('utf-8', 'ignore')}"
        )

    try:
        data = json.loads(proc.stdout.decode("utf-8"))
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Failed to parse JSON response: {e}\nRaw:\n{proc.stdout[:500]!r}")

    # Extract text from candidates → content → parts[].text
    texts = []
    for cand in (data.get("candidates") or []):
        content = cand.get("content") or {}
        for part in (content.get("parts") or []):
            t = part.get("text")
            if t:
                texts.append(t)

    # Fallback: sometimes the API may put text directly at top-level (rare).
    if not texts and "text" in data:
        texts.append(data["text"])

    return "\n".join(texts).strip()

# NOTE THAT WE SHOULD ALWAYS BE USING GEMINI-2.5-FLASH, THIS IS NOT A TYPO.
def ask_gemini(prompt: str, api_key: Optional[str] = None, model: str = "gemini-2.5-flash", max_retries: int = 3) -> str:
    """
    Ask Gemini a text-only question via REST using curl.

    Args:
        prompt: The text prompt to send to Gemini
        api_key: Optional API key (defaults to GEMINI_API_KEY environment variable)
        model: The Gemini model to use (default: gemini-2.5-flash)
        max_retries: Retries if response is empty (default: 3)

    Returns:
        Gemini's response text (possibly empty string if all retries yield empty).
    """
    api_key = api_key or os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY not set. Set it in your environment or .env.")

    # ---- Cache lookup ----
    _cache_params = {"prompt": prompt, "model": model}
    _cache_key_val = _cache_key(_cache_params)
    _cached = _load_cache(_cache_key_val)
    if _cached is not None:
        return _cached

    # Cache miss: notify log webhook
    _send_log_webhook(
        f"Gemini cache miss model={model} key={_cache_key_val[:8]} prompt='{(prompt or '')[:200]}'"
    )

    for attempt in range(max_retries):
        try:
            response_text = _curl_generate_content(api_key=api_key, model=model, prompt=prompt)
            if response_text:
                _save_cache(_cache_key_val, _cache_params, response_text)
                return response_text
            else:
                print(f"Attempt {attempt + 1}/{max_retries}: Empty response, retrying...")
        except Exception as e:
            if attempt == max_retries - 1:
                raise Exception(f"Gemini API request failed: {e}")
            print(f"Attempt {attempt + 1}/{max_retries} failed: {e}, retrying...")

        time.sleep(2)

    print(f"Warning: All {max_retries} attempts returned empty responses")
    return ""
