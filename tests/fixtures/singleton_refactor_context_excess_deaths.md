Function to refactor:

```python
def cell_some_sheet_z22(ctx):
    return (
        xl_number(united_states_total_deaths(ctx))
        - xl_number(united_states_expected_deaths(ctx))
    )
```

Cell metadata:

```yaml
address: SomeSheet!Z22
table_labels:
  - label: United States Vital Statistics
    concept: UNITED_STATES_VITAL_STATISTICS
row_labels:
  - label: Excess Deaths
    concept: EXCESS_DEATHS
column_labels: []
```

Dependencies:

```python
def xl_number(value: CellValue) -> float:
    """Coerce a scalar cell value to a number, raising on Excel errors."""
    # ...

def united_states_expected_deaths(ctx: EvalContext) -> float:
    """
    Expected deaths for the United States.

    Args:
        ctx: Workbook evaluation context.

    Returns:
        Expected deaths for the United States: baseline number of deaths that
        would have occurred in the absence of the shock.
    """
    # ...

def united_states_total_deaths(ctx: EvalContext) -> float:
    """
    Total observed deaths in the United States.

    Args:
        ctx: Workbook evaluation context.

    Returns:
        United States total deaths.
    """
    # ...
```
