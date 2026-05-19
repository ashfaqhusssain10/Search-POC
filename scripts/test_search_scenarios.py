"""20+ smoke-test scenarios for search_v4.

Each scenario picks a Supabase master-list item and asserts a soft expectation
about what should rank in the top hits returned from the canonical collection.

Two kinds of expectations:
  - expect_form    : the form/sub_category we expect for top-1
                     (catches obvious cross-form bleed, e.g. bread → curry)
  - expect_in_top3 : a substring that should appear in at least one of top 3 hits
                     (catches obvious semantic misses, e.g. biryani query → no biryani)

Pass = both checks satisfied OR the check is None.
Soft = top-1 score below 0.5 is flagged separately (low confidence, not fail).

Usage:
    python -m scripts.test_search_scenarios
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from scripts.search_v4 import ItemQueryResult, search_items_v4

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")


@dataclass
class Scenario:
    query: str
    expect_form: str | None = None        # e.g. "flatbread", "gravy", "rice dish"
    expect_in_top3: str | None = None     # case-insensitive substring
    note: str = ""


SCENARIOS: list[Scenario] = [
    # Curries & gravies
    Scenario("Paneer Butter Masala Curry", expect_form="gravy", expect_in_top3="paneer", note="creamy paneer curry"),
    Scenario("Dal Tadka", expect_form="gravy", expect_in_top3="dal", note="should surface other dals"),
    Scenario("Rajma Masala", expect_form="gravy", expect_in_top3="rajma", note="kidney bean curry"),
    Scenario("Mushroom Curry", expect_form="gravy", expect_in_top3="mushroom", note=""),

    # Breads
    Scenario("Ghee Chapathi", expect_form="flatbread", expect_in_top3="chapati", note="flatbread cluster"),
    Scenario("Stuffed Kulcha", expect_form="flatbread", expect_in_top3="kulcha", note=""),
    Scenario("Millet Poori", expect_form="flatbread", note="puffed/fried bread"),

    # Rice dishes & biryanis
    Scenario("Jackfruit Biryani", expect_form="rice dish", expect_in_top3="biryani", note="veg biryani"),
    Scenario("Chicken Pulav", expect_form="rice dish", expect_in_top3="pulao", note="non-veg pulao"),
    Scenario("Bagara Rice", expect_form="rice dish", note="south indian whole-spice rice"),
    Scenario("Tadka Rice", expect_form="rice dish", note="simple flavored rice"),

    # Non-veg main dishes
    Scenario("Mutton Roast", expect_in_top3="mutton", note="non-veg roast"),
    Scenario("Telangana Chicken Dry", expect_in_top3="chicken", note="dry chicken starter/main"),
    Scenario("Sweet and Sour Chicken", expect_in_top3="chicken", note="Indo-Chinese"),

    # Desserts & sweets
    Scenario("Double Ka Meetha", expect_form="sweet", note="bread pudding dessert"),
    Scenario("Walnut Brownie", expect_form="sweet", note="western dessert"),
    Scenario("Honey Dew Ice Cream", expect_form="sweet", expect_in_top3="ice cream", note=""),

    # Soups & snacks
    Scenario("Manchow Soup", expect_form="soup", expect_in_top3="soup", note=""),
    Scenario("Papdi Chaat", expect_in_top3="chaat", note="chaat snack"),
    Scenario("Mini Masala Kachori", expect_in_top3="kachori", note=""),

    # Drinks & sides
    Scenario("Mojito", note="beverage — softly check it doesn't bleed to food"),
    Scenario("Coriander Chutney", expect_in_top3="chutney", note="condiment"),

    # Egg
    Scenario("Egg Puff", expect_in_top3="egg", note="egg snack"),
]


def evaluate(result: ItemQueryResult, scenario: Scenario) -> tuple[str, list[str]]:
    """Return (status, notes). status ∈ {'PASS','FAIL','EMPTY','SOFT'}."""
    notes: list[str] = []
    if not result.hits:
        return "EMPTY", ["no hits returned"]

    top = result.hits[0]
    top3 = result.hits[:3]

    if scenario.expect_form is not None:
        actual_form = (top.form or "").lower()
        want = scenario.expect_form.lower()
        if want not in actual_form and actual_form not in want:
            notes.append(f"top-1 form='{top.form}' ≠ expected '{scenario.expect_form}'")

    if scenario.expect_in_top3 is not None:
        needle = scenario.expect_in_top3.lower()
        if not any(needle in (h.name or "").lower() for h in top3):
            notes.append(f"'{scenario.expect_in_top3}' not in top-3 names")

    if notes:
        return "FAIL", notes

    if top.score < 0.5:
        return "SOFT", [f"top-1 score {top.score:.3f} < 0.5"]
    return "PASS", []


def main() -> None:
    queries = [s.query for s in SCENARIOS]
    print(f"Running {len(queries)} scenarios...\n")
    results = search_items_v4(queries, top_k=5)
    result_by_query = {r.query_item: r for r in results}

    pass_n = fail_n = empty_n = soft_n = 0
    rows = []

    for s in SCENARIOS:
        r = result_by_query.get(s.query)
        if r is None:
            status, notes = "EMPTY", ["query missing from results"]
        else:
            status, notes = evaluate(r, s)

        if status == "PASS":
            pass_n += 1
        elif status == "FAIL":
            fail_n += 1
        elif status == "EMPTY":
            empty_n += 1
        else:
            soft_n += 1

        top_str = "—"
        if r and r.hits:
            top_str = f"#{1} {r.hits[0].name} ({r.hits[0].score:.3f}, {r.hits[0].form})"
        rows.append((status, s.query, top_str, "; ".join(notes), s.note))

    width_q = max(len(r[1]) for r in rows)
    width_t = max(len(r[2]) for r in rows)
    print(f"{'STATUS':<7} {'QUERY':<{width_q}}  {'TOP-1':<{width_t}}  NOTES")
    print("-" * (10 + width_q + width_t + 30))
    for status, q, top, notes, comment in rows:
        marker = {"PASS": "✓", "FAIL": "✗", "EMPTY": "○", "SOFT": "~"}[status]
        line = f"{marker} {status:<5} {q:<{width_q}}  {top:<{width_t}}  {notes}"
        if comment:
            line += f"   [{comment}]"
        print(line)

    print()
    print(f"Summary: {pass_n} PASS · {fail_n} FAIL · {soft_n} SOFT · {empty_n} EMPTY  (of {len(SCENARIOS)})")


if __name__ == "__main__":
    main()
