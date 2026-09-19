from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import BackgroundTasks, FastAPI, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import db
import scoring
from jobs import run_all_searches

APP_DIR = os.path.dirname(__file__)
SCRAPE_INTERVAL_HOURS = float(os.environ.get("SCRAPE_INTERVAL_HOURS", "6"))  # ~4x/day by default

db.init_db()

scheduler = BackgroundScheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler.add_job(
        run_all_searches,
        "interval",
        hours=SCRAPE_INTERVAL_HOURS,
        next_run_time=datetime.now(),  # run once immediately on boot
        id="scrape_job",
        replace_existing=True,
    )
    scheduler.start()
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="HiBid Watcher", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=os.path.join(APP_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(APP_DIR, "templates"))


def _nav_context(active: str):
    return {"profiles": db.list_profiles(), "active_tab": active}


@app.get("/")
def home(request: Request):
    lots = db.home_feed()
    return templates.TemplateResponse(
        request, "home.html", {"lots": lots, **_nav_context("home")}
    )


@app.get("/profile/{profile_name}")
def profile_page(request: Request, profile_name: str):
    profile = db.get_profile_by_name(profile_name)
    if not profile:
        return RedirectResponse("/", status_code=303)
    lots = db.profile_feed(profile["id"])
    return templates.TemplateResponse(
        request,
        "profile.html",
        {"profile": profile, "lots": lots, **_nav_context(profile_name)},
    )


@app.get("/settings")
def settings_page(request: Request):
    profiles = db.list_profiles()
    terms_by_profile = {p["id"]: db.list_terms_for_profile(p["id"]) for p in profiles}
    return templates.TemplateResponse(
        request,
        "settings.html",
        {"terms_by_profile": terms_by_profile, **_nav_context("settings")},
    )


@app.post("/settings/profiles")
def add_profile(name: str = Form(...)):
    name = name.strip()
    if name:
        db.add_profile(name)
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings/profiles/{profile_id}/terms")
def add_term(profile_id: int, term: str = Form(...), target_quantity: str = Form("")):
    term = term.strip()
    qty = int(target_quantity) if target_quantity.strip().isdigit() else None
    if term:
        db.add_term(profile_id, term, qty)
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings/terms/{term_id}/delete")
def remove_term(term_id: int):
    db.delete_term(term_id)
    return RedirectResponse("/settings", status_code=303)


@app.post("/run-now")
def run_now(background_tasks: BackgroundTasks):
    # Scraping can take a while (multiple terms, multiple pages, retries) -
    # run it in the background so the request returns immediately instead
    # of the browser hanging until the whole scrape finishes.
    background_tasks.add_task(run_all_searches)
    return RedirectResponse("/", status_code=303)


@app.post("/feedback")
def feedback(
    profile_id: int = Form(...),
    term: str = Form(...),
    lot_id: str = Form(...),
    title: str = Form(...),
    liked: str = Form(...),
    next: str = Form("/"),
):
    liked_bool = liked == "1"
    db.record_feedback(profile_id, term, lot_id, liked_bool)
    for word, delta in scoring.apply_feedback_to_weights(title, liked_bool).items():
        db.bump_word_weight(profile_id, term, word, delta)
    return RedirectResponse(next, status_code=303)
