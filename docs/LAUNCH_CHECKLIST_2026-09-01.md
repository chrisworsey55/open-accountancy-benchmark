# Franklin & McGrath launch checklist — 1 September 2026

## Before publishing

- [ ] Configure a production WSGI host and HTTPS domain; no deployment configuration exists in this repository.
- [ ] Set `FRANKLIN_MCGATH_LEADS_DB` to a durable, access-controlled path outside the repository and verify backup/retention responsibilities.
- [ ] Set `FRANKLIN_MCGATH_RESULTS_ROOT` to the reviewed directory of verified aggregate artifacts, if any results are to appear.
- [ ] Run the local launch routes and submit both forms; confirm the database records only the test submissions and is not publicly served.
- [ ] Complete a privacy/retention review for developer and accountancy-firm lead data, including contact channel and deletion process.
- [ ] Review every result artifact and only publish genuine model results; keep scripted references separate.
- [ ] Confirm any listed organisation, run date, or human-review field is supported by the artifact schema; otherwise leave it `N/A`.
- [ ] Run format, lint, strict typing, tests, docs, release check, validation of both worlds, and the credential-free demo.
- [ ] Perform the required human review for accidental real data and all licence/notice obligations.
- [ ] Verify the production domain’s title, description, Open Graph fields, mobile layout, keyboard navigation, form error states, and all navigation links.
- [ ] Confirm the launch-date copy says 1 September 2026 and that no unauthorised deployment, release, or repository publication action is taken from this task.

## Do not launch if

- A critical-failure result appears as an ordinary ranked entry.
- A scripted reference is presented as model performance.
- A form reports success when durable persistence fails.
- The lead store, results store, or a production deployment is not access-controlled.
- A public claim is unsupported by a verified artifact or test.
