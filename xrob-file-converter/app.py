import asyncio
import os
import shutil
import subprocess
import uuid
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

APP_DIR = Path("/app")
DATA_DIR = Path("/data")

UPLOAD_DIR = DATA_DIR / "uploads"
OUTPUT_DIR = DATA_DIR / "output"
TMP_DIR = DATA_DIR / "tmp"

for directory in (UPLOAD_DIR, OUTPUT_DIR, TMP_DIR):
    directory.mkdir(parents=True, exist_ok=True)

app = FastAPI(
    title="Xrob File Converter",
    docs_url=None,
    redoc_url=None,
)

app.mount(
    "/static",
    StaticFiles(directory=str(APP_DIR / "www")),
    name="static",
)


# ---------------------------------------------------------
# Conversion definitions
# ---------------------------------------------------------

CONVERSIONS = {
    # Images
    "png": {
        "extensions": ["jpg", "jpeg", "webp", "bmp", "gif", "tiff"],
        "command": lambda src, dst: [
            "magick",
            str(src),
            str(dst),
        ],
    },
    "jpg": {
        "extensions": ["png", "webp", "bmp", "gif", "tiff"],
        "command": lambda src, dst: [
            "magick",
            str(src),
            str(dst),
        ],
    },
    "webp": {
        "extensions": ["png", "jpg", "jpeg", "bmp", "gif"],
        "command": lambda src, dst: [
            "magick",
            str(src),
            str(dst),
        ],
    },

    # Audio/video
    "mp3": {
        "extensions": [
            "mp4", "mkv", "webm", "wav", "flac",
            "m4a", "aac", "ogg", "opus"
        ],
        "command": lambda src, dst: [
            "ffmpeg",
            "-y",
            "-i",
            str(src),
            "-vn",
            "-codec:a",
            "libmp3lame",
            "-q:a",
            "2",
            str(dst),
        ],
    },

    "wav": {
        "extensions": [
            "mp3", "flac", "m4a", "aac", "ogg", "opus"
        ],
        "command": lambda src, dst: [
            "ffmpeg",
            "-y",
            "-i",
            str(src),
            str(dst),
        ],
    },

    "flac": {
        "extensions": [
            "mp3", "wav", "m4a", "aac", "ogg", "opus"
        ],
        "command": lambda src, dst: [
            "ffmpeg",
            "-y",
            "-i",
            str(src),
            str(dst),
        ],
    },

    "mp4": {
        "extensions": [
            "mkv", "webm", "avi", "mov"
        ],
        "command": lambda src, dst: [
            "ffmpeg",
            "-y",
            "-i",
            str(src),
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            str(dst),
        ],
    },

    "mkv": {
        "extensions": [
            "mp4", "webm", "avi", "mov"
        ],
        "command": lambda src, dst: [
            "ffmpeg",
            "-y",
            "-i",
            str(src),
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            str(dst),
        ],
    },

    "webm": {
        "extensions": [
            "mp4", "mkv", "avi", "mov"
        ],
        "command": lambda src, dst: [
            "ffmpeg",
            "-y",
            "-i",
            str(src),
            str(dst),
        ],
    },
}


DOCUMENT_EXTENSIONS = {
    "pdf",
    "doc",
    "docx",
    "odt",
    "rtf",
    "txt",
    "xls",
    "xlsx",
    "ods",
    "ppt",
    "pptx",
    "odp",
}


# ---------------------------------------------------------
# Helpers
# ---------------------------------------------------------

def extension(filename: str) -> str:
    return Path(filename).suffix.lower().lstrip(".")


def safe_name(filename: str) -> str:
    name = Path(filename).name
    return "".join(
        c for c in name
        if c.isalnum() or c in "._- "
    ).strip() or "file"


