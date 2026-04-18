from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Tuple

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, HttpUrl

APP_NAME = os.getenv("APP_NAME", "Land Scout")
APP_DOMAIN = os.getenv("APP_DOMAIN", "landscout.rrvconstruction.com")
APP_BRAND = os.getenv("APP_BRAND", "RRV Construction")
USER_AGENT = f"Mozilla/5.0 (compatible; {APP_NAME.replace(' ', '')}/1.2)"

ARCGIS_GEOCODE_URL = "https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/findAddressCandidates"
FEMA_FLOOD_LAYER_URL = (
    "https://services.arcgis.com/P3ePLMYs2RVChkJx/arcgis/rest/services/"
    "USA_Flood_Hazard_Reduced_Set_gdb/FeatureServer/0/query"
)
FEMA_VIEWER_URL = "https://msc.fema.gov/nfhl"

COUNTY_CONFIG = {
    "lake": {
        "label": "Lake County",
        "property_search_url": "https://www.lakecopropappr.com/property-search.aspx",
        "map_search_url": "https://gis.lakecountyfl.gov/gisweb/",
        "parcel_note": "Lake supports parcel number, address, AltKey, and map-style parcel research."
    },
    "orange": {
        "label": "Orange County",
        "property_search_url": "https://ocpaweb.ocpafl.org/",
        "map_search_url": "https://ocpaweb.ocpafl.org/parcelsearch",
        "parcel_note": "Orange supports parcel ID, address, and property search workflows."
    },
    "polk": {
        "label": "Polk County",
        "property_search_url": "https://www.polkpa.org/searches.html",
        "map_search_url": "https://map.polkpa.org/",
        "parcel_note": "Polk supports parcel-based property search and map lookup."
    },
    "marion": {
        "label": "Marion County",
        "property_search_url": "https://www.pa.marion.fl.us/",
        "map_search_url": "https://www.pa.marion.fl.us/Maps",
        "parcel_note": "Marion supports property search plus county parcel map review."
    },
    "volusia": {
        "label": "Volusia County",
        "property_search_url": "https://vcpa.vcgov.org/search",
        "map_search_url": "https://vcpa.vcgov.org/search/real-property",
        "parcel_note": "Volusia supports parcel ID, address, and real property search."
    },
    "sumter": {
        "label": "Sumter County",
        "property_search_url": "https://www.sumterpa.com/",
        "map_search_url": "https://qpublic.schneidercorp.com/Application.aspx?AppID=1207&LayerID=36374&PageTypeID=2&PageID=13870",
        "parcel_note": "Sumter supports parcel search plus GIS/qPublic map access."
    },
}

COUNTY_NAME_MAP = {
    "lake county": "lake",
    "orange county": "orange",
    "polk county": "polk",
    "marion county": "marion",
    "volusia county": "volusia",
    "sumter county": "sumter",
}

app = FastAPI(title=APP_NAME)

class ScanRequest(BaseModel):
    listing_url: HttpUrl

def dedupe_keep_order(items: List[str]) -> List[str]:
    seen = set()
    out = []
    for item in items:
        cleaned = re.sub(r"\s+", " ", str(item)).strip()
        if cleaned and cleaned.lower() not in seen:
            seen.add(cleaned.lower())
            out.append(cleaned)
    return out

def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()

def extract_meta_content(soup: BeautifulSoup, *names: str) -> List[str]:
    wanted = {n.lower() for n in names}
    values: List[str] = []
    for tag in soup.find_all("meta"):
        key = (tag.get("property") or tag.get("name") or "").strip().lower()
        if key in wanted:
            content = tag.get("content")
            if content:
                values.append(clean_text(content))
    return values

