# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import demo_pb2


def _ad(redirect_url: str, text: str):
    return demo_pb2.Ad(redirect_url=redirect_url, text=text)


def create_ads_map() -> dict[str, tuple]:
    binoculars = _ad("/product/2ZYFJ3GM2N", "Roof Binoculars for sale. 50% off.")
    explorer_telescope = _ad("/product/66VCHSJNUP", "Starsense Explorer Refractor Telescope for sale. 20% off.")
    color_imager = _ad("/product/0PUK6V6EV0", "Solar System Color Imager for sale. 30% off.")
    optical_tube = _ad("/product/9SIQT8TOJO", "Optical Tube Assembly for sale. 10% off.")
    travel_telescope = _ad(
        "/product/1YMWWN1N4O",
        "Eclipsmart Travel Refractor Telescope for sale. Buy one, get second kit for free",
    )
    solar_filter = _ad("/product/6E92ZMYYFZ", "Solar Filter for sale. Buy two, get third one for free")
    cleaning_kit = _ad("/product/L9ECAV7KIM", "Lens Cleaning Kit for sale. Buy one, get second one for free")
    return {
        "binoculars": (binoculars,),
        "telescopes": (explorer_telescope,),
        "accessories": (color_imager, solar_filter, cleaning_kit),
        "assembly": (optical_tube,),
        "travel": (travel_telescope,),
    }
