"""Streamlit UI for Item-Based Platter Search POC.

Currently using `search_v4` — pure item-to-item similarity, no platter ranking.
Each selected dish gets its own top-5 closest canonical matches.

To revert to platter-based search:
    1. Swap the import below to `search_v3` (PM-spec) or `search_v2`
       (re-embed at query time)
    2. Uncomment the platter-rendering block at the bottom of this file
    3. Comment out the v4 rendering block currently active
"""

import os

import streamlit as st

from core.connections import neo4j_session
from scripts.search_v4 import ItemQueryResult, search_items_v4

# ── Older paths (kept for easy revert) ──────────────────────────────────────
# from scripts.search import PlatterResult, search_platters             # v1: community-based
# from scripts.search_v2 import PlatterResultV2, search_platters_v2     # v2: re-embed at query time
# from scripts.search_v3 import PlatterResultV3, search_platters_v3     # v3: PM-spec, platter-ranked

st.set_page_config(page_title="Platter Search", page_icon="🍽️", layout="centered")


def _check_password() -> bool:
    """Gate the app behind a shared password stored in env var APP_PASSWORD."""
    expected = os.getenv("APP_PASSWORD")
    if not expected:
        return True  # Local dev — no password configured

    if st.session_state.get("authenticated"):
        return True

    st.title("🔒 Platter Search")
    pw = st.text_input("Password", type="password")
    if st.button("Enter") and pw == expected:
        st.session_state["authenticated"] = True
        st.rerun()
    elif pw and pw != expected:
        st.error("Incorrect password.")
    return False


if not _check_password():
    st.stop()


# ---------------------------------------------------------------------------
# Cached data
# ---------------------------------------------------------------------------

@st.cache_data(show_spinner="Loading dish list...")
def load_canonical_items() -> list[str]:
    """Fetch all Supabase canonical item names from Neo4j, sorted."""
    with neo4j_session() as session:
        result = session.run(
            "MATCH (i:Item {source: 'supabase'}) RETURN i.name AS name ORDER BY i.name"
        )
        return [r["name"] for r in result]


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

st.title("Item Similarity Search")
st.caption("Select dishes — each one shows its top-5 closest matches in the catalogue.")

canonical_items = load_canonical_items()

selected = st.multiselect(
    label="Dishes",
    options=canonical_items,
    placeholder="Type to search and select dishes...",
)

search_clicked = st.button("Search", type="primary", disabled=not selected)

# ---------------------------------------------------------------------------
# Results (v4: pure item-to-item, no platter logic)
# ---------------------------------------------------------------------------

if search_clicked and selected:
    with st.spinner("Searching..."):
        results: list[ItemQueryResult] = search_items_v4(selected, top_k=5)

    if not results:
        st.warning("No matches found.")
    else:
        for r in results:
            veg_badge = (
                "🟢 VEG" if r.veg_type == "VEG"
                else "🔴 NON-VEG" if r.veg_type == "NONVEG"
                else "🟡 EGG" if r.veg_type == "EGG"
                else ""
            )
            header = f"**{r.query_item}**  {veg_badge}"

            with st.expander(header, expanded=True):
                if r.query_embedding_text:
                    st.caption("**Query embedding text**")
                    st.code(r.query_embedding_text, language=None)
                if not r.hits:
                    st.warning("No hits returned for this item.")
                    continue
                for rank, h in enumerate(r.hits, 1):
                    meta_bits = [b for b in (h.veg_type, h.form, h.category) if b]
                    meta_suffix = f" _( {' · '.join(meta_bits)} )_" if meta_bits else ""
                    st.write(
                        f"**#{rank}**  `{h.score:.3f}`  **{h.name}**{meta_suffix}"
                    )
                    if h.embedding_text:
                        st.code(h.embedding_text, language=None)


