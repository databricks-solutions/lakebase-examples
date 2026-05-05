"""
Databricks Apps entry: run the FastAPI ASGI app from entrypoint.py.

Using ``python app.py`` matches the documented app spec pattern and gives a
clear default when the runtime looks for a root-level Python file.
Port is read from DATABRICKS_APP_PORT when set (Apps inject this).
"""

import os

import uvicorn

if __name__ == "__main__":
    port_str = os.getenv("DATABRICKS_APP_PORT") or os.getenv("PORT") or "8000"
    port = int(port_str)
    uvicorn.run("entrypoint:app", host="0.0.0.0", port=port)
