"""Report generator that renders Watson's polished "Odysseus" themed HTML
(dark mode, sidebar TOC, card grids, metadata stats) instead of the modern
engine's basic template.

osint_workbench.reporting.generator.ReportGenerator._render_html wraps
sanitized markdown in a small, plain _HTML_TEMPLATE. visual_report.py's
generate_visual_report() (used by the legacy main.py/gui.py path) is the
actual polished report the README advertises as a headline feature ("slick
HTML reports with dark mode, sidebar TOC, card grids, and metadata stats").

VisualReportGenerator overrides ONLY the HTML-rendering step and reuses
ReportGenerator's filename sanitization, Markdown/PDF writing, and
intermediate-artifact logic unchanged, so migrating gui.py onto
OSINTEngine does not downgrade the actual deliverable users see.
"""
from __future__ import annotations

from osint_workbench.core.models import InvestigationState
from osint_workbench.reporting.generator import ReportGenerator


class VisualReportGenerator(ReportGenerator):
    """ReportGenerator subclass that renders via visual_report.generate_visual_report()."""

    def _render_html(
        self,
        state: InvestigationState,
        report_markdown: str,
        date_str: str,
    ) -> str:
        # Imported lazily: visual_report.py lives at the repo root (legacy
        # entry-point module), not inside the osint_workbench package.
        from visual_report import generate_visual_report

        target = state.config.target if state.config.target else "Unknown"
        category = state.config.category if state.config.category else "Unknown"

        sources = []
        if state.findings:
            for finding in state.findings.values():
                if isinstance(finding, dict):
                    sources.append({
                        "url": finding.get("url", ""),
                        "name": finding.get("name", ""),
                        "title": finding.get("title") or finding.get("name", ""),
                        "snippet": finding.get("snippet", ""),
                        "status": finding.get("status", ""),
                        "category": finding.get("category", ""),
                    })
                else:
                    sources.append({
                        "url": getattr(finding, "url", ""),
                        "name": getattr(finding, "name", ""),
                        "title": getattr(finding, "title", None) or getattr(finding, "name", ""),
                        "snippet": getattr(finding, "snippet", "") or "",
                        "status": getattr(finding, "status", ""),
                        "category": getattr(finding, "category", ""),
                    })

        active_count = sum(1 for s in sources if s["status"] == "Active/Accessible")
        stats = {
            "Duration": f"{state.elapsed_seconds:.1f}s",
            "Rounds": state.current_round,
            "Queries": len(sources),
            "URLs": active_count,
        }

        return generate_visual_report(
            question=f"OSINT Investigation: {target}",
            report_markdown=report_markdown,
            sources=sources,
            stats=stats,
            category="comparison" if category == "Domain" else None,
        )
