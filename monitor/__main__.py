import os

import uvicorn

if __name__ == "__main__":
    port = int(os.environ.get("MONITOR_PORT", "8090"))
    uvicorn.run("monitor.app:app", host="0.0.0.0", port=port, reload=False)
