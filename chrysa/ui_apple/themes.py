"""The curated colour themes. Pure data: no Qt, so it can be imported anywhere."""

from __future__ import annotations

from typing import TypedDict


class Palette(TypedDict):
    """One theme. Every theme defines every key, so lookups never miss."""

    key: str
    display_name: str
    bg_base: str  # window and page background
    bg_surface: str  # sidebar, cards, panels, menus
    border: str  # dividers and panel outlines
    accent: str  # playhead, primary and active-playback buttons
    accent_alt: str  # hover, active list items, secondary meters
    text_primary: str  # titles and labels
    text_muted: str  # metadata: durations, counts, hints


THEMES: dict[str, Palette] = {
    # Insertion order is the cycle order.
    "neon_grid": {
        "key": "neon_grid",
        "display_name": "Neon Grid",
        "bg_base": "#0d0d13",
        "bg_surface": "#171723",
        "border": "#282a3e",
        "accent": "#ff0055",
        "accent_alt": "#00f0ff",
        "text_primary": "#f0f0f5",
        "text_muted": "#6b6e82",
    },
    "deep_cosmos": {
        "key": "deep_cosmos",
        "display_name": "Deep Cosmos",
        "bg_base": "#080914",
        "bg_surface": "#101328",
        "border": "#1c2142",
        "accent": "#9d4edd",
        "accent_alt": "#4cc9f0",
        "text_primary": "#e2e8f0",
        "text_muted": "#5a6488",
    },
    "solarized_amber": {
        "key": "solarized_amber",
        "display_name": "Solarized Amber",
        "bg_base": "#120e0a",
        "bg_surface": "#1c1712",
        "border": "#2e241c",
        "accent": "#ff9f1c",
        "accent_alt": "#ffe49e",
        "text_primary": "#fefae0",
        "text_muted": "#7a6b5d",
    },
    "boreal_forest": {
        "key": "boreal_forest",
        "display_name": "Boreal Forest",
        "bg_base": "#0b120f",
        "bg_surface": "#131e19",
        "border": "#1e2e26",
        "accent": "#10b981",
        "accent_alt": "#a7f3d0",
        "text_primary": "#ecfdf5",
        "text_muted": "#4d685c",
    },
}

DEFAULT_THEME = "deep_cosmos"
