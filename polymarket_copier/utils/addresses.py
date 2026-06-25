"""Ethereum address normalization utilities."""

from __future__ import annotations


def normalize_address(address: str) -> str:
    """Normalize an Ethereum address to lowercase hex.

    Polymarket's APIs return wallet addresses in inconsistent casing
    (leaderboard uses lowercase, some activity endpoints use checksummed
    mixed-case). Normalizing to lowercase at every ingestion boundary
    prevents dict-lookup misses when comparing addresses from different
    sources (e.g. leaderboard vs. trade event).

    Returns an empty string unchanged so callers can still use falsy checks.
    """
    return address.lower() if address else address
