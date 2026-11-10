import asyncio
import json
import os
import geopandas as gpd
import httpx
import numpy as np
import pandas as pd

os.makedirs("data", exist_ok=True)
os.makedirs("images", exist_ok=True)

SEATS_PER_STATE = {
    "AL": 7, "AK": 1, "AZ": 9, "AR": 4, "CA": 52, "CO": 8, "CT": 5, "DE": 1,
    "FL": 28, "GA": 14, "HI": 2, "ID": 2, "IL": 17, "IN": 9, "IA": 4, "KS": 4,
    "KY": 6, "LA": 6, "ME": 2, "MD": 8, "MA": 9, "MI": 13, "MN": 8, "MS": 4,
    "MO": 8, "MT": 2, "NE": 3, "NV": 4, "NH": 2, "NJ": 12, "NM": 3, "NY": 26,
    "NC": 14, "ND": 1, "OH": 15, "OK": 5, "OR": 6, "PA": 17, "RI": 2, "SC": 7,
    "SD": 1, "TN": 9, "TX": 38, "UT": 4, "VT": 1, "VA": 11, "WA": 10, "WV": 2,
    "WI": 8, "WY": 1
}

STATE_FIPS = {
    "AL": "01", "AK": "02", "AZ": "04", "AR": "05", "CA": "06", "CO": "08", "CT": "09", "DE": "10",
    "FL": "12", "GA": "13", "HI": "15", "ID": "16", "IL": "17", "IN": "18", "IA": "19", "KS": "20",
    "KY": "21", "LA": "22", "ME": "23", "MD": "24", "MA": "25", "MI": "26", "MN": "27", "MS": "28",
    "MO": "29", "MT": "30", "NE": "31", "NV": "32", "NH": "33", "NJ": "34", "NM": "35", "NY": "36",
    "NC": "37", "ND": "38", "OH": "39", "OK": "40", "OR": "41", "PA": "42", "RI": "44", "SC": "45",
    "SD": "46", "TN": "47", "TX": "48", "UT": "49", "VT": "50", "VA": "51", "WA": "53", "WV": "54",
    "WI": "55", "WY": "56",
}

CNN_API_URL = "https://politics.api.cnn.io/results/race/{year}-HG-{state}-{district}.json"
CENSUS_URL = "https://www2.census.gov/geo/tiger/GENZ2024/shp/cb_2024_us_cd119_500k.zip"
# 120th Congress (2026 election) districts; Census publishes these per state only.
TIGER_CD120_URL = "https://www2.census.gov/geo/tiger/TIGER2026/CD/tl_2026_{fips}_cd120.zip"
MASTER_FILE = "election_2026_master.geojson"

CURRENT_YEAR = 2026
BASELINE_YEAR = 2024
# 2024 results are final, so they are fetched once and cached here.
BASELINE_FILE = f"results_{BASELINE_YEAR}.csv"

# States whose congressional districts changed between the 2024 and 2026
# elections (Census Bureau, 120th Congress redistricting page). Missouri passed
# a new map but it was not allowed for 2026, so it is NOT in this list.
# District N in these states covers different territory in each year, so a
# district-level 2024 -> 2026 shift there compares different electorates.
REDRAWN_FOR_2026 = {"AL", "CA", "FL", "LA", "NC", "OH", "TN", "TX", "UT"}


def prepare_census_shapes():
    """District shapes for the 2026 election, in EPSG:4326.

    Starts from the Census cartographic boundary file for the 119th Congress
    (generalized, clipped to the shoreline) and swaps in the 120th Congress
    TIGER/Line districts for the states that redrew for 2026.
    """
    print("Downloading Census boundary shapefile...")
    gdf = gpd.read_file(CENSUS_URL)

    gdf["STATEFP"] = gdf["STATEFP"].astype(str).str.zfill(2)
    gdf["CDFP"] = gdf["CD119FP"].astype(str).str.zfill(2)
    gdf["GEOID"] = gdf["STATEFP"] + gdf["CDFP"]

    gdf = gdf.to_crs(epsg=4326)[["GEOID", "STATEFP", "CDFP", "geometry"]]
    return apply_2026_redistricting(gdf)


