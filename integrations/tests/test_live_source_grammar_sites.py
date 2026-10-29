"""Site-by-path resolution operations of the closed request grammar are GET-only
and accept only a SharePoint host plus one plain site name (directive e2d2f62b791d)."""

import unittest

from integrations import live_source_grammar as grammar
from integrations.graph_read import resolve_site_drive
from integrations.live_source_guard import (ReadOnlyGuardError, SourceUnavailable,
                                            check_request)

HOST = "petrasoap.sharepoint.com"
SITE = ("petrasoap.sharepoint.com,22222222-0000-0000-0000-000000000001,"
        "22222222-0000-0000-0000-000000000002")
G = "https://graph.microsoft.com"


class SiteOperationGrammarTests(unittest.TestCase):
    def test_built_urls_parse_back_and_pass_the_guard_as_get(self):
        by_path = grammar.url("graph.site_by_path", {"$select": "id"}, host=HOST, name="AGENTS")
        self.assertEqual(by_path, f"{G}/v1.0/sites/{HOST}:/sites/AGENTS?%24select=id")
        drive = grammar.url("graph.site_drive", {"$select": "id"}, site=SITE)
        self.assertEqual(drive, f"{G}/v1.0/sites/{SITE}/drive?%24select=id")
        for url in (by_path, drive):
            self.assertEqual(check_request("GET", url, environ={}), "graph.microsoft.com")
            for method in ("POST", "PUT", "PATCH", "DELETE", "HEAD"):
                with self.assertRaises(ReadOnlyGuardError, msg=method):
                    check_request(method, url, environ={})

    def test_other_hosts_and_names_are_refused_before_io(self):
        for host in ("evil.example.com", "petrasoap.sharepoint.com.evil.com", "PETRASOAP",
                     "petrasoap.sharepoint.com:443", ""):
            with self.assertRaises(ValueError, msg=host):
                grammar.url("graph.site_by_path", host=host, name="AGENTS")
        for name in ("..", "AGENTS/drive", "AGENTS:", "A GENTS", "AGENTS%2Fx", "-x", "",
                     "a" * 129, "AGENTS/../Finance"):
            with self.assertRaises(ValueError, msg=name):
                grammar.url("graph.site_by_path", host=HOST, name=name)
        refused = [
            f"/v1.0/sites/evil.example.com:/sites/AGENTS",
            f"/v1.0/sites/{HOST}:/sites/AGENTS:",
            f"/v1.0/sites/{HOST}:/sites/AGENTS/drive",
            f"/v1.0/sites/{HOST}:/sites/AGENTS:/drive",
            f"/v1.0/sites/{HOST}:/sites/AGENTS:/drive/root:/Ontology UseCases:",
            f"/v1.0/sites/{HOST}:/teams/AGENTS",
            f"/v1.0/sites/{HOST}:/sites/A%2FB",
            f"/v1.0/sites/{HOST}:/sites/..",
            f"/v1.0/sites/{HOST}/drive",
            f"/v1.0/sites/{SITE}/drive/root",
            f"/v1.0/sites/{SITE}/drive/items/x/children",
        ]
        for path in refused:
            with self.assertRaises(ReadOnlyGuardError, msg=path):
                check_request("GET", G + path, environ={})
        # Same path on a host outside the read-only allowlist.
        with self.assertRaises(ReadOnlyGuardError):
            check_request("GET", f"https://{HOST}/v1.0/sites/{HOST}:/sites/AGENTS", environ={})


class _Api:
    def __init__(self, answers):
        self.answers, self.urls = list(answers), []

    def get_json(self, url, **_kw):
        check_request("GET", url, environ={})  # what SourceApi does before any I/O
        self.urls.append(url)
        return self.answers.pop(0)


class ResolveSiteDriveTests(unittest.TestCase):
    def test_resolves_the_default_drive_with_two_gets(self):
        api = _Api([{"id": SITE}, {"id": "b!agents-Drive1"}])
        self.assertEqual(resolve_site_drive(api, HOST, "AGENTS"), "b!agents-Drive1")
        self.assertEqual(api.urls, [f"{G}/v1.0/sites/{HOST}:/sites/AGENTS?%24select=id",
                                    f"{G}/v1.0/sites/{SITE}/drive?%24select=id"])

    def test_a_site_on_another_host_or_a_malformed_id_is_refused(self):
        for answer in ({"id": SITE.replace("petrasoap", "contoso")}, {"id": "x"}, {}, []):
            api = _Api([answer, {"id": "b!d"}])
            with self.assertRaises(SourceUnavailable, msg=answer):
                resolve_site_drive(api, HOST, "AGENTS")
            self.assertEqual(len(api.urls), 1)
        api = _Api([{"id": SITE}, {"id": "b!/../x"}])
        with self.assertRaises(SourceUnavailable):
            resolve_site_drive(api, HOST, "AGENTS")


if __name__ == "__main__":
    unittest.main()
