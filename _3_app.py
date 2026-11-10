import json
import geopandas as gpd
import numpy as np
import plotly.express as px
import plotly.graph_objects as go
import pydeck as pdk
import streamlit as st
from _1_data_collector import build_master_dataset

st.set_page_config(page_title="2026 House Results Dashboard", layout="wide")

PARTY_COLORS = {"DEM": "#2E5B88", "REP": "#D23B33", "OTHER": "#808080"}

# Shift map settings (match the notebook's map 3)
SHIFT_LIMIT = 0.15                     # colours saturate at a 15-point swing
SHIFT_SCALE = [[0.0, "#D23B33"], [0.5, "#F7F7F7"], [1.0, "#2E5B88"]]
REDRAWN_COLOR = "#E3E3E3"              # lines redrawn for 2026
NOT_COMPARABLE_COLOR = "#BDBDBD"       # uncontested / missing in either year


# --- CACHED DATA LOAD WITH REFRESH HANDLER ---
@st.cache_data(ttl=60)
def load_data():
    gdf = gpd.read_file("data/election_2026_master.geojson")
    # Same filter as the notebook: 50 states + DC, no territories
    us_state_fips = [str(i).zfill(2) for i in range(1, 57) if i not in [3, 7, 14, 43, 52]]
    gdf = gdf[gdf["STATEFP"].isin(us_state_fips)].copy()
    # Older master files stored is_called as the strings "True"/"False"
    gdf["is_called"] = gdf["is_called"].astype(str).str.lower().eq("true")
    return gdf


@st.cache_data(ttl=60)
def to_geojson(_gdf, cache_key):
    """GeoJSON dict for Plotly, built once per data load instead of once per tab."""
    return json.loads(_gdf[["GEOID", "geometry"]].to_json())


def shift_label(v):
    if np.isnan(v):
        return ""
    if abs(v) < 0.0005:
        return "no change"
    return f"{'D' if v > 0 else 'R'}+{abs(v) * 100:.1f} pts"


col_title, col_btn = st.columns([4, 1])
with col_title:
    st.title("🗳️ 2026 U.S. House Election Dashboard")
with col_btn:
    st.write("")
    if st.button("🔄 Sync Live CNN Data"):
        build_master_dataset()
        load_data.clear()
        to_geojson.clear()
        st.rerun()

gdf = load_data()
geojson = to_geojson(gdf, cache_key=len(gdf))

# --- KPI SCORECARDS ---
seats = gdf[~gdf["GEOID"].str.endswith("98")]   # 435 voting seats (drops DC's non-voting delegate)
called_races = seats[seats["is_called"]]
dem_seats = len(called_races[called_races["winner_party"] == "DEM"])
gop_seats = len(called_races[called_races["winner_party"] == "REP"])
uncalled_seats = int((~seats["is_called"]).sum())

m1, m2, m3, m4 = st.columns(4)
m1.metric("Called Democratic Seats", dem_seats)
m2.metric("Called Republican Seats", gop_seats)
m3.metric("Uncalled / Pending Races", uncalled_seats)
m4.metric("Total Votes Counted", f"{int(gdf['total_votes'].sum()):,}")

st.divider()

# --- MAP TABS ---
tab1, tab2, tab3, tab4 = st.tabs([
    "1. Binary Winner Map",
    "2. Margin Gradient Map",
    "3. Shift from 2024 to 2026",
    "4. 3D Voter Turnout Map",
])

# TAB 1: BINARY WINNER MAP
with tab1:
    st.subheader("Binary Party Lead / Winner Map")
    fig1 = px.choropleth(
        gdf,
        geojson=geojson,
        locations="GEOID",
        featureidkey="properties.GEOID",
        color="winner_party",
        color_discrete_map=PARTY_COLORS,
        scope="usa",
        hover_name="race_title",
        hover_data={"GEOID": False, "total_votes": ":,", "editorial_status": True}
    )
    fig1.update_layout(margin={"r": 0, "t": 0, "l": 0, "b": 0}, height=600)
    st.plotly_chart(fig1, width="stretch")

# TAB 2: MARGIN GRADIENT MAP
with tab2:
    st.subheader("Race Competitiveness (Margin of Victory)")
    fig2 = px.choropleth(
        gdf,
        geojson=geojson,
        locations="GEOID",
        featureidkey="properties.GEOID",
        color="margin",
        color_continuous_scale="RdBu",
        range_color=[-0.4, 0.4],
        scope="usa",
        hover_name="race_title",
        hover_data={"GEOID": False, "margin": ":.2%", "total_votes": ":,"}
    )
    fig2.update_layout(margin={"r": 0, "t": 0, "l": 0, "b": 0}, height=600)
    st.plotly_chart(fig2, width="stretch")

