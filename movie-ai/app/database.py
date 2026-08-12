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
            type TEXT NOT NULL
                CHECK(type IN ('Movie', 'Series'))
        )
    """)

    # -----------------------------------------
    # Upgrade existing database
    # -----------------------------------------

    columns = connection.execute(
        "PRAGMA table_info(watched)"
    ).fetchall()

    existing_columns = {
        column["name"]
        for column in columns
    }

    new_columns = {
        "poster": "TEXT",
        "backdrop": "TEXT",
        "year": "INTEGER",
        "overview": "TEXT",
        "tmdb_id": "INTEGER",
    }

    for column_name, column_type in new_columns.items():

        if column_name not in existing_columns:

            connection.execute(
                f"ALTER TABLE watched "
                f"ADD COLUMN {column_name} {column_type}"
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
    tmdb_id=None,
):

    connection = get_connection()

    connection.execute(
        """
        INSERT INTO watched (
            title,
            rating,
            type,
            poster,
            backdrop,
            year,
            overview,
            tmdb_id
        )
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
            tmdb_id,
        ),
    )

    connection.commit()
    connection.close()


def delete_movie(movie_id):

    connection = get_connection()

    connection.execute(
        "DELETE FROM watched WHERE id = ?",
        (movie_id,),
    )

    connection.commit()
    connection.close()
