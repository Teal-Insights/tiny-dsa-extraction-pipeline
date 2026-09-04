User-guide runnable cells should follow this interaction model: import keyword-only `compute_*` functions from the generated package API module and pass leaf inputs as Python scalars or 1-D sequences. There is no evaluation context: do not call `make_context()` or `set_*`.

Input shapes:

- Required arguments are keyword-only. Scalars stay bare values (for example `country_name="Borvelia"`), never a one-element list.
- Series arguments are 1-D sequences of measure values in the series' canonical key order. Pass exactly one value per key; omit optional arguments that already have defaults in the generated `data` module.
- Each `compute_*` returns a tuple of floats (one value per horizon year), not records.

Tabulate results with Polars: wrap the tuple and `select` the measure column with a clear alias.

```{{python}}
import polars as pl

from {api_import_path} import compute_example_output


output = compute_example_output(
    country_name="Borvelia",
    country_initial_debt=[60.0, 80.0, 55.0],
    growth_baseline=[0.02, 0.02, 0.02, 0.02, 0.02],
    interest_baseline=[0.03, 0.03, 0.03, 0.03, 0.03],
    primary_balance_baseline=[0.01, 0.01, 0.01, 0.01, 0.01],
)
frame = (
    pl.DataFrame({{"year": list(range(1, len(output) + 1)), "value": list(output)}})
    .select("year", pl.col("value").alias("Debt-to-GDP"))
)
```
