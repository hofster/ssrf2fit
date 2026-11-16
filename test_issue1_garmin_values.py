# Regression tests for issue #1 (wrong imported values in Garmin Connect)
# Run with:  pytest -v
import xml.etree.ElementTree as ET

import pytest

import ssrf2fit as s

# Small synthetic dive (Mares-like): temp/pressure only on some samples,
# mean depth 0 in the log, salinity and serial only in <divecomputer>.
DIVE_XML = """
<divelog><dives>
<dive number="24" date="2025-07-22" time="16:16:00" duration="3:00 min">
  <cylinder size="12.0 l" workpressure="200.0 bar" />
  <divecomputer model="Mares Quad Air" deviceid="fc8e6e8b">
    <extradata key="Serial" value="17180" />
    <water salinity="1000 g/l" />
    <depth max="20.0 m" mean="0.0 m" />
    <temperature water="24.0 C" />
    <sample time="0:00 min" depth="0.0 m" temp="28.0 C" pressure0="200.0 bar" />
    <sample time="1:00 min" depth="10.0 m" />
    <sample time="2:00 min" depth="20.0 m" temp="24.0 C" pressure0="190.0 bar" />
    <sample time="3:00 min" depth="0.0 m" pressure0="180.0 bar" />
  </divecomputer>
</dive>
</dives></divelog>
"""


def _dive(xml=DIVE_XML):
    root = ET.fromstring(xml)
    return s.extract_dive(root.find(".//dive"), {})


# ---------- parsing (no FIT needed) ----------

def test_parse_clock_with_hours():
    assert s.parse_sample_time("1:05:20 h") == 3920
    assert s.parse_sample_time("36:20 min") == 36 * 60 + 20
    assert s.parse_duration("45:00 min") == 2700


def test_mean_depth_from_samples_is_time_weighted():
    samples = [{"t": 0, "depth": 0.0}, {"t": 60, "depth": 10.0},
               {"t": 120, "depth": 20.0}, {"t": 180, "depth": 0.0}]
    assert s.mean_depth_from_samples(samples) == pytest.approx(10.0)
    assert s.mean_depth_from_samples(samples[:1]) is None


def test_mean_depth_computed_when_log_has_zero():
    d = _dive()
    assert d["avgdepth"] == pytest.approx(10.0)
    assert d["maxdepth"] == pytest.approx(20.0)


def test_temperature_is_carried_forward():
    d = _dive()
    assert [x["temp"] for x in d["samples"]] == [28.0, 28.0, 24.0, 24.0]
    assert d["temp_avg"] == pytest.approx(26.0)
    assert d["temp_min"] == pytest.approx(24.0)
    assert d["temp_max"] == pytest.approx(28.0)


def test_salinity_and_serial_from_divecomputer():
    d = _dive()
    assert d["salinity"] == pytest.approx(1000.0)
    assert d["dc_serial"] == 17180
    assert d["dc_model"] == "Mares Quad Air"


def test_pressure_samples():
    d = _dive()
    assert [x["pressure"] for x in d["samples"]] == [200.0, None, 190.0, 180.0]
    assert d["p_start"] == pytest.approx(200.0)
    assert d["p_end"] == pytest.approx(180.0)


# ---------- FIT output (needs fit_tool + garmin-fit-sdk) ----------

def _decode(path):
    sdk = pytest.importorskip("garmin_fit_sdk")
    return sdk.Decoder(sdk.Stream.from_file(str(path)))


def _build(tmp_path, xml=DIVE_XML):
    d = _dive(xml)
    out = tmp_path / "dive.fit"
    s.build_fit(d, str(out))
    return d, out


def test_fit_integrity_after_byte_patches(tmp_path):
    _, out = _build(tmp_path)
    dec = _decode(out)
    assert dec.is_fit()
    assert dec.check_integrity()      # header size, data size and CRC are consistent


def test_session_depth_and_message_index(tmp_path):
    _, out = _build(tmp_path)
    messages, errors = _decode(out).read()
    assert not errors
    sess = messages["session_mesgs"][0]
    assert sess.get("message_index") == 0
    assert sess.get("avg_depth") == pytest.approx(10.0, abs=0.01)
    assert sess.get("max_depth") == pytest.approx(20.0, abs=0.01)
    lap = messages["lap_mesgs"][0]
    assert lap.get("message_index") == 0
    assert lap.get("avg_depth") == pytest.approx(10.0, abs=0.01)


def test_dive_summary_is_linked_to_session(tmp_path):
    _, out = _build(tmp_path)
    messages, _ = _decode(out).read()
    ds = messages["dive_summary_mesgs"][0]
    assert ds.get("reference_index") == 0
    assert ds.get("bottom_time") == pytest.approx(180, abs=0.5)
    assert ds.get("dive_number") == 24
    assert ds.get("surface_interval") is None   # unknown, must not be written as 0


def test_temperature_in_records_and_session(tmp_path):
    _, out = _build(tmp_path)
    messages, _ = _decode(out).read()
    temps = [r.get("temperature") for r in messages["record_mesgs"]]
    assert temps == [28, 28, 24, 24]
    sess = messages["session_mesgs"][0]
    assert sess.get("avg_temperature") == 26
    assert sess.get("max_temperature") == 28
    assert sess.get("min_temperature") == 24


def test_duration_is_at_least_logged_duration(tmp_path):
    # logged duration (5 min) is longer than the last sample (3 min)
    xml = DIVE_XML.replace('duration="3:00 min"', 'duration="5:00 min"')
    _, out = _build(tmp_path, xml)
    messages, _ = _decode(out).read()
    sess = messages["session_mesgs"][0]
    assert sess.get("total_elapsed_time") == pytest.approx(300, abs=0.5)
    assert (sess.get("timestamp") - sess.get("start_time")).total_seconds() == 300


def test_device_info_has_model_and_serial(tmp_path):
    _, out = _build(tmp_path)
    messages, _ = _decode(out).read()
    di = messages["device_info_mesgs"][0]
    assert di.get("product_name") == "Mares Quad Air"
    assert di.get("serial_number") == 17180


def test_tank_pressure_written(tmp_path):
    if s.TankUpdateMessage is None:
        pytest.skip("this fit_tool version has no tank_update")
    _, out = _build(tmp_path)
    messages, _ = _decode(out).read()
    updates = messages["tank_update_mesgs"]
    assert [u.get("pressure") for u in updates] == pytest.approx([200.0, 190.0, 180.0])
    summary = messages["tank_summary_mesgs"][0]
    assert summary.get("start_pressure") == pytest.approx(200.0)
    assert summary.get("end_pressure") == pytest.approx(180.0)


def test_dive_gas_defaults_to_air(tmp_path):
    if s.DiveGasMessage is None:
        pytest.skip("this fit_tool version has no dive_gas")
    _, out = _build(tmp_path)
    messages, _ = _decode(out).read()
    gas = messages["dive_gas_mesgs"][0]
    assert gas.get("oxygen_content") == 21
    assert gas.get("helium_content") == 0


def test_patches_are_idempotent(tmp_path):
    _, out = _build(tmp_path)
    assert s.add_message_index(str(out)) == 0       # already present
    fields = s.read_session_fields(str(out))
    assert fields[254] == 0
    assert fields[140] == 10000 and fields[141] == 20000
    assert _decode(out).check_integrity()
