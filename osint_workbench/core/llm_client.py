"""Robust LLM Client for LM Studio communication.

Provides structured JSON parsing, retry logic with exponential backoff,
error propagation via typed exceptions, and token budget awareness.
"""

import json
import logging
import re
import time
from typing import Optional
from urllib.parse import urlparse

from openai import APIConnectionError, APITimeoutError, OpenAI

from osint_workbench.core.models import LLMResponse

logger = logging.getLogger(__name__)


class LLMClientError(Exception):
    """Raised when LLM communication fails after retries.

    Attributes:
        attempts: Number of attempts made before failure.
        last_error: Description of the last error encountered.
    """

    def __init__(self, message: str, attempts: int = 0, last_error: str = ""):
        super().__init__(message)
        self.attempts = attempts
        self.last_error = last_error


class LLMClient:
    """Wrapper around the OpenAI client for LM Studio communication.

    Provides ask(), ask_json(), estimate_tokens(), and update_base_url()
    with automatic retry, JSON auto-correction, and exponential backoff.
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:1234/v1",
        model: str = "",
        temperature: float = 0.7,
        max_retries: int = 3,
        system_prompt: str = "",
        api_key: str = "lm-studio",
    ):
        """Initialize LLM client pointing to an OpenAI-compatible endpoint.

        Args:
            base_url: The API endpoint URL (LM Studio, or any other
                OpenAI-compatible backend configured in AppConfig.backends).
            model: Model identifier to use for completions.
            temperature: Sampling temperature (0.0-2.0).
            max_retries: Maximum retry attempts for connection failures.
            system_prompt: Default system prompt for all requests.
            api_key: Bearer credential for the endpoint. Defaults to the
                LM Studio convention (server ignores the value but the
                OpenAI SDK requires a non-empty string); pass a real key
                for hosted OpenAI-compatible backends that authenticate.
        """
        self.base_url = base_url
        self.model = model
        self.temperature = temperature
        self.max_retries = max_retries
        self.default_system_prompt = system_prompt
        self.api_key = api_key or "lm-studio"
        # True when `model` was auto-detected from list_models() rather
        # than explicitly chosen (config, wizard, Model Tiers). Callers
        # that hold this client across multiple runs (routes.py's
        # singleton) use this to re-probe the endpoint every run instead
        # of trusting a guess forever -- otherwise a model swapped in the
        # backend mid-session (e.g. LM Studio's JIT loading silently
        # serving whatever id is sent) would go undetected exactly like
        # the hardcoded default this replaces. Explicit assignment
        # (switch_backend, set_active_model) always clears it back to
        # False.
        self.model_autodetected = False
        self._client = OpenAI(base_url=self.base_url, api_key=self.api_key)

    def ask(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
    ) -> LLMResponse:
        """Send a prompt and return a structured LLMResponse.

        Implements exponential backoff retry for connection failures
        (3 attempts, starting 1s, max 4s per wait).

        Passing `model`/`temperature` overrides this client's configured
        defaults for this single call only -- this is what lets one
        LLMClient instance (pointed at one backend endpoint) serve all
        three steering tiers (thinker/default/small), since LM Studio and
        other OpenAI-compatible servers accept `model` per request.

        Args:
            prompt: The user prompt to send.
            system_prompt: Optional system prompt override.
            model: Optional model identifier override for this call only.
            temperature: Optional sampling temperature override for this call only.
        Returns:
            LLMResponse with content and token usage.

        Raises:
            LLMClientError: If all connection attempts fail.
        """
        sys_prompt = system_prompt if system_prompt is not None else self.default_system_prompt

        messages = []
        if sys_prompt:
            messages.append({"role": "system", "content": sys_prompt})
        messages.append({"role": "user", "content": prompt})

        effective_model = model if model else self.model
        effective_temperature = temperature if temperature is not None else self.temperature

        last_error = ""
        backoff = 1.0  # Starting backoff in seconds

        for attempt in range(1, self.max_retries + 1):
            try:
                response = self._client.chat.completions.create(
                    model=effective_model,
                    messages=messages,
                    temperature=effective_temperature,
                )

                content = response.choices[0].message.content if response.choices else ""
                usage = response.usage

                prompt_tokens = usage.prompt_tokens if usage else 0
                completion_tokens = usage.completion_tokens if usage else 0
                total_tokens = usage.total_tokens if usage else 0

                return LLMResponse(
                    content=content or "",
                    tokens_used=total_tokens,
                    model=effective_model,
                    success=True,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total_tokens,
                )

            except (APIConnectionError, APITimeoutError, ConnectionError, OSError) as e:
                last_error = str(e)
                if attempt < self.max_retries:
                    time.sleep(min(backoff, 4.0))
                    backoff *= 2.0

        raise LLMClientError(
            f"Failed to connect to LLM after {self.max_retries} attempts: {last_error}",
            attempts=self.max_retries,
            last_error=last_error,
        )

    def ask_json(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        max_retries: int = 3,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
    ) -> dict:
        """Send prompt expecting JSON response. Parses and validates with retries.

        Strips markdown code fences, auto-corrects common JSON errors,
        and retries with error feedback on parse failure.

        Args:
            prompt: The user prompt requesting JSON output.
            system_prompt: Optional system prompt override.
            model: Optional model identifier override for this call only.
            temperature: Optional sampling temperature override for this call only.
            max_retries: Maximum parse retry attempts (default 3).

        Returns:
            Parsed dict from the LLM JSON response.

        Raises:
            LLMClientError: If JSON parsing fails after all retries.
        """
        current_prompt = prompt
        last_error = ""

        for attempt in range(1, max_retries + 1):
            response = self.ask(current_prompt, system_prompt, model=model, temperature=temperature)
            raw_text = response.content

            # Strip markdown code fences
            cleaned = self._strip_code_fences(raw_text)

            # Try parsing directly
            try:
                parsed = json.loads(cleaned)
                if isinstance(parsed, dict):
                    return parsed
                # Valid JSON but not a dict — treat as parse failure
            except json.JSONDecodeError:
                pass

            # Auto-correct common structural errors
            corrected = self._auto_correct_json(cleaned)
            try:
                parsed = json.loads(corrected)
                if isinstance(parsed, dict):
                    return parsed
                last_error = f"Expected JSON object (dict), got {type(parsed).__name__}"
            except json.JSONDecodeError as e:
                last_error = str(e)

            # On failure, retry with error message included in prompt
            if attempt < max_retries:
                current_prompt = (
                    f"{prompt}\n\n"
                    f"[Previous response was not valid JSON. "
                    f"Parse error: {last_error}. "
                    f"Please respond with valid JSON only, no markdown formatting.]"
                )

        raise LLMClientError(
            f"Failed to parse JSON after {max_retries} attempts: {last_error}",
            attempts=max_retries,
            last_error=last_error,
        )

    def list_models(self, timeout: float = 10.0) -> dict:
        """Fetch the list of model identifiers currently available at this
        client's endpoint (e.g. loaded/servable in LM Studio).

        Used by the model selector UI so the user picks from what's
        actually there instead of retyping a model id by hand. Never
        raises -- an unreachable endpoint is a normal, expected state for
        a local tool (server not started yet, wrong port) -- but the
        failure is always logged and returned as an explicit `error`
        string rather than collapsing into an empty list indistinguishable
        from "endpoint is up, zero models loaded".

        Args:
            timeout: Request timeout in seconds.

        Returns:
            Dict with `models` (sorted list of model id strings, empty on
            failure) and `error` (a human-readable message, or None on
            success).
        """
        try:
            response = self._client.models.list(timeout=timeout)
            ids = [m.id for m in getattr(response, "data", [])]
            return {"models": sorted(ids), "error": None}
        except Exception as exc:
            logger.warning("list_models failed for %s: %s", self.base_url, exc)
            return {"models": [], "error": f"Could not reach {self.base_url}: {exc}"}

    def estimate_tokens(self, text: str) -> int:
        """Estimate token count for a text string.

        Uses a character-based approximation (len(text) / 4) which is
        a reasonable estimate for most English text with LLM tokenizers.

        Args:
            text: The text to estimate tokens for.

        Returns:
            Estimated token count.
        """
        if not text:
            return 0
        return max(1, len(text) // 4)

    def update_base_url(self, new_url: str) -> None:
        """Dynamically update the LM Studio endpoint URL.

        Validates that the URL is well-formed HTTP or HTTPS.
        If invalid, retains the previous base_url.

        Args:
            new_url: The new endpoint URL to use.

        Raises:
            ValueError: If the URL is not a valid HTTP/HTTPS URL.
        """
        parsed = urlparse(new_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError(
                f"Invalid URL: '{new_url}'. Must be a well-formed HTTP or HTTPS URL."
            )

        self.base_url = new_url
        self._client = OpenAI(base_url=self.base_url, api_key=self.api_key)

    def _strip_code_fences(self, text: str) -> str:
        """Strip markdown code fences from LLM response text.

        Handles ```json...```, ```...```, and ~~~json...~~~ patterns.

        Args:
            text: Raw LLM response text.

        Returns:
            Text with code fences stripped.
        """
        # Strip ```json...``` and ```...``` fences
        stripped = re.sub(
            r"```(?:json)?\s*\n?(.*?)\n?\s*```",
            r"\1",
            text,
            flags=re.DOTALL,
        )

        # Strip ~~~json...~~~ fences
        stripped = re.sub(
            r"~~~(?:json)?\s*\n?(.*?)\n?\s*~~~",
            r"\1",
            stripped,
            flags=re.DOTALL,
        )

        return stripped.strip()

    def _auto_correct_json(self, text: str) -> str:
        """Attempt automatic correction of common JSON structural errors.

        Fixes:
        - Trailing commas before } or ]
        - Single quotes used instead of double quotes
        - Unescaped control characters within strings

        Args:
            text: Potentially malformed JSON text.

        Returns:
            Corrected text that may parse as valid JSON.
        """
        corrected = text

        # Remove trailing commas before } or ]
        corrected = re.sub(r",\s*([}\]])", r"\1", corrected)

        # Replace single quotes with double quotes (simple heuristic)
        # Only do this if there are no double quotes already in use as string delimiters
        if '"' not in corrected and "'" in corrected:
            corrected = corrected.replace("'", '"')
        elif "'" in corrected:
            # More careful replacement: replace single-quoted keys/values
            corrected = re.sub(
                r"(?<=[\[{,:\s])'([^']*?)'(?=\s*[,:\]}])",
                r'"\1"',
                corrected,
            )

        # Escape unescaped control characters within strings
        corrected = re.sub(
            r"[\x00-\x08\x0b\x0c\x0e-\x1f]",
            lambda m: f"\\u{ord(m.group()):04x}",
            corrected,
        )

        return corrected