def extract_coordinates(html: str, soup: BeautifulSoup) -> List[Dict[str, float]]:
    coords: List[Dict[str, float]] = []
    meta_lat = extract_meta_content(soup, "place:location:latitude", "og:latitude")
    meta_lon = extract_meta_content(soup, "place:location:longitude", "og:longitude")
    if meta_lat and meta_lon:
        try:
            coords.append({"lat": float(meta_lat[0]), "lon": float(meta_lon[0]), "source": "meta"})
        except ValueError:
            pass
    patterns = [
        r'"latitude"\s*:\s*(-?\d+(?:\.\d+)?)\s*,\s*"longitude"\s*:\s*(-?\d+(?:\.\d+)?)',
        r'"lat"\s*:\s*(-?\d+(?:\.\d+)?)\s*,\s*"lng"\s*:\s*(-?\d+(?:\.\d+)?)',
        r'"lat"\s*:\s*(-?\d+(?:\.\d+)?)\s*,\s*"lon"\s*:\s*(-?\d+(?:\.\d+)?)',
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, html, re.IGNORECASE):
            try:
                coords.append({"lat": float(match.group(1)), "lon": float(match.group(2)), "source": "json"})
            except ValueError:
                continue
    unique = []
    seen = set()
    for c in coords:
        key = (round(c["lat"], 6), round(c["lon"], 6))
        if key not in seen:
            seen.add(key)
            unique.append(c)
    return unique

def extract_possible_addresses(text: str) -> List[str]:
    patterns = [
        r"\b\d{1,6}\s+[A-Za-z0-9.\-' ]+?,\s*[A-Za-z.\-' ]+?,\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?\b",
        r"\b\d{1,6}\s+[A-Za-z0-9.\-' ]+?,\s*[A-Za-z.\-' ]+?,\s*[A-Z]{2}\b",
    ]
    found = []
    for pattern in patterns:
        found.extend(m.group(0) for m in re.finditer(pattern, text))
    return dedupe_keep_order(found)[:20]

def extract_possible_parcel_ids(text: str) -> List[str]:
    patterns = [
        r"\b(?:parcel\s*(?:id|#|number)?|apn|folio|tax\s*id|alt\s*key)\s*[:#]?\s*([A-Za-z0-9\-.]{6,30})\b",
        r"\b\d{2,4}[A-Za-z]?\-\d{2,4}\-\d{2,6}\-\d{2,6}\b",
        r"\b\d{7,20}\b",
    ]
    values = []
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            candidate = match.group(1) if match.lastindex else match.group(0)
            candidate = candidate.strip(" .,:;")
            if len(candidate) >= 6:
                values.append(candidate)
    return dedupe_keep_order(values)[:20]

