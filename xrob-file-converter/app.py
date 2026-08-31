import os
import shutil
import subprocess
import uuid
from pathlib import Path

from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

APP_DIR = Path("/app")
DATA_DIR = Path("/data")

UPLOAD_DIR = DATA_DIR / "uploads"
OUTPUT_DIR = DATA_DIR / "output"
TMP_DIR = DATA_DIR / "tmp"

for directory in (UPLOAD_DIR, OUTPUT_DIR, TMP_DIR):
    directory.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Xrob File Converter")

app.mount("/static", StaticFiles(directory=str(APP_DIR / "www")), name="static")


@app.get("/")
async def index():
    return FileResponse(APP_DIR / "www" / "index.html")


def safe_name(name: str) -> str:
    name = Path(name).name
    return "".join(
        c for c in name
        if c.isalnum() or c in "._- "
    ).strip() or "file"


def run_command(command):
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    if result.returncode != 0:
        raise RuntimeError(result.stderr[-3000:])

    return result


def convert_with_libreoffice(input_file: Path, output_dir: Path):
    command = [
        "libreoffice",
        "--headless",
        "--convert-to",
        "pdf",
        "--outdir",
        str(output_dir),
        str(input_file),
    ]

    run_command(command)

    expected = output_dir / f"{input_file.stem}.pdf"

    if not expected.exists():
        raise RuntimeError("LibreOffice did not create the PDF.")

    return expected


@app.post("/convert")
async def convert(
    file: UploadFile = File(...),
    target: str = "pdf",
):
    original_name = safe_name(file.filename or "file")
    extension = Path(original_name).suffix.lower()

    job_id = uuid.uuid4().hex

    work_dir = TMP_DIR / job_id
    work_dir.mkdir(parents=True, exist_ok=True)

    input_file = work_dir / original_name

    try:
        with open(input_file, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        target = target.lower().strip(".")

        document_extensions = {
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

        image_extensions = {
            ".png",
            ".jpg",
            ".jpeg",
            ".webp",
            ".bmp",
            ".tiff",
            ".tif",
            ".gif",
            ".svg",
        }

        # -------------------------------------------------
        # DOCUMENTS → PDF
        # -------------------------------------------------

        if target == "pdf" and extension in document_extensions:
            result = convert_with_libreoffice(
                input_file,
                work_dir,
            )

        # -------------------------------------------------
        # IMAGE → IMAGE
        # -------------------------------------------------

        elif extension in image_extensions and target in {
            "png",
            "jpg",
            "jpeg",
            "webp",
            "bmp",
            "tiff",
        }:
            output_ext = "jpg" if target == "jpeg" else target

            result = work_dir / f"{input_file.stem}.{output_ext}"

            command = [
                "convert",
                str(input_file),
                str(result),
            ]

            run_command(command)

        # -------------------------------------------------
        # SVG → PNG/JPG/WEBP
        # -------------------------------------------------

        elif extension == ".svg" and target in {
            "png",
            "jpg",
            "jpeg",
            "webp",
        }:
            output_ext = "jpg" if target == "jpeg" else target

            result = work_dir / f"{input_file.stem}.{output_ext}"

            command = [
                "convert",
                "-background",
                "none",
                str(input_file),
                str(result),
            ]

            run_command(command)

        # -------------------------------------------------
        # PDF → PNG
        # -------------------------------------------------

        elif extension == ".pdf" and target == "png":
            prefix = work_dir / "page"

            command = [
                "pdftoppm",
                "-png",
                str(input_file),
                str(prefix),
            ]

            run_command(command)

            pages = sorted(work_dir.glob("page-*.png"))

            if not pages:
                raise RuntimeError("No PDF pages were generated.")

            result = pages[0]

        # -------------------------------------------------
        # PDF → JPG
        # -------------------------------------------------

        elif extension == ".pdf" and target in {"jpg", "jpeg"}:
            prefix = work_dir / "page"

            command = [
                "pdftoppm",
                "-jpeg",
                str(input_file),
                str(prefix),
            ]

            run_command(command)

            pages = sorted(work_dir.glob("page-*.jpg"))

            if not pages:
                raise RuntimeError("No PDF pages were generated.")

            result = pages[0]

        # -------------------------------------------------
        # AUDIO / VIDEO
        # -------------------------------------------------

        elif target in {
            "mp3",
            "wav",
            "aac",
            "flac",
            "ogg",
            "mp4",
            "mkv",
            "webm",
            "avi",
            "mov",
        }:
            result = work_dir / f"{input_file.stem}.{target}"

            command = [
                "ffmpeg",
                "-y",
                "-i",
                str(input_file),
                str(result),
            ]

            run_command(command)

        else:
            raise HTTPException(
                status_code=400,
                detail=f"Conversion from {extension} to {target} is not supported yet.",
            )

        if not result.exists():
            raise RuntimeError("Conversion finished but output was not created.")

        final_name = result.name

        destination = OUTPUT_DIR / f"{job_id}_{final_name}"

        shutil.copy2(result, destination)

        return {
            "success": True,
            "filename": final_name,
            "download": f"/download/{job_id}/{final_name}",
        }

    except HTTPException:
        raise

    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=str(error),
        )

    finally:
        try:
            shutil.rmtree(work_dir)
        except Exception:
            pass


@app.get("/download/{job_id}/{filename}")
async def download(job_id: str, filename: str):
    filename = safe_name(filename)

    matches = list(OUTPUT_DIR.glob(f"{job_id}_{filename}"))

    if not matches:
        raise HTTPException(
            status_code=404,
            detail="File not found.",
        )

    return FileResponse(
        matches[0],
        filename=filename,
    )
