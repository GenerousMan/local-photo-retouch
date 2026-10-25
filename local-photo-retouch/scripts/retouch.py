#!/usr/bin/env python3
"""Crop, mask, composite and verify localized photo edits. Requires Pillow/NumPy."""

import argparse
import hashlib
import json
import math
from io import BytesIO
from pathlib import Path

import numpy as np
from PIL import Image, ImageCms, ImageDraw, ImageFilter, ImageOps


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read_photo(path):
    with Image.open(path) as src:
        if getattr(src, "n_frames", 1) != 1 or src.mode not in ("RGB", "RGBA"):
            raise ValueError("Expected a static 8-bit RGB/RGBA photo; decide conversion separately.")
        # Pillow may expose 16-bit RGB PNG/TIFF as RGB after silently downcasting.
        if src.format == "PNG":
            with Path(path).open("rb") as stream:
                header = stream.read(25)
            if len(header) < 25 or header[24] != 8:
                raise ValueError("Expected an 8-bit photo; convert high-bit-depth inputs explicitly.")
        if src.format == "TIFF":
            bits = src.tag_v2.get(258, (8,))
            if isinstance(bits, int):
                bits = (bits,)
            if any(value != 8 for value in bits):
                raise ValueError("Expected an 8-bit photo; convert high-bit-depth inputs explicitly.")
        image = ImageOps.exif_transpose(src)
        image.load()
        metadata = {k: image.info[k] for k in ("icc_profile", "dpi") if image.info.get(k)}
        exif = image.getexif()
        if exif:
            exif.pop(274, None)
            for tag, value in ((256, image.width), (257, image.height)):
                if tag in exif:
                    exif[tag] = value
            metadata["exif"] = exif.tobytes()
        return image.copy(), metadata


def check_new(*paths):
    resolved = [Path(p).resolve() for p in paths]
    if len(set(resolved)) != len(resolved):
        raise ValueError("Output paths must be distinct.")
    for p in resolved:
        if p.exists():
            raise ValueError(f"Refusing to overwrite {p}; use a new name.")


def save_png(image, path, **metadata):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as f:
        image.save(f, format="PNG", **metadata)


def save_json(value, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write("\n")


def checked_box(box, size):
    if len(box) != 4 or any(type(v) is not int for v in box):
        raise ValueError("Crop box must contain four integer coordinates.")
    l, t, r, b = box
    if not (0 <= l < r <= size[0] and 0 <= t < b <= size[1]):
        raise ValueError("Crop box must lie inside the displayed original image.")
    return tuple(box)


def read_session(path):
    session = json.loads(Path(path).read_text(encoding="utf-8"))
    if session.get("version") != 1:
        raise ValueError("Unsupported session version.")
    if digest(session["source"]) != session["source_sha256"]:
        raise ValueError("Source changed since cropping. Start a fresh session.")
    source, metadata = read_photo(session["source"])
    if list(source.size) != session["size"] or source.mode != session["mode"]:
        raise ValueError("Source dimensions or mode do not match the crop session.")
    return session, source, metadata, checked_box(session["box"], source.size)


def crop_photo(source_path, box, workdir):
    source_path = Path(source_path).resolve()
    source, metadata = read_photo(source_path)
    box = checked_box(box, source.size)
    workdir = Path(workdir)
    crop_path, selection_path, session_path = [workdir / n for n in ("crop.png", "selection.png", "session.json")]
    check_new(crop_path, selection_path, session_path)
    profile = {k: v for k, v in metadata.items() if k == "icc_profile"}
    save_png(source.crop(box), crop_path, **profile)
    preview = source.convert("RGB")
    preview.thumbnail((1600, 1600))
    sx, sy = preview.width / source.width, preview.height / source.height
    draw_box = (round(box[0] * sx), round(box[1] * sy), round((box[2] - 1) * sx), round((box[3] - 1) * sy))
    ImageDraw.Draw(preview).rectangle(draw_box, outline=(255, 205, 40), width=3)
    save_png(preview, selection_path, **profile)
    session = {
        "version": 1, "source": str(source_path), "source_sha256": digest(source_path),
        "size": list(source.size), "mode": source.mode, "box": list(box),
        "coordinate_space": "EXIF-oriented displayed image; right/bottom exclusive",
    }
    save_json(session, session_path)
    return {"session": str(session_path.resolve()), "crop": str(crop_path.resolve()), "size": list(source.size)}


def polygon_points(polygon, size):
    if not isinstance(polygon, list) or len(polygon) < 3:
        raise ValueError("Each polygon needs at least three points.")
    result = []
    for point in polygon:
        if not isinstance(point, list) or len(point) != 2:
            raise ValueError("Polygon points must be [x, y].")
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in point):
            raise ValueError("Polygon coordinates must be finite numbers.")
        x, y = point
        if not (0 <= x < size[0] and 0 <= y < size[1]):
            raise ValueError("Polygon point lies outside the crop.")
        result.append((x, y))
    return result


