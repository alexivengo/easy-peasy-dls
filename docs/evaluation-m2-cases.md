# M2 frozen cases

## Case registry

| Case | Current arm | Reference arm | Nominal attempts | Retry ceiling |
|---|---|---|---:|---:|
| SR-01 | SR-01.current | not-applicable | 1 | 2 |
| SR-02 | SR-02.current | not-applicable | 1 | 2 |
| SR-03 | SR-03.current | SR-03.primary-only | 3 | 4 |
| SR-04 | SR-04.repair | SR-04.fail-closed | 2 | 3 |

## SR-01

### Case fields

| Field | Value |
|---|---|
| case_id | SR-01 |
| claim | clean-control |
| fixture_sha | not-locked |
| tree_digest | not-locked |
| task_input_digest | not-locked |
| oracle_version | not-locked |
| oracle_digest | not-locked |
| oracle_owner | dls-maintainer |
| custody_digest | not-locked |
| repair_access_digest | not-applicable |
| current_manifest_digest | not-locked |
| reference_manifest_digest | not-applicable |
| time_ceiling_seconds | 900 |
| token_ceiling | 12000 |
| privacy | public-synthetic |
| custody_retention | retained-for:365d-after-decision |

### Arm records

| Arm | Expected verdict | Expected lanes | Call contract | Permitted manifest difference | Repair access | Actual verdict | Outcome | Hard oracle | Safety violations | Lanes | Attempts | Successful calls | Finding class |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| SR-01.current | review-clear | primary | primary=1;secondary=0;repair=0;transport-retry<=1 | none | not-applicable | not-run | not-run | not-run | not-run | not-run | not-run | not-run | not-run |

## SR-02

### Case fields

| Field | Value |
|---|---|
| case_id | SR-02 |
| claim | seeded-blocker |
| fixture_sha | not-locked |
| tree_digest | not-locked |
| task_input_digest | not-locked |
| oracle_version | not-locked |
| oracle_digest | not-locked |
| oracle_owner | dls-maintainer |
| custody_digest | not-locked |
| repair_access_digest | not-applicable |
| current_manifest_digest | not-locked |
| reference_manifest_digest | not-applicable |
| time_ceiling_seconds | 900 |
| token_ceiling | 12000 |
| privacy | public-synthetic |
| custody_retention | retained-for:365d-after-decision |

### Arm records

| Arm | Expected verdict | Expected lanes | Call contract | Permitted manifest difference | Repair access | Actual verdict | Outcome | Hard oracle | Safety violations | Lanes | Attempts | Successful calls | Finding class |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| SR-02.current | not-clear | primary | primary=1;secondary=0;repair=0;transport-retry<=1 | none | not-applicable | not-run | not-run | not-run | not-run | not-run | not-run | not-run | not-run |

## SR-03

### Case fields

| Field | Value |
|---|---|
| case_id | SR-03 |
| claim | critical-secondary |
| fixture_sha | not-locked |
| tree_digest | not-locked |
| task_input_digest | not-locked |
| oracle_version | not-locked |
| oracle_digest | not-locked |
| oracle_owner | dls-maintainer |
| custody_digest | not-locked |
| repair_access_digest | not-applicable |
| current_manifest_digest | not-locked |
| reference_manifest_digest | not-locked |
| time_ceiling_seconds | 900 |
| token_ceiling | 12000 |
| privacy | public-synthetic |
| custody_retention | retained-for:365d-after-decision |

### Arm records

| Arm | Expected verdict | Expected lanes | Call contract | Permitted manifest difference | Repair access | Actual verdict | Outcome | Hard oracle | Safety violations | Lanes | Attempts | Successful calls | Finding class |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| SR-03.current | not-clear | primary,secondary | primary=1;secondary=1;repair=0;transport-retry<=1 | none | not-applicable | not-run | not-run | not-run | not-run | not-run | not-run | not-run | not-run |
| SR-03.primary-only | review-clear | primary | primary=1;secondary=0;repair=0;transport-retry<=1 | secondary-lane=disabled | not-applicable | not-run | not-run | not-run | not-run | not-run | not-run | not-run | not-run |

## SR-04

### Case fields

| Field | Value |
|---|---|
| case_id | SR-04 |
| claim | repair-fail-closed |
| fixture_sha | not-locked |
| tree_digest | not-locked |
| task_input_digest | not-locked |
| oracle_version | not-locked |
| oracle_digest | not-locked |
| oracle_owner | dls-maintainer |
| custody_digest | not-locked |
| repair_access_digest | not-locked |
| current_manifest_digest | not-locked |
| reference_manifest_digest | not-locked |
| time_ceiling_seconds | 900 |
| token_ceiling | 12000 |
| privacy | public-synthetic |
| custody_retention | retained-for:365d-after-decision |

### Arm records

| Arm | Expected verdict | Expected lanes | Call contract | Permitted manifest difference | Repair access | Actual verdict | Outcome | Hard oracle | Safety violations | Lanes | Attempts | Successful calls | Finding class |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| SR-04.repair | review-clear | primary | primary=1;secondary=0;repair=1;transport-retry<=1 | repair-mode=compact | source-blind:review-output+format-error | not-run | not-run | not-run | not-run | not-run | not-run | not-run | not-run |
| SR-04.fail-closed | not-applicable | none | primary=0;secondary=0;repair=0;transport-retry<=0 | repair-mode=fail-closed | not-applicable | not-run | not-run | not-run | not-run | not-run | not-run | not-run | not-run |
