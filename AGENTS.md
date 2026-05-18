## Python best practices

Always run Python code with `uv run`.

Use `fastpyxl` as a drop-in replacement for `openpyxl`.

Use static type annotations and direct attribute access. True defensive programming means enforcing that incorrect code fails fast and loudly.

## Test-driven development

Practice test-driven development (TDD). First write RED-phase tests and watch them fail for the right reason, then write code to turn the tests GREEN.

## Git branching

If you are asked to commit your work, make sure you commit it to an issue branch created with `gh issue develop`. Check that you are not already on such a branch before creating a new one.

## Opening issues to `Teal-Insights/excel-grapher`

We control the `Teal-Insights/excel-grapher` repository, so we can and should open issues there directly when we encounter bugs or need new features.

If a bug blocks our work, open an issue, mark it urgent, and stop working until the bug is fixed. If we can work around it, you should still open an issue so that we can track it and fix it in the future.

Remember to include at least a working minimal complete verifiable example (MCVE) of the bug in the issue body. The MCVE must be *self-contained* (must not depend on any local file artifacts or environment variables).