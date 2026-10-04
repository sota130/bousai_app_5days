import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import app as application
from app import app, get_disaster_information


class HomePageTests(unittest.TestCase):
    def test_home_page_summarizes_disaster_information_and_navigation(self):
        response = app.test_client().get("/")

        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn("災害情報一覧", page)
        self.assertIn("緊急のお知らせ", page)
        self.assertIn("情報の種類で絞り込む", page)
        self.assertIn("詳細を見る", page)
        self.assertIn("更新日時", page)
        self.assertIn("600000", page)
        self.assertIn("現在地を表示", page)
        self.assertIn("開設中", page)
        self.assertIn("開設前", page)
        self.assertIn("状況未登録", page)
        self.assertIn("避難所マップ", page)
        self.assertIn("気象情報", page)
        self.assertIn('aria-current="page"', page)
        self.assertIn('href="/shelter_search"', page)
        self.assertIn('href="/board"', page)
        self.assertIn('id="main-menu"', page)

    def test_home_page_shows_resident_notices_but_not_internal_instructions(self):
        response = app.test_client().get("/")
        page = response.get_data(as_text=True)

        self.assertIn("B避難所へ避難してください", page)
        self.assertIn("土砂災害の危険があるため避難してください", page)
        self.assertNotIn("A地区の現地確認", page)
        self.assertNotIn("○○橋の通行止め確認", page)

    def test_disaster_information_combines_sources_and_sorts_newest_first(self):
        with (
            patch(
                "app.get_weather_warnings",
                return_value={
                    "area_name": "青森市",
                    "warnings": [{"name": "大雨警報", "status": "発表"}],
                    "report_time": "2026年10月04日 10:00",
                },
            ),
            patch(
                "app.load_json",
                return_value=[
                    {
                        "target": "住民",
                        "content": "避難してください",
                        "shelter": "青森小学校",
                        "status": "発信中",
                        "created_at": "2026年10月04日 11:00",
                    },
                    {
                        "target": "防災課",
                        "content": "職員向け情報",
                        "created_at": "2026年10月04日 12:00",
                    },
                ],
            ),
            patch("app.get_japan_time", return_value="2026年10月04日 12:01"),
        ):
            result = get_disaster_information()

        self.assertEqual(
            [item["title"] for item in result["items"]],
            ["避難してください", "大雨警報"],
        )
        self.assertEqual(result["items"][0]["category"], "evacuation")
        self.assertTrue(result["items"][0]["active"])
        self.assertEqual(result["items"][0]["shelter"], "青森小学校")
        self.assertEqual(result["items"][1]["category"], "weather")
        self.assertFalse(result["weather_error"])

    def test_disaster_information_api_returns_list_and_update_time(self):
        payload = {
            "items": [{"category": "notice", "title": "試験のお知らせ"}],
            "updated_at": "2026年10月04日 12:01",
            "weather_error": False,
        }
        with patch("app.get_disaster_information", return_value=payload):
            response = app.test_client().get("/api/disaster_information")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), payload)

    def test_shelter_registration_saves_coordinates_and_open_status(self):
        shelters = []
        with tempfile.TemporaryDirectory() as temp_dir:
            data_file = Path(temp_dir) / "shelters.json"
            with patch.object(application, "shelters", shelters), patch.object(
                application, "DATA_FILE", str(data_file)
            ):
                client = app.test_client()
                with client.session_transaction() as session:
                    session["logged_in"] = True
                response = client.post(
                    "/shelter_register",
                    data={
                        "action": "register",
                        "name": "青森市民センター",
                        "address": "青森県青森市中央1丁目",
                        "status": "開設中",
                        "capacity": "300",
                        "disaster_types": ["地震", "津波"],
                        "facilities": ["ペット可", "バリアフリー"],
                        "latitude": "40.8244",
                        "longitude": "140.7400",
                    },
                )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(shelters[0]["address"], "青森県青森市中央1丁目")
            self.assertEqual(shelters[0]["status"], "開設中")
            self.assertEqual(shelters[0]["capacity"], 300)
            self.assertEqual(shelters[0]["disaster_types"], ["地震", "津波"])
            self.assertEqual(shelters[0]["facilities"], ["ペット可", "バリアフリー"])
            self.assertEqual(shelters[0]["latitude"], 40.8244)
            self.assertEqual(shelters[0]["longitude"], 140.74)
            self.assertEqual(json.loads(data_file.read_text(encoding="utf-8")), shelters)

    def test_shelter_registration_rejects_partial_coordinates(self):
        with patch.object(application, "shelters", []):
            client = app.test_client()
            with client.session_transaction() as session:
                session["logged_in"] = True
            response = client.post(
                "/shelter_register",
                data={
                    "action": "register",
                    "name": "青森市民センター",
                    "address": "青森県青森市中央1丁目",
                    "status": "開設中",
                    "latitude": "40.8244",
                    "longitude": "",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("緯度と経度の両方".encode(), response.data)

    def test_shelter_status_update_is_saved_for_map_data(self):
        shelters = [{"id": 1, "name": "市民体育館", "status": "開設前"}]
        with tempfile.TemporaryDirectory() as temp_dir:
            data_file = Path(temp_dir) / "shelters.json"
            with patch.object(application, "shelters", shelters), patch.object(
                application, "DATA_FILE", str(data_file)
            ):
                client = app.test_client()
                with client.session_transaction() as session:
                    session["logged_in"] = True
                response = client.post(
                    "/shelter_register",
                    data={
                        "action": "update_status",
                        "shelter_id": "1",
                        "name": "市民体育館",
                        "address": "青森県青森市",
                        "status": "開設中",
                        "capacity": "120",
                        "disaster_types": ["地震"],
                        "facilities": ["ペット可"],
                        "latitude": "40.8",
                        "longitude": "140.7",
                    },
                )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(shelters[0]["status"], "開設中")
            self.assertEqual(shelters[0]["address"], "青森県青森市")
            self.assertEqual(shelters[0]["capacity"], 120)
            self.assertEqual(shelters[0]["latitude"], 40.8)
            self.assertEqual(shelters[0]["longitude"], 140.7)
            self.assertEqual(json.loads(data_file.read_text(encoding="utf-8")), shelters)

    def test_geocoding_api_returns_location_candidates(self):
        candidates = [{
            "label": "青森県青森市中央一丁目",
            "latitude": 40.8244,
            "longitude": 140.74,
        }]
        with patch("app.geocode_address", return_value=candidates):
            client = app.test_client()
            with client.session_transaction() as session:
                session["logged_in"] = True
            response = client.get(
                "/api/geocode",
                query_string={"address": "青森県青森市"}
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"candidates": candidates})

    def test_geocoding_api_reports_unmatched_address(self):
        with patch("app.geocode_address", return_value=[]):
            client = app.test_client()
            with client.session_transaction() as session:
                session["logged_in"] = True
            response = client.get("/api/geocode?address=住所のない場所")

        self.assertEqual(response.status_code, 404)
        self.assertIn("見つかりません", response.get_json()["error"])

    def test_shelter_detail_listing_includes_new_fields(self):
        shelters = [{
            "id": 10,
            "name": "青森市民センター",
            "address": "青森県青森市中央1丁目",
            "capacity": 300,
            "status": "開設中",
            "disaster_types": ["地震", "津波"],
            "facilities": ["ペット可"],
        }]
        with patch.object(application, "shelters", shelters):
            response = app.test_client().get("/all_shelters")

        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn("青森県青森市中央1丁目", page)
        self.assertIn("300人", page)
        self.assertIn("地震、津波", page)
        self.assertIn("ペット可", page)

    def test_resident_instruction_registration_is_reflected_as_urgent_home_information(self):
        instructions = []
        with tempfile.TemporaryDirectory() as temp_dir:
            data_file = Path(temp_dir) / "instructions.json"
            with patch.object(application, "instructions", instructions), patch.object(
                application, "INSTRUCTIONS_FILE", str(data_file)
            ), patch(
                "app.get_weather_warnings",
                return_value={"warnings": [], "area_name": "青森市"},
            ):
                client = app.test_client()
                with client.session_transaction() as session:
                    session["logged_in"] = True
                response = client.post(
                    "/board",
                    data={
                        "action": "register",
                        "content": "高台へ避難してください",
                        "shelter": "青森市民センター",
                        "priority": "高",
                    },
                )
                board_page = response.get_data(as_text=True)
                home_page = client.get("/").get_data(as_text=True)
                payload = client.get("/api/disaster_information").get_json()

            self.assertEqual(response.status_code, 200)
            self.assertIn("instruction is-urgent", board_page)
            self.assertIn("disaster-priority is-high", home_page)
            self.assertEqual(len(payload["items"]), 1)
            self.assertEqual(payload["items"][0]["category"], "evacuation")
            self.assertEqual(payload["items"][0]["priority"], "高")
            self.assertTrue(payload["items"][0]["urgent"])
            self.assertEqual(payload["items"][0]["shelter"], "青森市民センター")
            self.assertEqual(
                json.loads(data_file.read_text(encoding="utf-8")),
                instructions
            )

    def test_resident_instruction_can_be_resolved_and_no_longer_is_urgent(self):
        instructions = [{
            "id": 9,
            "target": "住民",
            "content": "避難してください",
            "status": "発信中",
            "priority": "高",
        }]
        with tempfile.TemporaryDirectory() as temp_dir:
            data_file = Path(temp_dir) / "instructions.json"
            data_file.write_text(json.dumps(instructions, ensure_ascii=False), encoding="utf-8")
            with patch.object(application, "instructions", instructions), patch.object(
                application, "INSTRUCTIONS_FILE", str(data_file)
            ):
                client = app.test_client()
                with client.session_transaction() as session:
                    session["logged_in"] = True
                response = client.post(
                    "/board",
                    data={
                        "action": "update_status",
                        "instruction_id": "9",
                        "status": "解除",
                    },
                )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(instructions[0]["status"], "解除")
            self.assertEqual(
                json.loads(data_file.read_text(encoding="utf-8"))[0]["status"],
                "解除"
            )


if __name__ == "__main__":
    unittest.main()
