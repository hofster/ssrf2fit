import xml.etree.ElementTree as ET
import pytest
import ssrf2fit as s

try:
    from garmin_fit_sdk import Decoder, Stream
    HAS_SDK = True
except ImportError:
    HAS_SDK = False


MULTI_DC_HR_SSRF = """<divelog program='subsurface' version='3'>
<divesites>
  <site uuid='1f824f5d' name='Baiersdorf' gps='49.665067 11.023231' />
</divesites>
<dives>
  <dive number='24' divesiteid='1f824f5d' date='2025-07-22' time='16:16:00' duration='0:20 min'>
    <divecomputer model='Mares Quad Air'>
      <depth max='18.6 m' mean='10.8 m' />
      <sample time='0:05 min' depth='1.3 m' temp='28.5 C' />
      <sample time='0:10 min' depth='5.0 m' pressure0='195.0 bar' />
      <sample time='0:15 min' depth='10.0 m' />
      <sample time='0:20 min' depth='8.0 m' />
    </divecomputer>
    <divecomputer model='FIT File import (Imported from file)'>
      <sample time='0:06 min' depth='0.0 m' heartbeat='65' />
      <sample time='0:10 min' depth='0.0 m' heartbeat='72' />
      <sample time='0:16 min' depth='0.0 m' heartbeat='80' />
    </divecomputer>
  </dive>
</dives>
</divelog>
"""


def test_extract_dive_multi_dc_heart_rate():
    root = ET.fromstring(MULTI_DC_HR_SSRF)
    sites = s.load_sites(root)
    dive = root.find(".//dive")
    d = s.extract_dive(dive, sites)

    assert d["dc_model"] == "Mares Quad Air"
    hrs = [smp["hr"] for smp in d["samples"]]
    # 0:05 -> matched to 0:06 (65), 0:10 -> exact (72), 0:15 -> matched to 0:16 (80), 0:20 -> matched to 0:16 (80)
    assert hrs == [65, 72, 80, 80]
    assert d["hr_avg"] == 74
    assert d["hr_max"] == 80


@pytest.mark.skipif(not HAS_SDK, reason="garmin_fit_sdk nicht installiert")
def test_build_fit_multi_dc_heart_rate(tmp_path):
    root = ET.fromstring(MULTI_DC_HR_SSRF)
    sites = s.load_sites(root)
    d = s.extract_dive(root.find(".//dive"), sites)
    out = tmp_path / "dive_hr.fit"
    s.build_fit(d, str(out))

    stream = Stream.from_file(str(out))
    decoder = Decoder(stream)
    messages, errors = decoder.read()
    assert not errors

    rec_hrs = [r.get("heart_rate") for r in messages["record_mesgs"] if r.get("heart_rate") is not None]
    assert rec_hrs == [65, 72, 80, 80]

    sess = messages["session_mesgs"][0]
    assert sess.get("avg_heart_rate") == 74
    assert sess.get("max_heart_rate") == 80
