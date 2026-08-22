from fastapi import FastAPI
from fastapi.responses import HTMLResponse

app = FastAPI(title="Music Downloader")


@app.get("/", response_class=HTMLResponse)
async def home():
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Music Downloader</title>

        <style>
            body {
                font-family: Arial, sans-serif;
                background: #111827;
                color: white;
                margin: 0;
                padding: 30px;
            }

            .container {
                max-width: 900px;
                margin: auto;
            }

            h1 {
                font-size: 32px;
            }

            .search {
                display: flex;
                gap: 10px;
                margin-top: 30px;
            }

            input {
                flex: 1;
                padding: 15px;
                border-radius: 10px;
                border: none;
                font-size: 16px;
            }

            button {
                padding: 15px 25px;
                border: none;
                border-radius: 10px;
                cursor: pointer;
                font-size: 16px;
            }

            .card {
                background: #1f2937;
                padding: 20px;
                margin-top: 20px;
                border-radius: 12px;
            }
        </style>
    </head>

    <body>
        <div class="container">

            <h1>🎵 Music Downloader</h1>

            <p>
                Search for music and add it to your Navidrome library.
            </p>

            <div class="search">
                <input
                    type="text"
                    placeholder="Search artist, album or song..."
                >

                <button>
                    🔍 Search
                </button>
            </div>

            <div class="card">
                <h2>Ready</h2>
                <p>
                    Music search will be added next.
                </p>
            </div>

        </div>
    </body>
    </html>
    """


@app.get("/health")
async def health():
    return {"status": "ok"}
