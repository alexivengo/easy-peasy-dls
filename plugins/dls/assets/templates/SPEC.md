# {{TITLE}} — Specification

ID: `{{ID}}`

## Problem and outcome

{{OUTCOME}}

## Scope

- {{SCOPE_ITEM}}

## Non-goals

- {{NON_GOAL}}

## Current-system discovery

{{DISCOVERY}}

## Requirements and acceptance

- `REQ-001`: {{REQUIREMENT}}

<!-- dls:architecture:start -->
## Architecture and alternatives

{{APPROACH}}
<!-- dls:architecture:end -->

## Interfaces, state, and failure behavior

{{INTERFACES}}

## Security, privacy, data, and operations

{{CROSS_CUTTING}}

## UI/UX contract

<!-- dls:design:start -->
{{UI_SOURCE}}
<!-- dls:design:end -->

For a UI change use one committed contract:

```text
Mode: source
Kind: precedent | artifact | external-version
Reference: repository/path-or-https-url
Version: git:<blob-sha> | immutable-external-version
Rationale: why this source governs the change
```

Or explicitly bypass mockups:

```text
Mode: bypass
Rationale: why implementation without a design source is acceptable
```

## Validation intent

- {{VALIDATION}}

## Risk rationale

{{RISK_RATIONALE}}
