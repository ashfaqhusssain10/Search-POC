import logging
from scripts.search import search_platters
from core.connections import close_connections

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

TEST_QUERIES = [
    "Dal Makhani",                             # Direct match
    "Makhani Dal",                             # Alias match
    "Prawn Pakoda",                            # Bridge match (should pull Chicken Pakoda communities)
    "Chicken Pakoda, Garlic Naan",             # Multi-item match
    "Paneer Butter Masala, Butter Chicken",    # Cross-veg/non-veg (should stay separate)
]

def run_verification():
    print("=== Search Verification ===\n")
    for query in TEST_QUERIES:
        print(f"Query: {query!r}")
        results = search_platters(query)
        if not results:
            print("  No matching platters found.\n")
            continue
        
        for i, r in enumerate(results[:2], 1):
            coverage = f"{r.matched_communities}/{r.query_community_count}"
            print(f"  #{i} {r.name} (Coverage: {coverage})")
            for item, comm in r.item_to_community.items():
                status = "✓" if comm in r.matched_community_names else "~"
                print(f"    {status} {item} -> {comm}")
        print()

if __name__ == "__main__":
    try:
        run_verification()
    finally:
        close_connections()
