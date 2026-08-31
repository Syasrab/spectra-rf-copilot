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
    """Get a temporary access token from DigiKey using client credentials.
    Retries automatically on connection timeouts, since DigiKey's API has
    shown intermittent connectivity issues during development."""
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
    """Search DigiKey by free-text keywords. Max useful limit is around 50.
    Retries automatically on connection timeouts."""
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
# LIVE DATASHEET RAG
# DigiKey's structured parametric fields don't capture everything a real
# datasheet says. DigiKey gives us a real link to the manufacturer's PDF
# for every part (DatasheetUrl). We fetch and extract text from it live.
# Some manufacturers (e.g. AMD/Xilinx) use JavaScript documentation portals
# instead of direct PDFs - we detect this and report it honestly rather
# than crash or silently fail.
#
# TODO (next session): Add Mouser API as a fallback datasheet source.
# When DigiKey's DatasheetUrl fails or returns non-PDF content, search
# Mouser for the same part number and try its datasheet link instead.
# =========================================================

_datasheet_cache = {}

def fetch_datasheet_text(url, max_pages=2):
    """
    Download a real manufacturer PDF datasheet and extract text from its
    first pages. Cached in-memory so we don't re-download the same PDF
    twice in one run.
    """
    if url in _datasheet_cache:
        return _datasheet_cache[url]

    if url.startswith("//"):
        url = "https:" + url

    try:
        response = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        content_type = response.headers.get("Content-Type", "")

        if "application/pdf" not in content_type and not response.content.startswith(b"%PDF"):
            result = (
                f"(This manufacturer's datasheet link is not a direct PDF, it is a "
                f"JavaScript-rendered documentation page at {url}. This tool cannot "
                f"read JavaScript-rendered pages, so no datasheet text is available "
                f"for this part beyond DigiKey's own structured specs.)"
            )
            _datasheet_cache[url] = result
            return result

        reader = PdfReader(io.BytesIO(response.content))
        text = ""
        for page in reader.pages[:max_pages]:
            text += page.extract_text() + "\n"
        text = text[:3000]
        _datasheet_cache[url] = text
        return text
    except Exception as e:
        return f"(Could not fetch or read datasheet PDF: {e})"


