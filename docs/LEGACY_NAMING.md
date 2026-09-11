# Retained legacy naming

The public product name is **Open Accountancy**, built by General Intelligence Holdings.
**Franklin & McGrath** is the former public name. The retained occurrences below are implementation identifiers or historical material, not a second public product.

| Retained form | Locations | Reason |
| --- | --- | --- |
| “Franklin & McGrath” | Historical documents, existing result labels and compatibility fixtures | Prior product name retained where changing it would rewrite historical evidence or break artifact compatibility. |
| `mirrorfirm` | Python package, module paths, schema artifact format identifiers, imports, and tests | Stable internal Python API; renaming would break existing integrations and result-artifact compatibility. |
| `mirror-firm` | Console-script command, CLI examples, cache path, temporary-directory prefixes, and package metadata | Stable developer command and on-disk compatibility. Public documentation labels it as the legacy command. |
| `MIRRORFIRM_*` | Existing environment-variable compatibility, chiefly APEX test configuration | Existing internal configuration name; no public product claim. |
| “Mirror Firm” | `SPEC.md`, `AGENTS.md`, historical commit messages, and Harvey-derived source comments | Normative/historical implementation material retained unchanged for architecture and attribution traceability. |
| “Mirror Firm” in upstream attribution | SPDX headers, `NOTICE`, `LICENSES/`, and derived-source comments | Required source provenance and third-party attribution. |

Current website copy and release documentation lead with Open Accountancy. Historical report labels and compatibility identifiers remain unchanged; references to the former name explain that relationship. The exact artifact format `mirrorfirm-aggregate-v1` is a compatibility identifier, not public brand copy.
