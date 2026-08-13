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
