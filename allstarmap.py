import json, re, requests, pandas as pd, dash, dash_leaflet as dl
from dash import html, Output, Input, ALL, dcc
from asterisk.ami import AMIClient, SimpleAction
import time

# ----------------------------------------------------------------------------
# Config – read conf file that must be
# username = <YOUR USERNAME OF ASTERISK>
# password = <YOUR PASSOWRD OF ASTERISK>
# node = <YOUR NODE NUMBER>
# ----------------------------------------------------------------------------
asterisk_conf = {}
with open("allstarmap.conf") as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            asterisk_conf[k.strip()] = v.strip()

home_id = int(asterisk_conf.get("node", 0))

# ----------------------------------------------------------------------------
# Function to send and receive from Asterisk
# ----------------------------------------------------------------------------
def asterisk_command(cmd):
    client = AMIClient(address="127.0.0.1", port=5038)
    resp = client.login(username=asterisk_conf["username"], secret=asterisk_conf["password"])
    if resp.response.is_error():
        return -1
    action = SimpleAction('Command', Command=cmd)
    resp = client.send_action(action)
    if resp.response.is_error():
        return -1
    return resp.response.keys['Output']
    client.logoff()

# ----------------------------------------------------------------------------
# Function to get the connections from asterisk
# ----------------------------------------------------------------------------
def get_local_connections():
    conns = []
   
    raw = asterisk_command(f"rpt xnode {home_id}")
    if raw == -1:
        return conns

    lines = raw.splitlines()
    nodes = lines[0].split('~')[:-1]
    if len(nodes) == 0:
        return conns
    for i in lines:
        if "RPT_ALINKS" in i:
            nodes_status = i.split(",")[1:]
            break
    for node in nodes:
        node_id = node[0:10].rstrip()
        node_ip = node[10:25].rstrip()
        node_dir = node[42:45].rstrip()
        node_uptime = node[53:61].rstrip()
        node_desc = ''
        node_mode = ''
        node_stat = ''
        for i in nodes_status:
            if node_id in i:
                if i[-2] == 'R':
                    node_mode = 'Monitor'
                if i[-2] == 'T':
                    node_mode = 'Transceiver'
                if i[-1] == 'U':
                    node_stat = 'Idle'
                if i[-1] == 'K':
                    node_stat = 'Transmit'
        if node_id[0] == '3': # is echolink
            raw_echo = asterisk_command(f"echolink show nodes")
            if raw_echo == -1:
                continue
            nodes_echo = raw_echo.splitlines()[1:]
            for node_echo in nodes_echo:
                if node_id[1:] in node_echo.split()[0]:
                    node_desc = 'Echolink - '+node_echo.split()[1]+' - '+node_echo.split()[3]
                    node_ip = node_echo.split()[2]
        node_id = int(node_id)

        conns.append(
            dict(node=node_id, direction=node_dir, uptime=node_uptime, ip=node_ip, desc=node_desc, mode=node_mode, status=node_stat)
        )

    return conns


# ---------------------------------------------------------------------------
# Function to get the status of a remote node
# ---------------------------------------------------------------------------
def get_node(node_id: int):
    """Restituisce il JSON del nodo remoto oppure None."""
    try:
        resp = requests.get(
            f"https://stats.allstarlink.org/api/stats/{node_id}", timeout=30
        )
        if resp.ok:
            return resp.json()
    except requests.RequestException:
        pass
    return None

# ----------------------------------------------------------------------------
# Function to get the list of nodes with coordinates
# ----------------------------------------------------------------------------
nodes_raw = requests.post(
    "https://stats.allstarlink.org/api/stats/nodeList",
    headers={
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
        "X-Requested-With": "XMLHttpRequest",
    },
    timeout=30,
).json()["data"]

nodes_df = pd.DataFrame(
    nodes_raw,
    columns=[
        "Node Link",
        "Bubble Link",
        "Node ID",
        "Freq",
        "Tone",
        "Location",
        "Country",
        "Site Name",
        "Affiliation",
        "Connections",
    ],
)
nodes_df["Connections"] = (
    pd.to_numeric(nodes_df["Connections"], errors="coerce").fillna(0).astype(int)
)
nodes_df["Node"] = (
    "https://stats.allstarlink.org"
    + nodes_df["Node Link"].str.extract(r'href="([^"]+)"', expand=False)
).str.extract(r"/stats/(\d+)$", expand=False).astype(int)
nodes_df = nodes_df[["Node", "Connections", "Location", "Node ID", "Freq"]]

