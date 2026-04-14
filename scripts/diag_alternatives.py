"""Quick diagnostic: show the score when Butter Milk is compared to communities in a platter."""
from core.connections import get_qdrant_client, neo4j_session
from openai import OpenAI
from core.settings import OPENAI_API_KEY, EMBEDDING_MODEL
from qdrant_client.models import FieldCondition, Filter, MatchAny

qdrant = get_qdrant_client()
client = OpenAI(api_key=OPENAI_API_KEY)

# Get the vector for Butter Milk
vector = client.embeddings.create(model=EMBEDDING_MODEL, input=["Butter Milk"]).data[0].embedding

# Get the community IDs that belong to a platter (e.g. North Indian Bowl which appeared in results)
with neo4j_session() as session:
    platter_communities = session.run("""
        MATCH (p:Platter {name: 'North Indian Bowl (Fixed)'})-[:HAS_COMMUNITY]->(c:Community)
        RETURN c.id
    """).data()

platter_community_ids = [r["c.id"] for r in platter_communities]
print(f"Platter has {len(platter_community_ids)} communities")

# Search within those communities — no score threshold (same as find_closest_in_platter)
results = qdrant.query_points(
    collection_name="item_search_communities",
    query=vector,
    limit=5,
    with_payload=True,
    query_filter=Filter(must=[FieldCondition(key="community_id", match=MatchAny(any=platter_community_ids))]),
).points

print("\nTop matches for 'Butter Milk' within platter communities (no score threshold):")
for hit in results:
    print(f"  Score: {hit.score:.4f}  Community: {hit.payload.get('name')}")
