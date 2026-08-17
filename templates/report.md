# Watson OSINT Research Report
**Date:** {{ date }}
**Target Type:** {{ target_type }}
**Target:** {{ target }}

---

## Executive Summary
{{ summary }}

---

## Sources & Findings

### Direct Lookup Tools
{% for source in findings %}{% if source.category == "Direct Lookup" %}
#### {{ source.name }}
- **Source URL:** [{{ source.url }}]({{ source.url }})
- **Status:** {{ source.status }}
- **Findings Summary:**
  ```text
  {{ source.notes }}
  ```
{% endif %}{% endfor %}

### Google Dorking Queries
{% for source in findings %}{% if source.category == "Google Dorking Query" %}
#### {{ source.name }}
- **Source URL:** [{{ source.url }}]({{ source.url }})
- **Status:** {{ source.status }}
- **Findings Summary:**
  ```text
  {{ source.notes }}
  ```
{% endif %}{% endfor %}

---

## Deeper Analysis & Intelligence Synthesis
{{ deep_analysis }}

---

## Conclusion & Next Steps
{{ conclusion }}
