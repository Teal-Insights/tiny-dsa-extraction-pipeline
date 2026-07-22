Cluster to refactor:

```python
def cell_forecast_b12(ctx):
    return (
        1.0
        if xl_compare(">=", xl_cell(ctx, "Forecast!B4"), xl_cell(ctx, "Assumptions!C2"))
        else 0.0
    )


def cell_forecast_c12(ctx):
    return (
        1.0
        if xl_compare(">=", xl_cell(ctx, "Forecast!C4"), xl_cell(ctx, "Assumptions!C2"))
        else 0.0
    )


def cell_forecast_d12(ctx):
    return (
        1.0
        if xl_compare(">=", xl_cell(ctx, "Forecast!D4"), xl_cell(ctx, "Assumptions!C2"))
        else 0.0
    )


def cell_forecast_e12(ctx):
    return (
        1.0
        if xl_compare(">=", xl_cell(ctx, "Forecast!E4"), xl_cell(ctx, "Assumptions!C2"))
        else 0.0
    )


def cell_forecast_f12(ctx):
    return (
        1.0
        if xl_compare(">=", xl_cell(ctx, "Forecast!F4"), xl_cell(ctx, "Assumptions!C2"))
        else 0.0
    )
```

Key vocabulary:

```yaml
- dimension_id: REPORTING_PERIOD
  concept: REPORTING_PERIOD
  dtype: int
  suggested_param_name: reporting_period
```

Member metadata:

```yaml
- address: Forecast!B12
  function_name: cell_forecast_b12
  expected_keys:
    REPORTING_PERIOD: 1
  binding_keys:
    REPORTING_PERIOD: 1
  binding_record:
    INDICATOR: growth_threshold_met
    TABLE: Quarterly Forecast
- address: Forecast!C12
  function_name: cell_forecast_c12
  expected_keys:
    REPORTING_PERIOD: 2
  binding_keys:
    REPORTING_PERIOD: 2
  binding_record:
    INDICATOR: growth_threshold_met
    TABLE: Quarterly Forecast
- address: Forecast!D12
  function_name: cell_forecast_d12
  expected_keys:
    REPORTING_PERIOD: 3
  binding_keys:
    REPORTING_PERIOD: 3
  binding_record:
    INDICATOR: growth_threshold_met
    TABLE: Quarterly Forecast
- address: Forecast!E12
  function_name: cell_forecast_e12
  expected_keys:
    REPORTING_PERIOD: 4
  binding_keys:
    REPORTING_PERIOD: 4
  binding_record:
    INDICATOR: growth_threshold_met
    TABLE: Quarterly Forecast
- address: Forecast!F12
  function_name: cell_forecast_f12
  expected_keys:
    REPORTING_PERIOD: 5
  binding_keys:
    REPORTING_PERIOD: 5
  binding_record:
    INDICATOR: growth_threshold_met
    TABLE: Quarterly Forecast
```

Dependencies:

```python
def xl_cell(ctx: EvalContext, address: str) -> CellValue:
    """Read a single workbook cell by address."""
    # ...

def xl_compare(op: str, left: CellValue, right: CellValue) -> bool:
    """Compare two scalar cell values using an Excel comparison operator."""
    # ...
```
