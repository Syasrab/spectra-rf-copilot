import os
import re
import io
import math
import time
import requests
from google import genai
from pypdf import PdfReader

# =========================================================
# AUTHENTICATION
# =========================================================

def get_token(max_retries=3, retry_delay=5):
    """Get a temporary access token from DigiKey using client credentials."""
    for attempt in range(1, max_retries + 1):
        try:
            response = requests.post(
                "https://api.digikey.com/v1/oauth2/token",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data={
                    "client_id": os.environ.get("DIGIKEY_CLIENT_ID"),
                    "client_secret": os.environ.get("DIGIKEY_CLIENT_SECRET"),
                    "grant_type": "client_credentials",
                },
                timeout=15,
            )
            data = response.json()
            if "access_token" not in data:
                print("Failed to get token. Full response:")
                print(data)
                exit(1)
            print("Got a token successfully.")
            return data["access_token"]
        except requests.exceptions.ConnectionError as e:
            print(f"Connection attempt {attempt}/{max_retries} failed: {e}")
            if attempt < max_retries:
                print(f"Retrying in {retry_delay} seconds...")
                time.sleep(retry_delay)
            else:
                print("All retry attempts failed. Check your internet connection.")
                raise


# =========================================================
# SEARCH
# =========================================================

def search_part(token, keywords, limit=50, max_retries=3, retry_delay=5):
    """Search DigiKey by free-text keywords."""
    for attempt in range(1, max_retries + 1):
        try:
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
                timeout=15,
            )
            return response.json()
        except requests.exceptions.ConnectionError as e:
            print(f"Search connection attempt {attempt}/{max_retries} failed: {e}")
            if attempt < max_retries:
                print(f"Retrying in {retry_delay} seconds...")
                time.sleep(retry_delay)
            else:
                print("All retry attempts failed for this search.")
                raise


# =========================================================
# FILTERING
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
    """Filter raw DigiKey search results down to genuinely usable bare ICs."""
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
# LIVE DATASHEET RAG (DigiKey primary, Mouser fallback)
# =========================================================

_datasheet_cache = {}

def search_mouser_datasheet(part_number):
    """
    Fallback datasheet source. Searches Mouser for the exact same part
    number DigiKey gave us, and returns its datasheet URL if found. Used
    only when DigiKey's own DatasheetUrl fails or isn't a real PDF (e.g.
    AMD/Xilinx's JavaScript documentation portal).
    """
    api_key = os.environ.get("MOUSER_API_KEY")
    if not api_key:
        return None

    try:
        response = requests.post(
            f"https://api.mouser.com/api/v1/search/keyword?apiKey={api_key}&version=1",
            headers={"Content-Type": "application/json"},
            json={
                "SearchByKeywordRequest": {
                    "keyword": part_number,
                    "records": 3,
                    "startingRecord": 0,
                    "searchOptions": "",
                    "searchWithYourSignUpLanguage": "",
                }
            },
            timeout=15,
        )
        data = response.json()
        parts = data.get("SearchResults", {}).get("Parts", [])
        for part in parts:
            if part.get("ManufacturerPartNumber", "").upper() == part_number.upper():
                datasheet_url = part.get("DataSheetUrl")
                if datasheet_url:
                    return datasheet_url
        return None
    except Exception as e:
        print(f"  Mouser fallback search failed: {e}")
        return None


