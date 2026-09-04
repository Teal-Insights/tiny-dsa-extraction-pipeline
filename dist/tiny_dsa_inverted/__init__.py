"""Inverted-tree mechanical extraction of Tiny DSA.

This package is a hand-rolled prototype of what ``dist/tiny_dsa`` should look
like once codegen inverts the Excel graph:

- Public ``compute_*`` functions take the leaf closure of their subgraph
  (required mutable inputs, defaulted constants) and return root-cell values.
- Input setters and the evaluation context go away; inputs are argument arrays.
- Internals take first-level dependencies only and can be trimmed so unused
  years do not expand the required argument set.
"""

from __future__ import annotations

from .api import (
    OutputRecord,
    as_records,
    compute_output_baseline,
    compute_output_delta,
    compute_output_shocked,
)

__all__ = [
    "OutputRecord",
    "as_records",
    "compute_output_baseline",
    "compute_output_delta",
    "compute_output_shocked",
]
