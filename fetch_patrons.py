import os
import json
import requests
import sys

ACCESS_TOKEN = os.environ.get("PATREON_ACCESS_TOKEN")
OUTPUT_FILE = "subscribers.json"

if not ACCESS_TOKEN:
    print("Error: PATREON_ACCESS_TOKEN is missing.")
    sys.exit(1)

headers = {
    "Authorization": f"Bearer {ACCESS_TOKEN}",
    "User-Agent": "GitHub-Action-Daily-Sync"
}

def get_campaign_id():
    url = "https://www.patreon.com/api/oauth2/v2/identity?include=campaign"
    r = requests.get(url, headers=headers)
    r.raise_for_status()
    data = r.json()
    if "included" in data:
        return data["included"][0]["id"]
    return None

def get_all_members_and_tiers(campaign_id):
    members = []
    tiers = {}
    
    url = f"https://www.patreon.com/api/oauth2/v2/campaigns/{campaign_id}/members"
    params = {
        "include": "currently_entitled_tiers",
        "fields[member]": "full_name,patron_status",
        "fields[tier]": "title,amount_cents",
        "page[count]": 100
    }

    while url:
        r = requests.get(url, headers=headers, params=params)
        r.raise_for_status()
        data = r.json()
        
        for member in data.get("data", []):
            if member["attributes"]["patron_status"] == "active_patron":
                members.append(member)

        if "included" in data:
            for item in data["included"]:
                if item["type"] == "tier":
                    tiers[item["id"]] = {
                        "id": item["id"],
                        "title": item["attributes"]["title"],
                        "amount": item["attributes"]["amount_cents"]
                    }
        
        url = data.get("links", {}).get("next")
        params = {}

    return members, tiers

def main():
    try:
        print("Fetching Campaign ID...")
        campaign_id = get_campaign_id()
        if not campaign_id:
            print("No campaign found.")
            return

        print(f"Fetching members for Campaign {campaign_id}...")
        members, tiers_dict = get_all_members_and_tiers(campaign_id)

        sorted_tiers = sorted(tiers_dict.values(), key=lambda x: x['amount'], reverse=True)
        top_tier_ids = [t['id'] for t in sorted_tiers[:2]]
        
        print(f"Top 2 Tiers identified: {[t['title'] for t in sorted_tiers[:2]]}")

        temp_list = []

        for member in members:
            member_tier_ids = [
                t["id"] for t in member.get("relationships", {})
                .get("currently_entitled_tiers", {}).get("data", [])
            ]
            
            if any(tid in top_tier_ids for tid in member_tier_ids):
                highest_tier_owned = max(
                    [tiers_dict[tid] for tid in member_tier_ids if tid in tiers_dict],
                    key=lambda x: x['amount'],
                    default=None
                )
                
                if highest_tier_owned:
                    temp_list.append({
                        "name": member["attributes"]["full_name"],
                        "tier": highest_tier_owned['title'],
                        "amount": highest_tier_owned['amount']
                    })

        temp_list.sort(key=lambda x: x['amount'], reverse=True)

        final_list = [{"name": item["name"], "tier": item["tier"]} for item in temp_list]

        print(f"Found {len(final_list)} subscribers. Sorted by most expensive.")

        with open(OUTPUT_FILE, "w") as f:
            json.dump(final_list, f, indent=2)
            
        print(f"Successfully saved to {OUTPUT_FILE}")

    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