def apply_2026_redistricting(base, url_template=TIGER_CD120_URL, simplify_deg=0.001):
    """Replace the redrawn states' districts with the 120th Congress versions.

    TIGER/Line districts include water (lakes, bays, offshore areas), so each new
    district is clipped to its state's outline taken from `base`, which is already
    shoreline-clipped. Clipping to the same outline also makes the new states line
    up exactly with their unchanged neighbours. `simplify_deg` (~0.001 deg = ~100 m)
    keeps file size and plotting time down without any visible change at
    national scale.
    """
    redrawn_fips = sorted(STATE_FIPS[a] for a in REDRAWN_FOR_2026)
    outlines = base[base["STATEFP"].isin(redrawn_fips)].dissolve(by="STATEFP")["geometry"]

    new_parts = []
    for fips in redrawn_fips:
        abbr = next(a for a, f in STATE_FIPS.items() if f == fips)
        print(f"Downloading 120th Congress districts for {abbr}...")
        st = gpd.read_file(url_template.format(fips=fips)).to_crs(epsg=4326)

        cd_col = next(c for c in st.columns if c.startswith("CD") and c.endswith("FP"))
        st["STATEFP"] = st["STATEFP"].astype(str).str.zfill(2)
        st["CDFP"] = st[cd_col].astype(str).str.zfill(2)
        st = st[st["CDFP"] != "ZZ"]                      # water-only "no district" areas
        st["GEOID"] = st["STATEFP"] + st["CDFP"]

        # Clip to the shoreline outline; buffer(0) drops stray line/point fragments
        # that an intersection can leave and repairs any invalid rings.
        st["geometry"] = st.geometry.intersection(outlines.loc[fips]).buffer(0)
        st["geometry"] = st.geometry.simplify(simplify_deg, preserve_topology=True)
        st = st[~st.geometry.is_empty]

        expected = SEATS_PER_STATE[abbr]
        if len(st) != expected or st["GEOID"].duplicated().any():
            raise ValueError(f"{abbr}: got {len(st)} districts from {cd_col}, expected {expected}")
        new_parts.append(st[["GEOID", "STATEFP", "CDFP", "geometry"]])

    kept = base[~base["STATEFP"].isin(redrawn_fips)]
    out = gpd.GeoDataFrame(pd.concat([kept, *new_parts], ignore_index=True), crs=base.crs)
    print(f"Swapped in 2026 districts for {len(redrawn_fips)} states "
          f"({sum(len(p) for p in new_parts)} districts).")
    return out


async def fetch_race(client, semaphore, state, district, year=CURRENT_YEAR):
    """Fetches single district from CNN API for the given election year."""
    url = CNN_API_URL.format(year=year, state=state, district=district)
    async with semaphore:
        try:
            res = await client.get(url, timeout=6.0)
            if res.status_code != 200:
                return None

            data = res.json()
            candidates = sorted(
                data.get("candidates", []),
                key=lambda c: c.get("voteNum", 0),
                reverse=True,
            )
            leader = candidates[0] if candidates else {}

            dem_votes = sum(c.get("voteNum", 0) for c in candidates if c.get("majorParty") == "DEM")
            gop_votes = sum(c.get("voteNum", 0) for c in candidates if c.get("majorParty") == "REP")
            total_votes = data.get("totalVote", 0)

            raw_winner = data.get("winnerBopPartyId")
            is_called = bool(raw_winner)
            leader_party = leader.get("majorParty", "OTHER")
            winner_party = raw_winner if is_called else leader_party

            if total_votes > 0:
                margin = (dem_votes - gop_votes) / total_votes
            else:
                margin = 0.8 if winner_party == "DEM" else (-0.8 if winner_party == "REP" else 0.0)

            fips = str(data.get("stateFipsCode")).zfill(2)
            # Census numbers at-large districts (AK, DE, ND, SD, VT, WY) "00",
            # while CNN calls them district 1.
            dist_str = "00" if SEATS_PER_STATE[state] == 1 else str(district).zfill(2)

            return {
                "GEOID": f"{fips}{dist_str}",
                "state": state,
                "district": district,
                "race_title": f"{state}-{district}",
                "editorial_status": data.get("editorialStatus", "uncalled"),
                "is_called": is_called,
                "leader_party": leader_party,
                "total_votes": total_votes,
                "winner_party": winner_party,
                "dem_votes": dem_votes,
                "gop_votes": gop_votes,
                "margin": margin,
                "uncontested": bool(data.get("uncontested", False)),
            }
        except Exception:
            return None


