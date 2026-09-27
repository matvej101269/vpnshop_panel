import uvicorn
import logging
import os

if __name__ == "__main__":
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, access_log=False)
