"""Planet type inference treats zero-filled spreadsheet cells as absent resources.

Run in the application container; all data lives in an in-memory SQLite database.
"""
import sqlite3
import sys
import unittest

sys.path.insert(0, ".")

from app.planetary import (  # noqa: E402
    PLANET_P0_MAP, P0_COLUMNS, _p0_col, _parse_planet_rows, _write_planet_rows,
)


class PlanetImportTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.addCleanup(self.con.close)
        self.con.row_factory = sqlite3.Row
        self.con.execute("CREATE TABLE system_geo (system TEXT, constellation TEXT)")
        self.con.execute("INSERT INTO system_geo VALUES ('Test', 'Fixture')")
        resources = ", ".join(f"{col} INTEGER NOT NULL DEFAULT 0" for col in P0_COLUMNS)
        self.con.execute(f"""CREATE TABLE pp_planets (
            system TEXT, planet_num INTEGER, planet_type TEXT, constellation TEXT,
            {resources}, UNIQUE(system, planet_num))""")
        self.names = sorted({name for names in PLANET_P0_MAP.values() for name in names})

    def paste(self, readings, *, sep="\t", explicit_type=None):
        headers = ["System", "Planet", *self.names]
        values = ["Test", "1", *(readings.get(name, "") for name in self.names)]
        if explicit_type is not None:
            headers.append("Type")
            values.append(explicit_type)
        return sep.join(headers) + "\n" + sep.join(values)

    def test_zero_filled_csv_and_spreadsheets_identify_every_planet_type(self):
        for sep in ("\t", ","):
            for zero in ("0", "0.0", "0e0", " 0.00 ", ""):
                for planet_type, resources in PLANET_P0_MAP.items():
                    with self.subTest(sep=sep, zero=zero, planet_type=planet_type):
                        readings = dict.fromkeys(self.names, zero)
                        readings.update(dict.fromkeys(resources, "42"))
                        rows, skipped, errors = _parse_planet_rows(self.paste(readings, sep=sep), self.con)
                        self.assertEqual((skipped, errors), (0, []))
                        self.assertEqual(len(rows), 1)
                        self.assertEqual(rows[0]["planet_type"], planet_type)
                        self.assertEqual(rows[0]["constellation"], "Fixture")
                        for name in self.names:
                            self.assertEqual(rows[0][_p0_col(name)], 42 if name in resources else 0)

    def test_positive_density_identifies_type_before_rounding_for_storage(self):
        readings = dict.fromkeys(self.names, "0")
        readings.update(dict.fromkeys(PLANET_P0_MAP["Temperate"], "42"))
        readings["Autotrophs"] = "0.1"  # Temperate's distinguishing resource.
        rows, skipped, errors = _parse_planet_rows(self.paste(readings), self.con)
        self.assertEqual((skipped, errors), (0, []))
        self.assertEqual(rows[0]["planet_type"], "Temperate")
        self.assertEqual(rows[0]["autotrophs"], 0)

    def test_zeroed_missing_resource_keeps_existing_ambiguity_warning(self):
        readings = dict.fromkeys(self.names, "0")
        readings.update(dict.fromkeys(PLANET_P0_MAP["Storm"], "42"))
        readings["Suspended Plasma"] = "0"
        rows, skipped, errors = _parse_planet_rows(self.paste(readings), self.con)
        self.assertEqual(skipped, 0)
        self.assertEqual(rows[0]["planet_type"], "Gas")
        self.assertEqual(len(errors), 1)
        self.assertIn("ambiguous type (Gas/Storm)", errors[0])

    def test_all_zero_row_needs_explicit_type(self):
        readings = dict.fromkeys(self.names, "0")
        rows, skipped, errors = _parse_planet_rows(self.paste(readings), self.con)
        self.assertEqual((rows, skipped), ([], 1))
        self.assertIn("Could not determine planet type", errors[0])
        rows, skipped, errors = _parse_planet_rows(
            self.paste(readings, explicit_type="Barren"), self.con)
        self.assertEqual((skipped, errors), (0, []))
        self.assertEqual(rows[0]["planet_type"], "Barren")
        self.assertTrue(all(rows[0][col] == 0 for col in P0_COLUMNS))

    def test_zero_filled_import_merges_without_erasing_known_densities(self):
        readings = dict.fromkeys(self.names, "0")
        readings.update(dict.fromkeys(PLANET_P0_MAP["Barren"], "42"))
        rows, skipped, errors = _parse_planet_rows(self.paste(readings), self.con)
        self.assertEqual((skipped, errors), (0, []))
        self.assertEqual(_write_planet_rows(self.con, rows), (1, 0))
        readings["Aqueous Liquids"] = "0.0"
        readings["Noble Metals"] = "75"
        rows, skipped, errors = _parse_planet_rows(self.paste(readings), self.con)
        self.assertEqual((skipped, errors), (0, []))
        self.assertEqual(_write_planet_rows(self.con, rows), (1, 0))
        stored = self.con.execute("SELECT * FROM pp_planets").fetchall()
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["planet_type"], "Barren")
        self.assertEqual(stored[0]["aqueous_liquids"], 42)
        self.assertEqual(stored[0]["noble_metals"], 75)

    def test_explicit_type_and_compact_format_still_work(self):
        readings = dict.fromkeys(self.names, "0")
        readings.update(dict.fromkeys(PLANET_P0_MAP["Storm"], "42"))
        readings["Suspended Plasma"] = "0"
        rows, skipped, errors = _parse_planet_rows(
            self.paste(readings, explicit_type="Storm"), self.con)
        self.assertEqual((skipped, errors), (0, []))
        self.assertEqual(rows[0]["planet_type"], "Storm")
        rows, skipped, errors = _parse_planet_rows("Test,1,Barren,,42,0,42,42,42", self.con)
        self.assertEqual((skipped, errors), (0, []))
        self.assertEqual(rows[0]["planet_type"], "Barren")
        self.assertEqual(rows[0]["base_metals"], 0)


if __name__ == "__main__":
    unittest.main()
