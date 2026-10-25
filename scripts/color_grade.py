#!/usr/bin/env python3
"""Deterministic global color grading and comparison of actual editing stages."""
import argparse
import hashlib
import importlib.metadata
from io import BytesIO
import json
import math
from pathlib import Path
import platform

import numpy as np
from PIL import Image, ImageCms, ImageDraw, ImageFont
from retouch import read_photo, checked_box, check_new, save_png, save_json, digest


def runtime_versions():
    versions = {'python': platform.python_version()}
    for name in ('Pillow', 'numpy', 'opencv-python-headless', 'opencv-python'):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            pass
    return versions


def number(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f'{name} must be finite and in [{low}, {high}].')
    return float(value)


def normalize_recipe(raw):
    allowed = {'version', 'description', 'exposure_ev', 'linear_rgb_gains', 'l_curve', 'saturation', 'color_adjustments'}
    if not isinstance(raw, dict) or set(raw) - allowed:
        raise ValueError('Recipe contains unknown fields or is not an object.')
    if type(raw.get('version')) is not int or raw['version'] != 1:
        raise ValueError('Recipe version must be 1.')
    r = dict(raw)
    if not isinstance(r.get('description', ''), str):
        raise ValueError('description must be text.')
    r['exposure_ev'] = number(r.get('exposure_ev', 0), 'exposure_ev', -8, 8)
    gains = r.get('linear_rgb_gains', [1, 1, 1])
    if not isinstance(gains, list) or len(gains) != 3:
        raise ValueError('linear_rgb_gains requires three positive multipliers.')
    r['linear_rgb_gains'] = [number(v, 'gain', 0.001, 8) for v in gains]
    r['saturation'] = number(r.get('saturation', 1), 'saturation', 0, 3)
    curve = r.get('l_curve', [[0, 0], [100, 100]])
    if not isinstance(curve, list) or len(curve) < 2 or any(not isinstance(v, list) or len(v) != 2 for v in curve):
        raise ValueError('l_curve requires [input L, output L] pairs.')
    curve = [[number(x, 'curve input', 0, 100), number(y, 'curve output', 0, 100)] for x, y in curve]
    if curve[0][0] != 0 or curve[-1][0] != 100 or any(b[0] <= a[0] or b[1] < a[1] for a, b in zip(curve, curve[1:])):
        raise ValueError('Curve inputs must increase from 0 to 100; outputs must be nondecreasing.')
    r['l_curve'] = curve
    edits = r.get('color_adjustments', [])
    if not isinstance(edits, list):
        raise ValueError('color_adjustments must be a list.')
    normalized = []
    for edit in edits:
        if not isinstance(edit, dict) or set(edit) - {'name', 'select', 'a_scale', 'b_scale', 'a_offset', 'b_offset'}:
            raise ValueError('Unknown color adjustment fields.')
        e = dict(edit)
        if not isinstance(e.get('name', ''), str):
            raise ValueError('Adjustment name must be text.')
        select = e.get('select', {})
        if not isinstance(select, dict) or set(select) - {'l', 'a', 'b'}:
            raise ValueError('Selections use only l, a and b.')
        bands = {}
        for channel, band in select.items():
            lo, hi = (0, 100) if channel == 'l' else (-128, 128)
            if not isinstance(band, list) or len(band) != 4:
                raise ValueError('Each band requires [outer low, inner low, inner high, outer high].')
            band = [number(v, channel, lo, hi) for v in band]
            if band != sorted(band):
                raise ValueError('Band bounds must be nondecreasing.')
            bands[channel] = band
        e['select'] = bands
        for channel in ('a', 'b'):
            e[channel + '_scale'] = number(e.get(channel + '_scale', 1), channel + '_scale', 0, 3)
            e[channel + '_offset'] = number(e.get(channel + '_offset', 0), channel + '_offset', -64, 64)
        normalized.append(e)
    r['color_adjustments'] = normalized
    return r


def is_identity(r):
    return (r['exposure_ev'] == 0 and r['linear_rgb_gains'] == [1, 1, 1] and r['saturation'] == 1
            and all(x == y for x, y in r['l_curve'])
            and all(e['a_scale'] == e['b_scale'] == 1 and e['a_offset'] == e['b_offset'] == 0 for e in r['color_adjustments']))


def band_weight(x, bounds):
    a, b, c, d = bounds
    def smooth(t):
        t = np.clip(t, 0, 1)
        return t*t*(3-2*t)
    rise = (x >= a).astype(np.float32) if a == b else smooth((x-a)/(b-a))
    fall = (x <= d).astype(np.float32) if c == d else 1-smooth((x-c)/(d-c))
    return rise * fall


