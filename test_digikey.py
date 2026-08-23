import os
import requests
from google import genai

# =========================================================
# AUTHENTICATION
# =========================================================

def get_token():
    """Get a temporary access token from DigiKey using client credentials."""
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


# =========================================================
# SEARCH
# =========================================================

def search_part(token, keywords, limit=50):
    """Search DigiKey by free-text keywords. Max useful limit is around 50."""
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


# =========================================================
# FILTERING
# Lessons learned building this:
#   1. Result limit matters - DigiKey silently returns only what you ask for,
#      always check ProductsCount vs how many you actually processed.
#   2. Status alone isn't enough - "Not For New Designs" is a real, valid
#      status distinct from "Discontinued"/"Obsolete", and just as unusable.
#   3. Top-level Category.Name is usually a broad bucket (e.g. "RF and
#      Wireless") that both good and bad parts share. The real signal is in
#      ChildCategories.
#   4. Different bad ChildCategories look similar to good ones - dev kits,
#      finished modules, and mixers all get filed near real chips. You need
#      an exclude list AND (when known) a require list, not just one or
#      the other.
#   5. Part-number string matching (checking for "EVAL"/"EVKIT" in the name)
#      misses most real dev kits, which have arbitrary naming (e.g.
#      "33-TP5390SDK-0"). Category-based exclusion is far more reliable.
# =========================================================

BAD_STATUSES = {
    "Discontinued at DigiKey",
    "Obsolete",
    "Not For New Designs",
}

EXCLUDED_TOP_CATEGORIES = {
    "Development Boards, Kits, Programmers",
}

EXCLUDED_CHILD_CATEGORIES = {
    "RF Receiver, Transmitter, and Transceiver Finished Units",
    "Evaluation Boards",
}


def filter_relevant_parts(data, required_child_categories=None):
    """
    Filter raw DigiKey search results down to genuinely usable bare ICs.

    required_child_categories: optional set of category names where the
    part must match at least ONE to be kept (e.g. {"RF Amplifiers"}).
    Leave as None to skip this check (useful when you don't yet know the
    right category name for a new part type - run a diagnostic search first).
    """
    candidates = data.get("Products", [])
    good = []
    dropped = []

    for p in candidates:
        part_num = p["ManufacturerProductNumber"]
        status = p["ProductStatus"]["Status"]
        cat = p.get("Category", {})
        top_category = cat.get("Name")
        child_names = [c["Name"] for c in cat.get("ChildCategories", [])]

        if status in BAD_STATUSES:
            dropped.append((part_num, f"status is '{status}'"))
        elif top_category in EXCLUDED_TOP_CATEGORIES:
            dropped.append((part_num, f"top category '{top_category}' is excluded (dev kit/board)"))
        elif any(c in EXCLUDED_CHILD_CATEGORIES for c in child_names):
            dropped.append((part_num, f"child category excluded (finished module/eval board): {child_names}"))
        elif required_child_categories and not any(c in required_child_categories for c in child_names):
            dropped.append((part_num, f"not filed under a required category (children: {child_names})"))
        else:
            good.append(p)

    return good, dropped


def extract_specs(product):
    """Turn DigiKey's Parameters list into a simple {name: value} dict."""
    specs = {}
    for param in product.get("Parameters", []):
        specs[param["ParameterText"]] = param["ValueText"]
    return specs


# =========================================================
# DIAGNOSTIC TOOL
# Use this FIRST whenever searching a new part category, to see real
# category names and descriptions before writing filter rules.
# Guessing category names (as we initially did with "RF Amplifiers")
# leads to filters that silently do nothing.
# =========================================================

def diagnostic_search(token, search_term, limit=50):
    print(f"\nSearching DigiKey for: {search_term}...")
    result = search_part(token, search_term, limit=limit)
    print(f"Total raw results: {result.get('ProductsCount', 0)} (showing {len(result.get('Products', []))})\n")

    for p in result.get("Products", []):
        part_num = p["ManufacturerProductNumber"]
        status = p["ProductStatus"]["Status"]
        cat = p.get("Category", {})
        child_names = [c["Name"] for c in cat.get("ChildCategories", [])]
        print(f"{part_num} | {status}")
        print(f"  Top category: {cat.get('Name')}")
        print(f"  Child categories: {child_names}")
        print(f"  Description: {p['Description'].get('DetailedDescription')}")
        print()

    return result


# =========================================================
# GEMINI - GROUNDED REASONING
# Strict rule enforced in every prompt: only use the retrieved data given,
# never recall specs from training knowledge. This is what fixed the
# hallucination failures we found in the original 5-model comparison.
# =========================================================

def ask_gemini_about_part(specs, part_number, requirement):
    """Check a single specific part against a requirement."""
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
    response = client.models.generate_content(model="gemini-3.6-flash", contents=prompt)
    return response.text


