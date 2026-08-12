from fastapi import FastAPI, Request, Form
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app.database import (
    init_database,
    get_all,
    add_movie,
    delete_movie
)

app = FastAPI(title="My Movie AI")

templates = Jinja2Templates(directory="app/templates")

init_database()


@app.get("/")
def home(request: Request):
    movies = get_all()

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "movies": movies
        }
    )


@app.post("/add")
def add(
    title: str = Form(...),
    rating: float = Form(...),
    media_type: str = Form(...)
):
    title = title.strip()

    if title and 0 <= rating <= 10:
        add_movie(title, rating, media_type)

    return RedirectResponse("/", status_code=303)


@app.post("/delete/{movie_id}")
def delete(movie_id: int):
    delete_movie(movie_id)

    return RedirectResponse("/", status_code=303)
