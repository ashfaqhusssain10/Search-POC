from core.connections import neo4j_session
import json

def run_diagnostic():
    with neo4j_session() as session:
        # 1. Communities for our items
        items = ['Chicken Pakoda', 'Prawn Pakoda', 'Fish Pakoda']
        for item_name in items:
            res = session.run('MATCH (i:Item {name: $name})-[:MEMBER_OF]->(c:Community) RETURN c.id, c.name', {'name': item_name}).single()
            if res:
                print(f"Item: {item_name} -> Comm: {res[0]} ({res[1]})")
            else:
                print(f"Item: {item_name} -> NOT IN ANY COMMUNITY")
                
        # 2. Communities linked to Standard Platter
        platter_name = 'Standard Platter'
        res = session.run('MATCH (p:Platter {name: $name})-[:HAS_COMMUNITY]->(c:Community) RETURN c.id, c.name', {'name': platter_name}).data()
        print(f"\n{platter_name} is linked to these Communities:")
        for rec in res:
            print(f"  - {rec['c.id']} ({rec['c.name']})")
            
        # 3. Items contained in Standard Platter and their communities
        res = session.run('''
            MATCH (p:Platter {name: $name})-[:CONTAINS]->(i:Item)
            OPTIONAL MATCH (i)-[:MEMBER_OF]->(c:Community)
            RETURN i.name, c.id, c.name
        ''', {'name': platter_name}).data()
        print(f"\nItems in {platter_name}:")
        for rec in res:
            print(f"  - {rec['i.name']} -> {rec['c.id']} ({rec['c.name']})")

if __name__ == "__main__":
    run_diagnostic()