def extract_county_state_clues(text: str) -> Tuple[List[str], List[str]]:
    county_matches = re.findall(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\s+County\b", text)
    states = re.findall(r"\b([A-Z]{2})\b", text)
    whitelist = {
        "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","IA","ID","IL","IN","KS","KY","LA",
        "MA","MD","ME","MI","MN","MO","MS","MT","NC","ND","NE","NH","NJ","NM","NV","NY","OH","OK",
        "OR","PA","RI","SC","SD","TN","TX","UT","VA","VT","WA","WI","WV","WY","DC"
    }
    states = [s for s in states if s in whitelist]
    return dedupe_keep_order(county_matches)[:10], dedupe_keep_order(states)[:10]

def guess_county(text_blob: str, county_clues: List[str]) -> Optional[str]:
    text_lower = text_blob.lower()
    for clue in county_clues:
        phrase = f"{clue.lower()} county"
        if phrase in COUNTY_NAME_MAP:
            return COUNTY_NAME_MAP[phrase]
    for phrase, slug in COUNTY_NAME_MAP.items():
        if phrase in text_lower:
            return slug
    return None

async def fetch_listing_html(url: str) -> str:
    async with httpx.AsyncClient(timeout=25, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.text

def parse_listing(html: str, source_url: str) -> Dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    title = clean_text(soup.title.text) if soup.title and soup.title.text else None
    text_parts = []
    for field in ["og:title", "og:description", "description", "twitter:description"]:
        text_parts.extend(extract_meta_content(soup, field))
    text_parts.append(soup.get_text(" ", strip=True))
    full_text = clean_text(" ".join(text_parts))
    county_clues, state_clues = extract_county_state_clues(full_text)
    county_slug = guess_county(full_text, county_clues)
    return {
        "title": title,
        "listing_url": source_url,
        "addresses": extract_possible_addresses(full_text),
        "parcel_ids": extract_possible_parcel_ids(full_text),
        "coordinates": extract_coordinates(html, soup),
        "county_clues": county_clues,
        "state_clues": state_clues,
        "county_slug": county_slug,
    }

async def geocode_address(address: str) -> Optional[Dict[str, Any]]:
    params = {"SingleLine": address, "outFields": "Match_addr,Addr_type", "f": "json", "maxLocations": 1}
    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": USER_AGENT}) as client:
        resp = await client.get(ARCGIS_GEOCODE_URL, params=params)
        resp.raise_for_status()
        data = resp.json()
    candidates = data.get("candidates") or []
    if not candidates:
        return None
    best = candidates[0]
    return {
        "address": best.get("address"),
        "score": best.get("score"),
        "lon": (best.get("location") or {}).get("x"),
        "lat": (best.get("location") or {}).get("y"),
    }

async def query_fema_flood_zone(lon: float, lat: float) -> Optional[Dict[str, Any]]:
    params = {
        "f": "json",
        "geometry": f"{lon},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "FLD_ZONE,SFHA_TF,ZONE_SUBTY,DFIRM_ID,STATIC_BFE,V_DATUM",
        "returnGeometry": "false",
    }
    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": USER_AGENT}) as client:
        resp = await client.get(FEMA_FLOOD_LAYER_URL, params=params)
        resp.raise_for_status()
        data = resp.json()
    features = data.get("features") or []
    if not features:
        return None
    return features[0].get("attributes") or {}

@app.get("/", response_class=HTMLResponse)
async def home() -> str:
    return f'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>{APP_NAME}</title>
  <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover" />
  <meta name="theme-color" content="#0f1015" />
  <link rel="manifest" href="/manifest.webmanifest">
  <style>
    :root {{
      --bg:#0d0f14; --panel:#151823; --panel2:#1a1e2b; --gold:#d4af37; --text:#f4f5f7;
      --muted:#b7beca; --line:rgba(212,175,55,.2); --ok:#85e0aa; --warn:#ffb366; --bad:#ff7d7d;
    }}
    *{{box-sizing:border-box;-webkit-tap-highlight-color:transparent}}
    body{{margin:0;background:linear-gradient(180deg,#0a0c10 0%, #111523 100%);color:var(--text);font-family:Inter,Arial,sans-serif;}}
    .shell{{max-width:860px;margin:0 auto;padding:18px 14px 48px;}}
    .hero,.card{{background:linear-gradient(180deg, rgba(255,255,255,.03), rgba(255,255,255,.01));border:1px solid rgba(255,255,255,.08);border-radius:24px;box-shadow:0 10px 40px rgba(0,0,0,.25);}}
    .hero{{padding:18px;border-color:var(--line);}}
    .eyebrow{{color:var(--gold);text-transform:uppercase;font-size:12px;letter-spacing:.16em;font-weight:800;margin-bottom:8px;}}
    h1{{margin:0 0 8px;line-height:1.05;font-size:34px;}}
    .sub{{color:var(--muted);line-height:1.55;font-size:15px;margin-bottom:16px;}}
    .stack{{display:flex;flex-direction:column;gap:10px}}
    input,button{{width:100%;border-radius:16px;padding:15px 14px;font-size:16px;}}
    input{{background:#0e1119;color:var(--text);border:1px solid rgba(255,255,255,.1);outline:none;}}
    button{{border:none;font-weight:800;cursor:pointer;}}
    .btn-primary{{background:var(--gold);color:#111;}}
    .btn-secondary{{background:#23293a;color:var(--text);border:1px solid rgba(255,255,255,.08);}}
    .row{{display:grid;grid-template-columns:1fr 1fr;gap:10px}}
    .cards{{display:grid;grid-template-columns:1fr;gap:12px;margin-top:14px}}
    .card{{padding:16px}}
    .label{{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.09em;margin-bottom:6px;}}
    .value{{font-size:20px;font-weight:800;line-height:1.25;word-break:break-word;}}
    .small{{color:var(--muted);line-height:1.6;font-size:14px;margin-top:6px;word-break:break-word;}}
    .pill{{display:inline-block;padding:8px 12px;border-radius:999px;font-size:12px;font-weight:800;margin-top:10px;}}
    .ok{{background:rgba(133,224,170,.12);color:var(--ok);border:1px solid rgba(133,224,170,.2)}}
    .warn{{background:rgba(255,179,102,.12);color:var(--warn);border:1px solid rgba(255,179,102,.2)}}
    .bad{{background:rgba(255,125,125,.12);color:var(--bad);border:1px solid rgba(255,125,125,.2)}}
    .actions{{display:flex;gap:10px;flex-wrap:wrap;margin-top:10px}}
    a.action{{display:inline-block;text-decoration:none;color:var(--text);background:#202536;border:1px solid rgba(255,255,255,.08);padding:10px 12px;border-radius:14px;font-size:14px;}}
    ul{{margin:8px 0 0 18px;color:var(--muted);line-height:1.6;padding:0;}}
    .history-item{{padding:12px;border:1px solid rgba(255,255,255,.08);border-radius:14px;margin-top:8px;background:#111522;}}
    .footer{{text-align:center;color:var(--muted);font-size:12px;margin-top:14px;}}
    @media (max-width:680px){{ .row{{grid-template-columns:1fr}} h1{{font-size:30px}} }}
  </style>
</head>
<body>
  <div class="shell">
    <div class="hero">
      <div class="eyebrow">{APP_BRAND} • {APP_NAME}</div>
      <h1>Flood check land listings from your phone</h1>
      <div class="sub">
        Paste a land-for-sale link. {APP_NAME} scans the listing for hidden coordinates, address clues, parcel IDs, county hints, and checks FEMA flood data. Built around Lake, Orange, Polk, Marion, Volusia, and Sumter workflows.
      </div>
      <div class="stack">
        <input id="url" placeholder="Paste listing URL here…" />
        <div class="row">
          <button class="btn-primary" onclick="runScan()">Check flood zone</button>
          <button class="btn-secondary" onclick="installHelp()">Install on phone</button>
        </div>
      </div>
    </div>

    <div class="cards">
      <div class="card"><div class="label">Status</div><div id="status" class="value">Ready</div><div id="statusPill"></div></div>
      <div class="card">
        <div class="label">Result</div>
        <div id="floodZone" class="value">No result yet</div>
        <div id="floodMeta" class="small"></div>
        <div class="actions">
          <a class="action" id="femaLink" href="{FEMA_VIEWER_URL}" target="_blank" rel="noreferrer">Open FEMA viewer</a>
          <a class="action" id="countyLink" href="#" target="_blank" rel="noreferrer">Open county search</a>
          <a class="action" id="mapLink" href="#" target="_blank" rel="noreferrer">Open county map</a>
        </div>
      </div>
      <div class="card"><div class="label">Listing</div><div id="listingTitle" class="value">-</div><div id="listingUrl" class="small">-</div></div>
      <div class="card"><div class="label">Resolved address</div><div id="address" class="value">-</div></div>
      <div class="card"><div class="label">Coordinates used</div><div id="coords" class="value">-</div></div>
      <div class="card"><div class="label">County and parcel clues</div><div id="clues" class="small">-</div></div>
      <div class="card"><div class="label">Notes</div><ul id="notes"></ul></div>
      <div class="card"><div class="label">Saved checks on this device</div><div id="history"></div></div>
    </div>
    <div class="footer">Suggested domain: https://{APP_DOMAIN}</div>
  </div>

<script>
let deferredPrompt = null;
window.addEventListener('beforeinstallprompt', (e) => {{
  e.preventDefault();
  deferredPrompt = e;
}});
function installHelp(){{
  if (deferredPrompt) {{ deferredPrompt.prompt(); return; }}
  alert("iPhone: open this in Safari, tap Share, then Add to Home Screen. Android: open in Chrome and tap Install App or Add to Home Screen.");
}}
function setText(id, value){{ document.getElementById(id).textContent = value || "-"; }}
function setPill(kind, text){{ document.getElementById("statusPill").innerHTML = `<span class="pill ${kind}">${text}</span>`; }}
function setList(id, items){{
  const el = document.getElementById(id);
  el.innerHTML = "";
  const safeItems = items && items.length ? items : ["No notes available."];
  safeItems.forEach(item => {{ const li = document.createElement("li"); li.textContent = item; el.appendChild(li); }});
}}
function saveHistory(item){{
  const key = "land_scout_history";
  const data = JSON.parse(localStorage.getItem(key) || "[]");
  data.unshift(item);
  localStorage.setItem(key, JSON.stringify(data.slice(0, 15)));
  renderHistory();
}}
function renderHistory(){{
  const key = "land_scout_history";
  const data = JSON.parse(localStorage.getItem(key) || "[]");
  const el = document.getElementById("history");
  if (!data.length) {{ el.innerHTML = '<div class="small">No saved checks yet.</div>'; return; }}
  el.innerHTML = data.map(item => `
    <div class="history-item">
      <div style="font-weight:800">${item.zone || 'Unknown zone'}</div>
      <div class="small">${item.title || 'Untitled listing'}</div>
      <div class="small">${item.url}</div>
      <div class="small">${item.county || 'County unknown'} • ${item.when}</div>
    </div>`).join("");
}}
async function runScan(){{
  const url = document.getElementById("url").value.trim();
  if (!url) {{ alert("Paste a listing URL first."); return; }}
  setText("status", "Scanning listing...");
  setPill("warn", "Working");
  try {{
    const resp = await fetch("/scan", {{
      method: "POST",
      headers: {{"Content-Type":"application/json"}},
      body: JSON.stringify({{listing_url: url}})
    }});
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || "Scan failed");
    setText("listingTitle", data.source_title || "Untitled listing");
    setText("listingUrl", data.listing_url);
    setText("address", data.normalized_address || (data.raw_candidates.addresses || []).join(" | ") || "No address found");
    setText("coords", data.coordinates_used ? `${data.coordinates_used.lat}, ${data.coordinates_used.lon}` : "No coordinates resolved");
    setText("floodZone", data.flood_zone ? `FEMA Zone ${data.flood_zone}` : "No flood zone resolved");
    const metaBits = [];
    if (data.zone_subtype) metaBits.push(`Subtype: ${data.zone_subtype}`);
    if (typeof data.sfha === "boolean") metaBits.push(`SFHA: ${data.sfha}`);
    if (data.panel_id) metaBits.push(`Panel: ${data.panel_id}`);
    if (data.confidence) metaBits.push(`Confidence: ${data.confidence}`);
    setText("floodMeta", metaBits.join(" • ") || "No FEMA metadata returned");
    const parcelText = (data.parcel_ids_found || []).length ? `Parcel/APN: ${(data.parcel_ids_found || []).join(", ")}` : "Parcel/APN: none found";
    const countyText = data.county_label ? `County guess: ${data.county_label}` : "County guess: not detected";
    setText("clues", `${countyText} | ${parcelText}`);
    setList("notes", data.notes || []);
    document.getElementById("countyLink").href = data.county_property_search_url || "#";
    document.getElementById("mapLink").href = data.county_map_url || "#";
    if (data.success) {{ setText("status", "Scan complete"); setPill("ok", "Success"); }}
    else {{ setText("status", "Partial result"); setPill("warn", "Needs more data"); }}
    saveHistory({{
      url: data.listing_url,
      title: data.source_title || "Untitled listing",
      zone: data.flood_zone ? `Zone ${data.flood_zone}` : "Unresolved",
      county: data.county_label || "Unknown county",
      when: new Date().toLocaleString()
    }});
  }} catch (err) {{
    setText("status", "Error checking listing");
    setPill("bad", "Error");
    setList("notes", [String(err)]);
  }}
}}
if ('serviceWorker' in navigator) {{
  navigator.serviceWorker.register('/sw.js').catch(() => {{}});
}}
renderHistory();
</script>
</body>
</html>'''

@app.get("/manifest.webmanifest")
async def manifest() -> JSONResponse:
    return JSONResponse({
        "name": APP_NAME,
        "short_name": "Land Scout",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#0d0f14",
        "theme_color": "#0f1015",
        "description": "Florida land listing flood checker for field use.",
        "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"}]
    })

@app.get("/sw.js", response_class=HTMLResponse)
async def sw() -> HTMLResponse:
    js = '''
const CACHE_NAME = "land-scout-rrv-v1";
const URLS = ["/", "/manifest.webmanifest", "/icon.svg"];
self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE_NAME).then(cache => cache.addAll(URLS)));
});
self.addEventListener("fetch", (event) => {
  if (event.request.method !== "GET") return;
  event.respondWith(caches.match(event.request).then(cached => cached || fetch(event.request).catch(() => caches.match("/"))));
});
'''
    return HTMLResponse(js, media_type="application/javascript")

@app.get("/icon.svg", response_class=HTMLResponse)
async def icon() -> HTMLResponse:
    return HTMLResponse('''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256">
  <rect width="256" height="256" rx="48" fill="#0f1015"/>
  <path d="M42 174L128 56l86 118H42z" fill="#d4af37"/>
  <path d="M80 174h96v28H80z" fill="#ffffff"/>
  <circle cx="128" cy="126" r="24" fill="#0f1015"/>
</svg>''', media_type="image/svg+xml")

@app.post("/scan")
async def scan_listing(payload: ScanRequest) -> JSONResponse:
    notes: List[str] = []
    listing_url = str(payload.listing_url)
    try:
        html = await fetch_listing_html(listing_url)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not fetch listing URL: {exc}")
    extracted = parse_listing(html, listing_url)
    normalized_address = None
    coordinates_used = None
    confidence = "low"

    if extracted["coordinates"]:
        first = extracted["coordinates"][0]
        coordinates_used = {"lat": first["lat"], "lon": first["lon"]}
        confidence = "high"
        notes.append("Used coordinates embedded in the listing page.")
    elif extracted["addresses"]:
        geocoded = await geocode_address(extracted["addresses"][0])
        if geocoded and geocoded.get("lat") is not None and geocoded.get("lon") is not None:
            normalized_address = geocoded.get("address")
            coordinates_used = {"lat": geocoded["lat"], "lon": geocoded["lon"]}
            confidence = "medium"
            notes.append("No listing coordinates found, so the app geocoded the address.")
            if geocoded.get("score") is not None:
                notes.append(f"Geocode score: {geocoded['score']}")

    county_slug = extracted.get("county_slug")
    county_config = COUNTY_CONFIG.get(county_slug) if county_slug else None

    if not coordinates_used and extracted["parcel_ids"]:
        notes.append("Parcel/APN found. Next move is the county property appraiser or map link to pin down the exact parcel location.")
        if county_config:
            notes.append(county_config["parcel_note"])
    elif not coordinates_used:
        notes.append("No usable address or coordinates were found on the page.")

    flood = None
    if coordinates_used:
        flood = await query_fema_flood_zone(coordinates_used["lon"], coordinates_used["lat"])

    sfha = None
    flood_zone = None
    zone_subtype = None
    panel_id = None

    if flood:
        flood_zone = flood.get("FLD_ZONE")
        zone_subtype = flood.get("ZONE_SUBTY")
        panel_id = flood.get("DFIRM_ID")
        sfha_raw = str(flood.get("SFHA_TF")).upper().strip() if flood.get("SFHA_TF") is not None else ""
        if sfha_raw in {"T", "TRUE", "Y", "YES", "1"}:
            sfha = True
            notes.append("FEMA classifies this point inside a Special Flood Hazard Area.")
        elif sfha_raw:
            sfha = False
            notes.append("FEMA does not classify this point inside a Special Flood Hazard Area.")
    elif coordinates_used:
        notes.append("No FEMA flood polygon matched the resolved point.")

    if county_config:
        notes.append(f"County workflow prepared for {county_config['label']}.")

    return JSONResponse({
        "success": bool(flood_zone),
        "listing_url": listing_url,
        "source_title": extracted.get("title"),
        "normalized_address": normalized_address,
        "parcel_ids_found": extracted["parcel_ids"],
        "county_clues": extracted["county_clues"],
        "state_clues": extracted["state_clues"],
        "county_slug": county_slug,
        "county_label": county_config["label"] if county_config else None,
        "county_property_search_url": county_config["property_search_url"] if county_config else None,
        "county_map_url": county_config["map_search_url"] if county_config else None,
        "coordinates_used": coordinates_used,
        "flood_zone": flood_zone,
        "zone_subtype": zone_subtype,
        "sfha": sfha,
        "panel_id": panel_id,
        "confidence": confidence,
        "fema_viewer_url": FEMA_VIEWER_URL,
        "notes": notes,
        "raw_candidates": extracted,
    })
