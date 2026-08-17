"""Multi-engine search with rate limiting, rotation, and cooldown handling.

Supports Google, Bing, and DuckDuckGo with per-engine rate limiting,
round-robin rotation, and exponential backoff on rate-limit/CAPTCHA responses.
"""

import logging
import random
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Tuple
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


class SearchEngine(Enum):
    """Supported search engines."""

    GOOGLE = "google"
    BING = "bing"
    DUCKDUCKGO = "duckduckgo"


@dataclass
class SearchResult:
    """A single search result from an engine."""

    url: str
    title: str
    snippet: str
    engine: SearchEngine


@dataclass
class _EngineState:
    """Internal per-engine tracking state."""

    last_request_time: float = 0.0
    cooldown_until: float = 0.0
    cooldown_duration: float = 30.0  # Starting cooldown in seconds


# Operator mappings per engine
_OPERATOR_MAP = {
    SearchEngine.GOOGLE: {
        "site:": "site:",
        "inurl:": "inurl:",
        "intitle:": "intitle:",
    },
    SearchEngine.BING: {
        "site:": "site:",
        "inurl:": "instreamset:",
        "intitle:": "intitle:",
    },
    SearchEngine.DUCKDUCKGO: {
        "site:": "site:",
        "inurl:": "site:",  # DDG doesn't support inurl, fallback to site:
        "intitle:": "intitle:",
    },
}

# Base search URLs per engine
_BASE_URLS = {
    SearchEngine.GOOGLE: "https://www.google.com/search?q=",
    SearchEngine.BING: "https://www.bing.com/search?q=",
    SearchEngine.DUCKDUCKGO: "https://duckduckgo.com/?q=",
}

# CAPTCHA indicators in response body
_CAPTCHA_INDICATORS = [
    "captcha",
    "unusual traffic",
    "are you a robot",
    "verify you are human",
    "automated requests",
    "please verify",
    "bot detection",
]


