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

# Maps Supabase typecode_name → canonical category family
TYPECODE_FAMILY_MAP: dict[str, str] = {
    "Curry": "Curry",
    "Dal": "Dal",
    "Biryani": "Biryani",
    "Pulav": "Rice",
    "FriedRice": "Rice",
    "Rice": "Rice",
    "Flavoured Rice": "Rice",
    "Flatbread": "Bread",
    "Bread": "Bread",
    "Stuffedbread": "Bread",
    "Friedbread": "Bread",
    "HotFry": "Starter",
    "SkewerGrill": "Starter",
    "PanFry": "Starter",
    "Grill": "Starter",
    "DryFry": "Starter",
    "Manchurian": "Starter",
    "ColdBite": "Starter",
    "MiniWrap": "Starter",
    "Snack": "Snack",
    "StuffedDough": "Snack",
    "Namkeen": "Snack",
    "Chips": "Snack",
    "Pasta": "Pasta",
    "Noodle": "Pasta",
    "Pizza": "Snack",
    "Sandwich": "Snack",
    "Chutney": "Accompaniment",
    "Dip": "Accompaniment",
    "Pickle": "Accompaniment",
    "Raita": "Accompaniment",
    "Spread": "Accompaniment",
    "Sauce": "Accompaniment",
    "Powder": "Accompaniment",
    "Garnish": "Accompaniment",
    "Crisp": "Accompaniment",
    "Side": "Side",
    "Curd": "Side",
    "Dairy": "Side",
    "Cake": "Dessert",
    "Pastry": "Dessert",
    "Cookie": "Dessert",
    "Frozen": "Dessert",
    "PuddingMithai": "Dessert",
    "FriedMithai": "Dessert",
    "LadduMithai": "Dessert",
    "SteamedMithai": "Dessert",
    "BreadMithai": "Dessert",
    "ColostrumMithai": "Dessert",
    "SweetGrainBowl": "Dessert",
    "Traditional": "Dessert",
    "Pudding": "Dessert",
    "Custard": "Dessert",
    "Casserole": "Dessert",
    "Shake": "Dessert",
    "Paan": "Dessert",
    "Rice": "Dessert",  # Rice in Desserts context (e.g. rice pudding)
    "LeafySalad": "Salad",
    "FruitSalad": "Fruit",
    "LegumeSalad": "Salad",
    "HeartySoup": "Soup",
    "ClearSoup": "Soup",
    "ClearBroth": "Soup",
    "CreamySoup": "Soup",
    "Soup": "Soup",
    "Juice": "Beverage",
    "MilkDrink": "Beverage",
    "HotDrink": "Beverage",
    "ColdDrink": "Beverage",
    "Beverage": "Beverage",
    "Alcoholic": "Beverage",
    "Tonic": "Beverage",
    "Refresher": "Beverage",
    "Syrup": "Beverage",
    "DryChaat": "Snack",
    "FusionChaat": "Snack",
    "CurdChaat": "Snack",
    "EggPlate": "Starter",
    "GrainBowl": "Rice",
    "SavoryBakery": "Snack",
    "Steamed": "Starter",
    "SweetGriddle": "Dessert",
    "Handheld": "Snack",
}


def typecode_family(typecode: str | None) -> str:
    """Map a Supabase typecode_name to a canonical category family."""
    if not typecode:
        return ""
    return TYPECODE_FAMILY_MAP.get(typecode.strip(), "")


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
