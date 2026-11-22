"""Read-only macroeconomic data bridge to ../public-data-gateway with offline fallback."""

from finops_lakehouse.gateway_bridge.macro_rates import (
    DEFAULT_GATEWAY_DIR,
    DEFAULT_REFERENCE_PATH,
    MacroRatesSnapshot,
    load_macro_rates,
    macro_snapshot_to_dict,
)

__all__ = [
    "DEFAULT_GATEWAY_DIR",
    "DEFAULT_REFERENCE_PATH",
    "MacroRatesSnapshot",
    "load_macro_rates",
    "macro_snapshot_to_dict",
]
