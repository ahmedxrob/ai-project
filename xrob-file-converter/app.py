import os
import uuid
import shutil
import subprocess
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles


APP_DIR = Path("/app")
DATA_DIR = Path("/data")

UPLOAD_DIR = DATA_DIR / "uploads"
OUTPUT_DIR = DATA_DIR / "output"
TMP_DIR = DATA_DIR / "tmp"

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
TMP_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(
    title="Xrob File Converter",
    version="1.0.0",
)

app.mount(
    "/static",
    StaticFiles(directory="/app/www"),
    name="static",
)


# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------

def safe_filename(filename: str) -> str:
    filename = os.path.basename(filename)

    allowed = (
        "abcdefghijklmnopqrstuvwxyz"
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        "0123456789"
        "._- "
    )

    filename = "".join(
        char if char in allowed else "_"
        for char in filename
    )

    return filename or "file"


def run_command(command, timeout=600):
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )

        if result.returncode != 0:
            raise RuntimeError(
                result.stderr[-4000:]
                or result.stdout[-4000:]
                or "Conversion failed."
            )

        return result

    except subprocess.TimeoutExpired:
        raise RuntimeError("Conversion timed out.")


def extension(path: Path):
    return path.suffix.lower()


# ------------------------------------------------------------
# Home page
# ------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index():
    html_file = APP_DIR / "www" / "index.html"

    return html_file.read_text(
        encoding="utf-8"
    )


# ------------------------------------------------------------
# Health
# ------------------------------------------------------------

@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "service": "Xrob File Converter",
    }


# ------------------------------------------------------------
# Conversion
# ------------------------------------------------------------

