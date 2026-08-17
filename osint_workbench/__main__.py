"""Entry point for the Watson OSINT Workbench application.

Run with: python -m osint_workbench
"""

import signal
import sys
import time

# Module-level state for the two-press SIGINT handler
_first_sigint_time: float = 0.0
_stop_requested: bool = False
_engine = None  # Will be set after engine creation


def _sigint_handler(signum, frame):
    """Handle Ctrl+C with two-press semantics.

    First press: graceful stop (set engine stop event).
    Second press within 5s: force terminate.
    """
    global _first_sigint_time, _stop_requested

    now = time.time()

    if _stop_requested and (now - _first_sigint_time) < 5.0:
        # Second press within 5s: force quit
        print("\n[!] Force terminating. No report will be generated.")
        sys.exit(1)

    # First press: graceful stop
    _stop_requested = True
    _first_sigint_time = now
    print("\n[*] Graceful stop requested. Finishing current round...")
    print("[*] Press Ctrl+C again within 5s to force quit.")

    # Set the engine stop event if the engine is running
    if _engine and _engine.is_running:
        _engine._stop_event.set()


# Register the signal handler
signal.signal(signal.SIGINT, _sigint_handler)


def main():
    """Start the Watson OSINT Workbench Flask application."""
    global _engine, _stop_requested

    print("Watson OSINT Workbench starting...")
    try:
        from osint_workbench.app import create_app
        from osint_workbench.core import paths
        from osint_workbench.core.integrity import resolve_portable_root

        # One-time move of legacy config.json/investigations.db/reports/
        # from the app's own source tree into the external per-user data
        # directory (see gui.py's startup hook for the full rationale).
        paths.migrate_legacy_data(resolve_portable_root())

        app, socketio = create_app()

        # Extract engine reference from the app for signal handler access
        _engine = app.config.get("ENGINE")

        # Reset stop state for fresh start
        _stop_requested = False

        socketio.run(app, host="127.0.0.1", port=5000)
    except ImportError:
        print(
            "Application not yet fully configured. "
            "Run task 16.1 to wire up all components.",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
