import sqlite3

from pathlib import Path


DATA_DIR = Path("/data")

DATA_DIR.mkdir(
    parents=True,
    exist_ok=True,
)

DB_PATH = DATA_DIR / "movies.db"


# ============================================================
# CONNECTION
# ============================================================

def get_connection():

    connection = sqlite3.connect(
        DB_PATH
    )

    connection.row_factory = sqlite3.Row

    return connection


# ============================================================
# INIT
# ============================================================

def init_database():

    connection = get_connection()

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS watched (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            rating REAL NOT NULL,
            type TEXT NOT NULL
                CHECK(type IN ('Movie', 'Series')),
            poster TEXT,
            backdrop TEXT,
            year INTEGER,
            overview TEXT,
            tmdb_id INTEGER
        )
        """
    )

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS recommendation_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tmdb_id INTEGER NOT NULL,
            type TEXT NOT NULL
                CHECK(type IN ('Movie', 'Series')),
            title TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # --------------------------------------------------------
    # Upgrade old watched database
    # --------------------------------------------------------

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
                f"ADD COLUMN {column_name} "
                f"{column_type}"
            )

    connection.commit()

    connection.close()


# ============================================================
# GET EVERYTHING
# ============================================================

def get_all():

    connection = get_connection()

    movies = connection.execute(
        """
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
        """
    ).fetchall()

    connection.close()

    return movies


# ============================================================
# DUPLICATE CHECK
# ============================================================

def movie_exists(
    title=None,
    media_type=None,
    tmdb_id=None,
):
    connection = get_connection()

    result = None

    if tmdb_id and media_type:

        result = connection.execute(
            """
            SELECT id
            FROM watched
            WHERE tmdb_id = ?
              AND type = ?
            LIMIT 1
            """,
            (
                tmdb_id,
                media_type,
            ),
        ).fetchone()

    elif title and media_type:

        result = connection.execute(
            """
            SELECT id
            FROM watched
            WHERE LOWER(title) = LOWER(?)
              AND type = ?
            LIMIT 1
            """,
            (
                title.strip(),
                media_type,
            ),
        ).fetchone()

    connection.close()

    return result is not None


# ============================================================
# ADD MOVIE / SERIES
# ============================================================

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

    if movie_exists(
        title=title,
        media_type=media_type,
        tmdb_id=tmdb_id,
    ):

        print(
            f"Duplicate prevented: "
            f"{title} [{media_type}]"
        )

        return False

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

    return True


# ============================================================
# DELETE
# ============================================================

def delete_movie(
    movie_id,
):

    connection = get_connection()

    connection.execute(
        """
        DELETE FROM watched
        WHERE id = ?
        """,
        (
            movie_id,
        ),
    )

    connection.commit()

    connection.close()


# ============================================================
# RECOMMENDATION HISTORY
# ============================================================

def add_recommendation_history(
    tmdb_id,
    media_type,
    title,
):
    if not tmdb_id:
        return

    connection = get_connection()

    # Don't insert the same recommendation twice.
    existing = connection.execute(
        """
        SELECT id
        FROM recommendation_history
        WHERE tmdb_id = ?
          AND type = ?
        LIMIT 1
        """,
        (
            tmdb_id,
            media_type,
        ),
    ).fetchone()

    if not existing:

        connection.execute(
            """
            INSERT INTO recommendation_history (
                tmdb_id,
                type,
                title
            )
            VALUES (?, ?, ?)
            """,
            (
                tmdb_id,
                media_type,
                title,
            ),
        )

        connection.commit()

    connection.close()


def get_recent_recommendation_ids(
    media_type,
    limit=500,
):
    connection = get_connection()

    rows = connection.execute(
        """
        SELECT tmdb_id
        FROM recommendation_history
        WHERE type = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (
            media_type,
            limit,
        ),
    ).fetchall()

    connection.close()

    return [
        row["tmdb_id"]
        for row in rows
    ]

