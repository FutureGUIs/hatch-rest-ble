# Hatch branding

The integration uses the Hatch mark selected from Hatch's website and approved by the user before inclusion. The mark belongs to Hatch; this community integration is not an official Hatch integration.

- Official source page: [Hatch blog](https://www.hatch.co/blog/hatch-restore-perfect-day-alarm).
- Original asset: [Hatch icon PNG](https://www.datocms-assets.com/98401/1743535597-hatch-icon-8-1-edited.png), 564×564 pixels.
- Original SHA-256: `8b98317c9978e18bc6606bae56dfdc75071ae0d18d4d0892fec3e589377dba91`.

The exports trim the original excess navy padding to a square crop, preserve the artwork's aspect ratio and colors, and leave padding around the mark. Both files are optimized, lossless PNGs:

| File within `custom_components/hatch_rest_ble` | Dimensions |
| --- | --- |
| `brand/icon.png` | 256×256 |
| `brand/icon@2x.png` | 512×512 |

These follow the [Home Assistant local branding layout](https://developers.home-assistant.io/docs/core/integration/brand_images/) and [image specifications](https://github.com/home-assistant/brands#image-specification). Local branding is supported from HA 2026.3; the integration's development baseline remains HA 2026.10. The navy background is part of the approved artwork, so the same icon is used for light and dark themes.
