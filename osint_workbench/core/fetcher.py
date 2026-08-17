"""Concurrent HTTP fetcher with connection pooling, rate limiting, and retry logic."""

import random
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from osint_workbench.core.models import FetchResult


class ConcurrentFetcher:
    """High-performance HTTP fetcher using connection pooling, retry logic,
    and concurrent execution via ThreadPoolExecutor.

    Never raises exceptions to the caller — all outcomes are wrapped in FetchResult.
    """

    def __init__(
        self,
        max_workers: int = 20,
        timeout: int = 10,
        max_retries: int = 2,
        rate_limit_per_second: float = 5.0,
    ):
        """Initialize with connection-pooled session and thread pool.

        Args:
            max_workers: Number of concurrent threads (clamped to 1-50).
            timeout: Per-request timeout in seconds (clamped to 1-60).
            max_retries: Number of retries for failed requests.
            rate_limit_per_second: Maximum requests per second.
        """
        # Clamp parameters to valid ranges
        self.max_workers = max(1, min(50, max_workers))
        self.timeout = max(1, min(60, timeout))
        self.max_retries = max_retries
        self.rate_limit_per_second = max(0.1, rate_limit_per_second)

        # Set up ThreadPoolExecutor
        self.executor = ThreadPoolExecutor(max_workers=self.max_workers)

        # Set up requests.Session with connection pooling and retry adapter
        self.session = requests.Session()
        retry_strategy = Retry(
            total=max_retries,
            backoff_factor=0.5,
            status_forcelist=[500, 502, 503, 504],
            allowed_methods=["GET", "HEAD"],
        )
        adapter = HTTPAdapter(
            pool_connections=20,
            pool_maxsize=20,
            max_retries=retry_strategy,
        )
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                )
            }
        )

    def fetch_batch(
        self, sources: List[dict], format_params: dict
    ) -> List[FetchResult]:
        """Fetch all sources concurrently with rate limiting.

        Returns exactly one FetchResult per source. Never raises exceptions.

        Args:
            sources: List of dicts with 'name' and 'url' keys.
            format_params: Dict of template variables for URL formatting.

        Returns:
            List of FetchResult objects, one per source.
        """
        results: List[FetchResult] = []
        fetch_tasks = []

        # Format all URLs, catching template errors
        for source in sources:
            name = source.get("name", "Unknown")
            raw_url = source.get("url", "")
            formatted_url = self._format_url(raw_url, format_params)
            fetch_tasks.append((name, formatted_url))

        # Submit to thread pool with rate-limited delays
        futures = {}
        delay_interval = 1.0 / self.rate_limit_per_second

        for i, (name, url) in enumerate(fetch_tasks):
            delay = i * delay_interval + random.uniform(0, 0.2)
            future = self.executor.submit(self._fetch_with_delay, name, url, delay)
            futures[future] = (name, url)

        # Collect results as they complete
        for future in as_completed(futures):
            try:
                result = future.result()
                results.append(result)
            except Exception as e:
                name, url = futures[future]
                results.append(
                    FetchResult(
                        name=name,
                        url=url,
                        status="Failed to connect",
                        error=str(e),
                    )
                )

        return results

    def fetch_single(self, name: str, url: str) -> FetchResult:
        """Fetch a single URL with retry and timeout handling.

        Never raises exceptions — always returns a FetchResult.

        Args:
            name: Human-readable name of the source.
            url: The URL to fetch.

        Returns:
            FetchResult with appropriate status for success or failure.
        """
        start_time = time.time()
        try:
            response = self.session.get(url, timeout=self.timeout)
            elapsed_ms = (time.time() - start_time) * 1000

            if response.status_code == 200:
                title, snippet = self._extract_content(response.text)
                return FetchResult(
                    name=name,
                    url=url,
                    status="Active/Accessible",
                    title=title,
                    snippet=snippet,
                    status_code=200,
                    response_time_ms=round(elapsed_ms, 2),
                )
            else:
                return FetchResult(
                    name=name,
                    url=url,
                    status=f"HTTP Status {response.status_code}",
                    status_code=response.status_code,
                    response_time_ms=round(elapsed_ms, 2),
                    error=f"Non-200 status code: {response.status_code}",
                )

        except requests.exceptions.Timeout:
            elapsed_ms = (time.time() - start_time) * 1000
            return FetchResult(
                name=name,
                url=url,
                status="Timeout",
                response_time_ms=round(elapsed_ms, 2),
                error=f"Request timed out after {self.timeout}s",
            )

        except requests.exceptions.ConnectionError as e:
            elapsed_ms = (time.time() - start_time) * 1000
            return FetchResult(
                name=name,
                url=url,
                status="Failed to connect",
                response_time_ms=round(elapsed_ms, 2),
                error=f"Connection error: {str(e)}",
            )

        except requests.exceptions.RequestException as e:
            elapsed_ms = (time.time() - start_time) * 1000
            return FetchResult(
                name=name,
                url=url,
                status="Failed to connect",
                response_time_ms=round(elapsed_ms, 2),
                error=f"Request error: {str(e)}",
            )

        except Exception as e:
            elapsed_ms = (time.time() - start_time) * 1000
            return FetchResult(
                name=name,
                url=url,
                status="Failed to connect",
                response_time_ms=round(elapsed_ms, 2),
                error=f"Unexpected error: {str(e)}",
            )

    def close(self) -> None:
        """Clean up session and thread pool resources."""
        try:
            self.session.close()
        except Exception:
            pass
        try:
            self.executor.shutdown(wait=False)
        except Exception:
            pass

    def _fetch_with_delay(
        self, name: str, url: str, delay: float
    ) -> FetchResult:
        """Fetch a URL after applying a rate-limiting delay.

        Args:
            name: Source name.
            url: URL to fetch.
            delay: Seconds to sleep before making the request.

        Returns:
            FetchResult from fetch_single.
        """
        if delay > 0:
            time.sleep(delay)
        return self.fetch_single(name, url)

    def _format_url(self, url_template: str, format_params: dict) -> str:
        """Format a URL template, replacing missing variables with empty string.

        Uses defaultdict(str) so missing keys become empty string instead of
        raising KeyError.

        Args:
            url_template: URL with {variable} placeholders.
            format_params: Dict of variable values.

        Returns:
            Formatted URL string.
        """
        try:
            safe_params = defaultdict(str, format_params)
            return url_template.format_map(safe_params)
        except (ValueError, IndexError):
            # If formatting fails entirely (e.g., malformed braces),
            # return the template as-is
            return url_template

    def _extract_content(self, html: str) -> tuple:
        """Extract page title and first 100 words of visible body text.

        Args:
            html: Raw HTML string.

        Returns:
            Tuple of (title, snippet) where title defaults to "No title"
            and snippet is up to 100 words of visible text.
        """
        try:
            soup = BeautifulSoup(html, "html.parser")

            # Extract title
            title_tag = soup.find("title")
            if title_tag and title_tag.get_text(strip=True):
                title = title_tag.get_text(strip=True)
            else:
                title = "No title"

            # Extract visible body text (excluding script and style)
            body = soup.find("body")
            if body:
                # Remove script and style elements
                for element in body.find_all(["script", "style"]):
                    element.decompose()
                text = body.get_text(separator=" ", strip=True)
            else:
                text = soup.get_text(separator=" ", strip=True)

            # Take first 100 whitespace-delimited words
            words = text.split()
            snippet = " ".join(words[:100])

            return title, snippet

        except Exception:
            return "No title", ""