async def fetch_all_results(year=CURRENT_YEAR):
    semaphore = asyncio.Semaphore(25)
    tasks_args = [
        (state, d)
        for state, count in SEATS_PER_STATE.items()
        for d in range(1, count + 1)
    ]

    print(f"Fetching {year} results from CNN...")
    async with httpx.AsyncClient(http2=True) as client:
        tasks = [fetch_race(client, semaphore, st, dist, year) for st, dist in tasks_args]
        results = await asyncio.gather(*tasks)

    found = [r for r in results if r is not None]
    print(f"  {year}: got {len(found)} of {len(tasks_args)} races")
    if len(found) < 0.9 * len(tasks_args):
        print(f"  WARNING: {len(tasks_args) - len(found)} {year} races missing - check network/API access.")
    return pd.DataFrame(found)


def load_baseline_results(refresh=False):
    """2024 House results per district, from the local cache or (once) from CNN."""
    path = f"data/{BASELINE_FILE}"
    if os.path.exists(path) and not refresh:
        print(f"Using cached {BASELINE_YEAR} results from {path}")
        return pd.read_csv(path, dtype={"GEOID": str})
    df = asyncio.run(fetch_all_results(BASELINE_YEAR))
    df = df[["GEOID", "dem_votes", "gop_votes", "total_votes", "uncontested"]]
    df.to_csv(path, index=False)
    return df


def two_party_margin(dem, gop):
    """(D - R) / (D + R); NaN unless both parties were on the ballot with votes."""
    dem, gop = dem.astype(float), gop.astype(float)
    contested = (dem > 0) & (gop > 0)
    return ((dem - gop) / (dem + gop)).where(contested)


def add_shift_columns(merged, baseline):
    """Add 2024 baseline columns and the 2024 -> 2026 two-party margin shift.

    shift_2p > 0 means the district moved toward Democrats, < 0 toward Republicans.
    shift_status: "ok" | "redrawn" (lines changed) | "not_comparable" (uncontested,
    D-vs-D / R-vs-R, or missing in either year).
    """
    b = baseline.rename(columns={c: f"{c}_{BASELINE_YEAR}" for c in baseline.columns if c != "GEOID"})
    merged = merged.drop(columns=[c for c in b.columns if c != "GEOID" and c in merged.columns])
    merged = merged.merge(b, on="GEOID", how="left")

    for col in ("dem_votes", "gop_votes", f"dem_votes_{BASELINE_YEAR}", f"gop_votes_{BASELINE_YEAR}"):
        merged[col] = merged[col].fillna(0)

    merged["margin_2p"] = two_party_margin(merged["dem_votes"], merged["gop_votes"])
    merged[f"margin_2p_{BASELINE_YEAR}"] = two_party_margin(
        merged[f"dem_votes_{BASELINE_YEAR}"], merged[f"gop_votes_{BASELINE_YEAR}"]
    )
    merged["shift_2p"] = merged["margin_2p"] - merged[f"margin_2p_{BASELINE_YEAR}"]

    fips_to_abbr = {f: a for a, f in STATE_FIPS.items()}
    redrawn = merged["STATEFP"].map(fips_to_abbr).isin(REDRAWN_FOR_2026)
    merged["shift_status"] = np.where(
        redrawn, "redrawn", np.where(merged["shift_2p"].notna(), "ok", "not_comparable")
    )
    merged.loc[merged["shift_status"] != "ok", "shift_2p"] = np.nan
    return merged


def build_master_dataset():
    gdf_shapes = prepare_census_shapes()
    df_results = asyncio.run(fetch_all_results())

    merged = gdf_shapes.merge(df_results, on="GEOID", how="left")
    
    merged["winner_party"] = merged["winner_party"].fillna("OTHER")
    merged["leader_party"] = merged["leader_party"].fillna("OTHER")
    merged["total_votes"] = merged["total_votes"].fillna(0)
    merged["margin"] = merged["margin"].fillna(0.0)
    merged["editorial_status"] = merged["editorial_status"].fillna("uncalled")
    # Districts with no CNN result (DC, territories) leave NaN here; without this the
    # column is written to GeoJSON as the strings "True"/"False".
    merged["is_called"] = merged["is_called"].fillna(False).astype(bool)
    merged["race_title"] = merged["race_title"].fillna(merged["GEOID"])

    merged = add_shift_columns(merged, load_baseline_results())

    merged.to_file(f"data/{MASTER_FILE}", driver="GeoJSON")
    print(f"Master file 'data/{MASTER_FILE}' updated successfully!")
    return merged


if __name__ == "__main__":
    build_master_dataset()