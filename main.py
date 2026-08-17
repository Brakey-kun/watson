"""Standalone one-shot CLI for the Watson OSINT Workbench.

Usage: python main.py <target_type> <target>
Example: python main.py Username bazzell

Runs a single investigation via the same tested OSINTEngine that backs the
Flask dashboard (gui.py) - see osint_workbench.engine_factory - and prints
progress to stdout as it goes. For the full web dashboard with history,
live status polling, and the setup wizard, run gui.py (or run.bat/run.sh)
instead.

This used to be its own imperative implementation (fetch loop, LLM calls,
query dedup) duplicating osint_workbench's engine. It's now a thin wrapper
so the CLI and the GUI always run identical investigation logic.
"""
import signal
import sys
import time

from osint_workbench.core.events import Event, EventType
from osint_workbench.core.models import InvestigationConfig
from osint_workbench.engine_factory import build_engine_from_config

# Module-level state for the two-press SIGINT handler, mirroring
# osint_workbench/__main__.py's CLI handler.
_engine = None
_first_sigint_time: float = 0.0
_stop_requested: bool = False


def _sigint_handler(signum, frame):
    """Two-press Ctrl+C semantics.

    First press: graceful stop (engine finishes the current round and
    still writes a report from whatever was found so far). Second press
    within 5s: force-quit immediately with no report.
    """
    global _first_sigint_time, _stop_requested

    now = time.time()
    if _stop_requested and (now - _first_sigint_time) < 5.0:
        print("\n[!] Force terminating. No report will be generated.")
        sys.exit(1)

    _stop_requested = True
    _first_sigint_time = now
    print("\n[*] Graceful stop requested. Finishing current round...")
    print("[*] Press Ctrl+C again within 5s to force quit.")

    if _engine is not None and _engine.is_running and _engine.current_state is not None:
        _engine.stop_investigation(_engine.current_state.investigation_id)


signal.signal(signal.SIGINT, _sigint_handler)


def _print_event(event: Event) -> None:
    """Console progress printer, subscribed to every investigation event."""
    if event.type == EventType.ROUND_STARTED:
        print(f"\n=== Round {event.data.get('round')} ===")
    elif event.type == EventType.ROUND_COMPLETE:
        print(f"[+] Round {event.data.get('round')} complete: "
              f"{event.data.get('findings_count')} findings")
    elif event.type == EventType.QUERY_SKIPPED:
        print(f"[*] Skipping duplicate query: {event.data.get('query')}")
    elif event.type == EventType.STOP_REQUESTED:
        print("[*] Stop requested. Wrapping up current round...")
    elif event.type == EventType.INVESTIGATION_COMPLETE:
        html_path = event.data.get("html_path")
        if html_path:
            print(f"[+] Visual HTML report saved to: {html_path}")
        print("[+] Investigation completed successfully.")
    elif event.type == EventType.INVESTIGATION_FAILED:
        print(f"[-] Investigation failed: {event.data}")
    else:
        print(f"[{event.type.value}] {event.data}")


def run_osint(
    target_type: str,
    target: str,
    max_rounds: str = "Auto",
    urgency: str = "normal OSINT search",
) -> None:
    """Run a single OSINT investigation end-to-end and print progress."""
    global _engine, _stop_requested

    _stop_requested = False
    engine, event_bus, fetcher = build_engine_from_config()
    for event_type in EventType:
        event_bus.subscribe(event_type, _print_event)
    _engine = engine

    try:
        config = InvestigationConfig(
            target=target, category=target_type, max_rounds=max_rounds, urgency=urgency,
        )
        engine.run_investigation(config)
    finally:
        fetcher.close()
        _engine = None


if __name__ == "__main__":
    # One-time move of legacy config.json/investigations.db/reports/ from
    # the app's own source tree into the external per-user data directory
    # (see gui.py's startup hook for why this can't live at module level:
    # `import main` alone must stay a pure, side-effect-free import).
    from osint_workbench.core import paths as _paths
    from osint_workbench.core.integrity import resolve_portable_root as _resolve_portable_root
    _paths.migrate_legacy_data(_resolve_portable_root())

    if len(sys.argv) < 3:
        print("Usage: python main.py <target_type> <target>")
        print("Example: python main.py Username bazzell")
    else:
        run_osint(sys.argv[1], sys.argv[2])
