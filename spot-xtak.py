#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import asyncio
import logging
import sys
import tempfile
import xml.etree.ElementTree as ET

from configparser import ConfigParser
from pathlib import Path
from urllib.parse import quote

try:
    import aiohttp
except ImportError as exc:
    sys.exit("spot-xtak requires aiohttp: python3 -m pip install aiohttp")

import pytak


SPOT_API_BASE = (
    "https://api.findmespot.com/spot-main-web/consumer/rest-api/2.0/public/feed"
)

SPOT_NO_FIX = -99999.0

POSITION_TYPES = {
    "OK",
    "TRACK",
    "EXTREME-TRACK",
    "UNLIMITED-TRACK",
    "NEWMOVEMENT",
    "CUSTOM",
    "POI",
    "HELP",
}

DEFAULTS = {
    "SPOT_FEED_KEY_FILE": "spot_feed_id.txt",
    "SPOT_FEED_PASSWORD": "",
    "SPOT_POLL_INTERVAL": "150",
    "SPOT_COT_TYPE": "a-f-G-E-S",
    "SPOT_COT_STALE": "",
}


def read_feed_key(path: str) -> str:
    key_path = Path(path)
    if not key_path.is_absolute():
        key_path = Path(__file__).resolve().parent / key_path
    if not key_path.exists():
        raise FileNotFoundError(
            f"SPOT feed key file not found: {key_path} — create it and paste "
            "your SPOT shared-page feed ID (GLID) inside."
        )
    key = key_path.read_text(encoding="utf-8").strip().splitlines()[0].strip()
    if not key:
        raise ValueError(f"SPOT feed key file is empty: {key_path}")
    return key


def maybe_convert_truststore(config) -> None:
    cafile = config.get("PYTAK_TLS_CLIENT_CAFILE", "")
    if not cafile or not cafile.lower().endswith((".p12", ".pfx")):
        return

    try:
        from cryptography.hazmat.primitives.serialization import (
            pkcs12,
            Encoding,
        )
    except ImportError:
        sys.exit(
            "A .p12 truststore was given but the 'cryptography' package is "
            "missing: python3 -m pip install cryptography"
        )

    password = config.get("PYTAK_TLS_TRUSTSTORE_PASSWORD", "") or config.get(
        "PYTAK_TLS_CLIENT_PASSWORD", ""
    )
    p12_data = Path(cafile).read_bytes()
    _, cert, extra = pkcs12.load_key_and_certificates(
        p12_data, password.encode() if password else None
    )

    pem_chunks = []
    for c in ([cert] if cert else []) + list(extra or []):
        pem_chunks.append(c.public_bytes(Encoding.PEM))

    if not pem_chunks:
        sys.exit(f"No certificates found inside truststore: {cafile}")

    tmp = tempfile.NamedTemporaryFile(
        prefix="spot-xtak_truststore_", suffix=".pem", delete=False
    )
    tmp.write(b"".join(pem_chunks))
    tmp.close()
    config["PYTAK_TLS_CLIENT_CAFILE"] = tmp.name
    logging.getLogger("spot-xtak").info(
        "Converted truststore %s -> %s", cafile, tmp.name
    )


def spot_to_cot(
    messenger_name: str,
    lat: float,
    lon: float,
    date_time: str,
    cot_type: str,
    stale: int,
    altitude: str = "",
    battery_state: str = "",
) -> bytes:
    uid = f"SPOT.{messenger_name}"
    callsign = f"{messenger_name} {date_time}"

    try:
        hae = str(float(altitude)) if altitude else "0.0"
    except ValueError:
        hae = "0.0"

    event = ET.Element("event")
    event.set("version", "2.0")
    event.set("type", cot_type)
    event.set("uid", uid)
    event.set("how", "m-g")
    event.set("time", pytak.cot_time())
    event.set("start", pytak.cot_time())
    event.set("stale", pytak.cot_time(stale))

    point = ET.SubElement(event, "point")
    point.set("lat", str(lat))
    point.set("lon", str(lon))
    point.set("hae", hae)
    point.set("ce", "9999999.0")
    point.set("le", "9999999.0")

    detail = ET.SubElement(event, "detail")
    contact = ET.SubElement(detail, "contact")
    contact.set("callsign", callsign)

    remarks = ET.SubElement(detail, "remarks")
    remark_text = f"SPOT device {messenger_name}, last report {date_time}"
    if battery_state:
        remark_text += f", battery {battery_state}"
    remarks.text = remark_text

    return ET.tostring(event)


