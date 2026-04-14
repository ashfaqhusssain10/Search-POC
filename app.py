"""Streamlit UI for Item-Based Platter Search POC."""

import streamlit as st

from core.connections import neo4j_session
from scripts.search import PlatterResult, search_platters

st.set_page_config(page_title="Platter Search", page_icon="🍽️", layout="centered")


# ---------------------------------------------------------------------------
# Cached data
# ---------------------------------------------------------------------------

@st.cache_data(show_spinner="Loading dish list...")
def load_canonical_items() -> list[str]:
    """Fetch all DynamoDB canonical item names from Neo4j, sorted."""
    with neo4j_session() as session:
        result = session.run(
            "MATCH (i:Item {source: 'dynamodb'}) RETURN i.name AS name ORDER BY i.name"
        )
        return [r["name"] for r in result]


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

st.title("Platter Search")
st.caption("Select dishes to find the best-matching platters.")

canonical_items = load_canonical_items()

selected = st.multiselect(
    label="Dishes",
    options=canonical_items,
    placeholder="Type to search and select dishes...",
)

search_clicked = st.button("Search", type="primary", disabled=not selected)

# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

if search_clicked and selected:
    query = ", ".join(selected)

    with st.spinner("Searching..."):
        results: list[PlatterResult] = search_platters(query)

    if not results:
        st.warning("No matching platters found. Try different dishes.")
    else:
        st.subheader(f"Top {len(results)} platters")

        for i, r in enumerate(results, 1):
            veg_label = "🟢 VEG" if r.veg else "🔴 NON-VEG"
            coverage_label = f"{r.matched_communities}/{r.query_community_count} dishes matched"

            with st.expander(
                f"#{i}  **{r.name}**  —  {coverage_label}  |  {veg_label}",
                expanded=(i == 1),
            ):
                # Metrics row
                col1, col2, col3 = st.columns(3)
                col1.metric("Dishes matched", f"{r.matched_communities} / {r.query_community_count}")
                col2.metric("Coverage", f"{r.coverage_ratio:.0%}")

                if r.min_price and r.max_price:
                    col3.metric("Price range", f"₹{int(r.min_price)} – ₹{int(r.max_price)}")
                else:
                    col3.metric("Type", r.platter_type)

                st.progress(r.coverage_ratio)

                # Per-item match status
                st.markdown("**Your dishes:**")
                for item, comm_name in r.item_to_community.items():
                    cid = r.item_to_community_id.get(item)
                    summary = r.community_summaries.get(cid) if cid else None

                    if comm_name and comm_name in r.matched_community_names:
                        st.write(f"✅ **{item}** → matched as *{comm_name}*")
                        if summary:
                            narrative = summary.get("narrative")
                            if narrative:
                                st.caption(narrative)
                            variants = summary.get("variant_names", [])
                            if variants:
                                st.markdown(f"<small>Also known as: {', '.join(variants)}</small>", unsafe_allow_html=True)
                    elif comm_name:
                        suggestion = r.suggested_alternatives.get(item)
                        if suggestion:
                            st.write(f"⚠️ **{item}** → not in this platter · closest: *{suggestion}*")
                        else:
                            st.write(f"⚠️ **{item}** → *{comm_name}* *(not in this platter)*")
                    else:
                        st.write(f"❌ **{item}** — not found in any community")

                # Full platter item list
                if r.items:
                    st.markdown("**All items in this platter:**")
                    cols = st.columns(3)
                    for idx, item_name in enumerate(r.items):
                        cols[idx % 3].write(f"• {item_name}")
