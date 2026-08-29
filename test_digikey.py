import os
import re
import math
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
#
# Lessons learned building this, kept here as documentation:
#   1. Result limit matters - DigiKey silently returns only what you ask
#      for. Always compare ProductsCount vs how many you actually got back.
#   2. Status alone isn't enough - "Not For New Designs" is a real, valid
#      status distinct from "Discontinued"/"Obsolete", and just as unusable.
#   3. Top-level Category.Name is usually a broad bucket (e.g. "RF and
#      Wireless") shared by good and bad parts alike. The real signal is in
#      ChildCategories.
#   4. Different bad ChildCategories look similar to good ones - dev kits,
#      finished modules, and mixers all get filed near real chips. You need
#      an exclude list AND (when known) a require list, not just one.
#   5. Part-number string matching (checking for "EVAL"/"EVKIT" in the
#      name) misses most real dev kits, which have arbitrary naming (e.g.
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
    Leave as None when you don't yet know the right category name for a
    new part type - run diagnostic_search() first to find out.
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
# hallucination failures found in the original 5-model comparison.
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
{p['ManufacturerProductNumber']} (${p.get('UnitPrice', 'unknown')}, {p.get('QuantityAvailable', 'unknown')} in stock):
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
3. State your final pick clearly on the very first line, in the format: "RECOMMENDATION: <exact part number>"
4. Then explain your choice using only the specific numbers given above (frequency range, gain, noise figure, stock, price).
5. If two candidates seem similarly good, say so and explain the tradeoff.
6. Keep the answer under 150 words.
"""
    response = client.models.generate_content(model="gemini-3.6-flash", contents=prompt)
    return response.text


def identify_recommended_part(recommendation_text, candidates):
    """
    Figure out which specific candidate Gemini actually recommended, instead
    of assuming it's whichever one the search API happened to return first.
    """
    match = re.search(r"RECOMMENDATION:\s*\**\s*([A-Za-z0-9\-/+]+)", recommendation_text)
    if match:
        recommended_num = match.group(1).strip("*").strip()
        for p in candidates:
            if p["ManufacturerProductNumber"] == recommended_num:
                return p

    best_p, best_pos = None, len(recommendation_text) + 1
    for p in candidates:
        pos = recommendation_text.find(p["ManufacturerProductNumber"])
        if pos != -1 and pos < best_pos:
            best_p, best_pos = p, pos
    return best_p


# =========================================================
# DETERMINISTIC RF CALCULATIONS
# Pure math, no AI involved. Array element count and patch dimensions are
# governed by fixed physics/formulas, not judgment calls, so they should
# never be left to an LLM to "decide."
# =========================================================

def min_elements_for_jammers(num_jammers):
    """N-1 rule: an N-element adaptive array can null at most N-1 interferers."""
    return num_jammers + 1


def patch_antenna_size(freq_mhz, dielectric_constant, substrate_height_mm):
    """Balanis microstrip patch antenna sizing formula. Returns mm."""
    c = 299792458
    f = freq_mhz * 1e6
    h = substrate_height_mm / 1000
    er = dielectric_constant

    W = c / (2 * f) * math.sqrt(2 / (er + 1))
    ereff = (er + 1) / 2 + (er - 1) / 2 * (1 + 12 * h / W) ** -0.5
    dL = 0.412 * h * (ereff + 0.3) * (W / h + 0.264) / ((ereff - 0.258) * (W / h + 0.8))
    L = c / (2 * f * math.sqrt(ereff)) - 2 * dL

    return {"width_mm": W * 1000, "length_mm": L * 1000, "effective_dielectric": ereff}


def array_ring_geometry(num_elements, diameter_mm, patch_size_mm):
    """Given N elements (1 center + (N-1) ring) and a diameter budget,
    compute the ring radius and whether it physically fits."""
    n_ring = max(num_elements - 1, 1)
    ring_radius = (diameter_mm / 2) - (patch_size_mm / 2) - 3  # 3mm margin
    fits = ring_radius > 0
    spacing_mm = None
    if fits and n_ring > 1:
        spacing_mm = 2 * ring_radius * math.sin(math.pi / n_ring)
    return {"n_ring": n_ring, "ring_radius_mm": ring_radius, "fits": fits, "element_spacing_mm": spacing_mm}


def design_array(num_jammers, center_freq_mhz, diameter_mm, dielectric_constant, substrate_height_mm):
    """Run all array calculations together and return one clean result."""
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


def frequency_plan(fmin_mhz, fmax_mhz):
    """Structured frequency output, separate from any AI prose."""
    return {
        "band_low_mhz": fmin_mhz,
        "band_high_mhz": fmax_mhz,
        "center_freq_mhz": round((fmin_mhz + fmax_mhz) / 2, 2),
        "bandwidth_mhz": round(fmax_mhz - fmin_mhz, 2),
    }


def parse_current_ma(specs):
    """Extract a numeric current-draw value (mA) from a specs dict, if present."""
    current_str = specs.get("Current - Supply", "")
    match = re.search(r"([\d.]+)\s*mA", current_str)
    if match:
        return float(match.group(1))
    return None


def power_budget(n_elements, lna_specs, digital_backend_watts=5.0):
    """
    Calculate a real power budget using the ACTUAL retrieved LNA current
    draw, not an assumption. Falls back honestly if data isn't available.
    """
    lna_current_ma = parse_current_ma(lna_specs)
    voltage_str = lna_specs.get("Voltage - Supply", "unknown")

    if lna_current_ma is None:
        return {
            "known": False,
            "note": "DigiKey did not provide a numeric current draw for this chip; power budget cannot be calculated precisely.",
        }

    total_lna_current_ma = lna_current_ma * n_elements
    total_lna_watts = (total_lna_current_ma / 1000) * 3.3  # assume 3.3V rail for the LNA stage
    total_watts = total_lna_watts + digital_backend_watts

    return {
        "known": True,
        "per_element_current_ma": lna_current_ma,
        "per_element_voltage_range": voltage_str,
        "total_elements": n_elements,
        "total_lna_current_ma": round(total_lna_current_ma, 1),
        "total_lna_power_w": round(total_lna_watts, 2),
        "estimated_digital_backend_w": digital_backend_watts,
        "estimated_total_system_power_w": round(total_watts, 2),
    }


# =========================================================
# FULL PIPELINE
# =========================================================

def find_and_recommend(token, search_term, requirement, required_child_categories=None, show_dropped=False):
    """Reusable single-category search + filter + grounded recommendation."""
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
        return None, None

    print(f"\nAsking Gemini to recommend the best of {len(good)} candidate(s)...")
    recommendation_text = ask_gemini_to_pick_best(good, requirement)
    print(f"\n--- Gemini's recommendation ---")
    print(recommendation_text)

    chosen_part = identify_recommended_part(recommendation_text, good)
    return recommendation_text, chosen_part


# =========================================================
# COMPLETE SOLUTION - matches Dr. Wasif's exact 4 requirements
# =========================================================

SYSTEM_ARCHITECTURE = [
    {"stage": "Antenna Array", "verified": True, "note": "Dimensions calculated below"},
    {"stage": "LNA", "verified": True, "note": "Live, DigiKey-verified recommendation below"},
    {"stage": "Bandpass Filter", "verified": False, "note": "Architecture reference only, not yet DigiKey-verified"},
    {"stage": "RF Transceiver / GNSS Front-End", "verified": False, "note": "Architecture reference only, not yet DigiKey-verified"},
    {"stage": "Digital Beamforming Processor (FPGA)", "verified": False, "note": "Architecture reference only, not yet DigiKey-verified"},
    {"stage": "GNSS Receiver Output", "verified": False, "note": "Final position/timing output stage"},
]


def complete_solution(token, num_jammers, band_mhz, diameter_mm, dielectric_constant, substrate_height_mm):
    """
    Answers Dr. Wasif's exact four requirements:
    1. Antenna dimensions
    2. How to power it up
    3. What frequencies to operate at
    4. Which chips and components to buy (with reasons)
    Plus a system block diagram (architecture list, diagram added separately).
    """
    fmin, fmax = band_mhz
    freq_plan = frequency_plan(fmin, fmax)
    array = design_array(num_jammers, freq_plan["center_freq_mhz"], diameter_mm, dielectric_constant, substrate_height_mm)

    requirement = (
        f"I need an LNA for a {array['n_elements']}-element GNSS antenna array "
        f"covering {fmin}-{fmax} MHz, mounted outdoors, needs to be currently purchasable."
    )
    recommendation_text, chosen_part = find_and_recommend(
        token,
        search_term="GPS GNSS LNA amplifier",
        requirement=requirement,
        required_child_categories={"RF Amplifiers"},
    )

    power = power_budget(array["n_elements"], extract_specs(chosen_part)) if chosen_part else None

    return {
        "1_antenna_dimensions": {
            "n_elements": array["n_elements"],
            "topology": f"1 center + {array['n_elements']-1} ring",
            "patch_width_mm": array["patch_width_mm"],
            "patch_length_mm": array["patch_length_mm"],
            "ring_radius_mm": array["ring_radius_mm"],
            "reason": (
                f"N-1 rule: {num_jammers} jammers require at least {array['n_elements']} elements. "
                f"Patch size calculated via the standard Balanis microstrip formula at "
                f"{freq_plan['center_freq_mhz']} MHz on a substrate with dielectric constant {dielectric_constant}."
            ),
        },
        "2_powering": power if power else {"known": False, "note": "No chip was recommended, cannot calculate power."},
        "3_frequencies": {
            **freq_plan,
            "reason": f"Band chosen to cover the required GNSS constellations within {fmin}-{fmax} MHz.",
        },
        "4_chips_and_components": {
            "recommendation": recommendation_text,
            "chosen_part": chosen_part["ManufacturerProductNumber"] if chosen_part else None,
        },
        "system_architecture": SYSTEM_ARCHITECTURE,
    }


# =========================================================
# MAIN
# =========================================================

def main():
    print("Getting DigiKey access token...")
    token = get_token()

    result = complete_solution(
        token,
        num_jammers=4,
        band_mhz=(1559, 1610),
        diameter_mm=125,
        dielectric_constant=9.8,
        substrate_height_mm=3.175,
    )

    print("\n\n=== FULL STRUCTURED RESULT ===")
    import json
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()