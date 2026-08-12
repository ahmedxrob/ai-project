import sqlite3
from pathlib import Path


DATA_DIR = Path("/data")
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "movies.db"


def get_connection():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_database():
    connection = get_connection()

    connection.execute("""
        CREATE TABLE IF NOT EXISTS watched (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            rating REAL NOT NULL,
            type TEXT NOT NULL CHECK(type IN ('Movie', 'Series')),
            poster TEXT,
            backdrop TEXT,
            year INTEGER,
            overview TEXT,
            tmdb_id INTEGER
        )
    """)

    # Upgrade existing databases
    columns = [
        ("poster", "TEXT"),
        ("backdrop", "TEXT"),
        ("year", "INTEGER"),
        ("overview", "TEXT"),
        ("tmdb_id", "INTEGER"),
    ]

    existing = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(watched)").fetchall()
    }

    for name, data_type in columns:
        if name not in existing:
            connection.execute(
                f"ALTER TABLE watched ADD COLUMN {name} {data_type}"
            )

    connection.commit()
    connection.close()


def get_all():
    connection = get_connection()

    movies = connection.execute("""
        SELECT
            id,
            title,
            rating,
            type,
            poster,
            backdrop,
            year,
            overview,
            tmdb_id
        FROM watched
        ORDER BY id DESC
    """).fetchall()

    connection.close()

    return movies


def add_movie(
    title,
    rating,
    media_type,
    poster=None,
    backdrop=None,
    year=None,
    overview=None,
    tmdb_id=None
):
    connection = get_connection()

    connection.execute(
        """
        INSERT INTO watched
        (title, rating, type, poster, backdrop, year, overview, tmdb_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            title,
            rating,
            media_type,
            poster,
            backdrop,
            year,
            overview,
            tmdb_id
        )
    )

    connection.commit()
    connection.close()


def delete_movie(movie_id):
    connection = get_connection()

    connection.execute(
        "DELETE FROM watched WHERE id = ?",
        (movie_id,)
    )

    connection.commit()
    connection.close()
