from __future__ import annotations

import json, secrets, functools
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional, List

import requests
from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from jose import jwt, JWTError
from pydantic import BaseModel, Field
from asterisk.ami import AMIClient, SimpleAction
import io, zipfile, xml.etree.ElementTree as ET
import pandas as pd
import re

# --------------------------- Config ------------------------------------
CONFIG_FILE = Path(__file__).with_name("config.json")
if not CONFIG_FILE.exists():
    raise FileNotFoundError("config.json missing.")
config: Dict = json.loads(CONFIG_FILE.read_text())

ast_cfg = config.get("asterisk", {})
api_cfg = config.get("api", {})

AST_USERNAME = ast_cfg.get("username")
AST_PASSWORD = ast_cfg.get("password")
AST_ADDRESS = ast_cfg.get("address")
AST_PORT = ast_cfg.get("port")

HOME_NODE: Optional[int] = None

ALLOWED_USERS: Dict[str, str] = api_cfg.get("users", {
    api_cfg.get("username", "admin"): api_cfg.get("password", "change_me")
})

# --------------------------- JWT ---------------------------------------------
SECRET_KEY = secrets.token_hex(32)
ALGORITHM  = "HS256"
ACCESS_TOKEN_EXPIRE_MIN = 60

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/token")

class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"

class User(BaseModel):
    username: str

def authenticate(u: str, p: str) -> Optional[User]:
    if u in ALLOWED_USERS and secrets.compare_digest(p, ALLOWED_USERS[u]):
        return User(username=u)
    return None

def create_token(username: str) -> str:
    exp = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MIN)
    return jwt.encode({"sub": username, "exp": exp}, SECRET_KEY, algorithm=ALGORITHM)

async def current_user(token: str = Depends(oauth2_scheme)) -> User:
    cred_exc = HTTPException(401, "Invalid credentials", headers={"WWW-Authenticate": "Bearer"})
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None or username not in ALLOWED_USERS:
            raise cred_exc
    except JWTError:
        raise cred_exc
    return User(username=username)