def grade_rgb(rgb, recipe):
    """Process one pixel block. Selection masks are color-based, never spatial."""
    if is_identity(recipe):
        return rgb.copy(), 0, 0
    try:
        import cv2
    except ImportError as exc:
        raise ValueError('Grading requires opencv-python-headless, Pillow and NumPy in the selected runtime.') from exc
    data = rgb.astype(np.float32)/255
    clipped_input = 0
    if recipe['exposure_ev'] or recipe['linear_rgb_gains'] != [1, 1, 1]:
        linear = np.where(data <= 0.04045, data/12.92, ((data+0.055)/1.055)**2.4)
        linear *= np.asarray(recipe['linear_rgb_gains'], np.float32) * (2**recipe['exposure_ev'])
        clipped_input = int(np.count_nonzero(np.any(linear > 1, axis=2)))
        linear = np.clip(linear, 0, 1)
        data = np.where(linear <= 0.0031308, linear*12.92, 1.055*linear**(1/2.4)-0.055).astype(np.float32)
    lab = cv2.cvtColor(data, cv2.COLOR_RGB2LAB)
    original = lab.copy()
    curve = np.asarray(recipe['l_curve'])
    lab[:, :, 0] = np.interp(original[:, :, 0], curve[:, 0], curve[:, 1])
    lab[:, :, 1:] *= recipe['saturation']
    for edit in recipe['color_adjustments']:
        weight = np.ones(rgb.shape[:2], np.float32)
        for channel, bounds in edit['select'].items():
            weight *= band_weight(original[:, :, {'l': 0, 'a': 1, 'b': 2}[channel]], bounds)
        for i, channel in ((1, 'a'), (2, 'b')):
            lab[:, :, i] += weight * (original[:, :, i]*(edit[channel+'_scale']-1)+edit[channel+'_offset'])
    # Convert through XYZ so out-of-sRGB-gamut pixels are observable before clamping.
    fy = (lab[:, :, 0]+16)/116
    fx = fy + lab[:, :, 1]/500
    fz = fy - lab[:, :, 2]/200
    def inverse(t):
        return np.where(t > 6/29, t**3, 3*(6/29)**2*(t-4/29))
    xyz = np.stack((inverse(fx)*0.950456, inverse(fy), inverse(fz)*1.088754), axis=2)
    matrix = np.array([[3.240479,-1.537150,-0.498535],[-0.969256,1.875991,0.041556],[0.055648,-0.204043,1.057311]],np.float32)
    linear = xyz @ matrix.T
    out_of_gamut = int(np.count_nonzero(np.any((linear < -1e-5) | (linear > 1+1e-5),axis=2)))
    linear = np.clip(linear, 0, 1)
    data = np.where(linear <= 0.0031308, linear*12.92, 1.055*linear**(1/2.4)-0.055)
    return np.rint(np.clip(data, 0, 1)*255).astype(np.uint8), clipped_input, out_of_gamut


def profile_encoding(profile):
    """Compare ICC encoding data, ignoring names, timestamps and other metadata."""
    data = profile.tobytes()
    descriptive_tags = {b'desc', b'cprt', b'dmnd', b'dmdd', b'meta'}
    tags = []
    count = int.from_bytes(data[128:132], 'big')
    for pos in range(132, 132 + count * 12, 12):
        signature = data[pos:pos + 4]
        if signature in descriptive_tags:
            continue
        offset = int.from_bytes(data[pos + 4:pos + 8], 'big')
        size = int.from_bytes(data[pos + 8:pos + 12], 'big')
        tags.append((signature, data[offset:offset + size]))
    return data[8:24], data[64:80], sorted(tags)


def as_srgb(image, metadata, policy='require-srgb', assume=False):
    icc = metadata.get('icc_profile')
    if not icc:
        if not assume:
            raise ValueError('Input has no ICC profile. Verify its color space, then use --assume-srgb if appropriate.')
        return image.copy(), dict(metadata), 'Untagged input explicitly interpreted as sRGB'
    profile = ImageCms.ImageCmsProfile(BytesIO(icc))
    description = ImageCms.getProfileDescription(profile).strip()
    target = ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB'))
    if profile_encoding(profile) == profile_encoding(target):
        return image.copy(), dict(metadata), description
    if policy != 'convert-srgb':
        raise ValueError(f'Input profile is {description!r}. Use --profile-policy convert-srgb for explicit conversion.')
    converted = ImageCms.profileToProfile(image.convert('RGB'), profile, target, outputMode='RGB')
    if image.mode == 'RGBA':
        converted.putalpha(image.getchannel('A'))
    metadata = {**metadata, 'icc_profile': target.tobytes()}
    return converted, metadata, description + ' -> sRGB'


