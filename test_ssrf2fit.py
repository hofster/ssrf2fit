# Tests for ssrf2fit
# Run with:  pytest -v
import datetime as dt
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

import ssrf2fit as s

EXAMPLE = Path(__file__).parent / "examples" / "example_dives.ssrf"


# ---------- parsing helpers ----------

def test_num_extracts_first_number():
    assert s.num("12.08 m") == 12.08
    assert s.num("193.0 bar") == 193.0
    assert s.num("1000 g/l") == 1000.0
    assert s.num(None) is None
    assert s.num("no number here") is None


def test_num_handles_negative():
    assert s.num("-1.5 C") == -1.5


def test_parse_duration_mmss():
    assert s.parse_duration("27:40 min") == 27 * 60 + 40
    assert s.parse_duration("1:05 min") == 65
    assert s.parse_duration(None) is None


def test_parse_sample_time():
    assert s.parse_sample_time("0:00 min") == 0
    assert s.parse_sample_time("36:20 min") == 36 * 60 + 20
    assert s.parse_sample_time(None) is None


# ---------- timezone logic (the hard-won core) ----------

def test_resolve_tz_germany_summer():
    # 2018-06-15 in Berlin -> CEST (UTC+2)
    naive = dt.datetime(2018, 6, 15, 10, 30)
    utc, local, tzname = s.resolve_tz(naive, 52.5, 13.2)
    assert tzname == "Europe/Berlin"
    assert local == naive
    # 10:30 local CEST -> 08:30 UTC
    assert utc.hour == 8 and utc.minute == 30


def test_resolve_tz_egypt():
    # Africa/Cairo
    naive = dt.datetime(2020, 10, 5, 9, 0)
    utc, local, tzname = s.resolve_tz(naive, 27.16, 33.98)
    assert tzname == "Africa/Cairo"


def test_resolve_tz_without_coords_falls_back_to_utc():
    naive = dt.datetime(2020, 1, 1, 12, 0)
    utc, local, tzname = s.resolve_tz(naive, None, None)
    assert tzname is None
    # treated as UTC -> same clock time
    assert utc.hour == 12


def test_to_fit_seconds_local_roundtrip():
    d = dt.datetime(2013, 6, 2, 12, 24, 0)
    secs = s.to_fit_seconds_local(d)
    back = s.FIT_EPOCH + dt.timedelta(seconds=secs)
    assert back == d


# ---------- SSRF parsing ----------

def test_load_sites():
    root = ET.parse(EXAMPLE).getroot()
    sites = s.load_sites(root)
    assert len(sites) == 3
    lake = sites["11111111"]
    assert lake["name"] == "Example Lake"
    assert lake["lat"] == pytest.approx(52.5)
    assert lake["country"] == "Germany"


def test_extract_dive_fields():
    root = ET.parse(EXAMPLE).getroot()
    sites = s.load_sites(root)
    dives = root.findall(".//dive")
    d = s.extract_dive(dives[0], sites)
    assert d["number"] == "1"
    assert d["maxdepth"] == pytest.approx(18.30)
    assert d["avgdepth"] == pytest.approx(9.100)
    assert d["suit"] == "7mm wetsuit"
    assert d["weight"] == pytest.approx(8.0)
    assert d["salinity"] == pytest.approx(1000.0)
    assert d["dc_model"] == "Example DiveComputer"
    assert len(d["samples"]) == 6


# ---------- end-to-end: build FIT and verify with Garmin SDK ----------

def _decode(path):
    from garmin_fit_sdk import Decoder, Stream
    return Decoder(Stream.from_file(str(path))).read()


def test_build_fit_is_valid_and_dive(tmp_path):
    root = ET.parse(EXAMPLE).getroot()
    sites = s.load_sites(root)
    dives = root.findall(".//dive")
    d = s.extract_dive(dives[0], sites)
    out = tmp_path / "dive.fit"
    s.build_fit(d, str(out))

    messages, errors = _decode(out)
    assert not errors
    sess = messages["session_mesgs"][0]
    # recognised as scuba dive
    assert str(sess.get("sport")) == "diving"
    assert str(sess.get("sub_sport")) == "single_gas_diving"
    # the manually-added depth fields Garmin actually reads
    assert sess.get("avg_depth") == pytest.approx(9.100, abs=0.01)
    assert sess.get("max_depth") == pytest.approx(18.30, abs=0.01)


def test_build_fit_timezone_offset(tmp_path):
    # Spain dive: local 11:15 CEST -> UTC 09:15, local_timestamp must encode +2h
    root = ET.parse(EXAMPLE).getroot()
    sites = s.load_sites(root)
    dives = root.findall(".//dive")
    d = s.extract_dive(dives[1], sites)
    out = tmp_path / "dive.fit"
    s.build_fit(d, str(out))
    messages, _ = _decode(out)
    sess = messages["session_mesgs"][0]
    # start_time stored as UTC
    assert sess.get("start_time").hour == 9
    assert sess.get("start_time").minute == 15


def test_build_fit_water_type(tmp_path):
    root = ET.parse(EXAMPLE).getroot()
    sites = s.load_sites(root)
    dives = root.findall(".//dive")
    # dive 0 = fresh (1000), dive 1 = salt (1030)
    d_fresh = s.extract_dive(dives[0], sites)
    d_salt = s.extract_dive(dives[1], sites)
    for d, expected in [(d_fresh, "fresh"), (d_salt, "salt")]:
        out = tmp_path / f"{expected}.fit"
        s.build_fit(d, str(out))
        messages, _ = _decode(out)
        ds = messages["dive_settings_mesgs"][0]
        assert str(ds.get("water_type")) == expected


def test_manual_dive_few_samples(tmp_path):
    # dive 2 was manually logged with only 4 samples - must still be valid
    root = ET.parse(EXAMPLE).getroot()
    sites = s.load_sites(root)
    dives = root.findall(".//dive")
    d = s.extract_dive(dives[2], sites)
    assert len(d["samples"]) == 4
    out = tmp_path / "manual.fit"
    s.build_fit(d, str(out))
    messages, errors = _decode(out)
    assert not errors
    assert len(messages["record_mesgs"]) == 4