class MultiEngineSearch:
    """Executes search queries across multiple engines with rate limiting and rotation.

    Features:
    - Per-engine rate limiting with jittered delays
    - Round-robin engine rotation (no engine gets consecutive queries)
    - Exponential backoff cooldown on HTTP 429 / CAPTCHA
    - Automatic failover to next available engine
    """

    def __init__(
        self,
        rate_limit_per_engine: float = 2.0,
        jitter_range: Tuple[float, float] = (0.5, 2.0),
        session: Optional[requests.Session] = None,
    ):
        """Initialize MultiEngineSearch.

        Args:
            rate_limit_per_engine: Maximum requests per second per engine.
            jitter_range: Tuple of (min, max) seconds for jittered delay.
            session: Optional requests.Session for HTTP calls.
        """
        self.rate_limit_per_engine = rate_limit_per_engine
        self.jitter_range = jitter_range
        self.session = session or requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        })

        # Per-engine state tracking
        self._engine_states: dict[SearchEngine, _EngineState] = {
            engine: _EngineState() for engine in SearchEngine
        }

        # Round-robin rotation index
        self._rotation_index: int = 0
        self._last_used_engine: Optional[SearchEngine] = None

    def build_dork_url(self, query: str, engine: SearchEngine) -> str:
        """Build an engine-specific search URL with operator mapping.

        Translates generic dork operators (site:, inurl:, intitle:) to
        engine-specific equivalents and constructs the full search URL.

        Args:
            query: The search query string (may contain dork operators).
            engine: The target search engine.

        Returns:
            Fully constructed search URL with URL-encoded query.
        """
        mapped_query = self._map_operators(query, engine)
        encoded_query = quote_plus(mapped_query)
        return f"{_BASE_URLS[engine]}{encoded_query}"

    def rotate_engine(self) -> SearchEngine:
        """Select the next available engine using round-robin rotation.

        Ensures no engine gets consecutive queries when multiple engines
        are available. Skips engines currently in cooldown.

        Returns:
            The next available SearchEngine.

        Raises:
            RuntimeError: If no engines are available (all in cooldown).
        """
        engines = list(SearchEngine)
        available = [e for e in engines if not self._is_rate_limited(e)]

        if not available:
            # All engines in cooldown - return the one with shortest cooldown
            now = time.time()
            shortest = min(
                engines,
                key=lambda e: self._engine_states[e].cooldown_until - now,
            )
            return shortest

        # Round-robin through available engines, skip last used if possible
        if len(available) > 1 and self._last_used_engine in available:
            available = [e for e in available if e != self._last_used_engine]

        selected = available[self._rotation_index % len(available)]
        self._rotation_index = (self._rotation_index + 1) % len(engines)
        self._last_used_engine = selected
        return selected

    def search(
        self,
        query: str,
        engines: Optional[List[SearchEngine]] = None,
        max_results_per_engine: int = 10,
    ) -> List[SearchResult]:
        """Execute search across multiple engines with rate limiting.

        For each engine: checks cooldown, applies rate limit with jitter,
        makes the HTTP request, and parses results. On 429 or CAPTCHA,
        marks the engine with cooldown and tries the next engine.

        Args:
            query: The search query string.
            engines: List of engines to use. Defaults to all engines.
            max_results_per_engine: Maximum results to extract per engine.

        Returns:
            List of SearchResult objects from all successful engine queries.
        """
        if engines is None:
            engines = list(SearchEngine)

        results: List[SearchResult] = []

        for engine in engines:
            # Check if engine is in cooldown
            if self._is_rate_limited(engine):
                logger.debug(
                    "Engine %s is in cooldown, skipping.", engine.value
                )
                continue

            # Apply rate limit with jitter
            self._wait_for_rate_limit(engine)

            # Build URL and make request
            url = self.build_dork_url(query, engine)

            try:
                response = self.session.get(url, timeout=15)

                # Check for rate limiting or CAPTCHA
                if response.status_code == 429 or self._detect_captcha(
                    response.text
                ):
                    logger.warning(
                        "Engine %s returned 429/CAPTCHA. Applying cooldown.",
                        engine.value,
                    )
                    self._apply_cooldown(engine)

                    # Try to rotate to another engine
                    alternate = self._find_alternate_engine(engine, engines)
                    if alternate is not None:
                        logger.info(
                            "Rotating query to engine %s.", alternate.value
                        )
                        if not self._is_rate_limited(alternate):
                            self._wait_for_rate_limit(alternate)
                            alt_url = self.build_dork_url(query, alternate)
                            alt_response = self.session.get(
                                alt_url, timeout=15
                            )
                            if alt_response.status_code == 200:
                                engine_results = self._parse_results(
                                    alt_response.text,
                                    alternate,
                                    max_results_per_engine,
                                )
                                results.extend(engine_results)
                                self._engine_states[
                                    alternate
                                ].last_request_time = time.time()
                    continue

                if response.status_code == 200:
                    engine_results = self._parse_results(
                        response.text, engine, max_results_per_engine
                    )
                    results.extend(engine_results)

                # Update last request time
                self._engine_states[engine].last_request_time = time.time()

            except requests.RequestException as e:
                logger.warning(
                    "Request to %s failed: %s", engine.value, str(e)
                )
                continue

        # If all engines were unavailable, wait for shortest cooldown
        if not results and all(
            self._is_rate_limited(e) for e in engines
        ):
            self._wait_for_all_engines_cooldown(engines)

        return results

    def _map_operators(self, query: str, engine: SearchEngine) -> str:
        """Map generic dork operators to engine-specific equivalents.

        Args:
            query: The query string with potential operators.
            engine: The target engine for operator mapping.

        Returns:
            Query with operators mapped to engine-specific format.
        """
        operator_map = _OPERATOR_MAP[engine]
        mapped = query

        for generic_op, engine_op in operator_map.items():
            if generic_op != engine_op:
                mapped = mapped.replace(generic_op, engine_op)

        return mapped

    def _is_rate_limited(self, engine: SearchEngine) -> bool:
        """Check if an engine is currently in cooldown.

        Args:
            engine: The engine to check.

        Returns:
            True if the engine is in cooldown, False otherwise.
        """
        state = self._engine_states[engine]
        return time.time() < state.cooldown_until

    def _apply_cooldown(self, engine: SearchEngine) -> None:
        """Apply exponential backoff cooldown to an engine.

        Sets cooldown_until to now + current cooldown_duration, then
        doubles the cooldown_duration up to a maximum of 300 seconds.

        Args:
            engine: The engine to apply cooldown to.
        """
        state = self._engine_states[engine]
        state.cooldown_until = time.time() + state.cooldown_duration
        # Double cooldown duration, cap at 300s
        state.cooldown_duration = min(state.cooldown_duration * 2, 300.0)
        logger.info(
            "Engine %s cooldown set to %.1fs. Next cooldown will be %.1fs.",
            engine.value,
            state.cooldown_duration / 2,  # Current cooldown applied
            state.cooldown_duration,  # Next cooldown if triggered again
        )

    def _wait_for_rate_limit(self, engine: SearchEngine) -> None:
        """Wait for jittered delay to respect rate limiting.

        Ensures minimum interval between requests to the same engine
        based on rate_limit_per_engine, plus random jitter.

        Args:
            engine: The engine to rate limit for.
        """
        state = self._engine_states[engine]
        now = time.time()
        min_interval = 1.0 / self.rate_limit_per_engine
        elapsed = now - state.last_request_time

        if elapsed < min_interval:
            base_wait = min_interval - elapsed
        else:
            base_wait = 0.0

        # Add jitter
        jitter = random.uniform(self.jitter_range[0], self.jitter_range[1])
        total_wait = base_wait + jitter

        if total_wait > 0:
            time.sleep(total_wait)

    def _detect_captcha(self, html: str) -> bool:
        """Detect CAPTCHA or bot-detection pages in response HTML.

        Args:
            html: The response HTML body.

        Returns:
            True if CAPTCHA indicators are detected.
        """
        lower_html = html.lower()
        return any(indicator in lower_html for indicator in _CAPTCHA_INDICATORS)

    def _find_alternate_engine(
        self, blocked_engine: SearchEngine, engines: List[SearchEngine]
    ) -> Optional[SearchEngine]:
        """Find an alternate engine that is not in cooldown.

        Args:
            blocked_engine: The engine that was just blocked.
            engines: List of engines to consider.

        Returns:
            An available alternate engine, or None if all are unavailable.
        """
        for engine in engines:
            if engine != blocked_engine and not self._is_rate_limited(engine):
                return engine
        return None

    def _wait_for_all_engines_cooldown(
        self, engines: List[SearchEngine]
    ) -> None:
        """Wait until the engine with the shortest cooldown becomes available.

        Logs a warning that all engines are in cooldown.

        Args:
            engines: List of engines to wait on.
        """
        now = time.time()
        shortest_wait = min(
            self._engine_states[e].cooldown_until - now for e in engines
        )

        if shortest_wait > 0:
            logger.warning(
                "All search engines are in cooldown. "
                "Waiting %.1f seconds for shortest cooldown to expire.",
                shortest_wait,
            )
            time.sleep(shortest_wait)

    def _parse_results(
        self, html: str, engine: SearchEngine, max_results: int
    ) -> List[SearchResult]:
        """Parse search results from engine HTML response.

        Extracts links, titles, and snippets from the engine's result page.
        Results parsing is best-effort and engine-specific.

        Args:
            html: The HTML response body.
            engine: The search engine that produced the response.
            max_results: Maximum number of results to extract.

        Returns:
            List of parsed SearchResult objects.
        """
        results: List[SearchResult] = []

        try:
            soup = BeautifulSoup(html, "html.parser")

            if engine == SearchEngine.GOOGLE:
                results = self._parse_google(soup, max_results)
            elif engine == SearchEngine.BING:
                results = self._parse_bing(soup, max_results)
            elif engine == SearchEngine.DUCKDUCKGO:
                results = self._parse_duckduckgo(soup, max_results)

            # Tag results with the engine
            for result in results:
                result.engine = engine

        except Exception as e:
            logger.warning(
                "Failed to parse results from %s: %s", engine.value, str(e)
            )

        return results

    def _parse_google(
        self, soup: BeautifulSoup, max_results: int
    ) -> List[SearchResult]:
        """Parse Google search results from HTML.

        Args:
            soup: BeautifulSoup parsed HTML.
            max_results: Maximum results to extract.

        Returns:
            List of SearchResult objects.
        """
        results: List[SearchResult] = []

        # Google wraps results in divs with class 'g'
        for div in soup.find_all("div", class_="g")[:max_results]:
            link_tag = div.find("a", href=True)
            title_tag = div.find("h3")
            snippet_tag = div.find("div", class_="VwiC3b") or div.find(
                "span", class_="aCOpRe"
            )

            if link_tag and title_tag:
                url = link_tag["href"]
                if url.startswith("/url?q="):
                    url = url.split("/url?q=")[1].split("&")[0]
                title = title_tag.get_text(strip=True)
                snippet = (
                    snippet_tag.get_text(strip=True) if snippet_tag else ""
                )
                results.append(
                    SearchResult(
                        url=url,
                        title=title,
                        snippet=snippet,
                        engine=SearchEngine.GOOGLE,
                    )
                )

        return results

    def _parse_bing(
        self, soup: BeautifulSoup, max_results: int
    ) -> List[SearchResult]:
        """Parse Bing search results from HTML.

        Args:
            soup: BeautifulSoup parsed HTML.
            max_results: Maximum results to extract.

        Returns:
            List of SearchResult objects.
        """
        results: List[SearchResult] = []

        # Bing uses <li class="b_algo"> for organic results
        for li in soup.find_all("li", class_="b_algo")[:max_results]:
            link_tag = li.find("a", href=True)
            snippet_tag = li.find("p") or li.find("div", class_="b_caption")

            if link_tag:
                url = link_tag["href"]
                title = link_tag.get_text(strip=True)
                snippet = (
                    snippet_tag.get_text(strip=True) if snippet_tag else ""
                )
                results.append(
                    SearchResult(
                        url=url,
                        title=title,
                        snippet=snippet,
                        engine=SearchEngine.BING,
                    )
                )

        return results

    def _parse_duckduckgo(
        self, soup: BeautifulSoup, max_results: int
    ) -> List[SearchResult]:
        """Parse DuckDuckGo search results from HTML.

        Args:
            soup: BeautifulSoup parsed HTML.
            max_results: Maximum results to extract.

        Returns:
            List of SearchResult objects.
        """
        results: List[SearchResult] = []

        # DuckDuckGo uses <article> or <div class="result"> patterns
        result_divs = soup.find_all(
            "div", class_="result"
        ) or soup.find_all("article")

        for div in result_divs[:max_results]:
            link_tag = div.find("a", href=True)
            title_tag = div.find("h2") or div.find("a")
            snippet_tag = div.find("a", class_="result__snippet") or div.find(
                "span"
            )

            if link_tag:
                url = link_tag["href"]
                title = (
                    title_tag.get_text(strip=True)
                    if title_tag
                    else link_tag.get_text(strip=True)
                )
                snippet = (
                    snippet_tag.get_text(strip=True) if snippet_tag else ""
                )
                results.append(
                    SearchResult(
                        url=url,
                        title=title,
                        snippet=snippet,
                        engine=SearchEngine.DUCKDUCKGO,
                    )
                )

        return results
