"""Connection tester for LLM API endpoints.

Tests connectivity by issuing a GET request to the /models endpoint
and categorizing the response into structured result types.
"""

import time
from dataclasses import dataclass, field
from typing import Optional

import requests


@dataclass
class ConnectionTestResult:
    """Result of a connection test to an LLM API endpoint."""

    success: bool
    status_code: Optional[int] = None
    error_category: Optional[str] = None  # "network_unreachable", "timeout", "auth_rejected", "unexpected_response"
    error_detail: Optional[str] = None
    models_available: list[str] = field(default_factory=list)
    response_time_ms: Optional[float] = None


def test_connection(endpoint: str, api_key: str, timeout: float = 10.0) -> ConnectionTestResult:
    """Test connectivity to an LLM API endpoint.

    Sends a GET request to {endpoint}/models with the provided API key
    and categorizes the response.

    Args:
        endpoint: The base URL of the API (e.g. "http://127.0.0.1:1234/v1").
        api_key: The API key for authentication.
        timeout: Request timeout in seconds (default 10.0).

    Returns:
        A ConnectionTestResult with categorized success/failure information.
    """
    url = f"{endpoint.rstrip('/')}/models"
    headers = {"Authorization": f"Bearer {api_key}"}

    start_time = time.time()

    try:
        response = requests.get(url, headers=headers, timeout=timeout)
        elapsed_ms = (time.time() - start_time) * 1000

        # HTTP 401 or 403 → auth_rejected
        if response.status_code in (401, 403):
            return ConnectionTestResult(
                success=False,
                status_code=response.status_code,
                error_category="auth_rejected",
                error_detail="Authentication failed. Check your API key.",
                response_time_ms=elapsed_ms,
            )

        # HTTP 200 → attempt to parse JSON for model list
        if response.status_code == 200:
            try:
                body = response.json()
                # OpenAI-compatible APIs return {"data": [{"id": "model-name"}, ...]}
                models = []
                if isinstance(body, dict) and "data" in body:
                    data = body["data"]
                    if isinstance(data, list):
                        models = [
                            item["id"]
                            for item in data
                            if isinstance(item, dict) and "id" in item
                        ]
                return ConnectionTestResult(
                    success=True,
                    status_code=200,
                    models_available=models,
                    response_time_ms=elapsed_ms,
                )
            except (ValueError, KeyError, TypeError):
                # HTTP 200 but invalid/unexpected JSON body
                return ConnectionTestResult(
                    success=False,
                    status_code=200,
                    error_category="unexpected_response",
                    error_detail="Server responded but returned unexpected data.",
                    response_time_ms=elapsed_ms,
                )

        # Any other HTTP status (4xx/5xx not 401/403)
        return ConnectionTestResult(
            success=False,
            status_code=response.status_code,
            error_category="unexpected_response",
            error_detail=f"Server returned error status {response.status_code}.",
            response_time_ms=elapsed_ms,
        )

    except requests.exceptions.Timeout:
        elapsed_ms = (time.time() - start_time) * 1000
        return ConnectionTestResult(
            success=False,
            error_category="timeout",
            error_detail="Connection timed out. The server may be overloaded.",
            response_time_ms=elapsed_ms,
        )

    except requests.exceptions.ConnectionError as exc:
        elapsed_ms = (time.time() - start_time) * 1000
        error_str = str(exc).lower()

        # Distinguish DNS failure from connection refused
        if "name or service not known" in error_str or "getaddrinfo" in error_str or "nodename nor servname" in error_str:
            detail = "Cannot resolve hostname. Check the endpoint URL."
        else:
            detail = "Cannot reach endpoint. Verify the server is running."

        return ConnectionTestResult(
            success=False,
            error_category="network_unreachable",
            error_detail=detail,
            response_time_ms=elapsed_ms,
        )

    except requests.exceptions.RequestException:
        elapsed_ms = (time.time() - start_time) * 1000
        return ConnectionTestResult(
            success=False,
            error_category="network_unreachable",
            error_detail="Cannot reach endpoint. Verify the server is running.",
            response_time_ms=elapsed_ms,
        )
