from __future__ import annotations

import unittest
from typing import Any

from msdial_repository_catalog.adapters.mbpost import MbPostAdapter
from msdial_repository_catalog.adapters.metabobank import MetaboBankAdapter
from msdial_repository_catalog.adapters.metabolights import MetaboLightsAdapter
from msdial_repository_catalog.adapters.workbench import MetabolomicsWorkbenchAdapter


class FakeClient:
    def __init__(self, values: dict[str, Any]) -> None:
        self.values = values

    def _value(self, url: str) -> Any:
        for token, value in self.values.items():
            if token in url:
                return value
        raise AssertionError(f"No fixture for {url}")

    def get_text(self, url: str) -> str:
        value = self._value(url)
        return value if isinstance(value, str) else __import__("json").dumps(value)

    def get_json(self, url: str) -> Any:
        value = self._value(url)
        return __import__("json").loads(value) if isinstance(value, str) else value


class NativeAdapterTests(unittest.TestCase):
    def test_workbench_splits_analysis_ids_and_prefers_declared_polarity(self) -> None:
        client = FakeClient(
            {
                "/summary/json": "study_id\tST1\nstudy_title\tMixed polarity\nstudy_summary\tPositive and negative LC-MS\n",
                "/analysis/json": (
                    "analysis_id\tAN1\nanalysis_summary\tNormal phase POSITIVE ION MODE\n"
                    "analysis_type\tMS\nchromatography_type\tNormal phase\n"
                    "ms_instrument_name\tTOF\nion_mode\tPOSITIVE\n\n"
                    "analysis_id\tAN2\nanalysis_summary\tNormal phase NEGATIVE ION MODE\n"
                    "analysis_type\tMS\nchromatography_type\tNormal phase\n"
                    "ms_instrument_name\tTOF\nion_mode\tNEGATIVE\n"
                ),
                "/factors/json": "local_sample_id\tS1\nfactors\tGroup:Control\nraw_data\tS1.raw\n",
                "SetupRawDataDownload": (
                    '<a href="/AN1_pos.zip">AN1_pos.zip</a> <b>(1M)</b> (Checksum:abcd)\n'
                    '<a href="/AN2_neg.zip">AN2_neg.zip</a> <b>(2M)</b> (Checksum:ef12)'
                ),
                "DRCCMetadata.php": "<html>LC-MS untargeted study</html>",
            }
        )
        payload = MetabolomicsWorkbenchAdapter(client).inspect_metadata("ST1")
        self.assertEqual(["Positive", "Negative"], [unit["ion_mode"] for unit in payload["analysis_units"]])
        self.assertEqual(["AN1_pos.zip"], [item["name"] for item in payload["analysis_units"][0]["files"]])
        self.assertEqual("Normal phase", payload["analysis_units"][0]["chromatography"])

    def test_metabolights_uses_each_assay_as_an_analysis_unit(self) -> None:
        study = {
            "mtblsStudy": {"datasetLicense": "CC0"},
            "isaInvestigation": {"studies": [{"title": "Root metabolomics", "description": "Untargeted"}]},
        }
        listing = {"data": {"assays": [{"filename": "a_pos.txt"}, {"filename": "a_neg.txt"}]}}
        client = FakeClient(
            {
                "/assays": listing,
                "/a_pos.txt": {"data": {"rows": [{"Sample Name": "P1", "Parameter Value[Scan polarity]": "positive", "Raw Spectral Data File": "P1.raw"}]}},
                "/a_neg.txt": {"data": {"rows": [{"Sample Name": "N1", "Parameter Value[Scan polarity]": "negative", "Raw Spectral Data File": "N1.raw"}]}},
                "/studies/MTBLS1": study,
                "/FILES/": "",
                "/s_MTBLS1.txt": "",
            }
        )
        payload = MetaboLightsAdapter(client).inspect_metadata("MTBLS1")
        self.assertEqual(2, len(payload["analysis_units"]))
        self.assertEqual({"Positive", "Negative"}, {unit["ion_mode"] for unit in payload["analysis_units"]})

    def test_mbpost_groups_files_by_analytical_condition(self) -> None:
        project = {"location": "MPST1.0", "title": "Lipidomics", "modifiedAt": "2026-01-01"}
        listing = {
            "list": [
                {"id": "1", "name": "p.raw", "size": 10, "type": "raw"},
                {"id": "2", "name": "n.raw", "size": 11, "type": "raw"},
            ]
        }
        def detail(polarity: str) -> dict[str, Any]:
            return {"presets": [{"category": "analyticalCondition", "presets": [
                {"key": "presetName", "value": f"LC {polarity}"},
                {"key": "methodType", "value": "LC-MS"},
                {"key": "chromatographyType", "value": "LC_Reversed phase"},
                {"key": "polarity", "value": polarity},
                {"key": "instrumentMode", "value": "DDA-high res."},
            ]}]}
        client = FakeClient({"files/1": detail("Positive"), "files/2": detail("Negative"), "files?": listing, "/api/projects/MPST1": project})
        payload = MbPostAdapter(client).inspect_metadata("MPST1")
        self.assertEqual(2, len(payload["analysis_units"]))
        self.assertEqual({"Positive", "Negative"}, {unit["ion_mode"] for unit in payload["analysis_units"]})

    def test_metabobank_splits_sdrf_technical_signatures(self) -> None:
        entry = {
            "identifier": "MTBKS1",
            "properties": {"Study Title": ["Mixed assay"], "Comment[Study type]": ["untargeted"]},
            "distribution": [{"encodingFormat": "DATA", "contentUrl": "https://example/data/"}],
        }
        sdrf = (
            "Source Name\tSample Name\tParameter Value[Scan polarity]\tParameter Value[Instrument]\tRaw Data File\n"
            "S1\tS1\tpositive\tLC-QTOF\traw/S1.raw/\n"
            "S2\tS2\tnegative\tLC-QTOF\traw/S2.raw/\n"
        )
        filelist = (
            "Type\tName\tSize\tMD5\n"
            "file\traw/S1.raw/a.dat\t10\taaaa\n"
            "file\traw/S2.raw/a.dat\t20\tbbbb\n"
        )
        client = FakeClient({"entries/metabobank/MTBKS1": entry, ".filelist.txt": filelist, ".sdrf.txt": sdrf})
        payload = MetaboBankAdapter(client).inspect_metadata("MTBKS1")
        self.assertEqual(2, len(payload["analysis_units"]))
        self.assertEqual({"Positive", "Negative"}, {unit["ion_mode"] for unit in payload["analysis_units"]})
        self.assertEqual([1, 1], [len(unit["sample_metadata"]) for unit in payload["analysis_units"]])


if __name__ == "__main__":
    unittest.main()
