"""Regenerate catalogue_menu_mapping.csv using fresh RDS export and CatalogueData."""

import csv
import uuid

CAT_BEVERAGES   = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
CAT_DESSERTS    = "a7b8c9d0-e1f2-3456-abcd-567890123456"
CAT_STARTERS    = "b2c3d4e5-f6a7-8901-bcde-f12345678901"
CAT_SNACKS      = "b8c9d0e1-f2a3-4567-bcde-678901234567"
CAT_RICE        = "c3d4e5f6-a7b8-9012-cdef-123456789012"
CAT_OTHERS      = "c9d0e1f2-a3b4-5678-cdef-789012345678"
CAT_BREADS      = "d4e5f6a7-b8c9-0123-defa-234567890123"
CAT_MAIN        = "e5f6a7b8-c9d0-1234-efab-345678901234"
CAT_SIDES       = "f6a7b8c9-d0e1-2345-fabc-456789012345"

# Manual overrides for specific names (lowercase)
NAME_OVERRIDES: dict[str, str] = {
    "blue lagoon": CAT_BEVERAGES,
    "bubble gum": CAT_BEVERAGES,
    "butterscotch": CAT_BEVERAGES,
    "coffee": CAT_BEVERAGES,
    "filter coffee": CAT_BEVERAGES,
    "ginger tea": CAT_BEVERAGES,
    "lemon tea": CAT_BEVERAGES,
    "pineapple drink": CAT_BEVERAGES,
    "plain coffee": CAT_BEVERAGES,
    "plain tea": CAT_BEVERAGES,
    "rainbow": CAT_BEVERAGES,
    "tea": CAT_BEVERAGES,
    "tetrapacked juice": CAT_BEVERAGES,
    "virgin mojito": CAT_BEVERAGES,
    "halwa": CAT_DESSERTS,
    "jalebi": CAT_DESSERTS,
    "jelebi with rabdi": CAT_DESSERTS,
    "marshmallows": CAT_DESSERTS,
    "brownies": CAT_DESSERTS,
    "chocolate dip": CAT_DESSERTS,
    "nethi bobatlu": CAT_DESSERTS,
    "blueberry": CAT_DESSERTS,
    "kiwi": CAT_OTHERS,
    "mango": CAT_OTHERS,
    "orange": CAT_OTHERS,
    "pineapple": CAT_OTHERS,
    "dragon fruit": CAT_OTHERS,
    "fruits": CAT_OTHERS,
    "seasonal cut fruits": CAT_OTHERS,
    "seasonal fruits": CAT_OTHERS,
    "strawberry": CAT_OTHERS,
    "vanilla": CAT_DESSERTS,
    "chocolate": CAT_DESSERTS,
    "garlic naan": CAT_BREADS,
    "kulcha": CAT_BREADS,
    "plain naan": CAT_BREADS,
    "pudina naan": CAT_BREADS,
    "poori": CAT_BREADS,
    "aloo chaat": CAT_SNACKS,
    "aloo chips": CAT_SNACKS,
    "aloo samosa - 1pc(big)": CAT_SNACKS,
    "aloo samosa(big)": CAT_SNACKS,
    "bhel puri": CAT_SNACKS,
    "biscuits": CAT_SNACKS,
    "boiled peanuts": CAT_SNACKS,
    "boiled peanuts fry": CAT_SNACKS,
    "cookie - 2pc": CAT_SNACKS,
    "crispers": CAT_SNACKS,
    "cup cake": CAT_SNACKS,
    "dahi puri": CAT_SNACKS,
    "dry fruits garnish": CAT_SNACKS,
    "honey almond": CAT_SNACKS,
    "pani puri": CAT_SNACKS,
    "papdi chaat": CAT_SNACKS,
    "roasted peanuts": CAT_SNACKS,
    "sev puri": CAT_SNACKS,
    "veg slider": CAT_SNACKS,
    "chicken burger": CAT_SNACKS,
    "chicken patty burger": CAT_SNACKS,
    "chicken mayo wrap": CAT_SNACKS,
    "veg shawarma": CAT_SNACKS,
    "chicken shawarma": CAT_SNACKS,
    "egg & shreded chicken sandwich": CAT_SNACKS,
    "chicken soft noodles": CAT_SNACKS,
    "veg soft noodles": CAT_SNACKS,
    "veg alfredo pasta": CAT_SNACKS,
    "chicken alfredo pasta": CAT_SNACKS,
    "white suace pasta": CAT_SNACKS,
    "jackfruit biryani": CAT_RICE,
    "chicken pulao": CAT_RICE,
    "chicken fried rice": CAT_RICE,
    "veg pulao": CAT_RICE,
    "veg manchuria": CAT_RICE,  # ambiguous — treating as main
    "pappucharu annam": CAT_RICE,
    "pulihora": CAT_RICE,
    "veg manchuria": CAT_MAIN,
    "veg manchurian gravy": CAT_MAIN,
    "chicken manchurian gravy": CAT_MAIN,
    "butter chicken masala": CAT_MAIN,
    "chicken curry": CAT_MAIN,
    "dum aloo curry": CAT_MAIN,
    "mirchi ka saalan": CAT_MAIN,
    "palak dal": CAT_MAIN,
    "plain curd": CAT_MAIN,
    "sweet corn soup": CAT_MAIN,
    "chicken soup": CAT_MAIN,
    "bhendi peanut fry": CAT_MAIN,
    "gongura boti fry": CAT_STARTERS,
    "peanut masala fry": CAT_STARTERS,
    "chilly apollo fish": CAT_STARTERS,
    "chilly chicken lollipop": CAT_STARTERS,
    "chilly garlic mushroom": CAT_STARTERS,
    "chilly prawns": CAT_STARTERS,
    "chicken lollipop": CAT_STARTERS,
    "assorted all meat skewers": CAT_STARTERS,
    "assorted chicken skewers": CAT_STARTERS,
    "assorted veggie skewers": CAT_STARTERS,
    "honey chilli pineapple skewers": CAT_STARTERS,
    "malai chicken skewers": CAT_STARTERS,
    "malai mushroom skewers": CAT_STARTERS,
    "malai paneer tikka skewers": CAT_STARTERS,
    "malai soya chaap skewers": CAT_STARTERS,
    "pachi mirchi chicken skewers": CAT_STARTERS,
    "pachimirchi fish skewers": CAT_STARTERS,
    "pachimirchi mushroom skewers": CAT_STARTERS,
    "pachimirchi prawns skewers": CAT_STARTERS,
    "tandoor chicken skewers": CAT_STARTERS,
    "tandoori fish skewers": CAT_STARTERS,
    "tandoori mushroom skewers": CAT_STARTERS,
    "tandoori paneer skewers": CAT_STARTERS,
    "tandoori pineapple skewers": CAT_STARTERS,
    "tandoori prawns skewers": CAT_STARTERS,
    "tandoori soya chaap skewers": CAT_STARTERS,
    "watermelon skewers": CAT_STARTERS,
    "butter": CAT_SIDES,
    "cheese": CAT_SIDES,
    "cut mirchi": CAT_SIDES,
    "ghee karam": CAT_SIDES,
    "masala": CAT_SIDES,
    "mouth freshener paan": CAT_SIDES,
    "onion": CAT_SIDES,
    "tomato ketchup": CAT_SIDES,
    "fire paan": CAT_SIDES,
    "sweet paan": CAT_SIDES,
    "chocolate paan": CAT_SIDES,
    "strawberry paan": CAT_SIDES,
    "plain": CAT_SIDES,
    "paneer ": CAT_STARTERS,
}