# =========================================================
# GEMINI - GROUNDED REASONING
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
            text = fetch_datasheet_text(datasheet_url)
            datasheet_excerpt = f"""
  REAL DATASHEET EXCERPT (fetched live from {datasheet_url}):
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

Here are REAL candidate parts, retrieved live from DigiKey's database, each with structured specs and (where available) an excerpt fetched live from the real manufacturer PDF datasheet:
{candidates_text}

USER REQUIREMENT: {requirement}

Rules:
1. Only use the specs and datasheet excerpts given above. Never recall anything about these parts from your own training knowledge.
2. Both the DigiKey specs and the datasheet excerpt are real, verified sources. If the datasheet excerpt confirms something the DigiKey specs don't mention, you may use it.
3. Pick exactly one part as your recommendation, or say none of them work if that's true.
4. State your final pick clearly on the very first line, in the format: "RECOMMENDATION: <exact part number>"
5. Then explain your choice using only the specific facts given above (frequency, gain, noise figure, constellations supported, stock, price).
6. If two candidates seem similarly good, say so and explain the tradeoff.
7. Keep the answer under 150 words.
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
    ring_radius = (diameter_mm / 2) - (patch_size_mm / 2) - 3
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
    """Calculate a real power budget using the ACTUAL retrieved LNA current draw."""
    lna_current_ma = parse_current_ma(lna_specs)
    voltage_str = lna_specs.get("Voltage - Supply", "unknown")

    if lna_current_ma is None:
        return {
            "known": False,
            "note": "DigiKey did not provide a numeric current draw for this chip; power budget cannot be calculated precisely.",
        }

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
        print("\nNo usable candidates survived filtering.")
        return None, None

    print(f"\nAsking Gemini to recommend the best of {len(good)} candidate(s) (fetching real datasheets)...")
    recommendation_text = ask_gemini_to_pick_best(good, requirement, fetch_datasheets=fetch_datasheets)
    print(f"\n--- Gemini's recommendation ---")
    print(recommendation_text)

    chosen_part = identify_recommended_part(recommendation_text, good)
    return recommendation_text, chosen_part


def safe_find_and_recommend(token, **kwargs):
    """Wraps find_and_recommend so a failure in one category (e.g. a
    DigiKey timeout) doesn't crash the entire complete_solution() call.
    Returns (error message, None) on failure, which downstream code
    already handles gracefully as 'not confirmed'."""
    try:
        return find_and_recommend(token, **kwargs)
    except Exception as e:
        print(f"  WARNING: This category failed and will be marked unconfirmed: {e}")
        return f"(This category could not be searched due to a connection error: {e})", None


# =========================================================
# COMPLETE SOLUTION - matches Dr. Wasif's exact 4 requirements,
# covering all 6 verified component categories.
# =========================================================

SYSTEM_ARCHITECTURE_TEMPLATE = [
    "Antenna Array",
    "LNA",
    "Bandpass Filter",
    "GNSS Front-End Receiver",
    "TCXO Reference Clock",
    "Digital Beamforming Processor (FPGA)",
    "Power Regulator (LDO)",
]


def complete_solution(token, num_jammers, band_mhz, diameter_mm, dielectric_constant, substrate_height_mm):
    """
    Answers Dr. Wasif's 4 requirements using all 6 verified component
    categories: antenna dimensions, powering, frequencies, and chips
    (LNA, filter, front-end, TCXO, FPGA, power regulator).
    """
    fmin, fmax = band_mhz
    freq_plan = frequency_plan(fmin, fmax)
    array = design_array(num_jammers, freq_plan["center_freq_mhz"], diameter_mm, dielectric_constant, substrate_height_mm)
    n = array["n_elements"]

    print("\n### 1/6: Finding LNA ###")
    lna_text, lna_part = safe_find_and_recommend(
        token, search_term="GPS GNSS LNA amplifier",
        requirement=f"I need an LNA for a {n}-element GNSS antenna array covering {fmin}-{fmax} MHz, mounted outdoors, currently purchasable.",
        required_child_categories={"RF Amplifiers"},
    )

    print("\n### 2/6: Finding bandpass filter ###")
    filter_text, filter_part = safe_find_and_recommend(
        token, search_term="SAW filter GNSS",
        requirement=f"I need a SAW bandpass filter covering GPS L1, GLONASS, and BeiDou around {fmin}-{fmax} MHz, to reject out-of-band interference before the LNA/receiver stage. Currently purchasable, active status.",
        required_child_categories={"SAW Filters"},
    )

    print("\n### 3/6: Finding GNSS front-end chip ###")
    frontend_text, frontend_part = safe_find_and_recommend(
        token, search_term="MAX2769",
        requirement=f"I need a GNSS RF front-end/receiver chip covering GPS L1, GLONASS, and BeiDou around {fmin}-{fmax} MHz, currently purchasable.",
        required_child_categories={"RF Receivers"},
    )

    print("\n### 4/6: Finding TCXO reference clock ###")
    tcxo_text, tcxo_part = safe_find_and_recommend(
        token, search_term="TCXO oscillator GPS",
        requirement=f"I need a TCXO reference oscillator for a {n}-channel phase-coherent array (all channels must share this same clock). Currently purchasable, active status, a real oscillator component (not an evaluation board or GPSDO module).",
        required_child_categories={"Oscillators"},
    )

    print("\n### 5/6: Finding FPGA/digital processor ###")
    fpga_text, fpga_part = safe_find_and_recommend(
        token, search_term="XC7Z020",
        requirement=f"I need an FPGA/SoC to run real-time digital beamforming (adaptive nulling algorithms like MVDR or power-inversion) for a {n}-channel phase-coherent GNSS antenna array. Currently purchasable, active status, a bare chip (not a development board or embedded module).",
        required_child_categories={"Embedded"},
    )

    print("\n### 6/6: Finding power regulator ###")
    power_reg_text, power_reg_part = safe_find_and_recommend(
        token, search_term="LDO regulator 3.3V low noise",
        requirement="I need a low-noise linear voltage regulator (LDO) providing a clean 3.3V rail for RF components (an LNA and a GNSS receiver IC), low output noise is important since this feeds sensitive RF circuitry. Currently purchasable, active status.",
        required_child_categories={"Power Management (PMIC)"},
    )

    power = power_budget(n, extract_specs(lna_part)) if lna_part else None

    components = {
        "lna": {"recommendation": lna_text, "chosen_part": lna_part["ManufacturerProductNumber"] if lna_part else None},
        "bandpass_filter": {"recommendation": filter_text, "chosen_part": filter_part["ManufacturerProductNumber"] if filter_part else None},
        "gnss_frontend": {"recommendation": frontend_text, "chosen_part": frontend_part["ManufacturerProductNumber"] if frontend_part else None},
        "tcxo_clock": {"recommendation": tcxo_text, "chosen_part": tcxo_part["ManufacturerProductNumber"] if tcxo_part else None},
        "fpga_processor": {"recommendation": fpga_text, "chosen_part": fpga_part["ManufacturerProductNumber"] if fpga_part else None},
        "power_regulator": {"recommendation": power_reg_text, "chosen_part": power_reg_part["ManufacturerProductNumber"] if power_reg_part else None},
    }

    parts_map = {
        "Antenna Array": None, "LNA": lna_part, "Bandpass Filter": filter_part,
        "GNSS Front-End Receiver": frontend_part, "TCXO Reference Clock": tcxo_part,
        "Digital Beamforming Processor (FPGA)": fpga_part, "Power Regulator (LDO)": power_reg_part,
    }
    architecture = []
    for stage_name in SYSTEM_ARCHITECTURE_TEMPLATE:
        part = parts_map[stage_name]
        if stage_name == "Antenna Array":
            architecture.append({"stage": stage_name, "verified": True, "note": "Dimensions calculated via Balanis formula"})
        else:
            architecture.append({
                "stage": stage_name,
                "verified": bool(part),
                "note": part["ManufacturerProductNumber"] if part else "No candidate confirmed",
            })

    return {
        "1_antenna_dimensions": {
            "n_elements": n,
            "topology": f"1 center + {n-1} ring",
            "patch_width_mm": array["patch_width_mm"],
            "patch_length_mm": array["patch_length_mm"],
            "ring_radius_mm": array["ring_radius_mm"],
            "reason": f"N-1 rule: {num_jammers} jammers require at least {n} elements. Patch size via Balanis formula at {freq_plan['center_freq_mhz']} MHz, dielectric constant {dielectric_constant}.",
        },
        "2_powering": power if power else {"known": False, "note": "No LNA recommended, cannot calculate power."},
        "3_frequencies": {**freq_plan, "reason": f"Band chosen to cover required GNSS constellations within {fmin}-{fmax} MHz."},
        "4_chips_and_components": components,
        "system_architecture": architecture,
    }


# =========================================================
# PRINTING / MAIN
# =========================================================

def print_full_solution(result):
    print("\n" + "="*70)
    print("COMPLETE SYSTEM SOLUTION")
    print("="*70)

    d = result["1_antenna_dimensions"]
    print(f"\n1. ANTENNA DIMENSIONS")
    print(f"   {d['n_elements']} elements ({d['topology']}), patch {d['patch_width_mm']}x{d['patch_length_mm']}mm")
    print(f"   Why: {d['reason']}")

    p = result["2_powering"]
    print(f"\n2. POWERING")
    if p.get("known"):
        print(f"   Estimated total system power: {p['estimated_total_system_power_w']}W")
    else:
        print(f"   {p.get('note')}")

    f = result["3_frequencies"]
    print(f"\n3. FREQUENCIES: {f['band_low_mhz']}-{f['band_high_mhz']} MHz (center {f['center_freq_mhz']} MHz)")

    print(f"\n4. CHIPS AND COMPONENTS")
    for name, info in result["4_chips_and_components"].items():
        print(f"\n   [{name.upper()}] -> {info['chosen_part']}")

    print(f"\nSYSTEM ARCHITECTURE:")
    for stage in result["system_architecture"]:
        tag = "[VERIFIED]" if stage["verified"] else "[NOT CONFIRMED]"
        print(f"   {tag} {stage['stage']}: {stage['note']}")


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

    print_full_solution(result)


if __name__ == "__main__":
    main()