def run_command(command: list[str], cwd: Path | None = None):
    try:
        result = subprocess.run(
            command,
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=600,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(
            status_code=504,
            detail="Conversion timed out.",
        )

    if result.returncode != 0:
        error = result.stderr.strip()

        if not error:
            error = "Conversion program failed."

        raise HTTPException(
            status_code=500,
            detail=error[-2000:],
        )

    return result


def libreoffice_available() -> bool:
    return shutil.which("libreoffice") is not None


# ---------------------------------------------------------
# Routes
# ---------------------------------------------------------

@app.get("/")
async def index():
    return FileResponse(APP_DIR / "www" / "index.html")


@app.get("/api/formats")
async def formats():
    result = {}

    for target, info in CONVERSIONS.items():
        result[target] = info["extensions"]

    result["documents"] = sorted(DOCUMENT_EXTENSIONS)

    return result


@app.post("/convert")
async def convert(
    file: UploadFile = File(...),
    target: str = "pdf",
):
    if not file.filename:
        raise HTTPException(
            status_code=400,
            detail="No filename supplied.",
        )

    target = target.lower().lstrip(".")

    original_name = safe_name(file.filename)
    source_ext = extension(original_name)

    job_id = uuid.uuid4().hex

    source = UPLOAD_DIR / f"{job_id}_{original_name}"

    await file.seek(0)

    with source.open("wb") as output:
        while True:
            chunk = await file.read(1024 * 1024)

            if not chunk:
                break

            output.write(chunk)

    output_name = (
        Path(original_name).stem
        + "."
        + target
    )

    output_file = OUTPUT_DIR / f"{job_id}_{output_name}"

    try:

        # ---------------------------------------------
        # LibreOffice documents → requested format
        # ---------------------------------------------

        if (
            source_ext in DOCUMENT_EXTENSIONS
            and target in DOCUMENT_EXTENSIONS
        ):
            if not libreoffice_available():
                raise HTTPException(
                    status_code=500,
                    detail="LibreOffice is not available.",
                )

            output_dir = TMP_DIR / job_id
            output_dir.mkdir(parents=True, exist_ok=True)

            run_command(
                [
                    "libreoffice",
                    "--headless",
                    "--convert-to",
                    target,
                    "--outdir",
                    str(output_dir),
                    str(source),
                ]
            )

            generated = list(output_dir.iterdir())

            if not generated:
                raise HTTPException(
                    status_code=500,
                    detail="LibreOffice did not produce an output file.",
                )

            generated_file = generated[0]

            shutil.move(
                str(generated_file),
                str(output_file),
            )

        # ---------------------------------------------
        # Anything → PDF through LibreOffice
        # ---------------------------------------------

        elif target == "pdf":
            if not libreoffice_available():
                raise HTTPException(
                    status_code=500,
                    detail="LibreOffice is not available.",
                )

            output_dir = TMP_DIR / job_id
            output_dir.mkdir(parents=True, exist_ok=True)

            run_command(
                [
                    "libreoffice",
                    "--headless",
                    "--convert-to",
                    "pdf",
                    "--outdir",
                    str(output_dir),
                    str(source),
                ]
            )

            generated = list(output_dir.glob("*.pdf"))

            if not generated:
                raise HTTPException(
                    status_code=500,
                    detail="Unable to create PDF.",
                )

            shutil.move(
                str(generated[0]),
                str(output_file),
            )

        # ---------------------------------------------
        # Image / audio / video conversion
        # ---------------------------------------------

        elif target in CONVERSIONS:

            info = CONVERSIONS[target]

            if source_ext not in info["extensions"]:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Conversion from .{source_ext} "
                        f"to .{target} is not supported."
                    ),
                )

            command = info["command"](
                source,
                output_file,
            )

            run_command(command)

        else:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported target format: {target}",
            )

        if not output_file.exists():
            raise HTTPException(
                status_code=500,
                detail="Conversion completed but output was not created.",
            )

        return {
            "success": True,
            "filename": output_file.name,
            "download": f"/download/{output_file.name}",
        }

    finally:
        try:
            source.unlink(missing_ok=True)
        except Exception:
            pass

        shutil.rmtree(
            TMP_DIR / job_id,
            ignore_errors=True,
        )


@app.get("/download/{filename}")
async def download(filename: str):
    filename = Path(filename).name

    file_path = OUTPUT_DIR / filename

    if not file_path.exists():
        raise HTTPException(
            status_code=404,
            detail="File not found.",
        )

    return FileResponse(
        file_path,
        filename=filename,
        media_type="application/octet-stream",
    )


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "cpu_safe": True,
    }
