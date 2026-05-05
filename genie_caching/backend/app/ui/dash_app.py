"""
Plotly Dash UI (Flask WSGI) mounted under /dash/* in FastAPI.

Uses loopback HTTP to reuse the FastAPI routers with forwarded auth headers.
Bootstrap CSS is loaded from CDN (no npm / no dash-bootstrap-components).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import dash.exceptions
import httpx
from dash import Dash, Input, Output, State, dash_table, dcc, html, no_update
from flask import request as flask_request

logger = logging.getLogger(__name__)

_BOOTSTRAP = "https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css"

_SKIP_HEADERS = frozenset({
    "host",
    "connection",
    "content-length",
    "transfer-encoding",
    "te",
    "trailer",
    "upgrade",
    "proxy-connection",
})

_ASSETS_DIR = Path(__file__).resolve().parent / "assets"


def _listen_port() -> int:
    for key in ("DATABRICKS_APP_PORT", "PORT", "UVICORN_PORT"):
        v = os.getenv(key)
        if v:
            try:
                return int(v.strip())
            except ValueError:
                pass
    return 8000


def _api_client(hdrs: dict[str, str]) -> httpx.Client:
    base = f"http://127.0.0.1:{_listen_port()}"
    return httpx.Client(base_url=base, headers=hdrs, timeout=180.0)


def _headers_from_env() -> dict[str, str]:
    h = flask_request.headers
    return {name: str(h.get(name)) for name in h.keys() if name.lower() not in _SKIP_HEADERS}


def fetch_gateway_options(hdrs: dict[str, str]) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    opts: list[dict[str, str]] = []
    rows: list[dict[str, Any]] = []
    try:
        with _api_client(hdrs) as client:
            r = client.get("/api/gateways")
        payload = r.json() if r.is_success else []
        if not isinstance(payload, list):
            return [], []
        for row in payload:
            if isinstance(row, dict) and row.get("id"):
                gid = row["id"]
                name = row.get("name") or gid[:12] + "…"
                opts.append({"label": name, "value": gid})
                rows.append({"name": name, "id_preview": gid[:14] + "…", "_gid": gid})
    except Exception:
        logger.exception("fetch_gateway_options failed")
        return [], []
    return opts, rows


def layout_root() -> html.Div:
    return html.Div(
        className="d-flex flex-column vh-100 bg-white",
        children=[
            dcc.Location(id="loc", refresh=False),
            html.Nav(
                className="navbar bg-light border-bottom shadow-sm px-3 py-2",
                children=html.Div(
                    className="container-fluid d-flex align-items-center",
                    children=[
                        html.Img(src="/dash/assets/genie-icon-alt.svg", height="26", className="me-2"),
                        html.Span("Genie Cache Gateway", className="navbar-brand fw-semibold mb-0 me-auto"),
                        html.A("OpenAPI docs", href="/docs", className="text-secondary small"),
                    ],
                ),
            ),
            dcc.Tabs(
                id="main-tabs",
                value="gateways",
                className="px-4 pt-2",
                children=[
                    dcc.Tab(
                        label="Gateways",
                        value="gateways",
                        selected_className="fw-bold border-primary border-bottom border-3 border-top-0 border-start-0 border-end-0",
                        children=html.Div(
                            className="p-4",
                            children=[
                                html.H5("Create gateway"),
                                html.P(
                                    "Requires Owner role on the app. Warehouse ID optional — server default is used when empty.",
                                    className="small text-muted",
                                ),
                                dcc.Input(
                                    id="gw-new-name",
                                    type="text",
                                    placeholder="Display name",
                                    className="form-control mb-2",
                                ),
                                dcc.Input(
                                    id="gw-new-space",
                                    type="text",
                                    placeholder="Genie space ID",
                                    className="form-control mb-2",
                                ),
                                dcc.Input(
                                    id="gw-new-warehouse",
                                    type="text",
                                    placeholder="SQL warehouse ID (optional)",
                                    className="form-control mb-2",
                                ),
                                html.Button(
                                    "Create gateway",
                                    id="gw-create-btn",
                                    n_clicks=0,
                                    className="btn btn-success",
                                ),
                                html.Div(id="gw-create-msg", className="mt-2 small"),
                                html.Hr(className="my-4"),
                                html.H5("Gateways"),
                                html.Div(id="pane-gateways-table"),
                                dcc.Store(id="gw-list-bump", data=0),
                            ],
                        ),
                    ),
                    dcc.Tab(
                        label="Playground",
                        value="playground",
                        selected_className="fw-bold border-primary border-bottom border-3 border-top-0 border-start-0 border-end-0",
                        children=html.Div(
                            className="p-4",
                            style={"maxWidth": "960px"},
                            children=[
                                dcc.Dropdown(id="gw-dd", placeholder="Gateway…"),
                                dcc.Textarea(
                                    id="q-text",
                                    className="form-control mt-3 font-monospace",
                                    placeholder="Natural language question…",
                                    style={"minHeight": "110px"},
                                ),
                                html.Button(
                                    "Run",
                                    id="go-btn",
                                    n_clicks=0,
                                    className="btn btn-primary mt-2",
                                ),
                                html.Div(id="play-msg", className="small text-muted mt-2"),
                                html.Div(id="play-result", className="mt-3"),
                                dcc.Store(id="poll-state", data={"on": False, "qid": None}),
                                dcc.Interval(id="tick", interval=2000, n_intervals=0),
                            ],
                        ),
                    ),
                    dcc.Tab(
                        label="Settings",
                        value="settings",
                        selected_className="fw-bold border-primary border-bottom border-3 border-top-0 border-start-0 border-end-0",
                        children=html.Div(
                            className="p-4",
                            id="pane-settings",
                            children=[
                                html.H5("Runtime configuration"),
                                html.Div(
                                    className="alert alert-light border",
                                    children=[
                                        html.P(
                                            ["Use ", html.Code("PUT /api/config"), " from ", html.A("/docs", href="/docs"), "."],
                                            className="mb-0 small",
                                        ),
                                    ],
                                ),
                            ],
                        ),
                    ),
                ],
            ),
        ],
    )


def _warehouse_table_fragment(data: dict) -> list:
    cols = data.get("columns")
    rows = data.get("data_array")
    if not cols or not isinstance(rows, list):
        return [html.Pre(json.dumps(data, indent=2)[:32000])]
    names = [(c.get("name") if isinstance(c, dict) else str(c)) for c in cols]
    tbl = dash_table.DataTable(
        columns=[{"name": n, "id": str(i)} for i, n in enumerate(names)],
        data=[
            {str(i): ("null" if c is None else str(c)) for i, c in enumerate(r)}
            for r in rows[:200]
        ],
        style_table={"overflowX": "auto"},
        style_cell={"fontSize": "12px"},
    )
    return [html.H6("Warehouse result (SQL)"), tbl]


def _render_result_children(sql: str | None, data: Any):
    chunks: list = []
    if sql:
        chunks.extend(
            [
                html.H6("SQL"),
                html.Pre(sql, className="small bg-light p-2 rounded border"),
            ]
        )
    if not data:
        return html.Div(chunks)

    if isinstance(data, dict) and ("warehouse_result" in data or "genie_attachments" in data):
        wh = data.get("warehouse_result")
        ga = data.get("genie_attachments")
        if ga:
            chunks.extend(
                [
                    html.H6("Genie message (attachments)"),
                    html.Pre(
                        json.dumps(ga, indent=2, default=str)[:48000],
                        className="small bg-white border rounded p-2",
                    ),
                ]
            )
        if isinstance(wh, dict):
            cols = wh.get("columns")
            rows = wh.get("data_array")
            if cols and isinstance(rows, list):
                chunks.extend(_warehouse_table_fragment(wh))
            elif wh:
                chunks.extend([html.H6("Warehouse result"), html.Pre(json.dumps(wh, indent=2)[:24000])])
        return html.Div(chunks)

    if isinstance(data, dict):
        cols = data.get("columns")
        rows = data.get("data_array")
        if cols and isinstance(rows, list):
            chunks.extend(_warehouse_table_fragment(data))
            return html.Div(chunks)
        chunks.append(html.Pre(json.dumps(data, indent=2)[:32000]))
        return html.Div(chunks)

    chunks.append(html.Pre(json.dumps(data, indent=2)[:32000]))
    return html.Div(chunks)


def build_wsgi_dashboard():  # -> Flask server
    # WSGIMiddleware mounts at /dash and passes PATH_INFO with the prefix stripped,
    # so Flask routes must use routes_pathname_prefix="/". Browser requests still
    # use /dash/... via requests_pathname_prefix.
    dash_app = Dash(
        __name__,
        suppress_callback_exceptions=True,
        assets_folder=str(_ASSETS_DIR),
        routes_pathname_prefix="/",
        requests_pathname_prefix="/dash/",
        external_stylesheets=[_BOOTSTRAP],
        title="Genie Cache Gateway",
    )
    dash_app.layout = layout_root()

    @dash_app.callback(
        Output("main-tabs", "value"),
        Output("gw-dd", "options"),
        Output("gw-dd", "value"),
        Input("loc", "pathname"),
        Input("loc", "search"),
    )
    def sync_url(loc_path: str | None, search_qs: str | None):
        hdr = _headers_from_env()
        opts, _ = fetch_gateway_options(hdr)

        # loc_path is the full browser path (e.g. /dash/ or /dash/playground).
        rel = (loc_path or "").removeprefix("/dash").strip("/")
        gid_pref = None
        qs = search_qs or ""
        if qs.startswith("?"):
            q = parse_qs(qs.removeprefix("?"))
            vals = q.get("gateway") or []
            gid_pref = vals[0] if vals else None

        last = rel.split("/")[-1] if rel else ""
        tab = "gateways"
        if last == "playground":
            tab = "playground"
        elif last == "settings":
            tab = "settings"
        chosen = gid_pref if gid_pref and any(o["value"] == gid_pref for o in opts) else None
        if tab == "playground" and chosen is None and opts:
            chosen = opts[0]["value"]
        return tab, opts, chosen

    def _gateways_table_body(hdr):
        _, tbl = fetch_gateway_options(hdr)
        if not tbl:
            return html.Div(
                className="alert alert-warning mb-0",
                children=[
                    html.P(
                        [
                            "No gateways yet — create one above, or use ",
                            html.A("POST /api/gateways", href="/docs"),
                            " (Owner role).",
                        ],
                        className="mb-0 small",
                    ),
                ],
            )
        return dash_table.DataTable(
            columns=[
                {"name": "Name", "id": "name"},
                {"name": "Id", "id": "id_preview"},
                {"name": "", "id": "go", "presentation": "markdown"},
            ],
            data=[
                {
                    "name": row["name"],
                    "id_preview": row["id_preview"],
                    "go": f"[Playground](/dash/playground?gateway={row['_gid']})",
                }
                for row in tbl
            ],
            markdown_options={"link_target": "_self"},
            style_as_list_view=True,
            style_table={"maxWidth": "840px"},
        )

    @dash_app.callback(
        Output("pane-gateways-table", "children"),
        Input("main-tabs", "value"),
        Input("gw-list-bump", "data"),
    )
    def gateways_table(active: str, _bump):  # noqa: ARG001
        if active != "gateways":
            raise dash.exceptions.PreventUpdate
        hdr = _headers_from_env()
        return _gateways_table_body(hdr)

    @dash_app.callback(
        Output("gw-create-msg", "children"),
        Output("gw-list-bump", "data"),
        Input("gw-create-btn", "n_clicks"),
        State("gw-new-name", "value"),
        State("gw-new-space", "value"),
        State("gw-new-warehouse", "value"),
        State("gw-list-bump", "data"),
        prevent_initial_call=True,
    )
    def create_gateway_clicked(n_click, name, space_id, wh_id, bump):
        if not n_click:
            raise dash.exceptions.PreventUpdate
        name = (name or "").strip()
        space_id = (space_id or "").strip()
        wh = (wh_id or "").strip()
        if not name or not space_id:
            return html.Span("Name and Genie space ID are required.", className="text-danger"), no_update
        payload: dict[str, Any] = {"name": name, "genie_space_id": space_id}
        if wh:
            payload["sql_warehouse_id"] = wh
        hdr = _headers_from_env()
        try:
            with _api_client(hdr) as client:
                r = client.post("/api/gateways", json=payload)
        except Exception as e:
            return html.Span(str(e), className="text-danger"), no_update
        if r.status_code >= 400:
            detail = ""
            try:
                detail = r.json().get("detail", "")
            except Exception:
                detail = r.text[:500]
            return html.Span(str(detail) or r.text[:500], className="text-danger"), no_update
        return html.Span(f"Created gateway “{name}”.", className="text-success"), int(bump or 0) + 1

    @dash_app.callback(
        Output("poll-state", "data"),
        Output("go-btn", "disabled"),
        Output("play-msg", "children"),
        Input("go-btn", "n_clicks"),
        State("gw-dd", "value"),
        State("q-text", "value"),
        prevent_initial_call=True,
    )
    def submit(nclk, gid, query):
        if not nclk:
            raise dash.exceptions.PreventUpdate
        hdr = _headers_from_env()
        query = (query or "").strip()
        if not gid or not query:
            return {"on": False, "qid": None}, False, "Select gateway and enter a query."
        gw_row: dict[str, Any] = {}
        try:
            with _api_client(hdr) as client:
                rr = client.get(f"/api/gateways/{gid}")
                gw_row = rr.json() if rr.is_success else {}
            if not isinstance(gw_row, dict):
                gw_row = {}
        except Exception as e:
            return {"on": False, "qid": None}, False, f"gateway error: {e}"

        payload: dict[str, Any] = {
            "query": query,
            "gateway_id": gid,
            "config": {
                "gateway_id": gw_row.get("id", gid),
                "genie_space_id": gw_row.get("genie_space_id"),
            },
        }
        if gw_row.get("sql_warehouse_id"):
            payload["config"].update(
                {
                    "sql_warehouse_id": gw_row.get("sql_warehouse_id"),
                    "similarity_threshold": gw_row.get("similarity_threshold", 0.92),
                    "max_queries_per_minute": gw_row.get("max_queries_per_minute", 5),
                    "cache_ttl_hours": gw_row.get("cache_ttl_hours", 24),
                    "embedding_provider": gw_row.get("embedding_provider", "databricks"),
                    "databricks_embedding_endpoint": gw_row.get(
                        "databricks_embedding_endpoint", "databricks-gte-large-en"
                    ),
                    "shared_cache": gw_row.get("shared_cache", True),
                    "storage_backend": "lakebase",
                }
            )
        try:
            with _api_client(hdr) as client:
                rp = client.post("/api/query", json=payload)
            if rp.status_code >= 400:
                return {"on": False, "qid": None}, False, html.Span(rp.text[:900], className="text-danger")
            qid = rp.json().get("query_id")
        except Exception as e:
            return {"on": False, "qid": None}, False, f"submit: {e}"
        if not qid:
            return {"on": False, "qid": None}, False, "no query_id returned"
        return {"on": True, "qid": qid}, True, html.Span(["Polling ", html.Code(qid)])

    @dash_app.callback(
        Output("play-msg", "children", allow_duplicate=True),
        Output("play-result", "children"),
        Output("go-btn", "disabled", allow_duplicate=True),
        Output("poll-state", "data", allow_duplicate=True),
        Input("tick", "n_intervals"),
        State("poll-state", "data"),
        prevent_initial_call=True,
    )
    def poll(_niv, state):
        st = state or {}
        if not st.get("on") or not st.get("qid"):
            raise dash.exceptions.PreventUpdate
        qid = st["qid"]
        hdr = _headers_from_env()
        try:
            with _api_client(hdr) as client:
                rsp = client.post(f"/api/query/{qid}/status")
            row = rsp.json() if rsp.is_success else {}
        except Exception:
            logger.exception("poll failed")
            raise dash.exceptions.PreventUpdate

        stage = row.get("stage", "?")
        err = row.get("error")

        badge = html.Div(html.Span(str(stage), className="badge bg-secondary"))
        terminal = {"completed", "failed"}
        if stage not in terminal:
            return badge, no_update, True, {"on": True, "qid": qid}

        if stage == "completed":
            return html.Div(["Done ", badge]), _render_result_children(row.get("sql_query"), row.get("result")), False, {
                "on": False,
                "qid": None,
            }
        return html.Div([badge, html.Span(str(err), className="text-danger ms-2")]), no_update, False, {
            "on": False,
            "qid": None,
        }

    return dash_app.server
