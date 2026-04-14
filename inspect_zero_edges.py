import json
from pathlib import Path

with open('llm_cache/zero_edge_canonicals.json') as f:
    zeros = json.load(f)

cache_dir = Path('llm_cache/variants')

print(f"{'Canonical':<45} {'Reason':<18} Near-miss scores (0.5-0.69)")
print('-' * 110)

for item in zeros:
    cid = item['canonical_id']
    cname = item['canonical_name']
    reason = item['reason']

    if reason == 'llm_failure':
        print(f"{cname:<45} {reason:<18} (no cache — will retry automatically)")
        continue

    cache_file = cache_dir / f'canonical_{cid}.json'
    if not cache_file.exists():
        print(f"{cname:<45} {reason:<18} (no cache file!)")
        continue

    data = json.loads(cache_file.read_text())
    scored = data.get('scored', [])
    near_miss = [s for s in scored if isinstance(s, dict) and 0.5 <= float(s.get('score', 0)) < 0.7]

    if near_miss:
        tops = ', '.join(f"{s.get('candidate_id')}={s.get('score')}" for s in near_miss[:5])
        print(f"{cname:<45} {reason:<18} near-misses: {tops}")
    else:
        print(f"{cname:<45} {reason:<18} (0 items scored 0.5+ — truly empty)")
