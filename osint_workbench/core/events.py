"""Event bus for real-time investigation event communication.

Provides a thread-safe pub/sub event system that decouples the OSINT engine
from the transport layer (WebSocket/polling).
"""

import logging
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, List, Tuple, Optional

logger = logging.getLogger(__name__)


class EventType(Enum):
    """Typed event types for the investigation lifecycle."""

    INVESTIGATION_STARTED = "investigation_started"
    INVESTIGATION_PAUSED = "investigation_paused"
    INVESTIGATION_RESUMED = "investigation_resumed"
    INVESTIGATION_COMPLETE = "investigation_complete"
    INVESTIGATION_FAILED = "investigation_failed"
    ROUND_STARTED = "round_started"
    ROUND_COMPLETE = "round_complete"
    FETCH_PROGRESS = "fetch_progress"
    LOG_MESSAGE = "log_message"
    STOP_REQUESTED = "stop_requested"
    QUERY_SKIPPED = "query_skipped"
    PLAN_UPDATED = "plan_updated"
    MEDIA_DISCOVERED = "media_discovered"
    MEDIA_INGESTED = "media_ingested"
    EXTRACTION_STARTED = "extraction_started"
    EXTRACTION_COMPLETE = "extraction_complete"
    EXTRACTION_FAILED = "extraction_failed"


# Terminal event types that trigger cleanup
TERMINAL_EVENTS = frozenset({
    EventType.INVESTIGATION_COMPLETE,
    EventType.INVESTIGATION_FAILED,
})


@dataclass
class Event:
    """A typed event associated with a specific investigation.

    Attributes:
        type: The event type enum value.
        investigation_id: The UUID of the investigation this event belongs to.
        data: Arbitrary payload data for the event.
    """

    type: EventType
    investigation_id: str
    data: dict = field(default_factory=dict)


class EventBus:
    """Thread-safe pub/sub event bus for investigation events.

    Supports per-type subscription, event emission to all registered handlers,
    and per-investigation cleanup of listeners.

    Events with no subscribers are discarded silently (no queuing, no error).
    Handler exceptions are logged and do not prevent delivery to other handlers.
    """

    def __init__(self) -> None:
        """Initialize the event bus with an empty subscription registry."""
        # Mapping: EventType -> list of (handler, investigation_id) tuples
        self._subscribers: Dict[EventType, List[Tuple[Callable[[Event], None], Optional[str]]]] = {}
        self._lock = threading.Lock()

    def subscribe(
        self,
        event_type: EventType,
        handler: Callable[[Event], None],
        investigation_id: Optional[str] = None,
    ) -> None:
        """Register a handler for a specific event type.

        Args:
            event_type: The type of event to subscribe to.
            handler: Callable that will be invoked with the Event when emitted.
            investigation_id: Optional investigation ID to scope the subscription.
                If provided, enables cleanup via unsubscribe_all.
        """
        with self._lock:
            if event_type not in self._subscribers:
                self._subscribers[event_type] = []
            self._subscribers[event_type].append((handler, investigation_id))

    def emit(self, event: Event) -> None:
        """Emit an event to all registered handlers for that event type.

        Handlers are called synchronously. If a handler raises an exception,
        it is logged and delivery continues to remaining handlers.

        Events with no subscribers are discarded silently.

        Args:
            event: The event to emit.
        """
        with self._lock:
            handlers = list(self._subscribers.get(event.type, []))

        # No subscribers — discard silently
        if not handlers:
            return

        # Deliver to all registered handlers for this event type
        for handler, _inv_id in handlers:
            try:
                handler(event)
            except Exception as exc:
                logger.error(
                    "Handler %r raised an exception for event %s (investigation=%s): %s",
                    handler,
                    event.type.value,
                    event.investigation_id,
                    exc,
                )

    def unsubscribe_all(self, investigation_id: str) -> None:
        """Remove all handlers registered for a specific investigation.

        This should be called after a terminal event (INVESTIGATION_COMPLETE
        or INVESTIGATION_FAILED) to clean up per-investigation listeners.

        Args:
            investigation_id: The investigation ID whose handlers should be removed.
        """
        with self._lock:
            for event_type in list(self._subscribers.keys()):
                self._subscribers[event_type] = [
                    (handler, inv_id)
                    for handler, inv_id in self._subscribers[event_type]
                    if inv_id != investigation_id
                ]
                # Clean up empty lists
                if not self._subscribers[event_type]:
                    del self._subscribers[event_type]
