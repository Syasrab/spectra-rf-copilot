import os
import requests
from google import genai

def get_token():
    response = requests.post(
        "https://api.digikey.com/v1/oauth2/token",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "client_id": os.environ.get("DIGIKEY_CLIENT_ID"),
            "client_secret": os.environ.get("DIGIKEY_CLIENT_SECRET"),
            "grant_type": "client_credentials",
        },
    )
    data = response.json()
    if "access_token" not in data:
        print("Failed to get token. Full response:")
        print(data)
        exit(1)
    print("Got a token successfully.")
    return data["access_token"]

def search_part(token, keywords, limit=20):
    response = requests.post(
        "https://api.digikey.com/products/v4/search/keyword",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "X-DIGIKEY-Client-Id": os.environ.get("DIGIKEY_CLIENT_ID"),
            "X-DIGIKEY-Locale-Site": "US",
            "X-DIGIKEY-Locale-Language": "en",
            "X-DIGIKEY-Locale-Currency": "USD",
        },
        json={"Keywords": keywords, "Limit": limit},
    )
    return response.json()

# Categories we consider genuinely relevant for RF front-end parts.
# Anything not in this list (mixers, antennas, connectors, eval boards) gets dropped.
ALLOWED_CATEGORIES = {
    "RF Amplifiers",
    "RF and Wireless",
}

def filter_relevant_parts(data):
    candidates = data.get("Products", [])
    good = []
    dropped = []
    for p in candidates:
        part_num = p["ManufacturerProductNumber"]
        status = p["ProductStatus"]["Status"]
        cat = p.get("Category", {})
        child_names = [c["Name"] for c in cat.get("ChildCategories", [])]
        is_eval = "EVKIT" in part_num.upper() or "EVAL" in part_num.upper() or "-EVB" in part_num.upper()

        if status != "Active":
            dropped.append((part_num, f"status is '{status}', not Active"))
        elif is_eval:
            dropped.append((part_num, "looks like an evaluation board, not the chip itself"))
        elif "RF Amplifiers" not in child_names:
            dropped.append((part_num, f"not filed under RF Amplifiers (children: {child_names})"))
        else:
            good.append(p)

    return good, dropped

def extract_specs(product):
    specs = {}
    for param in product.get("Parameters", []):
        specs[param["ParameterText"]] = param["ValueText"]
    return specs

def ask_gemini_about_part(specs, part_number, requirement):
    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

    specs_text = "\n".join(f"- {k}: {v}" for k, v in specs.items())

    prompt = f"""You are a strict, grounded RF component checker.

Here are the REAL, VERIFIED specs for {part_number}, retrieved live from DigiKey's database:
{specs_text}

USER REQUIREMENT: {requirement}

Rules:
1. Only use the specs listed above. Do not recall anything about this chip from your own training knowledge.
2. If the specs above don't contain enough information to answer, say so explicitly, don't guess.
3. Give a direct yes/no/unclear answer first, then explain using only the specs given.
4. Keep it to 3-4 sentences.
"""

    response = client.models.generate_content(
        model="gemini-3.6-flash",
        contents=prompt,
    )
    return response.text

def main():
    print("Step 1: getting access token...")
    token = get_token()

    search_term = "GPS GNSS LNA amplifier"
    print(f"Step 2: searching DigiKey for: {search_term}...")
    result = search_part(token, search_term)

    print("\nStep 3: filtering...")
    good, dropped = filter_relevant_parts(result)

    print(f"\nChecking the {len(good)} kept results' REAL descriptions:")
    for p in good:
        print(f"\n{p['ManufacturerProductNumber']}:")
        print(f"  Short: {p['Description'].get('ProductDescription')}")
        print(f"  Detailed: {p['Description'].get('DetailedDescription')}")

if __name__ == "__main__":
    main()