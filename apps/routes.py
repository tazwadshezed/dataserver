from fastapi import APIRouter, Request, HTTPException, Form
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
import json
from .util.config import get_redis_conn, get_postgres_conn, load_config
from .util.logger import make_logger
from .util.faults import set_fault

router = APIRouter()
config = load_config()

templates = Jinja2Templates(directory="/home/zoot/wireless-sensor-mesh-daq/dataserver/apps/templates")

# Optional legacy dashboard route
@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse("sitedata/dashboard.html", {"request": request})

router = APIRouter()
logger = make_logger("Route")

def normalize_mac(raw):
    # convert string like '00000000000003E9' → int → hex → colon format
    try:
        value = int(raw, 16)
        hex_str = f"{value:012x}"  # force 12 hex digits, lowercased
        return ":".join(hex_str[i:i+2] for i in range(0, 12, 2))
    except ValueError:
        return "invalid"

@router.get("/sitearray/map/layout", response_class=JSONResponse)
async def get_panel_layout():
    try:
        pg = get_postgres_conn()
        with pg.cursor() as cur:
            cur.execute("""
                SELECT sg.json
                FROM ss.site_graph sg
                JOIN ss.site_array sa ON sg.site_array_id = sa.id
                JOIN ss.site s ON s.id = sa.site_id
                WHERE s.sitename = %s
            """, ('TEST',))
            result = cur.fetchone()
            if not result:
                raise HTTPException(status_code=404, detail="Site graph not found")

            graph_json = json.loads(result[0])
            layout = []

            def walk(node):
                if not isinstance(node, dict):
                    return

                # Find panel node and look for monitor child
                if node.get("devtype") == "P":
                    x = node.get("x")
                    y = node.get("y")
                    for child in node.get("inputs", []):
                        if child.get("devtype") == "M":
                            mac = child.get("macaddr", "").lower()
                            if mac and x is not None and y is not None:
                                layout.append({"mac": mac, "x": x, "y": y})
                for child in node.get("inputs", []):
                    walk(child)

            sitearray = graph_json.get("sitearray")
            if sitearray:
                walk(sitearray)

            return layout

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error: {e}")


@router.get("/sitearray/map/status", response_class=JSONResponse)
async def sitearray_map_status():
    try:
        redis_conn = get_redis_conn(db=1)
        pg = get_postgres_conn()

        # Load site graph layout from postgres
        with pg.cursor() as cur:
            cur.execute("""
                SELECT sg.json
                FROM ss.site_graph sg
                JOIN ss.site_array sa ON sg.site_array_id = sa.id
                JOIN ss.site s ON sa.site_id = s.id
                WHERE s.sitename = %s
            """, ('TEST',))
            result = cur.fetchone()
            if not result:
                raise HTTPException(status_code=404, detail="Site graph not found")
            graph_json = json.loads(result[0])

        # Walk site graph to map monitor MACs to panel layout positions
        layout = {}
        def walk(node):
            if not isinstance(node, dict):
                return
            if node.get("devtype") == "P":
                x = node.get("x")
                y = node.get("y")
                for child in node.get("inputs", []):
                    if child.get("devtype") == "M":
                        mac = child.get("macaddr", "").lower()
                        if mac and x is not None and y is not None:
                            layout[mac] = {"x": x, "y": y}
            for child in node.get("inputs", []):
                walk(child)

        walk(graph_json.get("sitearray", {}))

        # Pull monitor statuses from Redis and match them to layout
        response = []
        for key in redis_conn.scan_iter("sitearray:monitor:*"):
            mac = key.split(":", 2)[2].lower()
            trimmed = mac[4:]  # Remove the first 4 characters → '001122334455'

            chunks = [trimmed[i:i + 2] for i in range(0, len(trimmed), 2)]
            formatted_mac = ":".join(chunks)
            data = redis_conn.hgetall(key)
            panel_info = layout.get(formatted_mac)

            if panel_info:
                response.append({
                    "mac": formatted_mac,
                    "status": data.get("status", "unknown"),
                    "voltage": data.get("voltage"),
                    "current": data.get("current"),
                    "x": panel_info["x"],
                    "y": panel_info["y"]
                })

        return response

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error: {e}")



@router.get("/sitearray/map", response_class=HTMLResponse)
async def mapviewer(request: Request):
    return templates.TemplateResponse("sitedata/mapviewer.html", {"request": request})


@router.post("/api/inject_fault")
async def inject_fault(mac: str = Form(...), fault: str = Form(...)):
    try:
        set_fault(mac, fault)
        return PlainTextResponse(f"Injected '{fault}' into {mac}")
    except Exception as e:
        return PlainTextResponse(f"Error: {e}", status_code=500)

@router.post("/api/clear_all_faults")
async def clear_all_faults():
    try:
        r = get_redis_conn(db=3)
        keys = r.keys("fault_injection:*")
        if keys:
            r.delete(*keys)
        return PlainTextResponse("✅ All faults cleared.")
    except Exception as e:
        return PlainTextResponse(f"Error: {e}", status_code=500)

