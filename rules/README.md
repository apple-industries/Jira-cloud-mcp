# Automation rules (as code)

Rule definitions for the booth-setup automation initiative (OPS-20). Authored as JSON and
deployed via the MCP's `create_automation_rule` tool, which POSTs to Jira's internal
automation API (`.../pro/rest/{scope}/rules`).

## Files
- `PI-167-import-unit-to-back-office.json` — manual "Import Unit to Back Office" rule
  (scope: **CUSTSVC**). See [PI-167](https://apple-industries.atlassian.net/browse/PI-167).

## Deploy / validate loop (all live steps need Jesse's approval)
1. Reload the MCP server so it exposes the new `create_automation_rule` / `delete_automation_rule` tools.
2. Fill placeholders (see below), then create the rule **DISABLED** in the `PI` or `CUSTSVC` scope:
   `create_automation_rule(rule_json=<file>, project_key="CUSTSVC")`.
3. **Round-trip validate:** `get_automation_rule(<new_id>, project_key="CUSTSVC")`, diff against
   this file, and fix any component the internal API rewrote/rejected (condition operators,
   response-branch smart values, and the import envelope shape are the likely spots).
4. Test on one real Software Setup ticket (dry — expect a 204 or a clear 400 comment).
5. Enable once verified.

## Placeholders / TODOs before deploy
- `<<ORCHARD_BASIC_AUTH_TOKEN>>` — reuse the same Basic-auth credential as the existing
  FTS-toggle rule (do **not** commit the real value; inject at deploy). Tracked cleanup: rotate
  this shared credential into a proper secret.
- `product_code` — derived in-rule as the **last 3 chars of the serial** (Orchard serials are
  `MMYY+seq+product_code`), so no mapping table is needed. Uses `.length.minus(3)` + `.substring`
  smart values — confirm these render as expected during round-trip.
- `content_code` — **omitted**: the import endpoint does not validate it (defaulted downstream).
  Content is handled by the SmileContent+/FTS flow, not this rule.
- Confirm the internal create endpoint/envelope: `/import` expects a rules payload — verify
  whether it wants `{"rules":[<rule>]}` vs the single-rule `{"rule":{...}}` shape used here
  (adjust `create_automation_rule`'s `path`/payload once confirmed by a test create).
- Confirm the CUSTSVC→EPRO link direction name(s) for the branch `linkTypes`
  (used "is a prerequisite of" / "depends on").

## Notes
- Inside the `jira.issue.related` branch, `{{issue.*}}` = the linked **EPRO** issue and
  `{{triggerIssue.*}}` = the **CUSTSVC** Software Setup issue.
- Import semantics (from `com.fpp.orchard.unit.websvc` → `UnitImportService`): 204 = success,
  400 = per-unit error list; `"Serial Number already exists"` = already imported (treat as OK).
