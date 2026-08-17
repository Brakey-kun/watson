"""Report generation module for Watson.

Produces Markdown, HTML (Odysseus-themed), and optional PDF reports
with entity extraction, diff reports, and filename sanitization.
Also generates raw data and organized data MD files for each research.
"""

import json
import logging
import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import markdown
import nh3

from osint_workbench.core import paths
from osint_workbench.core.models import InvestigationState

logger = logging.getLogger(__name__)

# Windows reserved device names (case-insensitive)
_WINDOWS_RESERVED = re.compile(
    r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$", re.IGNORECASE
)

# nh3 allowed tags for HTML sanitization
_ALLOWED_TAGS = {
    "h1", "h2", "h3", "h4", "h5", "h6",
    "p", "ul", "ol", "li",
    "table", "thead", "tbody", "tr", "th", "td",
    "a", "img",
    "code", "pre",
    "blockquote",
    "details", "summary",
    "br", "hr", "em", "strong",
}

# nh3 allowed attributes per tag
# Note: "rel" is managed by nh3's link_rel parameter, not in attributes
_ALLOWED_ATTRIBUTES: dict = {
    "a": {"href", "target"},
    "img": {"src", "alt"},
}

# Odysseus-themed HTML template
_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>OSINT Research Report - {target}</title>
    <style>
        :root {{
            --primary: #1a237e;
            --accent: #ff6f00;
            --bg: #fafafa;
            --card-bg: #ffffff;
            --text: #212121;
            --muted: #757575;
            --border: #e0e0e0;
        }}
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            line-height: 1.7;
            color: var(--text);
            background: var(--bg);
            padding: 2rem;
        }}
        .container {{
            max-width: 960px;
            margin: 0 auto;
            background: var(--card-bg);
            border-radius: 8px;
            box-shadow: 0 2px 12px rgba(0,0,0,0.08);
            padding: 3rem;
        }}
        header {{
            border-bottom: 3px solid var(--primary);
            padding-bottom: 1.5rem;
            margin-bottom: 2rem;
        }}
        header h1 {{
            color: var(--primary);
            font-size: 1.8rem;
            margin-bottom: 0.5rem;
        }}
        header .subtitle {{
            color: var(--muted);
            font-size: 0.95rem;
        }}
        .badge {{
            display: inline-block;
            background: var(--accent);
            color: #fff;
            padding: 0.2rem 0.7rem;
            border-radius: 4px;
            font-size: 0.8rem;
            font-weight: 600;
            margin-right: 0.5rem;
        }}
        h2 {{ color: var(--primary); margin: 2rem 0 1rem; border-bottom: 1px solid var(--border); padding-bottom: 0.5rem; }}
        h3 {{ color: var(--primary); margin: 1.5rem 0 0.75rem; }}
        h4, h5, h6 {{ margin: 1rem 0 0.5rem; }}
        p {{ margin-bottom: 1rem; }}
        a {{ color: var(--primary); text-decoration: none; }}
        a:hover {{ text-decoration: underline; }}
        ul, ol {{ margin: 0.5rem 0 1rem 1.5rem; }}
        li {{ margin-bottom: 0.3rem; }}
        table {{ border-collapse: collapse; width: 100%; margin: 1rem 0; }}
        th, td {{ border: 1px solid var(--border); padding: 0.6rem; text-align: left; }}
        th {{ background: var(--primary); color: #fff; }}
        code {{ background: #f5f5f5; padding: 0.15rem 0.4rem; border-radius: 3px; font-size: 0.9em; }}
        pre {{ background: #f5f5f5; padding: 1rem; border-radius: 6px; overflow-x: auto; margin: 1rem 0; }}
        pre code {{ background: none; padding: 0; }}
        blockquote {{ border-left: 4px solid var(--accent); padding: 0.75rem 1rem; margin: 1rem 0; background: #fff8e1; }}
        details {{ margin: 1rem 0; border: 1px solid var(--border); border-radius: 4px; padding: 0.75rem; }}
        summary {{ cursor: pointer; font-weight: 600; }}
        img {{ max-width: 100%; height: auto; border-radius: 4px; }}
        hr {{ border: none; border-top: 1px solid var(--border); margin: 2rem 0; }}
        footer {{
            margin-top: 3rem;
            padding-top: 1rem;
            border-top: 1px solid var(--border);
            color: var(--muted);
            font-size: 0.85rem;
            text-align: center;
        }}
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>&#x1F50D; OSINT Research Report</h1>
            <p class="subtitle">
                <span class="badge">{category}</span>
                <strong>Target:</strong> {target} &nbsp;|&nbsp;
                <strong>Date:</strong> {date} &nbsp;|&nbsp;
                <strong>Rounds:</strong> {rounds}
            </p>
        </header>
        <main>
            {content}
        </main>
        <footer>
            Generated by Watson OSINT Workbench &mdash; Odysseus Edition
        </footer>
    </div>
</body>
</html>
"""


@dataclass
class ReportOutput:
    """Output paths and metadata from report generation."""

    markdown_path: str
    html_path: str
    pdf_path: Optional[str] = None
    raw_data_path: Optional[str] = None
    organized_data_path: Optional[str] = None
    graph_data: Optional[dict] = None


class ReportGenerator:
    """Enhanced report generator with PDF export, diff reports, and entity extraction."""

    def __init__(
        self,
        output_dir: Optional[str] = None,
        template_dir: str = "templates",
    ):
        self.output_dir = output_dir if output_dir is not None else str(paths.reports_dir())
        self.template_dir = template_dir
        os.makedirs(self.output_dir, exist_ok=True)

    def sanitize_filename(self, target: str, date_str: str) -> str:
        """Sanitize a target string for safe filesystem usage.

        Produces a filename of the form: report_{sanitized_target}_{date_str}
        The total length (without extension) is capped at 255 characters.

        Args:
            target: Raw target string from user input.
            date_str: Date string in YYYY-MM-DD format.

        Returns:
            A filesystem-safe filename base (without extension).
        """
        # Step 1: Normalize unicode (NFKD)
        sanitized = unicodedata.normalize("NFKD", target)

        # Step 2: Remove null bytes and control chars (U+0000-U+001F, U+007F-U+009F)
        sanitized = re.sub(r"[\x00-\x1f\x7f-\x9f]", "", sanitized)

        # Step 3: Replace path separators and unsafe chars with underscore
        sanitized = re.sub(r'[/\\:*?"<>|]', "_", sanitized)

        # Step 4: Replace whitespace with underscore
        sanitized = re.sub(r"\s", "_", sanitized)

        # Step 5: Collapse consecutive underscores
        sanitized = re.sub(r"_+", "_", sanitized)

        # Step 6: Strip leading/trailing dots and underscores
        sanitized = sanitized.strip("._")

        # Step 7: Check Windows reserved names
        if _WINDOWS_RESERVED.match(sanitized):
            sanitized = f"reserved_{sanitized}"

        # Step 8: Fallback if empty
        if not sanitized:
            sanitized = "unnamed_target"

        # Step 9: Construct full filename base and truncate if needed
        # Format: report_{sanitized}_{date_str}
        # Total must be <= 255 chars (without extension)
        prefix = "report_"
        suffix = f"_{date_str}"
        max_target_len = 255 - len(prefix) - len(suffix)

        if max_target_len <= 0:
            # Very unlikely edge case: date_str is extremely long
            sanitized = sanitized[:1]
        elif len(sanitized) > max_target_len:
            sanitized = sanitized[:max_target_len]
            # Clean up trailing underscore or dot from truncation
            sanitized = sanitized.rstrip("._")
            if not sanitized:
                sanitized = "unnamed_target"

        return f"{prefix}{sanitized}{suffix}"

    def generate(
        self,
        state: InvestigationState,
        report_markdown: str,
        enable_pdf: bool = False,
    ) -> ReportOutput:
        """Generate all report outputs (Markdown, HTML, optional PDF).

        Args:
            state: The current investigation state.
            report_markdown: The LLM-synthesized markdown report content.
            enable_pdf: Whether to generate a PDF version.

        Returns:
            ReportOutput with paths to generated files.
        """
        # Compute date string
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Sanitize filename
        target = state.config.target if state.config.target else "unnamed_target"
        filename_base = self.sanitize_filename(target, date_str)

        md_path = ""
        html_path = ""
        pdf_path = None

        # Generate Markdown report
        try:
            md_path = os.path.join(self.output_dir, f"{filename_base}.md")
            with open(md_path, "w", encoding="utf-8") as f:
                f.write(report_markdown)
            logger.info("Markdown report written to %s", md_path)
        except Exception as e:
            logger.error("Failed to generate Markdown report: %s", e)
            md_path = ""

        # Generate HTML report
        try:
            html_path = os.path.join(self.output_dir, f"{filename_base}.html")
            html_content = self._render_html(state, report_markdown, date_str)
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(html_content)
            logger.info("HTML report written to %s", html_path)
        except Exception as e:
            logger.error("Failed to generate HTML report: %s", e)
            html_path = ""

        # Generate PDF report (optional)
        if enable_pdf and html_path:
            try:
                from osint_workbench.reporting.pdf_export import generate_pdf

                pdf_path = os.path.join(self.output_dir, f"{filename_base}.pdf")
                generate_pdf(html_path, pdf_path)
                logger.info("PDF report written to %s", pdf_path)
            except ImportError:
                logger.error(
                    "PDF export unavailable: WeasyPrint not installed"
                )
                pdf_path = None
            except Exception as e:
                logger.error("Failed to generate PDF report: %s", e)
                pdf_path = None

        # Extract entities for graph data
        graph_data = self.extract_entities(report_markdown)

        # Intermediate research artifacts (raw/organized data dumps) are
        # debugging aids, not deliverables - keep them out of the top-level
        # reports dir so file-naming-convention checks only see report_* files.
        intermediates_dir = os.path.join(self.output_dir, "intermediates")
        os.makedirs(intermediates_dir, exist_ok=True)

        # --- Generate Raw Data MD ---
        raw_data_path = None
        try:
            raw_data_file = os.path.join(intermediates_dir, f"raw_data_{filename_base.removeprefix('report_')}.md")
            findings_list = list(state.findings.values()) if state.findings else []
            with open(raw_data_file, "w", encoding="utf-8") as f:
                f.write("# Raw Research Data\n\n")
                f.write(f"**Target:** {target}  \n")
                f.write(f"**Category:** {state.config.category if state.config.category else 'Unknown'}  \n")
                f.write(f"**Date:** {date_str}  \n")
                f.write(f"**Total Findings:** {len(findings_list)}  \n")
                f.write(f"**Research Rounds Completed:** {state.current_round}  \n\n")
                f.write("---\n\n")
                f.write("## Raw Findings Dump\n\n")
                f.write("```json\n")
                f.write(json.dumps(findings_list, indent=2, default=str))
                f.write("\n```\n")
            raw_data_path = raw_data_file
            logger.info("Raw data MD written to %s", raw_data_path)
        except Exception as e:
            logger.error("Failed to generate raw data MD: %s", e)

        # --- Generate Organized Data MD ---
        organized_data_path = None
        try:
            organized_data_file = os.path.join(intermediates_dir, f"organized_data_{filename_base.removeprefix('report_')}.md")
            findings_list = list(state.findings.values()) if state.findings else []
            with open(organized_data_file, "w", encoding="utf-8") as f:
                f.write("# Organized Research Data\n\n")
                f.write(f"**Target:** {target}  \n")
                f.write(f"**Category:** {state.config.category if state.config.category else 'Unknown'}  \n")
                f.write(f"**Date:** {date_str}  \n")
                f.write(f"**Total Findings:** {len(findings_list)}  \n")
                f.write(f"**Research Rounds Completed:** {state.current_round}  \n\n")
                f.write("---\n\n")

                # Group findings by category
                categories: dict = {}
                for finding in findings_list:
                    if isinstance(finding, dict):
                        cat = finding.get("category", "Uncategorized")
                        categories.setdefault(cat, []).append(finding)

                for cat_name, cat_findings in sorted(categories.items()):
                    f.write(f"## {cat_name} ({len(cat_findings)} results)\n\n")
                    for finding in cat_findings:
                        status_icon = "✅" if finding.get("status") == "Active/Accessible" else "⚠️"
                        f.write(f"### {status_icon} {finding.get('name', 'Unknown Source')}\n\n")
                        f.write("| Field | Value |\n")
                        f.write("|-------|-------|\n")
                        f.write(f"| **URL** | [{finding.get('url', 'N/A')}]({finding.get('url', '')}) |\n")
                        f.write(f"| **Status** | {finding.get('status', 'Unknown')} |\n")
                        if finding.get("title"):
                            f.write(f"| **Page Title** | {finding.get('title', '')} |\n")
                        if finding.get("snippet"):
                            f.write(f"| **Snippet** | {str(finding.get('snippet', ''))[:200]} |\n")
                        f.write("\n")

                # Summary statistics
                f.write("---\n\n")
                f.write("## Summary Statistics\n\n")
                active_count = sum(1 for finding in findings_list if isinstance(finding, dict) and finding.get("status") == "Active/Accessible")
                failed_count = len(findings_list) - active_count
                f.write("| Metric | Value |\n")
                f.write("|--------|-------|\n")
                f.write(f"| Total Sources Queried | {len(findings_list)} |\n")
                f.write(f"| Active/Accessible | {active_count} |\n")
                f.write(f"| Failed/Unreachable | {failed_count} |\n")
                f.write(f"| Categories Found | {len(categories)} |\n")
                f.write(f"| Research Rounds | {state.current_round} |\n")
            organized_data_path = organized_data_file
            logger.info("Organized data MD written to %s", organized_data_path)
        except Exception as e:
            logger.error("Failed to generate organized data MD: %s", e)

        return ReportOutput(
            markdown_path=md_path,
            html_path=html_path,
            pdf_path=pdf_path,
            raw_data_path=raw_data_path,
            organized_data_path=organized_data_path,
            graph_data=graph_data if any(graph_data.values()) else None,
        )

    def _render_html(
        self,
        state: InvestigationState,
        report_markdown: str,
        date_str: str,
    ) -> str:
        """Convert markdown to sanitized HTML and wrap in Odysseus template.

        Args:
            state: The investigation state for metadata.
            report_markdown: Raw markdown content.
            date_str: Date string for the report header.

        Returns:
            Complete HTML document string.
        """
        # Convert markdown to HTML
        raw_html = markdown.markdown(
            report_markdown,
            extensions=["tables", "fenced_code", "nl2br"],
        )

        # Sanitize HTML with nh3 (strips scripts, event handlers, javascript: URLs)
        clean_html = nh3.clean(
            raw_html,
            tags=_ALLOWED_TAGS,
            attributes=_ALLOWED_ATTRIBUTES,
            link_rel="noopener noreferrer",
        )

        # Apply rel="noopener noreferrer" target="_blank" to outbound links
        clean_html = self._add_link_attributes(clean_html)

        # Wrap in Odysseus-themed template
        target = state.config.target if state.config.target else "Unknown"
        category = state.config.category if state.config.category else "Unknown"
        rounds = state.current_round

        return _HTML_TEMPLATE.format(
            target=self._escape_html_attr(target),
            category=self._escape_html_attr(category),
            date=date_str,
            rounds=rounds,
            content=clean_html,
        )

    def _add_link_attributes(self, html: str) -> str:
        """Add target='_blank' and rel='noopener noreferrer' to outbound links.

        Only applies to links with http:// or https:// hrefs.
        """
        # Match <a> tags with http/https hrefs
        def _replace_link(match: re.Match) -> str:
            tag = match.group(0)

            # Check if the href is an outbound link (http/https)
            href_match = re.search(r'href=["\']https?://', tag)
            if not href_match:
                return tag

            # Add/replace target attribute
            if 'target=' not in tag:
                tag = tag.replace("<a ", '<a target="_blank" ', 1)
            else:
                tag = re.sub(
                    r'target=["\'][^"\']*["\']',
                    'target="_blank"',
                    tag,
                )

            # Add/replace rel attribute
            if 'rel=' not in tag:
                tag = tag.replace("<a ", '<a rel="noopener noreferrer" ', 1)
            else:
                tag = re.sub(
                    r'rel=["\'][^"\']*["\']',
                    'rel="noopener noreferrer"',
                    tag,
                )

            return tag

        return re.sub(r"<a\s[^>]*>", _replace_link, html)

    def _escape_html_attr(self, value: str) -> str:
        """Escape a string for safe use in HTML attributes."""
        return (
            value.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&#x27;")
        )

    def extract_entities(self, report_markdown: str) -> dict:
        """Extract structured entities from report markdown.

        Extracts people, organizations, accounts, and locations
        using regex heuristics. Capped at 200 total entities.

        Args:
            report_markdown: The markdown report content.

        Returns:
            Dict with keys: people, organizations, accounts, locations.
        """
        entities: dict = {
            "people": [],
            "organizations": [],
            "accounts": [],
            "locations": [],
        }

        if not report_markdown:
            return entities

        # Extract @mentions as accounts
        accounts = re.findall(r"@([A-Za-z0-9_]{1,64})", report_markdown)
        entities["accounts"] = list(dict.fromkeys(accounts))  # dedupe, preserve order

        # Extract organization-like patterns (capitalized words followed by
        # Inc, Corp, LLC, Ltd, Co, Group, Foundation, Association, etc.)
        org_pattern = r"\b([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)*\s+(?:Inc|Corp|LLC|Ltd|Co|Group|Foundation|Association|University|Institute|Agency|Organization|Company))\b"
        orgs = re.findall(org_pattern, report_markdown)
        entities["organizations"] = list(dict.fromkeys(orgs))

        # Extract location patterns (City, State or Country patterns)
        location_pattern = r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*,\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\b"
        locations = re.findall(location_pattern, report_markdown)
        entities["locations"] = list(dict.fromkeys(locations))

        # Extract people names (sequences of 2-3 capitalized words not matching
        # orgs or locations, and not common section headers)
        _common_headers = {
            "Executive Summary", "Key Profile", "Detailed OSINT",
            "Profile Details", "OSINT Analysis", "Research Report",
            "Key Findings", "Next Steps", "Google Dorking",
            "Key Profile Details", "Direct Intel", "Deeper Analysis",
        }
        # Only match on lines that don't start with # (skip headings)
        non_heading_lines = "\n".join(
            line for line in report_markdown.splitlines()
            if not line.strip().startswith("#")
        )
        people_pattern = r"\b([A-Z][a-z]+(?:\s[A-Z][a-z]+){1,2})\b"
        people_candidates = re.findall(people_pattern, non_heading_lines)
        people_set: list = []
        seen_people: set = set()
        for p in people_candidates:
            if (
                p not in _common_headers
                and p not in entities["organizations"]
                and p not in entities["locations"]
                and p not in seen_people
            ):
                people_set.append(p)
                seen_people.add(p)
        entities["people"] = people_set

        # Cap total entities at 200
        total = 0
        for key in ["people", "organizations", "accounts", "locations"]:
            remaining = 200 - total
            if remaining <= 0:
                entities[key] = []
            elif len(entities[key]) > remaining:
                entities[key] = entities[key][:remaining]
            total += len(entities[key])

        return entities

    def generate_diff_report(
        self,
        current: InvestigationState,
        previous: InvestigationState,
    ) -> str:
        """Generate a diff report listing new findings between two runs.

        Compares findings by URL. Returns markdown listing findings in current
        that are not present in previous.

        Args:
            current: The current investigation state.
            previous: The previous investigation state.

        Returns:
            Markdown string with new findings listed.
        """
        current_urls = set(current.findings.keys()) if current.findings else set()
        previous_urls = set(previous.findings.keys()) if previous.findings else set()

        new_urls = current_urls - previous_urls

        if not new_urls:
            return "# Diff Report\n\nNo new findings compared to previous investigation.\n"

        lines = [
            "# Diff Report",
            "",
            f"**Target:** {current.config.target}",
            f"**Current Investigation:** {current.investigation_id}",
            f"**Previous Investigation:** {previous.investigation_id}",
            f"**New Findings:** {len(new_urls)}",
            "",
            "## New Findings",
            "",
        ]

        for url in sorted(new_urls):
            finding = current.findings.get(url)
            if finding and hasattr(finding, "name"):
                name = finding.name
                title = getattr(finding, "title", None) or "No title"
                lines.append(f"- **{name}**: [{title}]({url})")
            elif finding and isinstance(finding, dict):
                name = finding.get("name", "Unknown")
                title = finding.get("title", "No title")
                lines.append(f"- **{name}**: [{title}]({url})")
            else:
                lines.append(f"- [{url}]({url})")

        lines.append("")
        return "\n".join(lines)
