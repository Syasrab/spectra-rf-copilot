import os
import requests

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

def main():
    print("Step 1: getting access token...")
    token = get_token()

    print("Step 2: searching for MAX2659...")
    result = search_part(token, "MAX2659")

    print("Step 3: raw result below:")
    import json
    print(json.dumps(result, indent=2))

if __name__ == "__main__":
    main()