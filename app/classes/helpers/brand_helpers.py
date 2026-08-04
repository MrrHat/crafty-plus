"""Pure helpers for resolving custom-logo settings into static asset paths.

Kept free of Tornado/DB imports so it can be unit-tested in isolation.
"""

STOCK_FULL = "/static/assets/images/logo_long.svg"
STOCK_SQUARE = "/static/assets/images/crafty-logo-square.svg"
LOGO_FULL_DIR = "/static/assets/images/logos/full"
LOGO_SQUARE_DIR = "/static/assets/images/logos/square"


def resolve_brand_paths(settings):
    """Resolve raw brand settings into render-ready static paths.

    Args:
        settings: dict from ManagementController.get_brand_settings() with keys
            'full' and 'square' (each a filename, or "" for the stock logo).

    Returns:
        dict with keys 'full' and 'square', each an absolute /static path.
        Empty/missing filenames fall back to the stock Crafty logos.
    """
    full = settings.get("full") or ""
    square = settings.get("square") or ""
    return {
        "full": f"{LOGO_FULL_DIR}/{full}" if full else STOCK_FULL,
        "square": f"{LOGO_SQUARE_DIR}/{square}" if square else STOCK_SQUARE,
    }
