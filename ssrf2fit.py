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
from fit_tool.data_message import Field
from fit_tool.base_type import BaseType


def add_dive_field(msg, name, fid, value, scale=1000, units="m"):
    """Fuegt ein im fit_tool-Profil fehlendes Tauch-Feld manuell hinzu.
    fit_tool kennt session.avg_depth/max_depth (140/141) und die
    lap-Pendants (122/123) nicht, Garmin liest sie aber fuer die Anzeige."""
    if value is None:
        return
    f = Field(name=name, field_id=fid, base_type=BaseType.UINT32,
              scale=scale, offset=0, units=units, size=4)
    f.set_values([value])
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

def parse_duration(s):
    """'27:40 min' -> Sekunden."""
    if not s: return None
    m = re.match(r"(\d+):(\d+)", s)
    if m: return int(m.group(1)) * 60 + int(m.group(2))
    n = num(s)
    return int(n * 60) if n else None

def parse_sample_time(s):
    """'1:20 min' -> Sekunden."""
    if not s: return None
    m = re.match(r"(\d+):(\d+)", s)
    if m: return int(m.group(1)) * 60 + int(m.group(2))
    return None


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

    ws = dive.find("weightsystem")
    d["weight"] = num(ws.get("weight")) if ws is not None else None
    d["weight_desc"] = ws.get("description") if ws is not None else None

    dtemp = dive.find("divetemperature")
    d["airtemp"] = num(dtemp.get("air")) if dtemp is not None else None

    dc = dive.find("divecomputer")
    d["dc_model"] = dc.get("model") if dc is not None else None
    depth = dc.find("depth") if dc is not None else None
    d["maxdepth"] = num(depth.get("max")) if depth is not None else None
    d["avgdepth"] = num(depth.get("mean")) if depth is not None else None
    wtemp = dc.find("temperature") if dc is not None else None
    d["watertemp"] = num(wtemp.get("water")) if wtemp is not None else None

    samples = []
    src = dc if dc is not None else dive
    for sm in src.findall("sample"):
        t = parse_sample_time(sm.get("time"))
        dep = num(sm.get("depth"))
        tmp = num(sm.get("temp"))
        if t is None or dep is None: continue
        samples.append({"t": t, "depth": dep, "temp": tmp})
    d["samples"] = samples

    if d["maxdepth"] is None and samples:
        d["maxdepth"] = max(s["depth"] for s in samples)
    if d["duration"] is None and samples:
        d["duration"] = samples[-1]["t"]
    if d["watertemp"] is None:
        for s in samples:
            if s["temp"] is not None: d["watertemp"] = s["temp"]; break
    return d


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
    profile_span = samples[-1]["t"] if samples else (d["duration"] or 0)
    bottom_time = d["duration"] or profile_span

    utc_end = utc_start + dt.timedelta(seconds=profile_span)
    local_end = local_start + dt.timedelta(seconds=profile_span)
    start_ms = to_fit_ms_from_utc(utc_start)
    end_ms = to_fit_ms_from_utc(utc_end)
    watertemp_c = int(round(d["watertemp"])) if d["watertemp"] is not None else None

    # FILE ID
    fid = FileIdMessage()
    fid.type = FileType.ACTIVITY
    fid.manufacturer = Manufacturer.SUUNTO.value
    fid.product = 1
    fid.time_created = start_ms
    fid.serial_number = 1
    b.add(fid)

    # DEVICE INFO (Tauchcomputermodell)
    if d["dc_model"]:
        di = DeviceInfoMessage()
        di.timestamp = start_ms
        di.manufacturer = Manufacturer.SUUNTO.value
        di.product_name = d["dc_model"]
        di.device_index = 0
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

    # START EVENT
    ev = EventMessage()
    ev.event = Event.TIMER; ev.event_type = EventType.START; ev.timestamp = start_ms
    b.add(ev)

    # RECORDS
    for s in d["samples"]:
        r = RecordMessage()
        r.timestamp = start_ms + s["t"] * 1000
        r.depth = s["depth"]
        tc = int(round(s["temp"])) if s["temp"] is not None else watertemp_c
        if tc is not None: r.temperature = tc
        if lat is not None and lon is not None:
            r.position_lat = lat; r.position_long = lon
        b.add(r)

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

    # SESSION
    sess = SessionMessage()
    sess.timestamp = end_ms; sess.start_time = start_ms
    sess.total_elapsed_time = float(profile_span); sess.total_timer_time = float(profile_span)
    sess.sport = Sport.DIVING; sess.sub_sport = SubSport.SINGLE_GAS_DIVING
    if watertemp_c is not None:
        sess.avg_temperature = watertemp_c; sess.max_temperature = watertemp_c
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
    ds.bottom_time = float(bottom_time); ds.surface_interval = 0
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
    return {"tz": tzname, "local": local_start, "depth": d["maxdepth"],
            "dur": bottom_time, "n": len(d["samples"]), "site": site.get("name"),
            "country": site.get("country"), "suit": d["suit"],
            "salinity": d["salinity"], "dc": d["dc_model"]}


def main():
    if len(sys.argv) < 2:
        print(__doc__); sys.exit(1)
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