def make_mask(session_path, spec_path, output):
    _, _, _, box = read_session(session_path)
    size = (box[2] - box[0], box[3] - box[1])
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    if not spec.get("include"):
        raise ValueError("Mask spec must have nonempty include polygons.")
    feather, cutoff = spec.get("feather", 0), spec.get("cutoff", 3)
    if isinstance(feather, bool) or not isinstance(feather, (int, float)) or not math.isfinite(feather) or feather < 0:
        raise ValueError("feather must be a finite nonnegative number.")
    if type(cutoff) is not int or not 1 <= cutoff <= 255:
        raise ValueError("cutoff must be an integer from 1 to 255.")
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    for polygon in spec["include"]:
        draw.polygon(polygon_points(polygon, size), fill=255)
    if feather:
        mask = mask.filter(ImageFilter.GaussianBlur(feather))
    pixels = np.asarray(mask).copy()
    pixels[pixels < cutoff] = 0
    mask = Image.fromarray(pixels)
    draw = ImageDraw.Draw(mask)
    for polygon in spec.get("protect", []):
        draw.polygon(polygon_points(polygon, size), fill=0)
    if not mask.getbbox():
        raise ValueError("Mask contains no editable pixels.")
    check_new(output)
    save_png(mask, output)
    return {"mask": str(Path(output).resolve()), "size": list(size), "support_bbox": mask.getbbox()}


def read_mask(path, size):
    with Image.open(path) as image:
        if image.mode != "L" or image.size != size:
            raise ValueError("Mask must be an 8-bit grayscale image matching the original crop size.")
        mask = image.copy()
    if not mask.getbbox():
        raise ValueError("Mask contains no editable pixels.")
    return mask


def changed_bbox(changed):
    yy, xx = np.nonzero(changed)
    return [int(xx.min()), int(yy.min()), int(xx.max()) + 1, int(yy.max()) + 1] if len(xx) else None


def match_patch_profile(patch, patch_metadata, source_metadata):
    source_icc = source_metadata.get("icc_profile")
    patch_icc = patch_metadata.get("icc_profile")
    if source_icc == patch_icc:
        return patch
    # Untagged generated images and untagged RGB sources are interpreted as sRGB.
    src_profile = ImageCms.ImageCmsProfile(BytesIO(patch_icc)) if patch_icc else ImageCms.createProfile("sRGB")
    dst_profile = ImageCms.ImageCmsProfile(BytesIO(source_icc)) if source_icc else ImageCms.createProfile("sRGB")
    try:
        converted = ImageCms.profileToProfile(patch.convert("RGB"), src_profile, dst_profile, outputMode="RGB")
    except ImageCms.PyCMSError as exc:
        raise ValueError(f"Cannot match patch to source color profile: {exc}") from exc
    if patch.mode == "RGBA":
        converted.putalpha(patch.getchannel("A"))
    return converted


