"""Run: python3 -m unittest discover -s tests -v"""
import datetime as dt
import importlib.util
from pathlib import Path
import tempfile
import unittest

MODULE_PATH = Path(__file__).resolve().parents[1] / 'drivers' / 'driver.py'
spec = importlib.util.spec_from_file_location('driver', MODULE_PATH)
driver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(driver)


class DriverTest(unittest.TestCase):
    def test_date_math(self):
        initial, valid, lead = driver.time_case(dict(
            model='rrfs', domain='conus', verification='grid2obs_metar_tmp2m',
            init_date='20261115', cycle=0, forecast_hour=6))
        self.assertEqual((initial.strftime('%Y%m%d%H'), valid.strftime('%Y%m%d%H'), lead),
                         ('2026111500', '2026111506', 6))

    def test_cross_midnight(self):
        initial, valid, lead = driver.time_case(dict(
            model='rrfs', domain='conus', verification='grid2obs_metar_tmp2m',
            init_date='20261114', cycle=18, forecast_hour=12))
        self.assertEqual(valid.strftime('%Y%m%d%H'), '2026111506')

    def test_symlinks_and_protection(self):
        with tempfile.TemporaryDirectory() as folder:
            d = Path(folder)
            src = d / 'original.grib2'
            src.write_bytes(b'GRIBexample')
            target = d / 'input/rrfs.20261115/00/rrfs.t00z.2dfld.3km.f006.conus.grib2'
            driver.symlink_checked(src, target)
            self.assertTrue(target.is_symlink())
            self.assertEqual(target.read_bytes(), b'GRIBexample')
            driver.symlink_checked(src, target)
            other = d / 'different.grib2'
            other.touch()
            with self.assertRaises(RuntimeError):
                driver.symlink_checked(other, target)

    def test_source_template(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg = {'input': {'root': folder, 'template': 'archive/{init_date}/{cycle}/f{fhr:03d}.grib2'}}
            init = dt.datetime(2026, 11, 15, 0)
            valid = dt.datetime(2026, 11, 15, 6)
            source, rel = driver.source_paths(cfg, init, valid, 6)
            self.assertEqual(source, Path(folder) / 'archive/20261115/00/f006.grib2')
            self.assertEqual(str(rel), 'rrfs.20261115/00/rrfs.t00z.2dfld.3km.f006.conus.grib2')


if __name__ == '__main__':
    unittest.main()

