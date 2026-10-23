"""Open NHL contracts dataset: collectors, normalizer, publisher, and a thin loader.

    import nhl_contracts
    rows = nhl_contracts.load("contracts")
"""

from .load import TABLES, download, load, timestamp

__version__ = "0.1.0"
__all__ = ["TABLES", "download", "load", "timestamp", "__version__"]