rows = (
    requests.get("https://stats.allstarlink.org/api/stats/mapData", timeout=30)
    .text.strip()
    .split("\n")
)
map_df = pd.DataFrame(
    [r.split("\t") for r in rows],
    columns=[
        "Node",
        "Call",
        "Lat",
        "Lon",
        "Loc",
        "Site",
        "Tone",
        "Unknown",
    ],
)
map_df["Node"] = map_df["Node"].astype(int)
map_df["Lat"] = pd.to_numeric(map_df["Lat"], errors="coerce")
map_df["Lon"] = pd.to_numeric(map_df["Lon"], errors="coerce")

map_df = map_df[map_df["Lat"].between(-90, 90) & map_df["Lon"].between(-180, 180)]
nodes_df = nodes_df.merge(map_df[["Node", "Lat", "Lon"]], on="Node")
nodes_df = nodes_df.drop_duplicates(subset=["Node"])


coords = {int(r.Node): (r.Lat, r.Lon) for r in nodes_df.itertuples()}
desc_map = {
    int(r.Node): f"{r._4} {r.Freq} {r.Location}" for r in nodes_df.itertuples()
}

# ----------------------------------------------------------------------------
# Creation of markers for the map
# ----------------------------------------------------------------------------
max_conn = nodes_df["Connections"].max() or 1
radius = lambda c: 6 + c * 44 / max_conn
color = lambda c: f"rgb({255-int(255*c/max_conn)},{int(255*c/max_conn)},0)"

markers = [
    dl.CircleMarker(
        id={"type": "node", "index": r.Node},
        center=(r.Lat, r.Lon),
        radius=radius(r.Connections),
        color=None,
        fillColor=color(r.Connections),
        fillOpacity=0.5,
        stroke=False,
        children=[
            dl.Tooltip(f"Node: {r.Node}\nConn: {r.Connections}\n{r.Location}")
        ],
    )
    for r in nodes_df.itertuples()
    if r.Node != home_id
]

home_marker = []
if home_id in coords:
    home_row = nodes_df.loc[nodes_df["Node"] == home_id].iloc[0]
    conn_count = home_row["Connections"]
    location   = home_row["Location"]
    lat_home, lon_home = coords[home_id]

    home_marker = [
        dl.Marker(
            position=(lat_home, lon_home),
            id="home",
            children=[
                dl.Tooltip(
                    f"Node: {home_id}\nConn: {conn_count}\n{location}"
                )
            ],
        )
    ]
# ----------------------------------------------------------------------------
# Layout Dash
# ----------------------------------------------------------------------------
app = dash.Dash(__name__)
app.layout = html.Div(
    [
        html.H3("AllStarLink Nodes"),
        dl.Map(
            children=[
                dl.TileLayer(),
                dl.LayerGroup(home_marker + markers),
                dl.LayerGroup(id="links"),
            ],
            center=(20, 0),
            zoom=2,
            style={"height": "80vh", "width": "100%"},
        ),
        html.Div(id="panel"),  # ← pannello connessioni
        html.Div(id="disconnect-output", style={"display": "none"}),
    ]
)

# ----------------------------------------------------------------------------
# Callback for links and for the node home panel
# ----------------------------------------------------------------------------