def infer_category(name: str) -> str:
    """Keyword-based fallback for names not in NAME_OVERRIDES."""
    n = name.lower()
    if any(w in n for w in ["tea", "coffee", "juice", "drink", "mojito", "shake", "milk", "lagoon"]):
        return CAT_BEVERAGES
    if any(w in n for w in ["skewer", "tikka", "kebab", "lollipop", "pakoda", "manchurian", "65", "fry", "vepudu"]):
        return CAT_STARTERS
    if any(w in n for w in ["biryani", "rice", "pulao", "pulihora", "annam"]):
        return CAT_RICE
    if any(w in n for w in ["naan", "roti", "kulcha", "paratha", "poori", "pulka"]):
        return CAT_BREADS
    if any(w in n for w in ["halwa", "kheer", "payasam", "paan", "jamun", "meetha", "dessert", "cake", "brownie", "biscuit", "cookie"]):
        return CAT_DESSERTS
    if any(w in n for w in ["sandwich", "burger", "wrap", "pasta", "noodles", "shawarma", "chaat", "puri", "samosa", "slider"]):
        return CAT_SNACKS
    if any(w in n for w in ["curry", "masala", "dal", "pappu", "sambar", "rasam", "gravy", "soup", "curd", "kadhi"]):
        return CAT_MAIN
    if any(w in n for w in ["chutney", "pachadi", "raitha", "pickle", "salad", "papad", "ketchup", "butter", "onion", "ghee karam"]):
        return CAT_SIDES
    if any(w in n for w in ["fruit", "melon", "mango", "kiwi", "orange", "pineapple", "strawberry", "banana", "papaya"]):
        return CAT_OTHERS
    return CAT_OTHERS

RDS_EXPORT = "menu_items_export (2).csv"
CATALOGUE_DATA = "CatalogueData - results (4).csv"
OUTPUT = "rds_dump/catalogue_menu_mapping.csv"


def main() -> None:
    # Build index of RDS items by lowercase name
    rds_index: dict[str, dict] = {}
    with open(RDS_EXPORT, newline="", encoding="utf-8") as f:
        for item in csv.DictReader(f):
            rds_index[item["name"].strip().lower()] = item

    rows = []
    with open(CATALOGUE_DATA, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["itemActive"].strip().upper() != "ACTIVE":
                continue
            name_key = row["itemName"].strip().lower()
            match = rds_index.get(name_key)
            item_name = row["itemName"].strip()
            cat_id = match["category_id"].strip() if match else ""
            if not cat_id:
                cat_id = NAME_OVERRIDES.get(item_name.lower()) or infer_category(item_name)
            rows.append({
                "catalogueIdPK": str(uuid.uuid4()),
                "catalogueItemId": match["id"].strip() if match else "",
                "itemName": item_name,
                "menuItemId": row["itemId"].strip(),
                "status": row["itemActive"].strip(),
                "catalogueCategoryID": cat_id,
            })

    with open(OUTPUT, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["catalogueIdPK", "catalogueItemId", "itemName", "menuItemId", "status", "catalogueCategoryID"])
        writer.writeheader()
        writer.writerows(rows)

    matched = sum(1 for r in rows if r["menuItemId"])
    print(f"Total: {len(rows)} rows | Matched: {matched} | Unmatched: {len(rows) - matched}")


if __name__ == "__main__":
    main()
