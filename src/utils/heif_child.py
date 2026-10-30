"""Decode one HEIC or HEIF photo into the JPEG stored in its place (#33), in a
process of its own.

src/utils/photo_store.py runs it as ``python -I heif_child.py <limit> <quality>``:
the photo comes on stdin, the JPEG goes to stdout, and any failure is a nonzero
exit (an uncaught exception, or a signal when libheif aborts). The
address-space limit is set before Pillow or pillow-heif is imported, so a
decode that needs more memory than its size check promised fails here, not in
the API process. ``-I`` keeps the repository off ``sys.path``: this file
imports only the standard library, Pillow and pillow-heif.
"""
import io
import sys


def _limit_memory(limit: int) -> None:
    try:
        import resource
    except ImportError:
        # Windows, a development machine only: the decode runs without a limit.
        return
    resource.setrlimit(resource.RLIMIT_AS, (limit, limit))


def main() -> int:
    limit, quality = int(sys.argv[1]), int(sys.argv[2])
    if limit:
        _limit_memory(limit)

    from PIL import Image
    from pillow_heif import register_heif_opener

    # One thread: libheif otherwise decodes grid tiles four at a time.
    register_heif_opener(decode_threads=1)
    img = Image.open(io.BytesIO(sys.stdin.buffer.read()))
    if img.format != "HEIF":
        return 2
    # libheif applies the container's rotation and mirror, and pillow-heif
    # resets the EXIF orientation to 1 on open, so the copied EXIF cannot turn
    # the photo a second time. EXIF (capture date included) and the ICC
    # profile are kept; other metadata is not.
    exif = img.info.get("exif") or b""
    if exif and not exif.startswith(b"Exif\x00\x00"):
        exif = b"Exif\x00\x00" + exif
    icc_profile = img.info.get("icc_profile")
    if img.mode == "RGB":
        upright = img
        upright.load()
    else:
        upright = img.convert("RGB")
        img.close()  # the decoded original is not kept beside its copy
    buf = io.BytesIO()
    upright.save(buf, "JPEG", quality=quality, exif=exif, icc_profile=icc_profile)
    upright.close()
    sys.stdout.buffer.write(buf.getvalue())
    return 0


if __name__ == "__main__":
    sys.exit(main())
