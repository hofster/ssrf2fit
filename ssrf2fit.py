#!/usr/bin/env python3
#
# ssrf2fit - Convert Subsurface (.ssrf) dive logs to Garmin FIT files
# Copyright (C) 2026  hofster
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""
SSRF (Subsurface XML) -> FIT Konverter fuer Garmin Connect.

Liest einen Subsurface-Nativexport (.ssrf) und erzeugt pro Tauchgang eine
.fit-Datei mit Tiefenprofil, korrekter Ortszeit (aus GPS abgeleitet), GPS-
Position und allen verfuegbaren Zusatzfeldern (Anzug, Gewaesserart, Blei,
Tank, Tauchcomputermodell, Notizen).

Aufruf:
  python3 ssrf2fit.py <input.ssrf> [--all | <index>] [--outdir DIR]
"""

import sys, os, re
import datetime as dt
from zoneinfo import ZoneInfo
import xml.etree.ElementTree as ET

from fit_tool.fit_file_builder import FitFileBuilder
from fit_tool.profile.messages.file_id_message import FileIdMessage
from fit_tool.profile.messages.device_info_message import DeviceInfoMessage
from fit_tool.profile.messages.record_message import RecordMessage
from fit_tool.profile.messages.session_message import SessionMessage
from fit_tool.profile.messages.lap_message import LapMessage
from fit_tool.profile.messages.activity_message import ActivityMessage
from fit_tool.profile.messages.dive_summary_message import DiveSummaryMessage
from fit_tool.profile.messages.dive_settings_message import DiveSettingsMessage
from fit_tool.profile.messages.event_message import EventMessage
from fit_tool.profile.profile_type import (
    Sport, SubSport, FileType, Manufacturer, Event, EventType, Activity, WaterType
)
try:  # Flaschendruck: nicht in jeder fit_tool-Version vorhanden
    from fit_tool.profile.messages.tank_update_message import TankUpdateMessage
    from fit_tool.profile.messages.tank_summary_message import TankSummaryMessage
except Exception:
    TankUpdateMessage = TankSummaryMessage = None
try:  # Gasmischung (dive_gas)
    from fit_tool.profile.messages.dive_gas_message import DiveGasMessage
except Exception:
    DiveGasMessage = None
from fit_tool.data_message import Field
from fit_tool.base_type import BaseType


def add_dive_field(msg, name, fid, value, scale=1000, units="m"):
    """Fuegt ein Tauch-Feld hinzu oder ersetzt ein bestehendes Feld mit gleicher ID."""
    if value is None:
        return
    # Bestehendes Feld mit gleicher field_id entfernen, um Kollisionen zu vermeiden
    msg.fields = [field for field in msg.fields if getattr(field, "field_id", None) != fid]
    
    raw = int(round(value * scale))
    f = Field(name=name, field_id=fid, base_type=BaseType.UINT32,
              scale=scale, offset=0, units=units, size=4)
    try:
        f.set_value(0, value)
    except Exception:
        pass
    enc = getattr(f, "encoded_values", None)
    if not enc or enc[0] != raw:
        f.encoded_values = [raw]
    msg.fields.append(f)


try:
    from timezonefinder import TimezoneFinder
    _TF = TimezoneFinder()
except Exception:
    _TF = None

FIT_EPOCH = dt.datetime(1989, 12, 31)
UNIX_EPOCH = dt.datetime(1970, 1, 1)
UTC = ZoneInfo("UTC")


# ---------- Helfer ----------

def to_fit_ms_from_utc(d):
    if d.tzinfo is not None:
        d = d.astimezone(UTC).replace(tzinfo=None)
    return int((d - UNIX_EPOCH).total_seconds() * 1000)

def to_fit_seconds_local(d):
    return int((d - FIT_EPOCH).total_seconds())

def resolve_tz(local_naive, lat, lon):
    tzname = None
    if _TF is not None and lat is not None and lon is not None:
        tzname = _TF.timezone_at(lat=lat, lng=lon)
    if tzname:
        tz = ZoneInfo(tzname)
        la = local_naive.replace(tzinfo=tz)
        return la.astimezone(UTC), local_naive, tzname
    return local_naive.replace(tzinfo=UTC), local_naive, None

def num(s):
    """Erste Zahl aus einem String wie '12.08 m' oder '193.0 bar'."""
    if s is None: return None
    m = re.search(r"-?\d+\.?\d*", s)
    return float(m.group()) if m else None

def parse_clock(s):
    """'27:40 min', '1:05:20 h' oder '27:40.5' -> Sekunden (float) oder None."""
    if not s: return None
    m = re.match(r"\s*(?:(\d+):)?(\d+):(\d+(?:\.\d+)?)", s)
    if m:
        h = int(m.group(1) or 0)
        return h * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    return None

def parse_duration(s):
    """'27:40 min' -> Sekunden."""
    if not s: return None
    v = parse_clock(s)
    if v is not None: return int(round(v))
    n = num(s)
    return int(n * 60) if n else None

def parse_sample_time(s):
    """'1:20 min' -> Sekunden."""
    v = parse_clock(s)
    return int(round(v)) if v is not None else None

def mean_depth_from_samples(samples):
    """Zeitgewichtete Durchschnittstiefe (Trapezregel) aus den Samples."""
    if len(samples) < 2: return None
    area = 0.0
    for a, b in zip(samples, samples[1:]):
        area += (a["depth"] + b["depth"]) / 2.0 * (b["t"] - a["t"])
    span = samples[-1]["t"] - samples[0]["t"]
    return area / span if span > 0 else None


# ---------- SSRF Parsing ----------

def load_sites(root):
    sites = {}
    for site in root.findall(".//site"):
        uuid = site.get("uuid")
        gps = site.get("gps")
        lat = lon = None
        if gps:
            parts = gps.split()
            if len(parts) == 2:
                lat, lon = float(parts[0]), float(parts[1])
        geo = site.find("geo")
        country = geo.get("value") if geo is not None else None
        sites[uuid] = {"name": site.get("name"), "lat": lat, "lon": lon,
                       "country": country}
    return sites

def extract_dive(dive, sites):
    d = {}
    d["number"] = dive.get("number")
    d["date"] = dive.get("date")
    d["time"] = dive.get("time")
    d["duration"] = parse_duration(dive.get("duration"))
    d["sac"] = num(dive.get("sac"))
    d["salinity"] = num(dive.get("watersalinity"))
    siteid = dive.get("divesiteid")
    d["site"] = sites.get(siteid, {}) if siteid else {}

    dt_str = f"{d['date']}T{d['time']}"
    d["start_local"] = dt.datetime.fromisoformat(dt_str) if d["date"] and d["time"] else None

    d["divemaster"] = (dive.findtext("divemaster") or "").strip() or None
    d["buddy"] = (dive.findtext("buddy") or "").strip() or None
    d["notes"] = (dive.findtext("notes") or "").strip() or None
    d["suit"] = (dive.findtext("suit") or "").strip() or None

    cyl = dive.find("cylinder")
    d["cyl_size"] = num(cyl.get("size")) if cyl is not None else None
    d["cyl_start"] = num(cyl.get("start")) if cyl is not None else None
    d["cyl_end"] = num(cyl.get("end")) if cyl is not None else None
    d["o2"] = num(cyl.get("o2")) if cyl is not None else None
    d["he"] = num(cyl.get("he")) if cyl is not None else None

    ws = dive.find("weightsystem")
    d["weight"] = num(ws.get("weight")) if ws is not None else None
    d["weight_desc"] = ws.get("description") if ws is not None else None

    dtemp = dive.find("divetemperature")
    d["airtemp"] = num(dtemp.get("air")) if dtemp is not None else None

    dc = dive.find("divecomputer")
    d["dc_model"] = dc.get("model") if dc is not None else None
    d["dc_serial"] = None
    if dc is not None:
        sn = None
        for ex in dc.findall("extradata"):
            if (ex.get("key") or "").lower() == "serial": sn = ex.get("value")
        if sn and sn.strip().isdigit():
            d["dc_serial"] = int(sn)
        elif dc.get("deviceid"):
            try: d["dc_serial"] = int(dc.get("deviceid"), 16)
            except ValueError: pass
        w = dc.find("water")
        if d["salinity"] is None and w is not None:
            d["salinity"] = num(w.get("salinity"))
    depth = dc.find("depth") if dc is not None else None
    d["maxdepth"] = num(depth.get("max")) if depth is not None else None
    d["avgdepth"] = num(depth.get("mean")) if depth is not None else None
    wtemp = dc.find("temperature") if dc is not None else None
    d["watertemp"] = num(wtemp.get("water")) if wtemp is not None else None

    samples = []
    src = dc if dc is not None else dive
    last_temp = None
    for sm in src.findall("sample"):
        t = parse_sample_time(sm.get("time"))
        dep = num(sm.get("depth"))
        tmp = num(sm.get("temp"))
        pr = num(sm.get("pressure0"))
        # Subsurface schreibt 'temp' nur bei Aenderung bzw. 0.0 als "kein Wert":
        # Werte <= 0 ignorieren und letzten gueltigen Wert fortschreiben.
        if tmp is not None and tmp > 0: last_temp = tmp
        if t is None or dep is None: continue
        samples.append({"t": t, "depth": dep, "temp": last_temp,
                        "pressure": pr if (pr is not None and pr > 0) else None})
    samples.sort(key=lambda x: x["t"])
    # Fuehrende Samples ohne Temperatur mit dem ersten bekannten Wert fuellen
    first_known = next((x["temp"] for x in samples if x["temp"] is not None), None)
    for x in samples:
        if x["temp"] is None: x["temp"] = first_known
    d["samples"] = samples
    prs = [x["pressure"] for x in samples if x["pressure"] is not None]
    d["p_start"] = prs[0] if prs else None
    d["p_end"] = prs[-1] if prs else None
    temps = [x["temp"] for x in samples if x["temp"] is not None]
    d["temp_avg"] = sum(temps) / len(temps) if temps else None
    d["temp_max"] = max(temps) if temps else None
    d["temp_min"] = min(temps) if temps else None

    if d["maxdepth"] is None and samples:
        d["maxdepth"] = max(s["depth"] for s in samples)
    if d["duration"] is None and samples:
        d["duration"] = samples[-1]["t"]
    # Mittlere Tiefe: Subsurface liefert bei importierten Computern oft 0 m
    if not d["avgdepth"] or d["avgdepth"] <= 0:
        d["avgdepth"] = mean_depth_from_samples(samples)
    if d["watertemp"] is not None and d["watertemp"] <= 0:
        d["watertemp"] = None
    if d["watertemp"] is None:
        d["watertemp"] = d["temp_avg"]
    return d


# ---------- FIT-Nachbearbeitung ----------
# fit_tool schreibt manuell angehaengte Felder (Tiefe in Session/Lap) als 0.
# Statt sich auf die Interna von fit_tool zu verlassen, werden die Bytes nach
# dem Schreiben direkt korrigiert und die Datei-CRC neu berechnet.

_CRC_T = [0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
          0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400]

def fit_crc(data, crc=0):
    for b in data:
        crc = (crc >> 4 & 0x0FFF) ^ _CRC_T[crc & 0xF] ^ _CRC_T[b & 0xF]
        crc = (crc >> 4 & 0x0FFF) ^ _CRC_T[crc & 0xF] ^ _CRC_T[b >> 4 & 0xF]
    return crc

def patch_fit_fields(path, patches):
    """patches: {(global_msg_num, field_id): rohwert_uint32}.
    Ueberschreibt 4-Byte-Felder in-place und aktualisiert die CRC.
    Gibt die Anzahl der gepatchten Felder zurueck."""
    import struct
    data = bytearray(open(path, "rb").read())
    hs = data[0]
    pos, end = hs, len(data) - 2
    defs, done = {}, 0
    while pos < end:
        h = data[pos]; pos += 1
        if h & 0x80:                       # komprimierter Zeitstempel-Header
            lt = (h >> 5) & 3; is_def = False
        else:
            lt = h & 0x0F; is_def = bool(h & 0x40)
        if is_def:
            pos += 1                       # reserved
            en = ">" if data[pos] else "<"; pos += 1
            gm = struct.unpack(en + "H", data[pos:pos+2])[0]; pos += 2
            n = data[pos]; pos += 1
            fl = [tuple(data[pos+3*i:pos+3*i+3]) for i in range(n)]; pos += 3 * n
            dev = 0
            if h & 0x20:
                nd = data[pos]; pos += 1; dev = sum(data[pos+3*i+1] for i in range(nd)); pos += 3 * nd
            defs[lt] = (en, gm, fl, dev)
        else:
            en, gm, fl, dev = defs[lt]
            for fid, sz, _bt in fl:
                if (gm, fid) in patches and sz == 4:
                    data[pos:pos+4] = struct.pack(en + "I", patches[(gm, fid)]); done += 1
                pos += sz
            pos += dev
    data[-2:] = struct.pack("<H", fit_crc(data[:-2]))
    open(path, "wb").write(bytes(data))
    return done


def add_message_index(path, targets=(18, 19), index=0):
    """Haengt Feld 254 (message_index) an Session (18) und Lap (19) an, falls
    es fehlt. dive_summary verweist per reference_index darauf; ohne
    message_index kann Garmin die Summary (Tauchzeit, Ein-/Auftauchzeit)
    nicht zuordnen. Schreibt Definitionen und Daten neu, korrigiert Header
    und CRCs. Gibt die Anzahl erweiterter Definitionen zurueck."""
    import struct
    data = bytes(open(path, "rb").read())
    hs = data[0]
    pos, end = hs, len(data) - 2
    out, defs, n_ext = bytearray(), {}, 0
    while pos < end:
        h = data[pos]; start = pos; pos += 1
        if h & 0x80:
            lt = (h >> 5) & 3; is_def = False
        else:
            lt = h & 0x0F; is_def = bool(h & 0x40)
        if is_def:
            arch = data[pos + 1]; en = ">" if arch else "<"
            gm = struct.unpack(en + "H", data[pos+2:pos+4])[0]
            n = data[pos + 4]
            fl = [tuple(data[pos+5+3*i:pos+8+3*i]) for i in range(n)]
            p2 = pos + 5 + 3 * n
            dev = b""; ndev = 0
            if h & 0x20:
                ndev = data[p2]; dev = data[p2:p2+1+3*ndev]; p2 += 1 + 3 * ndev
            add = gm in targets and not any(f[0] == 254 for f in fl)
            defs[lt] = (en, gm, fl, ndev, add)
            if add:
                n_ext += 1
                out += bytes([h, 0, arch]) + data[pos+2:pos+4] + bytes([n + 1])
                for f in fl: out += bytes(f)
                out += bytes([254, 2, 0x84]) + dev
            else:
                out += data[start:p2]
            pos = p2
        else:
            en, gm, fl, ndev, add = defs[lt]
            size = sum(f[1] for f in fl)   # Developer-Felder werden von fit_tool nicht geschrieben
            out += data[start:pos + size]
            if add: out += struct.pack(en + "H", index)
            pos += size
    body = bytes(out)
    hdr = bytearray(data[:hs])
    hdr[4:8] = struct.pack("<I", len(body))
    if hs >= 14: hdr[12:14] = struct.pack("<H", fit_crc(bytes(hdr[:12])))
    res = bytes(hdr) + body
    res += struct.pack("<H", fit_crc(res))
    open(path, "wb").write(res)
    return n_ext


PATCHSTAND = "2026-11-15-r8 (+ Flaschengroesse via volume_used)"

def read_session_fields(path):
    """Liest Session-Felder (140, 141, 254) zur Kontrolle direkt aus der Datei."""
    import struct
    data = open(path, "rb").read()
    pos, end, defs, res = data[0], len(data) - 2, {}, {}
    while pos < end:
        h = data[pos]; pos += 1
        if h & 0x80:
            lt = (h >> 5) & 3; is_def = False
        else:
            lt = h & 0x0F; is_def = bool(h & 0x40)
        if is_def:
            en = ">" if data[pos + 1] else "<"
            gm = struct.unpack(en + "H", data[pos+2:pos+4])[0]
            n = data[pos + 4]
            fl = [tuple(data[pos+5+3*i:pos+8+3*i]) for i in range(n)]
            pos += 5 + 3 * n
            if h & 0x20:
                nd = data[pos]; pos += 1 + 3 * nd
            defs[lt] = (en, gm, fl)
        else:
            en, gm, fl = defs[lt]
            for fid, sz, _bt in fl:
                if gm == 18 and fid in (140, 141, 254):
                    res[fid] = int.from_bytes(data[pos:pos+sz], "little" if en == "<" else "big")
                pos += sz
    return res

def fix_dive_depths(path, avg_depth, max_depth):
    p = {}
    for gm, a, m in ((18, 140, 141), (19, 122, 123)):   # Session, Lap
        if avg_depth is not None: p[(gm, a)] = int(round(avg_depth * 1000))
        if max_depth is not None: p[(gm, m)] = int(round(max_depth * 1000))
    return patch_fit_fields(path, p)


# ---------- FIT-Bau ----------

def build_fit(d, out_path):
    b = FitFileBuilder(auto_define=True, min_string_size=50)
    site = d["site"] or {}
    lat = site.get("lat"); lon = site.get("lon")
    utc_start, local_start, tzname = resolve_tz(d["start_local"], lat, lon)

    # Zwei verschiedene Zeitbegriffe sauber trennen:
    # - profile_span: volle Aufzeichnungsdauer (erstes bis letztes Sample) =
    #   Eintauchen bis Auftauchen. Daraus leitet Garmin Auftauchzeit ab.
    # - bottom_time: eigentliche Tauchzeit (SSRF 'duration'), fuer dive_summary.
    samples = d["samples"]
    last_t = samples[-1]["t"] if samples else 0
    # Nie kuerzer als die im Log angegebene Tauchzeit, sonst Dauer 0 / Ende=Start
    profile_span = max(last_t, d["duration"] or 0)
    bottom_time = d["duration"] or profile_span
    if profile_span <= 0:
        raise ValueError("Tauchgang ohne Samples und ohne Dauer - kann keine FIT-Datei erzeugen")

    utc_end = utc_start + dt.timedelta(seconds=profile_span)
    local_end = local_start + dt.timedelta(seconds=profile_span)
    start_ms = to_fit_ms_from_utc(utc_start)
    end_ms = to_fit_ms_from_utc(utc_end)
    watertemp_c = int(round(d["watertemp"])) if d["watertemp"] is not None else None
    avgtemp_c = int(round(d["temp_avg"])) if d["temp_avg"] is not None else watertemp_c
    maxtemp_c = int(round(d["temp_max"])) if d["temp_max"] is not None else watertemp_c
    mintemp_c = int(round(d["temp_min"])) if d["temp_min"] is not None else watertemp_c

    # FILE ID
    fid = FileIdMessage()
    fid.type = FileType.ACTIVITY
    fid.manufacturer = Manufacturer.SUUNTO.value
    fid.product = 1
    fid.time_created = start_ms
    fid.serial_number = d.get("dc_serial") or 1
    if d["dc_model"]: fid.product_name = d["dc_model"]
    b.add(fid)

    # DEVICE INFO (Tauchcomputermodell)
    if d["dc_model"]:
        di = DeviceInfoMessage()
        di.timestamp = start_ms
        di.manufacturer = Manufacturer.SUUNTO.value
        di.product_name = d["dc_model"]
        di.device_index = 0
        di.product = 1
        if d.get("dc_serial"): di.serial_number = d["dc_serial"]
        b.add(di)

    # DIVE SETTINGS (Wasserart)
    if d["salinity"] is not None:
        dset = DiveSettingsMessage()
        dset.message_index = 0
        dset.water_type = WaterType.SALT if d["salinity"] >= 1015 else WaterType.FRESH
        dset.water_density = float(d["salinity"])
        if d["maxdepth"] is not None: dset.bottom_depth = d["maxdepth"]
        dset.bottom_time = float(bottom_time)
        b.add(dset)

    # DIVE GAS (Gasmischung; Luft, falls im Log nichts angegeben ist)
    if DiveGasMessage is not None:
        try:
            dg = DiveGasMessage()
            dg.message_index = 0
            dg.oxygen_content = int(round(d["o2"])) if d.get("o2") else 21
            dg.helium_content = int(round(d["he"])) if d.get("he") else 0
            try:
                from fit_tool.profile.profile_type import DiveGasStatus
                dg.status = DiveGasStatus.ENABLED
            except Exception:
                dg.status = 1
            b.add(dg)
        except Exception as e:
            print(f"  HINWEIS: dive_gas nicht schreibbar ({e!r}) - uebersprungen")
    else:
        print("  HINWEIS: Diese fit_tool-Version kennt dive_gas nicht - Gas uebersprungen")

    # START EVENT
    ev = EventMessage()
    ev.event = Event.TIMER; ev.event_type = EventType.START; ev.timestamp = start_ms
    b.add(ev)

    # RECORDS (+ Flaschendruck als tank_update)
    tank_sensor = d.get("dc_serial") or 1
    tank_ok = (TankUpdateMessage is not None and
               any(x["pressure"] is not None for x in d["samples"]))
    if d["samples"] and any(x["pressure"] is not None for x in d["samples"]) \
            and TankUpdateMessage is None:
        print("  HINWEIS: Diese fit_tool-Version kennt tank_update nicht - Flaschendruck uebersprungen")
    for s in d["samples"]:
        r = RecordMessage()
        r.timestamp = start_ms + s["t"] * 1000
        r.depth = s["depth"]
        tc = int(round(s["temp"])) if s["temp"] is not None else watertemp_c
        if tc is not None: r.temperature = tc
        if lat is not None and lon is not None:
            r.position_lat = lat; r.position_long = lon
        b.add(r)
        if tank_ok and s.get("pressure") is not None:
            try:
                tu = TankUpdateMessage()
                tu.timestamp = r.timestamp
                tu.sensor = tank_sensor
                tu.pressure = s["pressure"]          # bar
                b.add(tu)
            except Exception as e:
                tank_ok = False
                print(f"  HINWEIS: Flaschendruck nicht schreibbar ({e!r}) - uebersprungen")

    # STOP EVENT
    ev2 = EventMessage()
    ev2.event = Event.TIMER; ev2.event_type = EventType.STOP_ALL; ev2.timestamp = end_ms
    b.add(ev2)

    # LAP
    lap = LapMessage()
    lap.timestamp = end_ms; lap.start_time = start_ms
    lap.total_elapsed_time = float(profile_span); lap.total_timer_time = float(profile_span)
    if lat is not None and lon is not None:
        lap.start_position_lat = lat; lap.start_position_long = lon
    # Tiefenfelder, die fit_tool nicht kennt (Lap 122/123)
    add_dive_field(lap, "avg_depth", 122, d["avgdepth"])
    add_dive_field(lap, "max_depth", 123, d["maxdepth"])
    b.add(lap)

    # TANK SUMMARY
    if tank_ok and d["p_start"] is not None and d["p_end"] is not None:
        try:
            ts = TankSummaryMessage()
            ts.timestamp = end_ms
            ts.sensor = tank_sensor
            ts.start_pressure = d["p_start"]
            ts.end_pressure = d["p_end"]
            # Verbrauchte Gasmenge in Litern (Druckabfall x Flaschengroesse).
            # Aus volume_used / Druckabfall leiten Reader wie Subsurface die
            # Flaschengroesse ab; ohne den Wert setzen sie einen Standard (10 L).
            if d.get("cyl_size") and d["p_start"] > d["p_end"]:
                ts.volume_used = round(d["cyl_size"] * (d["p_start"] - d["p_end"]), 2)
            b.add(ts)
        except Exception as e:
            print(f"  HINWEIS: tank_summary nicht schreibbar ({e!r}) - uebersprungen")

    # SESSION
    sess = SessionMessage()
    sess.timestamp = end_ms; sess.start_time = start_ms
    sess.total_elapsed_time = float(profile_span); sess.total_timer_time = float(profile_span)
    sess.sport = Sport.DIVING; sess.sub_sport = SubSport.SINGLE_GAS_DIVING
    if avgtemp_c is not None:
        sess.avg_temperature = avgtemp_c; sess.max_temperature = maxtemp_c
        sess.min_temperature = mintemp_c
    if lat is not None and lon is not None:
        sess.start_position_lat = lat; sess.start_position_long = lon
    sess.first_lap_index = 0; sess.num_laps = 1
    # Tiefenfelder, die fit_tool nicht kennt (Session 140/141) - DIESE
    # liest Garmin fuer die "Ø Tiefe"-Kachel
    add_dive_field(sess, "avg_depth", 140, d["avgdepth"])
    add_dive_field(sess, "max_depth", 141, d["maxdepth"])
    b.add(sess)

    # DIVE SUMMARY
    ds = DiveSummaryMessage()
    ds.timestamp = end_ms; ds.reference_mesg = 18; ds.reference_index = 0
    if d["maxdepth"] is not None: ds.max_depth = d["maxdepth"]
    if d["avgdepth"] is not None: ds.avg_depth = d["avgdepth"]
    ds.bottom_time = float(bottom_time)
    if d["number"]:
        try: ds.dive_number = int(d["number"])
        except: pass
    b.add(ds)

    # ACTIVITY
    act = ActivityMessage()
    act.timestamp = end_ms
    act.local_timestamp = to_fit_seconds_local(local_end)
    act.total_timer_time = float(profile_span); act.num_sessions = 1
    act.type = Activity.MANUAL
    b.add(act)

    b.build().to_file(out_path)
    add_message_index(out_path)
    n_fix = fix_dive_depths(out_path, d["avgdepth"], d["maxdepth"])
    chk = read_session_fields(out_path)
    ok = (chk.get(254) == 0 and
          (d["avgdepth"] is None or chk.get(140) == int(round(d["avgdepth"] * 1000))) and
          (d["maxdepth"] is None or chk.get(141) == int(round(d["maxdepth"] * 1000))))
    print(f"  FIT-Check: Session Ø-Tiefe={chk.get(140, 0)/1000:.1f} m, "
          f"max={chk.get(141, 0)/1000:.1f} m, message_index={chk.get(254)} -> "
          f"{'OK' if ok else 'FEHLER (Patch nicht angewendet!)'}  [{n_fix} Tiefenfelder gepatcht]")
    return {"tz": tzname, "local": local_start, "depth": d["maxdepth"],
            "dur": bottom_time, "n": len(d["samples"]), "site": site.get("name"),
            "country": site.get("country"), "suit": d["suit"],
            "salinity": d["salinity"], "dc": d["dc_model"]}


def main():
    if len(sys.argv) < 2:
        print(__doc__); sys.exit(1)
    print("ssrf2fit Patchstand:", PATCHSTAND)
    src = sys.argv[1]; rest = sys.argv[2:]
    do_all = "--all" in rest
    outdir = "."
    if "--outdir" in rest: outdir = rest[rest.index("--outdir")+1]
    idx = None
    for a in rest:
        if a.isdigit(): idx = int(a)

    root = ET.parse(src).getroot()
    sites = load_sites(root)
    dives = root.findall(".//dive")
    print(f"Gefundene Tauchgaenge: {len(dives)}")

    if do_all:
        os.makedirs(outdir, exist_ok=True)
        import csv
        csv_path = os.path.join(outdir, "_begleitliste.csv")
        cf = open(csv_path, "w", newline="", encoding="utf-8-sig")
        writer = csv.writer(cf, delimiter=";")
        writer.writerow(["Dateiname", "Tauchgangnr", "Datum", "Eintauchzeit",
                         "Tauchzeit (mm:ss)", "Ort", "Tauchplatz (Subsurface)",
                         "Land", "Max-Tiefe m", "Wassertemp C", "Tauchcomputer",
                         "Anzug", "Blei (kg)", "Buddy", "Divemaster", "Notiz"])
        for i, dv in enumerate(dives):
            d = extract_dive(dv, sites)
            dstr = d["start_local"].strftime("%Y-%m-%d") if d["start_local"] else f"d{i}"
            fname = f"dive_{i:03d}_{dstr}.fit"
            out = os.path.join(outdir, fname)
            info = build_fit(d, out)
            print(f"  [{i:2d}] {info['local']} {(info['tz'] or '?'):16s} "
                  f"{str(info['depth'])+'m':>7s} {info['n']:3d}pts {info['site']}")
            # Tauchzeit (bottom_time) als mm:ss
            bt = d["duration"] or 0
            bt_str = f"{bt//60}:{bt%60:02d}"
            tstr = d["start_local"].strftime("%H:%M") if d["start_local"] else ""
            weight = f"{d['weight']:.1f}".replace(".", ",") if d["weight"] else ""
            writer.writerow([fname, d["number"] or "", dstr, tstr, bt_str,
                             info["site"] or "", (d["site"] or {}).get("name", ""),
                             info["country"] or "", d["maxdepth"] or "",
                             int(round(d["watertemp"])) if d["watertemp"] else "",
                             d["dc_model"] or "", d["suit"] or "", weight,
                             d["buddy"] or "", d["divemaster"] or "",
                             d["notes"] or ""])
        cf.close()
        print(f"\nFertig: {len(dives)} FIT-Dateien in {outdir}/")
        print(f"Begleitliste: {csv_path}")
    else:
        if idx is None: idx = 0
        d = extract_dive(dives[idx], sites)
        out = os.path.join(outdir, f"dive_{idx:03d}.fit")
        info = build_fit(d, out)
        print(f"#{idx}: {info['local']} | {info['tz']} | {info['depth']}m | "
              f"{info['dur']}s | {info['n']}pts | {info['site']} ({info['country']})")
        print(f"  Anzug: {info['suit']} | Salinitaet: {info['salinity']} | DC: {info['dc']}")
        print("FIT:", out)

if __name__ == "__main__":
    main()