def fetch_datasheet_text(url, max_pages=2, part_number=None):
    """
    Download a real manufacturer PDF datasheet and extract text. If the
    given URL isn't a real PDF (e.g. a JavaScript documentation portal),
    and a part_number is given, falls back to searching Mouser for the
    same exact part and trying its datasheet link instead.
    """
    if url in _datasheet_cache:
        return _datasheet_cache[url]

    original_url = url
    if url.startswith("//"):
        url = "https:" + url

    def try_fetch(fetch_url):
        response = requests.get(fetch_url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        content_type = response.headers.get("Content-Type", "")
        if "application/pdf" in content_type or response.content.startswith(b"%PDF"):
            reader = PdfReader(io.BytesIO(response.content))
            text = ""
            for page in reader.pages[:max_pages]:
                text += page.extract_text() + "\n"
            return text[:3000]
        return None

    try:
        text = try_fetch(url)
        if text:
            _datasheet_cache[original_url] = text
            return text

        if part_number:
            print(f"  DigiKey's datasheet link for {part_number} isn't a direct PDF, trying Mouser instead...")
            mouser_url = search_mouser_datasheet(part_number)
            if mouser_url:
                text = try_fetch(mouser_url)
                if text:
                    print(f"  Success: got real datasheet text from Mouser for {part_number}.")
                    _datasheet_cache[original_url] = text
                    return text

        result = (
            f"(Neither DigiKey's datasheet link ({url}) nor a Mouser fallback "
            f"produced a readable PDF for this part. No datasheet text available "
            f"beyond DigiKey's own structured specs.)"
        )
        _datasheet_cache[original_url] = result
        return result

    except Exception as e:
        return f"(Could not fetch or read datasheet PDF: {e})"


# =========================================================
# GEMINI - GROUNDED REASONING
# =========================================================

def ask_gemini_to_pick_best(candidates, requirement, fetch_datasheets=True):
    """Compare multiple real candidates and recommend the best one."""
    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

    candidates_text = ""
    for p in candidates:
        specs = extract_specs(p)
        specs_lines = "\n".join(f"    - {k}: {v}" for k, v in specs.items())
        part_num = p['ManufacturerProductNumber']

        datasheet_excerpt = ""
        datasheet_url = p.get('DatasheetUrl')
        if fetch_datasheets and datasheet_url:
            print(f"  Fetching real datasheet for {part_num}...")
            text = fetch_datasheet_text(datasheet_url, part_number=part_num)
            datasheet_excerpt = f"""
  REAL DATASHEET EXCERPT:
    {text}
"""

        candidates_text += f"""
{part_num} (${p.get('UnitPrice', 'unknown')}, {p.get('QuantityAvailable', 'unknown')} in stock):
  Description: {p['Description'].get('DetailedDescription')}
  DigiKey Specs:
{specs_lines}
{datasheet_excerpt}
"""

    prompt = f"""You are a strict, grounded RF component selection assistant.

Here are REAL candidate parts, retrieved live from DigiKey's database:
{candidates_text}

USER REQUIREMENT: {requirement}

Rules:
1. Only use the specs and datasheet excerpts given above. Never recall anything from training knowledge.
2. Pick exactly one part, or say none work if that's true.
3. State your pick on the first line: "RECOMMENDATION: <exact part number>"
4. Explain using only the facts given above.
5. Keep under 150 words.
"""
    response = client.models.generate_content(model="gemini-3.6-flash", contents=prompt)
    return response.text


def identify_recommended_part(recommendation_text, candidates):
    """Figure out which specific candidate Gemini actually recommended."""
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
# =========================================================

def min_elements_for_jammers(num_jammers):
    return num_jammers + 1


def patch_antenna_size(freq_mhz, dielectric_constant, substrate_height_mm):
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
    n_ring = max(num_elements - 1, 1)
    ring_radius = (diameter_mm / 2) - (patch_size_mm / 2) - 3
    fits = ring_radius > 0
    spacing_mm = None
    if fits and n_ring > 1:
        spacing_mm = 2 * ring_radius * math.sin(math.pi / n_ring)
    return {"n_ring": n_ring, "ring_radius_mm": ring_radius, "fits": fits, "element_spacing_mm": spacing_mm}


def design_array(num_jammers, center_freq_mhz, diameter_mm, dielectric_constant, substrate_height_mm):
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
    return {
        "band_low_mhz": fmin_mhz,
        "band_high_mhz": fmax_mhz,
        "center_freq_mhz": round((fmin_mhz + fmax_mhz) / 2, 2),
        "bandwidth_mhz": round(fmax_mhz - fmin_mhz, 2),
    }


def parse_current_ma(specs):
    current_str = specs.get("Current - Supply", "")
    match = re.search(r"([\d.]+)\s*mA", current_str)
    if match:
        return float(match.group(1))
    return None


def power_budget(n_elements, lna_specs, digital_backend_watts=5.0):
    lna_current_ma = parse_current_ma(lna_specs)
    voltage_str = lna_specs.get("Voltage - Supply", "unknown")

    if lna_current_ma is None:
        return {"known": False, "note": "DigiKey did not provide a numeric current draw for this chip; power budget cannot be calculated precisely."}

    total_lna_current_ma = lna_current_ma * n_elements
    total_lna_watts = (total_lna_current_ma / 1000) * 3.3
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

def find_and_recommend(token, search_term, requirement, required_child_categories=None, show_dropped=False, fetch_datasheets=True):
    print(f"\n{'='*60}")
    print(f"SEARCHING: {search_term}")
    print(f"{'='*60}")

    result = search_part(token, search_term)
    total = result.get("ProductsCount", 0)
    returned = len(result.get("Products", []))
    print(f"DigiKey reports {total} total matches, returned {returned}.")

    good, dropped = filter_relevant_parts(result, required_child_categories)
    print(f"\nKept {len(good)} usable candidate(s): {[p['ManufacturerProductNumber'] for p in good]}")

    if show_dropped:
        print(f"\nDropped {len(dropped)}:")
        for part_num, reason in dropped:
            print(f"  {part_num} -- {reason}")

    if not good:
        print("\nNo usable candidates survived filtering.")
        return None, None

    print(f"\nAsking Gemini to recommend the best of {len(good)} candidate(s)...")
    recommendation_text = ask_gemini_to_pick_best(good, requirement, fetch_datasheets=fetch_datasheets)
    print(f"\n--- Gemini's recommendation ---")
    print(recommendation_text)

    chosen_part = identify_recommended_part(recommendation_text, good)
    return recommendation_text, chosen_part


def safe_find_and_recommend(token, **kwargs):
    try:
        return find_and_recommend(token, **kwargs)
    except Exception as e:
        print(f"  WARNING: This category failed and will be marked unconfirmed: {e}")
        return f"(This category could not be searched due to a connection error: {e})", None


# =========================================================
# STAGE 1-2: INTAKE + CONFIRM UNDERSTANDING
# =========================================================

def understand_problem(user_description):
    """Gemini reads the user's free-text description, extracts structured
    parameters, and writes a plain-language restatement."""
    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

    prompt = f"""A user described an RF/GNSS interference mitigation design problem in their own words:

"{user_description}"

Extract the following parameters if mentioned. If a parameter isn't mentioned, use the default shown.
- num_jammers (default: 4)
- band_low_mhz (default: 1559)
- band_high_mhz (default: 1610)
- diameter_mm (default: 125)
- isolation_db (default: 20, mentioned but not yet used in calculations, say so honestly)

Respond in EXACTLY this format, nothing else:

PARAMS: num_jammers=<int>, band_low_mhz=<int>, band_high_mhz=<int>, diameter_mm=<int>, isolation_db=<int>

RESTATEMENT: <one clear paragraph, in plain conversational language, restating the problem back to the user using the extracted numbers. End by asking them to confirm or correct anything.>
"""
    response = client.models.generate_content(model="gemini-3.6-flash", contents=prompt)
    text = response.text

    params_match = re.search(r"PARAMS:\s*(.+)", text)
    restatement_match = re.search(r"RESTATEMENT:\s*(.+)", text, re.DOTALL)

    params = {}
    if params_match:
        for pair in params_match.group(1).split(","):
            key, value = pair.strip().split("=")
            params[key.strip()] = int(value.strip())

    restatement = restatement_match.group(1).strip() if restatement_match else text

    return params, restatement


# =========================================================
# STAGE 3: ANTENNA EXPLANATION + CALCULATION
# =========================================================

def explain_antenna_approach(state):
    """Gemini restates its understanding of the antenna sub-problem before
    the deterministic math runs."""
    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
    n_jammers = state["num_jammers"]
    diameter = state["diameter_mm"]
    fmin, fmax = state["band_mhz"]

    prompt = f"""You are about to explain an antenna array design approach to a user, before any calculation runs.

Known parameters:
- Interferers to counter: {n_jammers}
- Array diameter budget: {diameter} mm
- Frequency band: {fmin}-{fmax} MHz

Explain, in plain conversational language:
1. Why the number of antenna elements needed follows the N-1 rule (an N-element adaptive array can null at most N-1 interferers), and what that means for this specific case ({n_jammers} jammers).
2. That the physical patch antenna size will be calculated using the standard Balanis microstrip formula at the center of the given band.
3. That this is all deterministic physics/math, not a guess, so you're confident in this approach before running the numbers.

End by telling the user you're about to calculate the real numbers now.
Keep it to one short paragraph, conversational, not a lecture.
"""
    response = client.models.generate_content(model="gemini-3.6-flash", contents=prompt)
    return response.text


# =========================================================
# CONVERSATIONAL LOOP
# =========================================================

def conversational_loop(token):
    print("="*70)
    print("SPECTRA - Conversational RF Design Assistant")
    print("="*70)
    print("\nDescribe your GNSS interference mitigation problem in your own words.")
    print("Type 'quit' to exit.\n")

    stage = "intake"
    state = None

    while True:
        user_input = input("\nYou: ").strip()

        if user_input.lower() in ("quit", "exit"):
            print("Goodbye.")
            break

        if stage == "intake":
            print("\n(Reading your problem description...)\n")
            params, restatement = understand_problem(user_input)
            state = params
            state["band_mhz"] = (state.pop("band_low_mhz"), state.pop("band_high_mhz"))
            print(f"Spectra: {restatement}")
            stage = "confirm_problem"
            continue

        if stage == "confirm_problem":
            if user_input.lower() in ("yes", "correct", "yep", "right", "confirm"):
                print("\nSpectra: Great, let's move to the antenna array design next. Type 'continue' when ready.")
                stage = "confirm_antenna"
            else:
                print("\n(Updating based on your correction...)\n")
                params, restatement = understand_problem(user_input)
                state = params
                state["band_mhz"] = (state.pop("band_low_mhz"), state.pop("band_high_mhz"))
                print(f"Spectra: {restatement}")
            continue

        if stage == "confirm_antenna":
            if "continue" in user_input.lower() or "yes" in user_input.lower():
                print("\n(Explaining the antenna approach...)\n")
                explanation = explain_antenna_approach(state)
                print(f"Spectra: {explanation}\n")

                freq_plan_result = frequency_plan(state["band_mhz"][0], state["band_mhz"][1])
                array = design_array(
                    state["num_jammers"],
                    freq_plan_result["center_freq_mhz"],
                    state["diameter_mm"],
                    dielectric_constant=9.8,
                    substrate_height_mm=3.175,
                )
                state["freq_plan"] = freq_plan_result
                state["array"] = array

                print(f"Spectra: Here are the real numbers.")
                print(f"  Elements needed: {array['n_elements']} (1 center + {array['n_elements']-1} ring)")
                print(f"  Patch size: {array['patch_width_mm']} x {array['patch_length_mm']} mm")
                print(f"  Ring radius at {state['diameter_mm']}mm diameter: {array['ring_radius_mm']} mm")
                print(f"  Center frequency: {freq_plan_result['center_freq_mhz']} MHz")
                print(f"\nSpectra: Does this look right? Type 'continue' to move on to component selection.")
                stage = "confirm_components_start"
            else:
                print("Spectra: Let me know if you'd like to change anything, or type 'continue' when ready.")
            continue

        print(f"\n(Stage '{stage}' not built yet, this is as far as we've tested so far.)")


# =========================================================
# MAIN
# =========================================================

def main():
    part_number = "MAX2659ELT+T"
    api_key = os.environ.get("MOUSER_API_KEY")

    response = requests.post(
        f"https://api.mouser.com/api/v1/search/keyword?apiKey={api_key}&version=1",
        headers={"Content-Type": "application/json"},
        json={
            "SearchByKeywordRequest": {
                "keyword": part_number,
                "records": 3,
                "startingRecord": 0,
                "searchOptions": "",
                "searchWithYourSignUpLanguage": "",
            }
        },
        timeout=15,
    )
    data = response.json()
    parts = data.get("SearchResults", {}).get("Parts", [])
    for part in parts:
        print("ManufacturerPartNumber:", repr(part.get("ManufacturerPartNumber")))
        print("DataSheetUrl:", repr(part.get("DataSheetUrl")))
        print()


if __name__ == "__main__":
    main()