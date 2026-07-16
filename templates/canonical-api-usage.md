User-guide runnable cells should follow this interaction model: import from the generated package API module, create a context with `make_context()`, configure the scenario with `set_*` functions, then read results with `compute_*`. Single-cell setters accept a bare scalar; series setters accept a 1-D sequence of measure values, a tidy Polars DataFrame, or a list of records when the series is keyed.

Each `compute_*` call returns a list of records with fields such as `TIME_PERIOD`, `OBS_VALUE`, and any binding-defined key or context fields. Tabulate results with Polars: sort by the time or key dimension and `select` the measure column with a clear alias.

```{{python}}
import polars as pl

from {api_import_path} import (
    make_context,
    set_example_scalar,
    set_example_series,
    compute_example_output,
)


def results_frame(records: list[dict[str, object]]) -> pl.DataFrame:
    return (
        pl.DataFrame(records)
        .sort("TIME_PERIOD")
        .select(
            "TIME_PERIOD",
            pl.col("OBS_VALUE").alias("Value"),
        )
    )


ctx = make_context()
set_example_scalar(ctx, 1)
set_example_series(ctx, [1.0, 2.0, 3.0])
output = results_frame(compute_example_output(ctx=ctx))
```
