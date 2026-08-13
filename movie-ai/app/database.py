import sqlite3

from pathlib import Path


# ============================================================
# DATABASE
# ============================================================

DATA_DIR = Path("/data")

DATA_DIR.mkdir(
    parents=True,
    exist_ok=True,
)

DB_PATH = DATA_DIR / "movies.db"


def get_connection():

    connection = sqlite3.connect(
        DB_PATH
    )

    connection.row_factory = sqlite3.Row

    return connection


# ============================================================
# INITIALIZE
# ============================================================

def init_database():

    connection = get_connection()

    # --------------------------------------------------------
    # WATCHED
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # RECOMMENDATION HISTORY
    # --------------------------------------------------------

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
    # NOT INTERESTED
    # --------------------------------------------------------

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS not_interested (
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
    # UPGRADE OLD WATCHED DATABASE
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
# WATCHED
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


def find_movie(
    title=None,
    media_type=None,
    tmdb_id=None,
):

    connection = get_connection()

    result = None

    # First: exact TMDB ID + type.
    if tmdb_id and media_type:

        result = connection.execute(
            """
            SELECT *
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

    # Second: exact title + type.
    if (
        result is None
        and title
        and media_type
    ):

        result = connection.execute(
            """
            SELECT *
            FROM watched
            WHERE LOWER(TRIM(title))
                  =
                  LOWER(TRIM(?))
              AND type = ?
            LIMIT 1
            """,
            (
                title,
                media_type,
            ),
        ).fetchone()

    connection.close()

    return result


def movie_exists(
    title=None,
    media_type=None,
    tmdb_id=None,
):

    return (
        find_movie(
            title=title,
            media_type=media_type,
            tmdb_id=tmdb_id,
        )
        is not None
    )


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

    existing = None

    # --------------------------------------------------------
    # Find by TMDB ID
    # --------------------------------------------------------

    if tmdb_id:

        existing = connection.execute(
            """
            SELECT *
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

    # --------------------------------------------------------
    # Find by title + type
    # --------------------------------------------------------

    if existing is None:

        existing = connection.execute(
            """
            SELECT *
            FROM watched
            WHERE LOWER(TRIM(title))
                  =
                  LOWER(TRIM(?))
              AND type = ?
            LIMIT 1
            """,
            (
                title,
                media_type,
            ),
        ).fetchone()

    # --------------------------------------------------------
    # UPDATE EXISTING ROW
    # --------------------------------------------------------

    if existing:

        final_title = (
            title
            if title
            else existing["title"]
        )

        final_poster = (
            poster
            if poster
            else existing["poster"]
        )

        final_backdrop = (
            backdrop
            if backdrop
            else existing["backdrop"]
        )

        final_year = (
            year
            if year is not None
            else existing["year"]
        )

        final_overview = (
            overview
            if overview
            else existing["overview"]
        )

        final_tmdb_id = (
            tmdb_id
            if tmdb_id
            else existing["tmdb_id"]
        )

        connection.execute(
            """
            UPDATE watched
            SET
                title = ?,
                rating = ?,
                type = ?,
                poster = ?,
                backdrop = ?,
                year = ?,
                overview = ?,
                tmdb_id = ?
            WHERE id = ?
            """,
            (
                final_title,
                rating,
                media_type,
                final_poster,
                final_backdrop,
                final_year,
                final_overview,
                final_tmdb_id,
                existing["id"],
            ),
        )

        connection.commit()

        connection.close()

        print(
            f"Updated watched item: "
            f"{final_title} "
            f"[{media_type}] "
            f"TMDB={final_tmdb_id}"
        )

        return existing["id"]

    # --------------------------------------------------------
    # INSERT NEW ROW
    # --------------------------------------------------------

    cursor = connection.execute(
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

    new_id = cursor.lastrowid

    connection.close()

    print(
        f"Added watched item: "
        f"{title} "
        f"[{media_type}] "
        f"TMDB={tmdb_id}"
    )

    return new_id


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
    limit=50,
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


# ============================================================
# NOT INTERESTED
# ============================================================

def add_not_interested(
    tmdb_id,
    media_type,
    title,
):

    if not tmdb_id:
        return

    connection = get_connection()

    existing = connection.execute(
        """
        SELECT id
        FROM not_interested
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
            INSERT INTO not_interested (
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


def get_not_interested_ids(
    media_type,
):

    connection = get_connection()

    rows = connection.execute(
        """
        SELECT tmdb_id
        FROM not_interested
        WHERE type = ?
        """,
        (
            media_type,
        ),
    ).fetchall()

    connection.close()

    return [
        row["tmdb_id"]
        for row in rows
    ]

