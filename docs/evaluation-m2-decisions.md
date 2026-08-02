# M2 release records

## SR-01

### Record fields

| Field | Value |
|---|---|
| case_id | SR-01 |
| run_state | planned |
| plugin_version | not-locked |
| agent_version | not-locked |
| model | not-locked |
| effort | not-locked |
| run_date | not-locked |
| fixture_sha | not-locked |
| tree_digest | not-locked |
| task_input_digest | not-locked |
| oracle_version | not-locked |
| oracle_digest | not-locked |
| custody_digest | not-locked |
| repair_boundary_digest | not-applicable |
| repair_execution_proof_digest | not-applicable |
| current_manifest_digest | not-locked |
| reference_manifest_digest | not-applicable |
| processed_tokens | not-run |
| wall_time_seconds | not-run |
| custody_retention | retained-for:365d-after-decision |
| privacy_retention | not-applicable |

### Arm records

| Arm | Expected verdict | Expected lanes | Call contract | Permitted manifest difference | Repair access | Actual verdict | Outcome | Hard oracle | Safety violations | Lanes | Attempts | Successful calls | Finding class | Execution receipt |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| SR-01.current | review-clear | primary | primary=1;secondary=0;repair=0;transport-retry<=1 | none | not-applicable | not-run | not-run | not-run | not-run | not-run | not-run | not-run | not-run | not-run |

## SR-02

### Record fields

| Field | Value |
|---|---|
| case_id | SR-02 |
| run_state | planned |
| plugin_version | not-locked |
| agent_version | not-locked |
| model | not-locked |
| effort | not-locked |
| run_date | not-locked |
| fixture_sha | not-locked |
| tree_digest | not-locked |
| task_input_digest | not-locked |
| oracle_version | not-locked |
| oracle_digest | not-locked |
| custody_digest | not-locked |
| repair_boundary_digest | not-applicable |
| repair_execution_proof_digest | not-applicable |
| current_manifest_digest | not-locked |
| reference_manifest_digest | not-applicable |
| processed_tokens | not-run |
| wall_time_seconds | not-run |
| custody_retention | retained-for:365d-after-decision |
| privacy_retention | not-applicable |

### Arm records

| Arm | Expected verdict | Expected lanes | Call contract | Permitted manifest difference | Repair access | Actual verdict | Outcome | Hard oracle | Safety violations | Lanes | Attempts | Successful calls | Finding class | Execution receipt |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| SR-02.current | not-clear | primary | primary=1;secondary=0;repair=0;transport-retry<=1 | none | not-applicable | not-run | not-run | not-run | not-run | not-run | not-run | not-run | not-run | not-run |

## SR-03

### Record fields

| Field | Value |
|---|---|
| case_id | SR-03 |
| run_state | planned |
| plugin_version | not-locked |
| agent_version | not-locked |
| model | not-locked |
| effort | not-locked |
| run_date | not-locked |
| fixture_sha | not-locked |
| tree_digest | not-locked |
| task_input_digest | not-locked |
| oracle_version | not-locked |
| oracle_digest | not-locked |
| custody_digest | not-locked |
| repair_boundary_digest | not-applicable |
| repair_execution_proof_digest | not-applicable |
| current_manifest_digest | not-locked |
| reference_manifest_digest | not-locked |
| processed_tokens | not-run |
| wall_time_seconds | not-run |
| custody_retention | retained-for:365d-after-decision |
| privacy_retention | not-applicable |

### Arm records

| Arm | Expected verdict | Expected lanes | Call contract | Permitted manifest difference | Repair access | Actual verdict | Outcome | Hard oracle | Safety violations | Lanes | Attempts | Successful calls | Finding class | Execution receipt |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| SR-03.current | not-clear | primary,secondary | primary=1;secondary=1;repair=0;transport-retry<=1 | none | not-applicable | not-run | not-run | not-run | not-run | not-run | not-run | not-run | not-run | not-run |
| SR-03.primary-only | review-clear | primary | primary=1;secondary=0;repair=0;transport-retry<=1 | secondary-lane=disabled | not-applicable | not-run | not-run | not-run | not-run | not-run | not-run | not-run | not-run | not-run |

## SR-04

### Record fields

| Field | Value |
|---|---|
| case_id | SR-04 |
| run_state | planned |
| plugin_version | not-locked |
| agent_version | not-locked |
| model | not-locked |
| effort | not-locked |
| run_date | not-locked |
| fixture_sha | not-locked |
| tree_digest | not-locked |
| task_input_digest | not-locked |
| oracle_version | not-locked |
| oracle_digest | not-locked |
| custody_digest | not-locked |
| repair_boundary_digest | not-locked |
| repair_execution_proof_digest | not-run |
| current_manifest_digest | not-locked |
| reference_manifest_digest | not-locked |
| processed_tokens | not-run |
| wall_time_seconds | not-run |
| custody_retention | retained-for:365d-after-decision |
| privacy_retention | not-applicable |

### Arm records

| Arm | Expected verdict | Expected lanes | Call contract | Permitted manifest difference | Repair access | Actual verdict | Outcome | Hard oracle | Safety violations | Lanes | Attempts | Successful calls | Finding class | Execution receipt |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| SR-04.repair | review-clear | primary | primary=1;secondary=0;repair=1;transport-retry<=1 | repair-mode=compact | source-blind:review-output+format-error | not-run | not-run | not-run | not-run | not-run | not-run | not-run | not-run | not-run |
| SR-04.fail-closed | not-applicable | none | primary=0;secondary=0;repair=0;transport-retry<=0 | repair-mode=fail-closed | not-applicable | not-run | not-run | not-run | not-run | not-run | not-run | not-run | not-run | not-run |

## M2 decision

### Decision fields

| Field | Value |
|---|---|
| decision_state | pending-live-sample |
| decision_date | not-run |
| m2_outcome | not-run |
| decision | not-applicable |
| evidence | not-applicable |
