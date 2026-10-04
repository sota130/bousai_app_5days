import unittest

from app import parse_area_warnings


def warning_report(report_datetime, aomori_kinds, other_kinds=None):
    return {
        "reportDatetime": report_datetime,
        "warning": {
            "class20Items": [
                {"areaCode": "0220100", "kinds": aomori_kinds},
                {"areaCode": "0220200", "kinds": other_kinds or []},
            ]
        },
    }


class ParseAreaWarningsTests(unittest.TestCase):
    def test_latest_report_clears_warning_from_older_report(self):
        reports = [
            warning_report(
                "2026-10-04T10:00:00+09:00",
                [{"code": "14", "status": "発表"}],
            ),
            warning_report(
                "2026-10-04T11:00:00+09:00",
                [{"status": "発表警報・注意報はなし"}],
            ),
        ]

        warnings, report_datetime = parse_area_warnings(reports)

        self.assertEqual(warnings, [])
        self.assertEqual(report_datetime, "2026-10-04T11:00:00+09:00")

    def test_extracts_active_warning_only_for_aomori_and_uses_source_name(self):
        reports = [
            warning_report(
                "2026-10-04T11:00:00+09:00",
                [{"code": "14", "name": "雷注意報", "status": "継続"}],
                [{"code": "05", "status": "発表"}],
            )
        ]

        warnings, _ = parse_area_warnings(reports)

        self.assertEqual(
            warnings,
            [{"name": "雷注意報", "code": "14", "status": "継続"}],
        )


if __name__ == "__main__":
    unittest.main()