class SpotWorker(pytak.QueueWorker):
    def __init__(self, queue, config):
        super().__init__(queue, config)
        self.feed_id = read_feed_key(config.get("SPOT_FEED_KEY_FILE"))
        self.feed_password = config.get("SPOT_FEED_PASSWORD", "")
        self.poll_interval = max(
            int(config.get("SPOT_POLL_INTERVAL", "150")), 30
        )
        self.cot_type = config.get("SPOT_COT_TYPE", "a-f-G-E-S")
        stale_cfg = config.get("SPOT_COT_STALE", "")
        self.cot_stale = (
            int(stale_cfg) if stale_cfg else self.poll_interval * 2
        )

    @property
    def feed_url(self) -> str:
        url = f"{SPOT_API_BASE}/{self.feed_id}/message.xml"
        if self.feed_password:
            url += f"?feedPassword={quote(self.feed_password)}"
        return url

    async def handle_data(self, data: bytes) -> None:
        try:
            root = ET.fromstring(data)
        except ET.ParseError as exc:
            self._logger.warning("Unparseable SPOT response: %s", exc)
            return

        error = root.find(".//errors/error")
        if error is not None:
            code = error.findtext("code", "?")
            text = error.findtext("text", "") or error.findtext(
                "description", ""
            )
            self._logger.warning("SPOT API error %s: %s", code, text)
            return

        messages = root.findall(".//message")
        if not messages:
            self._logger.info("SPOT feed returned no messages.")
            return

        latest_by_device = {}
        for msg in messages:
            msg_type = msg.findtext("messageType", "")
            if msg_type not in POSITION_TYPES:
                continue

            key = msg.findtext("messengerId") or msg.findtext(
                "messengerName", "SPOT"
            )
            try:
                ts = int(msg.findtext("unixTime", "0"))
            except ValueError:
                ts = 0

            if key not in latest_by_device or ts > latest_by_device[key][0]:
                latest_by_device[key] = (ts, msg)

        if not latest_by_device:
            self._logger.info(
                "No position-bearing messages in feed (after type filter)."
            )
            return

        for _ts, msg in latest_by_device.values():
            name = msg.findtext("messengerName", "SPOT")
            date_time = msg.findtext("dateTime", "")
            msg_type = msg.findtext("messageType", "?")
            altitude = msg.findtext("altitude", "")
            battery_state = msg.findtext("batteryState", "")

            try:
                lat = float(msg.findtext("latitude", str(SPOT_NO_FIX)))
                lon = float(msg.findtext("longitude", str(SPOT_NO_FIX)))
            except ValueError:
                self._logger.warning("Bad coordinates for %s, skipping.", name)
                continue

            if lat <= -999.0 or lon <= -999.0:
                self._logger.info(
                    "%s: latest message (%s) has no GPS fix, skipping.",
                    name,
                    msg_type,
                )
                continue

            cot = spot_to_cot(
                name,
                lat,
                lon,
                date_time,
                self.cot_type,
                self.cot_stale,
                altitude,
                battery_state,
            )
            self._logger.info(
                "Sending CoT: %s @ %s,%s (%s, %s)",
                name,
                lat,
                lon,
                date_time,
                msg_type,
            )
            await self.put_queue(cot)

    async def poll_once(self, session: aiohttp.ClientSession) -> None:
        async with session.get(
            self.feed_url,
            timeout=aiohttp.ClientTimeout(total=30),
            headers={"Accept-Encoding": "gzip, deflate"},
        ) as resp:
            body = await resp.read()
            if resp.status != 200:
                self._logger.warning(
                    "SPOT API HTTP %s: %s", resp.status, body[:200]
                )
            await self.handle_data(body)

    async def run(self, _=-1) -> None:
        self._logger.info(
            "Polling SPOT feed every %ss -> %s",
            self.poll_interval,
            self.feed_url.split("?")[0],
        )
        async with aiohttp.ClientSession() as session:
            while True:
                try:
                    await self.poll_once(session)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._logger.warning("Poll failed: %s", exc)
                await asyncio.sleep(self.poll_interval)


async def main(config) -> None:
    clitool = pytak.CLITool(config)
    await clitool.setup()
    clitool.add_tasks(set([SpotWorker(clitool.tx_queue, config)]))
    await clitool.run()


def cli() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "-c",
        "--CONFIG_FILE",
        default="config.ini",
        help="Path to config file (default: ./config.ini)",
    )
    args = parser.parse_args()

    parser_ini = ConfigParser(defaults=DEFAULTS)
    config_path = Path(args.CONFIG_FILE)
    if config_path.exists():
        parser_ini.read(config_path)
    if not parser_ini.has_section("spot-xtak"):
        parser_ini.add_section("spot-xtak")
    config = parser_ini["spot-xtak"]

    logging.basicConfig(
        level=logging.DEBUG if config.getboolean("DEBUG", False) else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    maybe_convert_truststore(config)

    try:
        asyncio.run(main(config))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    cli()
