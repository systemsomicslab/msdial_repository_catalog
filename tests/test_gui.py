from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from msdial_repository_catalog.gui_server import CatalogGuiApplication, CatalogGuiServer
from msdial_repository_catalog.normalize import project_to_study
from msdial_repository_catalog.storage import Catalog


def gui_project() -> dict:
    return {
        "repository": "mb_post",
        "accession": "MPST-GUI-TEST",
        "title": "Inflammatory mouse plasma lipidomics",
        "description": "Aging and inflammation study",
        "public_url": "https://example.org/MPST-GUI-TEST",
        "analysis_units": [
            {
                "source_subrecord_id": "negative-dda",
                "label": "Negative DDA assay",
                "separation": "LC-MS",
                "chromatography": "Reversed phase",
                "ion_mode": "Negative",
                "acquisition_mode": "DDA",
                "ion_mobility": "Disabled",
                "instrument": "QTOF",
                "target_omics": "Lipidomics",
                "untargeted": True,
                "review_status": "reviewed",
                "sample_metadata": [
                    {
                        "sample_id": "mouse_1",
                        "source_name": "mouse_1",
                        "raw_file": "mouse_1.raw",
                        "values": {"Species": "Mus musculus", "Condition": "inflammation"},
                    }
                ],
                "files": [{"name": "mouse_1.raw", "size_bytes": 2048}],
            }
        ],
    }


class GuiApplicationTests(unittest.TestCase):
    def test_overview_search_and_unit_detail(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "catalog.sqlite"
            with Catalog(database) as catalog:
                catalog.ingest_study(project_to_study(gui_project()))
            application = CatalogGuiApplication(database)
            overview = application.status()
            self.assertEqual(1, overview["studies"])
            self.assertEqual("mb_post", overview["repositories"][0]["repository"])
            result = application.search({"biological_context": "inflammation"})
            self.assertEqual(1, result["count"])
            unit = application.unit(result["matches"][0]["unit_id"])
            self.assertEqual("Negative", unit["ion_mode"])
            self.assertEqual("mouse_1", unit["samples"][0]["sample_id"])

    def test_http_api_and_packaged_index(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "catalog.sqlite"
            with Catalog(database) as catalog:
                catalog.ingest_study(project_to_study(gui_project()))
            server = CatalogGuiServer(("127.0.0.1", 0), CatalogGuiApplication(database))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                root = f"http://127.0.0.1:{server.server_port}"
                with urllib.request.urlopen(root + "/", timeout=5) as response:
                    page = response.read().decode("utf-8")
                self.assertIn("MS-DIAL Repository Catalog", page)
                with urllib.request.urlopen(root + "/api/status", timeout=5) as response:
                    status = json.load(response)
                self.assertEqual(1, status["analysis_units"])
                with urllib.request.urlopen(
                    root + "/api/search?ion_mode=Negative&target_omics=Lipidomics", timeout=5
                ) as response:
                    search = json.load(response)
                self.assertEqual(1, search["count"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