def sample_stats(image):
    # Stride sampling preserves actual encoded values; no image resizing in diagnostics.
    step = max(1, int(math.ceil(math.sqrt(image.width*image.height/500000))))
    a = np.asarray(image.convert('RGB'))[::step, ::step]
    y = a @ np.array([0.2126, 0.7152, 0.0722])
    return {'sample_stride': step, 'sample_pixels': int(y.size),
            'encoded_luma_quantiles_0_1_50_99_100': np.quantile(y,[0,.01,.5,.99,1]).tolist(),
            'any_channel_at_0_percent': float(np.any(a==0,axis=2).mean()*100),
            'any_channel_at_255_percent': float(np.any(a==255,axis=2).mean()*100)}


def grade_photo(source, recipe_path, output, preview_max_edge=None, profile_policy='require-srgb', assume_srgb=False, jpeg=None):
    output = Path(output)
    if output.suffix.lower() != '.png':
        raise ValueError('Use PNG for the master or working preview; use --jpeg for an optional copy.')
    report_path = output.with_suffix('.color.json')
    paths = [output, report_path] + ([jpeg] if jpeg else [])
    check_new(*paths)
    if jpeg and Path(jpeg).suffix.lower() not in ('.jpg', '.jpeg'):
        raise ValueError('--jpeg must name a JPEG file.')
    recipe = normalize_recipe(json.loads(Path(recipe_path).read_text(encoding='utf-8')))
    raw, metadata = read_photo(source)
    source_icc = metadata.get('icc_profile')
    src, metadata, profile_note = as_srgb(raw, metadata, profile_policy, assume_srgb)
    if jpeg and src.mode == 'RGBA':
        raise ValueError('RGBA master supported; choose an explicit background separately before JPEG export.')
    if preview_max_edge is not None:
        if preview_max_edge <= 0:
            raise ValueError('preview-max-edge must be positive.')
        src.thumbnail((preview_max_edge,preview_max_edge),Image.Resampling.LANCZOS)
        metadata.pop('exif',None)
    result = Image.new('RGB',src.size)
    exposure_clipped = gamut_clipped = 0
    for top in range(0,src.height,384):
        box = (0,top,src.width,min(top+384,src.height))
        block, count1, count2 = grade_rgb(np.asarray(src.crop(box).convert('RGB')),recipe)
        exposure_clipped += count1
        gamut_clipped += count2
        result.paste(Image.fromarray(block),(0,top))
    if src.mode == 'RGBA':
        result.putalpha(src.getchannel('A'))
    save_png(result,output,**metadata)
    reopened, saved_metadata = read_photo(output)
    if reopened.size != src.size or not np.array_equal(np.asarray(reopened),np.asarray(result)) or saved_metadata.get('icc_profile') != metadata.get('icc_profile'):
        raise ValueError('Saved master verification failed.')
    if jpeg:
        Path(jpeg).parent.mkdir(parents=True,exist_ok=True)
        with Path(jpeg).open('xb') as f:
            result.save(f,format='JPEG',quality=96,subsampling=0,**metadata)
    report = {'source':str(Path(source).resolve()),'source_sha256':digest(source),
       'output':str(output.resolve()),'output_sha256':digest(output),'recipe_sha256':digest(recipe_path),'recipe':recipe,
       'source_size':list(raw.size),'output_size':list(result.size),'preview':preview_max_edge is not None,
       'runtime_versions':runtime_versions(),
       'source_icc_sha256':hashlib.sha256(source_icc).hexdigest() if source_icc else None,
       'output_icc_sha256':hashlib.sha256(metadata['icc_profile']).hexdigest() if metadata.get('icc_profile') else None,
       'profile_handling':profile_note,'generative_model':False,'pixel_mapping':'pointwise; no geometric transforms in full-resolution mode',
       'same_dimensions_as_source':raw.size==result.size,'saved_pixels_verified':True,
       'alpha_preserved':src.mode!='RGBA' or bool(np.array_equal(np.asarray(src.getchannel('A')),np.asarray(reopened.getchannel('A')))),
       'before':sample_stats(src),'after':sample_stats(result),
       'exposure_or_gain_clipped_pixel_percent':exposure_clipped/(src.width*src.height)*100,
       'out_of_srgb_gamut_pixel_percent':gamut_clipped/(src.width*src.height)*100,
       'scope':'Checks establish encoding and pixel mapping, not aesthetic quality. Inspect skin, highlights, shadows and color transitions.'}
    save_json(report,report_path)
    return {'output':str(output),'report':str(report_path),'size':list(result.size),'preview':report['preview']}


