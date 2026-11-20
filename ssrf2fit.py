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
Tank, Tauchcomputermodell, Notizen, Herzfrequenz).

Aufruf:
  python3 ssrf2fit.py <input.ssrf> [--all | <index>] [-o FILE] [--outdir DIR]
                      [--hr-file FILE] [--hr-offset SEC] [--hr-auto]
"""

import sys, os, re, csv, struct
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
FIT_EPOCH_UTC = dt.datetime(1989, 12, 31, tzinfo=ZoneInfo("UTC"))
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


# ---------- Externe HR-Dateien (.fit, .tcx, .csv) ----------

def _read_fit_hr_binary(fit_path):
    """Liest (unix_ts_sec, hr_bpm) direkt binaer aus Record-Messages (gm=20)
    einer beliebigen FIT-Datei. Umgeht UnicodeDecodeError bei defekten Strings
    in anderen Messages (z.B. Garmin Edge Produkt-/Dateinamen)."""
    with open(fit_path, "rb") as f:
        data = f.read()
    if len(data) < 14:
        return []
    hs = data[0]
    pos, end = hs, len(data) - 2
    defs = {}
    last_ts = None
    points = []
    fit_offset_sec = int((FIT_EPOCH_UTC - dt.datetime(1970, 1, 1, tzinfo=UTC)).total_seconds())

    while pos < end:
        h = data[pos]
        pos += 1
        if h & 0x80:  # Compressed timestamp header
            lt = (h >> 5) & 3
            is_def = False
            time_offset = h & 0x1F
            if last_ts is not None:
                ts = (last_ts & ~0x1F) + time_offset
                if ts < last_ts:
                    ts += 0x20
                last_ts = ts
        else:
            lt = h & 0x0F
            is_def = bool(h & 0x40)
            time_offset = None

        if is_def:
            pos += 1  # reserved
            en = ">" if data[pos] else "<"
            pos += 1
            gm = struct.unpack(en + "H", data[pos:pos+2])[0]
            pos += 2
            n = data[pos]
            pos += 1
            fl = [tuple(data[pos+3*i:pos+3*i+3]) for i in range(n)]
            pos += 3 * n
            dev = 0
            if h & 0x20:
                nd = data[pos]
                pos += 1
                dev = sum(data[pos+3*i+1] for i in range(nd))
                pos += 3 * nd
            defs[lt] = (en, gm, fl, dev)
        else:
            if lt not in defs:
                break
            en, gm, fl, dev = defs[lt]
            rec_ts = last_ts
            rec_hr = None
            for fid, sz, _bt in fl:
                if gm == 20:  # RECORD message
                    if fid == 253 and sz == 4:  # timestamp (uint32 FIT seconds)
                        val = struct.unpack(en + "I", data[pos:pos+4])[0]
                        if val != 0xFFFFFFFF:
                            rec_ts = val
                            last_ts = val
                    elif fid == 3 and sz == 1:  # heart_rate (uint8 bpm)
                        val = data[pos]
                        if val != 0xFF and val > 0:
                            rec_hr = val
                pos += sz
            pos += dev
            if gm == 20 and rec_ts is not None and rec_hr is not None:
                points.append((float(rec_ts + fit_offset_sec), int(rec_hr)))

    points.sort(key=lambda x: x[0])
    return points


def _parse_iso_ts(s):
    if not s:
        return None
    s = s.strip()
    try:
        d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=UTC)
        return d.timestamp()
    except ValueError:
        try:
            return float(s)
        except ValueError:
            return None


def parse_external_hr_file(hr_path):
    """Liest Herzfrequenz-Messpunkte aus .fit, .tcx oder .csv.
    Rueckgabe: sortierte Liste von (unix_ts_sec, hr_bpm)."""
    if not hr_path or not os.path.exists(hr_path):
        raise FileNotFoundError(f"HR-Datei nicht gefunden: {hr_path}")
    ext = os.path.splitext(hr_path)[1].lower()
    points = []

    if ext == ".fit":
        points = _read_fit_hr_binary(hr_path)

    elif ext == ".tcx":
        tree = ET.parse(hr_path)
        root = tree.getroot()
        for elem in root.iter():
            if elem.tag.endswith("Trackpoint"):
                t_val = None
                hr_val = None
                for child in elem.iter():
                    if child.tag.endswith("Time") and child.text:
                        t_val = _parse_iso_ts(child.text)
                    elif child.tag.endswith("Value") and child.text:
                        try:
                            v = int(round(float(child.text.strip())))
                            if v > 0:
                                hr_val = v
                        except ValueError:
                            pass
                if t_val is not None and hr_val is not None:
                    points.append((t_val, hr_val))

    elif ext == ".csv":
        with open(hr_path, "r", encoding="utf-8-sig", errors="replace") as f:
            sample_line = f.readline()
            f.seek(0)
            delim = ";" if sample_line.count(";") > sample_line.count(",") else ","
            reader = csv.DictReader(f, delimiter=delim)
            for row in reader:
                if not row:
                    continue
                norm = {k.strip().lower(): (v.strip() if v else "") for k, v in row.items() if k}
                t_str = (norm.get("timestamp") or norm.get("time") or norm.get("date")
                         or norm.get("zeit") or norm.get("zeitstempel"))
                hr_str = (norm.get("heart_rate") or norm.get("heart rate") or norm.get("hr")
                          or norm.get("bpm") or norm.get("puls") or norm.get("herzfrequenz"))
                if t_str and hr_str:
                    t_val = _parse_iso_ts(t_str)
                    try:
                        hr_val = int(round(float(hr_str.replace(",", "."))))
                    except ValueError:
                        hr_val = None
                    if t_val is not None and hr_val is not None and hr_val > 0:
                        points.append((t_val, hr_val))
    else:
        raise ValueError(f"Nicht unterstuetztes HR-Dateiformat: {ext} (erlaubt: .fit, .tcx, .csv)")

    points.sort(key=lambda x: x[0])
    return points


def apply_external_hr(d, hr_points, offset_sec=0, align_start=False, max_gap_sec=15):
    """Ordnet externe HR-Messpunkte (unix_ts_sec, hr) den Tauchgangs-Samples zu."""
    if not hr_points or not d.get("samples") or not d.get("start_local"):
        return 0
    site = d.get("site") or {}
    utc_start, _, _ = resolve_tz(d["start_local"], site.get("lat"), site.get("lon"))
    dive_start_unix = utc_start.timestamp()

    if align_start:
        first_hr_ts = hr_points[0][0]
        shift = (dive_start_unix - first_hr_ts) + offset_sec
    else:
        shift = offset_sec

    shifted = [(ts + shift, hr) for ts, hr in hr_points]
    matched = 0
    idx = 0
    n = len(shifted)

    for s in d["samples"]:
        target_ts = dive_start_unix + s["t"]
        while idx + 1 < n and shifted[idx + 1][0] <= target_ts:
            idx += 1
        best_hr = None
        best_diff = max_gap_sec + 1
        for cand_i in (idx, idx + 1):
            if 0 <= cand_i < n:
                diff = abs(shifted[cand_i][0] - target_ts)
                if diff <= max_gap_sec and diff < best_diff:
                    best_diff = diff
                    best_hr = shifted[cand_i][1]
        if best_hr is not None:
            s["hr"] = best_hr
            matched += 1

    hrs = [x["hr"] for x in d["samples"] if x.get("hr") is not None]
    d["avg_hr"] = int(round(sum(hrs) / len(hrs))) if hrs else None
    d["max_hr"] = max(hrs) if hrs else None
    return matched


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

    dcs = dive.findall("divecomputer")
    dc = dcs[0] if dcs else None
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

    # Sekundaere divecomputer-Bloecke auf heartbeat-Samples pruefen (z.B. FIT-Import in Subsurface)
    extra_hr = []
    for extra_dc in dcs[1:]:
        for sm in extra_dc.findall("sample"):
            t_ex = parse_sample_time(sm.get("time"))
            hr_ex = num(sm.get("heartbeat"))
            if t_ex is not None and hr_ex is not None and hr_ex > 0:
                extra_hr.append((t_ex, int(round(hr_ex))))
    extra_hr.sort(key=lambda x: x[0])

    samples = []
    src = dc if dc is not None else dive
    last_temp = None
    for sm in src.findall("sample"):
        t = parse_sample_time(sm.get("time"))
        dep = num(sm.get("depth"))
        tmp = num(sm.get("temp"))
        pr = num(sm.get("pressure0"))
        hr_val = num(sm.get("heartbeat"))
        hr = int(round(hr_val)) if (hr_val is not None and hr_val > 0) else None
        if hr is None and t is not None and extra_hr:
            best = min(extra_hr, key=lambda item: abs(item[0] - t))
            if abs(best[0] - t) <= 15:
                hr = best[1]
        if tmp is not None and tmp > 0: last_temp = tmp
        if t is None or dep is None: continue
        samples.append({"t": t, "depth": dep, "temp": last_temp,
                        "pressure": pr if (pr is not None and pr > 0) else None,
                        "hr": hr})
    samples.sort(key=lambda x: x["t"])
    first_known = next((x["temp"] for x in samples if x["temp"] is not None), None)
    for x in samples:
        if x["temp"] is None: x["temp"] = first_known
    d["samples"] = samples

    hrs = [x["hr"] for x in samples if x.get("hr") is not None]
    d["avg_hr"] = int(round(sum(hrs) / len(hrs))) if hrs else None
    d["max_hr"] = max(hrs) if hrs else None

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
    if not d["avgdepth"] or d["avgdepth"] <= 0:
        d["avgdepth"] = mean_depth_from_samples(samples)
    if d["watertemp"] is not None and d["watertemp"] <= 0:
        d["watertemp"] = None
    if d["watertemp"] is None:
        d["watertemp"] = d["temp_avg"]
    return d


# ---------- FIT-Nachbearbeitung ----------

_CRC_T = [0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
          0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400]

def fit_crc(data, crc=0):
    for b in data:
        crc = (crc >> 4 & 0x0FFF) ^ _CRC_T[crc & 0xF] ^ _CRC_T[b & 0xF]
        crc = (crc >> 4 & 0x0FFF) ^ _CRC_T[crc & 0xF] ^ _CRC_T[b >> 4 & 0xF]
    return crc

def patch_fit_fields(path, patches):
    """patches: {(global_msg_num, field_id): rohwert_uint32}."""
    data = bytearray(open(path, "rb").read())
    hs = data[0]
    pos, end = hs, len(data) - 2
    defs, done = {}, 0
    while pos < end:
        h = data[pos]; pos += 1
        if h & 0x80:
            lt = (h >> 5) & 3; is_def = False
        else:
            lt = h & 0x0F; is_def = bool(h & 0x40)
        if is_def:
            pos += 1
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
    """Haengt Feld 254 (message_index) an Session (18) und Lap (19) an, falls es fehlt."""
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
            size = sum(f[1] for f in fl)
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


PATCHSTAND = "2026-11-20-r10 (+ Binary FIT HR Reader, Offset & Align-Start)"

def read_session_fields(path):
    """Liest Session-Felder (140, 141, 254) zur Kontrolle direkt aus der Datei."""
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
    for gm, a, m in ((18, 140, 141), (19, 122, 123)):
        if avg_depth is not None: p[(gm, a)] = int(round(avg_depth * 1000))
        if max_depth is not None: p[(gm, m)] = int(round(max_depth * 1000))
    return patch_fit_fields(path, p)


# ---------- FIT-Bau ----------

def build_fit(d, out_path):
    b = FitFileBuilder(auto_define=True, min_string_size=50)
    site = d["site"] or {}
    lat = site.get("lat"); lon = site.get("lon")
    utc_start, local_start, tzname = resolve_tz(d["start_local"], lat, lon)

    samples = d["samples"]
    last_t = samples[-1]["t"] if samples else 0
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

    # DEVICE INFO
    if d["dc_model"]:
        di = DeviceInfoMessage()
        di.timestamp = start_ms
        di.manufacturer = Manufacturer.SUUNTO.value
        di.product_name = d["dc_model"]
        di.device_index = 0
        di.product = 1
        if d.get("dc_serial"): di.serial_number = d["dc_serial"]
        b.add(di)

    # DIVE SETTINGS
    if d["salinity"] is not None:
        dset = DiveSettingsMessage()
        dset.message_index = 0
        dset.water_type = WaterType.SALT if d["salinity"] >= 1015 else WaterType.FRESH
        dset.water_density = float(d["salinity"])
        if d["maxdepth"] is not None: dset.bottom_depth = d["maxdepth"]
        dset.bottom_time = float(bottom_time)
        b.add(dset)

    # DIVE GAS
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

    # START EVENT
    ev = EventMessage()
    ev.event = Event.TIMER; ev.event_type = EventType.START; ev.timestamp = start_ms
    b.add(ev)

    # RECORDS (+ Flaschendruck + Herzfrequenz)
    tank_sensor = d.get("dc_serial") or 1
    tank_ok = (TankUpdateMessage is not None and
               any(x["pressure"] is not None for x in d["samples"]))
    for s in d["samples"]:
        r = RecordMessage()
        r.timestamp = start_ms + s["t"] * 1000
        r.depth = s["depth"]
        tc = int(round(s["temp"])) if s["temp"] is not None else watertemp_c
        if tc is not None: r.temperature = tc
        if s.get("hr") is not None:
            r.heart_rate = int(s["hr"])
        if lat is not None and lon is not None:
            r.position_lat = lat; r.position_long = lon
        b.add(r)
        if tank_ok and s.get("pressure") is not None:
            try:
                tu = TankUpdateMessage()
                tu.timestamp = r.timestamp
                tu.sensor = tank_sensor
                tu.pressure = s["pressure"]
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
    if d.get("avg_hr") is not None:
        lap.avg_heart_rate = int(d["avg_hr"])
    if d.get("max_hr") is not None:
        lap.max_heart_rate = int(d["max_hr"])
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
    if d.get("avg_hr") is not None:
        sess.avg_heart_rate = int(d["avg_hr"])
    if d.get("max_hr") is not None:
        sess.max_heart_rate = int(d["max_hr"])
    if lat is not None and lon is not None:
        sess.start_position_lat = lat; sess.start_position_long = lon
    sess.first_lap_index = 0; sess.num_laps = 1
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
    hr_info = f", HR Ø={d['avg_hr']}/max={d['max_hr']} bpm" if d.get("avg_hr") else ""
    print(f"  FIT-Check: Session Ø-Tiefe={chk.get(140, 0)/1000:.1f} m, "
          f"max={chk.get(141, 0)/1000:.1f} m, message_index={chk.get(254)}{hr_info} -> "
          f"{'OK' if ok else 'FEHLER (Patch nicht angewendet!)'}  [{n_fix} Tiefenfelder gepatcht]")
    return {"tz": tzname, "local": local_start, "depth": d["maxdepth"],
            "dur": bottom_time, "n": len(d["samples"]), "site": site.get("name"),
            "country": site.get("country"), "suit": d["suit"],
            "salinity": d["salinity"], "dc": d["dc_model"],
            "avg_hr": d.get("avg_hr"), "max_hr": d.get("max_hr")}


def main():
    if len(sys.argv) < 2:
        print(__doc__); sys.exit(1)
    print("ssrf2fit Patchstand:", PATCHSTAND)
    src = sys.argv[1]; rest = sys.argv[2:]
    do_all = "--all" in rest
    align_start = any(flag in rest for flag in ("--hr-align-start", "--hr-force-start", "--hr-auto"))
    outdir = "."
    if "--outdir" in rest:
        outdir = rest[rest.index("--outdir") + 1]
    out_file = None
    if "-o" in rest:
        out_file = rest[rest.index("-o") + 1]
    elif "--output" in rest:
        out_file = rest[rest.index("--output") + 1]
    hr_file = None
    if "--hr-file" in rest:
        hr_file = rest[rest.index("--hr-file") + 1]
    hr_offset = 0
    if "--hr-offset" in rest:
        hr_offset = int(float(rest[rest.index("--hr-offset") + 1]))

    skip_indices = set()
    for flag in ("--outdir", "-o", "--output", "--hr-file", "--hr-offset"):
        if flag in rest:
            p = rest.index(flag)
            skip_indices.add(p)
            skip_indices.add(p + 1)

    idx = None
    for pos_i, a in enumerate(rest):
        if pos_i not in skip_indices and a.isdigit():
            idx = int(a)

    hr_points = []
    if hr_file:
        hr_points = parse_external_hr_file(hr_file)
        print(f"Externe HR-Datei geladen: {len(hr_points)} Messpunkte aus {hr_file}")

    root = ET.parse(src).getroot()
    sites = load_sites(root)
    dives = root.findall(".//dive")
    print(f"Gefundene Tauchgaenge: {len(dives)}")

    if do_all:
        os.makedirs(outdir, exist_ok=True)
        csv_path = os.path.join(outdir, "_begleitliste.csv")
        cf = open(csv_path, "w", newline="", encoding="utf-8-sig")
        writer = csv.writer(cf, delimiter=";")
        writer.writerow(["Dateiname", "Tauchgangnr", "Datum", "Eintauchzeit",
                         "Tauchzeit (mm:ss)", "Ort", "Tauchplatz (Subsurface)",
                         "Land", "Max-Tiefe m", "Wassertemp C", "Tauchcomputer",
                         "Anzug", "Blei (kg)", "Buddy", "Divemaster", "Notiz"])
        for i, dv in enumerate(dives):
            d = extract_dive(dv, sites)
            if hr_points:
                matched = apply_external_hr(d, hr_points, offset_sec=hr_offset, align_start=align_start)
                if matched:
                    print(f"  [{i:2d}] {matched} HR-Samples zugeordnet")
            dstr = d["start_local"].strftime("%Y-%m-%d") if d["start_local"] else f"d{i}"
            fname = f"dive_{i:03d}_{dstr}.fit"
            out = os.path.join(outdir, fname)
            info = build_fit(d, out)
            print(f"  [{i:2d}] {info['local']} {(info['tz'] or '?'):16s} "
                  f"{str(info['depth'])+'m':>7s} {info['n']:3d}pts {info['site']}")
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
        if hr_points:
            matched = apply_external_hr(d, hr_points, offset_sec=hr_offset, align_start=align_start)
            print(f"HR-Abgleich: {matched}/{len(d['samples'])} Tauchgangs-Samples mit Puls belegt"
                  + (" (Startzeit automatisch angeglichen)" if align_start else ""))
        out = out_file if out_file else os.path.join(outdir, f"dive_{idx:03d}.fit")
        info = build_fit(d, out)
        print(f"#{idx}: {info['local']} | {info['tz']} | {info['depth']}m | "
              f"{info['dur']}s | {info['n']}pts | {info['site']} ({info['country']})")
        print(f"  Anzug: {info['suit']} | Salinitaet: {info['salinity']} | DC: {info['dc']}")
        print("FIT:", out)

if __name__ == "__main__":
    main()