# TAB 3: SHIFT 2024 TO 2026 GRADIENT MAP
with tab3:
    st.subheader("Shift in Two-Party Margin, 2024 → 2026")

    if "shift_status" not in gdf.columns:
        st.warning("No 2024 baseline in the dataset yet. Click **Sync Live CNN Data** to build it.")
    else:
        status = gdf["shift_status"]
        ok = gdf[status == "ok"].copy()
        redrawn = gdf[status == "redrawn"]
        not_comp = gdf[status == "not_comparable"]
        ok["shift_text"] = ok["shift_2p"].apply(shift_label)

        def flat_layer(df, color, note):
            """A single-colour choropleth layer for districts without a comparable shift."""
            return go.Choropleth(
                geojson=geojson,
                featureidkey="properties.GEOID",
                locations=df["GEOID"],
                z=np.zeros(len(df)),
                colorscale=[[0, color], [1, color]],
                showscale=False,
                marker_line_color="#8C8C8C",
                marker_line_width=0.3,
                text=df["race_title"],
                hovertemplate="<b>%{text}</b><br>" + note + "<extra></extra>",
            )

        fig3 = go.Figure([
            flat_layer(redrawn, REDRAWN_COLOR, "Lines redrawn for 2026: not comparable"),
            flat_layer(not_comp, NOT_COMPARABLE_COLOR, "Uncontested or missing in 2024/2026"),
            go.Choropleth(
                geojson=geojson,
                featureidkey="properties.GEOID",
                locations=ok["GEOID"],
                z=ok["shift_2p"],
                zmin=-SHIFT_LIMIT,
                zmax=SHIFT_LIMIT,
                colorscale=SHIFT_SCALE,
                marker_line_color="black",
                marker_line_width=0.3,
                customdata=np.stack([
                    ok["race_title"], ok["shift_text"],
                    ok["margin_2p_2024"] * 100, ok["margin_2p"] * 100,
                ], axis=-1),
                hovertemplate=("<b>%{customdata[0]}</b><br>"
                               "Shift: %{customdata[1]}<br>"
                               "2024 margin: %{customdata[2]:+.1f}<br>"
                               "2026 margin: %{customdata[3]:+.1f}"
                               "<extra></extra>"),
                colorbar=dict(
                    title="Shift",
                    orientation="h", y=-0.02, yanchor="top", len=0.6, thickness=14,
                    tickvals=[-0.15, -0.10, -0.05, 0, 0.05, 0.10, 0.15],
                    ticktext=["R+15", "R+10", "R+5", "0", "D+5", "D+10", "D+15"],
                ),
            ),
        ])
        fig3.update_geos(scope="usa", visible=False)
        fig3.update_layout(margin={"r": 0, "t": 0, "l": 0, "b": 0}, height=650)
        st.plotly_chart(fig3, width="stretch")

        c1, c2, c3 = st.columns(3)
        c1.metric("Comparable districts", len(ok))
        c2.metric("Median shift", shift_label(ok["shift_2p"].median()) or "n/a")
        c3.metric("Moved toward DEM / GOP",
                  f"{(ok['shift_2p'] > 0).sum()} / {(ok['shift_2p'] < 0).sum()}")
        st.caption(
            "Shift = change in each district's two-party margin, (D − R) / (D + R), from 2024 to 2026. "
            "Blue = moved toward Democrats, red = toward Republicans, white = little change; colours "
            f"saturate at {SHIFT_LIMIT * 100:.0f} points. "
            f"Light gray ({len(redrawn)} districts): state redrew its lines for 2026, so the same "
            "district number covers different voters. "
            f"Darker gray ({len(not_comp)}): one party had no candidate or no reported votes in either year."
        )

# TAB 4: 3D EXTRUDED TURNOUT MAP
with tab4:
    st.subheader("3D District Extrusion by Total Voter Turnout")

    def assign_rgb(row):
        if row.get("winner_party") == "DEM":
            return [46, 91, 136, 220]
        elif row.get("winner_party") == "REP":
            return [210, 59, 51, 220]
        return [128, 128, 128, 200]

    gdf3d = gdf.copy()
    gdf3d["fill_color"] = gdf3d.apply(assign_rgb, axis=1)

    pydeck_layer = pdk.Layer(
        "GeoJsonLayer",
        data=json.loads(gdf3d.to_json()),
        extruded=True,
        wireframe=True,
        get_elevation="properties.total_votes",
        elevation_scale=0.15,
        get_fill_color="properties.fill_color",
        get_line_color=[255, 255, 255, 100],
        pickable=True
    )

    view_state = pdk.ViewState(latitude=37.5, longitude=-96.0, zoom=3.8, pitch=45)

    deck = pdk.Deck(
        layers=[pydeck_layer],
        initial_view_state=view_state,
        tooltip={"text": "District: {race_title}\nTotal Votes: {total_votes}\nStatus: {editorial_status}"}
    )

    st.pydeck_chart(deck)
