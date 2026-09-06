# Retained legacy naming

The public product name is **Franklin & McGrath**. The retained occurrences below are implementation identifiers or historical material, not a second public product.

| Retained form | Locations | Reason |
| --- | --- | --- |
| `mirrorfirm` | Python package, module paths, schema artifact format identifiers, imports, and tests | Stable internal Python API; renaming would break existing integrations and result-artifact compatibility. |
| `mirror-firm` | Console-script command, CLI examples, cache path, temporary-directory prefixes, and package metadata | Stable developer command and on-disk compatibility. Public documentation labels it as the legacy command. |
| `MIRRORFIRM_*` | Existing environment-variable compatibility, chiefly APEX test configuration | Existing internal configuration name; no public product claim. |
| “Mirror Firm” | `SPEC.md`, `AGENTS.md`, historical commit messages, and Harvey-derived source comments | Normative/historical implementation material retained unchanged for architecture and attribution traceability. |
| “Mirror Firm” in upstream attribution | SPDX headers, `NOTICE`, `LICENSES/`, and derived-source comments | Required source provenance and third-party attribution. |

No public website route, metadata field, launch copy, report heading, form confirmation, or public documentation prose should use the legacy name except when identifying the retained CLI command or source attribution. The exact artifact format `mirrorfirm-aggregate-v1` is a compatibility identifier, not public brand copy.
