import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import requests

import fetch_patrons


MISSING_NAME = object()


def member(member_id, name=MISSING_NAME, tier_ids=("gold",), status="active_patron"):
    attributes = {"patron_status": status, "email": "private@example.invalid"}
    if name is not MISSING_NAME:
        attributes["full_name"] = name
    return {
        "id": member_id,
        "type": "member",
        "attributes": attributes,
        "relationships": {
            "currently_entitled_tiers": {
                "data": [{"type": "tier", "id": tier_id} for tier_id in tier_ids]
            }
        },
    }


def tier(tier_id, title, amount):
    return {
        "id": tier_id,
        "type": "tier",
        "attributes": {"title": title, "amount_cents": amount},
    }


class FetchPatronsTests(unittest.TestCase):
    def run_main(self, members):
        tiers = {
            "bronze": {"id": "bronze", "title": "Bronze", "amount": 100},
            "silver": {"id": "silver", "title": "Silver", "amount": 500},
            "gold": {"id": "gold", "title": "Gold", "amount": 1000},
        }
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "subscribers.json"
            with contextlib.ExitStack() as stack:
                stack.enter_context(patch.object(fetch_patrons, "ACCESS_TOKEN", "test-only"))
                stack.enter_context(patch.object(fetch_patrons, "OUTPUT_FILE", str(output)))
                stack.enter_context(
                    patch.object(fetch_patrons, "get_campaign_id", return_value="test-campaign")
                )
                stack.enter_context(patch.object(
                    fetch_patrons,
                    "get_all_members_and_tiers",
                    return_value=(members, tiers),
                ))
                stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                fetch_patrons.main()
            return json.loads(output.read_text(encoding="utf-8"))

    def test_missing_name_preserves_eligible_count_and_highest_tier(self):
        result = self.run_main(
            [
                member("silver-member", "Zoë 李", ("silver",)),
                member("anonymous-member", tier_ids=("silver", "gold")),
                member("gold-member", "🙂 Céline", ("gold", "bronze")),
                member("lower-tier-member", "Lower tier", ("bronze",)),
                member("unknown-tier-member", "Unknown tier", ("unknown",)),
                member("no-tier-member", "No tier", ()),
            ]
        )

        self.assertEqual(
            result,
            [
                {"name": "Anonymous Supporter", "tier": "Gold"},
                {"name": "🙂 Céline", "tier": "Gold"},
                {"name": "Zoë 李", "tier": "Silver"},
            ],
        )
        for subscriber in result:
            self.assertEqual(set(subscriber), {"name", "tier"})

    def test_invalid_names_use_public_fallback_without_dropping_members(self):
        for name in (MISSING_NAME, None, "", " \t\n", 42, False, [], {"private": "name"}):
            with self.subTest(name=name):
                self.assertEqual(
                    self.run_main([member("invalid-name-member", name)]),
                    [{"name": "Anonymous Supporter", "tier": "Gold"}],
                )

    def test_pagination_keeps_requested_fields_on_cursor_only_next_link(self):
        first_member = member("first-member", "First", ("gold",))
        second_member = member("second-member", tier_ids=("silver",))
        inactive_member = member("inactive-member", "Inactive", status="declined_patron")
        pages = [
            {
                "data": [first_member, inactive_member],
                "included": [tier("gold", "Gold", 1000)],
                "links": {
                    "next": "https://www.patreon.com/api/oauth2/v2/campaigns/test-campaign/members?page%5Bcursor%5D=second-page"
                },
            },
            {
                "data": [second_member],
                "included": [tier("silver", "Silver", 500)],
                "links": {"next": None},
            },
        ]
        responses = [Mock(json=Mock(return_value=page)) for page in pages]

        with patch.object(fetch_patrons.requests, "get", side_effect=responses) as get:
            members, tiers = fetch_patrons.get_all_members_and_tiers("test-campaign")

        self.assertEqual(members, [first_member, second_member])
        self.assertEqual(
            tiers,
            {
                "gold": {"id": "gold", "title": "Gold", "amount": 1000},
                "silver": {"id": "silver", "title": "Silver", "amount": 500},
            },
        )
        self.assertEqual(get.call_count, 2)
        for index, call in enumerate(get.call_args_list):
            url = call.args[0] if call.args else call.kwargs["url"]
            effective_url = requests.Request("GET", url, params=call.kwargs.get("params")).prepare().url
            query = parse_qs(urlsplit(effective_url).query)
            with self.subTest(page=index + 1):
                self.assertIn("currently_entitled_tiers", query.get("include", [""])[0].split(","))
                self.assertTrue(
                    {"full_name", "patron_status"}.issubset(
                        set(query.get("fields[member]", [""])[0].split(","))
                    )
                )
                self.assertTrue(
                    {"title", "amount_cents"}.issubset(
                        set(query.get("fields[tier]", [""])[0].split(","))
                    )
                )
                if index == 1:
                    self.assertEqual(query.get("page[cursor]"), ["second-page"])
            responses[index].raise_for_status.assert_called_once_with()

    def test_pagination_does_not_duplicate_fields_already_in_next_link(self):
        next_url = requests.Request(
            "GET",
            "https://www.patreon.com/api/oauth2/v2/campaigns/test-campaign/members",
            params={
                "include": "currently_entitled_tiers",
                "fields[member]": "full_name,patron_status",
                "fields[tier]": "title,amount_cents",
                "page[count]": 100,
                "page[cursor]": "second-page",
            },
        ).prepare().url
        responses = [
            Mock(json=Mock(return_value={"data": [], "links": {"next": next_url}})),
            Mock(json=Mock(return_value={"data": [], "links": {"next": None}})),
        ]
        with patch.object(fetch_patrons.requests, "get", side_effect=responses) as get:
            self.assertEqual(fetch_patrons.get_all_members_and_tiers("test-campaign"), ([], {}))

        self.assertEqual(get.call_count, 2)
        call = get.call_args_list[1]
        url = call.args[0] if call.args else call.kwargs["url"]
        effective_url = requests.Request("GET", url, params=call.kwargs.get("params")).prepare().url
        query = parse_qs(urlsplit(effective_url).query)
        for key in ("include", "fields[member]", "fields[tier]", "page[count]", "page[cursor]"):
            with self.subTest(parameter=key):
                self.assertEqual(len(query.get(key, [])), 1)
        self.assertEqual(query["page[cursor]"], ["second-page"])

    def test_import_does_not_require_access_token(self):
        spec = importlib.util.spec_from_file_location("fetch_patrons_without_token", fetch_patrons.__file__)
        module = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ), patch.object(requests, "get") as get:
            os.environ.pop("PATREON_ACCESS_TOKEN", None)
            spec.loader.exec_module(module)
        self.assertIsNone(module.ACCESS_TOKEN)
        get.assert_not_called()

    def test_main_still_rejects_missing_access_token_before_fetching(self):
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(fetch_patrons, "ACCESS_TOKEN", None))
            get_campaign_id = stack.enter_context(patch.object(fetch_patrons, "get_campaign_id"))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            with self.assertRaises(SystemExit) as raised:
                fetch_patrons.main()
        self.assertEqual(raised.exception.code, 1)
        get_campaign_id.assert_not_called()


if __name__ == "__main__":
    unittest.main()