# ---------------------------------------------------------------------------
# Legacy v3 platter-ranked rendering (kept for easy revert).
# To restore: change import to `search_v3`, uncomment block below, comment
# out the v4 block currently active above.
# ---------------------------------------------------------------------------
#
# if search_clicked and selected:
#     query = ", ".join(selected)
#     with st.spinner("Searching..."):
#         results: list[PlatterResultV3] = search_platters_v3(query)
#     if not results:
#         st.warning("No matching platters found. Try different dishes.")
#     else:
#         platter_filter = st.radio(
#             "Show platters",
#             options=["All", "VEG only", "NON-VEG only"],
#             horizontal=True,
#         )
#         if platter_filter == "VEG only":
#             filtered = [r for r in results if r.veg]
#         elif platter_filter == "NON-VEG only":
#             filtered = [r for r in results if not r.veg]
#         else:
#             filtered = results
#         if not filtered:
#             st.info(f"No {platter_filter.lower()} platters in the results.")
#         else:
#             st.subheader(f"Top {len(filtered)} platters")
#         total_items = len(selected)
#         for i, r in enumerate(filtered, 1):
#             veg_label = "🟢 VEG" if r.veg else "🔴 NON-VEG"
#             header = (
#                 f"#{i}  **{r.name}**  —  {r.matched_count}/{total_items} dishes matched"
#                 f" · score {r.final_score:.2f}  |  {veg_label}"
#             )
#             with st.expander(header, expanded=(i == 1)):
#                 col1, col2, col3, col4 = st.columns(4)
#                 col1.metric("Dishes matched", f"{r.matched_count} / {total_items}")
#                 col2.metric("Coverage", f"{r.coverage:.0%}")
#                 col3.metric("Avg match quality", f"{r.quality:.2f}")
#                 if r.min_price and r.max_price:
#                     col4.metric("Price", f"₹{int(r.min_price)} – ₹{int(r.max_price)}")
#                 else:
#                     col4.metric("Type", r.platter_type)
#                 st.progress(r.coverage)
#                 st.markdown("**Your dishes:**")
#                 for m in r.matches:
#                     if m.canonical and m.platter_item_name:
#                         score_txt = f" _(score {m.score:.2f})_" if m.score else ""
#                         if m.platter_item_name.lower() == m.query_item.lower():
#                             st.write(f"✅ **{m.query_item}**{score_txt}")
#                         else:
#                             st.write(f"✅ **{m.query_item}** → **{m.platter_item_name}**{score_txt}")
#                     else:
#                         st.write(f"❌ **{m.query_item}** — not in this platter")
#                 if r.item_names:
#                     st.markdown("**All items in this platter:**")
#                     cols = st.columns(3)
#                     for idx, item_name in enumerate(r.item_names):
#                         cols[idx % 3].write(f"• {item_name}")
#
# ---------------------------------------------------------------------------
# Legacy v1 community-based rendering (kept for easy revert).
# To restore: change the import at the top to `search_platters` from
# `scripts.search`, and replace the result-rendering block above with this.
# ---------------------------------------------------------------------------
#
# if search_clicked and selected:
#     query = ", ".join(selected)
#     with st.spinner("Searching..."):
#         results: list[PlatterResult] = search_platters(query)
#     if not results:
#         st.warning("No matching platters found. Try different dishes.")
#     else:
#         platter_filter = st.radio(
#             "Show platters",
#             options=["All", "VEG only", "NON-VEG only"],
#             horizontal=True,
#         )
#         if platter_filter == "VEG only":
#             filtered = [r for r in results if r.veg]
#         elif platter_filter == "NON-VEG only":
#             filtered = [r for r in results if not r.veg]
#         else:
#             filtered = results
#         if not filtered:
#             st.info(f"No {platter_filter.lower()} platters in the results.")
#         else:
#             st.subheader(f"Top {len(filtered)} platters")
#         for i, r in enumerate(filtered, 1):
#             veg_label = "🟢 VEG" if r.veg else "🔴 NON-VEG"
#             coverage_label = (
#                 f"{r.matched_communities}/{r.query_community_count} dishes matched"
#                 f" · {r.skeleton_coverage_score:.0%} menu fit"
#             )
#             with st.expander(
#                 f"#{i}  **{r.name}**  —  {coverage_label}  |  {veg_label}",
#                 expanded=(i == 1),
#             ):
#                 col1, col2, col3, col4 = st.columns(4)
#                 col1.metric("Dishes matched", f"{r.matched_communities} / {r.query_community_count}")
#                 col2.metric("Coverage", f"{r.coverage_ratio:.0%}")
#                 col3.metric("Menu fit", f"{r.skeleton_coverage_score:.0%}")
#                 if r.min_price and r.max_price:
#                     col4.metric("Price range", f"₹{int(r.min_price)} – ₹{int(r.max_price)}")
#                 else:
#                     col4.metric("Type", r.platter_type)
#                 st.progress(r.coverage_ratio)
#                 st.markdown("**Menu fit:**")
#                 query_categories = ", ".join(
#                     f"{name}×{count}" for name, count in r.query_category_counts.items()
#                 ) or "No query skeleton available"
#                 platter_categories = ", ".join(
#                     f"{name}×{count}" for name, count in r.platter_category_counts.items()
#                 ) or "No platter skeleton available"
#                 raw_categories = ", ".join(r.platter_category_labels) or "No raw platter categories available"
#                 matched_categories = ", ".join(r.matched_query_categories) or "None"
#                 missing_categories = ", ".join(r.missing_query_categories) or "None"
#                 st.caption(f"Query family skeleton: {query_categories}")
#                 st.caption(f"Platter family skeleton: {platter_categories}")
#                 st.caption(f"Platter raw categories: {raw_categories}")
#                 st.caption(f"Matched families: {matched_categories}")
#                 st.caption(f"Missing families: {missing_categories}")
#                 st.markdown("**Your dishes:**")
#                 for item, comm_name in r.item_to_community.items():
#                     cid = r.item_to_community_id.get(item)
#                     summary = r.community_summaries.get(cid) if cid else None
#                     category_name = r.item_to_category.get(item)
#                     category_suffix = f" [{category_name}]" if category_name else ""
#                     if comm_name and comm_name in r.matched_community_names:
#                         actual_item = r.item_community_map.get(cid or "") if cid else None
#                         available_suffix = f" · available as **{actual_item}**" if actual_item and actual_item.lower() != comm_name.lower() else ""
#                         st.write(f"✅ **{item}**{category_suffix} → matched as *{comm_name}*{available_suffix}")
#                         if summary:
#                             narrative = summary.get("narrative")
#                             if narrative:
#                                 st.caption(narrative)
#                             variants = summary.get("variant_names", [])
#                             if variants:
#                                 st.markdown(f"<small>Also known as: {', '.join(variants)}</small>", unsafe_allow_html=True)
#                     else:
#                         suggestion = r.suggested_alternatives.get(item)
#                         if suggestion:
#                             st.write(
#                                 f"⚠️ **{item}**{category_suffix} → not in this platter · closest match: *{suggestion}*"
#                             )
#                         else:
#                             st.write(f"❌ **{item}**{category_suffix} — not in this platter")
#                 if r.items:
#                     st.markdown("**All items in this platter:**")
#                     cols = st.columns(3)
#                     for idx, item_name in enumerate(r.items):
#                         cols[idx % 3].write(f"• {item_name}")