def compare_photos(original, detail, graded, output, box=None, panel_width=800, font_path=None, title='', assume_srgb=False, no_local_edits=False):
    check_new(output)
    if panel_width < 100:
        raise ValueError('panel-width must be at least 100.')
    if no_local_edits:
        if not graded:
            raise ValueError('--no-local-edits requires --graded.')
        if detail:
            first, first_meta = read_photo(original)
            second, second_meta = read_photo(detail)
            if second.mode != first.mode or second.size != first.size or second_meta.get('icc_profile') != first_meta.get('icc_profile') or not np.array_equal(np.asarray(first), np.asarray(second)):
                raise ValueError('--no-local-edits requires identical original and detail inputs, including mode and ICC.')
        detail = None
    stages = [(original, '原图', 'Original')]
    if detail:
        stages.append((detail, '精修细节未调色', 'Detail retouch'))
    if graded:
        stages.append((graded, '精修且调色' if detail else '调色成片', 'Detail + color grade' if detail else 'Color grade'))
    if len(stages) < 2:
        raise ValueError('Comparison requires --detail or --graded.')
    images = []
    for path, _, _ in stages:
        im,meta = read_photo(path)
        im,_,_ = as_srgb(im,meta,'convert-srgb',assume_srgb)
        if im.mode == 'RGBA':
            background = Image.new('RGBA',im.size,'#f2f0eb')
            im = Image.alpha_composite(background,im)
        images.append(im.convert('RGB'))
    if len({im.size for im in images}) != 1:
        raise ValueError('Comparison stages must have equal displayed dimensions; do not silently align or stretch them.')
    box = checked_box(list(box),images[0].size) if box else (0,0,*images[0].size)
    height = round(panel_width*(box[3]-box[1])/(box[2]-box[0]))
    margin,gap,top = 18,18,110
    canvas = Image.new('RGB',(len(images)*panel_width+2*margin+(len(images)-1)*gap,height+top+margin),'#f2f0eb')
    if font_path:
        font = ImageFont.truetype(str(font_path),28)
        title_font = ImageFont.truetype(str(font_path),32)
        labels = [f'{i}. {stage[1]}' for i, stage in enumerate(stages, 1)]
    else:
        font = title_font = ImageFont.load_default()
        labels = [f'{i}. {stage[2]}' for i, stage in enumerate(stages, 1)]
        title = title.encode('ascii',errors='replace').decode('ascii')
    draw = ImageDraw.Draw(canvas)
    draw.text((margin,16),title,font=title_font,fill='#222222')
    for i,(im,label) in enumerate(zip(images,labels)):
        x = margin+i*(panel_width+gap)
        draw.text((x,64),label,font=font,fill='#333333')
        canvas.paste(im.crop(box).resize((panel_width,height),Image.Resampling.LANCZOS),(x,top))
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()
    path = Path(output)
    path.parent.mkdir(parents=True,exist_ok=True)
    if path.suffix.lower() == '.png':
        save_png(canvas,path,icc_profile=profile)
    elif path.suffix.lower() in ('.jpg','.jpeg'):
        with path.open('xb') as f:
            canvas.save(f,format='JPEG',quality=96,subsampling=0,icc_profile=profile)
    else:
        raise ValueError('Comparison output must be PNG or JPEG.')
    return {'output':str(path),'size':list(canvas.size),'source_box':list(box)}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    commands = p.add_subparsers(dest='command',required=True)
    g = commands.add_parser('grade',help='Apply a JSON color recipe; never synthesizes content.')
    for name in ('source','recipe','output'):
        g.add_argument('--'+name,required=True)
    g.add_argument('--preview-max-edge',type=int)
    g.add_argument('--profile-policy',choices=['require-srgb','convert-srgb'],default='require-srgb')
    g.add_argument('--assume-srgb',action='store_true')
    g.add_argument('--jpeg')
    c = commands.add_parser('compare',help='Compare the original with one or both completed editing stages.')
    for name in ('original','output'):
        c.add_argument('--'+name,required=True)
    for name in ('detail','graded'):
        c.add_argument('--'+name)
    c.add_argument('--box',nargs=4,type=int)
    c.add_argument('--panel-width',type=int,default=800)
    c.add_argument('--font')
    c.add_argument('--title',default='')
    c.add_argument('--assume-srgb',action='store_true')
    c.add_argument('--no-local-edits',action='store_true')
    args = p.parse_args()
    try:
        if args.command == 'grade':
            result = grade_photo(args.source,args.recipe,args.output,args.preview_max_edge,args.profile_policy,args.assume_srgb,args.jpeg)
        else:
            result = compare_photos(args.original,args.detail,args.graded,args.output,args.box,args.panel_width,args.font,args.title,args.assume_srgb,args.no_local_edits)
        print(json.dumps(result,ensure_ascii=False,indent=2))
    except (OSError,ValueError,KeyError,TypeError) as exc:
        p.exit(2,f'Error: {exc}\n')


if __name__ == '__main__':
    main()
