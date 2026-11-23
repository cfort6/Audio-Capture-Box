"""Prepare the boot image: turn a PNG into the exact bytes the framebuffer expects.

Run once at install time (python3 -m acb.bootsplash IN.png OUT.fb). At boot, a tiny
shell script just copies OUT.fb to /dev/fb0 as soon as the screen exists, so nothing
here can stop the Pi from starting.
"""

import os
import sys


def prepare(png, out):
    os.environ["SDL_VIDEODRIVER"] = "dummy"
    import numpy as np
    import pygame

    from .fbdisplay import FrameBuffer

    pygame.display.init()
    fb = FrameBuffer()
    img = pygame.image.load(png)
    surf = pygame.Surface((fb.w, fb.h))
    surf.fill((14, 16, 19))
    surf.blit(img, img.get_rect(center=(fb.w // 2, fb.h // 2)))
    buf = bytearray(fb.stride * fb.h)
    real = fb.dst
    fb.dst = np.ndarray((fb.h, fb.stride), dtype=np.uint8, buffer=buf)
    fb.show(surf)
    fb.dst = real
    fb.close()
    with open(out, "wb") as f:
        f.write(buf)
    print(f"Boot image prepared for a {fb.w}x{fb.h} {fb.bpp}-bit screen: {out}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: python3 -m acb.bootsplash IN.png OUT.fb")
    prepare(sys.argv[1], sys.argv[2])