def verify_photo(session_path, mask_path, output):
    session, source, metadata, box = read_session(session_path)
    mask = read_mask(mask_path, (box[2] - box[0], box[3] - box[1]))
    final, final_metadata = read_photo(output)
    report = {
        "source_size": list(source.size), "output_size": list(final.size),
        "same_size": source.size == final.size, "same_mode": source.mode == final.mode,
        "same_icc_profile": metadata.get("icc_profile") == final_metadata.get("icc_profile"),
        "source_sha256": session["source_sha256"], "output_sha256": digest(output),
        "mask_sha256": digest(mask_path), "box": list(box), "passed": False,
    }
    if not report["same_size"] or not report["same_mode"]:
        report["error"] = "Output size or mode differs from the displayed source."
        return report
    original, saved = np.asarray(source), np.asarray(final)
    changed = np.any(original != saved, axis=2)
    allowed = np.zeros(changed.shape, dtype=bool)
    l, t, r, b = box
    allowed[t:b, l:r] = np.asarray(mask) > 0
    outside = int(np.count_nonzero(changed & ~allowed))
    alpha_preserved = source.mode != "RGBA" or bool(np.array_equal(original[:, :, 3], saved[:, :, 3]))
    count = int(np.count_nonzero(changed))
    report.update({
        "changed_pixels": count, "has_changes": count > 0, "changed_bbox": changed_bbox(changed),
        "mask_support_pixels": int(np.count_nonzero(allowed)), "changed_pixels_outside_mask": outside,
        "alpha_preserved": alpha_preserved,
        "passed": outside == 0 and report["same_icc_profile"] and alpha_preserved,
        "scope": "Decoded pixels only; visual quality and requested effect require inspection.",
    })
    return report


def compose_photo(session_path, patch_path, mask_path, output, resize_patch=False):
    _, source, metadata, box = read_session(session_path)
    output = Path(output)
    if output.suffix.lower() != ".png":
        raise ValueError("Use PNG for the pixel-preserving master.")
    report_path = output.with_suffix(".verification.json")
    check_new(output, report_path)
    size = (box[2] - box[0], box[3] - box[1])
    mask = read_mask(mask_path, size)
    patch, patch_metadata = read_photo(patch_path)
    patch = match_patch_profile(patch, patch_metadata, metadata)
    original_patch_size = patch.size
    if patch.size != size:
        if not resize_patch or patch.width * size[1] != patch.height * size[0]:
            raise ValueError("Align the patch first. --resize-patch only permits equal-aspect scaling.")
        patch = patch.resize(size, Image.Resampling.LANCZOS)
    if patch.mode == "RGBA" and np.any((np.asarray(patch)[:, :, 3] != 255) & (np.asarray(mask) > 0)):
        raise ValueError("Generated patch must be opaque throughout the edit mask.")
    region = Image.composite(patch.convert("RGB"), source.crop(box).convert("RGB"), mask)
    if source.mode == "RGBA":
        region.putalpha(source.crop(box).getchannel("A"))
    final = source.copy()
    final.paste(region, box[:2])
    save_png(final, output, **metadata)
    report = verify_photo(session_path, mask_path, output)
    report.update({"patch_sha256": digest(patch_path), "patch_original_size": list(original_patch_size), "patch_resized": original_patch_size != size})
    save_json(report, report_path)
    if not report["passed"]:
        raise ValueError(f"Verification failed; inspect {report_path} before delivery.")
    return {"output": str(output.resolve()), "report": str(report_path.resolve()), **report}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    crop = commands.add_parser("crop", help="Record an immutable source and crop coordinates.")
    crop.add_argument("--source", required=True)
    crop.add_argument("--box", nargs=4, type=int, required=True, metavar=("LEFT", "TOP", "RIGHT", "BOTTOM"))
    crop.add_argument("--workdir", required=True)
    mask = commands.add_parser("mask", help="Build a feathered mask with exact protected polygons.")
    mask.add_argument("--session", required=True)
    mask.add_argument("--spec", required=True)
    mask.add_argument("--output", required=True)
    compose = commands.add_parser("compose", help="Composite a patch and verify the saved PNG.")
    compose.add_argument("--session", required=True)
    compose.add_argument("--patch", required=True)
    compose.add_argument("--mask", required=True)
    compose.add_argument("--output", required=True)
    compose.add_argument("--resize-patch", action="store_true")
    verify = commands.add_parser("verify", help="Recheck an image against the original and edit mask.")
    verify.add_argument("--session", required=True)
    verify.add_argument("--mask", required=True)
    verify.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        if args.command == "crop":
            result = crop_photo(args.source, args.box, args.workdir)
        elif args.command == "mask":
            result = make_mask(args.session, args.spec, args.output)
        elif args.command == "compose":
            result = compose_photo(args.session, args.patch, args.mask, args.output, args.resize_patch)
        else:
            result = verify_photo(args.session, args.mask, args.output)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result.get("passed") is False else 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(2, f"Error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
