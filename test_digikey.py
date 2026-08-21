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

def search_part(token, part_number):
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
        json={"Keywords": part_number, "Limit": 3},
    )
    return response.json()
def pick_best_result(data, part_number):
    candidates = data.get("Products", [])
    good = [
        p for p in candidates
        if "EVKIT" not in p["ManufacturerProductNumber"]
        and "EVAL" not in p["ManufacturerProductNumber"].upper()
        and p["ProductStatus"]["Status"] == "Active"
    ]
    if not good:
        print(f"No active chip found for {part_number}.")
        return None
    # Prefer the one with the most stock, if any have stock
    good.sort(key=lambda p: p["QuantityAvailable"], reverse=True)
    return good[0]

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

    print("Step 2: searching for MAX2659...")
    result = search_part(token, "MAX2659")

    print("Step 3: picking the correct chip out of the results...")
    best = pick_best_result(result, "MAX2659")
    if not best:
        return

    print(f"Found: {best['ManufacturerProductNumber']} (${best['UnitPrice']}, {best['QuantityAvailable']} in stock)")
    print("\nExtracted specs:")
    specs = extract_specs(best)
    for key, value in specs.items():
        print(f"  {key}: {value}")

    print("\nStep 4: asking Gemini a grounded question about this chip...")
    requirement = "I need an LNA that covers 1559-1610 MHz for a GNSS antenna design. Does this chip work?"
    answer = ask_gemini_about_part(specs, "MAX2659", requirement)
    print("\nGemini's answer:")
    print(answer)



if __name__ == "__main__":
    main()