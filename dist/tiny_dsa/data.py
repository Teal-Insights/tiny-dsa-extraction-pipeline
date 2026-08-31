from __future__ import annotations

from types import MappingProxyType

# --- Default inputs (leaf cells) ---
DEFAULT_INPUTS = {
    'Inputs': {
        (5, 2): 'Borvelia',
        (10, 2): 60,
        (11, 2): 80,
        (12, 2): 40,
        (16, 3): 3.5,
        (16, 4): 3.5,
        (16, 5): 3.5,
        (16, 6): 3.5,
        (16, 7): 3.5,
        (17, 3): 4,
        (17, 4): 4,
        (17, 5): 4,
        (17, 6): 4,
        (17, 7): 4,
        (18, 3): -1,
        (18, 4): -0.5,
        (18, 5): 0,
        (18, 6): 0.5,
        (18, 7): 1,
        (21, 2): 2,
        (22, 2): 1,
        (26, 2): -2,
        (26, 3): 2,
        (26, 4): -1,
    },
}

# --- Constant leaf values ---
CONSTANTS = MappingProxyType({
    'Engine': MappingProxyType({
        (5, 3): 1,
        (5, 4): 2,
        (5, 5): 3,
        (5, 6): 4,
        (5, 7): 5,
    }),
    'Inputs': MappingProxyType({
        (10, 1): 'Borvelia',
        (11, 1): 'Litellia',
        (12, 1): 'Aurelium',
    }),
})
