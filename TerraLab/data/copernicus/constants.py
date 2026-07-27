"""Copernicus HRIM service identity and pipeline constants."""

from __future__ import annotations

PRODUCT_URL = (
    "https://land.copernicus.eu/en/products/european-image-mosaic/"
    "high-resolution-image-mosaic-2018-true-colour-10m"
)
IMAGE_SERVER_URL = (
    "https://image.discomap.eea.europa.eu/arcgis/rest/services/"
    "GioLand/HRIM_HR_TrueColour_2018/ImageServer"
)
WMS_URL = (
    "https://image.discomap.eea.europa.eu/arcgis/services/"
    "GioLand/HRIM_HR_TrueColour_2018/ImageServer/WMSServer"
)
DATA_POLICY_URL = "https://land.copernicus.eu/en/data-policy"

PRODUCT_NAME = "High Resolution Image Mosaic 2018 True Colour, 10 m"
ATTRIBUTION = (
    "European Union's Copernicus Land Monitoring Service information. "
    "High Resolution Image Mosaic 2018 True Colour, 10 m."
)
ADAPTED_ATTRIBUTION = (
    "Generated using European Union's Copernicus Land Monitoring Service "
    "information. Data adapted by TerraLab."
)

CRS_WGS84 = "EPSG:4326"
CRS_PRODUCT = "EPSG:3035"
NOMINAL_RESOLUTION_M = 10.0
_SERVICE_COVERAGE_COORDS = (
    -31.385193,
    27.546328,
    44.932709,
    71.274744,
)

MAX_IMAGE_WIDTH = 15_000
MAX_IMAGE_HEIGHT = 4_100
MAX_MOSAIC_IMAGE_COUNT = 20
DEFAULT_FRAGMENT_WIDTH = 4_000
DEFAULT_FRAGMENT_HEIGHT = 4_000
# ``pixelType=U8`` on this ImageServer is a numeric cast, not a display
# rendering: the native U16 values are clipped at 255 and the result is
# effectively white.  TerraLab therefore always transports the native U16
# samples and performs one selection-wide conversion when an U8 output was
# requested.
SERVICE_PIXEL_TYPE = "U16"
U8_STRETCH_LOW_PERCENTILE = 2.0
U8_STRETCH_HIGH_PERCENTILE = 98.0
U8_STRETCH_METHOD = "selection_global_per_band_percentile_2_98"
DOWNLOAD_PIPELINE_VERSION = 2
# The service refuses mosaics above 20 source images.  Real queries against
# this product found 200 km blocks intersecting 25 Sentinel-2 tiles, while
# 150 km blocks remained below the limit (15 in the sampled worst cases) and
# still permit explicit validation at the documented 15,000-pixel width when
# exporting at 10 m.  Keep this physical ceiling in addition to the 4,000-pixel
# default so coarse-resolution exports cannot silently use 200+ km requests.
MAX_FRAGMENT_SPAN_M = 150_000.0
