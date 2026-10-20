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
    scheme: str  # "dark" | "light": which way tints and shadows are mixed
    flat: bool  # True = plain page background, no painted backdrop scene


THEMES: dict[str, Palette] = {
    # Insertion order is the cycle order. The two Apple themes are flat and neutral;
    # the four below them keep their colours (and painted scenes) from the original design.
    "apple_dark": {
        "key": "apple_dark",
        "display_name": "Dark",
        "bg_base": "#1c1c1e",
        "bg_surface": "#2c2c2e",
        "border": "#3a3a3c",
        "accent": "#0b78f0",
        "accent_alt": "#98989f",
        "text_primary": "#f5f5f7",
        "text_muted": "#98989f",
        "scheme": "dark",
        "flat": True,
    },
    "apple_light": {
        "key": "apple_light",
        "display_name": "Light",
        "bg_base": "#f5f5f7",
        "bg_surface": "#ffffff",
        "border": "#d2d2d7",
        "accent": "#0071e3",
        "accent_alt": "#6e6e73",
        "text_primary": "#1d1d1f",
        "text_muted": "#6e6e73",
        "scheme": "light",
        "flat": True,
    },
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
        "scheme": "dark",
        "flat": False,
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
        "scheme": "dark",
        "flat": False,
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
        "scheme": "dark",
        "flat": False,
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
        "scheme": "dark",
        "flat": False,
    },
}

DEFAULT_THEME = "apple_dark"