def build_home_view():
    time.sleep(1)
    conns = get_local_connections()
    if not conns:
        return [], html.Div("No connections.", style={"padding": "10px"})

    header = html.Tr(
        [html.Th(h, style={"textAlign": "left"}) for h in
         ["Node", "Description", "Direction", "Connection Time", "IP Address", "Mode", "Status", "Disconnect"]]
    )
    rows = []
    for c in conns:
        descr = c["desc"] + desc_map.get(c["node"], "")
        rows.append(html.Tr([
            html.Td(c["node"], style={"textAlign": "left"}),
            html.Td(descr,  style={"textAlign": "left"}),
            html.Td(c["direction"], style={"textAlign": "left"}),
            html.Td(c["uptime"],    style={"textAlign": "left"}),
            html.Td(c["ip"],        style={"textAlign": "left"}),
            html.Td(c["mode"],      style={"textAlign": "left"}),
            html.Td(c["status"],    style={"textAlign": "left"}),
            html.Td(
                html.Button(
                    "✖",
                    id={"type": "disconnect", "index": c["node"]},
                    n_clicks=0,
                    style={
                        "color":"white","backgroundColor":"red",
                        "border":"none","borderRadius":"3px",
                        "cursor":"pointer","width":"24px","height":"24px",
                        "lineHeight":"14px"
                    }
                ),
                style={"textAlign": "left"},
            ),
        ]))
    table = html.Table([header] + rows, style={"width":"100%","margin":"10px"})

    polylines = []
    lat0, lon0 = coords.get(home_id, (None, None))
    for c in conns:
        lat1, lon1 = coords.get(c["node"], (None, None))
        if None not in (lat0, lon0, lat1, lon1):
            polylines.append(
                dl.Polyline(
                    positions=[(lat0, lon0), (lat1, lon1)],
                    color="blue", weight=2, opacity=0.6
                )
            )
    return polylines, table

@app.callback(
    Output("links", "children"),
    Output("panel", "children"),
    Input({"type": "node",       "index": ALL}, "n_clicks"),
    Input("home",                "n_clicks"),
    Input({"type": "disconnect", "index": ALL}, "n_clicks"),
    Input({"type": "monitor",    "index": ALL}, "n_clicks"),
    Input({"type": "connect",    "index": ALL}, "n_clicks"),
)
def handle_all_clicks(node_clicks, home_click, disconnect_clicks, monitor_clicks, connect_clicks):
    ctx = dash.callback_context
    if not ctx.triggered:
        return [], ""
    raw_id = ctx.triggered[0]["prop_id"].split(".")[0]
    try:
        trig = json.loads(raw_id)
        typ = trig.get("type")
        idx = trig.get("index")
    except (json.JSONDecodeError, TypeError):
        typ = raw_id
        idx = None

    if typ == "disconnect":
        asterisk_command(f"rpt cmd {home_id} ilink 1 {idx}")
        return build_home_view()

    if typ == "monitor":
        asterisk_command(f"rpt cmd {home_id} ilink 2 {idx}")
        return build_home_view()

    if typ == "connect":
        asterisk_command(f"rpt cmd {home_id} ilink 3 {idx}")
        return build_home_view()

    if typ == "home":
        return build_home_view()

    if typ == "node":
        node_id = idx
        data = get_node(node_id)

        polylines = []
        if data:
            lat0, lon0 = coords.get(node_id, (None, None))
            for ln in data.get("stats", {}).get("data", {}).get("links", []):
                try:
                    ln_id = int(ln)
                    lat1, lon1 = coords.get(ln_id, (None, None))
                    if None not in (lat0, lon0, lat1, lon1):
                        polylines.append(
                            dl.Polyline(
                                positions=[(lat0, lon0), (lat1, lon1)],
                                color="blue",
                                weight=2,
                                opacity=0.6
                            )
                        )
                except (ValueError, KeyError):
                    continue

        panel = html.Div(
            [
                html.Button(
                    "Monitor",
                    id={"type": "monitor", "index": node_id},
                    n_clicks=0,
                    style={
                        "marginRight": "10px",
                        "padding": "8px 16px",
                        "backgroundColor": "#007bff",
                        "color": "white",
                        "border": "none",
                        "borderRadius": "4px",
                        "cursor": "pointer"
                    },
                ),
                html.Button(
                    "Connect",
                    id={"type": "connect", "index": node_id},
                    n_clicks=0,
                    style={
                        "padding": "8px 16px",
                        "backgroundColor": "#28a745",
                        "color": "white",
                        "border": "none",
                        "borderRadius": "4px",
                        "cursor": "pointer"
                    },
                ),
            ],
            style={"padding": "10px"}
        )

        return polylines, panel

    return [], ""

# ----------------------------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8501, debug=False)