def ask_gemini_to_pick_best(candidates, requirement):
    """Compare multiple real candidates and recommend the best one."""
    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

    candidates_text = ""
    for p in candidates:
        specs = extract_specs(p)
        specs_lines = "\n".join(f"    - {k}: {v}" for k, v in specs.items())
        candidates_text += f"""
{p['ManufacturerProductNumber']} (${p['UnitPrice']}, {p['QuantityAvailable']} in stock):
  Description: {p['Description'].get('DetailedDescription')}
  Specs:
{specs_lines}
"""

    prompt = f"""You are a strict, grounded RF component selection assistant.

Here are REAL candidate parts, retrieved live from DigiKey's database:
{candidates_text}

USER REQUIREMENT: {requirement}

Rules:
1. Only use the specs and descriptions given above. Never recall anything about these parts from your own training knowledge.
2. Pick exactly one part as your recommendation, or say none of them work if that's true.
3. Explain your choice using only the specific numbers given above (frequency range, gain, noise figure, stock, price).
4. If two candidates seem similarly good, say so and explain the tradeoff.
5. Keep the answer under 150 words.
"""
    response = client.models.generate_content(model="gemini-3.6-flash", contents=prompt)
    return response.text


# =========================================================
# FULL PIPELINE - reusable for any component category
# =========================================================

def find_and_recommend(token, search_term, requirement, required_child_categories=None, show_dropped=False):
    print(f"\n{'='*60}")
    print(f"SEARCHING: {search_term}")
    print(f"{'='*60}")

    result = search_part(token, search_term)
    total = result.get("ProductsCount", 0)
    returned = len(result.get("Products", []))
    print(f"DigiKey reports {total} total matches, returned {returned}.")
    if total > returned:
        print(f"WARNING: {total - returned} results were not retrieved (limit reached).")

    good, dropped = filter_relevant_parts(result, required_child_categories)
    print(f"\nKept {len(good)} usable candidate(s): {[p['ManufacturerProductNumber'] for p in good]}")

    if show_dropped:
        print(f"\nDropped {len(dropped)}:")
        for part_num, reason in dropped:
            print(f"  {part_num} -- {reason}")

    if not good:
        print("\nNo usable candidates survived filtering. Try a different search term,")
        print("or run diagnostic_search() to see what categories/descriptions actually exist.")
        return None

    print(f"\nAsking Gemini to recommend the best of {len(good)} candidate(s)...")
    recommendation = ask_gemini_to_pick_best(good, requirement)
    print(f"\n--- Gemini's recommendation ---")
    print(recommendation)
    return recommendation

# =========================================================
# DETERMINISTIC RF CALCULATIONS
# Pure math, no AI involved. This is intentional: array element count and
# patch dimensions are governed by fixed physics/formulas, not judgment
# calls, so they should never be left to an LLM to "decide."
# =========================================================

import math

def min_elements_for_jammers(num_jammers):
    """N-1 rule: an N-element adaptive array can null at most N-1 interferers."""
    return num_jammers + 1


def patch_antenna_size(freq_mhz, dielectric_constant, substrate_height_mm):
    """
    Balanis microstrip patch antenna sizing formula.
    Returns patch width and length in mm.
    """
    c = 299792458  # speed of light, m/s
    f = freq_mhz * 1e6
    h = substrate_height_mm / 1000
    er = dielectric_constant

    W = c / (2 * f) * math.sqrt(2 / (er + 1))
    ereff = (er + 1) / 2 + (er - 1) / 2 * (1 + 12 * h / W) ** -0.5
    dL = 0.412 * h * (ereff + 0.3) * (W / h + 0.264) / ((ereff - 0.258) * (W / h + 0.8))
    L = c / (2 * f * math.sqrt(ereff)) - 2 * dL

    return {"width_mm": W * 1000, "length_mm": L * 1000, "effective_dielectric": ereff}


def array_ring_geometry(num_elements, diameter_mm, patch_size_mm):
    """
    Given N elements (1 center + (N-1) ring) and a diameter budget,
    compute the ring radius and whether it physically fits.
    """
    n_ring = max(num_elements - 1, 1)
    ring_radius = (diameter_mm / 2) - (patch_size_mm / 2) - 3  # 3mm margin
    fits = ring_radius > 0
    spacing_mm = None
    if fits and n_ring > 1:
        spacing_mm = 2 * ring_radius * math.sin(math.pi / n_ring)
    return {"n_ring": n_ring, "ring_radius_mm": ring_radius, "fits": fits, "element_spacing_mm": spacing_mm}


def design_array(num_jammers, center_freq_mhz, diameter_mm, dielectric_constant, substrate_height_mm):
    """Run all three calculations together and return one clean result."""
    n_elements = min_elements_for_jammers(num_jammers)
    patch = patch_antenna_size(center_freq_mhz, dielectric_constant, substrate_height_mm)
    geometry = array_ring_geometry(n_elements, diameter_mm, max(patch["width_mm"], patch["length_mm"]))

    return {
        "n_elements": n_elements,
        "patch_width_mm": round(patch["width_mm"], 1),
        "patch_length_mm": round(patch["length_mm"], 1),
        "ring_radius_mm": round(geometry["ring_radius_mm"], 1) if geometry["fits"] else None,
        "fits_in_diameter": geometry["fits"],
        "element_spacing_mm": round(geometry["element_spacing_mm"], 1) if geometry["element_spacing_mm"] else None,
    }
# =========================================================
# MAIN
# =========================================================

def main():
    result = design_array(
        num_jammers=4,
        center_freq_mhz=1584.5,
        diameter_mm=125,
        dielectric_constant=9.8,
        substrate_height_mm=3.175,
    )
    print("Array design result:")
    for key, value in result.items():
        print(f"  {key}: {value}")

if __name__ == "__main__":
    main()