@app.post("/api/convert")
async def convert(
    file: UploadFile = File(...),
    output_format: str = Form(...),
):
    job_id = uuid.uuid4().hex

    original_name = safe_filename(
        file.filename or "file"
    )

    input_path = UPLOAD_DIR / f"{job_id}_{original_name}"

    try:

        # ----------------------------------------------------
        # Save upload
        # ----------------------------------------------------

        with input_path.open("wb") as buffer:
            shutil.copyfileobj(
                file.file,
                buffer,
            )

        input_ext = extension(input_path)

        output_format = (
            output_format
            .lower()
            .strip()
            .replace(".", "")
        )

        base_name = input_path.stem

        # ----------------------------------------------------
        # OFFICE → PDF
        # ----------------------------------------------------

        office_extensions = {
            ".doc",
            ".docx",
            ".odt",
            ".rtf",
            ".txt",
            ".xls",
            ".xlsx",
            ".ods",
            ".ppt",
            ".pptx",
            ".odp",
        }

        if input_ext in office_extensions and output_format == "pdf":

            run_command([
                "libreoffice",
                "--headless",
                "--convert-to",
                "pdf",
                "--outdir",
                str(OUTPUT_DIR),
                str(input_path),
            ])

            generated = OUTPUT_DIR / (
                input_path.stem + ".pdf"
            )

        # ----------------------------------------------------
        # PDF → PNG
        # ----------------------------------------------------

        elif input_ext == ".pdf" and output_format == "png":

            generated = OUTPUT_DIR / (
                base_name + ".png"
            )

            run_command([
                "pdftoppm",
                "-png",
                "-singlefile",
                str(input_path),
                str(
                    OUTPUT_DIR / base_name
                ),
            ])

        # ----------------------------------------------------
        # PDF → JPG
        # ----------------------------------------------------

        elif input_ext == ".pdf" and output_format in {
            "jpg",
            "jpeg",
        }:

            generated = OUTPUT_DIR / (
                base_name + ".jpg"
            )

            run_command([
                "pdftoppm",
                "-jpeg",
                "-singlefile",
                str(input_path),
                str(
                    OUTPUT_DIR / base_name
                ),
            ])

            generated = OUTPUT_DIR / (
                base_name + ".jpg"
            )

        # ----------------------------------------------------
        # IMAGE → PDF
        # ----------------------------------------------------

        elif (
            input_ext in {
                ".png",
                ".jpg",
                ".jpeg",
                ".webp",
                ".bmp",
                ".tiff",
                ".tif",
            }
            and output_format == "pdf"
        ):

            generated = OUTPUT_DIR / (
                base_name + ".pdf"
            )

            run_command([
                "convert",
                str(input_path),
                str(generated),
            ])

        # ----------------------------------------------------
        # SVG → PNG
        # ----------------------------------------------------

        elif input_ext == ".svg" and output_format == "png":

            generated = OUTPUT_DIR / (
                base_name + ".png"
            )

            run_command([
                "convert",
                str(input_path),
                str(generated),
            ])

        # ----------------------------------------------------
        # SVG → PDF
        # ----------------------------------------------------

        elif input_ext == ".svg" and output_format == "pdf":

            generated = OUTPUT_DIR / (
                base_name + ".pdf"
            )

            run_command([
                "convert",
                str(input_path),
                str(generated),
            ])

        # ----------------------------------------------------
        # IMAGE → JPG
        # ----------------------------------------------------

        elif (
            input_ext in {
                ".png",
                ".webp",
                ".bmp",
                ".tiff",
                ".tif",
            }
            and output_format in {
                "jpg",
                "jpeg",
            }
        ):

            generated = OUTPUT_DIR / (
                base_name + ".jpg"
            )

            run_command([
                "convert",
                str(input_path),
                str(generated),
            ])

        # ----------------------------------------------------
        # IMAGE → PNG
        # ----------------------------------------------------

        elif (
            input_ext in {
                ".jpg",
                ".jpeg",
                ".webp",
                ".bmp",
                ".tiff",
                ".tif",
            }
            and output_format == "png"
        ):

            generated = OUTPUT_DIR / (
                base_name + ".png"
            )

            run_command([
                "convert",
                str(input_path),
                str(generated),
            ])

        # ----------------------------------------------------
        # AUDIO → MP3
        # ----------------------------------------------------

        elif (
            input_ext in {
                ".wav",
                ".flac",
                ".m4a",
                ".aac",
                ".ogg",
                ".opus",
            }
            and output_format == "mp3"
        ):

            generated = OUTPUT_DIR / (
                base_name + ".mp3"
            )

            run_command([
                "ffmpeg",
                "-y",
                "-i",
                str(input_path),
                "-codec:a",
                "libmp3lame",
                "-q:a",
                "2",
                str(generated),
            ])

        # ----------------------------------------------------
        # VIDEO → MP4
        # ----------------------------------------------------

        elif (
            input_ext in {
                ".mkv",
                ".avi",
                ".mov",
                ".webm",
                ".m4v",
            }
            and output_format == "mp4"
        ):

            generated = OUTPUT_DIR / (
                base_name + ".mp4"
            )

            run_command([
                "ffmpeg",
                "-y",
                "-i",
                str(input_path),
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "23",
                "-c:a",
                "aac",
                str(generated),
            ], timeout=3600)

        # ----------------------------------------------------
        # VIDEO → MP3
        # ----------------------------------------------------

        elif (
            input_ext in {
                ".mp4",
                ".mkv",
                ".avi",
                ".mov",
                ".webm",
            }
            and output_format == "mp3"
        ):

            generated = OUTPUT_DIR / (
                base_name + ".mp3"
            )

            run_command([
                "ffmpeg",
                "-y",
                "-i",
                str(input_path),
                "-vn",
                "-codec:a",
                "libmp3lame",
                "-q:a",
                "2",
                str(generated),
            ])

        else:

            raise RuntimeError(
                f"Conversion from "
                f"{input_ext} to "
                f"{output_format.upper()} "
                f"is not supported yet."
            )

        # ----------------------------------------------------
        # Check output
        # ----------------------------------------------------

        if not generated.exists():
            raise RuntimeError(
                "The converter finished but "
                "no output file was created."
            )

        return JSONResponse({
            "success": True,
            "filename": generated.name,
            "download": (
                f"/api/download/"
                f"{generated.name}"
            ),
        })

    except Exception as error:

        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": str(error),
            },
        )

    finally:

        try:
            input_path.unlink(
                missing_ok=True
            )
        except Exception:
            pass


# ------------------------------------------------------------
# Download
# ------------------------------------------------------------

@app.get("/api/download/{filename}")
async def download(filename: str):

    filename = os.path.basename(filename)

    path = OUTPUT_DIR / filename

    if not path.exists():
        return JSONResponse(
            status_code=404,
            content={
                "error": "File not found."
            },
        )

    return FileResponse(
        path,
        filename=filename,
        media_type="application/octet-stream",
    )
