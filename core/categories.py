"""Shared category normalization and category-family helpers for SearchPOC."""

from __future__ import annotations

from collections import Counter


CATEGORY_FAMILY_MAP: dict[str, str] = {
    "Accompaniment": "Accompaniment",
    "Curry": "Curry",
    "Curries": "Curry",
    "Gravy": "Curry",
    "Mains": "Curry",
    "Dal": "Dal",
    "Liquid": "Dal",
    "Liquids": "Dal",
    "Flavoured Rice/Dal/Liquid/Fry/Chutney": "Rice",
    "Biryani": "Biryani",
    "Biryani & Pulav": "Biryani",
    "Biryani / Pulav": "Biryani",
    "Biryani/Curry": "Biryani",
    "Pulao": "Rice",
    "Flavoured Rice": "Rice",
    "Flavored Rice": "Rice",
    "Special Rice": "Rice",
    "Special Rice / Bread": "Rice",
    "Special Rice / Noodles": "Rice",
    "Special Rice / Noodles / Bread": "Rice",
    "Fried Rice/Noodles": "Rice",
    "Flavoured Rice/Bread": "Rice",
    "Bread": "Bread",
    "Bread/Side": "Bread",
    "Starter": "Starter",
    "Starters": "Starter",
    "Premium Starters": "Starter",
    "Hot/Starter": "Starter",
    "Cocktail Sides": "Starter",
    "BBQ Skewers": "Starter",
    "Appetizers": "Starter",
    "Grilled": "Starter",
    "Live Station": "Starter",
    "Snack": "Snack",
    "Savories": "Snack",
    "Savory": "Snack",
    "Hot": "Snack",
    "Fried Snacks": "Snack",
    "Baked / Fried Snacks": "Snack",
    "Baked / Grilled Snacks": "Snack",
    "Potatos / Fried Snacks": "Snack",
    "Pasta": "Pasta",
    "Fry": "Fry",
    "Side": "Side",
    "Sides": "Side",
    "Side/Desserts": "Side",
    "Sides & Beverages": "Side",
    "Standards": "Side",
    "Accompaniments": "Accompaniment",
    "Chutney": "Accompaniment",
    "Dips": "Accompaniment",
    "Fresh Chutney & Pickles": "Accompaniment",
    "Fresh Grinded Chutney": "Accompaniment",
    "Fresh Grind Chutney(pachadi)": "Accompaniment",
    "Fresh Pickels": "Accompaniment",
    "Dessert": "Dessert",
    "Desserts": "Dessert",
    "Desserts / Pan": "Dessert",
    "Sweet": "Dessert",
    "Sweet/Desserts": "Dessert",
    "Sweet/Fruit": "Dessert",
    "Sweets": "Dessert",
    "Sweets & Fruits": "Dessert",
    "Beverages / Sweets / Fruits": "Dessert",
    "Traditional Sweet": "Dessert",
    "Paan": "Dessert",
    "Ice Creams": "Dessert",
    "Prasadam": "Dessert",
    "Fruit": "Fruit",
    "Fruits": "Fruit",
    "Fruit/Sweet/Sides": "Fruit",
    "Salads / Fruits": "Fruit",
    "Salad": "Salad",
    "Soup": "Soup",
    "Soups": "Soup",
    "Beverage": "Beverage",
    "Beverages": "Beverage",
    "Hot & Cold Beverages": "Beverage",
    "Welcome Drink": "Beverage",
    "Welcome Drink/Beverage": "Beverage",
    "Refreshments": "Beverage",
    "Fresh Baked": "Bread",
    "Additional Curry": "Curry",
}

NORMALIZED_CATEGORIES: set[str] = {
    "Curry",
    "Dal",
    "Rice",
    "Biryani",
    "Bread",
    "Starter",
    "Snack",
    "Fry",
    "Side",
    "Accompaniment",
    "Dessert",
    "Fruit",
    "Salad",
    "Soup",
    "Beverage",
    "Pasta",
}

CATEGORY_MAP = CATEGORY_FAMILY_MAP


def category_family(raw: str | None) -> str:
    """Map a category name to a strict broad family, or empty string if unknown."""
    if raw is None:
        return ""
    clean = raw.strip()
    if not clean:
        return ""
    if clean in NORMALIZED_CATEGORIES:
        return clean
    return CATEGORY_FAMILY_MAP.get(clean, "")


def normalize_category(raw: str | None) -> str:
    """Map a raw category name to the shared normalized taxonomy for storage."""
    family = category_family(raw)
    if family:
        return family
    if raw is None:
        return ""
    return raw.strip()


def is_known_category(raw: str | None) -> bool:
    """Return whether a category is explicitly mapped or already canonical."""
    if raw is None:
        return False
    clean = raw.strip()
    return clean in CATEGORY_FAMILY_MAP or clean in NORMALIZED_CATEGORIES


def build_category_counts(categories: list[str | None]) -> dict[str, int]:
    """Count normalized categories, ignoring blanks."""
    counts = Counter(normalize_category(category) for category in categories)
    counts.pop("", None)
    return dict(sorted(counts.items()))


def build_category_family_counts(categories: list[str | None]) -> dict[str, int]:
    """Count strict broad category families, ignoring blanks and unknowns."""
    counts = Counter(category_family(category) for category in categories)
    counts.pop("", None)
    return dict(sorted(counts.items()))