# --------------------------- FastAPI app -------------------------------------
app = FastAPI(title="AllStarLink API", version="3.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

app.mount("/map", StaticFiles(directory="map", html=True), name="map")


@app.post("/auth/token", response_model=Token, tags=["auth"])
async def login(form: OAuth2PasswordRequestForm = Depends()):
    user = authenticate(form.username, form.password)
    if not user:
        raise HTTPException(400, "Incorrect username or password")
    return Token(access_token=create_token(user.username))

@app.get("/auth/me", response_model=User, tags=["auth"])
async def me(u: User = Depends(current_user)):
    return u

# --------------------------- Asterisk helpers --------------------------------

def asterisk_cmd(cmd: str) -> str:
    cli = AMIClient(address=AST_ADDRESS, port=AST_PORT)
    try:
        cli.login(username=AST_USERNAME, secret=AST_PASSWORD)
        res = cli.send_action(SimpleAction("Command", Command=cmd))
        return res.response.keys.get("Output", "")
    finally:
        cli.logoff()

# --------------------------- Parser connections ------------------------------

def parse_local_connections(home_id: int) -> List[Dict]:
    conns: List[Dict] = []
    raw = asterisk_cmd(f"rpt xnode {home_id}")
    if raw == "" or raw == -1:
        return conns

    lines = raw.splitlines()
    if not lines:
        return conns

    nodes = lines[0].split('~')[:-1]
    if not nodes:
        return conns

    nodes_status: List[str] = []
    for ln in lines:
        if "RPT_ALINKS" in ln:
            nodes_status = ln.split(",")[1:]
            break

    raw_echo_cache = None

    for node in nodes:
        node_id = node[0:10].rstrip()
        node_ip = node[10:25].rstrip()
        node_dir = node[42:45].rstrip()
        node_uptime = node[53:61].rstrip()
        node_desc = ''
        node_mode = ''
        node_stat = ''
        for st in nodes_status:
            if node_id in st:
                if st[-2] == 'R':
                    node_mode = 'Monitor'
                if st[-2] == 'T':
                    node_mode = 'Connect'
                if st[-1] == 'U':
                    node_stat = 'Idle'
                if st[-1] == 'K':
                    node_stat = 'Transmitting'
    
        if node_id.startswith('3'):
            if raw_echo_cache is None:
                raw_echo_cache = asterisk_cmd("echolink show nodes")
            if raw_echo_cache != -1 and raw_echo_cache:
                nodes_echo = raw_echo_cache.splitlines()[1:]
                for node_echo in nodes_echo:
                    parts = node_echo.split()
                    if len(parts) >= 4 and node_id[1:] in parts[0]:
                        node_desc = f"Echolink - {parts[1]} - {parts[3]}"
                        node_ip = parts[2]
                        break
        try:
            node_int = int(node_id)
        except ValueError:
            continue
        if not node_mode and not node_stat:
            node_mode = 'Muted'
        conns.append({
            "node": node_int,
            "direction": node_dir,
            "uptime": node_uptime,
            "ip": node_ip,
            "desc": node_desc,
            "mode": node_mode,
            "status": node_stat,
        })
    return conns

# ------------------------- Parser Local Nodeslist ----------------------------
def parser_local_nodelist() -> List[int]:
    nodes: List[Dict] = []
    raw = asterisk_cmd("rpt localnodes")
    
    nodes = [int(x) for x in re.findall(r'\d+', raw)]
    return nodes

# --------------------------- Endpoints Asterisk ------------------------------
class CLICommand(BaseModel):
    command: str

@app.post("/asterisk/command", tags=["asterisk"], summary="CLI command")
async def cmd(data: CLICommand, _: User = Depends(current_user)):
    return {"output": asterisk_cmd(data.command)}

class LinkAction(BaseModel):
    action: str = Field(..., pattern="^(monitor|connect|muted|disconnect)$")

_CODES = {"monitor": 2, "connect": 3, "muted": 4, "disconnect": 1}

@app.post("/asterisk/link/{node}", tags=["asterisk"])
async def link(node: int, act: LinkAction, _: User = Depends(current_user)):
    if HOME_NODE is None:
        raise HTTPException(400, "HOME_NODE unset. Use POST /asterisk/home.")
    return {"output": asterisk_cmd(f"rpt cmd {HOME_NODE} ilink {_CODES[act.action]} {node}")}

@app.get("/asterisk/localnodes", tags=["asterisk"])
async def localnodes(_: User = Depends(current_user)):
    return {"localnodes": parser_local_nodelist()}

@app.get("/asterisk/connections", tags=["asterisk"], summary="Local connections (rpt xnode)")
async def connections(_: User = Depends(current_user)):
    if HOME_NODE is None:
        raise HTTPException(400, "HOME_NODE unset. Use POST /asterisk/home.")
    return {"home_node": HOME_NODE, "connections": parse_local_connections(HOME_NODE)}

# ----------- Home node endpoints --------------------------------------------
class HomeNodeBody(BaseModel):
    node: int = Field(..., ge=1)

@app.get("/asterisk/home", tags=["asterisk"])
async def get_home(_: User = Depends(current_user)):
    return {"home_node": HOME_NODE}

@app.post("/asterisk/home", tags=["asterisk"])
async def set_home(body: HomeNodeBody, _: User = Depends(current_user)):
    global HOME_NODE
    HOME_NODE = body.node
    return {"home_node": HOME_NODE}

# ------------- Allstarlink Nodes info ---------------------------------------
@functools.lru_cache
def _nodes_df():
    nl = requests.post("https://stats.allstarlink.org/api/stats/nodeList", headers={"Accept":"application/json","Content-Type":"application/x-www-form-urlencoded","X-Requested-With":"XMLHttpRequest"}, timeout=30).json()["data"]
    df = pd.DataFrame(nl, columns=["Node Link","Bubble Link","Node ID","Freq","Tone","Location","Country","Site Name","Affiliation","Connections"])
    df["Connections"] = pd.to_numeric(df["Connections"], errors="coerce").fillna(0).astype(int)
    df["Node"] = ("https://stats.allstarlink.org"+df["Node Link"].str.extract(r'href="([^"]+)"',expand=False)).str.extract(r"/stats/(\d+)$",expand=False).astype(int)
    df = df[["Node","Node ID","Freq","Tone","Connections","Location","Country","Site Name","Affiliation"]]
    df = df.rename(columns={'Node ID': 'Call Sign'})
    mp = requests.get("https://stats.allstarlink.org/api/stats/mapData", timeout=30).text.splitlines()
    mp_df = pd.DataFrame([r.split("\t") for r in mp], columns=["Node","Call","Lat","Lon","Loc","Site","Tone","Unknown"])
    mp_df["Node"] = mp_df["Node"].astype(int)
    mp_df["Lat"] = pd.to_numeric(mp_df["Lat"], errors="coerce")
    mp_df["Lon"] = pd.to_numeric(mp_df["Lon"], errors="coerce")
    mp_df = mp_df[mp_df["Lat"].between(-90,90) & mp_df["Lon"].between(-180,180)]
    return df.merge(mp_df[["Node","Lat","Lon"]], on="Node").drop_duplicates("Node")

# --------------- Echolink Nodes info ---------------------------------------
@functools.lru_cache               # mirrors the behaviour of _nodes_df()
def _echolink_df() -> pd.DataFrame:
    url = "http://www.echolink.org/node_location.kmz"
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()

    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf, zf.open("doc.kml") as f:
        tree = ET.parse(f)

    root = tree.getroot()
    ns = {"kml": "http://earth.google.com/kml/2.0"}
    pattern = re.compile(r'^(.*?)\s*\(([^)]+)\)\s+(\d+)$')

    records = []
    for pm in root.findall(".//kml:Placemark", ns):
        name_el = pm.find("kml:name", ns)
        desc_el = pm.find("kml:description", ns)
        coords_el = pm.find(".//kml:coordinates", ns)
        if coords_el is None or not coords_el.text:
            continue

        desc_text = (desc_el.text or "").strip()
        m = pattern.match(desc_text)
        if m:
            desc_base, freq_str, node_str = m.groups()
            try:
                freq = float(freq_str)
            except ValueError:
                freq = None
            try:
                node = int(node_str)
            except ValueError:
                node = None
        else:
            desc_base, freq, node = desc_text, None, None

        lon, lat, *_ = coords_el.text.strip().split(",")
        try:
            lat = float(lat)
            lon = float(lon)
        except ValueError:
            continue

        records.append({
            "Node":      node,
            "Call Sign": name_el.text.strip() if name_el is not None else None,
            "Desc":      desc_base,
            "Freq":      freq,
            "Lat":       lat,
            "Lon":       lon,
        })

    return pd.DataFrame(records).dropna(subset=["Lat", "Lon"])


@app.get("/echolinknodes", tags=["nodes"], summary="EchoLink nodes")
async def echolinknodes():
    return _echolink_df().to_dict(orient="records")

@app.get("/nodes", tags=["nodes"])
async def nodes():
    return _nodes_df().to_dict(orient="records")

@app.get("/nodes/{node}/links", tags=["nodes"])
async def node_links(node: int):
    try:
        links = requests.get(f"https://stats.allstarlink.org/api/stats/{node}", timeout=30).json()["stats"]["data"].get("links", [])
    except Exception:
        links = []
    return {"node": node, "links": links}

# --------------------------- Entrypoint --------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8501, log_level="info", access_log=False)

