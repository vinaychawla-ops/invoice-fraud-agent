"""Supplier-invoice fraud detection agent built on Google ADK."""

__version__ = "0.1.0"

from .pipeline import run_pipeline  # noqa: F401

__all__ = ["__version__", "run_pipeline"]
