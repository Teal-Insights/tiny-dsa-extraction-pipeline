User-guide runnable cells should follow this interaction model: import `compute_*` functions from the generated package API module and pass a typed `{Output}Inputs` bundle. Build the bundle with `{Output}Inputs.from_defaults(...)`, which fills `data.*_DEFAULT` and accepts leaf overrides. There is no evaluation context: do not call `make_context()` or `set_*`, and do not pass leaf kwargs directly into `compute_*`.

Input shapes:

- `compute_*` takes one Inputs dataclass, not keyword-only leaf arguments. Construct it with `from_defaults` (for example `country_name="Borvelia"`), never a one-element list for a scalar.
- Series arguments are named-axis tensors or 1-D sequences of measure values in the series' canonical key order. Pass exactly one value per key; omit optional arguments that already have defaults in the generated `data` module.
- Each `compute_*` takes one Inputs argument and returns a scalar or Tensor (one value per horizon year), not records.

Tabulate results with Polars: wrap the tuple and `select` the measure column with a clear alias.

```{{python}}
import polars as pl

from {api_import_path} import compute_example_output
from {api_import_path}.model import ExampleOutputInputs


output = compute_example_output(
    ExampleOutputInputs.from_defaults(
        country_name="Borvelia",
        country_initial_debt=[60.0, 80.0, 55.0],
        growth_baseline=[0.02, 0.02, 0.02, 0.02, 0.02],
        interest_baseline=[0.03, 0.03, 0.03, 0.03, 0.03],
        primary_balance_baseline=[0.01, 0.01, 0.01, 0.01, 0.01],
    )
)
frame = (
    pl.DataFrame({{"year": list(range(1, len(output) + 1)), "value": list(output)}})
    .select("year", pl.col("value").alias("Debt-to-GDP"))
)
```
