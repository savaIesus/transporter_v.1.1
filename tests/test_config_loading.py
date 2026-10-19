"""Загрузка и валидация конфигурационных файлов.

Проверяются ``config.archive_config``, ``config.calibration_loader`` и
``config.camera_mapping``: defaults, нормализация значений, атомарная
запись и все ошибки валидации, которые выводятся на старте.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from config.archive_config import (
    DEFAULTS,
    load_archive_config,
    normalise_archive_config,
    save_archive_config,
)
from config.calibration_loader import (
    DEFAULTS as CALIBRATION_DEFAULTS,
    OPTIONAL_DEFAULTS,
    load_calibration,
)
from config.camera_mapping import (
    REQUIRED_ROLES,
    load_camera_mapping,
    validate_camera_mapping,
)


class ArchiveConfigNormaliseTest(unittest.TestCase):
    def test_defaults_when_none(self):
        self.assertEqual(normalise_archive_config(None), DEFAULTS)

    def test_only_root_path_is_configurable(self):
        result = normalise_archive_config({
            "enabled": False,
            "root_path": "~/archive2",
            "jpeg_quality": 70,
            "compress_on_shutdown": False,
            "delete_original_after_zip": False,
        })
        self.assertEqual(
            result,
            {"root_path": os.path.expanduser("~/archive2")},
        )

    def test_root_path_expands_vars(self):
        result = normalise_archive_config({"root_path": "$HOME/archive_x"})
        self.assertEqual(
            result["root_path"], os.path.expandvars("$HOME/archive_x"),
        )

    def test_empty_root_falls_back_to_default(self):
        result = normalise_archive_config({"root_path": "   "})
        self.assertEqual(result["root_path"], DEFAULTS["root_path"])


class ArchiveConfigFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def path(self, name):
        return os.path.join(self.tmp.name, name)

    def test_load_missing_file_creates_defaults(self):
        path = self.path("missing_archive.json")
        result = load_archive_config(path)
        self.assertEqual(result, DEFAULTS)
        with open(path, encoding="utf-8") as stream:
            self.assertEqual(json.load(stream), DEFAULTS)

    def test_load_invalid_json_falls_back_to_defaults(self):
        path = self.path("bad.json")
        with open(path, "w", encoding="utf-8") as stream:
            stream.write("{not json")
        self.assertEqual(load_archive_config(path), DEFAULTS)

    def test_load_migrates_legacy_switches_to_root_only(self):
        path = self.path("legacy.json")
        with open(path, "w", encoding="utf-8") as stream:
            json.dump({
                "root_path": "archive2",
                "enabled": False,
                "jpeg_quality": 75,
                "compress_on_shutdown": False,
            }, stream)

        self.assertEqual(load_archive_config(path), {"root_path": "archive2"})
        with open(path, encoding="utf-8") as stream:
            self.assertEqual(json.load(stream), {"root_path": "archive2"})

    def test_save_is_atomic_and_normalised(self):
        path = self.path("save.json")
        result = save_archive_config(path, {
            "root_path": "archive3", "enabled": False,
        })
        self.assertEqual(result, {"root_path": "archive3"})
        with open(path, encoding="utf-8") as stream:
            self.assertEqual(json.load(stream), result)
        self.assertFalse(os.path.exists(path + ".tmp"))


def _valid_calibration(**overrides):
    data = {**CALIBRATION_DEFAULTS, **OPTIONAL_DEFAULTS}
    data.update(overrides)
    return data


class CalibrationLoaderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def path(self, name):
        return os.path.join(self.tmp.name, name)

    def write(self, data):
        path = self.path("calibration.json")
        with open(path, "w", encoding="utf-8") as stream:
            json.dump(data, stream)
        return path

    def test_load_valid_calibration(self):
        path = self.write(_valid_calibration())
        result = load_calibration(path)
        self.assertEqual(result["conveyor_speed"], CALIBRATION_DEFAULTS["conveyor_speed"])
        self.assertEqual(result["settle_time"], OPTIONAL_DEFAULTS["settle_time"])

    def test_load_optional_timings_filled(self):
        path = self.write(_valid_calibration())
        result = load_calibration(path)
        self.assertIn("settle_time", result)
        self.assertIn("stage_trace_time", result)
        self.assertIn("review_time", result)

    def test_missing_file_raises(self):
        with self.assertRaisesRegex(RuntimeError, "не найден"):
            load_calibration(self.path("nope.json"))

    def test_invalid_json_raises(self):
        with open(self.path("calibration.json"), "w", encoding="utf-8") as stream:
            stream.write("{broken")
        with self.assertRaisesRegex(RuntimeError, "Ошибка чтения"):
            load_calibration(self.path("calibration.json"))

    def test_missing_required_field_raises(self):
        data = _valid_calibration()
        del data["conveyor_speed"]
        with self.assertRaisesRegex(ValueError, "missing="):
            load_calibration(self.write(data))

    def test_extra_field_raises(self):
        data = _valid_calibration(extra_field=1)
        with self.assertRaisesRegex(ValueError, "extra="):
            load_calibration(self.write(data))

    def test_float_where_int_expected_raises(self):
        with self.assertRaisesRegex(ValueError, "должен быть int"):
            load_calibration(self.write(_valid_calibration(conveyor_speed=1.5)))

    def test_non_finite_float_raises(self):
        with self.assertRaisesRegex(ValueError, "конечным"):
            load_calibration(self.write(_valid_calibration(settle_time=float("inf"))))

    def test_non_positive_parameter_raises(self):
        with self.assertRaisesRegex(ValueError, "> 0"):
            load_calibration(self.write(_valid_calibration(axis_speed=0)))

    def test_jog_hold_steps_range(self):
        with self.assertRaisesRegex(ValueError, "jog_hold_steps"):
            load_calibration(self.write(_valid_calibration(jog_hold_steps=5)))

    def test_dist2_positions_equal_raises(self):
        with self.assertRaisesRegex(ValueError, "различаться"):
            load_calibration(self.write(_valid_calibration(
                dist2_bad_position=10, dist2_cleanup_position=10,
            )))

    def test_dist2_negative_raises(self):
        with self.assertRaisesRegex(ValueError, "отрицательными"):
            load_calibration(self.write(_valid_calibration(dist2_bad_position=-1)))

    def test_settle_time_range(self):
        with self.assertRaisesRegex(ValueError, "settle_time"):
            load_calibration(self.write(_valid_calibration(settle_time=9.0)))

    def test_stage_trace_time_range(self):
        with self.assertRaisesRegex(ValueError, "stage_trace_time"):
            load_calibration(self.write(_valid_calibration(stage_trace_time=-1.0)))

    def test_review_time_range(self):
        with self.assertRaisesRegex(ValueError, "review_time"):
            load_calibration(self.write(_valid_calibration(review_time=99.0)))

    def test_not_a_dict_raises(self):
        with self.assertRaisesRegex(ValueError, "объект"):
            load_calibration(self.write([1, 2, 3]))


def _valid_mapping(**overrides):
    mapping = {role: index for index, role in enumerate(sorted(REQUIRED_ROLES))}
    mapping.update(overrides)
    return mapping


class CameraMappingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def path(self, name):
        return os.path.join(self.tmp.name, name)

    def test_validate_accepts_complete_mapping(self):
        mapping = _valid_mapping()
        self.assertEqual(validate_camera_mapping(mapping), mapping)

    def test_validate_rejects_non_dict(self):
        with self.assertRaisesRegex(ValueError, "объект"):
            validate_camera_mapping([1, 2])

    def test_validate_rejects_missing_roles(self):
        mapping = _valid_mapping()
        del mapping["RIGHT"]
        with self.assertRaisesRegex(ValueError, "missing="):
            validate_camera_mapping(mapping)

    def test_validate_rejects_extra_roles(self):
        mapping = _valid_mapping(EXTRA=7)
        with self.assertRaisesRegex(ValueError, "extra="):
            validate_camera_mapping(mapping)

    def test_validate_rejects_negative_id(self):
        with self.assertRaisesRegex(ValueError, "неотрицательными"):
            validate_camera_mapping(_valid_mapping(RIGHT=-1))

    def test_validate_rejects_non_int_id(self):
        with self.assertRaisesRegex(ValueError, "неотрицательными"):
            validate_camera_mapping(_valid_mapping(RIGHT="2"))

    def test_validate_rejects_duplicate_ids(self):
        mapping = _valid_mapping()
        mapping["RIGHT"] = mapping["LEFT"]
        with self.assertRaisesRegex(ValueError, "уникальными"):
            validate_camera_mapping(mapping)

    def test_load_missing_file_raises(self):
        with self.assertRaisesRegex(RuntimeError, "не найден"):
            load_camera_mapping(self.path("mapping.json"))

    def test_load_invalid_json_raises(self):
        with open(self.path("mapping.json"), "w", encoding="utf-8") as stream:
            stream.write("{bad")
        with self.assertRaisesRegex(RuntimeError, "Ошибка чтения"):
            load_camera_mapping(self.path("mapping.json"))

    def test_load_valid_mapping(self):
        path = self.path("mapping.json")
        with open(path, "w", encoding="utf-8") as stream:
            json.dump(_valid_mapping(), stream)
        self.assertEqual(load_camera_mapping(path), _valid_mapping())


if __name__ == "__main__":
    unittest.main()
