"""Build reduced levels once; render each viewport from the nearest level."""
from PIL import Image


def build_levels(image):
    levels = []
    source = image
    factor = 1
    while max(source.size) > 768:
        source = source.reduce(2)
        factor *= 2
        levels.append((factor, source))
    image.info['display_levels'] = levels
    return image


def render_viewport(image, box, size):
    scale = max(size[0] / (box[2] - box[0]), size[1] / (box[3] - box[1]))
    source, factor = image, 1
    for candidate_factor, candidate in image.info.get('display_levels', ()):
        if candidate_factor * scale > 1.15:
            break
        source, factor = candidate, candidate_factor
    region = (max(0, box[0] / factor), max(0, box[1] / factor),
              min(source.width, box[2] / factor), min(source.height, box[3] / factor))
    return source.resize(size, Image.Resampling.BILINEAR, box=region)
