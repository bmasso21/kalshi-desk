"""Mock of the Pushover messages API (form-encoded POST /1/messages.json) + a heartbeat endpoint."""
import re
import uuid
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

KEY = re.compile(r"[A-Za-z0-9]{30}")


def create_pushover(valid_token: str, valid_user: str):
    app = FastAPI()
    app.state.messages = []
    app.state.pings = 0

    @app.post("/1/messages.json")
    async def messages(request: Request):
        form = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
        errs = []
        if form.get("token") != valid_token:
            errs.append("application token is invalid")
        if form.get("user") != valid_user:
            errs.append("user identifier is not a valid user, group, or subscribed user key")
        if not form.get("message"):
            errs.append("message cannot be blank")
        pr = int(form.get("priority", 0))
        if pr == 2 and not (form.get("retry") and form.get("expire")):
            errs.append("retry and expire are required for emergency priority")
        if pr == 2 and (int(form["retry"]) < 30 or int(form["expire"]) > 10800):
            errs.append("retry must be >= 30 and expire <= 10800")
        if errs:
            return JSONResponse({"status": 0, "errors": errs, "request": str(uuid.uuid4())}, 400)
        app.state.messages.append(form)
        out = {"status": 1, "request": str(uuid.uuid4())}
        if pr == 2:
            out["receipt"] = "r" + uuid.uuid4().hex[:29]
        return out

    @app.get("/ping")
    def ping():
        app.state.pings += 1
        return "OK"

    return app
